#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Tests for configuration loader."""
from __future__ import print_function

import logging
from unittest.mock import MagicMock, patch

from harness_engineering_engine.handlers.config import Config


class TestConfig:
    def _init_config(self, env=None, setting=None):
        logger = MagicMock(spec=logging.Logger)
        env = env or {}
        setting = setting or {"db_backend": "dynamodb"}
        with patch.dict("os.environ", env, clear=True), \
             patch("boto3.client", return_value=MagicMock()), \
             patch("boto3.resource", return_value=MagicMock()):
            Config._initialized = False
            Config.SKILL_ROOT = ""
            Config.RUN_COMMAND_ENABLED = True
            Config.DRY_RUN = False
            Config.initialize(logger, setting)
        return Config

    def test_skill_root_not_set(self):
        cfg = self._init_config()
        assert cfg.SKILL_ROOT == ""

    def test_skill_root_from_env(self):
        cfg = self._init_config(env={"HSK_SKILL_ROOT": "/tmp/skills"})
        assert cfg.SKILL_ROOT == "/tmp/skills"

    def test_skill_root_from_setting(self):
        cfg = self._init_config(setting={"db_backend": "dynamodb", "hsk_skill_root": "/app/skills"})
        assert cfg.SKILL_ROOT == "/app/skills"

    def test_defaults(self):
        cfg = self._init_config()
        assert cfg.RUN_COMMAND_ENABLED is False
        assert cfg.DRY_RUN is False
        assert cfg.GIT_SSH_KEY_PATH == ""
        assert cfg.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS == 30
        assert cfg.RUN_COMMAND_OUTPUT_LIMIT_BYTES == 20_000
        assert cfg.SKILL_LOCAL_METADATA_FILE == ".hsk-skill.json"

    def test_overrides(self):
        # Boolean coercion: env vars are strings, so non-empty strings are
        # truthy. For v1, the engine settings dict is used to set booleans.
        cfg = self._init_config(env={
            "HSK_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS": "60",
            "HSK_RUN_COMMAND_OUTPUT_LIMIT_BYTES": "50000",
        })
        assert cfg.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS == 60
        assert cfg.RUN_COMMAND_OUTPUT_LIMIT_BYTES == 50_000
