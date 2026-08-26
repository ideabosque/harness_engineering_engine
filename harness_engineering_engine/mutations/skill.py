#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, DateTime, Field, Mutation, String

from ..models.repositories import get_repo
from ..types.skill import SkillType


class InsertUpdateSkill(Mutation):
    skill = Field(SkillType)

    class Arguments:
        skill_uuid = String(required=False)
        name = String(required=False)
        version = String(required=False)
        description = String(required=False)
        source_type = String(required=False)
        source_ref = String(required=False)
        s3_bucket = String(required=False)
        s3_key = String(required=False)
        s3_version_id = String(required=False)
        artifact_checksum = String(required=False)
        content_checksum = String(required=False)
        local_path = String(required=False)
        deployment_status = String(required=False)
        enabled = Boolean(required=False)
        is_active = Boolean(required=False)
        registered_at = DateTime(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "InsertUpdateSkill":
        try:
            skill = get_repo("skill").insert_update(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return InsertUpdateSkill(skill=skill)


class DeleteSkill(Mutation):
    ok = Boolean()

    class Arguments:
        skill_uuid = String(required=True)

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "DeleteSkill":
        try:
            ok = get_repo("skill").delete(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return DeleteSkill(ok=ok)
