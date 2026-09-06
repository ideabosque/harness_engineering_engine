# -*- coding: utf-8 -*-
"""Async command executor for background command execution.

Manages long-running skill commands launched with ``runCommand(background=true)``.
Processes run detached in a background thread; output is captured to a temp file
and polled via the ``pollCommand(run_id)`` query.

This mirrors the async-tool pattern already proven in ``mcp_daemon_engine``'s
``async_execute_tool_function``: launch → return a handle → poll for status.

The registry is process-local (in-memory dict). This is intentional for v1:
the engine runs as a single process (gateway dispatch), and cross-instance
polling would require a shared store (Redis/DynamoDB) that the current
architecture doesn't have. If horizontal scaling is needed, the registry
can be backed by DynamoDB or Redis without changing the public API.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config


# ---------------------------------------------------------------------------
# Process registry — thread-safe, in-memory, process-local
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry: Dict[str, Dict[str, Any]] = {}

# Cleanup completed entries after this many seconds to bound memory.
_ENTRY_TTL_SECONDS = 3600  # 1 hour


def _register(
    run_id: str,
    process: subprocess.Popen,
    stdout_path: str,
    stderr_path: str,
    skill_name: str,
    argv: list,
    timeout_seconds: int,
    output_limit: int,
) -> None:
    with _registry_lock:
        _registry[run_id] = {
            "run_id": run_id,
            "process": process,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "skill_name": skill_name,
            "argv": argv,
            "timeout_seconds": timeout_seconds,
            "output_limit": output_limit,
            "status": "running",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "output_truncated_bytes": 0,
            "started_at": threading.Condition(),  # not used for time, just a marker
            "completed_at": None,
            "thread": None,
        }


def _unregister(run_id: str) -> None:
    with _registry_lock:
        _registry.pop(run_id, None)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _registry_lock:
        return _registry.get(run_id)


def _read_partial(path: str, limit: int) -> tuple[str, bool]:
    """Read up to ``limit`` bytes from ``path``. Returns (content, truncated)."""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if size > limit:
            return content[: limit // 2], True
        return content, False
    except FileNotFoundError:
        return "", False
    except Exception:
        return "", False


def _cleanup_stale_entries() -> None:
    """Remove completed entries older than _ENTRY_TTL_SECONDS."""
    import time

    now = time.time()
    with _registry_lock:
        stale = [
            run_id
            for run_id, entry in _registry.items()
            if entry.get("completed_at")
            and (now - entry["completed_at"]) > _ENTRY_TTL_SECONDS
        ]
        for run_id in stale:
            # Clean up temp files
            entry = _registry[run_id]
            for path in (entry.get("stdout_path"), entry.get("stderr_path")):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            _registry.pop(run_id, None)


# ---------------------------------------------------------------------------
# Background runner — launched in a thread, writes output to temp files
# ---------------------------------------------------------------------------


def _background_runner(
    run_id: str,
    argv: list,
    cwd: str,
    stdout_path: str,
    stderr_path: str,
    timeout_seconds: int,
    output_limit: int,
    logger: logging.Logger,
) -> None:
    """Run the subprocess detached, capture output to files, update registry."""
    import time

    entry = get_run(run_id)
    if entry is None:
        return

    process: subprocess.Popen = entry["process"]
    timed_out = False

    try:
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            # Popen was created with these fds; we just wait.
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=5)
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)

        exit_code = process.returncode
    except Exception as e:
        logger.error(f"Background command {run_id} failed: {e}")
        # Write error to stderr file
        try:
            with open(stderr_path, "w") as f:
                f.write(f"Background runner error: {e}")
        except Exception:
            pass
        exit_code = -1
    finally:
        # Read and truncate output
        stdout_content, stdout_truncated = _read_partial(stdout_path, output_limit)
        stderr_content, stderr_truncated = _read_partial(stderr_path, output_limit)

        # Calculate total bytes before truncation
        stdout_total = os.path.getsize(stdout_path) if os.path.exists(stdout_path) else 0
        stderr_total = os.path.getsize(stderr_path) if os.path.exists(stderr_path) else 0
        output_truncated_bytes = 0
        if stdout_truncated:
            output_truncated_bytes += stdout_total - len(stdout_content.encode("utf-8"))
        if stderr_truncated:
            output_truncated_bytes += stderr_total - len(stderr_content.encode("utf-8"))

        # If truncated, rewrite the files with the truncated content
        if stdout_truncated:
            with open(stdout_path, "w", encoding="utf-8") as f:
                f.write(stdout_content)
        if stderr_truncated:
            with open(stderr_path, "w", encoding="utf-8") as f:
                f.write(stderr_content)

        with _registry_lock:
            if run_id in _registry:
                _registry[run_id]["status"] = "timed_out" if timed_out else "completed"
                _registry[run_id]["exit_code"] = str(exit_code) if exit_code is not None else None
                _registry[run_id]["timed_out"] = timed_out
                _registry[run_id]["truncated"] = stdout_truncated or stderr_truncated
                _registry[run_id]["output_truncated_bytes"] = output_truncated_bytes
                _registry[run_id]["completed_at"] = time.time()

    _cleanup_stale_entries()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def launch_background_command(
    logger: logging.Logger,
    skill_name: str,
    argv: list,
    cwd: str,
    timeout_seconds: int,
    output_limit: int,
) -> Dict[str, Any]:
    """Launch a command in the background and return a run_id handle.

    The caller polls status via ``poll_command(run_id)``.
    """
    run_id = str(uuid.uuid4())

    # Create temp files for stdout/stderr capture
    tmp_dir = tempfile.mkdtemp(prefix="hsk_async_")
    stdout_path = os.path.join(tmp_dir, "stdout.txt")
    stderr_path = os.path.join(tmp_dir, "stderr.txt")

    # Create the files empty
    open(stdout_path, "w").close()
    open(stderr_path, "w").close()

    stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=stdout_fd,
            stderr=stderr_fd,
            shell=False,
        )
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)

    _register(
        run_id=run_id,
        process=process,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        skill_name=skill_name,
        argv=argv,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
    )

    # Start background thread to wait for completion
    entry = get_run(run_id)
    thread = threading.Thread(
        target=_background_runner,
        args=(
            run_id,
            argv,
            cwd,
            stdout_path,
            stderr_path,
            timeout_seconds,
            output_limit,
            logger,
        ),
        daemon=True,
    )
    with _registry_lock:
        if run_id in _registry:
            _registry[run_id]["thread"] = thread
    thread.start()

    logger.info(
        f"Launched background command: run_id={run_id} skill={skill_name} "
        f"argv={argv!r} timeout={timeout_seconds}"
    )

    return {
        "run_id": run_id,
        "status": "running",
    }


def poll_command(run_id: str, logger: logging.Logger) -> Dict[str, Any]:
    """Poll the status of a background command.

    Returns:
        - ``status``: "running", "completed", "timed_out", or "not_found"
        - ``stdout``: partial or full stdout captured so far
        - ``stderr``: partial or full stderr captured so far
        - ``exit_code``: process exit code (None if still running)
        - ``timed_out``: bool
        - ``truncated``: bool
    """
    entry = get_run(run_id)
    if entry is None:
        return {
            "run_id": run_id,
            "status": "not_found",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
        }

    status = entry["status"]
    output_limit = entry["output_limit"]

    if status == "running":
        # Read partial output from the temp files while the process is still running
        stdout_content, _ = _read_partial(entry["stdout_path"], output_limit)
        stderr_content, _ = _read_partial(entry["stderr_path"], output_limit)
    else:
        # Completed — output files already truncated by the background runner
        stdout_content, _ = _read_partial(entry["stdout_path"], output_limit)
        stderr_content, _ = _read_partial(entry["stderr_path"], output_limit)

    return {
        "run_id": run_id,
        "status": status,
        "stdout": stdout_content,
        "stderr": stderr_content,
        "exit_code": entry.get("exit_code"),
        "timed_out": entry.get("timed_out", False),
        "truncated": entry.get("truncated", False),
        "output_truncated_bytes": entry.get("output_truncated_bytes", 0),
    }


__all__ = [
    "launch_background_command",
    "poll_command",
    "get_run",
]