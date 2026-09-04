#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Pytest configuration for harness_engineering_engine tests.

Autoloads ``tests/.env`` (when present) so tests that read HSK_* configuration
can run against locally-provided settings. Safe no-op when the file is absent.

On Windows, ``os.environ`` keys are case-insensitive for lookups but
``dict(os.environ)`` may collapse case variants, so a ``db_backend`` setting
entry can be lost when an uppercase ``DB_BACKEND`` also exists.  The helper
below builds the ``Config.initialize`` setting dict with both cases present
when only one was provided.
"""
from __future__ import print_function

__author__ = "bibow"

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv only in test extras
    load_dotenv = None


_ENV_PATH = Path(__file__).parent / ".env"

if load_dotenv is not None and _ENV_PATH.is_file():
    # Do not override variables already present in the real environment.
    load_dotenv(dotenv_path=_ENV_PATH, override=False)


def build_setting_from_env() -> dict:
    """Build the ``Config.initialize`` setting dict from the environment.

    Ensures both the env-style uppercase keys and the engine-style lowercase
    keys are present for database settings, so ``Config.initialize`` (which
    reads lowercase keys from the setting dict) works regardless of which
    case was supplied in ``tests/.env``.
    """
    setting = dict(os.environ)
    for lower, upper in (
        ("db_backend", "DB_BACKEND"),
        ("db_host", "DB_HOST"),
        ("db_port", "DB_PORT"),
        ("db_user", "DB_USER"),
        ("db_password", "DB_PASSWORD"),
        ("db_schema", "DB_SCHEMA"),
        ("region_name", "REGION_NAME"),
        ("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
        ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
    ):
        if lower not in setting and upper in setting:
            setting[lower] = setting[upper]
    return setting


__all__ = ["build_setting_from_env"]
