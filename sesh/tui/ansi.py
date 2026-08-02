"""ANSI helpers and the colour palette.

Colours are 256-colour indices rather than truecolor so the UI stays legible
under terminals that remap the low 16 and under the common dark/light split.
Everything routes through :func:`paint`, which becomes a no-op when colour is
disabled, so the rest of the code never has to branch on it.
"""

from __future__ import annotations

import os
import re
import unicodedata

ESC = "\x1b"
CSI = f"{ESC}["
RESET = f"{CSI}0m"

_color_enabled = True


def set_color_enabled(value: bool) -> None:
    global _color_enabled
    _color_enabled = value


def color_supported() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return os.environ.get("TERM") != "dumb"


class Cursor:
    hide = f"{CSI}?25l"
    show = f"{CSI}?25h"
    home = f"{CSI}H"

    @staticmethod
    def to(row: int, col: int) -> str:
        return f"{CSI}{row + 1};{col + 1}H"


class Screen:
    alt_on = f"{CSI}?1049h"
    alt_off = f"{CSI}?1049l"
    clear = f"{CSI}2J"
    clear_line = f"{CSI}2K"


class Paste:
    """Bracketed paste, so a pasted session id is not 36 keystrokes."""

    on = f"{CSI}?2004h"
    off = f"{CSI}?2004l"


def paint(
    text: str,
    fg: int | None = None,
    bg: int | None = None,
    bold: bool = False,
    dim: bool = False,
    italic: bool = False,
    underline: bool = False,
) -> str:
    if not _color_enabled or not text:
        return text
    codes: list[str] = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if italic:
        codes.append("3")
    if underline:
        codes.append("4")
    if fg is not None:
        codes.append(f"38;5;{fg}")
    if bg is not None:
        codes.append(f"48;5;{bg}")
    if not codes:
        return text
    return f"{CSI}{';'.join(codes)}m{text}{RESET}"


class Theme:
    """Named by role rather than colour, so meaning survives a retheme."""

    accent = 39  # cyan -- the current selection, the active scope
    accent_dim = 31
    match = 214  # orange -- search-match highlights
    live = 41  # green -- running sessions
    warn = 214
    danger = 203
    title = 255
    text = 250
    muted = 244
    faint = 240
    rule = 238
    sel_bg = 236
    branch = 141  # purple
    repo = 79  # teal
    time = 109


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _char_width(char: str) -> int:
    code = ord(char)
    if code == 0x200D:  # zero-width joiner: merges the next glyph into this one
        return -2
    if unicodedata.combining(char) or code in (0xFE0F, 0xFE0E):
        return 0
    if 0x200B <= code <= 0x200F or 0xE0000 <= code <= 0xE007F:
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    # Emoji outside the East Asian tables still occupy two columns.
    if 0x1F300 <= code <= 0x1FAFF:
        return 2
    return 1


def display_width(text: str) -> int:
    """Columns a string occupies.

    Terminal layout is column-based, so treating a CJK glyph or an emoji as one
    column silently corrupts every column to its right.
    """
    return max(0, sum(_char_width(c) for c in strip_ansi(text)))


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate to ``width`` display columns, appending an ellipsis when cut."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    budget = width - display_width(ellipsis)
    if budget <= 0:
        return ellipsis[:width]

    out: list[str] = []
    used = 0
    for char in strip_ansi(text):
        char_width = _char_width(char)
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return "".join(out) + ellipsis


def fit(text: str, width: int) -> str:
    """Pad to exactly ``width`` display columns, truncating when too long."""
    result = truncate(text, width)
    padding = width - display_width(result)
    return result + " " * padding if padding > 0 else result


def two_column(left: str, right: str, width: int) -> str:
    """Build a row from left/right segments, padding the gap to full width."""
    gap = width - display_width(left) - display_width(right)
    return left + " " * gap + right if gap > 0 else left
