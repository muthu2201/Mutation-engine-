"""Write a set of files as a commit on a git branch without touching the checkout.

Used for Colloid's data-only branches (the mutation data lake, materialised stacks). Only
plumbing is involved:

``hash-object -w`` writes blobs; a *temporary* index (``GIT_INDEX_FILE``), optionally loaded
from the branch tip, is updated with ``update-index --index-info`` and turned into a tree with
``write-tree``; ``commit-tree`` makes the commit (no parent for a new branch, so a data branch
shares no history with the code); ``update-ref <ref> <new> <old>`` moves the branch with
compare-and-swap. The working tree, the real index, HEAD and every other ref stay as they were.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

ZERO_OID = "0" * 40


class RefMoved(RuntimeError):
    """The branch moved between reading its tip and updating it (another writer won)."""


def git(repo: Path, *args: str, data: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
    res = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, env={**os.environ, **(env or {})}, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {res.stderr.decode(errors='replace').strip()[:400]}")
    return res.stdout


def branch_tip(repo: Path, branch: str) -> str | None:
    res = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], capture_output=True, check=False)
    return res.stdout.decode().strip() or None


def _identity(repo: Path) -> dict[str, str]:
    res = subprocess.run(["git", "-C", str(repo), "config", "user.email"], capture_output=True, check=False)
    if res.stdout.strip():
        return {}
    return {"GIT_AUTHOR_NAME": "Colloid", "GIT_AUTHOR_EMAIL": "colloid@colloid.invalid",
            "GIT_COMMITTER_NAME": "Colloid", "GIT_COMMITTER_EMAIL": "colloid@colloid.invalid"}


def commit_files(repo: Path, branch: str, files: Mapping[str, bytes], message: str, *, expected_tip: str | None,
                 replace_tree: bool = False) -> str:
    """Commit ``files`` (path -> content) onto ``branch`` and return the new commit id.

    ``expected_tip`` is the tip the caller based its change on (None = the branch must not
    exist yet); if the branch is elsewhere, :class:`RefMoved` is raised and nothing changes.
    With ``replace_tree`` the commit's tree is exactly ``files``; otherwise ``files`` are
    added to / overwrite the tip's tree."""
    repo = Path(repo).resolve()
    ref = f"refs/heads/{branch}"
    tip = branch_tip(repo, branch)
    if tip != expected_tip:
        raise RefMoved(f"{branch} is at {tip or 'nothing'}, expected {expected_tip or 'nothing'}")
    lines = []
    for path, data in sorted(files.items()):
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"refusing to write outside the tree: {path}")
        oid = git(repo, "hash-object", "-w", "--stdin", data=data).decode().strip()
        lines.append(f"100644 {oid}\t{path}\n")
    with tempfile.TemporaryDirectory(prefix="colloid-git-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if tip and not replace_tree:
            git(repo, "read-tree", tip, env=env)
        git(repo, "update-index", "--add", "--index-info", data="".join(lines).encode(), env=env)
        tree = git(repo, "write-tree", env=env).decode().strip()
    parent = ["-p", tip] if tip else []
    commit = git(repo, "commit-tree", tree, *parent, "-m", message, env=_identity(repo)).decode().strip()
    res = subprocess.run(["git", "-C", str(repo), "update-ref", "-m", f"colloid: {message.splitlines()[0][:60]}", ref, commit, tip or ZERO_OID],
                         capture_output=True, check=False)
    if res.returncode != 0:
        raise RefMoved(f"{branch} moved during the commit: {res.stderr.decode(errors='replace').strip()[:200]}")
    return commit


def push(repo: Path, branch: str, remote: str = "origin") -> str:
    ref = f"refs/heads/{branch}"
    git(Path(repo), "push", "-u", remote, f"{ref}:{ref}")
    return f"pushed {branch} to {remote}"
