#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for git-based skill deployment.

Regression coverage for the S3-elimination refactor: ``deploy_skill_package``
must clone straight from a git remote (no artifact store in between), install
the active version directly into the local skill root, and use git alone —
via a cheap ``git ls-remote`` — to decide whether a redeploy is a no-op.

Uses real local git repositories as the "remote" (git clone works fine on
local paths, so this exercises the actual git plumbing without network
access).
"""
from __future__ import print_function

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_engineering_engine.handlers import skill_version_cache
from harness_engineering_engine.handlers.skill_deployment import (
    _list_available_files,
    deploy_skill_package,
)


class FakeInfo:
    def __init__(self):
        self.context = {
            "logger": MagicMock(spec=logging.Logger),
            "partition_key": "test-partition",
        }


class FakeSkillListResult:
    def __init__(self, skill_list):
        self.skill_list = skill_list


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_git_skill_repo(
    tmp_path: Path,
    name: str = "rfq-assistant",
    subpath: str = "",
    cli_packages_yaml: str = "",
    allowed_commands_yaml: str = "allowed_commands: []\n",
) -> Path:
    """Create a local git repo containing one skill, committed on 'main'.

    ``subpath`` nests the skill folder under additional parent directories
    (e.g. ``"src/skills"``) to exercise recursive ``SKILL.md`` discovery.
    ``cli_packages_yaml`` injects a raw ``cli_packages:`` frontmatter block.
    ``allowed_commands_yaml`` defaults to an explicit empty list — pass
    ``""`` to omit the field entirely (P9's "genuinely absent" case).
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    _run(["git", "init", "-q"], cwd=remote)
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

    skill_dir = (remote / subpath / name) if subpath else (remote / name)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\n{allowed_commands_yaml}"
        f"{cli_packages_yaml}---\n\nBody.\n"
    )

    _run(["git", "add", "-A"], cwd=remote)
    _run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=remote,
    )
    return remote


class TestDeploySkillPackageActivation:
    def _patch_skill_root(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        return patch(
            "harness_engineering_engine.handlers.skill_deployment.resolve_skill_root",
            return_value=skill_root,
        ), skill_root

    def test_first_version_is_activated_automatically(self, tmp_path):
        remote = _make_git_skill_repo(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )

                assert result["failed"] == []
                assert len(result["deployed"]) == 1
                assert result["deployed"][0]["is_active"] is True
                assert result["deployed"][0]["resolved_commit"]

                _, kwargs = fake_repo.insert_update.call_args
                assert kwargs["is_active"] is True
                assert kwargs["deployment_status"] == "deployed"
                assert kwargs["resolved_commit"]

                # Cached locally for instant promote/rollback and for this
                # instance's own first skill()/runCommand call — but never
                # written into the live skill directory by deploy itself;
                # install is lazy (see skill_reader.py::skill()).
                assert not (skill_root / "rfq-assistant").exists()
                version = result["deployed"][0]["version"]
                cached = skill_version_cache.version_cache_dir(
                    skill_root, "rfq-assistant", version
                )
                assert (cached / "SKILL.md").is_file()

    def test_second_version_stays_inactive_until_promoted(self, tmp_path):
        remote = _make_git_skill_repo(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                existing_active = MagicMock(name="v1", is_active=True)
                fake_repo.list.return_value = FakeSkillListResult([existing_active])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )

                assert result["failed"] == []
                assert len(result["deployed"]) == 1
                assert result["deployed"][0]["is_active"] is False

                _, kwargs = fake_repo.insert_update.call_args
                assert kwargs["is_active"] is False
                assert kwargs["deployment_status"] == "registered"

                # Inactive version is registered only — not installed locally.
                assert not (skill_root / "rfq-assistant").exists()

    def test_redeploy_at_same_commit_is_skipped(self, tmp_path):
        """Version check uses git alone: an unchanged commit SHA is a no-op."""
        remote = _make_git_skill_repo(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                # First deploy registers the current commit.
                first = deploy_skill_package(
                    FakeInfo(),
                    git_repository_url=str(remote),
                    git_ref="main",
                    skill_name="rfq-assistant",
                )
                assert len(first["deployed"]) == 1
                resolved_commit = first["deployed"][0]["resolved_commit"]

                registered_row = MagicMock()
                registered_row.name = "rfq-assistant"
                registered_row.git_repository_url = str(remote)
                registered_row.git_ref = "main"
                registered_row.resolved_commit = resolved_commit
                fake_repo.list.return_value = FakeSkillListResult([registered_row])

                # Second deploy against the same unchanged commit is skipped —
                # no clone, no new row.
                second = deploy_skill_package(
                    FakeInfo(),
                    git_repository_url=str(remote),
                    git_ref="main",
                    skill_name="rfq-assistant",
                )
                assert second["deployed"] == []
                assert second["skipped"] == ["rfq-assistant"]


class TestDeploySkillPackageDiscoveryAndCliPackages:
    """Coverage for P8: recursive SKILL.md discovery and CLI package
    auto-registration/install (see docs/DEVELOPMENT_PLAN.md Known gap #6)."""

    def _patch_skill_root(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        return patch(
            "harness_engineering_engine.handlers.skill_deployment.resolve_skill_root",
            return_value=skill_root,
        ), skill_root

    def test_nested_skill_md_is_discovered(self, tmp_path):
        """A SKILL.md nested several directories deep is still found."""
        remote = _make_git_skill_repo(
            tmp_path, name="deep-skill", subpath="src/skills/nested"
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )

                assert result["failed"] == []
                assert len(result["deployed"]) == 1
                assert result["deployed"][0]["name"] == "deep-skill"

                # Cached locally (lazy install — see skill_reader.py), not
                # written into the live skill directory by deploy itself.
                assert not (skill_root / "deep-skill").exists()
                version = result["deployed"][0]["version"]
                cached = skill_version_cache.version_cache_dir(
                    skill_root, "deep-skill", version
                )
                assert (cached / "SKILL.md").is_file()

    def test_declared_cli_package_is_registered_but_not_installed(self, tmp_path):
        """A skill declaring cli_packages gets it registered at deploy
        time, but installation is lazy — deferred to first runCommand
        (see cli_package_manager.ensure_package), not done here."""
        cli_yaml = (
            "cli_packages:\n"
            "  - package_name: my-cli-tool\n"
            "    git_repository_url: https://github.com/example/my-cli-tool.git\n"
            '    version: "1.0.0"\n'
        )
        remote = _make_git_skill_repo(tmp_path, cli_packages_yaml=cli_yaml)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.cli_package_manager.register_cli_package"
                ) as mock_register, patch(
                    "harness_engineering_engine.handlers.cli_package_manager.ensure_package"
                ) as mock_ensure:
                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

                    assert result["failed"] == []
                    assert len(result["deployed"]) == 1

                    _, reg_kwargs = mock_register.call_args
                    assert reg_kwargs["package_name"] == "my-cli-tool"
                    assert (
                        reg_kwargs["git_repository_url"]
                        == "https://github.com/example/my-cli-tool.git"
                    )
                    assert reg_kwargs["version"] == "1.0.0"

                    mock_ensure.assert_not_called()

    def test_broken_cli_package_does_not_block_deploy(self, tmp_path):
        """A CLI package that would fail to install no longer fails this
        skill's deploy — installation is deferred to first runCommand, so
        deploy only ever registers it; a broken dependency surfaces as a
        runCommand error on first use instead (see cli_package_manager)."""
        cli_yaml = (
            "cli_packages:\n"
            "  - package_name: broken-tool\n"
            "    git_repository_url: https://github.com/example/broken-tool.git\n"
            '    version: "2.0.0"\n'
        )
        remote = _make_git_skill_repo(tmp_path, cli_packages_yaml=cli_yaml)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.cli_package_manager.register_cli_package"
                ) as mock_register, patch(
                    "harness_engineering_engine.handlers.cli_package_manager.ensure_package"
                ) as mock_ensure:
                    mock_ensure.return_value = {
                        "package_name": "broken-tool",
                        "status": "error",
                        "error": "pip install failed",
                    }

                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

                    assert result["failed"] == []
                    assert len(result["deployed"]) == 1

                    # Registered, but ensure_package (the actual install) is
                    # never called at deploy time — its "error" return value
                    # above is irrelevant here precisely because deploy
                    # never reaches it.
                    mock_register.assert_called_once()
                    mock_ensure.assert_not_called()

                    # Content is still cached locally, ready for this
                    # instance's own first skill()/runCommand call.
                    version = result["deployed"][0]["version"]
                    cached = skill_version_cache.version_cache_dir(
                        skill_root, "rfq-assistant", version
                    )
                    assert (cached / "SKILL.md").is_file()

    def test_no_cli_packages_declared_is_unaffected(self, tmp_path):
        """A skill with no cli_packages entries never touches the CLI
        package manager at all (regression guard for the common case)."""
        remote = _make_git_skill_repo(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.cli_package_manager.register_cli_package"
                ) as mock_register, patch(
                    "harness_engineering_engine.handlers.cli_package_manager.ensure_package"
                ) as mock_ensure:
                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

                    assert result["failed"] == []
                    assert len(result["deployed"]) == 1
                    mock_register.assert_not_called()
                    mock_ensure.assert_not_called()


class TestDeploySkillPackageSectionGeneration:
    """P9: allowed_commands/reference_files generation wired into deploy.

    ``generate_missing_sections`` itself is mocked in every test here — its
    own logic (skip on no API key, filter to requested fields, etc.) is
    covered separately in test_section_generator.py. What's under test is
    skill_deployment.py's wiring: which fields it asks for, and that the
    result lands in the sidecar (never in SKILL.md, never affecting the
    registered content_checksum).
    """

    def _patch_skill_root(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        return patch(
            "harness_engineering_engine.handlers.skill_deployment.resolve_skill_root",
            return_value=skill_root,
        ), skill_root

    def test_generation_requested_when_both_fields_absent(self, tmp_path):
        remote = _make_git_skill_repo(
            tmp_path,
            allowed_commands_yaml="",
            cli_packages_yaml=(
                "cli_packages:\n  - package_name: some-pkg\n"
            ),
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "_ensure_cli_packages"
                ), patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections"
                ) as mock_generate:
                    mock_generate.return_value = {
                        "allowed_commands": [{"argv": ["python", "helper.py"]}],
                        "reference_files": [],
                    }

                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        assert result["failed"] == []
        mock_generate.assert_called_once()
        missing_fields_arg = mock_generate.call_args[0][-1]
        assert missing_fields_arg == {"allowed_commands", "reference_files"}

        # Written into the version cache — deploy never touches the live
        # skill directory (see skill_reader.py for the lazy install path).
        version = result["deployed"][0]["version"]
        cached = skill_version_cache.version_cache_dir(
            skill_root, "rfq-assistant", version
        )
        sidecar = cached / ".hsk-generated.json"
        assert sidecar.is_file()
        import json as _json

        data = _json.loads(sidecar.read_text())
        assert data["allowed_commands"] == [{"argv": ["python", "helper.py"]}]

    def test_cli_packages_included_in_missing_fields_when_absent_too(self, tmp_path):
        """A skill declaring none of the three P9 fields at all — cli_packages
        must be requested alongside the other two, not just assumed absent
        because _ensure_cli_packages saw an empty declared list."""
        remote = _make_git_skill_repo(
            tmp_path, allowed_commands_yaml="", cli_packages_yaml=""
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "_ensure_cli_packages"
                ), patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections",
                    return_value={},
                ) as mock_generate:
                    deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        mock_generate.assert_called_once()
        missing_fields_arg = mock_generate.call_args[0][-1]
        assert missing_fields_arg == {"allowed_commands", "cli_packages", "reference_files"}

    def test_generation_not_requested_when_both_fields_declared(self, tmp_path):
        remote = _make_git_skill_repo(
            tmp_path,
            allowed_commands_yaml='allowed_commands:\n  - argv: ["python", "a.py"]\n',
            cli_packages_yaml=(
                'reference_files:\n  - "notes.md"\ncli_packages:\n'
                "  - package_name: some-pkg\n"
            ),
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "_ensure_cli_packages"
                ), patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections"
                ) as mock_generate:
                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        assert result["failed"] == []
        mock_generate.assert_not_called()
        sidecar = skill_root / "rfq-assistant" / ".hsk-generated.json"
        assert not sidecar.exists()

    def test_generation_requested_for_only_the_absent_field(self, tmp_path):
        remote = _make_git_skill_repo(
            tmp_path,
            allowed_commands_yaml='allowed_commands:\n  - argv: ["python", "a.py"]\n',
            cli_packages_yaml="cli_packages:\n  - package_name: some-pkg\n",
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "_ensure_cli_packages"
                ), patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections",
                    return_value={},
                ) as mock_generate:
                    deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        mock_generate.assert_called_once()
        missing_fields_arg = mock_generate.call_args[0][-1]
        assert missing_fields_arg == {"reference_files"}

    def test_no_sidecar_written_when_generation_yields_nothing(self, tmp_path):
        remote = _make_git_skill_repo(
            tmp_path,
            allowed_commands_yaml="",
            cli_packages_yaml="cli_packages:\n  - package_name: some-pkg\n",
        )
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "_ensure_cli_packages"
                ), patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections",
                    return_value={},
                ):
                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        assert result["failed"] == []
        sidecar = skill_root / "rfq-assistant" / ".hsk-generated.json"
        assert not sidecar.exists()


class TestDeploySkillPackageReferenceFilePull:
    """P9 extension: reference_files may name a path that lives elsewhere
    in the same repo clone (e.g. shared config/docs at a monorepo's root,
    or a CLI dependency's own source) rather than inside the skill's own
    folder — deploy must pull it in so the read path, which only ever
    looks inside the installed skill directory, finds it."""

    def _patch_skill_root(self, tmp_path):
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir()
        return patch(
            "harness_engineering_engine.handlers.skill_deployment.resolve_skill_root",
            return_value=skill_root,
        ), skill_root

    def _make_repo_with_root_level_config(self, tmp_path, name="rfq-assistant"):
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
        return remote

    def test_declared_reference_file_at_repo_root_is_pulled_into_skill_dir(self, tmp_path):
        remote = self._make_repo_with_root_level_config(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )

        assert result["failed"] == []
        version = result["deployed"][0]["version"]
        cached = skill_version_cache.version_cache_dir(
            skill_root, "rfq-assistant", version
        )
        pulled = cached / "config" / "shared.yaml"
        assert pulled.is_file()
        assert pulled.read_text() == "shared: true\n"

    def test_pulled_reference_files_recorded_in_checksum_exclusions_sidecar(self, tmp_path):
        """skill()'s own read-time checksum recomputation needs to know
        which reference_files entries were pulled in from elsewhere (so it
        can exclude them too, matching how the registered checksum was
        computed) — this must be recorded even when reference_files is
        fully declared and no other field needed generation at all. Kept
        in its own sidecar, separate from the human/agent-facing
        .hsk-generated.json — this is pure internal bookkeeping."""
        remote = self._make_repo_with_root_level_config(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )

        assert result["failed"] == []

        import json as _json

        version = result["deployed"][0]["version"]
        cached = skill_version_cache.version_cache_dir(
            skill_root, "rfq-assistant", version
        )

        generated_sidecar = cached / ".hsk-generated.json"
        if generated_sidecar.exists():
            assert "pulled_reference_files" not in _json.loads(generated_sidecar.read_text())

        exclusions_sidecar = cached / ".hsk-checksum-exclusions.json"
        exclusions_data = _json.loads(exclusions_sidecar.read_text())
        assert exclusions_data["excluded_relpaths"] == ["config/shared.yaml"]

    def test_generation_candidates_widened_with_repo_wide_files(self, tmp_path):
        """When reference_files is absent, generation must be offered
        candidates from the whole repo clone, not just the skill's own
        directory — otherwise a monorepo's shared root-level config/docs
        can never be proposed."""
        remote = tmp_path / "remote"
        remote.mkdir()
        _run(["git", "init", "-q"], cwd=remote)
        _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

        (remote / "config").mkdir()
        (remote / "config" / "shared.yaml").write_text("shared: true\n")

        skill_dir = remote / "rfq-assistant"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: rfq-assistant\ndescription: Test skill.\n"
            "allowed_commands: []\ncli_packages: []\n"
            "---\n\nSee `config/shared.yaml`.\n"
        )

        _run(["git", "add", "-A"], cwd=remote)
        _run(
            [
                "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-q", "-m", "init",
            ],
            cwd=remote,
        )

        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                with patch(
                    "harness_engineering_engine.handlers.skill_deployment."
                    "generate_missing_sections"
                ) as mock_generate:
                    mock_generate.return_value = {
                        "reference_files": ["config/shared.yaml"],
                    }

                    result = deploy_skill_package(
                        FakeInfo(), git_repository_url=str(remote), git_ref="main"
                    )

        assert result["failed"] == []
        candidate_files_arg = mock_generate.call_args[0][-2]
        assert "config/shared.yaml" in candidate_files_arg

        version = result["deployed"][0]["version"]
        cached = skill_version_cache.version_cache_dir(
            skill_root, "rfq-assistant", version
        )
        pulled = cached / "config" / "shared.yaml"
        assert pulled.is_file()

        import json as _json

        sidecar = cached / ".hsk-generated.json"
        data = _json.loads(sidecar.read_text())
        assert data["reference_files"] == ["config/shared.yaml"]

    def test_pulled_file_excluded_from_registered_checksum(self, tmp_path):
        """Deploying the same skill twice, with only the pulled-in
        repo-root file's content changed between commits, must not change
        the registered content_checksum for that skill — the file is a
        copy of shared material, not this skill's own authored content."""
        remote = self._make_repo_with_root_level_config(tmp_path)
        root_patch, skill_root = self._patch_skill_root(tmp_path)

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), git_repository_url=str(remote), git_ref="main"
                )
        checksum_1 = result["deployed"][0]["content_checksum"]

        (remote / "config" / "shared.yaml").write_text("shared: false\n")
        _run(["git", "add", "-A"], cwd=remote)
        _run(
            [
                "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-q", "-m", "change shared config only",
            ],
            cwd=remote,
        )

        stale_row = MagicMock()
        stale_row.name = "rfq-assistant"
        stale_row.git_repository_url = str(remote)
        stale_row.git_ref = "main"
        stale_row.resolved_commit = "stale-sha"

        with root_patch:
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([stale_row])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(),
                    git_repository_url=str(remote),
                    git_ref="main",
                    skill_name="rfq-assistant",
                )
        checksum_2 = result["deployed"][0]["content_checksum"]

        assert checksum_1 == checksum_2


class TestListAvailableFiles:
    """SKILL.md must never be a reference_files candidate — its content is
    already returned via body, so including it again would just be
    duplication."""

    def test_skill_md_is_excluded(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n\nBody.\n")
        (skill_dir / "notes.md").write_text("Real reference content.")

        files = _list_available_files(skill_dir)

        assert "SKILL.md" not in files
        assert "notes.md" in files

    def test_docs_folder_is_excluded(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n\nBody.\n")
        (skill_dir / "docs").mkdir()
        (skill_dir / "docs" / "plan.md").write_text("Should never be a candidate.")
        (skill_dir / "notes.md").write_text("Real reference content.")

        files = _list_available_files(skill_dir)

        assert "docs/plan.md" not in files
        assert "notes.md" in files

    def test_readme_is_excluded(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n\nBody.\n")
        (skill_dir / "README.md").write_text("Should never be a candidate.")
        (skill_dir / "notes.md").write_text("Real reference content.")

        files = _list_available_files(skill_dir)

        assert "README.md" not in files
        assert "notes.md" in files

    def test_tests_folder_is_excluded(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n\nBody.\n")
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "test_thing.py").write_text("Should never be a candidate.")
        (skill_dir / "notes.md").write_text("Real reference content.")

        files = _list_available_files(skill_dir)

        assert "tests/test_thing.py" not in files
        assert "notes.md" in files
