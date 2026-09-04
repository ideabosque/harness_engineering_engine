# -*- coding: utf-8 -*-
"""Git-based source access for skill deployment and refresh.

Skills are sourced directly from a git remote — there is no intermediate
artifact store. HTTPS remotes use whatever credential helper is already
configured on the host; SSH remotes (``git@host:org/repo.git`` or
``ssh://...``) use the system's default identity (``~/.ssh/id_*`` via the
user's ssh-agent/``~/.ssh/config``) unless ``HSK_GIT_SSH_KEY_PATH`` names an
alternate private key, in which case ``GIT_SSH_COMMAND`` is forced to use it.
"""
from __future__ import print_function

__author__ = "bibow"

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config


def _git_env() -> Dict[str, str]:
    """Build the subprocess environment for git commands.

    Leaves SSH identity resolution to the system default unless an alternate
    key is configured — git only consults ``GIT_SSH_COMMAND`` for SSH
    transport, so setting it is a no-op for HTTPS remotes.
    """
    env = os.environ.copy()
    key_path = Config.GIT_SSH_KEY_PATH
    if key_path:
        env["GIT_SSH_COMMAND"] = f'ssh -i "{key_path}" -o IdentitiesOnly=yes'
    return env


def _run_git(args: List[str], cwd: Optional[str] = None, check: bool = True):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def resolve_ref_sha(remote: str, ref: str) -> str:
    """Resolve ``ref`` (branch or tag) on ``remote`` to a commit SHA.

    Uses ``git ls-remote`` so the current version can be checked without a
    full clone — this is the only network call a "is there a new version"
    check needs to make.
    """
    result = _run_git(["ls-remote", remote, ref])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"Ref '{ref}' not found on remote '{remote}'.")
    return lines[0].split()[0]


def clone_at_ref(remote: str, ref: str) -> Path:
    """Shallow-clone ``remote`` at ``ref`` (branch/tag) into a new temp directory.

    Returns the clone directory; caller is responsible for cleanup.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="hsk_git_"))
    result = subprocess.run(
        ["git", "clone", "--depth=1", f"--branch={ref}", remote, str(tmpdir)],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Failed to clone {remote}@{ref}: {result.stderr.strip()}")
    return tmpdir


def clone_at_commit(remote: str, ref: str, commit_sha: str) -> Path:
    """Fetch ``remote`` pinned to ``commit_sha``, falling back to ``ref``.

    Tries a direct shallow fetch of the exact commit first (supported by most
    modern git servers); if the remote rejects fetching by SHA, falls back to
    a shallow clone of ``ref`` and lets the caller's content-checksum check
    catch the case where the branch has moved past the registered commit.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="hsk_git_"))
    try:
        _run_git(["init", "-q", str(tmpdir)])
        _run_git(["remote", "add", "origin", remote], cwd=str(tmpdir))
        fetch = subprocess.run(
            ["git", "fetch", "--depth=1", "origin", commit_sha],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if fetch.returncode == 0:
            _run_git(["checkout", "-q", "FETCH_HEAD"], cwd=str(tmpdir))
            return tmpdir

        shutil.rmtree(tmpdir, ignore_errors=True)
        return clone_at_ref(remote, ref)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def current_commit_sha(repo_dir: Path) -> str:
    """Return the resolved HEAD commit SHA of a cloned repo directory."""
    result = _run_git(["rev-parse", "HEAD"], cwd=str(repo_dir))
    return result.stdout.strip()


__all__ = [
    "resolve_ref_sha",
    "clone_at_ref",
    "clone_at_commit",
    "current_commit_sha",
]
