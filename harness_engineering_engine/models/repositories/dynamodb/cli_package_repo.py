# -*- coding: utf-8 -*-
"""DynamoDB repository for CLI package entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ...repositories.base import EntityRepository
from ._base import _normalize

from ...dynamodb import cli_package as _cli_pkg_mod


class CliPackageRepository(EntityRepository):
    """DynamoDB repository for CLI package entity."""

    @property
    def entity_type(self) -> str:
        return "cli_package"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        cli_package_uuid = keys.get("cli_package_uuid")
        if not partition_key or not cli_package_uuid:
            return None
        count = _cli_pkg_mod.get_cli_package_count(partition_key, cli_package_uuid)
        if count == 0:
            return None
        return _normalize(_cli_pkg_mod.get_cli_package(partition_key, cli_package_uuid))

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        cli_package_uuid = keys.get("cli_package_uuid")
        if not partition_key or not cli_package_uuid:
            return 0
        return _cli_pkg_mod.get_cli_package_count(partition_key, cli_package_uuid)

    def list(self, info: Any, **filters: Any) -> Any:
        return _cli_pkg_mod.resolve_cli_package_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _cli_pkg_mod.insert_update_cli_package(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _cli_pkg_mod.delete_cli_package(info, **kwargs)

    def get_type(self, info: Any, pkg: Any) -> Any:
        return _cli_pkg_mod.get_cli_package_type(info, pkg)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        return _cli_pkg_mod.resolve_cli_package(info, **kwargs)


__all__ = ["CliPackageRepository"]