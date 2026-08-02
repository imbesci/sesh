"""Human-facing formatting shared by the list, the preview and the CLI."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


def _round1(value: float) -> str:
    """Format to one decimal place, rounding halves away from zero.

    Python's default float formatting rounds halves to even, so 1.25 renders as
    "1.2" -- which reads as a bug in a size column, since the value is visibly
    above 1.25. Rounding half-up matches what people expect from a magnitude
    readout (and, incidentally, what the original TypeScript did).
    """
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def relative_time(timestamp: float, now: float | None = None) -> str:
    """Compact relative time: "3m", "2h", "5d", "3w", "1y".

    Deliberately unit-suffixed and short rather than "3 minutes ago" -- this
    column is scanned vertically dozens of rows at a time, and alignment matters
    more than grammar.
    """
    current = time.time() if now is None else now
    seconds = max(0.0, current - timestamp)
    if seconds < 45:
        return "now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    if days < 365:
        weeks = days // 7
        return f"{weeks}w" if weeks < 5 else f"{days // 30}mo"
    return f"{days // 365}y"


def absolute_time(timestamp: float) -> str:
    """Absolute timestamp for the detail pane."""
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def duration(start: float, end: float) -> str:
    """Duration between two instants, e.g. "1h 24m"."""
    seconds = max(0.0, end - start)
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rest = minutes % 60
    if hours < 24:
        return f"{hours}h {rest}m" if rest else f"{hours}h"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def bytes_(count: int) -> str:
    """Byte counts as "1.2M"."""
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        kib = count / 1024
        return f"{_round1(kib)}K" if count < 10240 else f"{kib:.0f}K"
    mib = count / (1024 * 1024)
    return f"{_round1(mib)}M" if count < 10 * 1024 * 1024 else f"{mib:.0f}M"


def number(count: int) -> str:
    """Token and item counts as "1.2M" / "88k"."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        thousands = count / 1000
        return f"{_round1(thousands)}k" if count < 10_000 else f"{thousands:.0f}k"
    return f"{_round1(count / 1_000_000)}M"


def short_model(model: str) -> str:
    """Shorten a model id to something that fits a column: "opus-5"."""
    name = model.removeprefix("claude-")
    name = name.removesuffix("-latest")
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        name = parts[0]
    return name


def short_path(path: str, home: str, max_width: int = 0) -> str:
    """Shorten a path for display.

    Collapses ``$HOME`` to ``~`` and, when still too long, elides *leading*
    segments -- the tail of a path is what identifies it.
    """
    if path == home:
        out = "~"
    elif path.startswith(home + "/"):
        out = "~" + path[len(home) :]
    else:
        out = path

    if max_width > 0 and len(out) > max_width:
        parts = out.split("/")
        while len(parts) > 2 and len("/".join(parts)) > max_width - 2:
            parts.pop(0)
        out = "…/" + "/".join(parts)
    return out
