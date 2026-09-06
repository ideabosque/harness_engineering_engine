# -*- coding: utf-8 -*-
"""Background refresh tracker for on-demand skill git fetches.

When ``skill(name)`` detects the local cache is stale or missing, it launches
the git refresh in a background thread and returns a ``status: "refreshing"``
signal instead of blocking. Callers retry after the refresh completes.

The tracker is process-local (in-memory dict), matching the same design
constraint as ``async_command_executor``: the engine runs as a single
process via gateway dispatch. Cross-instance refresh coordination would
require a shared store (Redis/DynamoDB).
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import threading
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Refresh registry — thread-safe, in-memory, process-local
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_refresh_registry: Dict[str, Dict[str, Any]] = {}

# How long a refresh entry stays in the registry after completion.
# Completed entries are cleaned up after this TTL to bound memory.
_COMPLETED_TTL_SECONDS = 600  # 10 minutes


def _key(partition_key: str, skill_name: str) -> str:
    """Build the registry key for a (tenant, skill) pair."""
    return f"{partition_key}#{skill_name}"


def is_refreshing(partition_key: str, skill_name: str) -> bool:
    """Return True if a background refresh is in progress for this skill."""
    with _registry_lock:
        entry = _refresh_registry.get(_key(partition_key, skill_name))
        return entry is not None and entry.get("status") == "refreshing"


def get_refresh_status(partition_key: str, skill_name: str) -> Optional[Dict[str, Any]]:
    """Return the current refresh status dict, or None if no refresh is tracked."""
    with _registry_lock:
        return _refresh_registry.get(_key(partition_key, skill_name))


def launch_refresh(
    logger: logging.Logger,
    partition_key: str,
    skill_name: str,
    active: Dict[str, Any],
    skill_root: str,
) -> Dict[str, Any]:
    """Launch a background git refresh for a skill and return immediately.

    Returns a dict with ``status: "refreshing"`` and a timestamp. Callers
    should retry ``skill(name)`` after a short wait; once the refresh
    completes, the local cache will be up to date and ``skill(name)`` will
    return the full body.
    """
    key = _key(partition_key, skill_name)

    with _registry_lock:
        existing = _refresh_registry.get(key)
        if existing and existing.get("status") == "refreshing":
            # Already refreshing — don't launch a second thread
            logger.info(
                f"Refresh already in progress for skill '{skill_name}' — returning existing status."
            )
            return {
                "status": "refreshing",
                "started_at": existing.get("started_at"),
            }

        _refresh_registry[key] = {
            "status": "refreshing",
            "started_at": time.time(),
            "completed_at": None,
            "error": None,
        }

    thread = threading.Thread(
        target=_refresh_runner,
        args=(logger, key, skill_name, active, skill_root),
        daemon=True,
    )
    thread.start()

    logger.info(
        f"Launched background refresh for skill '{skill_name}' (key={key})"
    )

    return {
        "status": "refreshing",
        "started_at": _refresh_registry[key]["started_at"],
    }


def _refresh_runner(
    logger: logging.Logger,
    key: str,
    skill_name: str,
    active: Dict[str, Any],
    skill_root: str,
) -> None:
    """Background thread that runs the git refresh and updates the registry."""
    from pathlib import Path

    try:
        from .skill_refresh import refresh_single_skill

        refresh_single_skill(logger, active, Path(skill_root))

        with _registry_lock:
            if key in _refresh_registry:
                _refresh_registry[key]["status"] = "completed"
                _refresh_registry[key]["completed_at"] = time.time()

        logger.info(
            f"Background refresh completed for skill '{skill_name}' (key={key})"
        )
    except Exception as e:
        logger.error(
            f"Background refresh failed for skill '{skill_name}' (key={key}): {e}"
        )
        with _registry_lock:
            if key in _refresh_registry:
                _refresh_registry[key]["status"] = "failed"
                _refresh_registry[key]["completed_at"] = time.time()
                _refresh_registry[key]["error"] = str(e)
    finally:
        _cleanup_stale_entries()


def _cleanup_stale_entries() -> None:
    """Remove completed entries older than _COMPLETED_TTL_SECONDS."""
    now = time.time()
    with _registry_lock:
        stale = [
            key
            for key, entry in _refresh_registry.items()
            if entry.get("completed_at")
            and (now - entry["completed_at"]) > _COMPLETED_TTL_SECONDS
        ]
        for key in stale:
            _refresh_registry.pop(key, None)


__all__ = [
    "is_refreshing",
    "get_refresh_status",
    "launch_refresh",
]