"""Locations of Claude Code's on-disk state, and how it encodes them."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def claude_home() -> Path:
    """Root of Claude Code's state.

    ``CLAUDE_CONFIG_DIR`` is respected because Claude Code itself honours it.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def projects_dir() -> Path:
    return claude_home() / "projects"


def live_sessions_dir() -> Path:
    """One JSON file per *running* Claude Code process, named by pid."""
    return claude_home() / "sessions"


def cache_dir() -> Path:
    """Our own cache directory, created on demand."""
    path = claude_home() / "sesh"
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_file() -> Path:
    return cache_dir() / "index.json"


_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_project_dir(cwd: str) -> str:
    """Encode a cwd the way Claude Code names its project directories.

    Every character outside ``[A-Za-z0-9]`` becomes ``-``. The transform is
    lossy and NOT invertible (``/a/b-c`` and ``/a/b/c`` both encode to
    ``-a-b-c``), which is exactly why this tool reads ``cwd`` out of the records
    instead of decoding directory names. Kept here only to locate the *likely*
    directory for a given cwd, and to check that a resume target will resolve.
    """
    return _NON_ALNUM.sub("-", cwd)


@dataclass(slots=True)
class TranscriptFile:
    path: str
    #: Session UUID (the filename stem).
    id: str
    #: Encoded project directory name.
    project_dir: str
    size_bytes: int
    mtime: float


def list_transcripts(root: Path | None = None) -> list[TranscriptFile]:
    """Enumerate every session transcript on disk.

    Only the top level of each project directory is considered. Subagent
    transcripts live deeper (``<id>/subagents/``) and are not sessions in their
    own right -- they have no id you could resume.

    Resilient by design: a project directory that vanishes mid-walk, or an
    unreadable file, must not take down the picker.
    """
    base = root if root is not None else projects_dir()
    out: list[TranscriptFile] = []

    try:
        project_dirs = list(os.scandir(base))
    except OSError:
        return out

    for project in project_dirs:
        try:
            if not project.is_dir():
                continue
            entries = list(os.scandir(project.path))
        except OSError:
            continue

        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                stat = entry.stat()
                if not entry.is_file() or stat.st_size == 0:
                    continue
            except OSError:
                continue  # raced with a delete
            out.append(
                TranscriptFile(
                    path=entry.path,
                    id=entry.name[: -len(".jsonl")],
                    project_dir=project.name,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
    return out


def subagent_dir(transcript_path: str) -> str:
    """Sibling directory holding subagent transcripts for a session, if any."""
    return transcript_path[: -len(".jsonl")] if transcript_path.endswith(".jsonl") else transcript_path
