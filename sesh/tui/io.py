"""The seam between the picker and the physical terminal.

Everything interesting about this tool -- scope widening, cursor tracking across
variable-height rows, overlay behaviour -- lives in the interaction layer, which
is exactly the layer that is impossible to test through a real TTY. Defining the
terminal and the frame buffer as protocols lets the tests drive the same code
path the user does and assert on the painted frame, instead of settling for
testing the data layer and hoping the UI works.
"""

from __future__ import annotations

from typing import Protocol

from .ansi import strip_ansi
from .term import Key, TerminalSize


class TerminalLike(Protocol):
    def size(self) -> TerminalSize: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def take_resize(self) -> bool: ...
    def read_keys(self, timeout: float | None = None) -> list[Key]: ...


class ScreenLike(Protocol):
    def resize(self, rows: int, cols: int) -> None: ...
    def invalidate(self) -> None: ...
    def render(self, lines: list[str]) -> None: ...


class HeadlessTerminal:
    """An in-memory terminal that accepts synthetic keys."""

    def __init__(self, rows: int = 40, cols: int = 140) -> None:
        self._size = TerminalSize(rows=rows, cols=cols)
        self._queue: list[Key] = []
        self._resized = False
        self.started = False
        self.stopped = False

    def size(self) -> TerminalSize:
        return self._size

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def take_resize(self) -> bool:
        was = self._resized
        self._resized = False
        return was

    def read_keys(self, timeout: float | None = None) -> list[Key]:
        keys = self._queue
        self._queue = []
        return keys

    # --- test driving --------------------------------------------------------

    def queue(self, name: str, **extra) -> None:
        """Queue a key by canonical name, e.g. "ctrl+f", "down", or "a"."""
        self._queue.append(
            Key(
                name=name,
                char=name if len(name) == 1 else None,
                ctrl=name.startswith("ctrl+"),
                alt=name.startswith("alt+"),
                **extra,
            )
        )

    def resize(self, rows: int, cols: int) -> None:
        self._size = TerminalSize(rows=rows, cols=cols)
        self._resized = True


class CaptureFrameBuffer:
    """A frame buffer that keeps the last painted frame instead of writing it."""

    def __init__(self) -> None:
        self.frames = 0
        self.last_frame: list[str] = []

    def resize(self, rows: int, cols: int) -> None:
        pass

    def invalidate(self) -> None:
        pass

    def render(self, lines: list[str]) -> None:
        self.frames += 1
        self.last_frame = list(lines)

    # --- assertions ----------------------------------------------------------

    def plain(self) -> list[str]:
        """The frame with styling removed, for readable assertions."""
        return [strip_ansi(line) for line in self.last_frame]

    def text(self) -> str:
        return "\n".join(self.plain())

    def left_pane(self) -> list[str]:
        """Only the list pane, so preview text cannot satisfy a list assertion."""
        return [line.split("│")[0] for line in self.plain()]

    def selected_row(self) -> str:
        """The list row currently carrying the selection marker."""
        for line in self.left_pane():
            if "▌" in line:
                return line
        return ""
