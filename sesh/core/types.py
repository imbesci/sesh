"""Data model for Claude Code sessions as they exist on disk.

A session is a single ``.jsonl`` transcript under::

    ~/.claude/projects/<encoded-origin-cwd>/<sessionId>.jsonl

The encoded directory name is derived from the cwd Claude Code was *launched*
in, but a session's cwd can drift (the user ``cd``s, or a subagent runs
elsewhere), so almost nothing should be inferred from the directory name.
Everything here is read from the record stream itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(slots=True)
class BranchStat:
    """One ``gitBranch`` value observed in a session."""

    name: str
    count: int
    #: Epoch seconds of the most recent record carrying this branch.
    last_seen: float


@dataclass(slots=True)
class CwdStat:
    """One ``cwd`` value observed in a session."""

    path: str
    count: int
    last_seen: float
    #: Resolved git repository root, or None if not in a repo.
    repo_root: str | None
    #: Primary repo root, with linked worktrees collapsed onto their parent.
    repo_key: str | None


@dataclass(slots=True)
class PromptEntry:
    """A single prompt the human typed, retained for search and preview."""

    text: str
    #: Epoch seconds.
    at: float
    #: Branch the prompt was sent on.
    branch: str | None


@dataclass(slots=True)
class ToolStat:
    """How often a tool was invoked in a session."""

    name: str
    count: int


@dataclass(slots=True)
class LiveSession:
    """A running Claude Code process, from ``~/.claude/sessions/<pid>.json``."""

    pid: int
    session_id: str
    cwd: str
    started_at: float
    updated_at: float
    version: str
    kind: str
    entrypoint: str
    #: Human-friendly process name shown in Claude Code's own UI.
    name: str | None = None
    #: e.g. "busy" | "idle".
    status: str | None = None


@dataclass(slots=True)
class SessionMeta:
    """Everything the picker needs about a session.

    Extracted in one streaming pass and cached. Deliberately flat and
    JSON-serialisable so it round-trips through the on-disk index without a
    schema layer.
    """

    #: Session UUID -- the value passed to ``claude --resume``.
    id: str
    #: Absolute path to the .jsonl transcript.
    file: str
    #: Encoded project directory name, e.g. "-Users-alice-dev-api".
    project_dir: str

    #: cwd of the earliest record that has one. The natural place to resume.
    origin_cwd: str
    #: Every distinct cwd the session touched, most-used first.
    cwds: list[CwdStat]
    #: Git root of origin_cwd (None when not a repo).
    repo_root: str | None
    #: Primary repo root, worktrees collapsed. The key scope filtering uses.
    repo_key: str | None
    #: Basename of repo_root, for display.
    repo_name: str | None

    #: Every distinct gitBranch observed, most-used first.
    branches: list[BranchStat]
    #: The branch the session spent the most records on.
    primary_branch: str | None
    #: The branch of the most recent record -- where the session left off.
    last_branch: str | None

    #: Claude Code's own generated title, when it got around to writing one.
    ai_title: str | None
    #: First substantive user prompt, cleaned of slash-command wrappers.
    first_prompt: str
    #: Most recent user prompt.
    last_prompt: str | None
    #: Every prompt the human typed (capped).
    #:
    #: This is the single most valuable thing to index: "the session where I
    #: asked about X" is how people actually remember their work, and searching
    #: these answers it without touching disk.
    prompts: list[PromptEntry]
    #: True when the prompt list hit its cap and is incomplete.
    prompts_truncated: bool
    #: Distinct file paths passed to file-editing tools (capped).
    files: list[str]
    #: Tool usage counts, most-used first.
    tools: list[ToolStat]

    started_at: float
    ended_at: float
    #: Number of prompts the human typed -- the natural "length" of a session.
    turns: int
    #: Total records in the file.
    records: int
    #: Records belonging to subagents rather than the main thread.
    sidechain_records: int
    #: Assistant tool_use blocks.
    tool_calls: int
    #: Sum of assistant output tokens (a proxy for effort spent).
    output_tokens: int
    #: Sum of assistant input tokens, including cache reads and writes.
    input_tokens: int
    #: Distinct model ids seen.
    models: list[str]
    #: Claude Code version that wrote the last record.
    version: str | None

    size_bytes: int
    mtime: float

    #: True when a sibling <id>/subagents directory exists.
    has_subagents: bool
    #: True when the transcript contains a compaction summary record.
    compacted: bool
    #: True when the session's last main-thread event was a tool call or its
    #: result rather than a closing message -- i.e. Claude was mid-action when
    #: the transcript ended. The "work I left hanging" signal.
    ended_mid_action: bool = False
    #: Session id this transcript was forked/continued from, when its early
    #: records carry a different session id than its own. None for the common
    #: case. Best-effort: dormant unless Claude Code preserves the parent id.
    forked_from: str | None = None

    #: Claude's final natural-language message on the main thread -- "where you
    #: left off". The other half of a session's identity: the prompts say what
    #: you asked, this says where it ended up. Capped for the index.
    last_assistant_text: str | None = None
    #: The last concrete action Claude took, rendered compactly, e.g.
    #: "Edit view.py" or "Bash: git commit -m …". None when nothing ran.
    last_action: str | None = None

    #: Set when a running process currently owns this session.
    live: LiveSession | None = None


# --- serialisation -----------------------------------------------------------
#
# The index is plain JSON so it stays inspectable and version-tolerant. These
# helpers are hand-rolled rather than pulled from a library because the shape is
# small, fixed, and the whole point of this project is to have no dependencies.

_NESTED: dict[str, Any] = {
    "cwds": CwdStat,
    "branches": BranchStat,
    "prompts": PromptEntry,
    "tools": ToolStat,
}


def meta_to_dict(meta: SessionMeta) -> dict[str, Any]:
    """Convert to a JSON-safe dict."""
    return asdict(meta)


def meta_from_dict(raw: dict[str, Any]) -> SessionMeta:
    """Rebuild from a cached dict, tolerating unknown or missing keys.

    Unknown keys are dropped rather than raising: an index written by a newer
    build should degrade to a rescan, not crash the picker.
    """
    known = {f.name for f in fields(SessionMeta)}
    data = {k: v for k, v in raw.items() if k in known}

    for key, cls in _NESTED.items():
        if key in data and isinstance(data[key], list):
            data[key] = [cls(**item) if isinstance(item, dict) else item for item in data[key]]

    live = data.get("live")
    if isinstance(live, dict):
        live_fields = {f.name for f in fields(LiveSession)}
        data["live"] = LiveSession(**{k: v for k, v in live.items() if k in live_fields})

    return SessionMeta(**data)
