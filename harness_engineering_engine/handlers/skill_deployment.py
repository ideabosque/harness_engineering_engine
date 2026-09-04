# -*- coding: utf-8 -*-
"""Skill deployment service — git intake, validation, local install, registration.

There is no intermediate artifact store: a skill is cloned straight from its
git remote, validated, checksummed, and cached locally
(``skill_version_cache``) so ``promoteSkillVersion``/``rollbackSkill`` can
swap versions instantly; the active version is additionally installed into
``HSK_SKILL_ROOT``. The registration row records the resolved commit SHA so
*other* hosts (that never cached this version locally) can refresh straight
from the same git remote (see ``skill_refresh.py``).
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models.repositories import get_repo
from . import git_client, skill_version_cache
from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root

SOURCE_TYPE = "git"


# ---------------------------------------------------------------------------
# Package validation
# ---------------------------------------------------------------------------

def _validate_skill_dir(skill_dir: Path) -> Dict[str, Any]:
    """Validate a skill directory and return frontmatter data."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"Missing SKILL.md in {skill_dir}")
    parsed = parse_skill_file(skill_md)
    return {
        "name": parsed.frontmatter.name,
        "description": parsed.frontmatter.description,
        "allowed_commands": parsed.frontmatter.allowed_commands,
        "cli_packages": parsed.frontmatter.cli_packages,
    }


# ---------------------------------------------------------------------------
# Deployment orchestration
# ---------------------------------------------------------------------------

def deploy_skill_package(
    info: Any,
    source: str,
    version: Optional[str] = None,
    skill_name: Optional[str] = None,
    git_ref: str = "main",
) -> Dict[str, Any]:
    """Deploy a skill package from a git remote.

    Steps:
    1. For a named source, cheaply resolve ``git_ref`` to a commit SHA
       (``git ls-remote`` — no clone) and skip the deploy entirely if that
       commit is already registered as this skill's source — git is the only
       thing consulted to decide whether a new version exists.
    2. Otherwise, clone the remote at ``git_ref`` and validate each skill
       found.
    3. Compute a content checksum for each skill.
    4. Install directly into ``HSK_SKILL_ROOT`` on this host when the
       version becomes active.
    5. Register each skill version in the database.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    repo = get_repo("skill")
    skill_root = resolve_skill_root()

    # ------------------------------------------------------------------
    # Cheap version check — git only, no clone.
    # ------------------------------------------------------------------
    if skill_name:
        sha = git_client.resolve_ref_sha(source, git_ref)
        existing = repo.list(info, name=skill_name, enabled=True)
        for row in getattr(existing, "skill_list", []):
            if (
                row.name == skill_name
                and row.source_ref == source
                and row.git_ref == git_ref
                and row.resolved_commit == sha
            ):
                logger.info(
                    f"Skill '{skill_name}' already at commit {sha} for "
                    f"{source}@{git_ref} — skipping redeploy."
                )
                return {"deployed": [], "failed": [], "skipped": [skill_name]}

    deployed: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []

    work_dir = Path(tempfile.mkdtemp(prefix="hsk_deploy_work_"))
    clone_dir: Optional[Path] = None
    try:
        clone_dir = git_client.clone_at_ref(source, git_ref)
        resolved_commit = git_client.current_commit_sha(clone_dir)
        content_root = clone_dir

        # ------------------------------------------------------------------
        # Discover skills
        # ------------------------------------------------------------------
        skill_dirs = sorted(content_root.glob("*/SKILL.md"))
        if not skill_dirs:
            if (content_root / "SKILL.md").is_file():
                skill_dirs = [content_root / "SKILL.md"]
            else:
                raise ValueError(f"No SKILL.md found in git source '{source}'.")

        for skill_md in skill_dirs:
            skill_source_dir = skill_md.parent
            try:
                validation = _validate_skill_dir(skill_source_dir)
                resolved_name = skill_name or validation["name"]
                resolved_version = version or datetime.now(timezone.utc).strftime(
                    "%Y.%m.%d.1"
                )

                content_checksum = compute_content_checksum(
                    skill_source_dir, Config.SKILL_LOCAL_METADATA_FILE
                )

                # Register in database. A skill's first-ever version is
                # activated automatically so it is immediately retrievable;
                # subsequent versions land inactive and require an explicit
                # promoteSkillVersion so a deploy never silently replaces
                # what agents are currently served.
                skill_uuid = str(uuid.uuid4())

                existing_active = repo.list(
                    info, name=resolved_name, enabled=True, is_active=True
                )
                has_active = bool(getattr(existing_active, "skill_list", []))
                activate = not has_active

                repo.insert_update(
                    info,
                    skill_uuid=skill_uuid,
                    name=resolved_name,
                    version=resolved_version,
                    description=validation["description"],
                    source_type=SOURCE_TYPE,
                    source_ref=source,
                    git_ref=git_ref,
                    resolved_commit=resolved_commit,
                    content_checksum=content_checksum,
                    deployment_status="deployed" if activate else "registered",
                    enabled=True,
                    is_active=activate,
                    registered_at=datetime.now(timezone.utc),
                    updated_by="system",
                )

                # Cache this version's content locally so promote/rollback
                # can swap to it instantly with no remote fetch. Only the
                # active version is additionally installed into the live
                # skill directory — an inactive version stays cached-only
                # until promoted.
                skill_version_cache.store_version(
                    skill_root, resolved_name, resolved_version, skill_source_dir
                )
                if activate:
                    skill_version_cache.install_from_cache(
                        skill_root, resolved_name, resolved_version
                    )
                    skill_version_cache.write_local_metadata(
                        skill_root / resolved_name,
                        name=resolved_name,
                        version=resolved_version,
                        source_type=SOURCE_TYPE,
                        source_ref=source,
                        git_ref=git_ref,
                        resolved_commit=resolved_commit,
                        content_checksum=content_checksum,
                    )

                logger.info(
                    f"Deployed skill '{resolved_name}' v{resolved_version} "
                    f"(commit={resolved_commit}, active={activate})"
                )

                deployed.append(
                    {
                        "skill_uuid": skill_uuid,
                        "name": resolved_name,
                        "version": resolved_version,
                        "source_type": SOURCE_TYPE,
                        "git_ref": git_ref,
                        "resolved_commit": resolved_commit,
                        "content_checksum": content_checksum,
                        "is_active": activate,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to deploy skill from {skill_md}: {e}")
                failed.append({"skill": str(skill_md), "error": str(e)})

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if clone_dir is not None:
            shutil.rmtree(clone_dir, ignore_errors=True)

    return {
        "deployed": deployed,
        "failed": failed,
        "skipped": [],
    }


__all__ = ["deploy_skill_package"]
