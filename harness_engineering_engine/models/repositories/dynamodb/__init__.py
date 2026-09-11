# -*- coding: utf-8 -*-
"""DynamoDB repositories — thin wrappers over PynamoDB model functions."""
from __future__ import print_function

__author__ = "bibow"

from typing import Dict

from ..base import EntityRepository


def register_all(registry: Dict[str, EntityRepository]) -> None:
    """Register all DynamoDB repositories into the given registry dict."""
    from .skill_repo import SkillRepository
    from .cli_package_repo import CliPackageRepository
    from .command_run_repo import CommandRunRepository

    repos = [
        SkillRepository(),
        CliPackageRepository(),
        CommandRunRepository(),
    ]
    for repo in repos:
        registry[repo.entity_type] = repo


__all__ = ["register_all"]
