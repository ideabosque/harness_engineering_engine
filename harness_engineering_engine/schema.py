#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import time
from typing import Any, Dict

from graphene import Boolean, Field, Int, ObjectType, ResolveInfo, String

from .mutations.skill import DeleteSkill, InsertUpdateSkill
from .mutations.skill_management import (
    DeploySkillPackage,
    DisableSkill,
    PruneSkillVersions,
    PromoteSkillVersion,
    RefreshLocalSkills,
    RegisterSkills,
    RollbackSkill,
    RunCommand,
)
from .queries.skill import resolve_search_skills, resolve_skill, resolve_skill_list
from .types.skill import SkillListType, SkillType


def type_class():
    return [
        SkillType,
        SkillListType,
    ]


class Query(ObjectType):
    ping = String()

    # Admin catalog view — not exposed as an agent MCP tool.
    skills = Field(
        SkillListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        name=String(required=False),
        description=String(required=False),
        enabled=Boolean(required=False),
        deployment_status=String(required=False),
    )

    # Agent-facing search
    search_skills = Field(
        SkillListType,
        query=String(required=True),
        limit=Int(required=False),
    )

    # Agent-facing retrieval
    skill = Field(
        SkillType,
        name=String(required=False),
        skill_uuid=String(required=False),
    )

    def resolve_ping(self, info: ResolveInfo) -> str:
        return f"Hello at {time.strftime('%X')}!!"

    def resolve_skills(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> SkillListType:
        return resolve_skill_list(info, **kwargs)

    def resolve_search_skills(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> SkillListType:
        return resolve_search_skills(info, **kwargs)

    def resolve_skill(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> SkillType | None:
        return resolve_skill(info, **kwargs)


class Mutations(ObjectType):
    # Deployment / management
    deploy_skill_package = DeploySkillPackage.Field()
    refresh_local_skills = RefreshLocalSkills.Field()
    rollback_skill = RollbackSkill.Field()
    promote_skill_version = PromoteSkillVersion.Field()
    disable_skill = DisableSkill.Field()
    prune_skill_versions = PruneSkillVersions.Field()
    register_skills = RegisterSkills.Field()

    # Skill CRUD
    insert_update_skill = InsertUpdateSkill.Field()
    delete_skill = DeleteSkill.Field()

    # Guarded command execution
    run_command = RunCommand.Field()
