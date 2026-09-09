# -*- coding: utf-8 -*-
"""Guarded command executor.

Deny-by-default command execution with structured argv allowlisting, path
normalization, timeout, output caps, and audit logging.

The executor is **not** exposed directly to agents.  Only the skill MCP module
calls this service after validating that the requested ``argv`` matches the
skill's registered ``allowed_commands``.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import Config
from .skill_path import resolve_skill_root
from .skill_reader import skill as _get_skill


# ---------------------------------------------------------------------------
# Allowlist matching
# ---------------------------------------------------------------------------

def _normalize_argv(argv: Union[str, List[str]]) -> List[str]:
    """Return a normalized argv list from either a string or a list."""
    if isinstance(argv, str):
        # Use shlex to split safely; reject if it contains shell metachars
        # that would indicate pipe/redirect/substitution intent.
        return shlex.split(argv)
    return list(argv)


def _match_allowed_command(
    argv: List[str],
    allowed: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the matching allowlist entry, or ``None`` if no match.

    Matching is exact for each argv element except when the element is a
    simple glob like ``*.json``.
    """
    for entry in allowed:
        allowed_argv = entry.get("argv", [])
        if len(argv) != len(allowed_argv):
            continue

        match = True
        for expected, actual in zip(allowed_argv, argv):
            if expected == actual:
                continue
            # Simple glob support (e.g. "*.json")
            if "*" in expected:
                prefix, _, suffix = expected.partition("*")
                if not (actual.startswith(prefix) and actual.endswith(suffix)):
                    match = False
                    break
                continue
            match = False
            break

        if match:
            return entry

    return None


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------

def _resolve_and_validate_paths(
    argv: List[str],
    skill_dir: Path,
    workspace_scope: Optional[str] = None,
) -> List[str]:
    """Resolve file paths in argv and ensure they stay inside approved roots.

    For v1, we simply resolve relative paths against ``skill_dir``.
    ``workspace_scope`` is reserved for future use (e.g. ``workspace_dir``).
    """
    resolved: List[str] = []
    for arg in argv:
        # Try to resolve as a path relative to the skill directory
        candidate = skill_dir / arg
        if candidate.exists():
            resolved.append(str(candidate.resolve()))
        else:
            resolved.append(arg)
    return resolved


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_command(
    info: Any,
    skill_name: str,
    argv: Union[str, List[str]],
    workspace_scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and execute a command for the given skill.

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``timed_out``,
    and ``truncated``.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    if not Config.RUN_COMMAND_ENABLED:
        raise RuntimeError(
            "HSK_RUN_COMMAND_ENABLED is false — command execution is disabled."
        )

    # ------------------------------------------------------------------
    # Resolve skill allowlist
    # ------------------------------------------------------------------
    skill_data = _get_skill(info, name=skill_name)
    allowed = skill_data.get("allowed_commands", [])

    if not allowed:
        raise PermissionError(
            f"Skill '{skill_name}' has no allowed_commands — command denied."
        )

    # ------------------------------------------------------------------
    # Ensure CLI packages are installed (v1.1)
    # ------------------------------------------------------------------
    cli_packages = skill_data.get("cli_packages", [])
    if cli_packages:
        from .cli_package_manager import ensure_package

        for pkg in cli_packages:
            pkg_name = pkg.get("package_name") or pkg.get("distribution_name")
            if not pkg_name:
                continue
            result = ensure_package(info, pkg_name)
            if result.get("status") != "ready":
                error = result.get("error", "unknown error")
                raise RuntimeError(
                    f"CLI package '{pkg_name}' is not ready: {error}"
                )

    # ------------------------------------------------------------------
    # Normalize argv
    # ------------------------------------------------------------------
    argv_list = _normalize_argv(argv)

    # ------------------------------------------------------------------
    # Reject shell constructs
    # ------------------------------------------------------------------
    for arg in argv_list:
        if any(c in arg for c in ("|", "&", ";", ">", "<", "`", "$(")):
            raise PermissionError(
                f"Shell metacharacters rejected: {arg}"
            )

    # ------------------------------------------------------------------
    # Allowlist match
    # ------------------------------------------------------------------
    matched = _match_allowed_command(argv_list, allowed)
    if matched is None:
        raise PermissionError(
            f"Command argv {argv_list!r} does not match any allowed_commands "
            f"entry for skill '{skill_name}'."
        )

    # ------------------------------------------------------------------
    # Resolve paths and working directory
    # ------------------------------------------------------------------
    skill_root = resolve_skill_root()
    skill_dir = skill_root / skill_name
    argv_list = _resolve_and_validate_paths(argv_list, skill_dir, workspace_scope)

    # ------------------------------------------------------------------
    # Timeout / output limits
    # ------------------------------------------------------------------
    timeout_seconds = int(
        matched.get("timeout_seconds", Config.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS)
    )
    output_limit = int(
        matched.get("output_limit_bytes", Config.RUN_COMMAND_OUTPUT_LIMIT_BYTES)
    )

    # ------------------------------------------------------------------
    # Dry-run
    # ------------------------------------------------------------------
    if Config.DRY_RUN:
        logger.info(f"DRY-RUN: skill={skill_name} argv={argv_list!r}")
        return {
            "stdout": f"DRY-RUN: {' '.join(argv_list)}",
            "stderr": "",
            "exit_code": "0",
            "timed_out": False,
            "truncated": False,
        }

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    partition_key = info.context.get("partition_key")
    logger.info(
        f"Executing command: skill={skill_name} argv={argv_list!r} "
        f"timeout={timeout_seconds} output_limit={output_limit} "
        f"tenant={partition_key}"
    )

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------
    timed_out = False
    truncated = False
    try:
        result = subprocess.run(
            argv_list,
            cwd=str(skill_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,  # never use a shell
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        stdout_len = len(stdout.encode("utf-8"))
        stderr_len = len(stderr.encode("utf-8"))
        truncated = False
        output_truncated_bytes = 0

        if stdout_len > output_limit:
            output_truncated_bytes += stdout_len - (output_limit // 2)
            stdout = stdout[: output_limit // 2]
            truncated = True
        if stderr_len > output_limit:
            output_truncated_bytes += stderr_len - (output_limit // 2)
            stderr = stderr[: output_limit // 2]
            truncated = True

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
            "timed_out": timed_out,
            "truncated": truncated,
            "output_truncated_bytes": output_truncated_bytes,
        }

    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning(
            f"Command timed out: skill={skill_name} argv={argv_list!r} "
            f"timeout={timeout_seconds}"
        )
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout_seconds} seconds.",
            "exit_code": None,
            "timed_out": timed_out,
            "truncated": False,
        }


__all__ = ["execute_command"]

