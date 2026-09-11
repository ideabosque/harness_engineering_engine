# -*- coding: utf-8 -*-
"""Async command executor for background command execution.

Manages long-running skill commands launched with ``runCommand(background=true)``.
Processes run detached in a background thread; output is captured to a temp file
and polled via the ``pollCommand(run_id)`` query.

This mirrors the async-tool pattern already proven in ``mcp_daemon_engine``'s
``async_execute_tool_function``: launch → return a handle → poll for status.

The process registry (``_registry``) is process-local (in-memory dict) and the
``subprocess.Popen`` handle it holds can only ever be waited on by the gateway
instance that launched it — that part does not change with a load balancer in
front of multiple instances.

What *does* change: when an ``info`` (GraphQL ``ResolveInfo``) is supplied,
launch and every status change are also mirrored to the ``command_run``
entity (``models/{postgresql,dynamodb}/command_run.py``, dispatched via
``get_repo("command_run")``). That makes ``poll_command`` correct from *any*
instance — not just the one that launched the run — by falling back to a DB
read when the local dict has no entry. See ``docs/DEVELOPMENT_PLAN.md`` §18
G-6 for the full design and its explicit non-goals (an instance dying
mid-run still orphans that run; this does not add a resumable job queue).

Callers that omit ``info`` (the existing unit tests, or any internal caller
that doesn't have GraphQL context) get the original process-local-only
behavior unchanged — DB mirroring is strictly additive and best-effort: a
failed DB write is logged and swallowed rather than failing the run, since
the OS process and the local registry are still the source of truth for the
launching instance.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import os
import subprocess
import tempfile
import threading
import uuid
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Process registry — thread-safe, in-memory, process-local
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry: Dict[str, Dict[str, Any]] = {}

# Cleanup completed entries after this many seconds to bound memory.
_ENTRY_TTL_SECONDS = 3600  # 1 hour

# How often the background thread checks on the process and (when DB
# mirroring is active) writes partial output — not just once at exit.
_DB_PROGRESS_INTERVAL_SECONDS = 3


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
# DB mirroring — best-effort, only when the caller supplied ``info``
# ---------------------------------------------------------------------------


def _db_insert_launch(
    info: Any, run_id: str, skill_name: str, argv: list, logger: logging.Logger
) -> None:
    try:
        import pendulum

        from ..models.repositories.dispatch import get_repo

        get_repo("command_run").insert_update(
            info,
            run_uuid=run_id,
            skill_name=skill_name,
            argv=json.dumps(argv),
            status="running",
            started_at=pendulum.now("UTC"),
            updated_by="run_command",
        )
    except Exception:
        logger.warning(
            f"command_run: failed to write launch row for run_id={run_id}",
            exc_info=True,
        )


def _db_write_progress(
    info: Any, run_id: str, stdout_content: str, stderr_content: str, logger: logging.Logger
) -> None:
    try:
        from ..models.repositories.dispatch import get_repo

        get_repo("command_run").insert_update(
            info,
            run_uuid=run_id,
            stdout=stdout_content,
            stderr=stderr_content,
            updated_by="run_command",
        )
    except Exception:
        logger.warning(
            f"command_run: failed to write progress for run_id={run_id}",
            exc_info=True,
        )


def _db_write_final(
    info: Any,
    run_id: str,
    status: str,
    exit_code: Optional[int],
    stdout_content: str,
    stderr_content: str,
    timed_out: bool,
    truncated: bool,
    output_truncated_bytes: int,
    logger: logging.Logger,
) -> None:
    try:
        import pendulum

        from ..models.repositories.dispatch import get_repo

        get_repo("command_run").insert_update(
            info,
            run_uuid=run_id,
            status=status,
            exit_code=str(exit_code) if exit_code is not None else None,
            stdout=stdout_content,
            stderr=stderr_content,
            timed_out=timed_out,
            truncated=truncated,
            output_truncated_bytes=output_truncated_bytes,
            completed_at=pendulum.now("UTC"),
            updated_by="run_command",
        )
    except Exception:
        logger.warning(
            f"command_run: failed to write completion for run_id={run_id}",
            exc_info=True,
        )


def _poll_from_db(run_id: str, info: Any, logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """Fall back to the DB-backed registry for a run this instance didn't launch."""
    try:
        partition_key = info.context.get("partition_key")
        if not partition_key:
            return None

        from ..models.repositories.dispatch import get_repo

        row = get_repo("command_run").get(partition_key=partition_key, run_uuid=run_id)
        if row is None:
            return None

        return {
            "run_id": run_id,
            "status": row.get("status") or "running",
            "stdout": row.get("stdout") or "",
            "stderr": row.get("stderr") or "",
            "exit_code": row.get("exit_code"),
            "timed_out": bool(row.get("timed_out", False)),
            "truncated": bool(row.get("truncated", False)),
            "output_truncated_bytes": row.get("output_truncated_bytes", 0) or 0,
        }
    except Exception:
        logger.warning(f"command_run: DB fallback poll failed for run_id={run_id}", exc_info=True)
        return None


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
    info: Any = None,
) -> None:
    """Run the subprocess detached, capture output to files, update registry."""
    import time

    entry = get_run(run_id)
    if entry is None:
        return

    process: subprocess.Popen = entry["process"]
    timed_out = False

    try:
        # Popen was created with its own stdout/stderr fds (opened in
        # launch_background_command) — the child writes through those
        # directly, so this thread only needs to wait on it. Do not
        # reopen stdout_path/stderr_path here: an O_TRUNC open on files
        # the child may already be writing to would race its output,
        # silently dropping whatever it had written so far.
        #
        # Wait in short intervals rather than one blocking call for the
        # full timeout, so a DB-mirrored run's row picks up fresh partial
        # output every few seconds instead of only at exit.
        elapsed = 0.0
        while True:
            wait_for = min(_DB_PROGRESS_INTERVAL_SECONDS, max(timeout_seconds - elapsed, 0.01))
            try:
                process.wait(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                elapsed += wait_for
                if elapsed >= timeout_seconds:
                    timed_out = True
                    process.kill()
                    process.wait(timeout=5)
                    break
                if info is not None:
                    stdout_progress, _ = _read_partial(stdout_path, output_limit)
                    stderr_progress, _ = _read_partial(stderr_path, output_limit)
                    _db_write_progress(info, run_id, stdout_progress, stderr_progress, logger)

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

        final_status = "timed_out" if timed_out else "completed"
        truncated = stdout_truncated or stderr_truncated

        with _registry_lock:
            if run_id in _registry:
                _registry[run_id]["status"] = final_status
                _registry[run_id]["exit_code"] = str(exit_code) if exit_code is not None else None
                _registry[run_id]["timed_out"] = timed_out
                _registry[run_id]["truncated"] = truncated
                _registry[run_id]["output_truncated_bytes"] = output_truncated_bytes
                _registry[run_id]["completed_at"] = time.time()

        if info is not None:
            _db_write_final(
                info,
                run_id,
                final_status,
                exit_code,
                stdout_content,
                stderr_content,
                timed_out,
                truncated,
                output_truncated_bytes,
                logger,
            )

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
    info: Any = None,
) -> Dict[str, Any]:
    """Launch a command in the background and return a run_id handle.

    The caller polls status via ``poll_command(run_id)``. When ``info`` (the
    GraphQL ``ResolveInfo`` for the launching request) is supplied, the run
    is also mirrored to the DB-backed ``command_run`` registry so that
    ``pollCommand`` is answerable from any gateway instance, not only this
    one — see the module docstring and ``docs/DEVELOPMENT_PLAN.md`` §18 G-6.
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

    if info is not None:
        _db_insert_launch(info, run_id, skill_name, argv, logger)

    # Start background thread to wait for completion
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
            info,
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


def poll_command(run_id: str, logger: logging.Logger, info: Any = None) -> Dict[str, Any]:
    """Poll the status of a background command.

    Checks the process-local registry first (fast path — always correct for
    the instance that launched the run). If the run is unknown locally and
    ``info`` is supplied, falls back to a DB read via ``command_run`` so that
    a poll landing on a *different* gateway instance than the one that
    launched the run still returns accurate status instead of a spurious
    ``not_found``. See ``docs/DEVELOPMENT_PLAN.md`` §18 G-6.

    Returns:
        - ``status``: "running", "completed", "timed_out", or "not_found"
        - ``stdout``: partial or full stdout captured so far
        - ``stderr``: partial or full stderr captured so far
        - ``exit_code``: process exit code (None if still running)
        - ``timed_out``: bool
        - ``truncated``: bool
    """
    entry = get_run(run_id)
    if entry is not None:
        status = entry["status"]
        output_limit = entry["output_limit"]

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

    if info is not None:
        db_result = _poll_from_db(run_id, info, logger)
        if db_result is not None:
            return db_result

    return {
        "run_id": run_id,
        "status": "not_found",
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
    }


__all__ = [
    "launch_background_command",
    "poll_command",
    "get_run",
]
