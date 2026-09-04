# -*- coding: utf-8 -*-
"""Harness Engineering Engine CLI package manager.

Handles registration, GitHub-based installation, version verification, and
package-level deployment locks for approved Python CLI packages.

Flow (per DEVELOPMENT_PLAN.md §11):
1. Load registered package metadata from the database.
2. Compare locally installed version with the registered version.
3. Missing → install from GitHub ref → verify → continue.
4. Outdated → lock → uninstall → reinstall → verify → continue.
5. Matching → no action.
6. Installation/verification failures are logged and block execution.

All pip operations use ``subprocess.run([sys.executable, "-m", "pip", ...])``
with ``shell=False`` — never ``shell=True``.
"""
from __future__ import print_function

__author__ = "bibow"

import importlib.metadata
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..handlers.config import Config
from ..models.repositories import get_repo


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


# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def _pip_install(github_url: str, git_ref: str) -> Dict[str, Any]:
    """Install a package from a GitHub ref via ``pip install git+url@ref``."""
    install_target = f"git+{github_url}"
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
    github_url: str,
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
        "github_repository_url": github_url,
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
    github_repository_url: str,
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
            github_repository_url=github_repository_url,
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
            github_url=github_repository_url,
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
            github_repository_url=github_repository_url,
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
            github_url=github_repository_url,
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
                    "github_repository_url": item.github_repository_url,
                    "version": item.version,
                    "git_ref": item.git_ref,
                }
                break

    if registration is None:
        msg = f"CLI package '{package_name}' is not registered or not active."
        logger.error(msg)
        return {"package_name": package_name, "status": "error", "error": msg}

    target_version = registration["version"]
    github_url = registration["github_repository_url"]
    git_ref = registration.get("git_ref") or ""

    # The distribution name may differ from the package_name; use package_name
    # as the distribution name by default.
    distribution_name = registration.get("distribution_name") or package_name

    # 2. Compare installed version with registered version
    installed_version = _get_installed_version(distribution_name)

    if installed_version == target_version:
        logger.info(
            f"CLI package '{package_name}' v{target_version} already installed."
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=installed_version,
            target_version=target_version,
            github_url=github_url,
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
        # 4. Re-check installed version after acquiring the lock
        installed_version = _get_installed_version(distribution_name)

        if installed_version == target_version:
            # Another worker installed it while we waited
            logger.info(
                f"CLI package '{package_name}' v{target_version} became current while waiting."
            )
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                github_url=github_url,
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
                github_url=github_url,
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
                    github_url=github_url,
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
                github_url=github_url,
                git_ref=git_ref,
                operation="install_started",
                status="started",
            )

        # Install from GitHub ref
        install_result = _pip_install(github_url, git_ref)
        if install_result["returncode"] != 0:
            error = install_result["stderr"] or install_result["stdout"]
            _audit_log(
                logger, partition_key, package_name,
                previous_version=installed_version,
                target_version=target_version,
                github_url=github_url,
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
                github_url=github_url,
                git_ref=git_ref,
                operation="verification_failed",
                status="failed",
                error=error,
            )
            return {"package_name": package_name, "status": "error", "error": error}

        logger.info(
            f"CLI package '{package_name}' v{target_version} installed and verified."
        )
        _audit_log(
            logger, partition_key, package_name,
            previous_version=installed_version,
            target_version=target_version,
            github_url=github_url,
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
        github_repository_url: str,
        version: str,
        git_ref: Optional[str] = None,
        description: Optional[str] = None,
        updated_by: str = "system",
    ) -> Dict[str, Any]:
        """Register or update a CLI package."""
        return register_cli_package(
            info,
            package_name=package_name,
            github_repository_url=github_repository_url,
            version=version,
            git_ref=git_ref,
            description=description,
            updated_by=updated_by,
        )