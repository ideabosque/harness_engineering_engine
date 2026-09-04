#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Argument, Boolean, Field, List, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..handlers import skill_version_cache
from ..handlers.skill_path import resolve_skill_root


def _activate_local_content(row: Any) -> None:
    """Swap the live skill directory to a newly-activated version's content.

    Uses the local version cache populated at deploy time — no remote fetch.
    A version that was never cached on this host (e.g. deployed from a
    different host) falls back to the existing on-demand git refresh the
    next time ``skill(name)`` is read.
    """
    skill_root = resolve_skill_root()
    installed = skill_version_cache.install_from_cache(
        skill_root, row.name, row.version
    )
    if installed:
        skill_version_cache.write_local_metadata(
            skill_root / row.name,
            name=row.name,
            version=row.version,
            source_type=row.source_type,
            source_ref=row.source_ref,
            git_ref=row.git_ref,
            resolved_commit=row.resolved_commit,
            content_checksum=row.content_checksum,
        )


class DeploySkillPackage(Mutation):
    """Deploy a skill package from a git remote.

    Git is the only thing consulted to decide whether a new version exists:
    when ``skillName`` is given, the target ref is resolved to a commit SHA
    via a cheap ``git ls-remote`` and the deploy is skipped (see ``skipped``)
    if that commit is already registered. Otherwise the repo is cloned and
    installed directly into ``HSK_SKILL_ROOT`` — there is no S3 or other
    artifact store in between.
    """

    class Arguments:
        source = String(required=True)
        version = String(required=False)
        skill_name = String(required=False)
        git_ref = String(required=False)

    deployed = Field(JSONCamelCase)
    failed = Field(JSONCamelCase)
    skipped = Field(JSONCamelCase)
    ok = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "DeploySkillPackage":
        try:
            from ..handlers.skill_deployment import deploy_skill_package

            result = deploy_skill_package(
                info,
                source=kwargs["source"],
                version=kwargs.get("version"),
                skill_name=kwargs.get("skill_name"),
                git_ref=kwargs.get("git_ref", "main"),
            )
            return DeploySkillPackage(
                deployed=result["deployed"],
                failed=result["failed"],
                skipped=result.get("skipped", []),
                ok=True,
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class RefreshLocalSkills(Mutation):
    """Compare local metadata against the registration table and refresh stale skills."""

    ok = Boolean()
    refreshed = Field(JSONCamelCase)
    skipped = Field(JSONCamelCase)
    failed_list = Field(JSONCamelCase, name="failed")

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "RefreshLocalSkills":
        try:
            from ..handlers.skill_refresh import refresh_local_skills

            result = refresh_local_skills(info)
            return RefreshLocalSkills(
                refreshed=result["refreshed"],
                skipped=result["skipped"],
                failed_list=result["failed"],
                ok=True,
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class RegisterSkills(Mutation):
    """Scan HSK_SKILL_ROOT and register each skill in the database."""

    class Arguments:
        root = String(required=False)
        prune = Boolean(required=False)

    ok = Boolean()
    updated = Field(JSONCamelCase)
    skipped = Field(JSONCamelCase)
    failed_list = Field(JSONCamelCase, name="failed")
    pruned = Field(JSONCamelCase)
    errors = Field(JSONCamelCase)

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "RegisterSkills":
        try:
            from ..handlers.skill_registration import register_skills

            result = register_skills(
                info,
                root=kwargs.get("root"),
                prune=kwargs.get("prune", False),
            )
            return RegisterSkills(
                updated=result["updated"],
                skipped=result["skipped"],
                failed_list=result["failed"],
                pruned=result["pruned"],
                errors=result["errors"],
                ok=True,
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class PromoteSkillVersion(Mutation):
    """Mark a registered version as the active version for a skill."""

    class Arguments:
        name = String(required=True)
        version = String(required=True)
        updated_by = String(required=True)

    ok = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "PromoteSkillVersion":
        try:
            from ..models.repositories import get_repo

            repo = get_repo("skill")
            partition_key = info.context.get("partition_key")

            # Deactivate all versions for this skill
            rows = repo.list(info, name=kwargs["name"], enabled=True)
            for row in rows.skill_list:
                if row.is_active:
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        is_active=False,
                        deployment_status="registered",
                        updated_by=kwargs["updated_by"],
                    )

            # Activate the requested version
            target = repo.list(info, name=kwargs["name"], enabled=True)
            for row in target.skill_list:
                if row.version == kwargs["version"]:
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        is_active=True,
                        deployment_status="deployed",
                        updated_by=kwargs["updated_by"],
                    )
                    _activate_local_content(row)
                    return PromoteSkillVersion(ok=True)

            raise ValueError(
                f"Skill '{kwargs['name']}' version '{kwargs['version']}' not found."
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class RollbackSkill(Mutation):
    """Roll back to a previous registered version (management-state change only)."""

    class Arguments:
        name = String(required=True)
        version = String(required=True)
        updated_by = String(required=True)

    ok = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "RollbackSkill":
        try:
            from ..models.repositories import get_repo

            repo = get_repo("skill")

            # Find current active version
            current_rows = repo.list(info, name=kwargs["name"], enabled=True)
            for row in current_rows.skill_list:
                if row.is_active:
                    # Mark current as rolled_back
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        is_active=False,
                        deployment_status="rolled_back",
                        updated_by=kwargs["updated_by"],
                    )

            # Find target version and activate it
            target_rows = repo.list(info, name=kwargs["name"], enabled=True)
            for row in target_rows.skill_list:
                if row.version == kwargs["version"]:
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        is_active=True,
                        deployment_status="deployed",
                        updated_by=kwargs["updated_by"],
                    )
                    _activate_local_content(row)
                    return RollbackSkill(ok=True)

            raise ValueError(
                f"Skill '{kwargs['name']}' version '{kwargs['version']}' not found."
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class DisableSkill(Mutation):
    """Disable a skill or specific skill version."""

    class Arguments:
        name = String(required=True)
        version = String(required=False)
        updated_by = String(required=True)

    ok = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "DisableSkill":
        try:
            from ..models.repositories import get_repo

            repo = get_repo("skill")
            rows = repo.list(info, name=kwargs["name"])
            for row in rows.skill_list:
                if kwargs.get("version") and row.version != kwargs["version"]:
                    continue
                repo.insert_update(
                    info,
                    skill_uuid=row.skill_uuid,
                    enabled=False,
                    updated_by=kwargs["updated_by"],
                )
            return DisableSkill(ok=True)
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class PruneSkillVersions(Mutation):
    """Disable old skill version rows while keeping their git history intact."""

    class Arguments:
        name = String(required=True)
        keep = String(required=False)
        updated_by = String(required=True)

    ok = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "PruneSkillVersions":
        try:
            from ..models.repositories import get_repo

            repo = get_repo("skill")
            keep = int(kwargs.get("keep", 3))
            updated_by = kwargs["updated_by"]

            rows = repo.list(info, name=kwargs["name"], enabled=True)
            # Sort by updated_at descending — keep newest N
            skill_versions = sorted(
                rows.skill_list,
                key=lambda r: r.updated_at,
                reverse=True,
            )
            skill_root = resolve_skill_root()
            for row in skill_versions[keep:]:
                repo.insert_update(
                    info,
                    skill_uuid=row.skill_uuid,
                    enabled=False,
                    updated_by=updated_by,
                )
                # Reclaim local disk — git history remains the durable
                # record, so the pruned version stays recoverable via a redeploy.
                skill_version_cache.remove_version(skill_root, row.name, row.version)

            return PruneSkillVersions(ok=True)
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e


class RunCommand(Mutation):
    """Execute a guarded command for a skill.

    This mutation is the bridge between the MCP-side ``run_command`` tool and
    the guarded command executor service.  It validates the allowlist, then
    executes the command via the executor.
    """

    class Arguments:
        name = String(required=True)
        argv = Argument(List(String), required=True)
        workspace_scope = String(required=False)

    stdout = String()
    stderr = String()
    exit_code = String()
    timed_out = Boolean()
    truncated = Boolean()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "RunCommand":
        try:
            from ..handlers.command_executor import execute_command

            result = execute_command(
                info,
                skill_name=kwargs["name"],
                argv=kwargs["argv"],
                workspace_scope=kwargs.get("workspace_scope"),
            )
            return RunCommand(
                stdout=result["stdout"],
                stderr=result["stderr"],
                exit_code=str(result["exit_code"]),
                timed_out=result["timed_out"],
                truncated=result["truncated"],
            )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e
