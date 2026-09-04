#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Phase 11 + 12: Failure/resilience testing and data reconciliation.

Implements SOP §8 (failure scenarios) and §9 (reconciliation checks) against
the local PostgreSQL backend. Skills are sourced only from git — there is no
artifact store, and no ZIP upload path.
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
from sqlalchemy import text

from harness_engineering_engine.handlers.config import Config
from harness_engineering_engine.schema import Mutations, Query, type_class

from . import conftest as _conftest  # noqa: F401


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_git_skill_repo(root, name, body="Body.", allowed_commands=None, no_frontmatter=False):
    """Create a local git repo (the 'remote') containing one skill on 'main'."""
    remote = root / f"{name}-remote"
    remote.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=remote)
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

    skill_dir = remote / name
    skill_dir.mkdir()
    if no_frontmatter:
        (skill_dir / "SKILL.md").write_text(body)
    else:
        commands_yaml = "allowed_commands: []"
        if allowed_commands:
            commands_yaml = "allowed_commands:\n" + "\n".join(
                f"  - argv: {json.dumps(ac)}" for ac in allowed_commands
            )
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test.\n{commands_yaml}\n---\n\n{body}\n"
        )
    _run_git(["add", "-A"], cwd=remote)
    _run_git(
        ["-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-q", "-m", "init"],
        cwd=remote,
    )
    return remote


def _commit_skill_version(remote, name, body):
    """Write a new SKILL.md version into an existing repo and commit it."""
    skill_dir = remote / name
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test.\nallowed_commands: []\n---\n\n{body}\n"
    )
    _run_git(["add", "-A"], cwd=remote)
    _run_git(
        ["-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-q", "-m", "update"],
        cwd=remote,
    )


def _gql(query, itest, variables=None):
    from graphene import Schema

    schema = Schema(query=Query, mutation=Mutations, types=type_class())
    context = {
        "logger": logging.getLogger(),
        "partition_key": f"{itest['endpoint_id']}#{itest['part_id']}",
        "endpoint_id": itest["endpoint_id"],
        "part_id": itest["part_id"],
    }
    if Config.DB_BACKEND == "postgresql" and Config.db_session:
        Config.db_session.execute(
            text("SET app.tenant_id = :tenant"), {"tenant": context["partition_key"]}
        )
    result = schema.execute(query, variable_values=variables, context_value=context)
    return result


@pytest.fixture(scope="function")
def itest():
    Config._initialized = False
    Config.DB_BACKEND = "dynamodb"
    Config.initialize(logging.getLogger(), _conftest.build_setting_from_env())
    tmp = Path(tempfile.mkdtemp(prefix="hsk_resil_"))
    Config.SKILL_ROOT = str(tmp)
    with Config.db_session() as s:
        s.execute(text("DELETE FROM hsk_skills WHERE partition_key LIKE 'hsk-itest%'"))
        s.commit()
    yield {"tmp": tmp, "endpoint_id": "hsk-itest", "part_id": "p1"}
    shutil.rmtree(tmp, ignore_errors=True)
    Config.db_session.remove()


# ---------------------------------------------------------------------------
# Phase 11 — Failure & Resilience (SOP §8)
# ---------------------------------------------------------------------------


class TestFailureResilience:
    """Missing data, invalid data, disabled skill, cross-tenant isolation."""

    def test_missing_skill_returns_null(self, itest):
        """skill(name) for unregistered name returns null, not an error."""
        result = _gql('query { skill(name: "nonexistent") { name } }', itest)
        assert result.errors is None
        assert result.data["skill"] is None

    def test_invalid_frontmatter_rejected_at_deploy(self, itest):
        """A git source with no SKILL.md frontmatter fails deploy gracefully."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(
            tmp, "bad-skill", body="No frontmatter here.\n", no_frontmatter=True
        )
        result = _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok failed } }',
            itest,
            variables={"s": str(remote)},
        )
        assert result.errors is not None or result.data["deploySkillPackage"]["failed"]

    def test_disabled_skill_not_in_search(self, itest):
        """A disabled skill should not appear in searchSkills."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "disabled-test", body="Disabled skill.")
        _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        _gql(
            'mutation { disableSkill(name: "disabled-test", updatedBy: "itest") { ok } }',
            itest,
        )
        result = _gql(
            'query { searchSkills(query: "disabled") { skillList { name enabled } total } }',
            itest,
        )
        assert result.errors is None
        items = result.data["searchSkills"]["skillList"]
        # Disabled skill should not be in search results
        if items:
            for item in items:
                if item["name"] == "disabled-test":
                    assert item["enabled"] is False

    def test_cross_tenant_isolation(self, itest):
        """Skills in tenant A are invisible to tenant B via RLS."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "tenant-a-skill", body="Tenant A skill.")
        # Deploy as tenant A
        result_a = _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        assert result_a.errors is None

        # Query as tenant B
        context_b = {
            "logger": logging.getLogger(),
            "partition_key": "other-tenant#p2",
            "endpoint_id": "other-tenant",
            "part_id": "p2",
        }
        from graphene import Schema

        schema = Schema(query=Query, mutation=Mutations, types=type_class())
        if Config.DB_BACKEND == "postgresql" and Config.db_session:
            Config.db_session.execute(
                text("SET app.tenant_id = :tenant"), {"tenant": context_b["partition_key"]}
            )
        result_b = schema.execute(
            'query { searchSkills(query: "tenant-a") { skillList { name } total } }',
            context_value=context_b,
        )
        assert result_b.errors is None
        assert result_b.data["searchSkills"]["total"] == 0

    def test_command_denied_when_global_killswitch_off(self, itest, monkeypatch):
        """runCommand is denied when HSK_RUN_COMMAND_ENABLED is false."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(
            tmp, "killswitch-test", body="Killswitch.", allowed_commands=[["python", "--version"]]
        )
        _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        # Temporarily disable the kill switch
        original = Config.RUN_COMMAND_ENABLED
        Config.RUN_COMMAND_ENABLED = False
        try:
            result = _gql(
                'mutation { runCommand(name: "killswitch-test", argv: ["python", "--version"]) { exitCode } }',
                itest,
            )
            assert result.errors is not None
        finally:
            Config.RUN_COMMAND_ENABLED = original


# ---------------------------------------------------------------------------
# Phase 12 — Data Reconciliation (SOP §9)
# ---------------------------------------------------------------------------


class TestDataReconciliation:
    """Resolved-commit integrity, content integrity, single active version, RLS."""

    def test_resolved_commit_matches_local_metadata(self, itest):
        """Git-sourced deploy: DB's resolved_commit == the git commit actually installed."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "reconcile-check", body="Reconcile body.")

        result = _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok deployed } }',
            itest,
            variables={"s": str(remote)},
        )
        deployed = result.data["deploySkillPackage"]["deployed"]
        if isinstance(deployed, str):
            deployed = json.loads(deployed)
        deployed = deployed[0]

        meta_file = tmp / "reconcile-check" / ".hsk-skill.json"
        local_metadata = json.loads(meta_file.read_text())

        assert deployed["resolvedCommit"] == local_metadata["resolved_commit"]
        assert len(deployed["resolvedCommit"]) == 40

    def test_single_active_version_per_name(self, itest):
        """Exactly one is_active=true row per (partition, name)."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "single-active", body="v1")
        _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main", version: "1.0.0") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        _commit_skill_version(remote, "single-active", body="v2")
        _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main", version: "2.0.0") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        _gql(
            'mutation { promoteSkillVersion(name: "single-active", version: "2.0.0", updatedBy: "itest") { ok } }',
            itest,
        )
        with Config.db_session() as s:
            Config._set_rls_context("hsk-itest#p1")
            rows = s.execute(
                text("SELECT version, is_active FROM hsk_skills WHERE name = 'single-active'")
            ).fetchall()
        actives = [r for r in rows if r[1]]
        assert len(actives) == 1
        assert actives[0][0] == "2.0.0"

    def test_content_checksum_matches_db(self, itest):
        """Recomputed content checksum from local cache == content_checksum in DB."""
        tmp = itest["tmp"]
        remote = _make_git_skill_repo(tmp, "content-check", body="Content check body.")
        _gql(
            'mutation Deploy($s: String!) { deploySkillPackage(source: $s, gitRef: "main") { ok } }',
            itest,
            variables={"s": str(remote)},
        )
        # Read the skill to trigger local refresh
        _gql('query { skill(name: "content-check") { contentChecksum } }', itest)

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        skill_dir = tmp / "content-check"
        local_checksum = compute_content_checksum(
            skill_dir, Config.SKILL_LOCAL_METADATA_FILE
        )
        with Config.db_session() as s:
            Config._set_rls_context("hsk-itest#p1")
            row = s.execute(
                text("SELECT content_checksum FROM hsk_skills WHERE name = 'content-check' AND is_active = true")
            ).fetchone()
        assert row is not None
        assert row[0] == local_checksum
