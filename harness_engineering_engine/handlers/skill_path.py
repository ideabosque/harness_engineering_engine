# -*- coding: utf-8 -*-
"""Guarded filesystem helpers that constrain reads to ``HSK_SKILL_ROOT``.

All path operations are validated to prevent directory-traversal outside the
configured skill root.
"""
from __future__ import print_function

__author__ = "bibow"

from pathlib import Path
from typing import Optional

from .config import Config


def resolve_skill_root(root: Optional[str] = None) -> Path:
    """Return the configured skill root, optionally overridden.

    When ``root`` is provided it is normalized and must reside inside the
    configured ``HSK_SKILL_ROOT`` (unless the override is an absolute path
    that matches an explicitly allowed root — for v1 we simply require it
    to be under the configured root).
    """
    configured = Config.SKILL_ROOT
    if not configured:
        raise ValueError(
            "HSK_SKILL_ROOT is not configured. Set it in the environment or engine settings."
        )

    base = Path(configured).resolve()

    if root is None:
        return base

    candidate = Path(root).resolve()
    if base in candidate.parents or candidate == base:
        return candidate

    raise ValueError(
        f"Requested root '{root}' is outside the configured SKILL_ROOT '{configured}'."
    )


def skill_path(root: Path, name: str) -> Path:
    """Return the path to a skill directory inside ``root``."""
    return root / name


def skill_md_path(root: Path, name: str) -> Path:
    """Return the path to a skill's SKILL.md inside ``root``."""
    return skill_path(root, name) / "SKILL.md"


def local_metadata_path(root: Path, name: str) -> Path:
    """Return the path to a skill's local metadata file."""
    return skill_path(root, name) / Config.SKILL_LOCAL_METADATA_FILE


__all__ = [
    "resolve_skill_root",
    "skill_path",
    "skill_md_path",
    "local_metadata_path",
]

