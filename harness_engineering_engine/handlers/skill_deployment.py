# -*- coding: utf-8 -*-
"""Skill deployment service — git intake, validation, local caching, registration.

There is no intermediate artifact store: a skill is cloned straight from its
git remote, validated, checksummed, and cached locally
(``skill_version_cache.store_version``) so ``promoteSkillVersion``/
``rollbackSkill`` can swap versions instantly. Deploy deliberately does
*not* also install into the live ``HSK_SKILL_ROOT/<name>`` directory —
that's lazy: the first ``skill()``/``runCommand`` call against this version
(on *any* instance, including this one) installs it, synchronously from
this local cache when present (no network), or by cloning from git when not
(see ``skill_reader.py``/``skill_refresh.py``). This keeps every instance's
install path uniform in a multi-instance deployment instead of only ever
pre-warming whichever instance happened to receive the deploy call. The
registration row records the resolved commit SHA so *other* hosts (that
never cached this version locally) can refresh straight from the same git
remote.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..models.repositories import get_repo
from . import git_client, skill_version_cache
from .checksums import compute_content_checksum
from .config import Config
from .reference_pull import pull_reference_files
from .section_generator import generate_missing_sections
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root




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
        "body": parsed.body,
        "allowed_commands": parsed.frontmatter.allowed_commands,
        "cli_packages": parsed.frontmatter.cli_packages,
        "reference_files": parsed.frontmatter.reference_files,
        # Which frontmatter keys the author actually wrote — distinct from
        # the parsed values above, which default to [] whether a field was
        # written as an empty list on purpose or left out entirely. P9
        # generation must only fill a key that's genuinely absent here.
        "declared_fields": set(parsed.raw_frontmatter.keys()),
    }


# Folder names (case-insensitive) whose contents must never be proposed
# as reference_files — none of this is documentation/config material a
# skill needs a copy of.
_EXCLUDED_FOLDER_NAMES = {"docs", "tests"}


def _is_under_excluded_folder(rel_parts: tuple) -> bool:
    """True if any directory component matches one of
    ``_EXCLUDED_FOLDER_NAMES`` (case-insensitive)."""
    return any(part.lower() in _EXCLUDED_FOLDER_NAMES for part in rel_parts[:-1])


def _is_readme(rel_parts: tuple) -> bool:
    """True if the filename component is ``README.md`` (case-insensitive)."""
    return bool(rel_parts) and rel_parts[-1].lower() == "readme.md"


def _list_available_files(skill_dir: Path) -> List[str]:
    """Sorted relative paths of every non-hidden file under ``skill_dir``,
    excluding ``SKILL.md``/``README.md`` and anything under a ``docs`` or
    ``tests`` folder.

    Used to ground P9's ``reference_files`` generation in files that
    actually exist — never an invented path. ``SKILL.md`` is excluded so
    it's never even a candidate: its content is already returned via
    ``body`` on every read, so including it again in ``references`` would
    just be duplication. ``README.md``/``docs``/``tests`` are excluded
    the same way — none of that is reference material a skill needs a
    copy of.
    """
    files: List[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if _is_under_excluded_folder(rel.parts) or _is_readme(rel.parts):
            continue
        rel_str = str(rel).replace("\\", "/")
        if rel_str == "SKILL.md":
            continue
        files.append(rel_str)
    return files


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _list_repo_wide_files(content_root: Path, skill_dir_set: Set[Path]) -> List[str]:
    """Repo-root-relative paths of every non-hidden file in the whole
    cloned repository, excluding every discovered skill's own directory
    (so one skill's ``reference_files`` generation is never handed
    another skill's internal files as a candidate), every ``SKILL.md``/
    ``README.md``, and anything under a ``docs`` or ``tests`` folder.

    Widens ``reference_files`` generation candidates beyond a skill's own
    directory for monorepos that keep shared reference material (config,
    and even a CLI dependency's own source) at the repository root rather
    than duplicated into every skill's own folder — a real, common shape,
    not something to force every skill author to restructure around.
    """
    files: List[str] = []
    for path in sorted(content_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(content_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if any(_is_relative_to(path.resolve(), d) for d in skill_dir_set):
            continue
        if _is_under_excluded_folder(rel.parts) or _is_readme(rel.parts):
            continue
        rel_str = str(rel).replace("\\", "/")
        if rel_str == "SKILL.md" or rel_str.endswith("/SKILL.md"):
            continue
        files.append(rel_str)
    return files


def _ensure_cli_packages(
    info: Any, cli_packages: List[Dict[str, Any]], updated_by: str
) -> None:
    """Auto-register every CLI package a skill declares.

    A ``cli_packages`` entry that carries ``git_repository_url`` and
    ``version`` is registered (upserted) here; one that doesn't is assumed
    to already be registered separately via ``insertUpdateCliPackage``.
    Registration is pure DB bookkeeping — no pip install happens here.
    Installation is lazy: ``ensure_package`` runs on first ``runCommand``
    against this skill (see ``command_executor.py`` /
    ``mutations/skill_management.py::RunCommand``), which also detects and
    reinstalls on git-commit drift, not just a changed ``version`` string.
    A broken/uninstallable dependency therefore no longer fails the deploy —
    it surfaces as a ``runCommand`` error on first use instead.
    """
    if not cli_packages:
        return

    from .cli_package_manager import register_cli_package

    for pkg in cli_packages:
        package_name = pkg.get("package_name") or pkg.get("distribution_name")
        if not package_name:
            continue

        git_repository_url = pkg.get("git_repository_url")
        pkg_version = pkg.get("version")
        if git_repository_url and pkg_version:
            register_cli_package(
                info,
                package_name=package_name,
                git_repository_url=git_repository_url,
                version=pkg_version,
                git_ref=pkg.get("git_ref"),
                description=pkg.get("description"),
                updated_by=updated_by,
            )


# ---------------------------------------------------------------------------
# Deployment orchestration
# ---------------------------------------------------------------------------

def deploy_skill_package(
    info: Any,
    git_repository_url: str,
    version: Optional[str] = None,
    skill_name: Optional[str] = None,
    git_ref: str = "main",
) -> Dict[str, Any]:
    """Deploy a skill package from a git remote.

    Steps:
    1. For a named git_repository_url, cheaply resolve ``git_ref`` to a commit SHA
       (``git ls-remote`` — no clone) and skip the deploy entirely if that
       commit is already registered as this skill's git_repository_url — git is the only
       thing consulted to decide whether a new version exists.
    2. Otherwise, clone the remote at ``git_ref`` and validate each skill
       found.
    3. Compute a content checksum for each skill.
    4. Cache the content locally (``skill_version_cache.store_version``) —
       never installed into the live ``HSK_SKILL_ROOT`` here; that happens
       lazily on the first ``skill()``/``runCommand`` call, on whichever
       instance receives it.
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
        sha = git_client.resolve_ref_sha(git_repository_url, git_ref)
        existing = repo.list(info, name=skill_name, enabled=True)
        for row in getattr(existing, "skill_list", []):
            if (
                row.name == skill_name
                and row.git_repository_url == git_repository_url
                and row.git_ref == git_ref
                and row.resolved_commit == sha
            ):
                logger.info(
                    f"Skill '{skill_name}' already at commit {sha} for "
                    f"{git_repository_url}@{git_ref} — skipping redeploy."
                )
                return {"deployed": [], "failed": [], "skipped": [skill_name]}

    deployed: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []

    work_dir = Path(tempfile.mkdtemp(prefix="hsk_deploy_work_"))
    clone_dir: Optional[Path] = None
    try:
        clone_dir = git_client.clone_at_ref(git_repository_url, git_ref)
        resolved_commit = git_client.current_commit_sha(clone_dir)
        content_root = clone_dir

        # ------------------------------------------------------------------
        # Discover skills — recursive, so a SKILL.md nested any number of
        # subfolders deep (e.g. a monorepo shaped like src/skills/<name>/)
        # is found, not just one directly under the repo root.
        # ------------------------------------------------------------------
        skill_dirs = sorted(
            p
            for p in content_root.rglob("SKILL.md")
            if ".git" not in p.relative_to(content_root).parts
        )
        if not skill_dirs:
            raise ValueError(f"No SKILL.md found in git git_repository_url '{git_repository_url}'.")
        skill_dir_set = {p.parent.resolve() for p in skill_dirs}

        for skill_md in skill_dirs:
            skill_git_repository_url_dir = skill_md.parent
            try:
                validation = _validate_skill_dir(skill_git_repository_url_dir)
                resolved_name = skill_name or validation["name"]
                resolved_version = version or datetime.now(timezone.utc).strftime(
                    "%Y.%m.%d.1"
                )

                # Auto-register any CLI packages this skill declares — pure
                # DB bookkeeping, no install. Install is lazy (see
                # cli_package_manager.ensure_package), so a broken/
                # uninstallable dependency surfaces at first runCommand,
                # not here.
                _ensure_cli_packages(
                    info, validation["cli_packages"], updated_by="system"
                )

                # P9: propose values for whichever of allowed_commands/
                # cli_packages/reference_files this skill's own SKILL.md
                # left entirely absent — never a field the author already
                # populated, even with an empty list. Regenerated every
                # deploy (not conditional on a prior sidecar existing); a
                # redeploy that gets skipped above (unchanged commit)
                # never reaches here at all, so an unchanged skill never
                # re-triggers this. A discovered cli_packages entry is
                # never passed to _ensure_cli_packages above — it only
                # ever names a package already installed on this host
                # (no git_repository_url to register/install from).
                missing_fields = {
                    field
                    for field in ("allowed_commands", "cli_packages", "reference_files")
                    if field not in validation["declared_fields"]
                }
                generated_sections: Dict[str, Any] = {}
                if missing_fields:
                    candidate_files = _list_available_files(skill_git_repository_url_dir)
                    if "reference_files" in missing_fields:
                        candidate_files = sorted(
                            set(candidate_files)
                            | set(_list_repo_wide_files(content_root, skill_dir_set))
                        )
                    generated_sections = generate_missing_sections(
                        logger,
                        resolved_name,
                        validation["description"],
                        validation["body"],
                        validation["cli_packages"],
                        candidate_files,
                        missing_fields,
                    )

                # Whichever reference_files this skill ends up with — the
                # author's own declared list, or what generation just
                # proposed — may name a path that lives elsewhere in this
                # same repository clone (e.g. shared config/docs, or a
                # CLI dependency's own source, at the repo root) rather
                # than inside this skill's own directory. Pull those in
                # now, before checksumming, so skill()'s read path (which
                # only ever looks inside the installed skill directory)
                # finds them with no knowledge of the wider repo layout.
                # Files pulled in from elsewhere are excluded from the
                # checksum below — they're a copy of material this skill
                # doesn't itself author.
                reference_files_declared = "reference_files" in validation["declared_fields"]
                reference_files_to_pull = (
                    validation["reference_files"]
                    if reference_files_declared
                    else generated_sections.get("reference_files", [])
                )
                pulled_excluded: Set[str] = set()
                if reference_files_to_pull:
                    resolved_refs, pulled_excluded = pull_reference_files(
                        logger,
                        content_root,
                        skill_git_repository_url_dir,
                        reference_files_to_pull,
                    )
                    if not reference_files_declared:
                        generated_sections["reference_files"] = resolved_refs

                content_checksum = compute_content_checksum(
                    skill_git_repository_url_dir,
                    Config.SKILL_LOCAL_METADATA_FILE,
                    excluded_relpaths=pulled_excluded,
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
                     
                    git_repository_url=git_repository_url,
                    git_ref=git_ref,
                    resolved_commit=resolved_commit,
                    content_checksum=content_checksum,
                    deployment_status="deployed" if activate else "registered",
                    enabled=True,
                    is_active=activate,
                    registered_at=datetime.now(timezone.utc),
                    updated_by="system",
                )

                # Cache this version's content locally so promote/rollback,
                # and this same instance's own first skill()/runCommand
                # call, can install it instantly with no remote fetch —
                # active or not, deploy itself never writes into the live
                # skill directory. See the module docstring: install is
                # lazy on every instance uniformly.
                skill_version_cache.store_version(
                    skill_root, resolved_name, resolved_version, skill_git_repository_url_dir
                )
                if generated_sections:
                    skill_version_cache.write_generated_sidecar(
                        skill_root,
                        resolved_name,
                        resolved_version,
                        resolved_commit,
                        generated_sections,
                    )
                skill_version_cache.write_checksum_exclusions(
                    skill_root,
                    resolved_name,
                    resolved_version,
                    resolved_commit,
                    sorted(pulled_excluded),
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
                        "git_repository_url": git_repository_url,
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
