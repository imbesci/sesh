"""Running Claude Code processes.

Claude Code writes one JSON file per process into ``~/.claude/sessions``, named
by pid. The files are not reliably cleaned up when a process dies (a crash or
SIGKILL leaves one behind), so a stale entry would otherwise mark a finished
session as "running" forever.

Liveness is therefore checked twice: signal 0 for existence, and the command
name for identity, because pids get recycled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from .paths import live_sessions_dir
from .types import LiveSession

_PS_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
_CLAUDE_COMMAND = re.compile(r"(^|/)claude(-code)?$|(^|/)(bun|node)$")


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    except OSError:
        return False


def _claude_pids() -> set[int] | None:
    """Pids currently running a ``claude`` executable.

    Signal-0 liveness alone is not enough: pid files outlive crashed processes,
    and pids get recycled, so a stale file can point at an unrelated program and
    mark a long-dead session as running. Confirming the command name costs one
    ``ps`` invocation for the whole set.

    Returns None when ``ps`` is unavailable, in which case callers fall back to
    the weaker check rather than hiding everything.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
    except (OSError, subprocess.SubprocessError):
        return None

    pids: set[int] = set()
    for line in result.stdout.splitlines():
        match = _PS_LINE.match(line)
        if match and _CLAUDE_COMMAND.search(match.group(2).strip()):
            pids.add(int(match.group(1)))
    return pids


def read_live_sessions() -> dict[str, LiveSession]:
    """Map of session id -> live process info, for sessions currently running."""
    out: dict[str, LiveSession] = {}
    directory = live_sessions_dir()

    try:
        names = [entry for entry in os.listdir(directory) if entry.endswith(".json")]
    except OSError:
        return out
    if not names:
        return out

    running = _claude_pids()

    for name in names:
        try:
            with open(directory / name, encoding="utf8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue

        session_id = raw.get("sessionId")
        if not isinstance(session_id, str):
            continue

        pid = raw.get("pid")
        if not isinstance(pid, int):
            try:
                pid = int(name[: -len(".json")])
            except ValueError:
                continue

        if not _is_alive(pid) or (running is not None and pid not in running):
            continue

        entry = LiveSession(
            pid=pid,
            session_id=session_id,
            cwd=raw.get("cwd") if isinstance(raw.get("cwd"), str) else "",
            # Claude Code records these in milliseconds.
            started_at=(raw.get("startedAt") or 0) / 1000,
            updated_at=(raw.get("updatedAt") or 0) / 1000,
            version=raw.get("version") if isinstance(raw.get("version"), str) else "",
            kind=raw.get("kind") if isinstance(raw.get("kind"), str) else "",
            entrypoint=raw.get("entrypoint") if isinstance(raw.get("entrypoint"), str) else "",
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
            status=raw.get("status") if isinstance(raw.get("status"), str) else None,
        )

        # If two processes claim one session (reattach after a crash), the most
        # recently updated one is authoritative.
        existing = out.get(session_id)
        if existing is None or entry.updated_at > existing.updated_at:
            out[session_id] = entry

    return out
