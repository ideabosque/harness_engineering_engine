#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import traceback
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import BooleanAttribute, UnicodeAttribute, UTCDateTimeAttribute
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

from ...handlers.config import Config
from ...types.skill import SkillListType, SkillType
from ...utils.normalization import normalize_to_json


class SkillNameIndex(LocalSecondaryIndex):
    """Local secondary index for skill name lookup within a partition."""

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "skill_name-index"

    partition_key = UnicodeAttribute(hash_key=True)
    name = UnicodeAttribute(range_key=True)


class UpdatedAtIndex(LocalSecondaryIndex):
    """Local secondary index for time-ordered skill listing."""

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        projection = AllProjection()
        index_name = "updated_at-index"

    partition_key = UnicodeAttribute(hash_key=True)
    updated_at = UnicodeAttribute(range_key=True)


class SkillModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "hsk-skills"

    partition_key = UnicodeAttribute(hash_key=True)
    skill_uuid = UnicodeAttribute(range_key=True)
    endpoint_id = UnicodeAttribute()
    part_id = UnicodeAttribute()

    # Public identity
    name = UnicodeAttribute()
    version = UnicodeAttribute()
    description = UnicodeAttribute(null=True)

    # Deployment source
    source_type = UnicodeAttribute(null=True)  # always "git"
    source_ref = UnicodeAttribute(null=True)  # git remote URL

    # Git pin — the ref requested at deploy time and the commit it resolved to.
    git_ref = UnicodeAttribute(null=True)
    resolved_commit = UnicodeAttribute(null=True)

    # Integrity
    content_checksum = UnicodeAttribute(null=True)

    # Runtime cache
    local_path = UnicodeAttribute(null=True)

    # Lifecycle
    deployment_status = UnicodeAttribute(default="uploaded")
    enabled = BooleanAttribute(default=True)
    is_active = BooleanAttribute(default=False)

    # Bookkeeping
    registered_at = UTCDateTimeAttribute(null=True)
    created_at = UTCDateTimeAttribute()
    updated_by = UnicodeAttribute()
    updated_at = UTCDateTimeAttribute()

    # Indexes
    skill_name_index = SkillNameIndex()
    updated_at_index = UpdatedAtIndex()


def purge_cache():
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                result = original_function(*args, **kwargs)

                from .cache import purge_entity_cascading_cache

                entity_keys = {}
                entity = kwargs.get("entity")
                if entity:
                    entity_keys["skill_uuid"] = getattr(entity, "skill_uuid", None)

                if not entity_keys.get("skill_uuid"):
                    entity_keys["skill_uuid"] = kwargs.get("skill_uuid")

                partition_key = args[0].context.get("partition_key") or kwargs.get(
                    "partition_key"
                )

                purge_entity_cascading_cache(
                    args[0].context.get("logger"),
                    entity_type="skill",
                    context_keys=(
                        {"partition_key": partition_key} if partition_key else None
                    ),
                    entity_keys=entity_keys if entity_keys else None,
                    cascade_depth=3,
                )

                return result
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
    cache_name=Config.get_cache_name("models", "skill"),
    cache_enabled=Config.is_cache_enabled,
)
def get_skill(partition_key: str, skill_uuid: str) -> SkillModel:
    return SkillModel.get(partition_key, skill_uuid)


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(5),
)
def _get_skill(partition_key: str, skill_uuid: str) -> SkillModel:
    return SkillModel.get(partition_key, skill_uuid)


def get_skill_count(partition_key: str, skill_uuid: str) -> int:
    return SkillModel.count(partition_key, SkillModel.skill_uuid == skill_uuid)


def get_skill_type(info: ResolveInfo, skill: SkillModel) -> SkillType:
    """Return minimal skill data; nested resolvers handle lazy loading."""
    _ = info
    skill_dict = skill.__dict__["attribute_values"].copy()
    return SkillType(**normalize_to_json(skill_dict))


def resolve_skill(info: ResolveInfo, **kwargs: Dict[str, Any]) -> SkillType | None:
    partition_key = info.context.get("partition_key")

    if kwargs.get("name"):
        results = SkillModel.query(
            partition_key,
            None,
            SkillModel.name == kwargs["name"],
        )
        try:
            skill = results.next()
            return get_skill_type(info, skill)
        except Exception:
            return None

    if "skill_uuid" not in kwargs:
        return None

    count = get_skill_count(partition_key, kwargs["skill_uuid"])
    if count == 0:
        return None

    return get_skill_type(
        info,
        get_skill(partition_key, kwargs["skill_uuid"]),
    )


@monitor_decorator
@resolve_list_decorator(
    attributes_to_get=["partition_key", "skill_uuid", "name", "updated_at"],
    list_type_class=SkillListType,
    type_funct=get_skill_type,
)
def resolve_skill_list(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Any:
    partition_key = info.context.get("partition_key")
    name = kwargs.get("name")
    description = kwargs.get("description")
    enabled = kwargs.get("enabled")
    deployment_status = kwargs.get("deployment_status")

    args = []
    inquiry_funct = SkillModel.scan
    count_funct = SkillModel.count
    if partition_key:
        args = [partition_key, None]
        inquiry_funct = SkillModel.updated_at_index.query
        count_funct = SkillModel.updated_at_index.count
        if name:
            count_funct = SkillModel.skill_name_index.count
            args[1] = SkillModel.name == name
            inquiry_funct = SkillModel.skill_name_index.query

    the_filters = None
    if description:
        the_filters &= SkillModel.description.contains(description)
    if enabled is not None:
        the_filters &= SkillModel.enabled == enabled
    if deployment_status:
        the_filters &= SkillModel.deployment_status == deployment_status
    if the_filters is not None:
        args.append(the_filters)

    return inquiry_funct, count_funct, args


@insert_update_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "skill_uuid",
    },
    model_funct=_get_skill,
    count_funct=get_skill_count,
    type_funct=get_skill_type,
)
@purge_cache()
def insert_update_skill(info: ResolveInfo, **kwargs: Dict[str, Any]) -> None:
    partition_key = info.context.get("partition_key")
    if kwargs.get("entity") is None:
        skill_uuid = kwargs.get("skill_uuid")
        cols = {
            "endpoint_id": info.context.get("endpoint_id"),
            "part_id": info.context.get("part_id"),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "name",
            "version",
            "description",
            "source_type",
            "source_ref",
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
        SkillModel(
            partition_key,
            skill_uuid,
            **cols,
        ).save()
        return

    skill = kwargs.get("entity")
    actions = [
        SkillModel.updated_by.set(kwargs["updated_by"]),
        SkillModel.updated_at.set(pendulum.now("UTC")),
    ]

    field_map = {
        "name": SkillModel.name,
        "version": SkillModel.version,
        "description": SkillModel.description,
        "source_type": SkillModel.source_type,
        "source_ref": SkillModel.source_ref,
        "git_ref": SkillModel.git_ref,
        "resolved_commit": SkillModel.resolved_commit,
        "content_checksum": SkillModel.content_checksum,
        "local_path": SkillModel.local_path,
        "deployment_status": SkillModel.deployment_status,
        "enabled": SkillModel.enabled,
        "is_active": SkillModel.is_active,
        "registered_at": SkillModel.registered_at,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(None if kwargs[key] == "null" else kwargs[key]))

    skill.update(actions=actions)
    return


@delete_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "skill_uuid",
    },
    model_funct=get_skill,
)
@purge_cache()
def delete_skill(info: ResolveInfo, **kwargs: Dict[str, Any]) -> bool:
    kwargs.get("entity").delete()
    return True
