"""Git lookups that never shell out.

The picker resolves a repo root for every distinct cwd it has ever seen, which
can be hundreds of paths. Spawning ``git rev-parse`` for each would cost tens of
milliseconds apiece and dominate startup, so we walk for a ``.git`` entry
ourselves and read ``.git/HEAD`` directly. Both are stable, documented on-disk
formats.

Nothing here requires git to be installed.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_repo_root_cache: dict[str, str | None] = {}
_main_root_cache: dict[str, str | None] = {}
_branch_cache: dict[str, tuple[str | None, float]] = {}

#: How long a HEAD read stays fresh. Branches change while the picker is open.
_BRANCH_TTL_SECONDS = 2.0

_GITDIR_RE = re.compile(r"^gitdir:\s*(.+)$", re.MULTILINE)
_HEAD_REF_RE = re.compile(r"^ref:\s*refs/heads/(.+)$")
_WORKTREE_RE = re.compile(r"^(.*)/\.git/worktrees/[^/]+/?$")


def repo_root(directory: str) -> str | None:
    """Find the repository root containing ``directory``, or None.

    Handles worktrees and submodules, where ``.git`` is a *file* containing a
    ``gitdir:`` pointer rather than a directory.
    """
    cached = _repo_root_cache.get(directory, ...)  # type: ignore[arg-type]
    if cached is not ...:
        return cached  # type: ignore[return-value]

    result: str | None = None
    current = Path(directory).resolve()
    while True:
        if (current / ".git").exists():
            result = str(current)
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    _repo_root_cache[directory] = result
    return result


def _git_dir(root: str) -> str | None:
    """Resolve the real ``.git`` directory for a repo root (worktree-aware)."""
    dot_git = Path(root) / ".git"
    try:
        if dot_git.is_dir():
            return str(dot_git)
        if dot_git.is_file():
            match = _GITDIR_RE.search(dot_git.read_text(encoding="utf8").strip())
            if not match:
                return None
            target = match.group(1).strip()
            return target if target.startswith("/") else str((Path(root) / target).resolve())
    except OSError:
        pass
    return None


def current_branch(directory: str) -> str | None:
    """Current branch of the repo containing ``directory``.

    Returns None when detached, bare, or not a repo. A detached HEAD is reported
    as None rather than the raw sha because Claude Code itself records the
    string "HEAD" in that situation, and conflating the two would make branch
    filters behave unpredictably.
    """
    root = repo_root(directory)
    if root is None:
        return None

    now = time.monotonic()
    cached = _branch_cache.get(root)
    if cached is not None and now - cached[1] < _BRANCH_TTL_SECONDS:
        return cached[0]

    branch: str | None = None
    git_dir = _git_dir(root)
    if git_dir:
        try:
            head = (Path(git_dir) / "HEAD").read_text(encoding="utf8").strip()
            match = _HEAD_REF_RE.match(head)
            if match:
                branch = match.group(1)
        except OSError:
            pass  # unreadable HEAD -- treat as detached

    _branch_cache[root] = (branch, now)
    return branch


def main_repo_root(directory: str) -> str | None:
    """The *primary* repository root, collapsing linked worktrees onto it.

    This matters for the branch workflow this tool is built around: someone who
    keeps a worktree per branch has sessions scattered across sibling
    directories that are, to them, obviously one project. Keying scope on the
    literal repo root would split those into unrelated groups. A linked
    worktree's gitdir is ``<main>/.git/worktrees/<name>``, so the main root is
    two levels up from the worktrees directory.
    """
    root = repo_root(directory)
    if root is None:
        return None

    cached = _main_root_cache.get(root, ...)  # type: ignore[arg-type]
    if cached is not ...:
        return cached  # type: ignore[return-value]

    result = root
    git_dir = _git_dir(root)
    if git_dir:
        match = _WORKTREE_RE.match(git_dir)
        if match:
            result = match.group(1)

    _main_root_cache[root] = result
    return result


# The branch picker deliberately does *not* list the repo's current branches.
# It lists the branches that actually have sessions (see ``branches_in``), which
# is both a shorter list and a more useful one -- it includes branches you have
# since deleted or merged, and those are exactly the ones whose sessions are
# hardest to find any other way.


def clear_caches() -> None:
    """Clear memoised lookups (used by tests and after a manual refresh)."""
    _repo_root_cache.clear()
    _main_root_cache.clear()
    _branch_cache.clear()
