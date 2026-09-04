#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict

from graphene import ResolveInfo
from silvaengine_utility import method_cache

from ..handlers.config import Config
from ..models.repositories import get_repo
from ..types.cli_package import CliPackageListType, CliPackageType


def resolve_cli_package(info: ResolveInfo, **kwargs: Dict[str, Any]) -> CliPackageType | None:
    return get_repo("cli_package").resolve_single(info, **kwargs)


@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("queries", "cli_package"),
    cache_enabled=Config.is_cache_enabled,
)
def resolve_cli_package_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> CliPackageListType:
    return get_repo("cli_package").list(info, **kwargs)