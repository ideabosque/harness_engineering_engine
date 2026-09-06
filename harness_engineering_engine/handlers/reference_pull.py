# -*- coding: utf-8 -*-
"""Pull a skill's ``reference_files`` into its own on-disk directory.

A monorepo often keeps shared reference material (config, design docs, and
even a dependency's own source) at the repository root rather than
duplicated into every skill's own folder, but a skill's own body text
still names it by path (e.g. ``config/design_system.yaml``, or a CLI
package's own module the skill's instructions walk through). ``skill()``'s
read path (``skill_reader._resolve_references``) only ever looks inside
the installed skill directory, by design (a skill must never reach outside
its own on-disk footprint at read time). This module bridges that gap at
deploy/refresh time: whichever ``reference_files`` entries are declared or
generated for a skill get physically copied into that skill's own
directory from wherever else in the same repository clone they actually
live, so the read path needs no knowledge of the wider repo layout.
"""
from __future__ import print_function

__author__ = "bibow"

import logging
import shutil
from pathlib import Path
from typing import List, Set, Tuple


def pull_reference_files(
    logger: logging.Logger,
    content_root: Path,
    skill_dir: Path,
    reference_files: List[str],
) -> Tuple[List[str], Set[str]]:
    """Ensure every entry in ``reference_files`` exists under ``skill_dir``.

    Returns ``(resolved, pulled_from_elsewhere)``:

    - ``resolved`` is the subset that actually exists on disk under
      ``skill_dir`` after this call. An entry that resolves nowhere —
      neither already present in ``skill_dir`` nor found at the same
      relative path under ``content_root`` — is dropped (with a warning)
      rather than left as a dangling reference.
    - ``pulled_from_elsewhere`` is the subset that had to be copied in
      from outside ``skill_dir``. The caller should exclude these from
      the skill's content checksum: they are a copy of material that
      lives elsewhere in the same repository at this same commit, not
      this skill's own authored content, so writing/refreshing them must
      never change the skill's registered checksum (same principle as
      the local metadata file and the generated-sections sidecar).
    """
    resolved: List[str] = []
    pulled: Set[str] = set()
    skill_dir_resolved = skill_dir.resolve()
    content_root_resolved = content_root.resolve()

    for rel in reference_files:
        if rel == "SKILL.md":
            continue

        dest = skill_dir / rel
        try:
            dest.resolve().relative_to(skill_dir_resolved)
        except ValueError:
            logger.warning(
                f"reference_files entry '{rel}' escapes the skill directory — skipped."
            )
            continue

        if dest.is_file():
            resolved.append(rel)
            continue

        src = content_root / rel
        try:
            src.resolve().relative_to(content_root_resolved)
        except ValueError:
            logger.warning(
                f"reference_files entry '{rel}' escapes the repository root — skipped."
            )
            continue

        if not src.is_file():
            logger.warning(
                f"reference_files entry '{rel}' was not found in the skill's own "
                f"folder or elsewhere in the repository — skipped."
            )
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        resolved.append(rel)
        pulled.add(rel)

    return resolved, pulled


__all__ = ["pull_reference_files"]
