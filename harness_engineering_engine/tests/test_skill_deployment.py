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

                # Installed directly into the local skill root — no S3 in between.
                installed = skill_root / "rfq-assistant" / "SKILL.md"
                assert installed.is_file()
                assert (skill_root / "rfq-assistant" / ".hsk-skill.json").is_file()

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
                assert (skill_root / "deep-skill" / "SKILL.md").is_file()

    def test_declared_cli_package_is_auto_registered_and_installed(self, tmp_path):
        """A skill declaring cli_packages gets it registered and installed
        immediately at deploy time — not deferred to first runCommand."""
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
                    mock_ensure.return_value = {
                        "package_name": "my-cli-tool",
                        "version": "1.0.0",
                        "status": "ready",
                    }

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

                    ensure_args, _ = mock_ensure.call_args
                    assert ensure_args[1] == "my-cli-tool"

    def test_cli_package_install_failure_fails_that_skill(self, tmp_path):
        """A CLI package that fails to install fails this skill's deploy —
        the skill is reported in ``failed``, not registered as deployed
        with a dependency that doesn't actually work."""
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
                ), patch(
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

                    assert result["deployed"] == []
                    assert len(result["failed"]) == 1
                    assert "broken-tool" in result["failed"][0]["error"]

                    # The skill row is never registered when its declared
                    # CLI dependency isn't actually usable.
                    fake_repo.insert_update.assert_not_called()
                    assert not (skill_root / "rfq-assistant").exists()

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

        sidecar = skill_root / "rfq-assistant" / ".hsk-generated.json"
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
