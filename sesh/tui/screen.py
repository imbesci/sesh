"""A line-diffing frame buffer.

Repainting the whole screen on every keystroke produces visible flicker and,
over SSH, noticeable lag -- and this UI repaints on *every* character typed into
the filter box. Since the layout is line-oriented, comparing rendered rows
against the previous frame and rewriting only what changed reduces a typical
keystroke to a handful of bytes.
"""

from __future__ import annotations

import sys

from .ansi import Cursor, Screen as ScreenCodes


class FrameBuffer:
    def __init__(self) -> None:
        self._previous: list[str] = []
        self._rows = 0
        self._cols = 0
        self._force_full = True

    def resize(self, rows: int, cols: int) -> None:
        if rows != self._rows or cols != self._cols:
            self._rows = rows
            self._cols = cols
            self._force_full = True

    def invalidate(self) -> None:
        """Force the next render to repaint everything."""
        self._force_full = True

    def render(self, lines: list[str]) -> None:
        out: list[str] = []

        if self._force_full:
            out.append(ScreenCodes.clear)
            out.append(Cursor.home)
            self._previous = []
            self._force_full = False

        count = min(len(lines), self._rows)
        for i in range(count):
            line = lines[i]
            if i < len(self._previous) and self._previous[i] == line:
                continue
            out.append(Cursor.to(i, 0))
            out.append(ScreenCodes.clear_line)
            out.append(line)
            if i < len(self._previous):
                self._previous[i] = line
            else:
                self._previous.append(line)

        # Clear any rows the new frame no longer uses.
        for i in range(count, min(len(self._previous), self._rows)):
            if self._previous[i] == "":
                continue
            out.append(Cursor.to(i, 0))
            out.append(ScreenCodes.clear_line)
            self._previous[i] = ""

        del self._previous[count:]

        if out:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
