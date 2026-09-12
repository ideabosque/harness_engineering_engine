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
from typing import Any, Dict, List, Optional

from ..models.repositories import get_repo
from . import skill_version_cache
from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root


def _resolve_references(skill_dir: Path, patterns: List[str]) -> List[Dict[str, Any]]:
    """Read every file matched by ``patterns`` (paths or globs) into
    ``{"path": ..., "content": ...}`` entries.

    Every match is containment-checked against ``skill_dir`` — a pattern
    that resolves outside of it (e.g. via ``../``) is silently skipped,
    same trust boundary as every other path input in this project. An
    unreadable/binary file is skipped rather than raising, since this is
    best-effort enrichment, not something that should ever break
    ``skill(name)`` for the whole skill. ``SKILL.md`` itself is always
    excluded — its content is already returned via ``body`` — regardless
    of whether it came from the author's own ``reference_files`` or the
    generated sidecar.
    """
    references: List[Dict[str, Any]] = []
    seen: set = set()
    skill_dir_resolved = skill_dir.resolve()

    for pattern in patterns:
        for path in sorted(skill_dir.glob(pattern)):
            if not path.is_file():
                continue
            try:
                path.resolve().relative_to(skill_dir_resolved)
            except ValueError:
                continue  # escaped skill_dir — skip

            rel = str(path.relative_to(skill_dir)).replace("\\", "/")
            if rel in seen or rel == "SKILL.md":
                continue
            seen.add(rel)

            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            references.append({"path": rel, "content": content})

    return references


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
    from git when the local cache is stale or missing.

    When the local cache is stale and a git refresh is needed, the refresh is
    launched in a background thread (via ``skill_refresh_tracker``) instead
    of blocking.  In that case, if ``SKILL.md`` is not yet available locally,
    the function returns a ``status: "refreshing"`` signal so the caller can
    retry after the refresh completes.  If ``SKILL.md`` IS available (stale
    but present), the function returns the stale content immediately while
    the refresh runs in the background — the caller gets a useful response
    on the first call and updated content on the next call.
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
        # Lazy install, fast path: this exact version may already be sitting
        # in the local per-version cache — because this same instance is
        # the one that deployed it, redeployed it, or promoted it earlier.
        # skill_deployment.py deliberately never writes into the live skill
        # directory itself (see its module docstring); this is where that
        # deferred install actually happens. A plain local copy needs no
        # network, so do it synchronously instead of the background-thread
        # git-refresh treatment below — the common case (this instance
        # already has it cached) should never have to return a transient
        # "refreshing" placeholder.
        installed_from_cache = skill_version_cache.install_from_cache(
            skill_root, name, active["version"]
        )
        if installed_from_cache:
            skill_version_cache.write_local_metadata(
                skill_dir,
                name=active["name"],
                version=active["version"],
                git_repository_url=active.get("git_repository_url"),
                git_ref=active.get("git_ref"),
                resolved_commit=active.get("resolved_commit"),
                content_checksum=active.get("content_checksum"),
            )
            logger.info(
                f"Installed skill '{name}' v{active['version']} from local "
                f"version cache (lazy install, no clone needed)."
            )
            # Fall through — SKILL.md is now installed; read it below as normal.
        else:
            # This instance has never cached this version locally —
            # genuinely needs a git fetch. Check if a background refresh is
            # already in progress.
            from .skill_refresh_tracker import is_refreshing, launch_refresh

            if is_refreshing(partition_key, name):
                logger.info(
                    f"Refresh already in progress for skill '{name}' — "
                    f"returning {'stale' if skill_dir.is_dir() else 'refreshing'} signal."
                )
                if not (skill_dir / "SKILL.md").is_file():
                    # No local content at all — tell the caller to retry
                    return {
                        "name": active["name"],
                        "version": active["version"],
                        "description": active["description"],
                        "body": "",
                        "status": "refreshing",
                        "allowed_commands": [],
                        "cli_packages": [],
                        "references": [],
                        "local_path": str(skill_dir),
                        "stale_index": True,
                        "deployment_status": active.get("deployment_status"),
                        "updated_at": active.get("updated_at"),
                    }
                # Fall through: SKILL.md exists locally (stale), return it
                # while the refresh completes in the background.
                logger.info(
                    f"Returning stale content for skill '{name}' while refresh completes."
                )
            else:
                # Launch the refresh in background
                logger.info(
                    f"Local metadata stale or missing for skill '{name}' — "
                    f"launching background refresh from git."
                )
                launch_refresh(
                    logger=logger,
                    partition_key=partition_key,
                    skill_name=name,
                    active=active,
                    skill_root=str(skill_root),
                )

                # If SKILL.md doesn't exist yet (first-time load), return refreshing
                if not (skill_dir / "SKILL.md").is_file():
                    return {
                        "name": active["name"],
                        "version": active["version"],
                        "description": active["description"],
                        "body": "",
                        "status": "refreshing",
                        "allowed_commands": [],
                        "cli_packages": [],
                        "references": [],
                        "local_path": str(skill_dir),
                        "stale_index": True,
                        "deployment_status": active.get("deployment_status"),
                        "updated_at": active.get("updated_at"),
                    }
                # Fall through: return stale content while refresh runs
                logger.info(
                    f"Returning stale content for skill '{name}' while background refresh completes."
                )

    # ------------------------------------------------------------------
    # Validate SKILL.md and compute local checksum
    # ------------------------------------------------------------------
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found for skill '{name}' at {skill_md}")

    parsed = parse_skill_file(skill_md)

    # ------------------------------------------------------------------
    # P9: fill any of allowed_commands/reference_files that SKILL.md left
    # entirely absent from the generated-sections sidecar (never a field
    # the author actually declared, even as an empty list — see
    # docs/DEVELOPMENT_PLAN.md §5). A sidecar left over from a different
    # commit than the one currently active is ignored, not trusted.
    #
    # Read before computing the checksum below: whichever reference_files
    # entries deploy_skill_package pulled in from elsewhere in the repo
    # (see reference_pull.pull_reference_files) were excluded from the
    # *registered* checksum, so this host's local checksum must exclude
    # them too — otherwise every skill with a pulled-in reference file
    # would look permanently "stale" (or, with
    # HSK_ALLOW_UNREGISTERED_CHANGES left at its documented default of
    # false, make skill() raise on every read) even immediately after a
    # clean deploy. That exclusion set lives in its own sidecar, separate
    # from the human/agent-facing generated one — see
    # skill_version_cache.write_checksum_exclusions.
    # ------------------------------------------------------------------
    declared_fields = set(parsed.raw_frontmatter.keys())
    generated = skill_version_cache.read_generated_sidecar(skill_dir)
    if generated and generated.get("resolved_commit") != active.get("resolved_commit"):
        generated = None

    checksum_exclusions = skill_version_cache.read_checksum_exclusions(skill_dir)
    if checksum_exclusions and checksum_exclusions.get("resolved_commit") != active.get(
        "resolved_commit"
    ):
        checksum_exclusions = None
    pulled_reference_files = (
        set(checksum_exclusions.get("excluded_relpaths", [])) if checksum_exclusions else set()
    )

    local_checksum = compute_content_checksum(
        skill_dir, Config.SKILL_LOCAL_METADATA_FILE, excluded_relpaths=pulled_reference_files
    )
    stale_index = local_checksum != active.get("content_checksum")

    if stale_index and not Config.ALLOW_UNREGISTERED_CHANGES:
        raise ValueError(
            f"Local content checksum for skill '{name}' differs from registered "
            f"checksum and HSK_ALLOW_UNREGISTERED_CHANGES is false."
        )

    allowed_commands = parsed.frontmatter.allowed_commands
    if "allowed_commands" not in declared_fields and generated:
        allowed_commands = generated.get("allowed_commands", allowed_commands)

    reference_files = parsed.frontmatter.reference_files
    if "reference_files" not in declared_fields and generated:
        reference_files = generated.get("reference_files", reference_files)

    cli_packages = parsed.frontmatter.cli_packages
    if "cli_packages" not in declared_fields and generated:
        cli_packages = generated.get("cli_packages", cli_packages)

    references = _resolve_references(skill_dir, reference_files)

    return {
        "name": active["name"],
        "version": active["version"],
        "description": active["description"],
        "body": parsed.body,
        "allowed_commands": allowed_commands,
        "cli_packages": cli_packages,
        "references": references,
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

