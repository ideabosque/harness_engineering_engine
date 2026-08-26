# -*- coding: utf-8 -*-
"""Checksum helpers for skill artifacts and local content.

``compute_content_checksum`` walks a skill directory and hashes all files
(excluding the metadata file itself) to produce a deterministic digest of the
unpacked skill folder.  ``compute_artifact_checksum`` hashes a ZIP artifact.
"""
from __future__ import print_function

__author__ = "bibow"

import hashlib
import os
from pathlib import Path


def compute_content_checksum(
    skill_dir: Path,
    metadata_filename: str = ".hsk-skill.json",
    algorithm: str = "sha256",
) -> str:
    """Compute a deterministic checksum of all files under ``skill_dir``.

    The metadata file (``metadata_filename``) is excluded so that writing it
    does not alter the checksum of the skill it describes.
    """
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")

    hasher = hashlib.new(algorithm)
    # Sort paths for deterministic ordering across platforms.
    for root, dirs, files in sorted(os.walk(skill_dir)):
        # Skip hidden directories such as __pycache__ or .git
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname == metadata_filename:
                continue
            fpath = Path(root) / fname
            relative = fpath.relative_to(skill_dir)
            # Hash the relative path so that renames change the checksum.
            hasher.update(str(relative).encode("utf-8"))
            with open(fpath, "rb") as fh:
                while True:
                    chunk = fh.read(8192)
                    if not chunk:
                        break
                    hasher.update(chunk)
    return hasher.hexdigest()


def compute_artifact_checksum(artifact_path: Path, algorithm: str = "sha256") -> str:
    """Compute a checksum of a ZIP artifact (or any file)."""
    if not artifact_path.is_file():
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    hasher = hashlib.new(algorithm)
    with open(artifact_path, "rb") as fh:
        while True:
            chunk = fh.read(8192)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_bytes_checksum(data: bytes, algorithm: str = "sha256") -> str:
    """Compute a checksum of an in-memory bytes object."""
    hasher = hashlib.new(algorithm)
    hasher.update(data)
    return hasher.hexdigest()


__all__ = [
    "compute_content_checksum",
    "compute_artifact_checksum",
    "compute_bytes_checksum",
]

