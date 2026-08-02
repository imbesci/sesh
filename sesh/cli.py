"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from .core.actions import editor_command
from .core.format import number, relative_time, short_path
from .core.git import current_branch, main_repo_root, repo_root
from .core.index_store import load_sessions
from .core.resume import ResumeError, best_resume_cwd, plan_resume, plan_to_shell, skip_permissions_default
from .core.types import SessionMeta, meta_to_dict
from .core.view import SCOPE_ORDER, Anchor, compute_view, default_view
from .tui.ansi import Theme, color_supported, paint, set_color_enabled
from .tui.app import App

HOME = os.path.expanduser("~")

EPILOG = """
query syntax (shared with the picker):
  branch: repo: file: dir: tool: model: id:   facets
  age: after: before:                         time windows
  turns: tokens: records:                     numeric, e.g. turns:>5
  is:live is:unfinished is:fork               state; also compacted, subagents
  text:"..."                                  full-text search inside transcripts
  'exact  !exclude                            literal substring, negation

examples:
  sesh                      picker; resumes with --dangerously-skip-permissions
  sesh --safe               same picker, but resume with permission prompts on
  sesh auth redirect        picker with a filter pre-applied
  sesh --last               resume the most recent session here
  sesh --open               open the best match's directory in $EDITOR
  sesh is:unfinished        sessions left mid-action
  sesh --list --all         print every session
  sesh 'branch:main age:7d' combine filters
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # Reflect whichever name launched us in usage text. The launcher passes
        # it via SESH_PROG (argv[0] is just "__main__.py" under `python -m sesh`).
        prog=os.environ.get("SESH_PROG") or "sesh",
        description="Find and resume Claude Code sessions.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="*", help="filter to apply")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-l", "--list", action="store_true", help="print matches instead of opening the picker")
    mode.add_argument("--json", action="store_true", help="emit matching session metadata as JSON")
    mode.add_argument("--print", dest="print_cmd", action="store_true", help="print the resume command for the best match")
    mode.add_argument("--last", action="store_true", help="resume the most recent matching session")
    mode.add_argument("--open", dest="open_editor", action="store_true", help="open the best match's directory in $EDITOR")

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("-a", "--all", dest="scope_all", action="store_true", help="every session on this machine")
    scope.add_argument("--repo", dest="scope_repo", action="store_true", help="every session in this repository")
    scope.add_argument("--dir", dest="scope_dir", action="store_true", help="sessions rooted at the current directory")
    scope.add_argument("--branch", metavar="NAME", help="a specific branch")

    parser.add_argument("--reindex", action="store_true", help="rebuild the metadata cache from scratch")
    parser.add_argument("--limit", type=int, default=40, help="rows to print (default: 40)")
    parser.add_argument(
        "--safe",
        action="store_true",
        help="resume without --dangerously-skip-permissions (also: SESH_SAFE=1)",
    )
    return parser


def build_anchor(cwd: str) -> Anchor:
    root = repo_root(cwd)
    return Anchor(
        cwd=cwd,
        repo_key=main_repo_root(cwd),
        repo_name=os.path.basename(root) if root else None,
        branch=current_branch(cwd),
    )


def launch(command: str, args: list[str], cwd: str) -> int:
    """Run ``claude`` as a foreground child and mirror its exit status."""
    try:
        return subprocess.call([command, *args], cwd=cwd)
    except FileNotFoundError:
        print(f"\nsesh: could not find the `{command}` executable on PATH.", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130
    except OSError as err:
        print(f"\nsesh: failed to launch {command}: {err}", file=sys.stderr)
        return 127


def format_row(session: SessionMeta, now: float) -> str:
    live = paint(" ●", fg=Theme.live) if session.live else "  "
    age = paint(relative_time(session.ended_at, now).rjust(4), fg=Theme.time)
    repo = paint((session.repo_name or short_path(session.origin_cwd, HOME, 18))[:16].ljust(16), fg=Theme.repo)
    branch_name = "—" if session.last_branch in (None, "HEAD") else session.last_branch
    branch = paint(branch_name[:12].ljust(12), fg=Theme.branch)
    title = (session.ai_title or session.first_prompt or "")[:58].ljust(58)
    meta = paint(f"{str(session.turns).rjust(3)}p {number(session.output_tokens).rjust(6)}", fg=Theme.faint)
    return f"{live}{age}  {repo} {branch} {title} {meta}  {paint(session.id[:8], fg=Theme.faint)}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_color_enabled(color_supported() and sys.stdout.isatty())

    query = " ".join(args.query)
    interactive = not (args.list or args.json or args.print_cmd or args.last or args.open_editor)
    # --safe wins over the env default; otherwise resume with the skip flag.
    skip_permissions = skip_permissions_default() and not args.safe
    anchor = build_anchor(os.getcwd())

    announced = False

    def progress(done: int, total: int) -> None:
        # On a cold cache this can take a moment; say so rather than appearing
        # hung. Written to stderr so piping stdout stays clean.
        nonlocal announced
        if not sys.stderr.isatty() or total < 12:
            return
        if done >= total:
            sys.stderr.write("\r" + " " * 44 + "\r")
        elif done % 10 == 0 or not announced:
            announced = True
            sys.stderr.write(f"\rsesh: indexing sessions… {done}/{total}")
        sys.stderr.flush()

    result = load_sessions(force=args.reindex, on_progress=progress)
    sessions = result.sessions

    if args.reindex:
        print(f"Indexed {len(sessions)} sessions in {result.elapsed_ms:.0f}ms.")
        if not (args.list or args.json):
            return 0

    if not sessions:
        print("sesh: no Claude Code sessions found under ~/.claude/projects.", file=sys.stderr)
        return 1

    if interactive:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("sesh: the picker needs a terminal. Use --list or --print when piping.", file=sys.stderr)
            return 1

        outcome = App(sessions, anchor, query, skip_permissions=skip_permissions).run()
        if outcome.kind != "resume" or outcome.plan is None:
            return 0

        plan = outcome.plan
        for warning in plan.warnings:
            print(paint(f"! {warning}", fg=Theme.warn), file=sys.stderr)
        label = outcome.session.ai_title or outcome.session.first_prompt or outcome.session.id if outcome.session else ""
        print(paint(f"↻ {label}", fg=Theme.muted), file=sys.stderr)
        print(paint(f"  {plan_to_shell(plan)}", fg=Theme.faint), file=sys.stderr)
        return launch(plan.command, plan.args, plan.cwd)

    # --- non-interactive modes -----------------------------------------------
    view = default_view(anchor)
    view.query = query
    if args.scope_all:
        view.scope = "all"
    elif args.scope_repo:
        view.scope = "repo"
    elif args.scope_dir:
        view.scope = "dir"
    elif args.branch:
        view.scope = "branch"
        view.branch_filter = args.branch

    explicit_scope = bool(args.scope_all or args.scope_repo or args.scope_dir or args.branch)

    import time

    now = time.time()
    hits = compute_view(sessions, view, anchor, now).hits

    # Widen automatically when the default scope comes up empty, mirroring the
    # picker. An explicit --repo/--all/--branch is respected as given: if you
    # asked a precise question, a silently broader answer is the wrong reply.
    if not hits and not explicit_scope:
        for scope in SCOPE_ORDER[SCOPE_ORDER.index(view.scope) + 1 :]:
            view.scope = scope
            hits = compute_view(sessions, view, anchor, now).hits
            if hits:
                break

    if args.json:
        print(json.dumps([meta_to_dict(h.session) for h in hits[: args.limit]], indent=2))
        return 0

    if not hits:
        print("sesh: no sessions matched.", file=sys.stderr)
        return 1

    if args.list:
        for hit in hits[: args.limit]:
            print(format_row(hit.session, now))
        if len(hits) > args.limit:
            print(paint(f"… {len(hits) - args.limit} more", fg=Theme.faint))
        return 0

    best = hits[0].session

    if args.open_editor:
        target = best_resume_cwd(best)
        if target is None or not target.exists:
            print(f"sesh: {best.id[:8]}'s directory no longer exists.", file=sys.stderr)
            return 1
        command = editor_command(target.cwd)
        if command is None:
            print("sesh: no editor found. Set $EDITOR or $VISUAL, or install `code`.", file=sys.stderr)
            return 1
        print(paint(f"→ {target.cwd}", fg=Theme.muted), file=sys.stderr)
        return launch(command[0], command[1:], target.cwd)

    try:
        plan = plan_resume(best, skip_permissions=skip_permissions)
    except ResumeError as err:
        print(f"sesh: {err}", file=sys.stderr)
        return 1

    if args.print_cmd:
        print(plan_to_shell(plan))
        return 0

    for warning in plan.warnings:
        print(paint(f"! {warning}", fg=Theme.warn), file=sys.stderr)
    print(paint(f"↻ {best.ai_title or best.first_prompt or best.id}", fg=Theme.muted), file=sys.stderr)
    return launch(plan.command, plan.args, plan.cwd)


if __name__ == "__main__":
    sys.exit(main())
