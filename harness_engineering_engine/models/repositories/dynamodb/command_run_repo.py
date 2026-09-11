# -*- coding: utf-8 -*-
"""DynamoDB repository for CommandRun entity (async command run registry)."""

from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, List, Optional

from ...dynamodb import command_run as _cmd_run_mod
from ..base import EntityRepository


class CommandRunRepository(EntityRepository):
    """DynamoDB repository for the async command run registry."""

    @property
    def entity_type(self) -> str:
        return "command_run"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        run_uuid = keys.get("run_uuid")
        if not partition_key or not run_uuid:
            return None
        return _cmd_run_mod.get_command_run_dict(partition_key, run_uuid)

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        run_uuid = keys.get("run_uuid")
        if not partition_key or not run_uuid:
            return 0
        return 1 if _cmd_run_mod.get_command_run(partition_key, run_uuid) else 0

    def list(self, info: Any, **filters: Any) -> List[Dict[str, Any]]:
        """Return completed runs older than ``completed_before`` for cleanup."""
        partition_key = info.context.get("partition_key")
        completed_before = filters.get("completed_before")
        limit = filters.get("limit", 200)
        if not partition_key or completed_before is None:
            return []
        return _cmd_run_mod.list_stale_command_runs(
            partition_key, completed_before, limit=limit
        )

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        partition_key = info.context.get("partition_key")
        run_uuid = kwargs.pop("run_uuid")
        updated_by = kwargs.pop("updated_by", "system")
        return _cmd_run_mod.insert_update_command_run(
            partition_key, run_uuid, updated_by, **kwargs
        )

    def delete(self, info: Any, **kwargs: Any) -> bool:
        partition_key = kwargs.get("partition_key") or info.context.get("partition_key")
        run_uuid = kwargs.get("run_uuid")
        return _cmd_run_mod.delete_command_run(partition_key, run_uuid)


__all__ = ["CommandRunRepository"]
