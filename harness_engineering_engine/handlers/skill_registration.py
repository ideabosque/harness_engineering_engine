# -*- coding: utf-8 -*-
"""Skill registration — scans ``HSK_SKILL_ROOT`` and upserts index rows."""
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import Any, Dict, List, Optional

from ..models.repositories import get_repo
from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root


def register_skills(
    info: Any,
    root: Optional[str] = None,
    prune: bool = False,
) -> Dict[str, Any]:
    """Scan the skill root and register each valid skill in the database.

    This is idempotent: rows whose checksum has not changed are skipped.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    skill_root = resolve_skill_root(root)
    repo = get_repo("skill")

    updated: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    pruned: List[str] = []
    errors: Dict[str, str] = {}

    # Discover skill directories
    discovered = set()
    for skill_md in sorted(skill_root.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        name = skill_dir.name
        discovered.add(name)

        try:
            parsed = parse_skill_file(skill_md)

            # Compute local content checksum (excluding .hsk-skill.json)
            content_checksum = compute_content_checksum(
                skill_dir, Config.SKILL_LOCAL_METADATA_FILE
            )

            # Read local metadata if present
            local_version: Optional[str] = None
            local_metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE
            if local_metadata_file.is_file():
                import json

                with open(local_metadata_file, "r", encoding="utf-8") as fh:
                    local_metadata = json.load(fh)
                local_version = local_metadata.get("version")

            # Build the registration row
            skill_uuid: Optional[str] = None
            if Config.DB_BACKEND == "dynamodb":
                from ..models.dynamodb.skill import SkillModel
                from ..utils.normalization import normalize_to_json

                existing = None
                results = SkillModel.query(
                    partition_key,
                    None,
                    SkillModel.name == name,
                )
                for row in results:
                    existing = normalize_to_json(row.attribute_values)
                    break

                if existing:
                    skill_uuid = existing["skill_uuid"]
                    if existing.get("content_checksum") == content_checksum:
                        logger.info(f"Skill '{name}' unchanged — skipping.")
                        skipped.append(name)
                        continue
                else:
                    import uuid

                    skill_uuid = str(uuid.uuid4())

                repo.insert_update(
                    info,
                    skill_uuid=skill_uuid,
                    name=name,
                    version=local_version or "0.0.0",
                    description=parsed.frontmatter.description,
                    content_checksum=content_checksum,
                    local_path=str(skill_dir),
                    deployment_status="registered",
                    enabled=True,
                    is_active=True,
                    updated_by="system",
                )
                updated.append(name)
                logger.info(f"Registered skill '{name}' (uuid={skill_uuid})")
            else:
                # PostgreSQL path
                existing_list = repo.list(info, name=name, enabled=True)
                existing = None
                for item in existing_list.skill_list:
                    if item.name == name:
                        existing = {
                            "skill_uuid": item.skill_uuid,
                            "content_checksum": item.content_checksum,
                        }
                        break

                if existing:
                    skill_uuid = existing["skill_uuid"]
                    if existing.get("content_checksum") == content_checksum:
                        skipped.append(name)
                        continue
                else:
                    import uuid

                    skill_uuid = str(uuid.uuid4())

                repo.insert_update(
                    info,
                    skill_uuid=skill_uuid,
                    name=name,
                    version=local_version or "0.0.0",
                    description=parsed.frontmatter.description,
                    content_checksum=content_checksum,
                    local_path=str(skill_dir),
                    deployment_status="registered",
                    enabled=True,
                    is_active=True,
                    updated_by="system",
                )
                updated.append(name)

        except Exception as e:
            logger.error(f"Failed to register skill '{name}': {e}")
            failed.append(name)
            errors[name] = str(e)

    # Prune rows whose folders no longer exist
    if prune:
        all_rows = repo.list(info, enabled=True)
        for row in all_rows.skill_list:
            if row.name not in discovered:
                try:
                    repo.insert_update(
                        info,
                        skill_uuid=row.skill_uuid,
                        enabled=False,
                        updated_by="system",
                    )
                    pruned.append(row.name)
                    logger.info(f"Pruned skill '{row.name}' (folder removed)")
                except Exception as e:
                    logger.error(f"Failed to prune skill '{row.name}': {e}")

    return {
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "pruned": pruned,
        "errors": errors,
    }


__all__ = ["register_skills"]

