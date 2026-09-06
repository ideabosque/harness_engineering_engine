#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for pulling reference_files in from elsewhere in a repo clone."""
from __future__ import print_function

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from harness_engineering_engine.handlers.reference_pull import pull_reference_files


def _logger():
    return MagicMock(spec=logging.Logger)


class TestPullReferenceFiles:
    def test_file_already_in_skill_dir_is_left_alone_and_not_marked_pulled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "notes.md").write_text("native content")

            resolved, pulled = pull_reference_files(
                _logger(), content_root, skill_dir, ["notes.md"]
            )

            assert resolved == ["notes.md"]
            assert pulled == set()

    def test_file_elsewhere_in_repo_is_copied_in_and_marked_pulled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (content_root / "config").mkdir()
            (content_root / "config" / "design_system.yaml").write_text("tokens: {}\n")

            resolved, pulled = pull_reference_files(
                _logger(),
                content_root,
                skill_dir,
                ["config/design_system.yaml"],
            )

            assert resolved == ["config/design_system.yaml"]
            assert pulled == {"config/design_system.yaml"}
            copied = skill_dir / "config" / "design_system.yaml"
            assert copied.is_file()
            assert copied.read_text() == "tokens: {}\n"

    def test_missing_entry_is_dropped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            logger = _logger()

            resolved, pulled = pull_reference_files(
                logger, content_root, skill_dir, ["nowhere.md"]
            )

            assert resolved == []
            assert pulled == set()
            logger.warning.assert_called_once()

    def test_skill_md_is_never_pulled_even_if_named(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)

            resolved, pulled = pull_reference_files(
                _logger(), content_root, skill_dir, ["SKILL.md"]
            )

            assert resolved == []
            assert pulled == set()

    def test_traversal_outside_repo_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            outside = root / "outside.txt"
            outside.write_text("secret")

            logger = _logger()
            resolved, pulled = pull_reference_files(
                logger, content_root, skill_dir, ["../../../outside.txt"]
            )

            assert resolved == []
            assert pulled == set()
            assert not (skill_dir / "outside.txt").exists()
            logger.warning.assert_called_once()

    def test_mixed_native_and_pulled_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_root = root / "repo"
            skill_dir = content_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "notes.md").write_text("native")
            (content_root / "docs").mkdir()
            (content_root / "docs" / "plan.md").write_text("shared plan")

            resolved, pulled = pull_reference_files(
                _logger(),
                content_root,
                skill_dir,
                ["notes.md", "docs/plan.md"],
            )

            assert resolved == ["notes.md", "docs/plan.md"]
            assert pulled == {"docs/plan.md"}
