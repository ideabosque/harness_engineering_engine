# -*- coding: utf-8 -*-
"""PostgreSQL repositories."""
from __future__ import print_function

__author__ = "bibow"

from typing import Dict

from ..base import EntityRepository


def register_all(registry: Dict[str, EntityRepository]) -> None:
    """Register all PostgreSQL repositories into the given registry dict."""
    from .skill_repo import SkillPGRepository
    from .cli_package_repo import CliPackagePGRepository
    from .command_run_repo import CommandRunPGRepository

    repos = [
        SkillPGRepository(),
        CliPackagePGRepository(),
        CommandRunPGRepository(),
    ]
    for repo in repos:
        registry[repo.entity_type] = repo


__all__ = ["register_all"]
