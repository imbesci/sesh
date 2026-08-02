"""Streaming metadata extraction from a session transcript.

The TypeScript original began as a hand-rolled scanner that avoided
``JSON.parse`` on the theory that parsing megabytes of tool output we
immediately discard would dominate startup. Measurement said otherwise, and the
same holds here: Python's ``json`` parses ~19MB of transcript in ~43ms, and the
targeted string-scanning alternative was both slower *and* wrong.

Wrong, specifically, because the record-level ``type`` field appears *after* the
nested message body on assistant records and *before* it on user records, so no
positional heuristic identifies it reliably -- and quoted transcript content
(this tool's own sessions, for instance) can spoof any string search.

So: parse every line. The only concession is a size guard, since one
pathological line should not blow out memory.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .git import main_repo_root, repo_root
from .paths import subagent_dir
from .types import BranchStat, CwdStat, PromptEntry, SessionMeta, ToolStat

#: A single line larger than this is skipped rather than parsed.
MAX_LINE_BYTES = 8 * 1024 * 1024

#: Bound on prompts retained per session, to keep the on-disk index small.
MAX_PROMPTS = 120
#: Bound on characters retained per prompt.
MAX_PROMPT_CHARS = 300
#: Bound on distinct touched files retained per session.
MAX_FILES = 80
#: Bound on characters retained for Claude's last message.
MAX_LEFTOFF_CHARS = 280

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_LOCAL_STDOUT = re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL)
_COMMAND_MESSAGE = re.compile(r"<command-message>.*?</command-message>", re.DOTALL)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SHORT_TAG = re.compile(r"</?[a-zA-Z][^>\n]{0,40}>")
_WHITESPACE = re.compile(r"\s+")
_BARE_ID = re.compile(r"^[0-9a-f]{8,}(\s+toolu_[A-Za-z0-9]+)?$", re.IGNORECASE)
_REMINDER_ONLY = re.compile(r"^<system-reminder>.*</system-reminder>\s*$", re.DOTALL)


def clean_prompt_text(raw: str) -> str:
    """Strip the wrappers Claude Code injects around slash commands and hooks.

    For a slash command we prefer the arguments over the bare name -- "/goal" is
    a useless title, "/goal <the actual goal>" is often the best title the
    session has.
    """
    text = _SYSTEM_REMINDER.sub(" ", raw)
    text = _LOCAL_STDOUT.sub(" ", text)
    text = _COMMAND_MESSAGE.sub(" ", text)

    name_match = _COMMAND_NAME.search(text)
    if name_match:
        name = name_match.group(1).strip()
        args_match = _COMMAND_ARGS.search(text)
        args = args_match.group(1).strip() if args_match else ""
        text = f"{name} {args}" if args else name

    # Remaining short tags are structural noise; long ones are probably prose.
    text = _SHORT_TAG.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def is_synthetic_prompt_raw(raw: str) -> bool:
    """True for user records that are machine-generated rather than typed.

    Must be tested against the *raw* text, before tag stripping. A background
    task completion arrives as ``<task-notification><task-id>a46...</task-id>``;
    strip the tags first and it becomes a bare hex id, which no longer looks
    synthetic and ends up presented as something the human said -- and counted
    as a turn. Getting this wrong is not cosmetic: turn counts are a sort key,
    and these records outnumber real prompts in any session that fans out to
    subagents.
    """
    text = raw.lstrip()
    if not text:
        return True
    return (
        text.startswith("<task-notification")
        or text.startswith("<user-memory-input")
        or text.startswith("<local-command-stdout")
        or text.startswith("<command-stdout")
        or text.startswith("Caveat:")
        or text.startswith("[Request interrupted")
        or text.startswith("This session is being continued from a previous")
        or bool(_REMINDER_ONLY.match(text))
    )


def _is_empty_prompt(text: str) -> bool:
    """True when cleaned text carries no meaningful content."""
    return not text or bool(_BARE_ID.match(text))


class _Tally:
    """Accumulates per-key counts and last-seen timestamps during the scan."""

    __slots__ = ("_counts", "_last")

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._last: dict[str, float] = {}

    def add(self, key: str, at: float) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        if at > self._last.get(key, 0.0):
            self._last[key] = at

    def ranked(self) -> list[tuple[str, int, float]]:
        """Most frequent first; ties broken by recency."""
        items = [(k, c, self._last.get(k, 0.0)) for k, c in self._counts.items()]
        items.sort(key=lambda item: (-item[1], -item[2]))
        return items


def parse_timestamp(value: object) -> float:
    """Parse an ISO-8601 timestamp to epoch seconds, or 0."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def as_dict(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def typed_prompt_text(content: object) -> str | None:
    """Extract what the human typed from a user message's content.

    Returns None when the content is not a typed prompt at all. Newer records
    wrap prompts in a list of text blocks; anything containing a ``tool_result``
    is a response to the model, not a message from the user.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for raw_block in content:
        block = as_dict(raw_block)
        if block is None:
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            text = as_str(block.get("text"))
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else None


#: Tool-input keys that best identify what a call did, tried in order. A path
#: names the target; a command/pattern/prompt is the payload; anything else
#: falls back to the bare tool name.
_TOOL_PATH_KEYS = ("file_path", "notebook_path", "path")
_TOOL_PAYLOAD_KEYS = ("command", "pattern", "query", "description", "prompt", "url")


def describe_tool_use(name: str | None, params: dict | None) -> str:
    """A compact one-liner for a single ``tool_use`` block, for "last action".

    "Edit view.py" and "Bash: git commit -m …" carry the state of a session at a
    glance; the raw JSON does not. File tools are keyed by their target's
    basename (the full path lives in ``files``); command-shaped tools show a
    trimmed snippet of what they ran.
    """
    label = name or "tool"
    if not params:
        return label
    for key in _TOOL_PATH_KEYS:
        path = as_str(params.get(key))
        if path:
            return f"{label} {os.path.basename(path.rstrip('/')) or path}"
    for key in _TOOL_PAYLOAD_KEYS:
        value = as_str(params.get(key))
        if value:
            snippet = _WHITESPACE.sub(" ", value).strip()
            if snippet:
                return f"{label}: {snippet[:60]}"
    return label


@dataclass(slots=True)
class ScanInput:
    path: str
    id: str
    project_dir: str
    size_bytes: int
    mtime: float


def scan_session(source: ScanInput) -> SessionMeta:
    """Extract metadata for one transcript.

    Never raises: a corrupt or half-written file yields a degraded-but-usable
    record, because a session you cannot see is worse than a session with a
    missing title.
    """
    branches = _Tally()
    cwds = _Tally()
    models: dict[str, None] = {}  # insertion-ordered set
    tools: dict[str, int] = {}
    files: dict[str, None] = {}
    prompts: list[PromptEntry] = []

    records = 0
    tool_calls = 0
    output_tokens = 0
    input_tokens = 0
    started_at = 0.0
    ended_at = 0.0
    version: str | None = None
    ai_title: str | None = None
    last_prompt_record: str | None = None
    last_assistant_text: str | None = None
    last_action: str | None = None
    compacted = False
    session_id = source.id
    origin_cwd = ""
    sidechain_records = 0
    prompts_truncated = False
    # The last thing that happened on the main thread, for the "unfinished"
    # signal: "assistant_text" (Claude wrapped up), "assistant_tool"/"tool_result"
    # (mid-action), "user" (asked, unanswered). Updated in record order.
    last_event: str | None = None
    #: First session id seen in the stream, to spot a forked/continued transcript.
    first_session_id: str | None = None

    try:
        with open(source.path, encoding="utf8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # A partially-flushed final line is normal while Claude
                    # Code writes.
                    continue
                if not isinstance(record, dict):
                    continue
                records += 1

                record_type = as_str(record.get("type"))
                if not record_type:
                    continue

                at = parse_timestamp(record.get("timestamp"))
                if at:
                    if not started_at or at < started_at:
                        started_at = at
                    if at > ended_at:
                        ended_at = at

                is_sidechain = record.get("isSidechain") is True
                if is_sidechain:
                    sidechain_records += 1

                if record_type == "ai-title":
                    # Rewritten as the session evolves; the last one wins.
                    title = as_str(record.get("aiTitle"))
                    if title:
                        ai_title = title
                    continue
                if record_type == "last-prompt":
                    prompt = as_str(record.get("lastPrompt"))
                    if prompt:
                        last_prompt_record = clean_prompt_text(prompt)
                    continue
                if record_type == "summary":
                    compacted = True
                    continue

                cwd = as_str(record.get("cwd"))
                if cwd and cwd.startswith("/"):
                    cwds.add(cwd, at)
                    if not origin_cwd:
                        origin_cwd = cwd

                # Subagents inherit the parent's branch; counting their records
                # would let one big fan-out decide which branch a session
                # appears to belong to.
                if not is_sidechain:
                    branch = as_str(record.get("gitBranch"))
                    if branch:
                        branches.add(branch, at)

                found_version = as_str(record.get("version"))
                if found_version:
                    version = found_version
                found_id = as_str(record.get("sessionId"))
                if found_id:
                    if first_session_id is None:
                        first_session_id = found_id
                    session_id = found_id

                message = as_dict(record.get("message"))

                if record_type == "assistant" and message is not None:
                    model = as_str(message.get("model"))
                    if model:
                        models[model] = None
                    usage = as_dict(message.get("usage"))
                    if usage is not None:
                        out = usage.get("output_tokens")
                        if isinstance(out, int):
                            output_tokens += out
                        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                            value = usage.get(key)
                            if isinstance(value, int):
                                input_tokens += value

                    content = message.get("content")
                    # "Where you left off": the main thread's final words and
                    # final action. Subagent turns are excluded -- a fan-out
                    # worker's last message is not where *this* session ended.
                    # Records stream in order, so the last overwrite wins.
                    text_parts: list[str] = []
                    # Which kind of block the turn ended on -- Claude typically
                    # emits text then tool calls, so a trailing tool_use means it
                    # signed off by acting, not by concluding.
                    final_block_kind: str | None = None
                    if isinstance(content, list):
                        for raw_block in content:
                            block = as_dict(raw_block)
                            if block is None:
                                continue
                            block_type = block.get("type")
                            if block_type == "text":
                                snippet = as_str(block.get("text"))
                                if snippet and snippet.strip():
                                    text_parts.append(snippet.strip())
                                    final_block_kind = "text"
                                continue
                            if block_type != "tool_use":
                                continue
                            final_block_kind = "tool_use"
                            tool_calls += 1
                            name = as_str(block.get("name"))
                            if name:
                                tools[name] = tools.get(name, 0) + 1
                            params = as_dict(block.get("input"))
                            if not is_sidechain:
                                last_action = describe_tool_use(name, params)
                            # Touched-file paths make "which session edited X?"
                            # answerable instantly, without a content grep.
                            if len(files) < MAX_FILES and params is not None:
                                path = as_str(params.get("file_path")) or as_str(
                                    params.get("notebook_path")
                                )
                                if path:
                                    files[path] = None
                    elif isinstance(content, str) and content.strip():
                        text_parts.append(content.strip())
                        final_block_kind = "text"

                    if not is_sidechain:
                        if text_parts:
                            last_assistant_text = _WHITESPACE.sub(
                                " ", " ".join(text_parts)
                            ).strip()[:MAX_LEFTOFF_CHARS]
                        if final_block_kind == "tool_use":
                            last_event = "assistant_tool"
                        elif final_block_kind == "text":
                            last_event = "assistant_text"
                    continue

                if record_type == "user" and message is not None and not is_sidechain:
                    # Structural filters first: tool results and injected meta
                    # records are not things the human typed.
                    if "toolUseResult" in record:
                        # A tool came back but Claude has not (yet) responded.
                        last_event = "tool_result"
                        continue
                    if record.get("isMeta") is True:
                        continue

                    text = typed_prompt_text(message.get("content"))
                    if text is None or is_synthetic_prompt_raw(text):
                        continue

                    cleaned = clean_prompt_text(text)
                    if _is_empty_prompt(cleaned):
                        continue

                    last_event = "user"
                    if len(prompts) < MAX_PROMPTS:
                        prompts.append(
                            PromptEntry(
                                text=cleaned[:MAX_PROMPT_CHARS],
                                at=at,
                                branch=as_str(record.get("gitBranch")),
                            )
                        )
                    else:
                        prompts_truncated = True
    except OSError:
        pass  # unreadable -- keep whatever we gathered

    cwd_ranked = cwds.ranked()
    if not origin_cwd and cwd_ranked:
        origin_cwd = cwd_ranked[0][0]

    cwd_stats = [
        CwdStat(path=path, count=count, last_seen=last, repo_root=repo_root(path), repo_key=main_repo_root(path))
        for path, count, last in cwd_ranked
    ]
    branch_stats = [BranchStat(name=name, count=count, last_seen=last) for name, count, last in branches.ranked()]

    root = repo_root(origin_cwd) if origin_cwd else None
    last_branch = max(branch_stats, key=lambda b: b.last_seen).name if branch_stats else None

    tool_stats = [ToolStat(name=name, count=count) for name, count in sorted(tools.items(), key=lambda kv: -kv[1])]

    ended_mid_action = last_event in ("assistant_tool", "tool_result")
    # A parent id only counts when the stream also *became* this session -- the
    # early records belong to an ancestor, the later ones to us.
    forked_from = (
        first_session_id
        if first_session_id and first_session_id != session_id and session_id == source.id
        else None
    )

    return SessionMeta(
        id=session_id,
        file=source.path,
        project_dir=source.project_dir,
        origin_cwd=origin_cwd,
        cwds=cwd_stats,
        repo_root=root,
        repo_key=main_repo_root(origin_cwd) if origin_cwd else None,
        repo_name=os.path.basename(root) if root else None,
        branches=branch_stats,
        primary_branch=branch_stats[0].name if branch_stats else None,
        last_branch=last_branch,
        ai_title=ai_title,
        first_prompt=prompts[0].text if prompts else "",
        last_prompt=last_prompt_record or (prompts[-1].text if prompts else None),
        prompts=prompts,
        prompts_truncated=prompts_truncated,
        files=list(files),
        tools=tool_stats,
        started_at=started_at or source.mtime,
        ended_at=ended_at or source.mtime,
        turns=len(prompts) + (1 if prompts_truncated else 0),
        records=records,
        sidechain_records=sidechain_records,
        tool_calls=tool_calls,
        output_tokens=output_tokens,
        input_tokens=input_tokens,
        models=list(models),
        version=version,
        size_bytes=source.size_bytes,
        mtime=source.mtime,
        has_subagents=Path(subagent_dir(source.path)).exists(),
        compacted=compacted,
        ended_mid_action=ended_mid_action,
        forked_from=forked_from,
        last_assistant_text=last_assistant_text,
        last_action=last_action,
        live=None,
    )


def scan_path(path: str) -> SessionMeta:
    """Scan a transcript by path (used by tests and the CLI)."""
    stat = os.stat(path)
    return scan_session(
        ScanInput(
            path=path,
            id=os.path.basename(path)[: -len(".jsonl")],
            project_dir=os.path.basename(os.path.dirname(path)),
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
        )
    )
