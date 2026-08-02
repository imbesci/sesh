"""The query language.

A single text box has to serve two very different needs: "I vaguely remember
this session" (fuzzy, forgiving) and "show me exactly the sessions on this
branch touching this file" (precise, composable). Rather than mode-switching
between them, typed terms are fuzzy and ``field:value`` terms are exact filters,
so a query can start vague and be tightened word by word without ever retyping
it in another syntax.

Grammar (whitespace-separated, all conjunctive)::

    foo              fuzzy match across title, prompts, repo, branch, path
    'foo             exact substring match
    !foo             negation -- exclude matches
    branch:main  b:  branch (substring, case-insensitive)
    repo:api     r:  repository name or path
    dir:src      d:  any working directory the session used
    file:a.py    f:  a file the session edited or read
    tool:Bash    t:  a tool the session invoked
    model:opus   m:  model id
    id:3f9           session id prefix
    after:2026-07-01 / before:2026-08-01   absolute date bounds
    age:7d           touched within the last 7 days (s/m/h/d/w/y)
    turns:>5         numeric comparison (also records:, tokens:)
    text:"..."       full-text search inside transcript bodies
    is:live          currently running; also compacted/subagents/git/empty
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from .fuzzy import Match, fuzzy_match, substring_match
from .types import SessionMeta


@dataclass(slots=True)
class QueryTerm:
    field: str | None
    value: str
    negated: bool
    exact: bool


@dataclass(slots=True)
class ParsedQuery:
    terms: list[QueryTerm] = field(default_factory=list)
    #: The free-text portion, used for fuzzy scoring and highlighting.
    free_text: str = ""


FIELD_ALIASES: dict[str, str] = {
    "b": "branch", "branch": "branch",
    "r": "repo", "repo": "repo",
    "d": "dir", "dir": "dir", "path": "dir", "cwd": "dir",
    "f": "file", "file": "file",
    "t": "tool", "tool": "tool",
    "m": "model", "model": "model",
    "id": "id",
    "after": "after", "since": "after",
    "before": "before", "until": "before",
    "age": "age",
    "turns": "turns", "records": "records", "tokens": "tokens",
    "is": "is", "in": "is",
    "text": "text", "content": "text", "grep": "text", "body": "text",
}

_TOKEN_RE = re.compile(r'(?:[^\s"]+|"[^"]*")+')
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdwy])?$", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"^\d+[smhdwy]$", re.IGNORECASE)
_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)([km])?$", re.IGNORECASE)

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def parse_query(text: str) -> ParsedQuery:
    terms: list[QueryTerm] = []
    free: list[str] = []

    for raw_token in _TOKEN_RE.findall(text):
        token = raw_token
        negated = token.startswith("!")
        if negated:
            token = token[1:]
        exact = token.startswith("'")
        if exact:
            token = token[1:]
        if not token:
            continue

        colon = token.find(":")
        if colon > 0:
            key = token[:colon].lower()
            name = FIELD_ALIASES.get(key)
            if name:
                value = token[colon + 1 :].strip('"')
                if value:
                    terms.append(QueryTerm(field=name, value=value, negated=negated, exact=exact))
                    continue

        value = token.strip('"')
        if not value:
            continue
        terms.append(QueryTerm(field=None, value=value, negated=negated, exact=exact))
        if not negated:
            free.append(value)

    return ParsedQuery(terms=terms, free_text=" ".join(free))


def deep_terms(query: ParsedQuery) -> list[str]:
    """The free-text terms that require a transcript-body search."""
    return [t.value for t in query.terms if t.field == "text" and not t.negated]


def _parse_duration(value: str) -> float | None:
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "d").lower()
    factor = _DURATION_UNITS.get(unit)
    return amount * factor if factor else None


def _parse_date_bound(value: str, now: float) -> float | None:
    """Parse a date bound; accepts YYYY-MM-DD and relative durations."""
    text = value.strip()
    if _RELATIVE_RE.match(text):
        duration = _parse_duration(text)
        return now - duration if duration is not None else None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _numeric_compare(actual: float, expression: str) -> bool:
    """Evaluate ``>5``, ``<10``, ``>=3``, or a bare number (treated as >=)."""
    match = _COMPARE_RE.match(expression.strip())
    if not match:
        return False
    target = float(match.group(2))
    suffix = (match.group(3) or "").lower()
    if suffix == "k":
        target *= 1_000
    elif suffix == "m":
        target *= 1_000_000

    operator = match.group(1)
    if operator == ">":
        return actual > target
    if operator == "<":
        return actual < target
    if operator == ">=":
        return actual >= target
    if operator == "<=":
        return actual <= target
    if operator == "=":
        return actual == target
    return actual >= target


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(haystack) and needle.lower() in haystack.lower()  # type: ignore[union-attr]


@dataclass(slots=True)
class QueryContext:
    """Out-of-band inputs the evaluator needs but cannot compute itself.

    Body search is asynchronous, so its results arrive separately. While a
    search is still running ``deep_hits`` is None and ``text:`` terms pass,
    which keeps the list stable instead of blanking it on every keystroke.
    """

    deep_hits: dict[str, str] | None = None


def _haystacks(session: SessionMeta) -> list[str]:
    """Text a free-text term is matched against.

    Ordered cheapest-and-most-relevant first so a title hit outranks a prompt
    hit for the same term.
    """
    out: list[str] = []
    if session.ai_title:
        out.append(session.ai_title)
    if session.first_prompt:
        out.append(session.first_prompt)
    if session.repo_name:
        out.append(session.repo_name)
    if session.last_branch:
        out.append(session.last_branch)
    out.append(session.origin_cwd)
    if session.last_prompt:
        out.append(session.last_prompt)
    # Where the session ended up -- lets "the one where it committed the auth
    # fix" match on Claude's own last words, not just what you typed.
    if session.last_assistant_text:
        out.append(session.last_assistant_text)
    if session.last_action:
        out.append(session.last_action)
    return out


def _matches_field(session: SessionMeta, term: QueryTerm, now: float, ctx: QueryContext | None) -> bool:
    value = term.value
    name = term.field

    if name == "text":
        if ctx is None or ctx.deep_hits is None:
            return True
        return session.file in ctx.deep_hits
    if name == "branch":
        return any(_contains(b.name, value) for b in session.branches)
    if name == "repo":
        return _contains(session.repo_name, value) or _contains(session.repo_root, value)
    if name == "dir":
        return any(_contains(c.path, value) for c in session.cwds)
    if name == "file":
        return any(_contains(f, value) for f in session.files)
    if name == "tool":
        return any(_contains(t.name, value) for t in session.tools)
    if name == "model":
        return any(_contains(m, value) for m in session.models)
    if name == "id":
        return session.id.lower().startswith(value.lower())
    if name == "after":
        bound = _parse_date_bound(value, now)
        return True if bound is None else session.ended_at >= bound
    if name == "before":
        bound = _parse_date_bound(value, now)
        return True if bound is None else session.ended_at <= bound
    if name == "age":
        duration = _parse_duration(value)
        return True if duration is None else now - session.ended_at <= duration
    if name == "turns":
        return _numeric_compare(session.turns, value)
    if name == "records":
        return _numeric_compare(session.records, value)
    if name == "tokens":
        return _numeric_compare(session.output_tokens, value)
    if name == "is":
        flag = value.lower()
        if flag in ("live", "running"):
            return session.live is not None
        if flag in ("git", "repo"):
            return session.repo_root is not None
        if flag == "compacted":
            return session.compacted
        if flag == "subagents":
            return session.has_subagents
        if flag == "empty":
            return session.turns == 0
        if flag in ("unfinished", "wip"):
            return session.ended_mid_action
        if flag == "fork":
            return session.forked_from is not None
        return True
    return True


@dataclass(slots=True)
class Highlight:
    text: str
    positions: list[int]


@dataclass(slots=True)
class QueryHit:
    session: SessionMeta
    score: int
    #: Best-matching text and its highlight positions, for the list row.
    highlight: Highlight | None


def evaluate(
    session: SessionMeta,
    query: ParsedQuery,
    now: float | None = None,
    ctx: QueryContext | None = None,
) -> QueryHit | None:
    """Apply a parsed query to a session, or None when it is excluded.

    Free-text terms are matched against several fields and the best hit wins,
    which means typing a repo name, a branch, or a half-remembered phrase from a
    prompt all work without the user having to say which one they meant.
    """
    current = time.time() if now is None else now
    score = 0
    highlight: Highlight | None = None

    for term in query.terms:
        if term.field:
            ok = _matches_field(session, term, current, ctx)
            if ok == term.negated:
                return None
            # Field matches contribute a flat bonus; they are filters, not
            # rankings.
            if not term.negated:
                score += 20
            # Show the line the body search actually hit -- otherwise a `text:`
            # match gives no clue where in a huge transcript it landed.
            if term.field == "text" and not term.negated and highlight is None and ctx and ctx.deep_hits:
                snippet = ctx.deep_hits.get(session.file)
                if snippet:
                    index = snippet.lower().find(term.value.lower())
                    highlight = Highlight(
                        text=snippet,
                        positions=[] if index == -1 else list(range(index, index + len(term.value))),
                    )
            continue

        matcher = substring_match if term.exact else fuzzy_match
        best: Match | None = None
        best_text = ""

        for text in _haystacks(session):
            found = matcher(text, term.value)
            if found and (best is None or found.score > best.score):
                best = found
                best_text = text

        # Prompts are searched separately and slightly discounted: a hit deep in
        # a long conversation is real, but a hit in the title is a better
        # answer.
        if best is None or best.score < 40:
            for prompt in session.prompts:
                found = matcher(prompt.text, term.value)
                if found:
                    discounted = Match(score=found.score - 6, positions=found.positions)
                    if best is None or discounted.score > best.score:
                        best = discounted
                        best_text = prompt.text

        # Files are a last resort, and only their basenames are fuzzy-matched.
        # Matching whole paths would let any query find almost anything, since a
        # deep path offers dozens of incidental letters to match against.
        if best is None:
            for path in session.files:
                base = path.rsplit("/", 1)[-1]
                found = matcher(base, term.value)
                if found and (best is None or found.score > best.score):
                    offset = len(path) - len(base)
                    best = Match(score=found.score - 12, positions=[p + offset for p in found.positions])
                    best_text = path

        if term.negated:
            if best is not None:
                return None
            continue
        if best is None:
            return None

        score += best.score
        if highlight is None:
            highlight = Highlight(text=best_text, positions=best.positions)

    return QueryHit(session=session, score=score, highlight=highlight)
