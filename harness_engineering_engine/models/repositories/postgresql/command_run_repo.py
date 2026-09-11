# -*- coding: utf-8 -*-
"""PostgreSQL repository for CommandRun entity (async command run registry).

There is no GraphQL CRUD surface for this entity — it exists purely so that
``handlers/async_command_executor.py`` can make a run's status visible from
any gateway instance, and so the scheduler can prune stale rows. See
``docs/DEVELOPMENT_PLAN.md`` §18 G-6.
"""

from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, List, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ...postgresql.base import normalize_row
from ...postgresql.command_run import CommandRunModel
from ..base import EntityRepository

_FIELDS = [
    "skill_name",
    "argv",
    "status",
    "exit_code",
    "stdout",
    "stderr",
    "timed_out",
    "truncated",
    "output_truncated_bytes",
    "started_at",
    "completed_at",
]


class CommandRunPGRepository(EntityRepository):
    """PostgreSQL repository for the async command run registry."""

    @property
    def entity_type(self) -> str:
        return "command_run"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        run_uuid = keys.get("run_uuid")
        if not partition_key or not run_uuid:
            return None
        session = Config.db_session
        row = (
            session.query(CommandRunModel)
            .filter(
                CommandRunModel.partition_key == partition_key,
                CommandRunModel.run_uuid == run_uuid,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        run_uuid = keys.get("run_uuid")
        if not partition_key or not run_uuid:
            return 0
        session = Config.db_session
        return (
            session.query(CommandRunModel)
            .filter(
                CommandRunModel.partition_key == partition_key,
                CommandRunModel.run_uuid == run_uuid,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> List[Dict[str, Any]]:
        """Return completed runs older than ``completed_before`` for cleanup.

        Internal-only — there is no GraphQL list query for command runs.
        """
        session = Config.db_session
        partition_key = info.context.get("partition_key")
        completed_before = filters.get("completed_before")
        limit = filters.get("limit", 200)

        query = session.query(CommandRunModel)
        if partition_key:
            query = query.filter(CommandRunModel.partition_key == partition_key)
        if completed_before is not None:
            query = query.filter(
                CommandRunModel.completed_at.isnot(None),
                CommandRunModel.completed_at < completed_before,
            )
        rows = query.limit(limit).all()
        return [normalize_row(row) for row in rows]

    def insert_update(self, info: ResolveInfo, **kwargs: Any) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = info.context.get("partition_key")
        run_uuid = kwargs.get("run_uuid")

        try:
            row = None
            if run_uuid:
                row = (
                    session.query(CommandRunModel)
                    .filter(
                        CommandRunModel.partition_key == partition_key,
                        CommandRunModel.run_uuid == run_uuid,
                    )
                    .first()
                )
            if row is None:
                row = self._create_row(info, **kwargs)
                session.add(row)
            else:
                for field in _FIELDS:
                    if field in kwargs:
                        setattr(row, field, kwargs[field])
                row.updated_by = kwargs.get("updated_by", "system")
                row.updated_at = pendulum.now("UTC")

            session.commit()
            session.refresh(row)
            return normalize_row(row)
        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> CommandRunModel:
        partition_key = info.context.get("partition_key")
        endpoint_id = info.context.get("endpoint_id")
        part_id = info.context.get("part_id")

        cols = {
            "partition_key": partition_key,
            "endpoint_id": endpoint_id,
            "part_id": part_id,
            "updated_by": kwargs.get("updated_by", "system"),
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
            "started_at": kwargs.get("started_at", pendulum.now("UTC")),
        }
        for field in _FIELDS:
            if field in kwargs:
                cols[field] = kwargs[field]
        if kwargs.get("run_uuid"):
            cols["run_uuid"] = kwargs["run_uuid"]

        return CommandRunModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = kwargs.get("partition_key") or info.context.get("partition_key")
        run_uuid = kwargs.get("run_uuid")

        try:
            row = (
                session.query(CommandRunModel)
                .filter(
                    CommandRunModel.partition_key == partition_key,
                    CommandRunModel.run_uuid == run_uuid,
                )
                .first()
            )
            if not row:
                return True
            session.delete(row)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            if logger:
                logger.error(traceback.format_exc())
            raise e
        finally:
            Config.db_session.remove()


__all__ = ["CommandRunPGRepository"]
