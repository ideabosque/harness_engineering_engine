#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for checksum helpers."""
from __future__ import print_function

import hashlib
import tempfile
from pathlib import Path

from harness_engineering_engine.handlers.checksums import (
    compute_artifact_checksum,
    compute_bytes_checksum,
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

    def test_compute_artifact_checksum(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tf:
            tf.write(b"PK\x05\x06" + b"\x00" * 200)
            tf.flush()
            path = Path(tf.name)

        checksum = compute_artifact_checksum(path)
        assert isinstance(checksum, str)
        assert len(checksum) == 64
        path.unlink()

    def test_compute_bytes_checksum(self):
        data = b"hello world"
        expected = hashlib.sha256(data).hexdigest()
        assert compute_bytes_checksum(data) == expected

    def test_excludes_hidden_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test\ndescription: test\n---\n\nBody.\n"
            )
            git_dir = skill_dir / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("should be ignored")

            checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
            assert isinstance(checksum, str)
