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
from typing import List, Optional

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
    source_type: str,
    source_ref: Optional[str],
    git_ref: Optional[str],
    resolved_commit: Optional[str],
    content_checksum: str,
) -> None:
    """Write ``.hsk-skill.json`` describing the version now installed at ``skill_dir``."""
    metadata = {
        "name": name,
        "version": version,
        "source_type": source_type,
        "source_ref": source_ref,
        "git_ref": git_ref,
        "resolved_commit": resolved_commit,
        "content_checksum": content_checksum,
        "last_refresh_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE
    with open(metadata_file, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)


__all__ = [
    "version_cache_dir",
    "store_version",
    "install_from_cache",
    "remove_version",
    "write_local_metadata",
]
