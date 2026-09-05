# -*- coding: utf-8 -*-
"""Local skill refresh service.

Compares local ``.hsk-skill.json`` metadata against the registration table's
resolved commit and re-fetches from git — the only source of truth — when it
has changed, then atomically replaces the local skill directory.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import git_client
from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root


def _ignore_hidden(_dir: str, names: List[str]) -> List[str]:
    """``shutil.copytree`` ignore hook — drop dotfiles/dotdirs such as ``.git``."""
    return [n for n in names if n.startswith(".")]


def _resolve_skill_content_dir(clone_dir: Path, skill_name: str) -> Path:
    """Locate the skill's content within a cloned repo.

    Multi-skill repos nest each skill under a folder named after it; a
    single-skill repo has ``SKILL.md`` at the root.
    """
    candidate = clone_dir / skill_name
    if candidate.is_dir() and (candidate / "SKILL.md").is_file():
        return candidate
    if (clone_dir / "SKILL.md").is_file():
        return clone_dir
    raise ValueError(f"Fetched source for '{skill_name}' does not contain SKILL.md")


def _atomic_replace(src: Path, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src``."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def refresh_single_skill(
    logger: logging.Logger,
    active: Dict[str, Any],
    root: Path,
) -> None:
    """Fetch and install the given active skill version into ``root``.

    Steps:
    1. Clone the git remote pinned to the registered commit (falling back to
       ``git_ref`` if the remote rejects fetching by exact SHA).
    2. Locate and validate the skill's SKILL.md.
    3. Compute content checksum and compare to the registered checksum.
    4. Atomically replace the local skill directory.
    5. Write ``.hsk-skill.json``.
    """
    skill_name = active["name"]
    skill_dir = root / skill_name

    git_repository_url = active.get("git_repository_url")
    git_ref = active.get("git_ref") or "main"
    resolved_commit = active.get("resolved_commit")
    if not git_repository_url or not resolved_commit:
        raise ValueError(f"Skill '{skill_name}' is missing git source metadata.")

    logger.info(
        f"Refreshing skill '{skill_name}' — fetching {git_repository_url}@{resolved_commit}"
    )

    clone_dir = git_client.clone_at_commit(git_repository_url, git_ref, resolved_commit)
    try:
        skill_content_dir = _resolve_skill_content_dir(clone_dir, skill_name)
        parse_skill_file(skill_content_dir / "SKILL.md")

        content_checksum = compute_content_checksum(
            skill_content_dir, Config.SKILL_LOCAL_METADATA_FILE
        )
        if content_checksum != active.get("content_checksum"):
            raise ValueError(
                f"Content checksum mismatch for skill '{skill_name}': "
                f"expected {active.get('content_checksum')}, got {content_checksum}"
            )

        install_src = Path(tempfile.mkdtemp(prefix="hsk_install_")) / skill_name
        shutil.copytree(skill_content_dir, install_src, ignore=_ignore_hidden)
        _atomic_replace(install_src, skill_dir)

        metadata = {
            "name": skill_name,
            "version": active["version"],
            "git_repository_url": git_repository_url,
            "git_ref": git_ref,
            "resolved_commit": resolved_commit,
            "content_checksum": content_checksum,
            "last_refresh_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE
        with open(metadata_file, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2)

        logger.info(f"Skill '{skill_name}' refreshed successfully at {skill_dir}")
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def refresh_local_skills(
    info: Any,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare local metadata against the registration table and refresh stale skills.

    Returns a summary of refreshed, skipped, and failed skills.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    skill_root = resolve_skill_root(root)

    from ..models.repositories import get_repo

    repo = get_repo("skill")
    active_result = repo.list(info, enabled=True, is_active=True)
    active_skills = active_result.skill_list if hasattr(active_result, "skill_list") else []

    refreshed: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    for skill_type in active_skills:
        name = skill_type.name
        try:
            skill_dir = skill_root / name
            metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE

            local_metadata = None
            if metadata_file.is_file():
                with open(metadata_file, "r", encoding="utf-8") as fh:
                    local_metadata = json.load(fh)

            active_dict = {
                "name": name,
                "version": skill_type.version,
                "git_repository_url": skill_type.git_repository_url,
                "git_ref": skill_type.git_ref,
                "resolved_commit": skill_type.resolved_commit,
                "content_checksum": skill_type.content_checksum,
            }

            from .skill_reader import _local_metadata_matches

            if _local_metadata_matches(local_metadata, active_dict):
                skipped.append(name)
                continue

            refresh_single_skill(logger, active_dict, skill_root)
            refreshed.append(name)
        except Exception as e:
            logger.error(f"Failed to refresh skill '{name}': {e}")
            failed.append(name)

    return {
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
    }


__all__ = ["refresh_local_skills", "refresh_single_skill"]
