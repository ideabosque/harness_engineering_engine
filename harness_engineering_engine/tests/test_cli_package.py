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
            git_repository_url="https://github.com/example/test-pkg.git",
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
            git_repository_url="https://github.com/example/update-pkg.git",
            version="1.0.0",
            updated_by="itest",
        )
        # Update version
        result = register_cli_package(
            FakeInfo(),
            package_name="update-pkg",
            git_repository_url="https://github.com/example/update-pkg.git",
            version="2.0.0",
            git_ref="v2.0.0",
            updated_by="itest",
        )
        assert result["operation"] == "updated"
        assert result["version"] == "2.0.0"


class TestEnsurePackage:
    """``git_client.resolve_ref_sha`` is mocked in every test below — without
    it, ``ensure_package``'s new commit-staleness check (§ lazy install
    redesign) would attempt a real ``git ls-remote`` against these
    fake/example URLs on every call, which is slow and network-dependent
    for no test value. A resolution failure already falls back to the
    version-only check (see ``_commit_confirmed_current``), so omitting the
    mock wouldn't break these tests — it would just make them flaky and
    slow, which is exactly why it's mocked instead of left alone."""

    def test_unregistered_package_returns_error(self, itest):
        result = ensure_package(FakeInfo(), "nonexistent-pkg")
        assert result["status"] == "error"
        assert "not registered" in result["error"]

    def test_matching_version_no_action(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="matching-pkg",
            git_repository_url="https://github.com/example/matching-pkg.git",
            version="0.0.1",  # pytest is typically installed at this version or higher
            git_ref="v0.0.1",
            updated_by="itest",
        )
        # Mock the installed version to match the registered version
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            return_value="0.0.1",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha000",
        ):
            result = ensure_package(FakeInfo(), "matching-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "0.0.1"

    def test_missing_package_triggers_install(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="missing-pkg",
            git_repository_url="https://github.com/example/missing-pkg.git",
            version="1.2.3",
            git_ref="v1.2.3",
            updated_by="itest",
        )
        # Mock: not installed → install succeeds → verification matches
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=[None, None, "1.2.3"],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha123",
        ):
            result = ensure_package(FakeInfo(), "missing-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "1.2.3"

    def test_outdated_package_triggers_upgrade(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="outdated-pkg",
            git_repository_url="https://github.com/example/outdated-pkg.git",
            version="2.0.0",
            git_ref="v2.0.0",
            updated_by="itest",
        )
        # Mock: installed=1.0.0 → uninstall succeeds → install succeeds → verified=2.0.0
        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            side_effect=["1.0.0", "1.0.0", "2.0.0"],
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_uninstall",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha456",
        ):
            result = ensure_package(FakeInfo(), "outdated-pkg")
        assert result["status"] == "ready"
        assert result["version"] == "2.0.0"

    def test_install_failure_blocks_execution(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="fail-pkg",
            git_repository_url="https://github.com/example/fail-pkg.git",
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
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha789",
        ):
            result = ensure_package(FakeInfo(), "fail-pkg")
        assert result["status"] == "error"
        assert "Install failed" in result["error"]

    def test_verification_failure_blocks_execution(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="verify-fail-pkg",
            git_repository_url="https://github.com/example/verify-fail-pkg.git",
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
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakeshaabc",
        ):
            result = ensure_package(FakeInfo(), "verify-fail-pkg")
        assert result["status"] == "error"
        assert "Verification failed" in result["error"]


class TestEnsurePackageCommitStaleness:
    """§ lazy install redesign: a floating ``git_ref`` (e.g. ``main``) can
    gain new commits without the skill author ever bumping the declared
    ``version`` string — a version-string-only comparison would silently
    miss that. ``ensure_package`` now also tracks, in a host-local marker
    file, which commit was last installed, and reinstalls on drift even
    when the version string is unchanged.
    """

    def test_matching_version_and_commit_skips_reinstall(self, itest):
        register_cli_package(
            FakeInfo(),
            package_name="stable-pkg",
            git_repository_url="https://github.com/example/stable-pkg.git",
            version="1.0.0",
            git_ref="main",
            updated_by="itest",
        )
        from harness_engineering_engine.handlers.cli_package_manager import (
            _write_install_marker,
        )

        _write_install_marker(
            "stable-pkg", "stable-pkg", "1.0.0", "fakesha-same",
            "https://github.com/example/stable-pkg.git", "main",
        )

        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            return_value="1.0.0",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha-same",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install"
        ) as mock_install, patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_uninstall"
        ) as mock_uninstall:
            result = ensure_package(FakeInfo(), "stable-pkg")

        assert result["status"] == "ready"
        mock_install.assert_not_called()
        mock_uninstall.assert_not_called()

    def test_commit_drift_triggers_reinstall_despite_unchanged_version(self, itest):
        """The registered version string never changes (floating git_ref
        case), but the ref's commit moved — this must still reinstall."""
        register_cli_package(
            FakeInfo(),
            package_name="floating-pkg",
            git_repository_url="https://github.com/example/floating-pkg.git",
            version="1.0.0",
            git_ref="main",
            updated_by="itest",
        )
        from harness_engineering_engine.handlers.cli_package_manager import (
            _read_install_marker,
            _write_install_marker,
        )

        _write_install_marker(
            "floating-pkg", "floating-pkg", "1.0.0", "fakesha-old",
            "https://github.com/example/floating-pkg.git", "main",
        )

        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            return_value="1.0.0",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha-new",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ) as mock_install, patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_uninstall",
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""},
        ) as mock_uninstall:
            result = ensure_package(FakeInfo(), "floating-pkg")

        assert result["status"] == "ready"
        mock_uninstall.assert_called_once()
        mock_install.assert_called_once()

        marker = _read_install_marker("floating-pkg")
        assert marker["resolved_commit"] == "fakesha-new"

    def test_marker_backfilled_when_absent_and_version_already_matches(self, itest):
        """A package installed before this commit-tracking existed has no
        marker yet — a matching version string is still trusted (no forced
        reinstall), but the marker gets backfilled for the next check."""
        register_cli_package(
            FakeInfo(),
            package_name="preexisting-pkg",
            git_repository_url="https://github.com/example/preexisting-pkg.git",
            version="1.0.0",
            git_ref="main",
            updated_by="itest",
        )
        from harness_engineering_engine.handlers.cli_package_manager import (
            _read_install_marker,
        )

        assert _read_install_marker("preexisting-pkg") is None

        with patch(
            "harness_engineering_engine.handlers.cli_package_manager._get_installed_version",
            return_value="1.0.0",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager.git_client.resolve_ref_sha",
            return_value="fakesha-baseline",
        ), patch(
            "harness_engineering_engine.handlers.cli_package_manager._pip_install"
        ) as mock_install:
            result = ensure_package(FakeInfo(), "preexisting-pkg")

        assert result["status"] == "ready"
        mock_install.assert_not_called()

        marker = _read_install_marker("preexisting-pkg")
        assert marker is not None
        assert marker["resolved_commit"] == "fakesha-baseline"