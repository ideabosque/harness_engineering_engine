#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import traceback
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import (
    BooleanAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from silvaengine_dynamodb_base import (
    BaseModel,
    delete_decorator,
    insert_update_decorator,
    monitor_decorator,
    resolve_list_decorator,
)
from silvaengine_utility import method_cache
from tenacity import retry, stop_after_attempt, wait_exponential

from ..handlers.config import Config
from ..types.cli_package import CliPackageListType, CliPackageType
from ..utils.normalization import normalize_to_json


class PackageNameIndex(LocalSecondaryIndex):
    """Local secondary index for package name lookup within a partition."""

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "package_name-index"

    partition_key = UnicodeAttribute(hash_key=True)
    package_name = UnicodeAttribute(range_key=True)


class UpdatedAtIndex(LocalSecondaryIndex):
    """Local secondary index for time-ordered package listing."""

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "updated_at-index"

    partition_key = UnicodeAttribute(hash_key=True)
    updated_at = UnicodeAttribute(range_key=True)


class CliPackageModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "hsk-cli-packages"

    partition_key = UnicodeAttribute(hash_key=True)
    cli_package_uuid = UnicodeAttribute(range_key=True)
    endpoint_id = UnicodeAttribute()
    part_id = UnicodeAttribute()

    # Package identity
    package_name = UnicodeAttribute()
    github_repository_url = UnicodeAttribute()
    version = UnicodeAttribute()
    git_ref = UnicodeAttribute(null=True)
    description = UnicodeAttribute(null=True)

    # Lifecycle
    status = UnicodeAttribute(default="active")  # active | inactive | failed
    enabled = BooleanAttribute(default=True)

    # Bookkeeping
    created_at = UTCDateTimeAttribute()
    updated_by = UnicodeAttribute()
    updated_at = UTCDateTimeAttribute()

    # Indexes
    package_name_index = PackageNameIndex()
    updated_at_index = UpdatedAtIndex()


def purge_cache():
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                return original_function(*args, **kwargs)
            except Exception as e:
                log = traceback.format_exc()
                args[0].context.get("logger").error(log)
                raise e

        return wrapper_function

    return actual_decorator


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(5),
)
@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("models", "cli_package"),
    cache_enabled=Config.is_cache_enabled,
)
def get_cli_package(partition_key: str, cli_package_uuid: str) -> CliPackageModel:
    return CliPackageModel.get(partition_key, cli_package_uuid)


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(5),
)
def _get_cli_package(partition_key: str, cli_package_uuid: str) -> CliPackageModel:
    return CliPackageModel.get(partition_key, cli_package_uuid)


def get_cli_package_count(partition_key: str, cli_package_uuid: str) -> int:
    return CliPackageModel.count(
        partition_key, CliPackageModel.cli_package_uuid == cli_package_uuid
    )


def get_cli_package_type(info: ResolveInfo, pkg: CliPackageModel) -> CliPackageType:
    _ = info
    pkg_dict = pkg.__dict__["attribute_values"].copy()
    return CliPackageType(**normalize_to_json(pkg_dict))


def resolve_cli_package(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> CliPackageType | None:
    partition_key = info.context.get("partition_key")

    if kwargs.get("package_name"):
        results = CliPackageModel.query(
            partition_key,
            None,
            CliPackageModel.package_name == kwargs["package_name"],
        )
        try:
            pkg = results.next()
            return get_cli_package_type(info, pkg)
        except Exception:
            return None

    if "cli_package_uuid" not in kwargs:
        return None

    count = get_cli_package_count(partition_key, kwargs["cli_package_uuid"])
    if count == 0:
        return None

    return get_cli_package_type(
        info,
        get_cli_package(partition_key, kwargs["cli_package_uuid"]),
    )


@monitor_decorator
@resolve_list_decorator(
    attributes_to_get=[
        "partition_key",
        "cli_package_uuid",
        "package_name",
        "updated_at",
    ],
    list_type_class=CliPackageListType,
    type_funct=get_cli_package_type,
)
def resolve_cli_package_list(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Any:
    partition_key = info.context.get("partition_key")
    package_name = kwargs.get("package_name")
    status = kwargs.get("status")

    args = []
    inquiry_funct = CliPackageModel.scan
    count_funct = CliPackageModel.count
    if partition_key:
        args = [partition_key, None]
        inquiry_funct = CliPackageModel.updated_at_index.query
        count_funct = CliPackageModel.updated_at_index.count
        if package_name:
            count_funct = CliPackageModel.package_name_index.count
            args[1] = CliPackageModel.package_name == package_name
            inquiry_funct = CliPackageModel.package_name_index.query

    the_filters = None
    if status:
        the_filters &= CliPackageModel.status == status
    if the_filters is not None:
        args.append(the_filters)

    return inquiry_funct, count_funct, args


@insert_update_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "cli_package_uuid",
    },
    model_funct=_get_cli_package,
    count_funct=get_cli_package_count,
    type_funct=get_cli_package_type,
)
@purge_cache()
def insert_update_cli_package(info: ResolveInfo, **kwargs: Dict[str, Any]) -> None:
    partition_key = info.context.get("partition_key")
    if kwargs.get("entity") is None:
        import uuid

        cli_package_uuid = kwargs.get("cli_package_uuid") or str(uuid.uuid4())
        cols = {
            "endpoint_id": info.context.get("endpoint_id"),
            "part_id": info.context.get("part_id"),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "package_name",
            "github_repository_url",
            "version",
            "git_ref",
            "description",
            "status",
            "enabled",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]
        CliPackageModel(
            partition_key,
            cli_package_uuid,
            **cols,
        ).save()
        return

    pkg = kwargs.get("entity")
    actions = [
        CliPackageModel.updated_by.set(kwargs["updated_by"]),
        CliPackageModel.updated_at.set(pendulum.now("UTC")),
    ]

    field_map = {
        "package_name": CliPackageModel.package_name,
        "github_repository_url": CliPackageModel.github_repository_url,
        "version": CliPackageModel.version,
        "git_ref": CliPackageModel.git_ref,
        "description": CliPackageModel.description,
        "status": CliPackageModel.status,
        "enabled": CliPackageModel.enabled,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(None if kwargs[key] == "null" else kwargs[key]))

    pkg.update(actions=actions)
    return


@delete_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "cli_package_uuid",
    },
    model_funct=get_cli_package,
)
@purge_cache()
def delete_cli_package(info: ResolveInfo, **kwargs: Dict[str, Any]) -> bool:
    kwargs.get("entity").delete()
    return True