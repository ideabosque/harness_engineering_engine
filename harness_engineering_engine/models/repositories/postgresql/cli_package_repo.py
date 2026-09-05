# -*- coding: utf-8 -*-
"""PostgreSQL repository for CLI package entity."""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

import pendulum
from graphene import ResolveInfo

from ....handlers.config import Config
from ....types.cli_package import CliPackageListType, CliPackageType
from ....utils.normalization import normalize_to_json
from ...postgresql.base import normalize_row
from ..base import EntityRepository
from ...postgresql.cli_package import CliPackageModel


class CliPackagePGRepository(EntityRepository):
    """PostgreSQL repository for CLI package entity."""

    @property
    def entity_type(self) -> str:
        return "cli_package"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        cli_package_uuid = keys.get("cli_package_uuid")
        if not partition_key or not cli_package_uuid:
            return None
        session = Config.db_session
        row = (
            session.query(CliPackageModel)
            .filter(
                CliPackageModel.partition_key == partition_key,
                CliPackageModel.cli_package_uuid == cli_package_uuid,
            )
            .first()
        )
        return normalize_row(row) if row else None

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        cli_package_uuid = keys.get("cli_package_uuid")
        if not partition_key or not cli_package_uuid:
            return 0
        session = Config.db_session
        return (
            session.query(CliPackageModel)
            .filter(
                CliPackageModel.partition_key == partition_key,
                CliPackageModel.cli_package_uuid == cli_package_uuid,
            )
            .count()
        )

    def list(self, info: ResolveInfo, **filters: Any) -> Any:
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        page_number = filters.get("page_number", 1)
        limit = filters.get("limit", 10)
        package_name = filters.get("package_name")
        status = filters.get("status")

        query = session.query(CliPackageModel)
        if partition_key:
            query = query.filter(CliPackageModel.partition_key == partition_key)
        if package_name:
            query = query.filter(CliPackageModel.package_name == package_name)
        if status:
            query = query.filter(CliPackageModel.status == status)

        total = query.count()
        offset = (page_number - 1) * limit
        rows = (
            query.order_by(CliPackageModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        pkg_list = [self.get_type(info, row) for row in rows]
        return CliPackageListType(cli_package_list=pkg_list, total=total)

    def insert_update(self, info: ResolveInfo, **kwargs: Any) -> Optional[Dict[str, Any]]:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = info.context.get("partition_key")
        cli_package_uuid = kwargs.get("cli_package_uuid")

        try:
            if cli_package_uuid:
                row = (
                    session.query(CliPackageModel)
                    .filter(
                        CliPackageModel.partition_key == partition_key,
                        CliPackageModel.cli_package_uuid == cli_package_uuid,
                    )
                    .first()
                )
                if not row:
                    row = self._create_row(info, **kwargs)
                    session.add(row)
                else:
                    field_map = [
                        "package_name",
                        "git_repository_url",
                        "version",
                        "git_ref",
                        "description",
                        "status",
                        "enabled",
                    ]
                    for field in field_map:
                        if field in kwargs:
                            setattr(row, field, None if kwargs[field] == "null" else kwargs[field])
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

    def _create_row(self, info: ResolveInfo, **kwargs: Any) -> CliPackageModel:
        import uuid

        partition_key = info.context.get("partition_key")
        endpoint_id = info.context.get("endpoint_id")
        part_id = info.context.get("part_id")
        cli_package_uuid = kwargs.get("cli_package_uuid") or str(uuid.uuid4())

        cols = {
            "partition_key": partition_key,
            "endpoint_id": endpoint_id,
            "part_id": part_id,
            "updated_by": kwargs.get("updated_by", "system"),
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "package_name",
            "git_repository_url",
            "version",
            "git_ref",
            "description",
            "status",
            "enabled",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        if cli_package_uuid:
            cols["cli_package_uuid"] = cli_package_uuid

        return CliPackageModel(**cols)

    def delete(self, info: ResolveInfo, **kwargs: Any) -> bool:
        session = Config.db_session
        logger = info.context.get("logger")
        partition_key = info.context.get("partition_key")
        cli_package_uuid = kwargs.get("cli_package_uuid")

        try:
            row = (
                session.query(CliPackageModel)
                .filter(
                    CliPackageModel.partition_key == partition_key,
                    CliPackageModel.cli_package_uuid == cli_package_uuid,
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

    def get_type(self, info: ResolveInfo, row: Any) -> CliPackageType | None:
        data = normalize_row(row)
        if data is None:
            return None
        return CliPackageType(**normalize_to_json(data))

    def resolve_single(self, info: ResolveInfo, **kwargs: Any) -> Optional[CliPackageType]:
        session = Config.db_session
        partition_key = info.context.get("partition_key")

        if kwargs.get("package_name"):
            row = (
                session.query(CliPackageModel)
                .filter(
                    CliPackageModel.partition_key == partition_key,
                    CliPackageModel.package_name == kwargs["package_name"],
                )
                .first()
            )
            return self.get_type(info, row) if row else None

        if "cli_package_uuid" not in kwargs:
            return None

        count = self.count(
            partition_key=partition_key, cli_package_uuid=kwargs["cli_package_uuid"]
        )
        if count == 0:
            return None

        row = (
            session.query(CliPackageModel)
            .filter(
                CliPackageModel.partition_key == partition_key,
                CliPackageModel.cli_package_uuid == kwargs["cli_package_uuid"],
            )
            .first()
        )
        return self.get_type(info, row) if row else None


__all__ = ["CliPackagePGRepository"]