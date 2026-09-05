# -*- coding: utf-8 -*-
"""PostgreSQL repository for Skill entity."""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.skill import SkillListType, SkillType
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ..base import EntityRepository
from ...postgresql.skill import SkillModel


class SkillPGRepository(EntityRepository):
    """PostgreSQL repository for Skill entity."""

    @property
    def entity_type(self) -> str:
        return "skill"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        skill_uuid = keys.get("skill_uuid")
        if not partition_key or not skill_uuid:
            return None
        session = Config.db_session
        row = (
            session.query(SkillModel)
            .filter(
                SkillModel.partition_key == partition_key,
                SkillModel.skill_uuid == skill_uuid,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        skill_uuid = keys.get("skill_uuid")
        if not partition_key or not skill_uuid:
            return 0
        session = Config.db_session
        return (
            session.query(SkillModel)
            .filter(
                SkillModel.partition_key == partition_key,
                SkillModel.skill_uuid == skill_uuid,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        """Return paginated skill list matching the GraphQL connection shape."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        name = filters.get("name")
        description = filters.get("description")
        enabled = filters.get("enabled")
        deployment_status = filters.get("deployment_status")
        is_active = filters.get("is_active")

        query = session.query(SkillModel)
        if partition_key:
            query = query.filter(SkillModel.partition_key == partition_key)
        if name:
            query = query.filter(SkillModel.name.ilike(f"%{name}%"))
        if description:
            query = query.filter(SkillModel.description.ilike(f"%{description}%"))
        if enabled is not None:
            query = query.filter(SkillModel.enabled == enabled)
        if deployment_status:
            query = query.filter(SkillModel.deployment_status == deployment_status)
        if is_active is not None:
            query = query.filter(SkillModel.is_active == is_active)

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(SkillModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        skill_list = [self.get_type(info, row) for row in rows]
        return SkillListType(skill_list=skill_list, total=total)

    def insert_update(self, info: ResolveInfo, **kwargs: Any) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = info.context.get("partition_key")
        skill_uuid = kwargs.get("skill_uuid")

        try:
            if skill_uuid:
                row = (
                    session.query(SkillModel)
                    .filter(
                        SkillModel.partition_key == partition_key,
                        SkillModel.skill_uuid == skill_uuid,
                    )
                    .first()
                )
                if not row:
                    row = self._create_row(info, **kwargs)
                    session.add(row)
                else:
                    field_map = [
                        "name",
                        "version",
                        "description",
                        "git_repository_url",
                        "git_ref",
                        "resolved_commit",
                        "content_checksum",
                        "local_path",
                        "deployment_status",
                        "enabled",
                        "is_active",
                        "registered_at",
                    ]
                    for field in field_map:
                        if field in kwargs:
                            val = kwargs[field]
                            setattr(row, field, None if val == "null" else val)
                    row.updated_by = kwargs.get("updated_by", "system")
                    row.updated_at = pendulum.now("UTC")
            else:
                row = self._create_row(info, **kwargs)
                session.add(row)

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

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> SkillModel:
        partition_key = info.context.get("partition_key")
        endpoint_id = info.context.get("endpoint_id")
        part_id = info.context.get("part_id")
        skill_uuid = kwargs.get("skill_uuid")

        cols = {
            "partition_key": partition_key,
            "endpoint_id": endpoint_id,
            "part_id": part_id,
            "updated_by": kwargs.get("updated_by", "system"),
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "name",
            "version",
            "description",
            "git_repository_url",
            "git_ref",
            "resolved_commit",
            "content_checksum",
            "local_path",
            "deployment_status",
            "enabled",
            "is_active",
            "registered_at",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        if skill_uuid:
            cols["skill_uuid"] = skill_uuid

        return SkillModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = info.context.get("partition_key")
        skill_uuid = kwargs.get("skill_uuid")

        try:
            row = (
                session.query(SkillModel)
                .filter(
                    SkillModel.partition_key == partition_key,
                    SkillModel.skill_uuid == skill_uuid,
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

    def get_type(self, info: ResolveInfo, row: Any) -> SkillType | None:
        data = normalize_row(row)
        if data is None:
            return None
        return SkillType(**normalize_to_json(data))

    def resolve_single(self, info: ResolveInfo, **kwargs: Any) -> Optional[SkillType]:
        """Resolve a single skill, supporting name lookup."""
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        if kwargs.get("name"):
            row = (
                session.query(SkillModel)
                .filter(
                    SkillModel.partition_key == partition_key,
                    SkillModel.name == kwargs["name"],
                )
                .first()
            )
            return self.get_type(info, row) if row else None

        if "skill_uuid" not in kwargs:
            return None

        count = self.count(partition_key=partition_key, skill_uuid=kwargs["skill_uuid"])
        if count == 0:
            return None

        row = (
            session.query(SkillModel)
            .filter(
                SkillModel.partition_key == partition_key,
                SkillModel.skill_uuid == kwargs["skill_uuid"],
            )
            .first()
        )
        return self.get_type(info, row) if row else None


__all__ = ["SkillPGRepository"]
