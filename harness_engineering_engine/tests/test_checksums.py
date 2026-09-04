#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for checksum helpers."""
from __future__ import print_function

import tempfile
from pathlib import Path

from harness_engineering_engine.handlers.checksums import compute_content_checksum


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
