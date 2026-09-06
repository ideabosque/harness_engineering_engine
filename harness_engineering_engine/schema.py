#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import time
from typing import Any, Dict

from graphene import Boolean, Field, Int, ObjectType, ResolveInfo, String

from .mutations.cli_package import (
    DeleteCliPackage,
    EnsureCliPackage,
    InsertUpdateCliPackage,
)
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
from .queries.cli_package import resolve_cli_package, resolve_cli_package_list
from .queries.poll_command import resolve_poll_command
from .queries.skill import resolve_search_skills, resolve_skill, resolve_skill_list
from .types.cli_package import CliPackageListType, CliPackageType
from .types.poll_command import PollCommandType
from .types.skill import SkillListType, SkillType


def type_class():
    return [
        SkillType,
        SkillListType,
        CliPackageType,
        CliPackageListType,
        PollCommandType,
    ]


class Query(ObjectType):
    ping = String()

    # Admin catalog view — not exposed as an agent MCP tool.
    skills = Field(
        SkillListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        name_filter=String(name="name", required=False),
        description_filter=String(name="description", required=False),
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
        name=String(name="name", required=False),
        skill_uuid=String(required=False),
    )

    # CLI package admin view
    cli_packages = Field(
        CliPackageListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        package_name=String(required=False),
        status=String(required=False),
    )

    cli_package = Field(
        CliPackageType,
        package_name=String(required=False),
        cli_package_uuid=String(required=False),
    )

    # Agent-facing async command polling
    poll_command = Field(
        PollCommandType,
        run_id=String(name="run_id", required=True),
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

    def resolve_cli_packages(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> CliPackageListType:
        return resolve_cli_package_list(info, **kwargs)

    def resolve_cli_package(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> CliPackageType | None:
        return resolve_cli_package(info, **kwargs)

    def resolve_poll_command(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> PollCommandType | None:
        return resolve_poll_command(info, **kwargs)


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

    # CLI package management (v1.1)
    insert_update_cli_package = InsertUpdateCliPackage.Field()
    delete_cli_package = DeleteCliPackage.Field()
    ensure_cli_package = EnsureCliPackage.Field()