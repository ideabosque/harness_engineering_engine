#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for skill deployment activation behavior.

Regression coverage for the fix to Known issue #2 in DEVELOPMENT_PLAN.md:
``deploy_skill_package`` must auto-activate a skill's first-ever version
(so it is immediately retrievable without a manual promote step), but must
leave subsequent versions inactive until an operator explicitly promotes
them.
"""
from __future__ import print_function

import logging
import zipfile
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


def _make_skill_zip(tmp_path: Path, name: str = "rfq-assistant") -> Path:
    skill_dir = tmp_path / "src" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\nallowed_commands: []\n---\n\n"
        "Body.\n"
    )

    zip_path = tmp_path / "package.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(skill_dir / "SKILL.md", f"{name}/SKILL.md")
    return zip_path


class TestDeploySkillPackageActivation:
    def _mock_config(self):
        return patch(
            "harness_engineering_engine.handlers.skill_deployment.Config"
        )

    def _mock_s3(self, mock_config):
        mock_config.SKILL_ARTIFACT_BUCKET = "test-bucket"
        mock_config.SKILL_ARTIFACT_PREFIX = "skills/"
        mock_config.SKILL_LOCAL_METADATA_FILE = ".hsk-skill.json"
        client = MagicMock()
        client.put_object.return_value = {"VersionId": "v1"}
        mock_config.aws_s3 = client
        return client

    def test_first_version_is_activated_automatically(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path)

        with self._mock_config() as mock_config:
            self._mock_s3(mock_config)
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                fake_repo.list.return_value = FakeSkillListResult([])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), source=str(zip_path), source_type="zip"
                )

                assert result["failed"] == []
                assert len(result["deployed"]) == 1
                assert result["deployed"][0]["is_active"] is True

                _, kwargs = fake_repo.insert_update.call_args
                assert kwargs["is_active"] is True
                assert kwargs["deployment_status"] == "deployed"

    def test_second_version_stays_inactive_until_promoted(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path)

        with self._mock_config() as mock_config:
            self._mock_s3(mock_config)
            with patch(
                "harness_engineering_engine.handlers.skill_deployment.get_repo"
            ) as mock_get_repo:
                fake_repo = MagicMock()
                existing_active = MagicMock(name="v1", is_active=True)
                fake_repo.list.return_value = FakeSkillListResult([existing_active])
                mock_get_repo.return_value = fake_repo

                result = deploy_skill_package(
                    FakeInfo(), source=str(zip_path), source_type="zip"
                )

                assert result["failed"] == []
                assert len(result["deployed"]) == 1
                assert result["deployed"][0]["is_active"] is False

                _, kwargs = fake_repo.insert_update.call_args
                assert kwargs["is_active"] is False
                assert kwargs["deployment_status"] == "uploaded"
