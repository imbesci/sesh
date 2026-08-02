"""Pure functions from state to screen lines."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..core.format import (
    absolute_time,
    bytes_,
    duration,
    number,
    relative_time,
    short_model,
    short_path,
)
from ..core.query import QueryHit
from ..core.resume import best_resume_cwd
from ..core.types import SessionMeta
from ..core.view import Anchor, ViewState
from .ansi import Theme, display_width, fit, paint, truncate, two_column

HOME = os.path.expanduser("~")

# Column widths, used both to lay out rows and to decide what fits.
W_MARKER = 2
W_AGE = 4
W_REPO = 14
W_BRANCH = 13
W_TURNS = 4
W_TOKENS = 6
MIN_TITLE = 18


@dataclass(slots=True)
class ListLayout:
    show_repo: bool
    show_branch: bool
    title_width: int


def compute_layout(hits: list[QueryHit], width: int) -> ListLayout:
    """Decide which columns earn their space.

    A column holding the same value in every visible row is pure noise -- once
    you have scoped to one branch, a branch column just pushes the title out of
    view. So repo and branch appear only when the visible set actually spans
    more than one of them, which means the list gets *wider* titles exactly when
    it is least ambiguous.
    """
    repos = {hit.session.repo_key or hit.session.origin_cwd for hit in hits[:200]}
    branches = {hit.session.last_branch or "—" for hit in hits[:200]}

    show_repo = len(repos) > 1
    show_branch = len(branches) > 1

    def fixed() -> int:
        return (
            W_MARKER + W_AGE + 1
            + (W_REPO + 1 if show_repo else 0)
            + (W_BRANCH + 1 if show_branch else 0)
            + 1 + W_TURNS + 1 + W_TOKENS
        )

    # Under pressure, drop repo before branch: within a project, which branch a
    # session belongs to is the more discriminating fact.
    if width - fixed() < MIN_TITLE and show_repo:
        show_repo = False
    if width - fixed() < MIN_TITLE and show_branch:
        show_branch = False

    return ListLayout(show_repo, show_branch, max(6, width - fixed()))


def highlight_text(text: str, positions: list[int], **base) -> str:
    """Apply match highlighting to the characters at ``positions``."""
    if not positions:
        return paint(text, **base)

    marked = set(positions)
    out: list[str] = []
    run = ""
    run_hit = False

    def flush() -> None:
        nonlocal run
        if not run:
            return
        if run_hit:
            out.append(paint(run, fg=Theme.match, bold=True, bg=base.get("bg")))
        else:
            out.append(paint(run, **base))
        run = ""

    for index, char in enumerate(text):
        hit = index in marked
        if hit != run_hit:
            flush()
            run_hit = hit
        run += char
    flush()
    return "".join(out)


def session_label(session: SessionMeta) -> str:
    """The label shown for a session: its title, else its first prompt."""
    if session.ai_title and session.ai_title.strip():
        return session.ai_title.strip()
    if session.first_prompt.strip():
        return session.first_prompt.strip()
    return f"(no prompts) {session.id[:8]}"


def _snippet_around(text: str, positions: list[int], width: int) -> tuple[str, list[int]]:
    """Extract a window of text centred on the match, keeping positions aligned."""
    if width <= 0:
        return "", []
    if len(text) <= width:
        return text, positions

    first = positions[0] if positions else 0
    last = positions[-1] if positions else first
    pad = max(0, (width - (last - first)) // 2)
    start = max(0, first - pad)
    if start + width > len(text):
        start = max(0, len(text) - width)

    prefix = "…" if start > 0 else ""
    body = text[start : start + width - len(prefix)]
    shift = len(prefix) - start
    moved = [p + shift for p in positions if len(prefix) <= p + shift < len(prefix) + len(body)]
    return prefix + body, moved


def render_rows(
    hits: list[QueryHit],
    start: int,
    height: int,
    cursor: int,
    width: int,
    layout: ListLayout,
    has_query: bool,
    now: float,
) -> list[str]:
    """Render list rows.

    A row is normally one line; when a query matched something other than the
    visible title, a dim second line shows the matching snippet so the user can
    see *why* the row is in the list.
    """
    lines: list[str] = []

    for index in range(start, len(hits)):
        if len(lines) >= height:
            break
        hit = hits[index]
        session = hit.session
        selected = index == cursor

        label = session_label(session)
        matched_label = has_query and hit.highlight is not None and hit.highlight.text == label

        # The selection is a background wash. Every segment carries the
        # background explicitly, because each styled fragment ends with a full
        # reset -- wrapping the finished row would blank the wash at the first
        # reset.
        bg = Theme.sel_bg if selected else None

        def seg(text: str, **style) -> str:
            return paint(text, bg=bg, **style)

        if session.live is not None:
            row = seg("● ", fg=Theme.live, bold=True)
        elif selected:
            row = seg("▌ ", fg=Theme.accent, bold=True)
        else:
            row = seg("  ")

        row += seg(fit(relative_time(session.ended_at, now), W_AGE), fg=Theme.time, dim=not selected)
        row += seg(" ")

        if layout.show_repo:
            repo = session.repo_name or short_path(session.origin_cwd, HOME, W_REPO).split("/")[-1] or "—"
            row += seg(fit(repo, W_REPO), fg=Theme.repo, dim=not selected) + seg(" ")
        if layout.show_branch:
            branch = "—" if session.last_branch in (None, "HEAD") else session.last_branch
            row += seg(fit(branch, W_BRANCH), fg=Theme.branch, dim=not selected) + seg(" ")

        title_style = {"fg": Theme.title if selected else Theme.text, "bold": selected, "bg": bg}
        title_text = fit(truncate(label, layout.title_width), layout.title_width)
        if matched_label:
            row += highlight_text(title_text, hit.highlight.positions, **title_style)
        else:
            row += paint(title_text, **title_style)

        meta_fg = Theme.muted if selected else Theme.faint
        row += seg(" " + fit(str(session.turns) if session.turns else "·", W_TURNS), fg=meta_fg)
        row += seg(" " + fit(number(session.output_tokens), W_TOKENS), fg=meta_fg)

        used = display_width(row)
        if used < width:
            row += seg(" " * (width - used))

        lines.append(row)

        # Secondary line: where the match actually landed.
        if has_query and hit.highlight is not None and not matched_label and len(lines) < height:
            indent = W_MARKER + W_AGE + 1
            snippet, moved = _snippet_around(hit.highlight.text, hit.highlight.positions, width - indent - 2)
            lines.append(
                " " * indent
                + paint("↳ ", fg=Theme.faint)
                + highlight_text(snippet, moved, fg=Theme.muted)
            )

    return lines


def render_header(anchor: Anchor, shown: int, scoped: int, total: int, width: int) -> str:
    """Where you are, and how much you are looking at."""
    repo = anchor.repo_name or short_path(anchor.cwd, HOME, 30)
    left = paint(" sesh ", fg=Theme.accent, bold=True) + paint(repo, fg=Theme.repo, bold=True)
    if anchor.branch:
        left += paint(f" {anchor.branch}", fg=Theme.branch)

    right = (
        paint(str(shown), fg=Theme.title, bold=True)
        + paint(f"/{scoped}", fg=Theme.muted)
        + paint(f" of {total} ", fg=Theme.faint)
    )
    return two_column(left, right, width)


def render_chips(view: ViewState, anchor: Anchor, width: int) -> str:
    """Active scope, sort and filters -- always visible."""

    def chip(label: str, value: str, active: bool) -> str:
        return paint(f" {label}:", fg=Theme.faint) + paint(
            value, fg=Theme.accent if active else Theme.muted, bold=active
        )

    left = chip("scope", view.scope, True) + chip("sort", view.sort, view.sort != "recent")
    if view.branch_filter:
        left += chip("branch", view.branch_filter, True)
    if view.repo_filter:
        left += chip("repo", view.repo_filter.rsplit("/", 1)[-1], True)
    if not view.hide_empty:
        left += paint("  +empty", fg=Theme.faint)

    if view.scope == "branch" and anchor.branch:
        hint = f"on {anchor.branch}"
    elif view.scope == "repo":
        hint = "all branches"
    elif view.scope == "dir":
        hint = short_path(anchor.cwd, HOME, 28)
    else:
        hint = "everywhere"

    return two_column(left, paint(f"{hint} ", fg=Theme.faint), width)


def render_input(query: str, cursor_pos: int, width: int, deep: bool, status: str = "") -> str:
    """The query input line, with a visible cursor position and search status."""
    prompt = paint(" ⌕ " if deep else " ❯ ", fg=Theme.warn if deep else Theme.accent, bold=True)
    status_text = paint(f"{status} ", fg=Theme.warn) if status else ""
    available = width - 3 - display_width(status_text)

    # Scroll the input horizontally so the caret stays visible on long queries.
    start = max(0, cursor_pos - available + 1)
    visible = query[start : start + available]
    text = (
        paint(visible, fg=Theme.title)
        if visible
        else paint("type to filter…  (alt+h for keys)", fg=Theme.faint)
    )
    return two_column(prompt + text, status_text, width)


def rule(width: int, label: str = "") -> str:
    if not label:
        return paint("─" * max(0, width), fg=Theme.rule)
    text = f" {label} "
    right = max(0, width - 2 - display_width(text))
    return paint("──", fg=Theme.rule) + paint(text, fg=Theme.faint) + paint("─" * right, fg=Theme.rule)


def render_preview(
    session: SessionMeta | None, width: int, height: int, scroll: int, now: float
) -> list[str]:
    """The detail pane for the highlighted session.

    Ordered by what answers "is this the one?" fastest: the title, when and
    where it ran, then the prompts in order. The prompt list is the real payload
    -- a session's identity lives in what was asked of it.
    """
    if session is None:
        return [paint("  no session selected", fg=Theme.faint)]

    lines: list[str] = []
    inner = width - 2

    def add(text: str) -> None:
        lines.append("  " + text)

    def field(key: str, value: str, **style) -> None:
        add(paint(fit(key, 9), fg=Theme.faint) + paint(truncate(value, inner - 9), **style))

    add(paint(truncate(session_label(session), inner), fg=Theme.title, bold=True))
    add("")

    if session.live is not None:
        status = f" · {session.live.status}" if session.live.status else ""
        field("status", f"running · pid {session.live.pid}{status}", fg=Theme.live, bold=True)

    when = (
        f"{absolute_time(session.started_at)}  ·  {relative_time(session.ended_at, now)} ago"
        f"  ·  {duration(session.started_at, session.ended_at)}"
    )
    field("when", when, fg=Theme.time)

    if session.repo_name:
        field("repo", session.repo_name, fg=Theme.repo)
        add(paint(fit("", 9) + truncate(short_path(session.repo_root or "", HOME, inner - 10), inner - 9), fg=Theme.faint))

    if session.branches:
        shown = "  ".join(
            ("detached" if b.name == "HEAD" else b.name) + f"·{b.count}" for b in session.branches[:4]
        )
        field("branch", shown, fg=Theme.branch)

    target = best_resume_cwd(session)
    missing = "  (missing)" if target is not None and not target.exists else ""
    field("cwd", short_path(session.origin_cwd, HOME, inner - 12) + missing, fg=Theme.muted)
    for cwd in session.cwds[1:3]:
        add(paint(fit("", 9) + truncate(short_path(cwd.path, HOME, inner - 10), inner - 9), fg=Theme.faint))

    stats = [
        f"{session.turns} prompt{'' if session.turns == 1 else 's'}",
        f"{session.tool_calls} tools",
        f"{number(session.output_tokens)} out",
        bytes_(session.size_bytes),
    ]
    if session.compacted:
        stats.append("compacted")
    if session.has_subagents:
        stats.append("subagents")
    field("stats", "  ·  ".join(stats), fg=Theme.muted)

    if session.models:
        field("model", ", ".join(short_model(m) for m in session.models), fg=Theme.muted)
    field("id", session.id, fg=Theme.faint)

    if session.tools:
        field("tools", "  ".join(f"{t.name}·{t.count}" for t in session.tools[:5]), fg=Theme.faint)

    if session.files:
        add("")
        add(paint(f"files ({len(session.files)})", fg=Theme.faint))
        for path in session.files[:6]:
            add(paint(truncate(short_path(path, HOME, inner - 2), inner - 2), fg=Theme.muted))

    if session.prompts:
        add("")
        suffix = "+" if session.prompts_truncated else ""
        lines.append(rule(width, f"prompts ({len(session.prompts)}{suffix})"))
        for prompt in session.prompts:
            add(
                paint(fit(relative_time(prompt.at, now), 5), fg=Theme.faint)
                + paint(truncate(prompt.text, inner - 6), fg=Theme.text)
            )

    start = max(0, min(scroll, max(0, len(lines) - height)))
    return lines[start : start + height]


def wrap_text(text: str, width: int) -> list[str]:
    """Hard-wrap text to a column width, preserving existing newlines."""
    out: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        for word in paragraph.split():
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
            while len(line) > width:
                out.append(line[:width])
                line = line[width:]
        if line:
            out.append(line)
    return out
