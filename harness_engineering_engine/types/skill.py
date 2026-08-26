#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from graphene import Boolean, DateTime, Field, List, ObjectType, String
from silvaengine_dynamodb_base import ListObjectType
from silvaengine_utility import JSONCamelCase


class SkillType(ObjectType):
    partition_key = String()
    endpoint_id = String()
    part_id = String()
    skill_uuid = String()

    name = String()
    version = String()
    description = String()

    source_type = String()
    source_ref = String()

    s3_bucket = String()
    s3_key = String()
    s3_version_id = String()

    artifact_checksum = String()
    content_checksum = String()
    local_path = String()

    deployment_status = String()
    enabled = Boolean()
    is_active = Boolean()

    registered_at = DateTime()
    created_at = DateTime()
    updated_by = String()
    updated_at = DateTime()

    # Populated only by the agent-facing skill(name) read path
    # (handlers.skill_reader.skill), not by plain DB row lookups.
    body = String()
    allowed_commands = Field(JSONCamelCase)
    cli_packages = Field(JSONCamelCase)
    local_content_checksum = String()
    stale_index = Boolean()


class SkillListType(ListObjectType):
    skill_list = List(SkillType)
