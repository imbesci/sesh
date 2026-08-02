"""Full-text search across transcript bodies.

The index deliberately keeps only what the human typed, which answers most
"which session was that?" questions cheaply. It cannot answer the rest: the
error message you pasted, a filename Claude mentioned once, a command buried in
tool output. Those live in the raw transcripts, which are far too large to hold
in an index but perfectly cheap to grep on demand.

ripgrep does the work when available. It is not a hard dependency -- the
fallback reads the candidate files directly, which is slower but keeps the
feature working everywhere.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

from .paths import subagent_dir
from .types import SessionMeta

#: Result lines longer than this are trimmed around the match.
SNIPPET_WIDTH = 160
#: Upper bound on files handed to one search.
MAX_FILES = 4000
#: Matches reported per file before ripgrep moves on.
MAX_COUNT_PER_FILE = 5

_ripgrep_available: bool | None = None


@dataclass(slots=True)
class DeepHit:
    #: Transcript path (the parent session's, after attribution).
    file: str
    #: A representative matching line, trimmed for display.
    snippet: str
    #: Number of matching lines, capped per file.
    count: int


def _has_ripgrep() -> bool:
    global _ripgrep_available
    if _ripgrep_available is None:
        try:
            result = subprocess.run(
                ["rg", "--version"], capture_output=True, timeout=3, check=False
            )
            _ripgrep_available = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _ripgrep_available = False
    return _ripgrep_available


def _make_snippet(line: str, needle: str) -> str:
    """Pull a readable snippet out of a raw transcript line.

    Transcript lines are JSON records that can be megabytes long, so the raw
    match is unusable as-is. We centre a window on the match and unescape the
    common sequences so the result reads like text rather than like JSON.
    """
    index = line.lower().find(needle.lower())
    start = max(0, index - SNIPPET_WIDTH // 3)
    raw = line[start : start + SNIPPET_WIDTH]
    for source, target in (("\\n", " "), ("\\t", " "), ('\\"', '"'), ("\\\\", "\\")):
        raw = raw.replace(source, target)
    return " ".join(raw.split())


def _collect_jsonl(directory: str, depth: int) -> list[str]:
    """Collect ``.jsonl`` files beneath a directory, to a bounded depth.

    A session's sibling directory is not flat -- subagent transcripts sit under
    ``<id>/subagents/``, alongside other per-session state -- and the layout has
    changed across Claude Code versions. Walking a couple of levels finds them
    wherever they currently live instead of hard-coding one path that will
    quietly stop matching.
    """
    if depth < 0:
        return []
    out: list[str] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for entry in entries:
        if entry.is_dir():
            out.extend(_collect_jsonl(entry.path, depth - 1))
        elif entry.name.endswith(".jsonl"):
            out.append(entry.path)
    return out


def _run_ripgrep(files: list[str], pattern: str, cancel: threading.Event) -> dict[str, DeepHit]:
    out: dict[str, DeepHit] = {}
    # -F fixed strings: users type error messages and paths, not regexes, and a
    # stray "(" should narrow a search rather than break it.
    args = [
        "rg", "-F", "-i", "--no-heading", "--with-filename",
        "--max-count", str(MAX_COUNT_PER_FILE), "-N", pattern, *files,
    ]
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except (OSError, subprocess.SubprocessError):
        return out

    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel.is_set():
                process.kill()
                break
            # rg prefixes each match with "<path>:".
            separator = line.find(":")
            if separator == -1:
                continue
            path = line[:separator]
            rest = line[separator + 1 :]
            existing = out.get(path)
            if existing is not None:
                existing.count += 1
            else:
                out[path] = DeepHit(file=path, snippet=_make_snippet(rest, pattern), count=1)
    finally:
        try:
            process.stdout.close()  # type: ignore[union-attr]
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            process.kill()
    return out


def _run_fallback(files: list[str], pattern: str, cancel: threading.Event) -> dict[str, DeepHit]:
    """Scanner for environments without ripgrep."""
    out: dict[str, DeepHit] = {}
    needle = pattern.lower()
    for path in files:
        if cancel.is_set():
            break
        try:
            with open(path, encoding="utf8", errors="replace") as handle:
                for line in handle:
                    if needle in line.lower():
                        out[path] = DeepHit(file=path, snippet=_make_snippet(line, pattern), count=1)
                        break
        except OSError:
            continue
    return out


def deep_search(
    sessions: list[SessionMeta],
    pattern: str,
    cancel: threading.Event | None = None,
) -> dict[str, DeepHit]:
    """Search the given sessions' transcripts for ``pattern``.

    Takes the candidate sessions rather than a directory so the caller can
    narrow by scope first: grepping only the current repo's transcripts is
    typically an order of magnitude less work than grepping everything.
    """
    signal = cancel if cancel is not None else threading.Event()
    if not pattern.strip():
        return {}

    # Subagent transcripts hold a large share of what actually happened in a
    # delegating session. Searching only the main transcript would silently miss
    # it, so their files are searched too and their hits attributed back to the
    # parent session.
    owner: dict[str, str] = {}
    files: list[str] = []
    for session in sessions[:MAX_FILES]:
        files.append(session.file)
        owner[session.file] = session.file
        if not session.has_subagents:
            continue
        for path in _collect_jsonl(subagent_dir(session.file), 2):
            files.append(path)
            owner[path] = session.file

    if not files:
        return {}

    raw = _run_ripgrep(files, pattern, signal) if _has_ripgrep() else _run_fallback(files, pattern, signal)

    # Collapse subagent hits onto their parent session.
    out: dict[str, DeepHit] = {}
    for path, hit in raw.items():
        key = owner.get(path, path)
        existing = out.get(key)
        if existing is not None:
            existing.count += hit.count
        else:
            out[key] = DeepHit(file=key, snippet=hit.snippet, count=hit.count)
    return out
