# -*- coding: utf-8 -*-
"""Skill deployment service — GitHub/ZIP intake, packaging, S3 upload, registration."""
from __future__ import print_function

__author__ = "bibow"

import logging
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models.repositories import get_repo
from .checksums import compute_artifact_checksum, compute_content_checksum
from .config import Config
from .skill_frontmatter import parse_skill_file


# ---------------------------------------------------------------------------
# GitHub source handling
# ---------------------------------------------------------------------------

def _download_github_repo(github_url: str, ref: str = "main") -> Path:
    """Clone a public GitHub repository (shallow) to a temp directory."""
    tmpdir = Path(tempfile.mkdtemp(prefix="hsk_deploy_"))
    clone_url = github_url.rstrip("/")
    if not clone_url.endswith(".git"):
        clone_url = f"{clone_url}.git"

    result = subprocess.run(
        [
            "git",
            "clone",
            "--depth=1",
            f"--branch={ref}",
            clone_url,
            str(tmpdir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(
            f"Failed to clone {github_url}@{ref}: {result.stderr.strip()}"
        )
    return tmpdir


# ---------------------------------------------------------------------------
# ZIP source handling
# ---------------------------------------------------------------------------

def _validate_zip_layout(zip_path: Path) -> List[Path]:
    """Return a list of skill directories inside the ZIP.

    A valid ZIP contains one or more folders, each with a ``SKILL.md``.
    """
    skill_dirs: List[Path] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        # Detect root-level SKILL.md (single-skill ZIP)
        if any(n == "SKILL.md" for n in names):
            skill_dirs.append(zip_path.parent)
        else:
            # Multi-skill ZIP — look for */SKILL.md
            seen = set()
            for n in names:
                parts = n.split("/")
                if parts[-1] == "SKILL.md" and len(parts) == 2:
                    seen.add(parts[0])
            for folder in sorted(seen):
                skill_dirs.append(zip_path.parent / folder)
    return skill_dirs


def _normalize_zip(zip_path: Path, work_dir: Path) -> Path:
    """Extract and normalize a ZIP into a working directory of skill folders."""
    extract_dir = work_dir / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    # If the ZIP contains a single top-level folder, descend into it
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(extract_dir))
        inner.rmdir()

    return extract_dir


# ---------------------------------------------------------------------------
# Package validation and checksums
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
        "allowed_commands": parsed.frontmatter.allowed_commands,
        "cli_packages": parsed.frontmatter.cli_packages,
    }


# ---------------------------------------------------------------------------
# Deployment orchestration
# ---------------------------------------------------------------------------

def deploy_skill_package(
    info: Any,
    source: str,
    source_type: Optional[str] = None,
    version: Optional[str] = None,
    skill_name: Optional[str] = None,
    git_ref: str = "main",
) -> Dict[str, Any]:
    """Deploy a skill package from a GitHub URL or ZIP file.

    Steps:
    1. Resolve source (GitHub clone or uploaded ZIP).
    2. Normalize layout and validate each skill.
    3. Compute checksums.
    4. Upload ZIP artifact(s) to S3.
    5. Register each skill version in the database.
    """
    logger = info.context.get("logger") or logging.getLogger(__name__)
    partition_key = info.context.get("partition_key")

    if not partition_key:
        raise ValueError("partition_key is required in context.")

    if not Config.SKILL_ARTIFACT_BUCKET:
        raise ValueError("HSK_SKILL_ARTIFACT_BUCKET is not configured.")

    # Normalize source_type
    if source_type is None:
        source_type = "github" if source.startswith("http") else "zip"
    source_type = source_type.lower()
    if source_type not in ("github", "zip"):
        raise ValueError(f"Unsupported source_type: {source_type}")

    repo = get_repo("skill")
    deployed: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []

    work_dir = Path(tempfile.mkdtemp(prefix="hsk_deploy_work_"))
    try:
        # ------------------------------------------------------------------
        # Obtain source
        # ------------------------------------------------------------------
        if source_type == "github":
            repo_dir = _download_github_repo(source, git_ref)
            skill_root = repo_dir
        else:
            # ZIP file path
            zip_path = Path(source)
            if not zip_path.is_file():
                raise FileNotFoundError(f"ZIP file not found: {zip_path}")
            skill_root = _normalize_zip(zip_path, work_dir)

        # ------------------------------------------------------------------
        # Discover skills
        # ------------------------------------------------------------------
        skill_dirs = sorted(skill_root.glob("*/SKILL.md"))
        if not skill_dirs:
            # Try the root itself as a single-skill package
            if (skill_root / "SKILL.md").is_file():
                skill_dirs = [skill_root / "SKILL.md"]
            else:
                raise ValueError(
                    f"No SKILL.md found in {source_type} source '{source}'."
                )

        for skill_dir in skill_dirs:
            try:
                validation = _validate_skill_dir(skill_dir.parent)
                resolved_name = skill_name or validation["name"]
                resolved_version = version or datetime.now(timezone.utc).strftime("%Y.%m.%d.1")

                # Compute checksums
                content_checksum = compute_content_checksum(
                    skill_dir.parent, Config.SKILL_LOCAL_METADATA_FILE
                )

                # Create a ZIP artifact for this skill
                artifact_path = work_dir / f"{resolved_name}-{resolved_version}.zip"
                with zipfile.ZipFile(artifact_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fpath in sorted(skill_dir.parent.rglob("*")):
                        zf.write(fpath, fpath.relative_to(skill_dir.parent))

                artifact_checksum = compute_artifact_checksum(artifact_path)

                # Upload to S3
                s3_key = (
                    f"{Config.SKILL_ARTIFACT_PREFIX}"
                    f"{resolved_name}/{resolved_version}/{artifact_checksum}.zip"
                )
                client = Config.aws_s3
                if client is None:
                    raise RuntimeError("AWS S3 client is not initialized.")

                with open(artifact_path, "rb") as fh:
                    put_kwargs: Dict[str, Any] = {
                        "Bucket": Config.SKILL_ARTIFACT_BUCKET,
                        "Key": s3_key,
                        "Body": fh.read(),
                    }
                    response = client.put_object(**put_kwargs)
                s3_version_id = response.get("VersionId")

                logger.info(
                    f"Deployed skill '{resolved_name}' v{resolved_version} to s3://"
                    f"{Config.SKILL_ARTIFACT_BUCKET}/{s3_key} "
                    f"(version_id={s3_version_id})"
                )

                # Register in database. A skill's first-ever version is
                # activated automatically so it is immediately retrievable;
                # subsequent versions land inactive and require an explicit
                # promoteSkillVersion so a deploy never silently replaces
                # what agents are currently served.
                import uuid
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
                    source_type=source_type,
                    source_ref=source,
                    s3_bucket=Config.SKILL_ARTIFACT_BUCKET,
                    s3_key=s3_key,
                    s3_version_id=s3_version_id,
                    artifact_checksum=artifact_checksum,
                    content_checksum=content_checksum,
                    deployment_status="deployed" if activate else "uploaded",
                    enabled=True,
                    is_active=activate,
                    registered_at=datetime.now(timezone.utc),
                    updated_by="system",
                )

                deployed.append(
                    {
                        "skill_uuid": skill_uuid,
                        "name": resolved_name,
                        "version": resolved_version,
                        "s3_bucket": Config.SKILL_ARTIFACT_BUCKET,
                        "s3_key": s3_key,
                        "s3_version_id": s3_version_id,
                        "artifact_checksum": artifact_checksum,
                        "is_active": activate,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to deploy skill from {skill_dir}: {e}")
                failed.append({"skill": str(skill_dir), "error": str(e)})

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "deployed": deployed,
        "failed": failed,
    }


__all__ = ["deploy_skill_package"]

