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

    # Deployment source
    git_repository_url = String()

    git_ref = String()
    resolved_commit = String()

    content_checksum = String()
    local_path = String()

    deployment_status = String()
    enabled = Boolean()
    is_active = Boolean()

    registered_at = DateTime()
    created_at = DateTime()
    updated_by = String()
    updated_at = DateTime()

    # Populated only when a background git refresh is in progress and
    # the local cache is not yet available. "refreshing" tells the caller
    # to retry after a short wait. Absent in the normal (content ready) case.
    status = String()

    # Populated only by the agent-facing skill(name) read path
    # (handlers.skill_reader.skill), not by plain DB row lookups.
    body = String()
    allowed_commands = Field(JSONCamelCase)
    cli_packages = Field(JSONCamelCase)
    # P9 — content of every file the skill's reference_files frontmatter
    # names/globs, resolved and read fresh each call. Never scripts (those
    # stay execution-only, referenced by path inside allowed_commands).
    references = Field(JSONCamelCase)
    local_content_checksum = String()
    stale_index = Boolean()


class SkillListType(ListObjectType):
    skill_list = List(SkillType)
