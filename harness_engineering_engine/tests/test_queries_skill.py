#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for the skill(name) query resolver wiring.

Regression coverage for the fix to Known issue #1 in DEVELOPMENT_PLAN.md:
``resolve_skill`` must dispatch to ``handlers.skill_reader.skill`` for the
agent-facing ``name`` lookup (so the SKILL.md body and on-demand S3 refresh
are actually reachable through GraphQL), and fall back to a plain repo
lookup for the admin-facing ``skill_uuid`` lookup.
"""
from __future__ import print_function

import logging
from unittest.mock import MagicMock, patch

from harness_engineering_engine.queries.skill import resolve_skill
from harness_engineering_engine.types.skill import SkillType


class FakeInfo:
    def __init__(self):
        self.context = {
            "logger": MagicMock(spec=logging.Logger),
            "partition_key": "test-partition",
        }


class TestResolveSkill:
    def test_name_lookup_dispatches_to_skill_reader_and_returns_body(self):
        with patch(
            "harness_engineering_engine.queries.skill.read_skill"
        ) as mock_read:
            mock_read.return_value = {
                "name": "rfq-assistant",
                "version": "2026.08.24.1",
                "description": "RFQ workflow.",
                "body": "You are an RFQ assistant.",
                "allowed_commands": [{"argv": ["python", "scripts/build_quote.py"]}],
                "cli_packages": [],
                "local_content_checksum": "abc123",
                "stale_index": False,
            }

            result = resolve_skill(FakeInfo(), name="rfq-assistant")

            mock_read.assert_called_once()
            assert isinstance(result, SkillType)
            assert result.name == "rfq-assistant"
            assert result.body == "You are an RFQ assistant."
            assert result.allowed_commands == [
                {"argv": ["python", "scripts/build_quote.py"]}
            ]
            assert result.stale_index is False

    def test_name_not_found_returns_none_instead_of_raising(self):
        with patch(
            "harness_engineering_engine.queries.skill.read_skill"
        ) as mock_read:
            mock_read.side_effect = ValueError(
                "Skill 'missing-skill' is not enabled or does not exist."
            )

            result = resolve_skill(FakeInfo(), name="missing-skill")

            assert result is None

    def test_missing_skill_md_returns_none(self):
        with patch(
            "harness_engineering_engine.queries.skill.read_skill"
        ) as mock_read:
            mock_read.side_effect = FileNotFoundError("SKILL.md not found")

            result = resolve_skill(FakeInfo(), name="broken-skill")

            assert result is None

    def test_unexpected_error_propagates(self):
        with patch(
            "harness_engineering_engine.queries.skill.read_skill"
        ) as mock_read:
            mock_read.side_effect = RuntimeError("AWS S3 client is not initialized.")

            try:
                resolve_skill(FakeInfo(), name="rfq-assistant")
                assert False, "expected RuntimeError to propagate"
            except RuntimeError:
                pass

    def test_uuid_lookup_falls_back_to_repo(self):
        with patch(
            "harness_engineering_engine.queries.skill.read_skill"
        ) as mock_read, patch(
            "harness_engineering_engine.queries.skill.get_repo"
        ) as mock_get_repo:
            fake_repo = MagicMock()
            fake_repo.resolve_single.return_value = SkillType(
                skill_uuid="11111111-1111-1111-1111-111111111111",
                name="rfq-assistant",
            )
            mock_get_repo.return_value = fake_repo

            result = resolve_skill(
                FakeInfo(), skill_uuid="11111111-1111-1111-1111-111111111111"
            )

            mock_read.assert_not_called()
            fake_repo.resolve_single.assert_called_once()
            assert result.skill_uuid == "11111111-1111-1111-1111-111111111111"
