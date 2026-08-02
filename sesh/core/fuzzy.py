"""Fuzzy subsequence matching with positional scoring.

Modelled on fzf's algorithm because its ranking behaviour is what people have
internalised: matches at word starts beat matches mid-word, runs of adjacent
characters beat scattered ones, and a short haystack beats a long one for the
same match.

Returns matched indices so the UI can highlight exactly what matched -- a user
who cannot see *why* a row matched cannot refine their query.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SCORE_MATCH = 16
BONUS_BOUNDARY = 8  # match right after a separator, e.g. "-e" in "plan-editor"
BONUS_CAMEL = 7  # lower->upper transition
BONUS_CONSECUTIVE = 8  # adjacent to the previous match
BONUS_FIRST_CHAR = 12  # match at index 0
PENALTY_GAP_START = -3
PENALTY_GAP_EXTEND = -1

_SEPARATORS = frozenset(" -_/.:,\\")


@dataclass(slots=True)
class Match:
    score: int
    #: Indices into the haystack that matched, ascending.
    positions: list[int]


def _within_span_limit(needle_length: int, span: int) -> bool:
    """How far matched characters may spread before a match stops counting.

    Pure subsequence matching is too permissive when used to *filter* rather
    than merely rank. Typing "parser" happily matches a path like
    ``/home/a·l·i·ce/dev/p·r·oj/s·rc/wid·get·s.py`` by scattering six letters
    across forty characters, and the user gets a list full of rows they cannot
    explain. Requiring the match to stay reasonably compact removes that noise
    while leaving genuine abbreviations ("pe" -> "plan-editor") intact.
    """
    return span <= max(needle_length * 3, needle_length + 8)


def fuzzy_match(haystack: str, needle: str) -> Match | None:
    """Score ``needle`` against ``haystack``, case-insensitively.

    Uses a greedy forward pass to find the earliest match, then a backward pass
    to slide each matched character as late as possible without breaking order.
    The backward pass is what makes the highlights land on the intended word --
    greedy-only matching produces visibly wrong ones.
    """
    if not needle:
        return Match(score=0, positions=[])
    if not haystack:
        return None

    hay = haystack.lower()
    need = needle.lower()
    if len(need) > len(hay):
        return None

    # Forward pass: earliest possible subsequence.
    positions: list[int] = []
    cursor = 0
    for char in need:
        found = hay.find(char, cursor)
        if found == -1:
            return None
        positions.append(found)
        cursor = found + 1

    # Backward pass: push each match as late as it can go, maximising
    # consecutive runs and tending to land on the intended word.
    limit = len(hay) - 1
    for i in range(len(need) - 1, -1, -1):
        char = need[i]
        best = positions[i]
        for j in range(limit, best, -1):
            if hay[j] == char:
                best = j
                break
        positions[i] = best
        limit = best - 1

    span = positions[-1] - positions[0] + 1
    if not _within_span_limit(len(need), span):
        return None

    score = 0
    previous = -2
    for index in positions:
        score += SCORE_MATCH

        if index == 0:
            score += BONUS_FIRST_CHAR
        else:
            before = haystack[index - 1]
            current = haystack[index]
            if before in _SEPARATORS:
                score += BONUS_BOUNDARY
            elif before.islower() and current.isupper():
                score += BONUS_CAMEL

        if index == previous + 1:
            score += BONUS_CONSECUTIVE
        elif previous >= 0:
            gap = index - previous - 1
            score += PENALTY_GAP_START + PENALTY_GAP_EXTEND * (gap - 1)
        previous = index

    # Prefer tighter matches in shorter fields, so a hit in a 20-char title
    # outranks the same hit buried in a 4000-char prompt blob.
    score -= int(math.log2(len(haystack) + 1))
    return Match(score=score, positions=positions)


def substring_match(haystack: str, needle: str) -> Match | None:
    """Case-insensitive substring match, reported in the same shape."""
    if not needle:
        return Match(score=0, positions=[])
    index = haystack.lower().find(needle.lower())
    if index == -1:
        return None
    positions = list(range(index, index + len(needle)))
    boundary = BONUS_BOUNDARY if index == 0 or haystack[index - 1] in _SEPARATORS else 0
    # Exact substrings are a stronger signal than a scattered fuzzy hit.
    return Match(score=len(needle) * (SCORE_MATCH + BONUS_CONSECUTIVE) + boundary, positions=positions)
