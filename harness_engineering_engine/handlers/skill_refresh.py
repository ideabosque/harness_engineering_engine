# -*- coding: utf-8 -*-
"""Local skill refresh service.

Compares local metadata against the registration table and S3, downloads
artifacts, unpacks, validates, and atomically replaces skill directories.
"""
from __future__ import print_function

__author__ = "bibow"

import io
import json
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checksums import compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file
from .skill_path import resolve_skill_root


def _s3_client():
    """Return the initialized S3 client."""
    if Config.aws_s3 is None:
        raise RuntimeError("AWS S3 client is not initialized.")
    return Config.aws_s3


def _download_artifact(bucket: str, key: str, version_id: Optional[str] = None) -> bytes:
    """Download a ZIP artifact from S3 and return the raw bytes."""
    client = _s3_client()
    kwargs: Dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = client.get_object(**kwargs)
    return response["Body"].read()


def _unpack_to_temp(zip_bytes: bytes, skill_name: str) -> Path:
    """Extract a ZIP artifact into a temporary directory and return the path."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"hsk_skill_{skill_name}_"))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmpdir)

    # If the ZIP contains a single top-level folder, descend into it.
    entries = list(tmpdir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        # Move contents up one level
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(tmpdir))
        inner.rmdir()

    return tmpdir


def _validate_extracted_dir(tmpdir: Path) -> None:
    """Validate that the extracted directory contains a well-formed SKILL.md."""
    skill_md = tmpdir / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"Extracted package does not contain SKILL.md: {tmpdir}")
    parse_skill_file(skill_md)


def _atomic_replace(src: Path, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src``."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))


def refresh_single_skill(
    logger: logging.Logger,
    active: Dict[str, Any],
    root: Path,
) -> None:
    """Download and install the given active skill version into ``root``.

    Steps:
    1. Download the ZIP artifact from S3.
    2. Unpack to a temp directory.
    3. Validate SKILL.md.
    4. Compute content checksum and compare to registered checksum.
    5. Atomically replace the local skill directory.
    6. Write ``.hsk-skill.json``.
    """
    skill_name = active["name"]
    skill_dir = root / skill_name

    logger.info(f"Refreshing skill '{skill_name}' — downloading {active['s3_key']}")

    zip_bytes = _download_artifact(
        active["s3_bucket"],
        active["s3_key"],
        active.get("s3_version_id"),
    )

    # Verify artifact checksum before unpacking
    artifact_checksum = compute_artifact_checksum_from_bytes(zip_bytes)
    if artifact_checksum != active.get("artifact_checksum"):
        raise ValueError(
            f"Artifact checksum mismatch for skill '{skill_name}': "
            f"expected {active.get('artifact_checksum')}, got {artifact_checksum}"
        )

    tmpdir = _unpack_to_temp(zip_bytes, skill_name)
    try:
        _validate_extracted_dir(tmpdir)

        content_checksum = compute_content_checksum(tmpdir, Config.SKILL_LOCAL_METADATA_FILE)
        if content_checksum != active.get("content_checksum"):
            raise ValueError(
                f"Content checksum mismatch for skill '{skill_name}': "
                f"expected {active.get('content_checksum')}, got {content_checksum}"
            )

        _atomic_replace(tmpdir, skill_dir)

        # Write local metadata
        metadata = {
            "name": skill_name,
            "version": active["version"],
            "source_type": active.get("source_type"),
            "source_ref": active.get("source_ref"),
            "s3_bucket": active["s3_bucket"],
            "s3_key": active["s3_key"],
            "s3_version_id": active.get("s3_version_id"),
            "artifact_checksum": artifact_checksum,
            "content_checksum": content_checksum,
            "last_refresh_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_file = skill_dir / Config.SKILL_LOCAL_METADATA_FILE
        with open(metadata_file, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2)

        logger.info(f"Skill '{skill_name}' refreshed successfully at {skill_dir}")
    finally:
        if tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


def compute_artifact_checksum_from_bytes(data: bytes) -> str:
    """Checksum helper for in-memory bytes."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


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
                "s3_bucket": skill_type.s3_bucket,
                "s3_key": skill_type.s3_key,
                "s3_version_id": skill_type.s3_version_id,
                "artifact_checksum": skill_type.artifact_checksum,
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

