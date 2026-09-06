# -*- coding: utf-8 -*-
"""Checksum helpers for local skill content.

``compute_content_checksum`` walks a skill directory and hashes all files
(excluding the metadata file itself) to produce a deterministic digest of the
unpacked skill folder.
"""
from __future__ import print_function

__author__ = "bibow"

import hashlib
import os
from pathlib import Path
from typing import Optional, Set

# Fixed filename for the P9 "auto-generated sections" sidecar
# (allowed_commands/reference_files an author left out of SKILL.md,
# proposed via OpenAI at deploy time). Always excluded from the checksum,
# the same way the local metadata file is — writing or regenerating it
# must never change the skill's registered content_checksum. Unlike the
# metadata filename, this one isn't user-configurable.
GENERATED_SIDECAR_FILENAME = ".hsk-generated.json"

# Fixed filename for the checksum-exclusion bookkeeping sidecar: which
# reference_files paths were pulled in from elsewhere in the repo (see
# reference_pull.pull_reference_files) and so must be excluded from this
# skill's checksum, both at deploy time and by skill()'s own read-time
# recomputation. Kept separate from GENERATED_SIDECAR_FILENAME — that
# file is human/agent-facing ("what got generated for this skill"),
# this one is pure internal plumbing nobody needs to read.
CHECKSUM_EXCLUSIONS_FILENAME = ".hsk-checksum-exclusions.json"


def compute_content_checksum(
    skill_dir: Path,
    metadata_filename: str = ".hsk-skill.json",
    algorithm: str = "sha256",
    excluded_relpaths: Optional[Set[str]] = None,
) -> str:
    """Compute a deterministic checksum of all files under ``skill_dir``.

    The metadata file (``metadata_filename``) and the generated-sections
    sidecar (``GENERATED_SIDECAR_FILENAME``) are excluded by basename (at
    any depth) so that writing either does not alter the checksum of the
    skill they describe.

    ``excluded_relpaths`` additionally excludes specific paths, matched
    exactly (relative to ``skill_dir``, forward-slash separated) rather
    than by basename — used for ``reference_files`` entries pulled in
    from elsewhere in the same repository (see ``reference_pull``): they
    are a copy of material that isn't this skill's own authored content,
    so they must not affect its registered checksum, but excluding them
    by basename would be too broad (a same-named file deliberately
    authored elsewhere in the skill's own directory must still count).
    """
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")

    excluded_filenames = {
        metadata_filename,
        GENERATED_SIDECAR_FILENAME,
        CHECKSUM_EXCLUSIONS_FILENAME,
    }
    excluded_relpaths = excluded_relpaths or set()

    hasher = hashlib.new(algorithm)
    # os.walk is topdown by default, so mutating `dirs` in place (and
    # keeping it sorted) both prunes hidden directories such as .git before
    # descending into them *and* gives deterministic ordering across
    # platforms. Wrapping the walk in sorted() would defeat the pruning: it
    # eagerly consumes the whole tree before this filter ever runs.
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname in excluded_filenames:
                continue
            fpath = Path(root) / fname
            relative = fpath.relative_to(skill_dir)
            relative_str = str(relative).replace("\\", "/")
            if relative_str in excluded_relpaths:
                continue
            # Hash the relative path so that renames change the checksum.
            hasher.update(str(relative).encode("utf-8"))
            with open(fpath, "rb") as fh:
                while True:
                    chunk = fh.read(8192)
                    if not chunk:
                        break
                    hasher.update(chunk)
    return hasher.hexdigest()


__all__ = [
    "compute_content_checksum",
    "GENERATED_SIDECAR_FILENAME",
    "CHECKSUM_EXCLUSIONS_FILENAME",
]
