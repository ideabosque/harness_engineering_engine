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

from harness_engineering_engine.handlers.skill_deployment import deploy_skill_package


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
) -> Path:
    """Create a local git repo containing one skill, committed on 'main'.

    ``subpath`` nests the skill folder under additional parent directories
    (e.g. ``"src/skills"``) to exercise recursive ``SKILL.md`` discovery.
    ``cli_packages_yaml`` injects a raw ``cli_packages:`` frontmatter block.
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    _run(["git", "init", "-q"], cwd=remote)
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)

    skill_dir = (remote / subpath / name) if subpath else (remote / name)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\nallowed_commands: []\n"
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
