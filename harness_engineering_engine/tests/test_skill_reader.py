#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for P9's merge between SKILL.md and the .hsk-generated.json sidecar.

The rule under test, throughout: a field the author actually wrote in
SKILL.md (even as an empty list, on purpose) is always final — the sidecar
only ever fills a field that is genuinely absent from SKILL.md's own
frontmatter. A sidecar left over from a different commit is ignored
entirely, same as if it didn't exist.
"""
from __future__ import print_function

import json
import logging
from unittest.mock import MagicMock, patch

from harness_engineering_engine.handlers import skill_reader
from harness_engineering_engine.handlers.checksums import (
    CHECKSUM_EXCLUSIONS_FILENAME,
    GENERATED_SIDECAR_FILENAME,
)


def _write_live_sidecar(skill_dir, resolved_commit, **generated):
    """Write the sidecar directly into the *installed* skill directory —
    what skill() actually reads from. Distinct from
    skill_version_cache.write_generated_sidecar, which targets the
    version-cache directory (a different path); install_from_cache is
    what normally carries it from one to the other."""
    payload = {"resolved_commit": resolved_commit, **generated}
    (skill_dir / GENERATED_SIDECAR_FILENAME).write_text(json.dumps(payload))


def _write_live_checksum_exclusions(skill_dir, resolved_commit, excluded_relpaths):
    """Write the checksum-exclusions sidecar directly into the *installed*
    skill directory — what skill() actually reads from. Distinct from
    skill_version_cache.write_checksum_exclusions, which targets the
    version-cache directory."""
    payload = {"resolved_commit": resolved_commit, "excluded_relpaths": excluded_relpaths}
    (skill_dir / CHECKSUM_EXCLUSIONS_FILENAME).write_text(json.dumps(payload))


class FakeInfo:
    def __init__(self):
        self.context = {
            "logger": MagicMock(spec=logging.Logger),
            "partition_key": "test-partition",
        }


def _write_skill(skill_dir, frontmatter_extra="", body="Body.\n"):
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: my-skill\ndescription: Test.\n{frontmatter_extra}---\n\n{body}"
    )


def _write_matching_local_metadata(skill_dir, active):
    (skill_dir / ".hsk-skill.json").write_text(
        json.dumps(
            {
                "name": active["name"],
                "version": active["version"],
                "content_checksum": active["content_checksum"],
                "resolved_commit": active["resolved_commit"],
            }
        )
    )


def _active_row(**overrides):
    row = {
        "name": "my-skill",
        "version": "1.0",
        "description": "Test.",
        "git_repository_url": "https://example.com/repo.git",
        "git_ref": "main",
        "resolved_commit": "commit-a",
        "content_checksum": None,  # filled in per test after checksum is known
        "deployment_status": "deployed",
        "updated_at": None,
    }
    row.update(overrides)
    return row


class TestSkillGeneratedSectionsMerge:
    def _call_skill(self, tmp_path, skill_dir, active):
        with patch(
            "harness_engineering_engine.handlers.skill_reader.resolve_skill_root",
            return_value=tmp_path,
        ), patch(
            "harness_engineering_engine.handlers.skill_reader._get_active_skill",
            return_value=active,
        ):
            return skill_reader.skill(FakeInfo(), name="my-skill")

    def test_declared_allowed_commands_wins_even_when_sidecar_has_more(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(
            skill_dir,
            frontmatter_extra='allowed_commands:\n  - argv: ["python", "a.py"]\n',
        )
        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)
        _write_live_sidecar(
            skill_dir, "commit-a",
            allowed_commands=[{"argv": ["msv", "status"]}],
        )

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["allowed_commands"] == [{"argv": ["python", "a.py"]}]

    def test_sidecar_fills_absent_allowed_commands(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(skill_dir)  # no allowed_commands at all
        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)
        _write_live_sidecar(
            skill_dir, "commit-a",
            allowed_commands=[{"argv": ["msv", "status"]}],
        )

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["allowed_commands"] == [{"argv": ["msv", "status"]}]

    def test_explicit_empty_list_is_not_treated_as_absent(self, tmp_path):
        """allowed_commands: [] is a deliberate 'nothing allowed' — the
        sidecar must not fill it even though the parsed value is also []."""
        skill_dir = tmp_path / "my-skill"
        _write_skill(skill_dir, frontmatter_extra="allowed_commands: []\n")
        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)
        _write_live_sidecar(
            skill_dir, "commit-a",
            allowed_commands=[{"argv": ["msv", "status"]}],
        )

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["allowed_commands"] == []

    def test_stale_sidecar_from_different_commit_is_ignored(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(skill_dir)
        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum, resolved_commit="commit-b")
        _write_matching_local_metadata(skill_dir, active)
        # Sidecar was generated for a different (older) commit.
        _write_live_sidecar(
            skill_dir, "commit-a",
            allowed_commands=[{"argv": ["msv", "status"]}],
        )

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["allowed_commands"] == []

    def test_references_resolved_from_declared_reference_files(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(
            skill_dir,
            frontmatter_extra='reference_files:\n  - "notes.md"\n',
        )
        (skill_dir / "notes.md").write_text("Some reference content.")

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["references"] == [
            {"path": "notes.md", "content": "Some reference content."}
        ]

    def test_references_resolved_from_sidecar_when_reference_files_absent(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(skill_dir)  # no reference_files declared
        (skill_dir / "notes.md").write_text("Generated-pick content.")

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)
        _write_live_sidecar(
            skill_dir, "commit-a",
            reference_files=["notes.md"],
        )

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["references"] == [
            {"path": "notes.md", "content": "Generated-pick content."}
        ]

    def test_no_reference_files_anywhere_yields_empty_references(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        _write_skill(skill_dir)
        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["references"] == []

    def test_skill_md_is_never_returned_as_a_reference_even_if_declared(self, tmp_path):
        """Even if an author explicitly lists SKILL.md in reference_files
        (or a generated sidecar somehow names it), it must never come back
        in references — its content is already in body."""
        skill_dir = tmp_path / "my-skill"
        _write_skill(
            skill_dir,
            frontmatter_extra='reference_files:\n  - "SKILL.md"\n  - "notes.md"\n',
        )
        (skill_dir / "notes.md").write_text("Real reference content.")

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        checksum = compute_content_checksum(skill_dir, ".hsk-skill.json")
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["references"] == [
            {"path": "notes.md", "content": "Real reference content."}
        ]

    def test_pulled_reference_file_does_not_falsely_flag_stale_index(self, tmp_path):
        """A reference_files entry pulled in from elsewhere in the repo at
        deploy time (see reference_pull.pull_reference_files) is excluded
        from the *registered* checksum. skill()'s own read-time checksum
        recomputation must exclude that same path too — otherwise a
        correctly, freshly deployed skill would look permanently stale (or,
        with HSK_ALLOW_UNREGISTERED_CHANGES left false, make skill() raise
        on every single read)."""
        skill_dir = tmp_path / "my-skill"
        _write_skill(
            skill_dir,
            frontmatter_extra='reference_files:\n  - "config/shared.yaml"\n',
        )
        (skill_dir / "config").mkdir()
        (skill_dir / "config" / "shared.yaml").write_text("shared: true\n")

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        # What deploy_skill_package registers: the checksum computed with
        # the pulled-in file excluded.
        checksum = compute_content_checksum(
            skill_dir, ".hsk-skill.json", excluded_relpaths={"config/shared.yaml"}
        )
        active = _active_row(content_checksum=checksum)
        _write_matching_local_metadata(skill_dir, active)
        _write_live_checksum_exclusions(skill_dir, "commit-a", ["config/shared.yaml"])

        result = self._call_skill(tmp_path, skill_dir, active)

        assert result["stale_index"] is False
        assert result["local_content_checksum"] == checksum

    def test_checksum_exclusions_from_a_different_commit_are_ignored(self, tmp_path):
        """A checksum-exclusions sidecar left over from a previous, different
        commit must not be trusted — otherwise a genuinely stale directory
        could be masked as fresh."""
        from harness_engineering_engine.handlers.config import Config

        skill_dir = tmp_path / "my-skill"
        _write_skill(
            skill_dir,
            frontmatter_extra='reference_files:\n  - "config/shared.yaml"\n',
        )
        (skill_dir / "config").mkdir()
        (skill_dir / "config" / "shared.yaml").write_text("shared: true\n")

        from harness_engineering_engine.handlers.checksums import compute_content_checksum

        # Registered checksum assumes config/shared.yaml was excluded (as a
        # pulled-in file would be). If the exclusions sidecar is discarded
        # (wrong commit) the local recomputation includes that file, which
        # must genuinely mismatch — proving the guard isn't silently
        # papering over real content differences.
        checksum = compute_content_checksum(
            skill_dir, ".hsk-skill.json", excluded_relpaths={"config/shared.yaml"}
        )
        active = _active_row(content_checksum=checksum, resolved_commit="commit-b")
        _write_matching_local_metadata(skill_dir, active)
        # Exclusions sidecar was written for a different (older) commit.
        _write_live_checksum_exclusions(skill_dir, "commit-a", ["config/shared.yaml"])

        original = Config.ALLOW_UNREGISTERED_CHANGES
        Config.ALLOW_UNREGISTERED_CHANGES = True
        try:
            result = self._call_skill(tmp_path, skill_dir, active)
        finally:
            Config.ALLOW_UNREGISTERED_CHANGES = original

        assert result["stale_index"] is True
