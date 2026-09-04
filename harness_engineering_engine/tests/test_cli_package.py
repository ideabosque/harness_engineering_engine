#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for the CLI package manager (v1.1)."""
from __future__ import print_function

__author__ = "bibow"

import logging
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from harness_engineering_engine.handlers.cli_package_manager import (
    ensure_package,
    register_cli_package,
)
from harness_engineering_engine.handlers.config import Config

from . import conftest as _conftest  # noqa: F401


class FakeInfo:
    def __init__(self, partition_key="hsk-itest#p1"):
        self.context = {
            "logger": logging.getLogger(),
            "partition_key": partition_key,
            "endpoint_id": "hsk-itest",
            "part_id": "p1",
        }


@pytest.fixture(scope="function")
def itest():
    Config._initialized = False
    Config.DB_BACKEND = "dynamodb"
    Config.initialize(logging.getLogger(), _conftest.build_setting_from_env())
    tmp = Path(tempfile.mkdtemp(prefix="hsk_cli_"))
    Config.SKILL_ROOT = str(tmp)
    with Config.db_session() as s:
        s.execute(text("DELETE FROM hsk_cli_packages WHERE partition_key LIKE 'hsk-itest%'"))
        s.commit()
    yield {"tmp": tmp, "endpoint_id": "hsk-itest", "part_id": "p1"}
    shutil.rmtree(tmp, ignore_errors=True)
    Config.db_session.remove()


class TestRegisterCliPackage:
    def test_register_new_package(self, itest):
        result = register_cli_package(
            FakeInfo(),
            package_name="test-pkg",
            github_repository_url="https://github.com/example/test-pkg.git",
            version="1.0.0",
            git_ref="v1.0.0",
            description="Test package",
            updated_by="itest",
        )
        assert result["operation"] == "registered"
        assert result["package_name"] == "test-pkg"
        assert result["version"] == "1.0.0"

    def test_update_existing_package(self, itest):
        # First registration
        register_cli_package(
            FakeInfo(),
            package_name="update-pkg",
            github_repository_url="https://github.com/example/update-pkg.git",
            version="1.0.0",
            updated_by="itest",
        )
        # Update version
        result = register_cli_package(
            FakeInfo(),
            package_name="update-pkg",
            github_repository_url="https://github.com/example/update-pkg.git",
            version="2.0.0",
            git_ref="v2.0.0",
            updated_by="itest",
        )
        assert result["operation"] == "updated"
        assert result["version"] == "2.0.0"


class TestEnsurePackage:
    def test_unregistered_package_returns_error(self, itest):
        result = ensure_package(FakeInfo(), "nonexistent-pkg")
        assert result["status"] == "error"
        assert "not registered" in result["error"]

    def test_matching_version_no_action(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="matching-pkg",
            github_repository_url="https://github.com/example/matching-pkg.git",
            version="0.0.1",  # pytest is typically installed at this version or higher
            git_ref="v0.0.1",
            updated_by="itest",
        )
        # Mock the installed version to match the registered version
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            return_value="0.0.1",
        ):
            result = ensure_package(FakeInfo(), "matching-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "0.0.1"

    def test_missing_package_triggers_install(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="missing-pkg",
            github_repository_url="https://github.com/example/missing-pkg.git",
            version="1.2.3",
            git_ref="v1.2.3",
            updated_by="itest",
        )
        # Mock: not installed → install succeeds → verification matches
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=[None, "1.2.3"],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ):
            result = ensure_package(FakeInfo(), "missing-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "1.2.3"

    def test_outdated_package_triggers_upgrade(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="outdated-pkg",
            github_repository_url="https://github.com/example/outdated-pkg.git",
            version="2.0.0",
            git_ref="v2.0.0",
            updated_by="itest",
        )
        # Mock: installed=1.0.0 → uninstall succeeds → install succeeds → verified=2.0.0
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=["1.0.0", "2.0.0"],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_uninstall",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ):
            result = ensure_package(FakeInfo(), "outdated-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "2.0.0"

    def test_install_failure_blocks_execution(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="fail-pkg",
            github_repository_url="https://github.com/example/fail-pkg.git",
            version="3.0.0",
            git_ref="v3.0.0",
            updated_by="itest",
        )
        # Mock: not installed → install fails
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=[None, None],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 1, "stdout": "", "stderr": "pip error"},
        ):
            result = ensure_package(FakeInfo(), "fail-pkg")
        assert result["status"] == "error"
        assert "Install failed" in result["error"]

    def test_verification_failure_blocks_execution(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="verify-fail-pkg",
            github_repository_url="https://github.com/example/verify-fail-pkg.git",
            version="4.0.0",
            git_ref="v4.0.0",
            updated_by="itest",
        )
        # Mock: not installed → recheck after lock still None → install succeeds → verification returns wrong version
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=[None, None, "wrong-version"],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ):
            result = ensure_package(FakeInfo(), "verify-fail-pkg")
        assert result["status"] == "error"
        assert "Verification failed" in result["error"]