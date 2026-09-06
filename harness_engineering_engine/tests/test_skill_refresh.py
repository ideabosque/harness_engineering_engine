#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for skill_refresh.py's reference_files consistency with deploy.

A declared ``reference_files`` entry pointing at a path elsewhere in the
repo (e.g. shared config at a monorepo's root) must be pulled into the
installed skill directory on refresh too, the same way
``deploy_skill_package`` does it — and must match the checksum that deploy
registered, since refresh is what every *other* host uses to reproduce the
exact same registered version straight from git.
"""
from __future__ import print_function

import logging
import subprocess
from unittest.mock import MagicMock

from harness_engineering_engine.handlers.checksums import compute_content_checksum
from harness_engineering_engine.handlers.skill_refresh import refresh_single_skill


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_repo_with_declared_reference_at_root(tmp_path, name="rfq-assistant"):
    remote = tmp_path / "remote"
    remote.mkdir()
    _run(["git", "init", "-q"], cwd=remote)
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

    (remote / "config").mkdir()
    (remote / "config" / "shared.yaml").write_text("shared: true\n")

    skill_dir = remote / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\n"
        'allowed_commands: []\nreference_files:\n  - "config/shared.yaml"\n'
        "---\n\nBody references `config/shared.yaml`.\n"
    )

    _run(["git", "add", "-A"], cwd=remote)
    _run(
        [
            "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
            "commit", "-q", "-m", "init",
        ],
        cwd=remote,
    )

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(remote),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return remote, sha


class TestRefreshSingleSkillReferenceFiles:
    def test_declared_root_level_reference_is_pulled_and_checksum_matches_deploy(
        self, tmp_path
    ):
        remote, sha = _make_repo_with_declared_reference_at_root(tmp_path)

        # What deploy_skill_package would have registered: the skill's own
        # directory content, excluding the pulled-in repo-root file.
        expected_checksum = compute_content_checksum(
            remote / "rfq-assistant",
            ".hsk-skill.json",
            excluded_relpaths={"config/shared.yaml"},
        )

        active = {
            "name": "rfq-assistant",
            "version": "1.0",
            "git_repository_url": str(remote),
            "git_ref": "main",
            "resolved_commit": sha,
            "content_checksum": expected_checksum,
        }

        root = tmp_path / "skill_root"
        root.mkdir()
        logger = MagicMock(spec=logging.Logger)

        refresh_single_skill(logger, active, root)

        installed = root / "rfq-assistant" / "config" / "shared.yaml"
        assert installed.is_file()
        assert installed.read_text() == "shared: true\n"

    def test_checksum_mismatch_still_raised_for_real_content_drift(self, tmp_path):
        """The new reference-pull step must not accidentally swallow a
        genuine checksum mismatch on the skill's own authored content."""
        remote, sha = _make_repo_with_declared_reference_at_root(tmp_path)

        active = {
            "name": "rfq-assistant",
            "version": "1.0",
            "git_repository_url": str(remote),
            "git_ref": "main",
            "resolved_commit": sha,
            "content_checksum": "not-the-real-checksum",
        }

        root = tmp_path / "skill_root"
        root.mkdir()
        logger = MagicMock(spec=logging.Logger)

        try:
            refresh_single_skill(logger, active, root)
            assert False, "expected a checksum mismatch ValueError"
        except ValueError as e:
            assert "checksum mismatch" in str(e).lower()
