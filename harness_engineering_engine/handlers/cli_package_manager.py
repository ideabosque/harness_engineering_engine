# -*- coding: utf-8 -*-
"""Harness Engineering Engine CLI package manager.

Handles registration and lazy, GitHub-based installation/reinstallation of
approved Python CLI packages. Registration (DB bookkeeping) and installation
(pip + local marker) are deliberately separate: ``deploySkillPackage`` only
ever registers a declared ``cli_packages`` entry; ``ensure_package`` is what
actually installs, and it runs lazily on first ``runCommand`` against a
skill that declares the package (see ``command_executor.py`` and
``mutations/skill_management.py::RunCommand``).

Flow:
1. Load registered package metadata (name/git_repository_url/version/
   git_ref) from the database.
2. Compare the locally installed pip distribution version against the
   registered ``version`` string.
3. Also resolve the registered ``git_ref`` to its current commit SHA (a
   cheap ``git ls-remote``, no clone) and compare against the commit this
   host last installed from (recorded in a local marker file, since which
   commit is actually installed is host-local state, not something a
   shared DB row can represent safely in a multi-instance deployment) — a
   floating ref like ``main`` can gain new commits without the skill
   author ever bumping the declared ``version`` string, which a
   version-string-only comparison would silently miss.
4. Both match → no action (the common, fast case).
5. Either differs → lock → install (or uninstall+reinstall) → verify →
   record the new commit in the local marker → continue.
6. Installation/verification failures are logged and block execution.

All pip operations use ``subprocess.run([sys.executable, "-m", "pip", ...])``
with ``shell=False`` — never ``shell=True``.
"""
from __future__ import print_function

__author__ = "bibow"

import importlib.metadata
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..handlers.config import Config
from ..models.repositories import get_repo
from . import git_client
from .skill_path import resolve_skill_root


# ---------------------------------------------------------------------------
# Package-level deployment locks
# ---------------------------------------------------------------------------

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(partition_key: str, package_name: str) -> threading.Lock:
    """Return a process-level lock for a (partition, package) pair."""
    key = f"{partition_key}:{package_name}"
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def _get_installed_version(distribution_name: str) -> Optional[str]:
    """Return the installed distribution version, or ``None`` if not installed.

    Uses ``importlib.metadata`` (stdlib) to read the installed distribution
    metadata, not the source folder.
    """
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _resolve_current_commit(git_url: str, git_ref: str) -> Optional[str]:
    """Resolve ``git_ref`` to its current commit SHA, or ``None`` on failure.

    A cheap ``git ls-remote`` — no clone. Returning ``None`` (rather than
    raising) means a transient network hiccup falls back to the
    version-string-only check instead of forcing a spurious reinstall.
    """
    if not git_url:
        return None
    try:
        return git_client.resolve_ref_sha(git_url, git_ref or "main")
    except Exception:
        return None


def _install_marker_path(package_name: str) -> Path:
    """Path to the host-local record of what this host last installed.

    Deliberately host-local (like a skill's own ``.hsk-skill.json``), not a
    DB field on the ``CliPackage`` registration row: the row is shared
    tenant state, but "is this commit actually installed in THIS host's
    Python environment" can only ever be true for one host at a time in a
    multi-instance deployment.
    """
    return resolve_skill_root() / ".hsk-cli-packages" / f"{package_name}.json"


def _read_install_marker(package_name: str) -> Optional[Dict[str, Any]]:
    path = _install_marker_path(package_name)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_install_marker(
    package_name: str,
    distribution_name: str,
    version: str,
    resolved_commit: Optional[str],
    git_repository_url: str,
    git_ref: str,
) -> None:
    path = _install_marker_path(package_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "package_name": package_name,
        "distribution_name": distribution_name,
        "version": version,
        "resolved_commit": resolved_commit,
        "git_repository_url": git_repository_url,
        "git_ref": git_ref,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def _pip_install(git_url: str, git_ref: str) -> Dict[str, Any]:
    """Install a package from a GitHub ref via ``pip install git+url@ref``."""
    install_target = f"git+{git_url}"
    if git_ref:
        install_target = f"{install_target}@{git_ref}"

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", install_target],
        capture_output=True,
        text=True,
        timeout=300,
        shell=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def _pip_uninstall(distribution_name: str) -> Dict[str, Any]:
    """Uninstall a package via ``pip uninstall -y``."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", distribution_name],
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def _audit_log(
    logger: logging.Logger,
    partition_key: str,
    package_name: str,
    previous_version: Optional[str],
    target_version: str,
    git_url: str,
    git_ref: str,
    operation: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Record an installation audit entry in the log.

    A real implementation would persist this to a DB table; for v1.1 the
    audit record is emitted as a structured log line.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "partition_key": partition_key,
        "package_name": package_name,
        "previous_version": previous_version,
        "target_version": target_version,
        "git_repository_url": git_url,
        "git_ref": git_ref,
        "operation": operation,
        "status": status,
        "error": error,
    }
    logger.info(f"CLI package audit: {entry}")


# ---------------------------------------------------------------------------
# Package registration
# ---------------------------------------------------------------------------


def register_cli_package(
    info: Any,
    package_name: str,
    git_repository_url: str,
    version: str,
    git_ref: Optional[str] = None,
    description: Optional[str] = None,
    status: str = "active",
    updated_by: str = "system",
) -> Dict[str, Any]:
    """Register or update a CLI package in the database."""
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    repo = get_repo("cli_package")

    # Check if package already registered by name
    existing = None
    if Config.DB_BACKEND == "dynamodb":
        from ..models.dynamodb.cli_package import CliPackageModel
        from ..utils.normalization import normalize_to_json

        results = CliPackageModel.query(
            partition_key,
            None,
            CliPackageModel.package_name == package_name,
        )
        for row in results:
            existing = normalize_to_json(row.attribute_values)
            break
    else:
        result = repo.list(info, package_name=package_name)
        for item in result.cli_package_list:
            if item.package_name == package_name:
                existing = {
                    "cli_package_uuid": item.cli_package_uuid,
                    "version": item.version,
                }
                break

    if existing:
        # Update existing registration
        repo.insert_update(
            info,
            cli_package_uuid=existing["cli_package_uuid"],
            package_name=package_name,
            git_repository_url=git_repository_url,
            version=version,
            git_ref=git_ref,
            description=description,
            status=status,
            updated_by=updated_by,
        )
        logger.info(
            f"Updated CLI package registration: {package_name} v{version}"
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=existing.get("version"),
            target_version=version,
            git_url=git_repository_url,
            git_ref=git_ref or "",
            operation="registration_update",
            status="succeeded",
        )
        return {"package_name": package_name, "version": version, "operation": "updated"}
    else:
        # New registration
        import uuid

        repo.insert_update(
            info,
            cli_package_uuid=str(uuid.uuid4()),
            package_name=package_name,
            git_repository_url=git_repository_url,
            version=version,
            git_ref=git_ref,
            description=description,
            status=status,
            enabled=True,
            updated_by=updated_by,
        )
        logger.info(
            f"Registered CLI package: {package_name} v{version}"
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=None,
            target_version=version,
            git_url=git_repository_url,
            git_ref=git_ref or "",
            operation="registration",
            status="succeeded",
        )
        return {"package_name": package_name, "version": version, "operation": "registered"}


# ---------------------------------------------------------------------------
# Ensure package — the core runtime flow
# ---------------------------------------------------------------------------


def ensure_package(info: Any, package_name: str) -> Dict[str, Any]:
    """Ensure the registered version of ``package_name`` is installed and verified.

    This is called by the command executor before running a CLI package command.

    Returns a dict with ``package_name``, ``version``, ``status`` ("ready" or
    "error"), and ``error`` (when status is error).
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    # 1. Load registered package metadata
    repo = get_repo("cli_package")
    registration = None
    if Config.DB_BACKEND == "dynamodb":
        from ..models.dynamodb.cli_package import CliPackageModel
        from ..utils.normalization import normalize_to_json

        results = CliPackageModel.query(
            partition_key,
            None,
            CliPackageModel.package_name == package_name,
        )
        for row in results:
            data = normalize_to_json(row.attribute_values)
            if data.get("enabled") and data.get("status") == "active":
                registration = data
                break
    else:
        result = repo.list(info, package_name=package_name, status="active")
        for item in result.cli_package_list:
            if item.enabled and item.package_name == package_name:
                registration = {
                    "package_name": item.package_name,
                    "git_repository_url": item.git_repository_url,
                    "version": item.version,
                    "git_ref": item.git_ref,
                }
                break

    if registration is None:
        msg = f"CLI package '{package_name}' is not registered or not active."
        logger.error(msg)
        return {"package_name": package_name, "status": "error", "error": msg}

    target_version = registration["version"]
    git_url = registration["git_repository_url"]
    git_ref = registration.get("git_ref") or ""

    # The distribution name may differ from the package_name; use package_name
    # as the distribution name by default.
    distribution_name = registration.get("distribution_name") or package_name

    # 2. Compare installed version with registered version, and — since a
    # floating git_ref can gain new commits without the declared version
    # string ever changing — also compare the commit this host last
    # installed from against the ref's current commit.
    installed_version = _get_installed_version(distribution_name)
    install_marker = _read_install_marker(package_name)
    current_commit = _resolve_current_commit(git_url, git_ref)

    def _commit_confirmed_current() -> bool:
        # No commit to compare against (offline, or a non-git registration)
        # → don't force a reinstall on a version string that already
        # matches; that would turn a transient network hiccup into
        # needless churn. A version match with no prior marker at all is
        # treated as already-current too (e.g. installed before this
        # commit-tracking existed) — the marker below then gets backfilled
        # so the *next* check has something real to compare against.
        if current_commit is None or install_marker is None:
            return True
        return install_marker.get("resolved_commit") == current_commit

    if installed_version == target_version and _commit_confirmed_current():
        if current_commit is not None and install_marker is None:
            _write_install_marker(
                package_name, distribution_name, target_version,
                current_commit, git_url, git_ref,
            )
        logger.info(
            f"CLI package '{package_name}' v{target_version} already installed "
            f"(commit {current_commit or 'unresolved'})."
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=installed_version,
            target_version=target_version,
            git_url=git_url,
            git_ref=git_ref,
            operation="version_check",
            status="succeeded",
        )
        return {"package_name": package_name, "version": target_version, "status": "ready"}

    # 3. Acquire package-level lock
    lock = _get_lock(partition_key, package_name)
    acquired = lock.acquire(timeout=300)
    if not acquired:
        msg = f"Could not acquire lock for package '{package_name}' within 300s."
        logger.error(msg)
        return {"package_name": package_name, "status": "error", "error": msg}

    try:
        # 4. Re-check installed version (and commit) after acquiring the lock
        installed_version = _get_installed_version(distribution_name)
        install_marker = _read_install_marker(package_name)

        if installed_version == target_version and _commit_confirmed_current():
            # Another worker installed it while we waited
            logger.info(
                f"CLI package '{package_name}' v{target_version} became current while waiting."
            )
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                git_url=git_url,
                git_ref=git_ref,
                operation="upgrade_skipped",
                status="succeeded",
            )
            return {"package_name": package_name, "version": target_version, "status": "ready"}

        # 5. Install or upgrade
        if installed_version is not None:
            # Outdated → uninstall → reinstall
            logger.info(
                f"Upgrading CLI package '{package_name}': "
                f"{installed_version} → {target_version}"
            )
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                git_url=git_url,
                git_ref=git_ref,
                operation="upgrade_started",
                status="started",
            )

            uninstall_result = _pip_uninstall(distribution_name)
            if uninstall_result["returncode"] != 0:
                error = uninstall_result["stderr"] or uninstall_result["stdout"]
                _audit_log(
                    logger, partition_key, package_name,
                    previous_version=installed_version,
                    target_version=target_version,
                    git_url=git_url,
                    git_ref=git_ref,
                    operation="upgrade_failed",
                    status="failed",
                    error=error,
                )
                return {"package_name": package_name, "status": "error", "error": f"Uninstall failed: {error}"}
        else:
            # Missing → install
            logger.info(
                f"Installing CLI package '{package_name}' v{target_version} from GitHub."
            )
            _audit_log(
                logger, partition_key, package_name,
                previous_version=None,
                target_version=target_version,
                git_url=git_url,
                git_ref=git_ref,
                operation="install_started",
                status="started",
            )

        # Install from GitHub ref
        install_result = _pip_install(git_url, git_ref)
        if install_result["returncode"] != 0:
            error = install_result["stderr"] or install_result["stdout"]
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                git_url=git_url,
                git_ref=git_ref,
                operation="install_failed",
                status="failed",
                error=error,
            )
            return {"package_name": package_name, "status": "error", "error": f"Install failed: {error}"}

        # 6. Verify installed version
        verified_version = _get_installed_version(distribution_name)
        if verified_version != target_version:
            error = f"Verification failed: expected {target_version}, got {verified_version}"
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                git_url=git_url,
                git_ref=git_ref,
                operation="verification_failed",
                status="failed",
                error=error,
            )
            return {"package_name": package_name, "status": "error", "error": error}

        _write_install_marker(
            package_name, distribution_name, target_version,
            current_commit, git_url, git_ref,
        )
        logger.info(
            f"CLI package '{package_name}' v{target_version} installed and verified "
            f"(commit {current_commit or 'unresolved'})."
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=installed_version,
            target_version=target_version,
            git_url=git_url,
            git_ref=git_ref,
            operation="install_succeeded",
            status="succeeded",
        )
        return {"package_name": package_name, "version": target_version, "status": "ready"}

    finally:
        lock.release()


__all__ = [
    "register_cli_package",
    "ensure_package",
    "CliPackageManager",
]


# ---------------------------------------------------------------------------
# Class wrapper for backward compatibility
# ---------------------------------------------------------------------------


class CliPackageManager:
    """Manage Python CLI packages that skills depend on."""

    def __init__(self, logger: logging.Logger, setting: Dict[str, Any]) -> None:
        self.logger = logger
        self.setting = setting

    def ensure_package(self, info: Any, package_name: str) -> Dict[str, Any]:
        """Ensure the registered version of ``package_name`` is installed."""
        return ensure_package(info, package_name)

    def register_package(
        self,
        info: Any,
        package_name: str,
        git_repository_url: str,
        version: str,
        git_ref: Optional[str] = None,
        description: Optional[str] = None,
        updated_by: str = "system",
    ) -> Dict[str, Any]:
        """Register or update a CLI package."""
        return register_cli_package(
            info,
            package_name=package_name,
            git_repository_url=git_repository_url,
            version=version,
            git_ref=git_ref,
            description=description,
            updated_by=updated_by,
        )