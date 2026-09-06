# -*- coding: utf-8 -*-
"""Local per-version content cache.

Every deployed version's validated content is cached under
``HSK_SKILL_ROOT/.hsk-versions/<name>/<version>/`` on the deploying host so
that ``promoteSkillVersion``/``rollbackSkill`` can swap the live skill
directory instantly, with no remote fetch. A host that never cached a given
version still has a fallback: the on-demand refresh in ``skill_reader.py``
fetches straight from git, pinned to the registered commit.
"""
from __future__ import print_function

__author__ = "bibow"

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checksums import CHECKSUM_EXCLUSIONS_FILENAME, GENERATED_SIDECAR_FILENAME
from .config import Config


def _ignore_hidden(_dir: str, names: List[str]) -> List[str]:
    """``shutil.copytree`` ignore hook — drop dotfiles/dotdirs such as ``.git``."""
    return [n for n in names if n.startswith(".")]


def version_cache_dir(skill_root: Path, name: str, version: str) -> Path:
    return skill_root / ".hsk-versions" / name / version


def store_version(skill_root: Path, name: str, version: str, content_dir: Path) -> None:
    """Copy ``content_dir`` into the version cache, overwriting any prior copy."""
    dest = version_cache_dir(skill_root, name, version)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(content_dir, dest, ignore=_ignore_hidden)


def install_from_cache(skill_root: Path, name: str, version: str) -> bool:
    """Atomically swap the live skill directory for a cached version.

    Returns ``False`` (no-op) if that version was never cached on this host.
    """
    src = version_cache_dir(skill_root, name, version)
    if not src.is_dir():
        return False

    dest = skill_root / name
    swap = skill_root / f".{name}.swap"
    if swap.exists():
        shutil.rmtree(swap)
    shutil.copytree(src, swap, ignore=_ignore_hidden)

    # The generated-sections sidecar (P9) and the checksum-exclusions
    # sidecar are both dotfiles, so the copytree above already dropped
    # them via _ignore_hidden — the same filter that correctly excludes
    # .git from the git-sourced content. Carry them over explicitly here,
    # the same way write_local_metadata is a separate write rather than
    # part of that copy.
    generated_src = src / GENERATED_SIDECAR_FILENAME
    if generated_src.is_file():
        shutil.copy2(generated_src, swap / GENERATED_SIDECAR_FILENAME)

    exclusions_src = src / CHECKSUM_EXCLUSIONS_FILENAME
    if exclusions_src.is_file():
        shutil.copy2(exclusions_src, swap / CHECKSUM_EXCLUSIONS_FILENAME)

    if dest.exists():
        shutil.rmtree(dest)
    swap.rename(dest)
    return True


def remove_version(skill_root: Path, name: str, version: str) -> None:
    """Delete a version's cached content (used when pruning old versions)."""
    shutil.rmtree(version_cache_dir(skill_root, name, version), ignore_errors=True)


def write_local_metadata(
    skill_dir: Path,
    name: str,
    version: str,
    git_repository_url: Optional[str],
    git_ref: Optional[str],
    resolved_commit: Optional[str],
    content_checksum: str,
) -> None:
    """Write ``.hsk-skill.json`` describing the version now installed at ``skill_dir``."""
    metadata = {
        "name": name,
        "version": version,
        "git_repository_url": git_repository_url,
        "git_ref": git_ref,
        "resolved_commit": resolved_commit,
        "content_checksum": content_checksum,
        "last_refresh_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE
    with open(metadata_file, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)


def write_generated_sidecar(
    skill_root: Path,
    name: str,
    version: str,
    resolved_commit: Optional[str],
    generated: Dict[str, Any],
) -> None:
    """Write the P9 sidecar into this version's cache directory.

    ``generated`` should contain only the keys actually produced by
    ``handlers.section_generator.generate_missing_sections`` — never a
    field the skill's own SKILL.md already declared. ``resolved_commit``
    lets a reader detect and ignore a sidecar left over from a different
    commit (should not happen given P9 regenerates every deploy, but this
    makes that assumption enforced rather than merely assumed).
    """
    if not generated:
        return
    dest = version_cache_dir(skill_root, name, version)
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "version": version,
        "resolved_commit": resolved_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **generated,
    }
    with open(dest / GENERATED_SIDECAR_FILENAME, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def read_generated_sidecar(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the P9 sidecar from an installed skill directory, if present."""
    sidecar = skill_dir / GENERATED_SIDECAR_FILENAME
    if not sidecar.is_file():
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_checksum_exclusions(
    skill_root: Path,
    name: str,
    version: str,
    resolved_commit: Optional[str],
    excluded_relpaths: List[str],
) -> None:
    """Record, into this version's cache directory, which reference_files
    paths were pulled in from elsewhere in the repo (see
    ``reference_pull.pull_reference_files``) and so must be excluded from
    this skill's content checksum.

    Deliberately a separate file from ``write_generated_sidecar``: that
    one is what got generated *for* the skill (human/agent-facing);
    this one is pure internal bookkeeping ``skill()``'s own read-time
    checksum recomputation needs, nothing a skill author or agent needs
    to see. Writes nothing when ``excluded_relpaths`` is empty — most
    skills pull in nothing at all.
    """
    if not excluded_relpaths:
        return
    dest = version_cache_dir(skill_root, name, version)
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "resolved_commit": resolved_commit,
        "excluded_relpaths": sorted(excluded_relpaths),
    }
    with open(dest / CHECKSUM_EXCLUSIONS_FILENAME, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def read_checksum_exclusions(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the checksum-exclusions sidecar from an installed skill
    directory, if present."""
    path = skill_dir / CHECKSUM_EXCLUSIONS_FILENAME
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "version_cache_dir",
    "store_version",
    "install_from_cache",
    "remove_version",
    "write_local_metadata",
    "write_generated_sidecar",
    "read_generated_sidecar",
    "write_checksum_exclusions",
    "read_checksum_exclusions",
]
