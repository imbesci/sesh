"""Terminal session management and key decoding.

Two responsibilities that must not be separated: whatever we turn on has to be
turned back off on *every* exit path. A picker that leaves the terminal in raw
mode with a hidden cursor after a crash is worse than no picker, so teardown is
registered against process exit and signals, and is idempotent.
"""

from __future__ import annotations

import atexit
import os
import re
import select
import signal
import sys
import termios
import tty
from dataclasses import dataclass

from .ansi import Cursor, Paste, Screen

_CTRL_NAMES = {
    1: "a", 2: "b", 3: "c", 4: "d", 5: "e", 6: "f", 7: "g",
    8: "h", 11: "k", 12: "l", 14: "n", 15: "o", 16: "p", 17: "q",
    18: "r", 19: "s", 20: "t", 21: "u", 22: "v", 23: "w", 24: "x",
    25: "y", 26: "z",
}

_CSI_FINAL = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end", "Z": "shift+tab",
}

_CSI_TILDE = {
    "1": "home", "2": "insert", "3": "delete", "4": "end",
    "5": "pageup", "6": "pagedown", "7": "home", "8": "end",
    "15": "f5", "17": "f6", "18": "f7", "19": "f8",
    "20": "f9", "21": "f10", "23": "f11", "24": "f12",
}

_PARAM_RE = re.compile(r"[0-9;?]")


@dataclass(slots=True)
class Key:
    #: Canonical name: "up", "enter", "ctrl+f", "alt+b", or a literal character.
    name: str
    #: The raw printable character, when the key produced one.
    char: str | None = None
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    #: Full pasted text, when this event came from a bracketed paste.
    pasted: str | None = None


def _decode_modifiers(param: str | None) -> tuple[bool, bool, bool]:
    """xterm modifier encoding: 1 + bit flags (1=shift, 2=alt, 4=ctrl)."""
    try:
        bits = int(param) - 1 if param else 0
    except ValueError:
        bits = 0
    return bool(bits & 1), bool(bits & 2), bool(bits & 4)


def decode_keys(data: str) -> tuple[list[Key], str]:
    """Decode a chunk of terminal input into key events.

    Returns the decoded keys and any trailing bytes that form an incomplete
    escape sequence, which the caller should prepend to the next read.

    Written as a pure function over a string so it can be unit-tested without a
    terminal -- escape-sequence bugs are otherwise nearly impossible to pin
    down.
    """
    keys: list[Key] = []
    index = 0
    length = len(data)

    while index < length:
        char = data[index]

        # Bracketed paste: consume to the terminator and emit as one event.
        if data.startswith("\x1b[200~", index):
            end = data.find("\x1b[201~", index)
            if end == -1:
                return keys, data[index:]
            keys.append(Key(name="paste", pasted=data[index + 6 : end]))
            index = end + 6
            continue

        if char == "\x1b":
            if index + 1 >= length:
                # Could be a lone ESC or the start of a sequence; hold it.
                return keys, data[index:]

            nxt = data[index + 1]
            if nxt in ("[", "O"):
                cursor = index + 2
                params = ""
                while cursor < length and _PARAM_RE.match(data[cursor]):
                    params += data[cursor]
                    cursor += 1
                if cursor >= length:
                    return keys, data[index:]  # truncated sequence

                final = data[cursor]
                parts = params.split(";")
                shift, alt, ctrl = _decode_modifiers(parts[1] if len(parts) > 1 else None)

                if final == "~":
                    name = _CSI_TILDE.get(parts[0])
                    if name:
                        keys.append(Key(name=name, ctrl=ctrl, alt=alt, shift=shift))
                else:
                    base = _CSI_FINAL.get(final)
                    if base == "shift+tab":
                        keys.append(Key(name="shift+tab", shift=True))
                    elif base:
                        prefix = ("ctrl+" if ctrl else "") + ("alt+" if alt else "") + ("shift+" if shift else "")
                        keys.append(Key(name=prefix + base, ctrl=ctrl, alt=alt, shift=shift))
                index = cursor + 1
                continue

            # ESC-prefixed key = Alt+key.
            code = ord(nxt)
            if code == 127:
                keys.append(Key(name="alt+backspace", alt=True))
            elif code < 32:
                base = _CTRL_NAMES.get(code)
                keys.append(Key(name=f"ctrl+alt+{base}" if base else "unknown", ctrl=True, alt=True))
            else:
                keys.append(Key(name=f"alt+{nxt.lower()}", char=nxt, alt=True, shift=nxt != nxt.lower()))
            index += 2
            continue

        code = ord(char)
        if code in (13, 10):
            keys.append(Key(name="enter"))
        elif code == 9:
            keys.append(Key(name="tab"))
        elif code in (127, 8):
            keys.append(Key(name="backspace"))
        elif code < 32:
            base = _CTRL_NAMES.get(code)
            keys.append(Key(name=f"ctrl+{base}" if base else "unknown", ctrl=True))
        else:
            keys.append(Key(name=char, char=char))
        index += 1

    return keys, ""


@dataclass(slots=True)
class TerminalSize:
    rows: int
    cols: int


class Terminal:
    """A real terminal in raw mode, on the alternate screen."""

    def __init__(self) -> None:
        self._active = False
        self._saved: list | None = None
        self._pending = ""
        self._resized = False
        self._previous_winch = None

    def size(self) -> TerminalSize:
        try:
            columns, lines = os.get_terminal_size(sys.stdout.fileno())
            return TerminalSize(rows=lines or 24, cols=columns or 80)
        except OSError:
            return TerminalSize(rows=24, cols=80)

    def start(self) -> None:
        if self._active:
            return
        self._active = True

        if sys.stdin.isatty():
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())

        sys.stdout.write(Screen.alt_on + Screen.clear + Cursor.hide + Paste.on)
        sys.stdout.flush()

        atexit.register(self.stop)
        try:
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        except (ValueError, OSError):
            self._previous_winch = None

    def _on_winch(self, *_args) -> None:
        self._resized = True

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False

        sys.stdout.write(Paste.off + Cursor.show + Screen.alt_off)
        sys.stdout.flush()

        if self._saved is not None and sys.stdin.isatty():
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            except termios.error:
                pass
            self._saved = None

        if self._previous_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._previous_winch)
            except (ValueError, OSError):
                pass
            self._previous_winch = None

        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    def take_resize(self) -> bool:
        """Consume a pending resize notification, if any."""
        was = self._resized
        self._resized = False
        return was

    def read_keys(self, timeout: float | None = None) -> list[Key]:
        """Block for up to ``timeout`` seconds and decode whatever arrives.

        An unterminated escape sequence is carried into the next read, so a
        multi-byte key split across two reads still decodes correctly.
        """
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            return []
        if not ready:
            return []

        try:
            chunk = os.read(sys.stdin.fileno(), 4096)
        except (OSError, InterruptedError):
            return []
        if not chunk:
            return []

        text = self._pending + chunk.decode("utf8", errors="replace")
        keys, self._pending = decode_keys(text)
        return keys

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
