"""Turning the full session list into what the user sees.

Two independent knobs, deliberately kept separate:

- **Scope** answers "which universe of sessions am I looking at?" It is
  positional -- derived from where you launched the tool -- and is the thing you
  widen when a search comes up empty.
- **Query** answers "which of those do I want?" It is textual and additive.

Conflating them (the usual mistake: one filter box that also controls scope)
makes widening the search destructive, because you have to delete your query to
see more. Here one key goes from branch to everything without losing what you
typed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from .query import ParsedQuery, QueryContext, QueryHit, evaluate, parse_query
from .types import SessionMeta

SCOPE_ORDER = ("branch", "repo", "dir", "all")
SORT_ORDER = ("recent", "relevance", "unfinished", "turns", "tokens", "size", "oldest", "title")


@dataclass(slots=True)
class Anchor:
    """Where the tool was launched, which anchors the positional scopes."""

    cwd: str
    repo_key: str | None
    repo_name: str | None
    branch: str | None


@dataclass(slots=True)
class ViewState:
    scope: str = "all"
    sort: str = "recent"
    query: str = ""
    #: Explicit branch override from the branch picker; None means "use anchor".
    branch_filter: str | None = None
    #: Explicit repo override from the project picker.
    repo_filter: str | None = None
    #: Hide sessions with no typed prompts -- usually accidental launches.
    hide_empty: bool = True
    #: When set, show only sessions related to this session id (same task),
    #: ignoring scope. Cleared by esc or by toggling off.
    related_to: str | None = None


def default_view(anchor: Anchor) -> ViewState:
    # Start as narrow as the location supports. Inside a repo on a branch, the
    # sessions you want are almost always the ones from that branch; outside a
    # repo, "everything under this directory" is the tightest honest scope.
    if anchor.repo_key:
        scope = "branch" if anchor.branch else "repo"
    else:
        scope = "dir"
    return ViewState(scope=scope)


def _is_under(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or nested beneath it."""
    if child == parent:
        return True
    return child.startswith(parent if parent.endswith("/") else parent + "/")


def _in_scope(session: SessionMeta, view: ViewState, anchor: Anchor) -> bool:
    repo_key = view.repo_filter or anchor.repo_key

    if view.scope == "all":
        return True

    if view.scope == "dir":
        # Sessions that ever worked at or below this directory. Using every cwd
        # rather than just the origin means a session you started in the repo
        # root still shows up when you are down in a subdirectory.
        return any(_is_under(c.path, anchor.cwd) for c in session.cwds) or _is_under(
            session.origin_cwd, anchor.cwd
        )

    if view.scope == "repo":
        if not repo_key:
            return _is_under(session.origin_cwd, anchor.cwd)
        return session.repo_key == repo_key or any(c.repo_key == repo_key for c in session.cwds)

    # branch
    branch = view.branch_filter or anchor.branch
    if not branch:
        # Detached HEAD, or no branch to filter on: fall back to the repo.
        return _in_scope(session, replace(view, scope="repo"), anchor)
    repo_ok = (
        True
        if not repo_key
        else session.repo_key == repo_key or any(c.repo_key == repo_key for c in session.cwds)
    )
    return repo_ok and any(b.name == branch for b in session.branches)


def is_related(session: SessionMeta, ref: SessionMeta) -> bool:
    """Whether ``session`` looks like part of the same task as ``ref``.

    A task rarely lives in one session -- you stop for the day, start fresh
    tomorrow, split work across two windows. What ties those together is
    concrete overlap: the same repo *and* a shared branch or file, or a shared
    file even across repos (you were editing the same thing). Same repo alone is
    too broad to mean "same task".
    """
    if session.id == ref.id:
        return True
    ref_files = set(ref.files)
    shares_file = bool(ref_files) and any(path in ref_files for path in session.files)
    same_repo = bool(ref.repo_key) and session.repo_key == ref.repo_key
    ref_branches = {b.name for b in ref.branches if b.name not in (None, "HEAD")}
    shares_branch = any(b.name in ref_branches for b in session.branches)
    return shares_file or (same_repo and shares_branch)


def _sort_key(sort: str):
    """Return a key function for the given sort order."""
    if sort == "recent":
        return lambda hit: -hit.session.ended_at
    if sort == "oldest":
        return lambda hit: hit.session.ended_at
    if sort == "unfinished":
        # Sessions left mid-action first, most recent of those on top.
        return lambda hit: (0 if hit.session.ended_mid_action else 1, -hit.session.ended_at)
    if sort == "turns":
        return lambda hit: (-hit.session.turns, -hit.session.ended_at)
    if sort == "tokens":
        return lambda hit: (-hit.session.output_tokens, -hit.session.ended_at)
    if sort == "size":
        return lambda hit: (-hit.session.size_bytes, -hit.session.ended_at)
    if sort == "title":
        return lambda hit: (hit.session.ai_title or hit.session.first_prompt).lower()
    # relevance
    return lambda hit: (-hit.score, -hit.session.ended_at)


@dataclass(slots=True)
class ViewResult:
    hits: list[QueryHit] = field(default_factory=list)
    #: How many sessions the scope admitted, before the query narrowed them.
    in_scope_count: int = 0
    total_count: int = 0
    #: The in-scope sessions, so a body search can be limited to them.
    scoped: list[SessionMeta] = field(default_factory=list)


def compute_view(
    sessions: list[SessionMeta],
    view: ViewState,
    anchor: Anchor,
    now: float | None = None,
    ctx: QueryContext | None = None,
) -> ViewResult:
    """Compute the visible list.

    When a query is present the sort silently switches to relevance unless the
    user pinned an explicit order -- a search whose best match is at row 40
    because the list is date-ordered is a search that failed.
    """
    current = time.time() if now is None else now
    parsed: ParsedQuery = parse_query(view.query)

    # "Related to" replaces scope as the membership axis: sibling sessions of a
    # task live in other branches and directories, so honouring scope here would
    # hide exactly what was asked for.
    ref = None
    if view.related_to is not None:
        ref = next((s for s in sessions if s.id == view.related_to), None)

    hits: list[QueryHit] = []
    scoped: list[SessionMeta] = []

    for session in sessions:
        if view.hide_empty and session.turns == 0 and session.live is None:
            continue
        if ref is not None:
            if not is_related(session, ref):
                continue
        elif not _in_scope(session, view, anchor):
            continue
        scoped.append(session)
        hit = evaluate(session, parsed, current, ctx)
        if hit is not None:
            hits.append(hit)

    effective_sort = "relevance" if view.sort == "recent" and parsed.free_text else view.sort

    # Running sessions are not floated to the top: a claude process left open in
    # a terminal for days is not "recent", and hoisting it above a session you
    # touched minutes ago makes the age column read as mis-sorted. One actively
    # working ranks first on its own merits, since its transcript is being
    # written right now; the live dot marks the rest where they belong.
    hits.sort(key=_sort_key(effective_sort))

    return ViewResult(
        hits=hits,
        in_scope_count=len(scoped),
        total_count=len(sessions),
        scoped=scoped,
    )


def branches_in(
    sessions: list[SessionMeta], anchor: Anchor, repo_only: bool
) -> list[tuple[str, int, float]]:
    """Distinct branches present in the given sessions, most recent first."""
    counts: dict[str, int] = {}
    last: dict[str, float] = {}

    for session in sessions:
        if repo_only and anchor.repo_key:
            in_repo = session.repo_key == anchor.repo_key or any(
                c.repo_key == anchor.repo_key for c in session.cwds
            )
            if not in_repo:
                continue
        for branch in session.branches:
            counts[branch.name] = counts.get(branch.name, 0) + branch.count
            last[branch.name] = max(last.get(branch.name, 0.0), branch.last_seen)

    items = [(name, count, last[name]) for name, count in counts.items()]
    items.sort(key=lambda item: -item[2])
    return items


def projects_in(sessions: list[SessionMeta]) -> list[tuple[str, str, int, float]]:
    """Distinct projects (repos, or bare directories) across all sessions."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    last: dict[str, float] = {}

    for session in sessions:
        key = session.repo_key or session.origin_cwd
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        names[key] = session.repo_name or key.rstrip("/").rsplit("/", 1)[-1] or key
        last[key] = max(last.get(key, 0.0), session.ended_at)

    items = [(key, names[key], count, last[key]) for key, count in counts.items()]
    items.sort(key=lambda item: -item[3])
    return items
