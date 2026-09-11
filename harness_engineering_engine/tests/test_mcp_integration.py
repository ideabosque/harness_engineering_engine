#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Integration tests: mcp_skill_provider × harness_engineering_engine.

Implements SOP scenarios INT-001 through INT-012 — validates that the four
MCP tools (search_skills, get_skill, run_command, poll_command) correctly
integrate with harness_engineering_engine's GraphQL schema through the
in-process dispatch_graphql entry point.

Every GraphQL call's query, variables, and response are captured to a JSON
log for the per-call function results report.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.schema import Mutations, Query, type_class

from . import conftest as _conftest  # noqa: F401  (loads tests/.env)

# ---------------------------------------------------------------------------
# Call log — captures every GraphQL call for the per-call report
# ---------------------------------------------------------------------------
_CALL_LOG = []
_CALL_LOG_PATH = None


def _set_call_log_path(path):
    global _CALL_LOG_PATH
    _CALL_LOG_PATH = path


def _log_call(query, variables, result, elapsed_ms):
    entry = {
        "query": query.strip()[:500],
        "variables": variables,
        "result": result,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    _CALL_LOG.append(entry)
    return entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _commit_skill_version(remote, name, body, allowed_commands=None,
                           cli_packages=None, references=None):
    commands_yaml = "allowed_commands: []"
    if allowed_commands:
        commands_yaml = "allowed_commands:\n" + "\n".join(
            f"  - argv: {json.dumps(ac)}" for ac in allowed_commands
        )
    packages_yaml = ""
    if cli_packages:
        packages_yaml = "cli_packages:\n" + "\n".join(
            f"  - name: {pkg}" for pkg in cli_packages
        )
    refs_yaml = ""
    if references:
        refs_yaml = "references:\n" + "\n".join(
            f"  - {ref}" for ref in references
        )

    skill_dir = remote / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n"
        f"{commands_yaml}\n{packages_yaml}\n{refs_yaml}\n---\n\n{body}\n"
    )
    _run_git(["add", "-A"], cwd=remote)
    _run_git(
        ["-c", "user.email=test@example.com", "-c", "user.name=test",
         "commit", "-q", "-m", f"update {name}"],
        cwd=remote,
    )


def _make_git_skill_repo(root, name, body, allowed_commands=None,
                          cli_packages=None, references=None):
    remote = root / f"{name}-remote"
    remote.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=remote)
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)
    _commit_skill_version(remote, name, body, allowed_commands,
                          cli_packages, references)
    return remote


def _gql(query, itest, variables=None):
    """Execute GraphQL directly via graphene Schema.execute — no gateway."""
    from graphene import Schema

    schema = Schema(query=Query, mutation=Mutations, types=type_class())
    context = {
        "logger": logging.getLogger(),
        "partition_key": f"{itest['endpoint_id']}#{itest['part_id']}",
        "endpoint_id": itest["endpoint_id"],
        "part_id": itest["part_id"],
    }

    if Config.DB_BACKEND == "postgresql" and Config.db_session:
        from sqlalchemy import text
        Config.db_session.execute(
            text("SET app.tenant_id = :tenant"),
            {"tenant": context["partition_key"]},
        )

    t0 = time.perf_counter()
    result = schema.execute(
        query,
        variable_values=variables,
        context_value=context,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result.errors:
        err_str = str(result.errors)
        _log_call(query, variables, {"errors": err_str}, elapsed_ms)
        raise AssertionError(f"GraphQL errors: {result.errors}")

    data = result.data
    _log_call(query, variables, data, elapsed_ms)
    return data


def _deploy_skill(itest, remote, name, git_ref="main"):
    """Deploy a skill from a local git repo via deploySkillPackage mutation.

    Schema (introspected):
      deploySkillPackage(gitRepositoryUrl: String!, skillName: String, gitRef: String, version: String)
      Returns: { ok, deployed, failed, skipped }
    """
    query = """
        mutation DeploySkill($gitRepositoryUrl: String!, $skillName: String, $gitRef: String) {
            deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, skillName: $skillName, gitRef: $gitRef) {
                ok
                deployed
                failed
                skipped
            }
        }
    """
    variables = {
        "gitRepositoryUrl": str(remote),
        "skillName": name,
        "gitRef": git_ref,
    }
    return _gql(query, itest, variables)["deploySkillPackage"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def itest():
    """Fresh isolated state per test: temp skill root + clean table."""
    Config._initialized = False
    Config.DB_BACKEND = "dynamodb"
    Config.initialize(logging.getLogger(), _conftest.build_setting_from_env())

    tmp = Path(tempfile.mkdtemp(prefix="hsk_itest_"))
    Config.SKILL_ROOT = str(tmp)

    from sqlalchemy import text
    with Config.db_session() as s:
        s.execute(text("DELETE FROM hsk_skills WHERE partition_key LIKE 'hsk-itest%'"))
        s.commit()

    yield {"tmp": tmp, "endpoint_id": "hsk-itest", "part_id": "p1"}

    shutil.rmtree(tmp, ignore_errors=True)
    Config.db_session.remove()


@pytest.fixture(scope="function", autouse=True)
def _reset_call_log():
    _CALL_LOG.clear()
    yield
    if _CALL_LOG_PATH and _CALL_LOG:
        with open(_CALL_LOG_PATH, "a") as f:
            for entry in _CALL_LOG:
                f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# INT-001 — search_skills → searchSkills (fuzzy ranking)
# ---------------------------------------------------------------------------

class TestINT001SearchSkills:
    def test_fuzzy_search_returns_ranked_results(self, itest):
        tmp = itest["tmp"]
        for name, body in [
            ("python-linter", "Lint Python code"),
            ("python-formatter", "Format Python code"),
            ("video-renderer", "Render videos"),
        ]:
            remote = _make_git_skill_repo(tmp, name, body)
            _deploy_skill(itest, remote, name)

        data = _gql(
            """
            query Search($query: String!) {
                searchSkills(query: $query) {
                    total
                    skillList { name description }
                }
            }
            """,
            itest,
            {"query": "python"},
        )["searchSkills"]

        names = [s["name"] for s in data["skillList"]]
        assert "python-linter" in names, f"python-linter not in {names}"
        assert "python-formatter" in names, f"python-formatter not in {names}"

        # python-* should rank above video-renderer if it appears
        python_idx = [i for i, s in enumerate(data["skillList"]) if "python" in s["name"]]
        video_idx = [i for i, s in enumerate(data["skillList"]) if "video" in s["name"]]
        if python_idx and video_idx:
            assert min(python_idx) < min(video_idx), \
                f"python skills should rank above video: {names}"


# ---------------------------------------------------------------------------
# INT-002 — get_skill → skill query (normal path)
# ---------------------------------------------------------------------------

class TestINT002GetSkill:
    def test_get_skill_returns_body_and_metadata(self, itest):
        tmp = itest["tmp"]
        body = "This is the SKILL.md body.\n\nRun `echo hello` to test."
        remote = _make_git_skill_repo(
            tmp, "test-skill", body,
            allowed_commands=[["echo", "hello"]],
            cli_packages=["echo"],
        )
        _deploy_skill(itest, remote, "test-skill")

        data = _gql(
            """
            query GetSkill($name: String!) {
                skill(name: $name) {
                    name
                    body
                    allowedCommands
                    cliPackages
                    status
                }
            }
            """,
            itest,
            {"name": "test-skill"},
        )["skill"]

        assert data is not None, "skill should be found"
        assert "SKILL.md body" in data["body"], \
            f"body mismatch: {data['body'][:100]}"
        assert data["allowedCommands"] is not None, "allowedCommands should exist"
        assert data["cliPackages"] is not None, "cliPackages should exist"
        # Normal path — status should not be "refreshing"
        assert data.get("status") != "refreshing", "should not be refreshing"


# ---------------------------------------------------------------------------
# INT-003 — get_skill with stale cache → non-blocking refresh
# ---------------------------------------------------------------------------

class TestINT003NonBlockingRefresh:
    def test_stale_cache_triggers_background_refresh(self, itest):
        tmp = itest["tmp"]
        body = "Original body v1."
        remote = _make_git_skill_repo(tmp, "refresh-skill", body)
        _deploy_skill(itest, remote, "refresh-skill")

        # Corrupt the local metadata to force a refresh
        skill_dir = tmp / "refresh-skill"
        meta_file = skill_dir / ".hsk-skill.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            meta["local_content_checksum"] = "stale_checksum"
            meta_file.write_text(json.dumps(meta))

        # First call — may return refreshing status or stale content
        data1 = _gql(
            """
            query GetSkill($name: String!) {
                skill(name: $name) {
                    name
                    body
                    status
                }
            }
            """,
            itest,
            {"name": "refresh-skill"},
        )["skill"]

        assert data1 is not None, "skill should be found"
        has_body = data1.get("body") is not None
        has_status = data1.get("status") == "refreshing"
        assert has_body or has_status, \
            f"Expected body or refreshing status, got: {data1}"

        # Wait for background refresh to complete
        time.sleep(3)

        # Second call — should have fresh content, no refreshing status
        data2 = _gql(
            """
            query GetSkill($name: String!) {
                skill(name: $name) {
                    name
                    body
                    status
                }
            }
            """,
            itest,
            {"name": "refresh-skill"},
        )["skill"]

        assert data2 is not None
        assert data2.get("body") is not None, \
            "body should be present after refresh"
        assert data2.get("status") != "refreshing", \
            "should not be refreshing after wait"


# ---------------------------------------------------------------------------
# INT-004 — run_command (sync) → runCommand mutation
# ---------------------------------------------------------------------------

class TestINT004SyncRunCommand:
    def test_sync_command_executes_and_returns_output(self, itest):
        tmp = itest["tmp"]
        body = "Python version skill."
        remote = _make_git_skill_repo(
            tmp, "echo-skill", body,
            allowed_commands=[["python", "--version"]],
        )
        _deploy_skill(itest, remote, "echo-skill")

        data = _gql(
            """
            mutation RunCmd($name: String!, $argv: [String!]!) {
                runCommand(name: $name, argv: $argv) {
                    stdout
                    stderr
                    exitCode
                    truncated
                    outputTruncatedBytes
                }
            }
            """,
            itest,
            {"name": "echo-skill", "argv": ["python", "--version"]},
        )["runCommand"]

        # python --version outputs to stdout (3.3+) or stderr (older)
        combined = (data["stdout"] or "") + (data["stderr"] or "")
        assert "Python" in combined, f"expected 'Python' in output: {combined}"
        assert data["exitCode"] == "0", f"exit code: {data['exitCode']}"
        assert data["truncated"] is False, "should not be truncated"
        assert data["outputTruncatedBytes"] == 0, "should be 0 bytes truncated"


# ---------------------------------------------------------------------------
# INT-005 — run_command rejected (not in allowlist)
# ---------------------------------------------------------------------------

class TestINT005CommandNotAllowed:
    def test_command_not_in_allowlist_rejected(self, itest):
        tmp = itest["tmp"]
        body = "Echo skill with limited allowlist."
        remote = _make_git_skill_repo(
            tmp, "locked-skill", body,
            allowed_commands=[["python", "--version"]],
        )
        _deploy_skill(itest, remote, "locked-skill")

        with pytest.raises(AssertionError, match="GraphQL errors"):
            _gql(
                """
                mutation RunCmd($name: String!, $argv: [String!]!) {
                    runCommand(name: $name, argv: $argv) {
                        stdout
                        exitCode
                    }
                }
                """,
                itest,
                {"name": "locked-skill", "argv": ["python", "-c", "print('hack')"]},
            )


# ---------------------------------------------------------------------------
# INT-006 — run_command (background=true) → run_id returned
# ---------------------------------------------------------------------------

class TestINT006BackgroundRunCommand:
    def test_background_command_returns_run_id_immediately(self, itest):
        tmp = itest["tmp"]
        body = "Background test skill."
        # Write a temp script to avoid shell metacharacter rejection (semicolons)
        script = tmp / "bg_script.py"
        script.write_text("import time\ntime.sleep(2)\nprint('done')\n")
        remote = _make_git_skill_repo(
            tmp, "bg-skill", body,
            allowed_commands=[["python", str(script)]],
        )
        _deploy_skill(itest, remote, "bg-skill")

        t0 = time.perf_counter()
        data = _gql(
            """
            mutation RunCmd($name: String!, $argv: [String!]!, $background: Boolean) {
                runCommand(name: $name, argv: $argv, background: $background) {
                    runId
                    stdout
                }
            }
            """,
            itest,
            {"name": "bg-skill", "argv": ["python", str(script)],
             "background": True},
        )["runCommand"]
        elapsed = time.perf_counter() - t0

        assert data["runId"] is not None, "run_id should be returned"
        assert elapsed < 2.0, f"background call took {elapsed:.1f}s — should be immediate"

        itest["run_id"] = data["runId"]


# ---------------------------------------------------------------------------
# INT-007 — poll_command → pollCommand query
# ---------------------------------------------------------------------------

class TestINT007PollCommand:
    def test_poll_command_running_then_completed(self, itest):
        tmp = itest["tmp"]
        body = "Background test skill."
        # Write a temp script to avoid shell metacharacter rejection
        script = tmp / "poll_script.py"
        script.write_text("import time\ntime.sleep(1)\nprint('done')\n")
        remote = _make_git_skill_repo(
            tmp, "poll-skill", body,
            allowed_commands=[["python", str(script)]],
        )
        _deploy_skill(itest, remote, "poll-skill")

        # Launch background command
        launch_data = _gql(
            """
            mutation RunCmd($name: String!, $argv: [String!]!, $background: Boolean) {
                runCommand(name: $name, argv: $argv, background: $background) {
                    runId
                    stdout
                }
            }
            """,
            itest,
            {"name": "poll-skill", "argv": ["python", str(script)],
             "background": True},
        )["runCommand"]

        run_id = launch_data["runId"]
        assert run_id is not None

        # Poll immediately — should be running (or already completed if fast)
        poll_data = _gql(
            """
            query Poll($run_id: String!) {
                pollCommand(run_id: $run_id) {
                    runId
                    status
                    stdout
                    exitCode
                }
            }
            """,
            itest,
            {"run_id": run_id},
        )["pollCommand"]

        assert poll_data["status"] in ("running", "completed"), \
            f"unexpected status: {poll_data['status']}"

        # Wait for completion
        time.sleep(2)

        # Poll again — should be completed
        poll_data2 = _gql(
            """
            query Poll($run_id: String!) {
                pollCommand(run_id: $run_id) {
                    runId
                    status
                    stdout
                    exitCode
                    outputTruncatedBytes
                }
            }
            """,
            itest,
            {"run_id": run_id},
        )["pollCommand"]

        assert poll_data2["status"] == "completed", \
            f"should be completed: {poll_data2['status']}"
        assert "done" in (poll_data2["stdout"] or ""), \
            f"stdout should contain 'done': {poll_data2['stdout']}"
        assert poll_data2["exitCode"] == "0", \
            f"exit code should be 0: {poll_data2['exitCode']}"


# ---------------------------------------------------------------------------
# INT-008 — poll_command with unknown run_id
# ---------------------------------------------------------------------------

class TestINT008PollUnknownRunId:
    def test_poll_unknown_run_id_returns_not_found(self, itest):
        data = _gql(
            """
            query Poll($run_id: String!) {
                pollCommand(run_id: $run_id) {
                    runId
                    status
                }
            }
            """,
            itest,
            {"run_id": "nonexistent-uuid-12345"},
        )["pollCommand"]

        assert data is not None, "should return a result"
        assert data["status"] == "not_found", \
            f"expected not_found, got: {data['status']}"


# ---------------------------------------------------------------------------
# INT-009 — shell metacharacters rejected
# ---------------------------------------------------------------------------

class TestINT009ShellMetacharacters:
    def test_shell_metacharacters_rejected(self, itest):
        tmp = itest["tmp"]
        body = "Python skill."
        remote = _make_git_skill_repo(
            tmp, "meta-skill", body,
            allowed_commands=[["python", "--version"]],
        )
        _deploy_skill(itest, remote, "meta-skill")

        with pytest.raises(AssertionError, match="GraphQL errors"):
            _gql(
                """
                mutation RunCmd($name: String!, $argv: [String!]!) {
                    runCommand(name: $name, argv: $argv) {
                        stdout
                    }
                }
                """,
                itest,
                {"name": "meta-skill", "argv": ["python", "--version; rm -rf /"]},
            )


# ---------------------------------------------------------------------------
# INT-010 — kill switch disabled
# ---------------------------------------------------------------------------

class TestINT010KillSwitch:
    def test_kill_switch_rejects_commands(self, itest):
        tmp = itest["tmp"]
        body = "Python skill."
        remote = _make_git_skill_repo(
            tmp, "kill-skill", body,
            allowed_commands=[["python", "--version"]],
        )
        _deploy_skill(itest, remote, "kill-skill")

        original = Config.RUN_COMMAND_ENABLED
        Config.RUN_COMMAND_ENABLED = False
        try:
            with pytest.raises(AssertionError, match="GraphQL errors"):
                _gql(
                    """
                    mutation RunCmd($name: String!, $argv: [String!]!) {
                        runCommand(name: $name, argv: $argv) {
                            stdout
                        }
                    }
                    """,
                    itest,
                    {"name": "kill-skill", "argv": ["python", "--version"]},
                )
        finally:
            Config.RUN_COMMAND_ENABLED = original


# ---------------------------------------------------------------------------
# INT-011 — get_skill for non-existent skill
# ---------------------------------------------------------------------------

class TestINT011SkillNotFound:
    def test_get_nonexistent_skill_returns_null(self, itest):
        data = _gql(
            """
            query GetSkill($name: String!) {
                skill(name: $name) {
                    name
                    body
                }
            }
            """,
            itest,
            {"name": "nonexistent-skill-xyz"},
        )["skill"]

        assert data is None, f"expected None for nonexistent skill, got: {data}"


# ---------------------------------------------------------------------------
# INT-012 — output truncation volume indicator
# ---------------------------------------------------------------------------

class TestINT012OutputTruncation:
    def test_large_output_truncated_with_volume_indicator(self, itest):
        tmp = itest["tmp"]
        body = "Big output skill."
        # Write a temp script to avoid shell metacharacter issues
        big_script = tmp / "big_output_script.py"
        big_script.write_text("print('x' * 50000)\n")
        remote = _make_git_skill_repo(
            tmp, "big-output-skill", body,
            allowed_commands=[["python", str(big_script)]],
        )
        _deploy_skill(itest, remote, "big-output-skill")

        data = _gql(
            """
            mutation RunCmd($name: String!, $argv: [String!]!) {
                runCommand(name: $name, argv: $argv) {
                    stdout
                    stderr
                    exitCode
                    truncated
                    outputTruncatedBytes
                }
            }
            """,
            itest,
            {"name": "big-output-skill", "argv": ["python", str(big_script)]},
        )["runCommand"]

        assert data["truncated"] is True, "should be truncated"
        assert data["outputTruncatedBytes"] > 0, \
            f"outputTruncatedBytes should be > 0: {data['outputTruncatedBytes']}"
        stdout_len = len(data["stdout"].encode("utf-8"))
        assert stdout_len <= Config.RUN_COMMAND_OUTPUT_LIMIT_BYTES, \
            f"stdout {stdout_len}B exceeds limit {Config.RUN_COMMAND_OUTPUT_LIMIT_BYTES}B"


# ---------------------------------------------------------------------------
# INT-013 — pollCommand survives a "different instance" (§18 G-6)
# ---------------------------------------------------------------------------

class TestINT013CrossInstancePoll:
    """End-to-end (real GraphQL dispatch, real Postgres) proof of §18 G-6.

    Launches a background command through the actual ``runCommand`` mutation
    (not a handler call with a fake context), confirms the run was mirrored
    to ``hsk_command_runs``, then simulates the poll landing on a *different*
    gateway instance by evicting the process-local registry entry — the only
    thing that is genuinely process-local and therefore the only thing a
    second process wouldn't have. ``pollCommand`` must still resolve the
    correct final status/output/exit_code from the DB instead of `not_found`.
    """

    def test_poll_after_local_registry_eviction_falls_back_to_db(self, itest):
        from harness_engineering_engine.handlers.async_command_executor import (
            _registry,
            _registry_lock,
        )
        from harness_engineering_engine.models.repositories import get_repo

        tmp = itest["tmp"]
        body = "Cross-instance poll test skill."
        script = tmp / "cross_instance_script.py"
        script.write_text("print('cross-instance output')\n")
        remote = _make_git_skill_repo(
            tmp, "cross-instance-skill", body,
            allowed_commands=[["python", str(script)]],
        )
        _deploy_skill(itest, remote, "cross-instance-skill")

        launch_data = _gql(
            """
            mutation RunCmd($name: String!, $argv: [String!]!, $background: Boolean) {
                runCommand(name: $name, argv: $argv, background: $background) {
                    runId
                }
            }
            """,
            itest,
            {"name": "cross-instance-skill", "argv": ["python", str(script)],
             "background": True},
        )["runCommand"]
        run_id = launch_data["runId"]
        assert run_id is not None

        # Wait for the background thread to finish and mirror completion to the DB.
        for _ in range(50):
            with _registry_lock:
                entry = _registry.get(run_id)
            if entry is not None and entry["status"] != "running":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("background command did not complete in time")

        # Prove the mutation itself (not a test harness shortcut) wrote the
        # row: read it back straight from the repository.
        partition_key = f"{itest['endpoint_id']}#{itest['part_id']}"
        db_row = get_repo("command_run").get(partition_key=partition_key, run_uuid=run_id)
        assert db_row is not None, "command_run row was not written by runCommand"
        assert db_row["status"] == "completed"
        assert "cross-instance output" in (db_row["stdout"] or "")

        # Simulate the poll landing on a different gateway instance: the
        # process-local dict — and only the process-local dict — is gone.
        with _registry_lock:
            _registry.pop(run_id, None)

        poll_data = _gql(
            """
            query Poll($run_id: String!) {
                pollCommand(run_id: $run_id) {
                    runId
                    status
                    stdout
                    exitCode
                }
            }
            """,
            itest,
            {"run_id": run_id},
        )["pollCommand"]

        assert poll_data["status"] == "completed", \
            f"expected DB fallback to report completed, got: {poll_data['status']}"
        assert "cross-instance output" in (poll_data["stdout"] or "")
        assert poll_data["exitCode"] == "0"