"""Session actions: trash, restore, clipboard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import cache_dir, subagent_dir
from .types import SessionMeta


def trash_dir() -> Path:
    return cache_dir() / "trash"


@dataclass(slots=True)
class TrashEntry:
    id: str
    deleted_at: float
    #: Directory holding the moved files.
    directory: str
    #: Original transcript path, for restore.
    original_path: str


class ActionError(Exception):
    """A user-facing failure that should be shown in the status bar."""


def trash_session(session: SessionMeta) -> TrashEntry:
    """Move a session's files to a trash directory instead of unlinking them.

    Claude Code's own ``claude project purge`` deletes *all* state for a
    project, which is far too blunt when the goal is "get this one dead session
    out of my list". Deleting a single transcript is not an officially supported
    operation, so doing it reversibly is the responsible middle ground: the row
    disappears immediately, and nothing is actually destroyed until the user
    says so.
    """
    stamp = time.time()
    destination = trash_dir() / f"{int(stamp * 1000)}-{session.id}"

    try:
        destination.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(session.file):
            raise ActionError("Transcript file is already gone.")
        shutil.move(session.file, destination / os.path.basename(session.file))

        # Sibling directory holds subagent transcripts and cached tool results.
        sibling = subagent_dir(session.file)
        if os.path.exists(sibling):
            shutil.move(sibling, destination / os.path.basename(sibling))

        (destination / "sesh-trash.json").write_text(
            json.dumps(
                {
                    "id": session.id,
                    "deleted_at": stamp,
                    "original_path": session.file,
                    "project_dir": session.project_dir,
                },
                indent=2,
            ),
            encoding="utf8",
        )
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"Could not move session to trash: {err}") from err

    return TrashEntry(
        id=session.id,
        deleted_at=stamp,
        directory=str(destination),
        original_path=session.file,
    )


def restore_trash(entry: TrashEntry) -> None:
    """Undo a trash operation."""
    try:
        names = [n for n in os.listdir(entry.directory) if n != "sesh-trash.json"]
        for name in names:
            source = os.path.join(entry.directory, name)
            if name.endswith(".jsonl"):
                target = entry.original_path
            else:
                target = subagent_dir(entry.original_path)
            if os.path.exists(target):
                raise ActionError(f"Cannot restore: {target} already exists.")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
        shutil.rmtree(entry.directory, ignore_errors=True)
    except ActionError:
        raise
    except OSError as err:
        raise ActionError(f"Could not restore session: {err}") from err


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard.

    Best-effort and silent on failure -- a missing clipboard utility should
    never interrupt the picker.
    """
    if sys.platform == "darwin":
        candidates: list[list[str]] = [["pbcopy"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]

    for command in candidates:
        try:
            result = subprocess.run(
                command, input=text, text=True, timeout=2, check=False, capture_output=True
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
