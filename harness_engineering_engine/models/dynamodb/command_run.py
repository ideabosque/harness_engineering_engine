# -*- coding: utf-8 -*-
"""DynamoDB model for CommandRun entity.

Distributed-safe backing store for the async command run registry — a poll
landing on any gateway instance (not just the one that launched the run) can
read accurate status here instead of only through the process-local
in-memory registry in ``handlers/async_command_executor.py``.  See
``docs/DEVELOPMENT_PLAN.md`` §18 G-6.

Unlike ``cli_package``'s DynamoDB module, reads here deliberately do not use
``method_cache`` — status changes multiple times a second while a run is in
progress, and a cached read would make ``pollCommand`` appear stuck.
"""

from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, List, Optional

import pendulum
from pynamodb.attributes import (
    BooleanAttribute,
    NumberAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from silvaengine_dynamodb_base import BaseModel

from ...utils.normalization import normalize_to_json


class CompletedAtIndex(LocalSecondaryIndex):
    """Local secondary index for locating stale completed runs to prune.

    Only completed/timed-out runs carry ``completed_at`` — a still-running
    run is absent from this index by construction, so the cleanup sweep
    never has to filter those out itself.
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "completed_at-index"

    partition_key = UnicodeAttribute(hash_key=True)
    completed_at = UTCDateTimeAttribute(range_key=True)


class CommandRunModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "hsk-command-runs"

    partition_key = UnicodeAttribute(hash_key=True)
    run_uuid = UnicodeAttribute(range_key=True)
    endpoint_id = UnicodeAttribute(null=True)
    part_id = UnicodeAttribute(null=True)

    # Run identity
    skill_name = UnicodeAttribute()
    argv = UnicodeAttribute()  # JSON-encoded list[str]

    # Status
    status = UnicodeAttribute(default="running")  # running | completed | timed_out
    exit_code = UnicodeAttribute(null=True)
    stdout = UnicodeAttribute(null=True)
    stderr = UnicodeAttribute(null=True)
    timed_out = BooleanAttribute(default=False)
    truncated = BooleanAttribute(default=False)
    output_truncated_bytes = NumberAttribute(default=0)

    # Timing
    started_at = UTCDateTimeAttribute()
    completed_at = UTCDateTimeAttribute(null=True)

    # Bookkeeping
    created_at = UTCDateTimeAttribute()
    updated_by = UnicodeAttribute()
    updated_at = UTCDateTimeAttribute()

    completed_at_index = CompletedAtIndex()


def get_command_run(partition_key: str, run_uuid: str) -> Optional[CommandRunModel]:
    """Uncached read — status must always reflect the latest write."""
    try:
        return CommandRunModel.get(partition_key, run_uuid)
    except CommandRunModel.DoesNotExist:
        return None


def get_command_run_dict(partition_key: str, run_uuid: str) -> Optional[Dict[str, Any]]:
    row = get_command_run(partition_key, run_uuid)
    return _to_dict(row) if row else None


def _to_dict(row: CommandRunModel) -> Dict[str, Any]:
    data = row.__dict__["attribute_values"].copy()
    return normalize_to_json(data)


def insert_update_command_run(
    partition_key: str,
    run_uuid: str,
    updated_by: str,
    **fields: Any,
) -> Dict[str, Any]:
    """Create the row if absent, otherwise merge ``fields`` into it."""
    row = get_command_run(partition_key, run_uuid)
    now = pendulum.now("UTC")

    if row is None:
        cols = {
            "endpoint_id": fields.get("endpoint_id"),
            "part_id": fields.get("part_id"),
            "updated_by": updated_by,
            "created_at": now,
            "updated_at": now,
            "started_at": fields.get("started_at", now),
        }
        for key in (
            "skill_name",
            "argv",
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "timed_out",
            "truncated",
            "output_truncated_bytes",
            "completed_at",
        ):
            if key in fields:
                cols[key] = fields[key]
        row = CommandRunModel(partition_key, run_uuid, **cols)
        row.save()
        return _to_dict(row)

    actions = [
        CommandRunModel.updated_by.set(updated_by),
        CommandRunModel.updated_at.set(now),
    ]
    field_map = {
        "status": CommandRunModel.status,
        "exit_code": CommandRunModel.exit_code,
        "stdout": CommandRunModel.stdout,
        "stderr": CommandRunModel.stderr,
        "timed_out": CommandRunModel.timed_out,
        "truncated": CommandRunModel.truncated,
        "output_truncated_bytes": CommandRunModel.output_truncated_bytes,
        "completed_at": CommandRunModel.completed_at,
    }
    for key, field in field_map.items():
        if key in fields:
            actions.append(field.set(fields[key]))
    row.update(actions=actions)
    row.refresh()
    return _to_dict(row)


def delete_command_run(partition_key: str, run_uuid: str) -> bool:
    row = get_command_run(partition_key, run_uuid)
    if row is None:
        return True
    row.delete()
    return True


def list_stale_command_runs(
    partition_key: str, before: Any, limit: int = 200
) -> List[Dict[str, Any]]:
    """Return completed runs older than ``before`` (an aware datetime)."""
    results = CommandRunModel.completed_at_index.query(
        partition_key,
        CommandRunModel.completed_at < before,
        limit=limit,
    )
    return [_to_dict(row) for row in results]


__all__ = [
    "CommandRunModel",
    "get_command_run",
    "get_command_run_dict",
    "insert_update_command_run",
    "delete_command_run",
    "list_stale_command_runs",
]
