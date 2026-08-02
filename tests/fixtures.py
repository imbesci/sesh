"""Builders for test data shaped like real Claude Code transcripts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from typing import Any

from sesh.core.types import BranchStat, CwdStat, PromptEntry, SessionMeta
from sesh.core.view import Anchor

NOW = time.time()


def session(**over: Any) -> SessionMeta:
    """A SessionMeta with sensible defaults, overridden per test."""
    session_id = over.pop("id", None) or str(uuid.uuid4())
    cwd = over.pop("origin_cwd", "/repo/api")
    repo_key = over.pop("repo_key", "/repo/api")

    defaults: dict[str, Any] = {
        "id": session_id,
        "file": f"/projects/-repo-api/{session_id}.jsonl",
        "project_dir": "-repo-api",
        "origin_cwd": cwd,
        "cwds": [CwdStat(path=cwd, count=10, last_seen=NOW, repo_root=repo_key, repo_key=repo_key)],
        "repo_root": repo_key,
        "repo_key": repo_key,
        "repo_name": repo_key.split("/")[-1] if repo_key else None,
        "branches": [BranchStat(name="main", count=10, last_seen=NOW)],
        "primary_branch": "main",
        "last_branch": "main",
        "ai_title": "A session",
        "first_prompt": "do the thing",
        "last_prompt": "do the thing",
        "prompts": [PromptEntry(text="do the thing", at=NOW, branch="main")],
        "prompts_truncated": False,
        "files": [],
        "tools": [],
        "started_at": NOW - 3600,
        "ended_at": NOW,
        "turns": 1,
        "records": 20,
        "sidechain_records": 0,
        "tool_calls": 3,
        "output_tokens": 1000,
        "input_tokens": 5000,
        "models": ["claude-opus-5"],
        "version": "2.1.212",
        "size_bytes": 4096,
        "mtime": NOW,
        "has_subagents": False,
        "compacted": False,
        "live": None,
    }
    defaults.update(over)
    return SessionMeta(**defaults)


def anchor(**over: Any) -> Anchor:
    defaults = {"cwd": "/repo/api", "repo_key": "/repo/api", "repo_name": "api", "branch": "main"}
    defaults.update(over)
    return Anchor(**defaults)  # type: ignore[arg-type]


def user_prompt(text: str, **over: Any) -> dict:
    """A typed user record."""
    record = {
        "parentUuid": None,
        "isSidechain": False,
        "promptId": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": str(uuid.uuid4()),
        "timestamp": "2026-08-01T12:00:00.000Z",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/repo/api",
        "sessionId": "s1",
        "version": "2.1.212",
        "gitBranch": "main",
    }
    record.update(over)
    return record


def assistant_message(content: list, **over: Any) -> dict:
    """An assistant record.

    Key ordering matters: real transcripts nest ``message`` *before* the
    envelope ``type``, which is precisely the shape that broke the first scanner
    implementation, so fixtures must reproduce it.
    """
    record = {
        "parentUuid": str(uuid.uuid4()),
        "isSidechain": False,
        "message": {
            "model": "claude-opus-5",
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 10,
                "output_tokens": 50,
            },
        },
        "requestId": "req_1",
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "timestamp": "2026-08-01T12:00:00.000Z",
        "cwd": "/repo/api",
        "sessionId": "s1",
        "version": "2.1.212",
        "gitBranch": "main",
    }
    record.update(over)
    return record


def tool_result(text: str, **over: Any) -> dict:
    record = {
        "parentUuid": str(uuid.uuid4()),
        "isSidechain": False,
        "promptId": "p1",
        "type": "user",
        "message": {"role": "user", "content": [{"tool_use_id": "t1", "type": "tool_result", "content": text}]},
        "toolUseResult": {"stdout": text},
        "uuid": str(uuid.uuid4()),
        "timestamp": "2026-08-01T12:00:00.000Z",
        "cwd": "/repo/api",
        "sessionId": "s1",
        "version": "2.1.212",
        "gitBranch": "main",
    }
    record.update(over)
    return record


class Transcript:
    """A temporary transcript on disk, cleaned up on exit."""

    def __init__(self, records: list, name: str = "s1") -> None:
        self.dir = tempfile.mkdtemp(prefix="sesh-test-")
        project = os.path.join(self.dir, "-repo-api")
        os.makedirs(project, exist_ok=True)
        self.path = os.path.join(project, f"{name}.jsonl")
        with open(self.path, "w", encoding="utf8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def append_raw(self, text: str) -> None:
        with open(self.path, "a", encoding="utf8") as handle:
            handle.write(text)

    def add_subagent(self, records: list, name: str = "agent-abc") -> str:
        directory = os.path.join(self.path[: -len(".jsonl")], "subagents")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{name}.jsonl")
        with open(path, "w", encoding="utf8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "Transcript":
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()
