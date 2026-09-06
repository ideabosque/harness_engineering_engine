#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for the P9 generated-sections sidecar in the local version cache.

The sidecar is a dotfile, so it would normally be dropped by the same
``_ignore_hidden`` filter that correctly excludes ``.git`` from
git-sourced content — ``install_from_cache`` must carry it across the
swap explicitly, the same way ``write_local_metadata`` is a separate
write rather than part of that copy.
"""
from __future__ import print_function

from harness_engineering_engine.handlers import skill_version_cache
from harness_engineering_engine.handlers.checksums import (
    CHECKSUM_EXCLUSIONS_FILENAME,
    GENERATED_SIDECAR_FILENAME,
)


class TestGeneratedSidecar:
    def test_write_and_read_roundtrip(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        cache_dir = skill_version_cache.version_cache_dir(skill_root, "my-skill", "1.0")
        cache_dir.mkdir(parents=True)

        skill_version_cache.write_generated_sidecar(
            skill_root,
            "my-skill",
            "1.0",
            "abc123",
            {"allowed_commands": [{"argv": ["msv", "status"]}]},
        )

        loaded = skill_version_cache.read_generated_sidecar(cache_dir)
        assert loaded["resolved_commit"] == "abc123"
        assert loaded["allowed_commands"] == [{"argv": ["msv", "status"]}]
        assert loaded["name"] == "my-skill"
        assert loaded["version"] == "1.0"

    def test_empty_generated_dict_writes_nothing(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        cache_dir = skill_version_cache.version_cache_dir(skill_root, "my-skill", "1.0")
        cache_dir.mkdir(parents=True)

        skill_version_cache.write_generated_sidecar(
            skill_root, "my-skill", "1.0", "abc123", {}
        )

        assert not (cache_dir / GENERATED_SIDECAR_FILENAME).exists()

    def test_read_missing_sidecar_returns_none(self, tmp_path):
        assert skill_version_cache.read_generated_sidecar(tmp_path) is None

    def test_read_corrupted_sidecar_returns_none(self, tmp_path):
        (tmp_path / GENERATED_SIDECAR_FILENAME).write_text("not valid json {{{")
        assert skill_version_cache.read_generated_sidecar(tmp_path) is None

    def test_install_from_cache_carries_the_sidecar_across(self, tmp_path):
        """The main content copy uses _ignore_hidden (correctly dropping
        .git-like artifacts) — the sidecar must survive that anyway."""
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()

        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n\nBody.\n")

        skill_version_cache.store_version(skill_root, "my-skill", "1.0", content_dir)
        skill_version_cache.write_generated_sidecar(
            skill_root, "my-skill", "1.0", "abc123", {"reference_files": ["x.md"]}
        )

        installed = skill_version_cache.install_from_cache(skill_root, "my-skill", "1.0")

        assert installed is True
        live_sidecar = skill_root / "my-skill" / GENERATED_SIDECAR_FILENAME
        assert live_sidecar.is_file()
        loaded = skill_version_cache.read_generated_sidecar(skill_root / "my-skill")
        assert loaded["reference_files"] == ["x.md"]
        # The real content still made it across too.
        assert (skill_root / "my-skill" / "SKILL.md").is_file()

    def test_install_from_cache_without_a_sidecar_is_unaffected(self, tmp_path):
        """A version that never had generation run (e.g. SKILL.md already
        declared both fields) installs cleanly with no sidecar at all."""
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()

        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n\nBody.\n")

        skill_version_cache.store_version(skill_root, "my-skill", "1.0", content_dir)
        installed = skill_version_cache.install_from_cache(skill_root, "my-skill", "1.0")

        assert installed is True
        assert not (skill_root / "my-skill" / GENERATED_SIDECAR_FILENAME).exists()


class TestChecksumExclusions:
    """Bookkeeping for which reference_files paths were pulled in from
    elsewhere in the repo, and so must be excluded from this skill's
    content checksum — kept in its own sidecar, separate from the
    human/agent-facing generated-sections one."""

    def test_write_and_read_roundtrip(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        cache_dir = skill_version_cache.version_cache_dir(skill_root, "my-skill", "1.0")
        cache_dir.mkdir(parents=True)

        skill_version_cache.write_checksum_exclusions(
            skill_root, "my-skill", "1.0", "abc123", ["config/shared.yaml"]
        )

        loaded = skill_version_cache.read_checksum_exclusions(cache_dir)
        assert loaded["resolved_commit"] == "abc123"
        assert loaded["excluded_relpaths"] == ["config/shared.yaml"]

    def test_empty_excluded_relpaths_writes_nothing(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        cache_dir = skill_version_cache.version_cache_dir(skill_root, "my-skill", "1.0")
        cache_dir.mkdir(parents=True)

        skill_version_cache.write_checksum_exclusions(
            skill_root, "my-skill", "1.0", "abc123", []
        )

        assert not (cache_dir / CHECKSUM_EXCLUSIONS_FILENAME).exists()

    def test_read_missing_returns_none(self, tmp_path):
        assert skill_version_cache.read_checksum_exclusions(tmp_path) is None

    def test_read_corrupted_returns_none(self, tmp_path):
        (tmp_path / CHECKSUM_EXCLUSIONS_FILENAME).write_text("not valid json {{{")
        assert skill_version_cache.read_checksum_exclusions(tmp_path) is None

    def test_install_from_cache_carries_it_across(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()

        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n\nBody.\n")

        skill_version_cache.store_version(skill_root, "my-skill", "1.0", content_dir)
        skill_version_cache.write_checksum_exclusions(
            skill_root, "my-skill", "1.0", "abc123", ["config/shared.yaml"]
        )

        installed = skill_version_cache.install_from_cache(skill_root, "my-skill", "1.0")

        assert installed is True
        loaded = skill_version_cache.read_checksum_exclusions(skill_root / "my-skill")
        assert loaded["excluded_relpaths"] == ["config/shared.yaml"]

    def test_install_from_cache_without_exclusions_is_unaffected(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()

        content_dir = tmp_path / "content"
        content_dir.mkdir()
        (content_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n\nBody.\n")

        skill_version_cache.store_version(skill_root, "my-skill", "1.0", content_dir)
        installed = skill_version_cache.install_from_cache(skill_root, "my-skill", "1.0")

        assert installed is True
        assert not (skill_root / "my-skill" / CHECKSUM_EXCLUSIONS_FILENAME).exists()
