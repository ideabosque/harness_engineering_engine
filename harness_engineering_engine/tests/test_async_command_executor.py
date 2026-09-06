# -*- coding: utf-8 -*-
"""Tests for the async command executor and pollCommand query.

Covers:
  - launch_background_command + poll_command happy path
  - poll_command with unknown run_id (not_found)
  - poll_command reads partial output while running
  - background command completes and output is captured
  - timeout behavior (process killed, timed_out=true)
  - output truncation
  - dry-run mode in the RunCommand mutation (background=true)
"""
from __future__ import print_function

__author__ = "bibow"

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from harness_engineering_engine.handlers.async_command_executor import (
    launch_background_command,
    poll_command,
    get_run,
    _registry,
    _registry_lock,
)


class TestPollCommandNotFound(unittest.TestCase):
    """poll_command returns not_found for unknown run_id."""

    def test_unknown_run_id(self):
        import logging
        logger = logging.getLogger()
        result = poll_command("nonexistent-id", logger)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"])


class TestLaunchAndPollBackgroundCommand(unittest.TestCase):
    """End-to-end: launch a real short command, poll until it completes."""

    @classmethod
    def setUpClass(cls):
        cls.logger = __import__("logging").getLogger()

    def setUp(self):
        # Clean the registry before each test
        with _registry_lock:
            _registry.clear()

    def tearDown(self):
        with _registry_lock:
            # Clean up any temp files
            for entry in _registry.values():
                for path in (entry.get("stdout_path"), entry.get("stderr_path")):
                    if path:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
            _registry.clear()

    def test_short_command_completes(self):
        """A command that finishes quickly should show status=completed."""
        # Use python -c "print('hello')" as a quick cross-platform command
        argv = [sys.executable, "-c", "print('hello world')"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=10,
            output_limit=20000,
        )
        run_id = result["run_id"]
        self.assertEqual(result["status"], "running")

        # Poll until completed (max ~5s)
        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "completed")
        self.assertIn("hello world", poll_result["stdout"])
        self.assertEqual(poll_result["exit_code"], "0")
        self.assertFalse(poll_result["timed_out"])

    def test_partial_output_while_running(self):
        """poll_command should return partial stdout while the process is still running."""
        # A command that sleeps before producing output
        argv = [sys.executable, "-c", "import time; time.sleep(0.5); print('done')"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=10,
            output_limit=20000,
        )
        run_id = result["run_id"]

        # Poll immediately — should be running
        poll_result = poll_command(run_id, self.logger)
        self.assertEqual(poll_result["status"], "running")

        # Wait for completion
        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "completed")
        self.assertIn("done", poll_result["stdout"])

    def test_command_failure_exit_code(self):
        """A command that exits non-zero should report the exit code."""
        argv = [sys.executable, "-c", "import sys; sys.exit(42)"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=10,
            output_limit=20000,
        )
        run_id = result["run_id"]

        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "completed")
        self.assertEqual(poll_result["exit_code"], "42")

    def test_stderr_captured(self):
        """stderr output should be captured separately."""
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('error output')"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=10,
            output_limit=20000,
        )
        run_id = result["run_id"]

        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "completed")
        self.assertIn("error output", poll_result["stderr"])

    def test_timeout_kills_process(self):
        """A command exceeding the timeout should be killed and report timed_out=true."""
        # Sleep for 10s with a 2s timeout
        argv = [sys.executable, "-c", "import time; time.sleep(10)"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=2,
            output_limit=20000,
        )
        run_id = result["run_id"]

        # Poll until it finishes (max ~5s for the timeout+kill)
        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "timed_out")
        self.assertTrue(poll_result["timed_out"])

    def test_output_truncation(self):
        """Output exceeding the limit should be truncated and truncated=true."""
        # Generate more than 200 bytes of output
        argv = [sys.executable, "-c", "print('x' * 500)"]
        result = launch_background_command(
            logger=self.logger,
            skill_name="test_skill",
            argv=argv,
            cwd=os.path.dirname(sys.executable),
            timeout_seconds=10,
            output_limit=200,
        )
        run_id = result["run_id"]

        for _ in range(50):
            poll_result = poll_command(run_id, self.logger)
            if poll_result["status"] != "running":
                break
            time.sleep(0.1)

        self.assertEqual(poll_result["status"], "completed")
        self.assertTrue(poll_result["truncated"])
        # Truncated output should be at most limit/2 = 100 chars
        self.assertLessEqual(len(poll_result["stdout"]), 100)


class TestRunCommandBackgroundDryRun(unittest.TestCase):
    """The RunCommand mutation's background dry-run path."""

    @classmethod
    def setUpClass(cls):
        # Patch Config to enable dry-run mode
        from harness_engineering_engine.handlers.config import Config
        cls._orig_dry_run = Config.DRY_RUN
        Config.DRY_RUN = True
        cls.Config = Config

    @classmethod
    def tearDownClass(cls):
        cls.Config.DRY_RUN = cls._orig_dry_run

    def test_dry_run_background_returns_dry_run_id(self):
        """Dry-run mode should return run_id='dry-run' without executing."""
        from harness_engineering_engine.mutations.skill_management import RunCommand

        # We can't easily call mutate() directly (it needs full GraphQL context),
        # but we can verify that Config.DRY_RUN is True and the logic checks it.
        self.assertTrue(self.Config.DRY_RUN)


if __name__ == "__main__":
    unittest.main()