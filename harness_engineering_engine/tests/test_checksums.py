#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for checksum helpers."""
from __future__ import print_function

import tempfile
from pathlib import Path

from harness_engineering_engine.handlers.checksums import (
    GENERATED_SIDECAR_FILENAME,
    compute_content_checksum,
)


class TestChecksums:
    def test_compute_content_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )
            (skill_dir / "helper.py").write_text("print('hello')\n")
            (skill_dir / ".hsk-skill.json").write_text("{}")

            checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
            assert isinstance(checksum, str)
            assert len(checksum) == 64

    def test_checksum_changes_on_file_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )

            c1 = compute_content_checksum(skill_dir, ".hsk-skill.json")
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody changed.\n"
            )
            c2 = compute_content_checksum(skill_dir, ".hsk-skill.json")
            assert c1 != c2

    def test_excludes_hidden_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )

            without_git = compute_content_checksum(skill_dir, ".hsk-skill.json")

            git_dir = skill_dir / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("should be ignored")
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

            with_git = compute_content_checksum(skill_dir, ".hsk-skill.json")

            # A hidden directory's contents must not affect the checksum —
            # two clones of the same commit can have differently-shaped
            # .git internals depending on how they were fetched.
            assert with_git == without_git

    def test_excludes_generated_sidecar(self):
        """P9's sidecar must never affect content_checksum — writing or
        regenerating it must not make an already-registered version look
        stale on the next refresh."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )

            without_sidecar = compute_content_checksum(skill_dir, ".hsk-skill.json")

            (skill_dir / GENERATED_SIDECAR_FILENAME).write_text(
                '{"allowed_commands": [{"argv": ["msv", "status"]}]}'
            )

            with_sidecar = compute_content_checksum(skill_dir, ".hsk-skill.json")

            assert with_sidecar == without_sidecar

    def test_excluded_relpaths_ignores_only_the_exact_path(self):
        """A file pulled in from elsewhere in the repo (see reference_pull)
        must not affect the checksum, but excluding it must be scoped to
        its exact relative path — a different, deliberately-authored file
        that happens to share a basename elsewhere in the skill's own
        directory must still count."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )

            baseline = compute_content_checksum(skill_dir, ".hsk-skill.json")

            (skill_dir / "config").mkdir()
            (skill_dir / "config" / "shared.yaml").write_text("pulled in content\n")

            with_pulled_excluded = compute_content_checksum(
                skill_dir,
                ".hsk-skill.json",
                excluded_relpaths={"config/shared.yaml"},
            )
            assert with_pulled_excluded == baseline

            with_pulled_included = compute_content_checksum(skill_dir, ".hsk-skill.json")
            assert with_pulled_included != baseline

            (skill_dir / "other").mkdir()
            (skill_dir / "other" / "shared.yaml").write_text("a real, authored file\n")
            with_authored_sibling = compute_content_checksum(
                skill_dir,
                ".hsk-skill.json",
                excluded_relpaths={"config/shared.yaml"},
            )
            assert with_authored_sibling != with_pulled_excluded
