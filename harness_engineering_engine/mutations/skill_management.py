#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Argument, Boolean, Field, Int, List, Mutation, String
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
            git_repository_url=row.git_repository_url,
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
        git_repository_url = String(required=True)
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
                git_repository_url=kwargs["git_repository_url"],
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

    When ``background`` is True, the command is launched detached in a
    background thread and a ``run_id`` is returned immediately.  The caller
    polls status via the ``pollCommand(run_id)`` query.  This is the async
    path for long-running scripts that would otherwise exceed the 30s
    synchronous timeout.
    """

    class Arguments:
        name = String(required=True)
        argv = Argument(List(String), required=True)
        workspace_scope = String(required=False)
        background = Boolean(required=False)

    stdout = String()
    stderr = String()
    exit_code = String()
    timed_out = Boolean()
    truncated = Boolean()
    output_truncated_bytes = Int()
    run_id = String()

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "RunCommand":
        try:
            background = kwargs.get("background", False)

            if background:
                return RunCommand._run_background(info, **kwargs)
            else:
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
                    output_truncated_bytes=result.get("output_truncated_bytes", 0),
                )
        except Exception as e:
            info.context.get("logger").error(traceback.format_exc())
            raise e

    @staticmethod
    def _run_background(info: Any, **kwargs: Dict[str, Any]) -> "RunCommand":
        """Launch a command in background mode and return a run_id handle.

        Reuses the synchronous executor's validation (kill switch, allowlist
        match, shell-metacharacter rejection, CLI package ensure) but launches
        the actual subprocess detached instead of blocking on it.
        """
        from ..handlers.async_command_executor import launch_background_command
        from ..handlers.command_executor import (
            _normalize_argv,
            _match_allowed_command,
            _resolve_and_validate_paths,
        )
        from ..handlers.config import Config
        from ..handlers.skill_path import resolve_skill_root
        from ..handlers.skill_reader import skill as _get_skill

        logger = info.context.get("logger") or __import__("logging").getLogger(__name__)

        # ------------------------------------------------------------------
        # Kill switch
        # ------------------------------------------------------------------
        if not Config.RUN_COMMAND_ENABLED:
            raise RuntimeError(
                "HSK_RUN_COMMAND_ENABLED is false — command execution is disabled."
            )

        # ------------------------------------------------------------------
        # Resolve skill allowlist (same as synchronous path)
        # ------------------------------------------------------------------
        skill_data = _get_skill(info, name=kwargs["name"])
        allowed = skill_data.get("allowed_commands", [])

        if not allowed:
            raise PermissionError(
                f"Skill '{kwargs['name']}' has no allowed_commands — command denied."
            )

        # ------------------------------------------------------------------
        # Ensure CLI packages are installed (same as synchronous path)
        # ------------------------------------------------------------------
        cli_packages = skill_data.get("cli_packages", [])
        if cli_packages:
            from ..handlers.cli_package_manager import ensure_package

            for pkg in cli_packages:
                pkg_name = pkg.get("package_name") or pkg.get("distribution_name")
                if not pkg_name:
                    continue
                result = ensure_package(info, pkg_name)
                if result.get("status") != "ready":
                    error = result.get("error", "unknown error")
                    raise RuntimeError(
                        f"CLI package '{pkg_name}' is not ready: {error}"
                    )

        # ------------------------------------------------------------------
        # Normalize argv
        # ------------------------------------------------------------------
        argv_list = _normalize_argv(kwargs["argv"])

        # ------------------------------------------------------------------
        # Reject shell constructs
        # ------------------------------------------------------------------
        for arg in argv_list:
            if any(c in arg for c in ("|", "&", ";", ">", "<", "`", "$(")):
                raise PermissionError(f"Shell metacharacters rejected: {arg}")

        # ------------------------------------------------------------------
        # Allowlist match
        # ------------------------------------------------------------------
        matched = _match_allowed_command(argv_list, allowed)
        if matched is None:
            raise PermissionError(
                f"Command argv {argv_list!r} does not match any allowed_commands "
                f"entry for skill '{kwargs['name']}'."
            )

        # ------------------------------------------------------------------
        # Resolve paths and working directory
        # ------------------------------------------------------------------
        skill_root = resolve_skill_root()
        skill_dir = skill_root / kwargs["name"]
        argv_list = _resolve_and_validate_paths(
            argv_list, skill_dir, kwargs.get("workspace_scope")
        )

        # ------------------------------------------------------------------
        # Timeout / output limits
        # ------------------------------------------------------------------
        timeout_seconds = int(
            matched.get("timeout_seconds", Config.RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS)
        )
        # Background commands get a larger default timeout since they don't
        # block the caller. Use 10x the synchronous default if not explicitly
        # set in the allowlist entry.
        if "timeout_seconds" not in matched:
            timeout_seconds = max(timeout_seconds * 10, 300)

        output_limit = int(
            matched.get("output_limit_bytes", Config.RUN_COMMAND_OUTPUT_LIMIT_BYTES)
        )

        # ------------------------------------------------------------------
        # Dry-run
        # ------------------------------------------------------------------
        if Config.DRY_RUN:
            logger.info(f"DRY-RUN (background): skill={kwargs['name']} argv={argv_list!r}")
            return RunCommand(
                run_id="dry-run",
                stdout=f"DRY-RUN: {' '.join(argv_list)}",
                stderr="",
                exit_code="0",
                timed_out=False,
                truncated=False,
            )

        # ------------------------------------------------------------------
        # Launch
        # ------------------------------------------------------------------
        partition_key = info.context.get("partition_key")
        logger.info(
            f"Launching background command: skill={kwargs['name']} argv={argv_list!r} "
            f"timeout={timeout_seconds} output_limit={output_limit} tenant={partition_key}"
        )

        result = launch_background_command(
            logger=logger,
            skill_name=kwargs["name"],
            argv=argv_list,
            cwd=str(skill_dir),
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
            info=info,
        )

        return RunCommand(
            run_id=result["run_id"],
            stdout="",
            stderr="",
            exit_code=None,
            timed_out=False,
            truncated=False,
        )
