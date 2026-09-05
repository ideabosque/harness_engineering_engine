# -*- coding: utf-8 -*-
"""Skill reader — on-demand refresh + SKILL.md body retrieval.

This service implements the core ``skill(name)`` logic described in the
development plan:

1. Resolve the active enabled registration row.
2. Read local ``.hsk-skill.json`` metadata.
3. If local metadata is missing, outdated, or inconsistent, fetch the active
   commit from git, validate, and atomically replace the local directory.
4. Read ``SKILL.md`` and return the body + metadata.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ..models.repositories import get_repo
from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root


def _get_active_skill(
    info: Any,
    partition_key: str,
    name: str,
) -> Optional[Dict[str, Any]]:
    """Return the active enabled skill row for ``name``."""
    repo = get_repo("skill")
    # Use the query resolver by name — find active enabled version
    if Config.DB_BACKEND == "dynamodb":
        from ..models.dynamodb.skill import SkillModel
        from ..utils.normalization import normalize_to_json

        results = SkillModel.query(
            partition_key,
            None,
            SkillModel.name == name,
        )
        for row in results:
            data = normalize_to_json(row.attribute_values)
            if data.get("enabled") and data.get("is_active"):
                return data
        return None
    else:
        # PostgreSQL path — use repo list filter
        result = repo.list(info, name=name, enabled=True)
        for item in result.skill_list:
            if item.is_active and item.name == name:
                return {
                    "skill_uuid": item.skill_uuid,
                    "name": item.name,
                    "version": item.version,
                    "description": item.description,
                    "git_repository_url": item.git_repository_url,
                    "git_ref": item.git_ref,
                    "resolved_commit": item.resolved_commit,
                    "content_checksum": item.content_checksum,
                    "local_path": item.local_path,
                    "deployment_status": item.deployment_status,
                    "enabled": item.enabled,
                    "is_active": item.is_active,
                    "registered_at": item.registered_at,
                    "updated_at": item.updated_at,
                }
        return None


def _local_metadata_matches(
    metadata: Optional[Dict[str, Any]],
    active: Dict[str, Any],
) -> bool:
    """Return ``True`` if local metadata matches the active registration."""
    if metadata is None:
        return False
    return (
        metadata.get("name") == active.get("name")
        and metadata.get("version") == active.get("version")
        and metadata.get("content_checksum") == active.get("content_checksum")
        and metadata.get("resolved_commit") == active.get("resolved_commit")
    )


def _download_and_install(
    logger: logging.Logger,
    active: Dict[str, Any],
    root: Path,
) -> None:
    """Fetch the active git commit and install it into the local cache.

    This function clones to a temporary directory, validates, and then
    atomically replaces the skill directory.
    """
    from .skill_refresh import refresh_single_skill

    refresh_single_skill(logger, active, root)


def skill(
    info: Any,
    name: str,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the active enabled skill version and return its body + metadata.

    This is the primary agent-facing read path.  It handles on-demand refresh
    from S3 when the local cache is stale or missing.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    active = _get_active_skill(info, partition_key, name)
    if active is None:
        raise ValueError(f"Skill '{name}' is not enabled or does not exist.")

    skill_root = resolve_skill_root(root)
    skill_dir = skill_root / name
    metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE

    # ------------------------------------------------------------------
    # On-demand refresh check
    # ------------------------------------------------------------------
    local_metadata: Optional[Dict[str, Any]] = None
    if metadata_file.is_file():
        try:
            with open(metadata_file, "r", encoding="utf-8") as fh:
                local_metadata = json.load(fh)
        except Exception:
            local_metadata = None

    if not _local_metadata_matches(local_metadata, active):
        logger.info(
            f"Local metadata stale or missing for skill '{name}' — refreshing from git."
        )
        _download_and_install(logger, active, skill_root)
        # Re-read metadata after refresh
        if metadata_file.is_file():
            with open(metadata_file, "r", encoding="utf-8") as fh:
                local_metadata = json.load(fh)

    # ------------------------------------------------------------------
    # Validate SKILL.md and compute local checksum
    # ------------------------------------------------------------------
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found for skill '{name}' at {skill_md}")

    parsed = parse_skill_file(skill_md)

    local_checksum = compute_content_checksum(skill_dir, Config.SKILL_LOCAL_METADATA_FILE)
    stale_index = local_checksum != active.get("content_checksum")

    if stale_index and not Config.ALLOW_UNREGISTERED_CHANGES:
        raise ValueError(
            f"Local content checksum for skill '{name}' differs from registered "
            f"checksum and HSK_ALLOW_UNREGISTERED_CHANGES is false."
        )

    return {
        "name": active["name"],
        "version": active["version"],
        "description": active["description"],
        "body": parsed.body,
        "allowed_commands": parsed.frontmatter.allowed_commands,
        "cli_packages": parsed.frontmatter.cli_packages,
        "local_path": str(skill_dir),
        "git_repository_url": active.get("git_repository_url"),
        "git_ref": active.get("git_ref"),
        "resolved_commit": active.get("resolved_commit"),
        "content_checksum": active.get("content_checksum"),
        "local_content_checksum": local_checksum,
        "stale_index": stale_index,
        "deployment_status": active.get("deployment_status"),
        "updated_at": active.get("updated_at"),
    }


__all__ = ["skill"]

