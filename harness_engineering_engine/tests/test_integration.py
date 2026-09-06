#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Integration test suite for the Harness Engineering platform (PostgreSQL backend).

Implements SOP scenarios INT-001, INT-002, INT-004, INT-005, INT-007 end-to-end
against the local dev stack: in-process ``dispatch_graphql`` and local
PostgreSQL (``hsk_skills`` + RLS). Skills are sourced only from git — there is
no artifact store, and no ZIP upload path. Tests clone a throwaway local repo
(a real ``git init`` + commit under a temp directory) so they exercise the
actual git plumbing without needing network access.

Every test writes GraphQL through the same entry point the gateway uses, with
explicit tenant context (``endpoint_id``/``part_id`` → ``partition_key``).
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.schema import Mutations, Query, type_class

from . import conftest as _conftest  # noqa: F401  (loads tests/.env)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_git_skill_repo(root: Path, name: str, body: str, allowed_commands=None) -> Path:
    """Create a local git repo (the 'remote') containing one skill on 'main'."""
    remote = root / f"{name}-remote"
    remote.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=remote)
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

    _commit_skill_version(remote, name, body, allowed_commands)
    return remote


def _commit_skill_version(remote: Path, name: str, body: str, allowed_commands=None) -> None:
    """Write a new SKILL.md version into an existing repo and commit it."""
    commands_yaml = "allowed_commands: []"
    if allowed_commands:
        commands_yaml = "allowed_commands:\n" + "\n".join(
            f"  - argv: {json.dumps(ac)}" for ac in allowed_commands
        )
    skill_dir = remote / name
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n{commands_yaml}\n---\n\n{body}\n"
    )
    _run_git(["add", "-A"], cwd=remote)
    _run_git(
        ["-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-q", "-m", "update"],
        cwd=remote,
    )


def _gql(query: str, itest: dict, variables=None):
    """Execute GraphQL directly via graphene Schema.execute — no gateway, no HTTP."""
    from graphene import Schema

    schema = Schema(query=Query, mutation=Mutations, types=type_class())

    context = {
        "logger": logging.getLogger(),
        "partition_key": f"{itest['endpoint_id']}#{itest['part_id']}",
        "endpoint_id": itest["endpoint_id"],
        "part_id": itest["part_id"],
    }

    # Set RLS context for PostgreSQL
    if Config.DB_BACKEND == "postgresql" and Config.db_session:
        from sqlalchemy import text
        Config.db_session.execute(
            text("SET app.tenant_id = :tenant"),
            {"tenant": context["partition_key"]},
        )

    result = schema.execute(
        query,
        variable_values=variables,
        context_value=context,
    )

    if result.errors:
        raise AssertionError(f"GraphQL errors: {result.errors}")
    return result.data


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


# ---------------------------------------------------------------------------
# INT-001 — registerSkills scans HSK_SKILL_ROOT and upserts the index
# ---------------------------------------------------------------------------


class TestRegisterSkills:
    def test_register_scans_and_upserts(self, itest):
        tmp = itest["tmp"]
        for name in ("alpha-skill", "beta-skill"):
            (tmp / name).mkdir()
            (tmp / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name}.\nallowed_commands: []\n---\n"
                f"\n{name} body.\n"
            )

        data = _gql(
            "mutation { registerSkills { ok updated failed } }", itest
        )["registerSkills"]
        assert data["ok"] is True
        assert sorted(data["updated"]) == ["alpha-skill", "beta-skill"]
        assert data["failed"] == []

        # Idempotent second run — unchanged content → skipped
        data2 = _gql(
            "mutation { registerSkills { ok updated skipped } }", itest
        )["registerSkills"]
        assert data2["updated"] == []
        assert sorted(data2["skipped"]) == ["alpha-skill", "beta-skill"]


# ---------------------------------------------------------------------------
# INT-002 — deploySkillPackage installs locally and registers (+auto-activate)
# ---------------------------------------------------------------------------


class TestDeploySkillPackage:
    def test_git_deploy_installs_locally_and_registers(self, itest):
        remote = _make_git_skill_repo(itest["tmp"], "git-skill", body="Git body v1.")
        data = _gql(
            """
            mutation Deploy($gitRepositoryUrl: String!) {
                deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, gitRef: "main") {
                    ok deployed failed
                }
            }
            """,
            itest,
            variables={"gitRepositoryUrl": str(remote)},
        )["deploySkillPackage"]
        assert data["ok"] is True
        assert data["failed"] == []
        # `deployed` is a JSONCamelCase scalar — parse the list from the dict
        deployed_list = data["deployed"] if isinstance(data["deployed"], list) else json.loads(data["deployed"])
        assert len(deployed_list) == 1

        deployed = deployed_list[0]
        assert deployed["isActive"] is True
        assert len(deployed["resolvedCommit"]) == 40

        # Installed directly into the local skill root — no artifact store.
        installed = itest["tmp"] / "git-skill" / "SKILL.md"
        assert installed.is_file()
        assert "Git body v1." in installed.read_text()


# ---------------------------------------------------------------------------
# INT-004 — skill(name) refreshes local cache straight from git when stale/missing
# ---------------------------------------------------------------------------


class TestOnDemandRefresh:
    def test_stale_local_cache_triggers_git_refresh(self, itest):
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "refresh-skill", body="Fresh body from git.")
        _gql(
            """
            mutation Deploy($gitRepositoryUrl: String!) {
                deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, gitRef: "main") { ok }
            }
            """,
            itest,
            variables={"gitRepositoryUrl": str(remote)},
        )

        first = _gql(
            'query { skill(name: "refresh-skill") { body localContentChecksum staleIndex } }',
            itest,
        )["skill"]
        assert first["body"] == "Fresh body from git."
        assert first["staleIndex"] is False

        # Corrupt local metadata so it no longer matches the active row
        meta_file = tmp / "refresh-skill" / ".hsk-skill.json"
        assert meta_file.is_file()
        meta = json.loads(meta_file.read_text())
        meta["resolved_commit"] = "0" * 40
        meta_file.write_text(json.dumps(meta))

        # skill(name) now launches a background refresh instead of blocking.
        # The stale content is returned immediately (SKILL.md exists locally),
        # and the metadata file is updated once the background thread completes.
        second = _gql(
            'query { skill(name: "refresh-skill") { body localContentChecksum staleIndex } }',
            itest,
        )["skill"]
        assert second["body"] == "Fresh body from git."

        # Wait for the background refresh to complete (max ~10s)
        import time as _time
        for _ in range(100):
            refreshed_meta = json.loads(meta_file.read_text())
            if refreshed_meta["resolved_commit"] != "0" * 40:
                break
            _time.sleep(0.1)

        assert refreshed_meta["resolved_commit"] != "0" * 40


# ---------------------------------------------------------------------------
# INT-005 — promoteSkillVersion / rollbackSkill lifecycle
# ---------------------------------------------------------------------------


class TestPromoteRollback:
    def test_promote_and_rollback_switch_active_version(self, itest):
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "lifecycle-skill", body="Body version ONE.")
        r1 = _gql(
            """
            mutation Deploy($gitRepositoryUrl: String!) {
                deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, gitRef: "main", version: "1.0.0") {
                    ok deployed
                }
            }
            """,
            itest,
            variables={"gitRepositoryUrl": str(remote)},
        )["deploySkillPackage"]
        r1_deployed = r1["deployed"] if isinstance(r1["deployed"], list) else json.loads(r1["deployed"])
        assert r1_deployed[0]["version"] == "1.0.0"

        # A second commit on the same remote gives a distinct resolved_commit.
        _commit_skill_version(remote, "lifecycle-skill", body="Body version TWO.")
        r2 = _gql(
            """
            mutation Deploy($gitRepositoryUrl: String!) {
                deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, gitRef: "main", version: "2.0.0") {
                    ok deployed
                }
            }
            """,
            itest,
            variables={"gitRepositoryUrl": str(remote)},
        )["deploySkillPackage"]
        r2_deployed = r2["deployed"] if isinstance(r2["deployed"], list) else json.loads(r2["deployed"])
        assert r2_deployed[0]["version"] == "2.0.0"
        assert r2_deployed[0]["isActive"] is False

        body1 = _gql(
            'query { skill(name: "lifecycle-skill") { body version } }', itest
        )["skill"]
        assert body1["version"] == "1.0.0"

        _gql(
            'mutation { promoteSkillVersion(name: "lifecycle-skill", version: "2.0.0", updatedBy: "itest") { ok } }',
            itest,
        )
        body2 = _gql(
            'query { skill(name: "lifecycle-skill") { body version } }', itest
        )["skill"]
        assert body2["version"] == "2.0.0"
        assert "TWO" in body2["body"]

        _gql(
            'mutation { rollbackSkill(name: "lifecycle-skill", version: "1.0.0", updatedBy: "itest") { ok } }',
            itest,
        )
        body3 = _gql(
            'query { skill(name: "lifecycle-skill") { body version } }', itest
        )["skill"]
        assert body3["version"] == "1.0.0"
        assert "ONE" in body3["body"]

        # Exactly one active version per name
        from sqlalchemy import text

        with Config.db_session() as s:
            rows = s.execute(
                text("SELECT version, is_active FROM hsk_skills WHERE name = 'lifecycle-skill'")
            ).fetchall()
        actives = [r for r in rows if r[1]]
        assert len(actives) == 1
        assert actives[0][0] == "1.0.0"


# ---------------------------------------------------------------------------
# INT-007 — Guarded command executor policy
# ---------------------------------------------------------------------------


class TestRunCommandPolicy:
    def _deploy_skill_with_command(self, itest):
        remote = _make_git_skill_repo(
            itest["tmp"],
            "cmd-skill",
            body="Command skill.",
            allowed_commands=[["python", "--version"]],
        )
        _gql(
            """
            mutation Deploy($gitRepositoryUrl: String!) {
                deploySkillPackage(gitRepositoryUrl: $gitRepositoryUrl, gitRef: "main") { ok }
            }
            """,
            itest,
            variables={"gitRepositoryUrl": str(remote)},
        )

    def test_allowlisted_command_executes(self, itest):
        self._deploy_skill_with_command(itest)
        data = _gql(
            'mutation { runCommand(name: "cmd-skill", argv: ["python", "--version"]) {'
            " stdout stderr exitCode timedOut truncated } }",
            itest,
        )["runCommand"]
        assert data["exitCode"] == "0"
        assert data["timedOut"] is False
        assert "Python" in data["stdout"] or "Python" in data["stderr"]

    def test_non_allowlisted_command_rejected(self, itest):
        self._deploy_skill_with_command(itest)
        with pytest.raises(AssertionError, match="does not match any allowed_commands"):
            _gql(
                'mutation { runCommand(name: "cmd-skill", argv: ["python", "-c", "print(1)"]) { exitCode } }',
                itest,
            )

    def test_shell_metacharacters_rejected(self, itest):
        self._deploy_skill_with_command(itest)
        with pytest.raises(AssertionError, match="Shell metacharacters"):
            _gql(
                'mutation { runCommand(name: "cmd-skill", argv: ["python", "--version", "|", "more"]) { exitCode } }',
                itest,
            )
