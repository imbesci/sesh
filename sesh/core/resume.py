"""Launching ``claude --resume``.

The load-bearing detail: session-id lookup is scoped to the *project directory*,
which Claude Code derives from the cwd of the process. Resuming from the wrong
directory fails with "No conversation found with session ID", even though the
transcript is sitting right there on disk. So the picker cannot simply exec in
place -- it has to reconstruct a working directory whose encoded form matches
the directory the transcript lives in.

A session's recorded cwd can drift (the user ``cd``s mid-session, subagents run
elsewhere), so the recorded origin cwd is a strong hint, not an answer. We
verify it against the project directory name and fall back through the other
directories the session touched.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

from .paths import encode_project_dir
from .types import SessionMeta


def skip_permissions_default() -> bool:
    """Whether resume should pass ``--dangerously-skip-permissions`` by default.

    On by default, because that is how this user drives Claude Code (their
    settings.json already sets ``skipDangerousModePermissionPrompt``). Set
    ``SESH_SAFE=1`` in the environment, or pass ``--safe`` on the command line, to
    resume without the flag for a session where you want the prompts back.
    """
    return os.environ.get("SESH_SAFE", "").strip().lower() not in ("1", "true", "yes", "on")


@dataclass(slots=True)
class ResumeTarget:
    cwd: str
    #: True when this cwd's encoded name matches the transcript's project dir.
    exact_project: bool
    exists: bool


def resume_targets(session: SessionMeta) -> list[ResumeTarget]:
    """Rank candidate working directories for resuming, best first.

    A directory that both exists and encodes to the right project name is
    guaranteed to work. One that encodes correctly but is missing can be
    recreated. One that merely exists is a gamble worth offering only when
    nothing better is available.
    """
    seen: set[str] = set()
    candidates: list[str] = []
    if session.origin_cwd:
        candidates.append(session.origin_cwd)
    candidates.extend(c.path for c in session.cwds)

    targets: list[ResumeTarget] = []
    for cwd in candidates:
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        targets.append(
            ResumeTarget(
                cwd=cwd,
                exact_project=encode_project_dir(cwd) == session.project_dir,
                exists=os.path.isdir(cwd),
            )
        )

    targets.sort(key=lambda t: (0 if t.exact_project else 2) + (0 if t.exists else 1))
    return targets


def best_resume_cwd(session: SessionMeta) -> ResumeTarget | None:
    """The directory we would use, or None when nothing usable remains."""
    targets = resume_targets(session)
    for target in targets:
        if target.exists:
            return target
    return targets[0] if targets else None


@dataclass(slots=True)
class ResumePlan:
    command: str
    args: list[str]
    cwd: str
    #: Non-fatal problems worth telling the user about before launching.
    warnings: list[str] = field(default_factory=list)


class ResumeError(Exception):
    """Raised when no usable invocation can be constructed."""


def plan_resume(
    session: SessionMeta,
    fork: bool = False,
    cwd: str | None = None,
    extra_args: list[str] | None = None,
    skip_permissions: bool = False,
) -> ResumePlan:
    """Build the exact invocation for resuming a session, without running it.

    Separated from execution so the UI can show it, copy it, and let tests
    assert on it. When ``skip_permissions`` is set, the invocation leads with
    ``--dangerously-skip-permissions`` so the resumed session runs without
    permission prompts.
    """
    warnings: list[str] = []

    if cwd is not None:
        target_cwd = cwd
        if encode_project_dir(target_cwd) != session.project_dir:
            warnings.append(
                "Resuming from a different project directory than the session was created in. "
                "Claude Code scopes session lookup by directory, so this may fail."
            )
    else:
        target = best_resume_cwd(session)
        if target is None:
            raise ResumeError("This session has no recorded working directory.")
        if not target.exists:
            raise ResumeError(
                f"The session's working directory no longer exists:\n  {target.cwd}\n"
                "Recreate it, or press o to resume from the current directory instead."
            )
        if not target.exact_project:
            warnings.append(
                f"Resuming from {target.cwd}, which does not match this transcript's project "
                "directory. Claude Code may not find the session."
            )
        target_cwd = target.cwd

    if session.live is not None:
        status = f" ({session.live.status})" if session.live.status else ""
        warnings.append(
            f"This session is already open in pid {session.live.pid}{status}. "
            "Two processes writing one transcript interleave their messages -- consider forking."
        )

    # The flag leads the invocation, matching how Claude Code documents it:
    # `claude --dangerously-skip-permissions --resume <id>`.
    args: list[str] = ["--dangerously-skip-permissions"] if skip_permissions else []
    args += ["--resume", session.id]
    if fork:
        args.append("--fork-session")
    if extra_args:
        args.extend(extra_args)

    return ResumePlan(command="claude", args=args, cwd=target_cwd, warnings=warnings)


def plan_to_shell(plan: ResumePlan) -> str:
    """Render a plan as a copy-pasteable shell command."""
    parts = " ".join(shlex.quote(a) for a in plan.args)
    return f"cd {shlex.quote(plan.cwd)} && {plan.command} {parts}"
