"""Persistent metadata cache.

A cold scan of a large ``~/.claude/projects`` tree is the only slow operation in
this tool, and it is entirely redundant across runs: transcripts are
append-only, so a file whose (mtime, size) pair is unchanged cannot have changed
content. Caching on that pair turns every subsequent launch into a stat-only
walk.

Bumping ``INDEX_VERSION`` invalidates every entry -- do it whenever the shape of
SessionMeta or the extraction logic changes, otherwise stale fields silently
survive upgrades. (This is not hypothetical: adding ``repo_key`` without a bump
is exactly how branch filtering broke during development.)
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .live import read_live_sessions
from .paths import TranscriptFile, index_file, list_transcripts
from .scan import ScanInput, scan_session
from .types import SessionMeta, meta_from_dict, meta_to_dict

INDEX_VERSION = 7

#: Concurrent transcript readers. Parsing holds the GIL, but reading does not,
#: so a handful of workers still overlaps IO with parse work.
SCAN_WORKERS = 8


@dataclass(slots=True)
class LoadResult:
    sessions: list[SessionMeta] = field(default_factory=list)
    #: How many transcripts had to be re-read (0 means a fully warm cache).
    scanned: int = 0
    elapsed_ms: float = 0.0


def _load_index() -> dict[str, dict]:
    try:
        with open(index_file(), encoding="utf8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict) and raw.get("version") == INDEX_VERSION:
            entries = raw.get("entries")
            if isinstance(entries, dict):
                return entries
    except (OSError, ValueError):
        pass  # missing or corrupt -- rebuild
    return {}


def _save_index(entries: dict[str, dict]) -> None:
    """Write the cache atomically.

    Write-then-rename so a crash mid-write cannot leave a truncated index that
    would force a full rescan on the next launch.
    """
    path = index_file()
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf8") as handle:
            json.dump({"version": INDEX_VERSION, "entries": entries}, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_sessions(
    force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> LoadResult:
    """Load metadata for every session on disk, using and updating the cache."""
    import time

    started = time.time()
    cached = {} if force else _load_index()
    files = list_transcripts()

    fresh: dict[str, dict] = {}
    stale: list[TranscriptFile] = []

    for entry in files:
        hit = cached.get(entry.path)
        if (
            hit
            and hit.get("size_bytes") == entry.size_bytes
            and abs(float(hit.get("mtime", -1)) - entry.mtime) < 1e-6
        ):
            fresh[entry.path] = hit
        else:
            stale.append(entry)

    total = len(stale)
    if on_progress:
        on_progress(0, total)

    def scan_one(entry: TranscriptFile) -> tuple[str, dict] | None:
        try:
            meta = scan_session(
                ScanInput(
                    path=entry.path,
                    id=entry.id,
                    project_dir=entry.project_dir,
                    size_bytes=entry.size_bytes,
                    mtime=entry.mtime,
                )
            )
        except Exception:
            # An unreadable transcript is omitted rather than aborting the load.
            return None
        return entry.path, {
            "size_bytes": entry.size_bytes,
            "mtime": entry.mtime,
            "meta": meta_to_dict(meta),
        }

    if stale:
        done = 0
        with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(stale))) as pool:
            for result in pool.map(scan_one, stale):
                done += 1
                if on_progress:
                    on_progress(done, total)
                if result is not None:
                    fresh[result[0]] = result[1]

    _save_index(fresh)

    # Overlay live-process state, which is inherently uncacheable.
    live_map = read_live_sessions()
    sessions: list[SessionMeta] = []
    for entry in fresh.values():
        try:
            meta = meta_from_dict(entry["meta"])
        except (KeyError, TypeError):
            continue
        meta.live = live_map.get(meta.id)
        sessions.append(meta)

    sessions.sort(key=lambda s: -s.ended_at)
    return LoadResult(
        sessions=sessions,
        scanned=len(stale),
        elapsed_ms=(time.time() - started) * 1000,
    )


def refresh_sessions(previous: list[SessionMeta]) -> list[SessionMeta]:
    """Re-read only the sessions whose files changed, and refresh live state.

    Used by the picker's background refresh so an open TUI reflects a session
    you are actively working in from another terminal.
    """
    by_path = {s.file: s for s in previous}
    live_map = read_live_sessions()
    out: list[SessionMeta] = []

    for entry in list_transcripts():
        prior = by_path.get(entry.path)
        if prior is not None and prior.size_bytes == entry.size_bytes and prior.mtime == entry.mtime:
            prior.live = live_map.get(prior.id)
            out.append(prior)
            continue
        try:
            meta = scan_session(
                ScanInput(
                    path=entry.path,
                    id=entry.id,
                    project_dir=entry.project_dir,
                    size_bytes=entry.size_bytes,
                    mtime=entry.mtime,
                )
            )
            meta.live = live_map.get(meta.id)
            out.append(meta)
        except Exception:
            if prior is not None:
                out.append(prior)

    out.sort(key=lambda s: -s.ended_at)
    return out
