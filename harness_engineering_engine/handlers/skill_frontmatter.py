# -*- coding: utf-8 -*-
"""Parse ``SKILL.md`` YAML frontmatter and body.

Skill files use a ``---`` delimited YAML block followed by a markdown
instruction body.  Only ``name``, ``description``, and ``allowed_commands``
are required/optional per the v1 spec.
"""
from __future__ import print_function

__author__ = "bibow"

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

# Regex that matches a YAML frontmatter block at the very start of the file.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n?---\s*\n?(.*)", re.DOTALL)


@dataclass
class SkillFrontmatter:
    """Structured frontmatter data extracted from a SKILL.md file."""

    name: str = ""
    description: str = ""
    allowed_commands: List[Dict[str, Any]] = field(default_factory=list)
    cli_packages: List[Dict[str, Any]] = field(default_factory=list)
    reference_files: List[str] = field(default_factory=list)


@dataclass
class ParsedSkill:
    """Result of parsing a SKILL.md file."""

    frontmatter: SkillFrontmatter
    body: str
    raw_frontmatter: Dict[str, Any] = field(default_factory=dict)


def parse_frontmatter(text: str) -> ParsedSkill:
    """Split a SKILL.md string into YAML frontmatter and markdown body.

    Raises ``ValueError`` when the frontmatter block is missing, malformed, or
    required fields are absent.
    """
    # A UTF-8 BOM (U+FEFF) at the very start of the file is common from
    # editors/tools that default to "UTF-8 with BOM" — strip it before
    # matching, since the frontmatter regex requires the file to start
    # with "---" literally. Real-world case: every SKILL.md in a repo
    # saved with a BOM failed with "does not contain a YAML frontmatter
    # block" even though the file visually looked correct.
    if text.startswith("﻿"):
        text = text[1:]

    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("SKILL.md does not contain a YAML frontmatter block.")

    fm_text, body = match.group(1), match.group(2)

    try:
        raw = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML frontmatter: {e}") from e

    if raw is None:
        raw = {}

    if not isinstance(raw, dict):
        raise ValueError("YAML frontmatter must be a mapping, not a scalar/list.")

    # Validate required fields
    name = raw.get("name", "").strip()
    description = raw.get("description", "").strip()
    if not name:
        raise ValueError("Frontmatter field 'name' is required.")
    if not description:
        raise ValueError("Frontmatter field 'description' is required.")

    allowed_commands = raw.get("allowed_commands", [])
    if not isinstance(allowed_commands, list):
        raise ValueError("Frontmatter field 'allowed_commands' must be a list.")

    cli_packages = raw.get("cli_packages", [])
    if not isinstance(cli_packages, list):
        raise ValueError("Frontmatter field 'cli_packages' must be a list.")

    reference_files = raw.get("reference_files", [])
    if not isinstance(reference_files, list):
        raise ValueError("Frontmatter field 'reference_files' must be a list.")

    frontmatter = SkillFrontmatter(
        name=name,
        description=description,
        allowed_commands=allowed_commands,
        cli_packages=cli_packages,
        reference_files=reference_files,
    )

    return ParsedSkill(
        frontmatter=frontmatter,
        body=body.strip(),
        raw_frontmatter=raw,
    )


def parse_skill_file(path: Path) -> ParsedSkill:
    """Read a SKILL.md file from disk and parse it."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_frontmatter(fh.read())


__all__ = ["SkillFrontmatter", "ParsedSkill", "parse_frontmatter", "parse_skill_file"]

