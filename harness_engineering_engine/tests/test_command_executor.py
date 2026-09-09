#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for the guarded command executor."""
from __future__ import print_function

import logging
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_engineering_engine.handlers.command_executor import execute_command


class FakeInfo:
    """Minimal GraphQL info mock for testing."""

    def __init__(self, partition_key: str = "test-partition"):
        self.context = {
            "logger": MagicMock(spec=logging.Logger),
            "partition_key": partition_key,
        }


class TestCommandExecutor:
    """Unit tests for the guarded command executor."""

    @contextmanager
    def _mock_config(self, tmp_dir: str, enabled: bool = True, dry_run: bool = False):
        """Patch Config with test-friendly values."""
        with patch(
            "harness_engineering_engine.handlers.command_executor.Config"
        ) as mock_config:
            mock_config.RUN_COMMAND_ENABLED = enabled
            mock_config.DRY_RUN = dry_run
            mock_config.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS = 5
            mock_config.RUN_COMMAND_OUTPUT_LIMIT_BYTES = 10_000
            mock_config.SKILL_ROOT = tmp_dir
            mock_config.SKILL_LOCAL_METADATA_FILE = ".hsk-skill.json"
            yield mock_config

    def _write_skill(self, root: Path, name: str):
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            f"---\nname: {name}\ndescription: Test skill.\n"
            f"allowed_commands: []\n---\n\nBody.\n"
        )
        metadata = skill_dir / ".hsk-skill.json"
        metadata.write_text("{}")
        return skill_dir

    def test_kill_switch_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            with self._mock_config(td, enabled=False):
                with pytest.raises(RuntimeError, match="HSK_RUN_COMMAND_ENABLED is false"):
                    execute_command(FakeInfo(), "test-skill", ["python", "--version"])

    def test_skill_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.skill_reader._get_active_skill"
                ) as mock_active:
                    mock_active.return_value = None
                    with pytest.raises(ValueError, match="not enabled or does not exist"):
                        execute_command(FakeInfo(), "missing-skill", ["python", "--version"])

    def test_no_allowed_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {"allowed_commands": []}
                    with pytest.raises(PermissionError, match="no allowed_commands"):
                        execute_command(FakeInfo(), "test-skill", ["python", "--version"])

    def test_shell_metacharacters_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": ["python", "--version"]}],
                    }
                    with pytest.raises(PermissionError, match="Shell metacharacters"):
                        execute_command(
                            FakeInfo(), "test-skill", ["python", "--version", "|", "tee"]
                        )

    def test_non_allowlisted_command_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": ["python", "--version"]}],
                    }
                    with pytest.raises(
                        PermissionError, match="does not match any allowed_commands"
                    ):
                        execute_command(FakeInfo(), "test-skill", ["rm", "-rf", "/"])

    def test_allowlisted_command_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": ["python", "--version"]}],
                    }
                    with patch(
                        "harness_engineering_engine.handlers.command_executor.resolve_skill_root"
                    ) as mock_root:
                        mock_root.return_value = root
                        result = execute_command(
                            FakeInfo(), "test-skill", ["python", "--version"]
                        )
                        assert result["exit_code"] == 0
                        assert result["timed_out"] is False
                        assert isinstance(result["truncated"], bool)

    def test_none_subprocess_output_is_normalized(self):
        """Treat missing stdout/stderr from subprocess as empty strings."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": ["ioa", "research", "summary"]}],
                    }
                    with patch(
                        "harness_engineering_engine.handlers.command_executor.resolve_skill_root"
                    ) as mock_root:
                        mock_root.return_value = root
                        with patch(
                            "harness_engineering_engine.handlers.command_executor.subprocess.run"
                        ) as mock_run:
                            mock_run.return_value = MagicMock(
                                stdout=None,
                                stderr=None,
                                returncode=0,
                            )

                            result = execute_command(
                                FakeInfo(), "test-skill", ["ioa", "research", "summary"]
                            )

                            assert result["stdout"] == ""
                            assert result["stderr"] == ""
                            assert result["exit_code"] == 0
                            assert result["truncated"] is False

    def test_utf8_subprocess_output_is_decoded(self):
        """Decode UTF-8 CLI output consistently on Windows hosts."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = self._write_skill(root, "test-skill")
            script = skill_dir / "emit_utf8.py"
            script.write_text(
                "import sys\nsys.stdout.buffer.write('Vitamin C — Market Summary'.encode('utf-8'))\n",
                encoding="utf-8",
            )

            with self._mock_config(td):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": [sys.executable, "emit_utf8.py"]}],
                    }
                    with patch(
                        "harness_engineering_engine.handlers.command_executor.resolve_skill_root"
                    ) as mock_root:
                        mock_root.return_value = root

                        result = execute_command(
                            FakeInfo(), "test-skill", [sys.executable, "emit_utf8.py"]
                        )

                        assert result["stdout"] == "Vitamin C — Market Summary"
                        assert result["stderr"] == ""
                        assert result["exit_code"] == 0
                        assert result["truncated"] is False

    def test_glob_allowlist_match(self):
        """Verify that glob patterns in allowed_commands match actual argv."""
        from harness_engineering_engine.handlers.command_executor import (
            _match_allowed_command,
        )

        entry = _match_allowed_command(
            ["python", "scripts/build.py", "--spec", "spec.json"],
            [{"argv": ["python", "scripts/build.py", "--spec", "*.json"]}],
        )
        assert entry is not None

        entry = _match_allowed_command(
            ["python", "scripts/build.py", "--spec", "spec.xml"],
            [{"argv": ["python", "scripts/build.py", "--spec", "*.json"]}],
        )
        assert entry is None

        entry = _match_allowed_command(
            ["python", "scripts/build.py"],
            [{"argv": ["python", "scripts/build.py", "--spec", "*.json"]}],
        )
        assert entry is None

    def test_dry_run_returns_without_executing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_skill(root, "test-skill")

            with self._mock_config(td, dry_run=True):
                with patch(
                    "harness_engineering_engine.handlers.command_executor._get_skill"
                ) as mock_get:
                    mock_get.return_value = {
                        "allowed_commands": [{"argv": ["python", "--version"]}],
                    }
                    with patch(
                        "harness_engineering_engine.handlers.command_executor.resolve_skill_root"
                    ) as mock_root:
                        mock_root.return_value = root
                        result = execute_command(
                            FakeInfo(), "test-skill", ["python", "--version"]
                        )
                        assert "DRY-RUN" in result["stdout"]
                        assert result["exit_code"] == "0"
                        assert result["timed_out"] is False
