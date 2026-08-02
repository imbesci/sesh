"""Loading a readable conversation out of a transcript, for the viewer.

The picker's list and preview run entirely off cached metadata; this is the one
place that reads a whole transcript on demand. It exists because deciding "is
this the session I want?" from a title and a first prompt is often a coin flip
-- being able to skim what actually happened is what makes the difference
between finding a session and guessing at one.

Tool results are reduced to a one-line signature rather than dropped: seeing
that a turn ran ``Bash(git rebase -i)`` is frequently the identifying detail,
while the 40KB of output that followed never is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .scan import as_dict, as_str, parse_timestamp, typed_prompt_text, clean_prompt_text, is_synthetic_prompt_raw

#: Bound on retained entries; older ones are dropped from the front.
MAX_ENTRIES = 4000
#: Bound on characters kept per entry.
MAX_TEXT = 4000


@dataclass(slots=True)
class TranscriptEntry:
    #: "user" | "assistant" | "thinking" | "tool" | "meta"
    role: str
    text: str
    at: float
    #: Tool name, for role == "tool".
    tool: str | None = None
    #: True for subagent (sidechain) records.
    sidechain: bool = False


_TOOL_SUMMARY_KEYS = (
    "command",
    "file_path",
    "notebook_path",
    "pattern",
    "query",
    "url",
    "prompt",
    "description",
    "path",
)


def _describe_tool(name: str, params: object) -> str:
    """Compact a tool invocation into one identifying line."""
    fields = as_dict(params)
    if fields is None:
        return name
    for key in _TOOL_SUMMARY_KEYS:
        value = as_str(fields.get(key))
        if value:
            one_line = " ".join(value.split())
            if one_line:
                return f"{name}({one_line[:160]})"
    return name


def load_transcript(
    path: str,
    include_sidechains: bool = False,
    include_thinking: bool = False,
) -> list[TranscriptEntry]:
    entries: list[TranscriptEntry] = []

    def push(entry: TranscriptEntry) -> None:
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries.pop(0)

    try:
        handle = open(path, encoding="utf8", errors="replace")
    except OSError:
        return entries

    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue

            sidechain = record.get("isSidechain") is True
            if sidechain and not include_sidechains:
                continue

            record_type = as_str(record.get("type"))
            at = parse_timestamp(record.get("timestamp"))
            message = as_dict(record.get("message"))

            if record_type == "summary":
                summary = as_str(record.get("summary"))
                if summary:
                    push(TranscriptEntry(role="meta", text=f"Compacted: {summary[:MAX_TEXT]}", at=at))
                continue

            if record_type == "user" and message is not None:
                if "toolUseResult" in record or record.get("isMeta") is True:
                    continue
                raw = typed_prompt_text(message.get("content"))
                if raw is None:
                    continue
                # Machine-generated turns are shown, but marked, so the
                # transcript stays faithful without them masquerading as things
                # the human said.
                role = "meta" if is_synthetic_prompt_raw(raw) else "user"
                cleaned = clean_prompt_text(raw)
                if cleaned:
                    push(TranscriptEntry(role=role, text=cleaned[:MAX_TEXT], at=at, sidechain=sidechain))
                continue

            if record_type == "assistant" and message is not None:
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for raw_block in content:
                    block = as_dict(raw_block)
                    if block is None:
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = (as_str(block.get("text")) or "").strip()
                        if text:
                            push(
                                TranscriptEntry(
                                    role="assistant", text=text[:MAX_TEXT], at=at, sidechain=sidechain
                                )
                            )
                    elif block_type == "thinking" and include_thinking:
                        text = (as_str(block.get("thinking")) or "").strip()
                        if text:
                            push(
                                TranscriptEntry(
                                    role="thinking", text=text[:MAX_TEXT], at=at, sidechain=sidechain
                                )
                            )
                    elif block_type == "tool_use":
                        name = as_str(block.get("name")) or "tool"
                        push(
                            TranscriptEntry(
                                role="tool",
                                text=_describe_tool(name, block.get("input")),
                                at=at,
                                tool=name,
                                sidechain=sidechain,
                            )
                        )

    return entries
