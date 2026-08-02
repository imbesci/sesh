"""The interactive picker: state, key handling, and the event loop."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace

from ..core.actions import ActionError, TrashEntry, copy_to_clipboard, restore_trash, trash_session
from ..core.deep_search import deep_search
from ..core.format import relative_time, short_path
from ..core.index_store import refresh_sessions
from ..core.query import QueryContext, deep_terms, parse_query
from ..core.resume import ResumeError, ResumePlan, plan_resume, plan_to_shell, skip_permissions_default
from ..core.transcript import TranscriptEntry, load_transcript
from ..core.types import SessionMeta
from ..core.view import (
    SCOPE_ORDER,
    SORT_ORDER,
    Anchor,
    ViewResult,
    ViewState,
    branches_in,
    compute_view,
    default_view,
    projects_in,
)
from .ansi import Theme, color_supported, fit, paint, set_color_enabled, truncate, two_column
from .io import ScreenLike, TerminalLike
from .render import (
    compute_layout,
    render_chips,
    render_header,
    render_input,
    render_preview,
    render_rows,
    rule,
    session_label,
    wrap_text,
)
from .screen import FrameBuffer
from .term import Key, Terminal

HOME = os.path.expanduser("~")

#: Minimum terminal width before the preview pane is worth showing.
PREVIEW_MIN_COLS = 104
#: Width of the list pane as a fraction of the terminal, when split.
LIST_FRACTION = 0.52
#: How often to re-check for new/changed sessions while the picker is open.
REFRESH_SECONDS = 4.0
#: Quiet period before a `text:` query hits the disk.
DEEP_SEARCH_DEBOUNCE = 0.18
#: How long the loop blocks waiting for input before doing periodic work.
POLL_SECONDS = 0.2


@dataclass(slots=True)
class PickerItem:
    value: str
    label: str
    hint: str = ""


@dataclass
class Overlay:
    """A modal layered over the list."""

    kind: str  # "help" | "picker" | "confirm" | "viewer"
    title: str = ""
    items: list[PickerItem] = field(default_factory=list)
    cursor: int = 0
    filter: str = ""
    on_pick = None
    text: str = ""
    detail: list[str] = field(default_factory=list)
    on_yes = None
    session: SessionMeta | None = None
    entries: list[TranscriptEntry] = field(default_factory=list)
    scroll: int = 0
    loading: bool = False
    typing: bool = False


@dataclass(slots=True)
class AppResult:
    """What the picker asks the caller to do after it exits."""

    kind: str  # "quit" | "resume"
    plan: ResumePlan | None = None
    session: SessionMeta | None = None


class App:
    def __init__(
        self,
        sessions: list[SessionMeta],
        anchor: Anchor,
        initial_query: str = "",
        terminal: TerminalLike | None = None,
        screen: ScreenLike | None = None,
        auto_refresh: bool = True,
        skip_permissions: bool | None = None,
    ) -> None:
        self.terminal: TerminalLike = terminal if terminal is not None else Terminal()
        self.screen: ScreenLike = screen if screen is not None else FrameBuffer()
        self.auto_refresh = auto_refresh
        self.skip_permissions = skip_permissions if skip_permissions is not None else skip_permissions_default()

        self.sessions = sessions
        self.anchor = anchor
        self.view = default_view(anchor)
        self.view.query = initial_query

        self.query_cursor = len(initial_query)
        self.cursor = 0
        self.scroll = 0
        self.preview_scroll = 0
        self.show_preview = True
        self.overlay: Overlay | None = None
        self.message: tuple[str, str] | None = None
        self._message_until = 0.0
        self._last_trash: TrashEntry | None = None
        self.now = time.time()
        self.dirty = True
        self.action: AppResult | None = None
        self.rows = 24
        self.cols = 80

        self._viewer_thinking = False
        self._viewer_sidechains = False

        self._deep_hits: dict[str, str] | None = None
        self._deep_key = ""
        self._deep_pending = False
        self._deep_deadline = 0.0
        self._deep_cancel: threading.Event | None = None
        self._deep_thread: threading.Thread | None = None
        self._deep_result: tuple[str, dict[str, str]] | None = None
        self._deep_lock = threading.Lock()
        self._last_refresh = time.monotonic()

        self.result: ViewResult = compute_view(self.sessions, self.view, self.anchor, self.now)

        # Starting scoped to a branch is only helpful if that branch has
        # sessions. Silently widening beats greeting the user with an empty list
        # they have to diagnose.
        self._widen_until_non_empty()

    # ---------------------------------------------------------------- data ---

    def _widen_until_non_empty(self) -> None:
        guard = 0
        while not self.result.hits and guard < len(SCOPE_ORDER):
            guard += 1
            index = SCOPE_ORDER.index(self.view.scope)
            if index >= len(SCOPE_ORDER) - 1:
                break
            self.view.scope = SCOPE_ORDER[index + 1]
            self.recompute()

    def recompute(self) -> None:
        self.now = time.time()
        self.result = compute_view(
            self.sessions, self.view, self.anchor, self.now, QueryContext(deep_hits=self._deep_hits)
        )
        if self.cursor >= len(self.result.hits):
            self.cursor = max(0, len(self.result.hits) - 1)
        self.preview_scroll = 0
        self.dirty = True
        self._schedule_deep_search()

    def current(self) -> SessionMeta | None:
        if 0 <= self.cursor < len(self.result.hits):
            return self.result.hits[self.cursor].session
        return None

    # -------------------------------------------------------- body search ---

    def _schedule_deep_search(self) -> None:
        """Debounce and dispatch a transcript-body search.

        Grepping transcripts is orders of magnitude more expensive than
        filtering the index, so it must never run per keystroke, and an
        in-flight search must be abandoned the moment the query changes --
        otherwise a slow search from three characters ago lands after a fast one
        and silently overwrites it.
        """
        terms = deep_terms(parse_query(self.view.query))

        # The scope is part of the cache key. A search only covers the sessions
        # it ran against, so widening from branch to all has to re-search rather
        # than reuse a result set that never saw the newly-included transcripts.
        key = (
            " ".join([*terms, self.view.scope, self.view.branch_filter or "", self.view.repo_filter or ""])
            if terms
            else ""
        )

        if not terms:
            self._deep_pending = False
            if self._deep_key:
                self._deep_key = ""
                self._deep_hits = None
                self._cancel_deep()
            return

        if key == self._deep_key:
            return

        self._deep_key = key
        self._deep_hits = None
        self._deep_pending = True
        self._deep_deadline = time.monotonic() + DEEP_SEARCH_DEBOUNCE
        self._cancel_deep()

    def _cancel_deep(self) -> None:
        if self._deep_cancel is not None:
            self._deep_cancel.set()
        self._deep_cancel = None
        self._deep_thread = None

    def _start_deep_search_if_due(self) -> None:
        if not self._deep_pending or self._deep_thread is not None:
            return
        if time.monotonic() < self._deep_deadline:
            return

        key = self._deep_key
        terms = deep_terms(parse_query(self.view.query))
        scoped = list(self.result.scoped)
        cancel = threading.Event()
        self._deep_cancel = cancel

        def work() -> None:
            merged: dict[str, str] | None = None
            for term in terms:
                hits = deep_search(scoped, term, cancel)
                if cancel.is_set():
                    return
                snippets = {path: hit.snippet for path, hit in hits.items()}
                if merged is None:
                    merged = snippets
                else:
                    # Multiple text: terms are conjunctive, like every other
                    # filter.
                    merged = {p: s for p, s in merged.items() if p in snippets}
            if cancel.is_set():
                return
            with self._deep_lock:
                self._deep_result = (key, merged or {})

        thread = threading.Thread(target=work, daemon=True)
        self._deep_thread = thread
        thread.start()

    def _apply_deep_results(self) -> None:
        with self._deep_lock:
            pending = self._deep_result
            self._deep_result = None
        if pending is None:
            return
        key, hits = pending
        if key != self._deep_key:
            return  # a newer query superseded this one
        self._deep_hits = hits
        self._deep_pending = False
        self._deep_thread = None
        self.now = time.time()
        self.result = compute_view(
            self.sessions, self.view, self.anchor, self.now, QueryContext(deep_hits=self._deep_hits)
        )
        if self.cursor >= len(self.result.hits):
            self.cursor = max(0, len(self.result.hits) - 1)
        self.dirty = True

    def settle(self, timeout: float = 5.0) -> None:
        """Wait for any in-flight body search and apply it.

        Exists for tests and for the ``--list`` path, where determinism matters
        more than responsiveness.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._start_deep_search_if_due()
            thread = self._deep_thread
            if thread is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            self._apply_deep_results()
            if not self._deep_pending:
                return
            time.sleep(0.01)

    # ---------------------------------------------------------- main loop ---

    def begin(self) -> None:
        size = self.terminal.size()
        self.rows, self.cols = size.rows, size.cols
        self.screen.resize(self.rows, self.cols)
        self.draw()

    def run(self) -> AppResult:
        set_color_enabled(color_supported())
        self.terminal.start()
        try:
            self.begin()
            while self.action is None:
                for key in self.terminal.read_keys(timeout=POLL_SECONDS):
                    self.handle_key(key)
                    if self.action is not None:
                        break
                if self.action is not None:
                    break

                if self.terminal.take_resize():
                    size = self.terminal.size()
                    self.rows, self.cols = size.rows, size.cols
                    self.screen.resize(self.rows, self.cols)
                    self.dirty = True

                self._start_deep_search_if_due()
                self._apply_deep_results()
                self._expire_message()
                self._maybe_refresh()
                self.draw()
        finally:
            self._cancel_deep()
            self.terminal.stop()
        return self.action or AppResult(kind="quit")

    def _expire_message(self) -> None:
        if self.message is not None and time.monotonic() > self._message_until:
            self.message = None
            self.dirty = True

    def _maybe_refresh(self) -> None:
        """Pick up transcripts that changed while the picker is open.

        The selection is restored by id across the reshuffle -- without that, a
        background refresh would yank the selection out from under someone
        mid-scroll, since sessions reorder by recency and the live one keeps
        moving.
        """
        if not self.auto_refresh:
            return
        if self.overlay is not None and self.overlay.kind == "viewer":
            return
        if time.monotonic() - self._last_refresh < REFRESH_SECONDS:
            return
        self._last_refresh = time.monotonic()

        selected = self.current()
        selected_id = selected.id if selected else None
        try:
            self.sessions = refresh_sessions(self.sessions)
        except OSError:
            return
        self.recompute()
        if selected_id:
            for index, hit in enumerate(self.result.hits):
                if hit.session.id == selected_id:
                    self.cursor = index
                    break

    # ------------------------------------------------------------ drawing ---

    def draw(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        self.now = time.time()

        width = self.cols
        lines: list[str] = [
            render_header(
                self.anchor,
                len(self.result.hits),
                self.result.in_scope_count,
                self.result.total_count,
                width,
            ),
            render_chips(self.view, self.anchor, width),
            render_input(
                self.view.query,
                self.query_cursor,
                width,
                bool(self._deep_key),
                "searching transcripts…" if self._deep_pending else "",
            ),
            rule(width),
        ]

        body_top = len(lines)
        body_height = max(1, self.rows - body_top - 1)

        split = self.show_preview and width >= PREVIEW_MIN_COLS
        list_width = int(width * LIST_FRACTION) if split else width
        preview_width = width - list_width - 1 if split else 0

        self._ensure_cursor_visible(body_height)

        layout = compute_layout(self.result.hits, list_width)
        list_lines = render_rows(
            self.result.hits,
            self.scroll,
            body_height,
            self.cursor,
            list_width,
            layout,
            bool(self.view.query.strip()),
            self.now,
        )

        if not self.result.hits:
            list_lines.append("")
            list_lines.append(paint("  no sessions match", fg=Theme.faint))
            for hint in self._empty_hints():
                list_lines.append(paint(f"  {hint}", fg=Theme.faint))

        preview_lines = (
            render_preview(self.current(), preview_width, body_height, self.preview_scroll, self.now)
            if split
            else []
        )

        for i in range(body_height):
            left = fit(list_lines[i] if i < len(list_lines) else "", list_width)
            if not split:
                lines.append(left)
            else:
                right = preview_lines[i] if i < len(preview_lines) else ""
                lines.append(left + paint("│", fg=Theme.rule) + fit(right, preview_width))

        lines.append(self._render_status(width))

        if self.overlay is not None:
            self._draw_overlay(lines, width)

        self.screen.render(lines)

    def _empty_hints(self) -> list[str]:
        """Guidance when a filter has emptied the list -- always actionable."""
        hints: list[str] = []
        if self.view.query:
            hints.append("esc clears the filter")
        if self.view.scope != "all":
            index = SCOPE_ORDER.index(self.view.scope)
            wider = SCOPE_ORDER[index + 1] if index + 1 < len(SCOPE_ORDER) else "all"
            hints.append(f"tab widens the scope to {wider}")
        if self.view.hide_empty:
            hints.append("ctrl+g shows sessions with no prompts")
        return hints

    def _render_status(self, width: int) -> str:
        if self.message is not None:
            text, kind = self.message
            fg = Theme.danger if kind == "error" else Theme.live if kind == "success" else Theme.accent
            return paint(fit(f" {text}", width), fg=fg)

        keys = [
            ("↵", "resume"), ("^f", "fork"), ("tab", "scope"),
            ("^b", "branch"), ("^p", "project"), ("^v", "view"), ("alt+h", "keys"),
        ]
        left = "".join(paint(f" {k}", fg=Theme.accent) + paint(f" {v}", fg=Theme.faint) for k, v in keys)
        session = self.current()
        right = paint(f"{short_path(session.origin_cwd, HOME, 34)} ", fg=Theme.faint) if session else ""
        return two_column(left, right, width)

    def _ensure_cursor_visible(self, height: int) -> None:
        """Keep the highlighted row on screen.

        Rows are variable height (a match snippet adds a line), so visibility is
        resolved by simulating the render rather than by arithmetic on a fixed
        row height.
        """
        if self.cursor < self.scroll:
            self.scroll = self.cursor
            return

        has_query = bool(self.view.query.strip())

        def height_of(index: int) -> int:
            if not (0 <= index < len(self.result.hits)):
                return 1
            hit = self.result.hits[index]
            secondary = has_query and hit.highlight is not None and hit.highlight.text != session_label(hit.session)
            return 2 if secondary else 1

        while True:
            used = 0
            last = self.scroll
            for index in range(self.scroll, len(self.result.hits)):
                step = height_of(index)
                if used + step > height:
                    break
                used += step
                last = index
            if self.cursor <= last or self.scroll >= len(self.result.hits) - 1:
                break
            self.scroll += 1

    # ----------------------------------------------------------- overlays ---

    def _draw_overlay(self, lines: list[str], width: int) -> None:
        overlay = self.overlay
        assert overlay is not None
        if overlay.kind == "viewer":
            self._draw_viewer(lines, width)
            return

        box_width = min(width - 8, 78 if overlay.kind == "help" else 66)
        left = max(2, (width - box_width) // 2)

        if overlay.kind == "help":
            title = "keys"
            content = self._help_content(box_width)
        elif overlay.kind == "picker":
            title = overlay.title
            content = self._picker_content(overlay, box_width)
        else:
            title = "confirm"
            content = [paint(overlay.text, fg=Theme.title, bold=True), ""]
            content += [paint(truncate(d, box_width - 4), fg=Theme.muted) for d in overlay.detail]
            content.append("")
            content.append(
                paint("  y", fg=Theme.danger, bold=True)
                + paint(" confirm    ", fg=Theme.muted)
                + paint("n/esc", fg=Theme.accent)
                + paint(" cancel", fg=Theme.muted)
            )

        # Content taller than the terminal is clipped rather than allowed to
        # push the box off-screen.
        content = content[: max(1, self.rows - 4)]

        box_height = len(content) + 2
        top = max(0, (self.rows - box_height) // 2)
        pad = " " * left

        def put(row: int, text: str) -> None:
            # Overlay rows replace the underlying row entirely. Splicing styled
            # text into an already-styled line at a display column is not
            # reliably possible once ANSI resets are involved, and a modal has
            # no reason to preserve what is behind it.
            if not 0 <= row < self.rows:
                return
            while len(lines) <= row:
                lines.append("")
            lines[row] = pad + text

        title_text = f" {title} "
        put(
            top,
            paint("╭─", fg=Theme.accent_dim)
            + paint(title_text, fg=Theme.accent, bold=True)
            + paint("─" * max(0, box_width - 3 - len(title_text)) + "╮", fg=Theme.accent_dim),
        )
        for index, row in enumerate(content):
            put(
                top + 1 + index,
                paint("│", fg=Theme.accent_dim) + fit(row, box_width - 2) + paint("│", fg=Theme.accent_dim),
            )
        put(top + box_height - 1, paint("╰" + "─" * (box_width - 2) + "╯", fg=Theme.accent_dim))

    def _help_content(self, box_width: int) -> list[str]:
        # Two-column layout, but only for lines that have two columns -- section
        # headers and the closing note are prose and get the full width.
        key_col = 18
        out: list[str] = []
        for line in HELP_LINES:
            if line.startswith("#"):
                out.append(paint(line[1:], fg=Theme.accent, bold=True))
                continue
            if "\t" not in line:
                out.append(paint(truncate(line, box_width - 4), fg=Theme.muted))
                continue
            keys, _, description = line.partition("\t")
            out.append(
                paint(fit(truncate(keys, key_col - 1), key_col), fg=Theme.title)
                + paint(truncate(description, box_width - 4 - key_col), fg=Theme.muted)
            )
        return out

    def _picker_items(self, overlay: Overlay) -> list[PickerItem]:
        text = overlay.filter.lower()
        if not text:
            return overlay.items
        return [i for i in overlay.items if text in i.label.lower() or text in i.value.lower()]

    def _picker_content(self, overlay: Overlay, box_width: int) -> list[str]:
        visible = self._picker_items(overlay)
        max_rows = max(3, min(self.rows - 10, 16))
        start = max(0, min(overlay.cursor - max_rows // 2, max(0, len(visible) - max_rows)))

        content = [
            paint("❯ ", fg=Theme.accent)
            + paint(overlay.filter or "filter…", fg=Theme.title if overlay.filter else Theme.faint),
            rule(box_width - 2),
        ]
        if not visible:
            content.append(paint("  no matches", fg=Theme.faint))
        for offset, item in enumerate(visible[start : start + max_rows]):
            index = start + offset
            selected = index == overlay.cursor
            marker = paint("▌ ", fg=Theme.accent) if selected else "  "
            label = paint(
                fit(truncate(item.label, box_width - 22), box_width - 22),
                fg=Theme.title if selected else Theme.text,
                bold=selected,
            )
            content.append(marker + label + paint(item.hint, fg=Theme.faint))
        return content

    def _draw_viewer(self, lines: list[str], width: int) -> None:
        overlay = self.overlay
        assert overlay is not None
        height = self.rows - 3

        needle = overlay.filter.lower()
        entries = [e for e in overlay.entries if needle in e.text.lower()] if needle else overlay.entries

        body: list[str] = []
        for entry in entries:
            if entry.role == "user":
                tag, style = "❯", {"fg": Theme.accent, "bold": True}
            elif entry.role == "assistant":
                tag, style = " ", {"fg": Theme.text}
            elif entry.role == "tool":
                tag, style = "·", {"fg": Theme.faint}
            elif entry.role == "thinking":
                tag, style = "~", {"fg": Theme.faint, "italic": True}
            else:
                tag, style = "!", {"fg": Theme.warn}

            prefix = paint(f" {tag} ", fg=Theme.accent if entry.role == "user" else Theme.faint, bold=entry.role == "user")
            for index, chunk in enumerate(wrap_text(entry.text, width - 4)):
                body.append((prefix if index == 0 else "   ") + paint(chunk, **style))
            if entry.role == "user":
                body.append("")

        overlay.scroll = max(0, min(overlay.scroll, max(0, len(body) - height)))

        lines.clear()
        title = truncate(session_label(overlay.session), width - 30) if overlay.session else ""
        lines.append(
            two_column(
                paint(" ⤢ ", fg=Theme.accent, bold=True) + paint(title, fg=Theme.title, bold=True),
                paint(f"{overlay.scroll}/{len(body)} ", fg=Theme.faint),
                width,
            )
        )
        if overlay.typing:
            lines.append(render_input(overlay.filter, len(overlay.filter), width, True))
        else:
            label = "loading…" if overlay.loading else f"{len(entries)} blocks" + (
                f' matching "{overlay.filter}"' if needle else ""
            )
            lines.append(rule(width, label))

        for i in range(height):
            index = overlay.scroll + i
            lines.append(fit(body[index] if index < len(body) else "", width))

        lines.append(
            paint(
                fit(" ↑↓ scroll   pgup/pgdn page   / search   t thinking   s subagents   esc back   ↵ resume", width),
                fg=Theme.faint,
            )
        )

    # ---------------------------------------------------------- key input ---

    def flash(self, text: str, kind: str = "info") -> None:
        self.message = (text, kind)
        self._message_until = time.monotonic() + (6.0 if kind == "error" else 3.0)
        self.dirty = True

    def handle_key(self, key: Key) -> None:
        try:
            if self.overlay is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_list_key(key)
        except ActionError as err:
            self.flash(str(err), "error")
        except Exception as err:  # pragma: no cover - defensive
            self.flash(f"error: {err}", "error")
        self.draw()

    def _handle_overlay_key(self, key: Key) -> None:
        overlay = self.overlay
        assert overlay is not None

        if overlay.kind == "help":
            self.overlay = None
            self.dirty = True
            return

        if overlay.kind == "confirm":
            if key.name == "y":
                self.overlay = None
                if overlay.on_yes:
                    overlay.on_yes()
            elif key.name in ("n", "escape", "ctrl+c"):
                self.overlay = None
            self.dirty = True
            return

        if overlay.kind == "picker":
            items = self._picker_items(overlay)
            name = key.name
            if name == "escape":
                self.overlay = None
            elif name == "enter":
                item = items[overlay.cursor] if 0 <= overlay.cursor < len(items) else None
                self.overlay = None
                if item is not None and overlay.on_pick:
                    overlay.on_pick(item)
            elif name in ("up", "ctrl+p"):
                overlay.cursor = max(0, overlay.cursor - 1)
            elif name in ("down", "ctrl+n"):
                overlay.cursor = min(len(items) - 1, overlay.cursor + 1)
            elif name == "backspace":
                overlay.filter = overlay.filter[:-1]
                overlay.cursor = 0
            elif name == "ctrl+u":
                overlay.filter = ""
                overlay.cursor = 0
            elif key.char and not key.ctrl and not key.alt:
                overlay.filter += key.char
                overlay.cursor = 0
            self.dirty = True
            return

        # viewer
        height = self.rows - 3
        if overlay.typing:
            if key.name in ("escape", "enter"):
                overlay.typing = False
            elif key.name == "backspace":
                overlay.filter = overlay.filter[:-1]
            elif key.name == "ctrl+u":
                overlay.filter = ""
            elif key.char and not key.ctrl and not key.alt:
                overlay.filter += key.char
            self.dirty = True
            return

        name = key.name
        if name in ("escape", "q", "ctrl+v"):
            self.overlay = None
        elif name == "enter":
            self.overlay = None
            self._do_resume()
            return
        elif name in ("up", "k", "ctrl+p"):
            overlay.scroll -= 1
        elif name in ("down", "j", "ctrl+n"):
            overlay.scroll += 1
        elif name in ("pageup", "ctrl+u"):
            overlay.scroll -= height - 2
        elif name in ("pagedown", "ctrl+d", " "):
            overlay.scroll += height - 2
        elif name in ("home", "g"):
            overlay.scroll = 0
        elif name in ("end", "G"):
            overlay.scroll = 1 << 30
        elif name == "/":
            overlay.typing = True
        elif name == "t":
            self._viewer_thinking = not self._viewer_thinking
            self._reload_viewer()
        elif name == "s":
            self._viewer_sidechains = not self._viewer_sidechains
            self._reload_viewer()
        elif name == "ctrl+c":
            self.action = AppResult(kind="quit")
            return

        overlay.scroll = max(0, overlay.scroll)
        self.dirty = True

    def _reload_viewer(self) -> None:
        overlay = self.overlay
        if overlay is None or overlay.session is None:
            return
        overlay.entries = load_transcript(
            overlay.session.file,
            include_sidechains=self._viewer_sidechains,
            include_thinking=self._viewer_thinking,
        )
        overlay.loading = False
        self.dirty = True

    def _handle_list_key(self, key: Key) -> None:
        name = key.name
        page = max(1, self.rows - 6)

        # --- exit ------------------------------------------------------------
        if name in ("ctrl+c", "ctrl+q"):
            self.action = AppResult(kind="quit")
            return
        if name == "escape":
            if self.view.query:
                self.view.query = ""
                self.query_cursor = 0
                self.recompute()
            else:
                self.action = AppResult(kind="quit")
            return

        # --- launch ----------------------------------------------------------
        if name == "enter":
            self._do_resume()
            return
        if name == "ctrl+f":
            self._do_resume(fork=True)
            return
        if name == "ctrl+o":
            self._do_resume(cwd=self.anchor.cwd)
            return

        # --- movement --------------------------------------------------------
        if name in ("up", "ctrl+p"):
            self._move(-1)
            return
        if name in ("down", "ctrl+n"):
            self._move(1)
            return
        if name == "pageup":
            self._move(-page)
            return
        if name == "pagedown":
            self._move(page)
            return
        if name == "home":
            self.cursor = 0
            self.scroll = 0
            self.preview_scroll = 0
            self.dirty = True
            return
        if name == "end":
            self.cursor = max(0, len(self.result.hits) - 1)
            self.preview_scroll = 0
            self.dirty = True
            return

        # --- scope, sort, filters --------------------------------------------
        if name == "tab":
            self._cycle_scope(1)
            return
        if name == "shift+tab":
            self._cycle_scope(-1)
            return
        if name == "ctrl+s":
            self.view.sort = SORT_ORDER[(SORT_ORDER.index(self.view.sort) + 1) % len(SORT_ORDER)]
            self.cursor = self.scroll = 0
            self.recompute()
            return
        if name == "ctrl+b":
            self._open_branch_picker()
            return
        if name == "ctrl+r":
            self._open_project_picker()
            return
        if name == "ctrl+g":
            self.view.hide_empty = not self.view.hide_empty
            self.recompute()
            self.flash(
                "hiding sessions with no prompts" if self.view.hide_empty else "showing all sessions"
            )
            return

        # --- panes ------------------------------------------------------------
        if name == "ctrl+t":
            self.show_preview = not self.show_preview
            self.screen.invalidate()
            self.dirty = True
            return
        if name == "ctrl+v":
            self._open_viewer()
            return
        if name == "alt+up":
            self.preview_scroll = max(0, self.preview_scroll - 3)
            self.dirty = True
            return
        if name == "alt+down":
            self.preview_scroll += 3
            self.dirty = True
            return

        # --- session actions ---------------------------------------------------
        if name == "ctrl+y":
            self._copy_command()
            return
        if name == "alt+y":
            session = self.current()
            if session:
                ok = copy_to_clipboard(session.id)
                self.flash(
                    f"copied session id {session.id}" if ok else "no clipboard tool available",
                    "success" if ok else "error",
                )
            return
        if name == "ctrl+x":
            self._confirm_delete()
            return
        if name == "alt+u":
            self._undo_delete()
            return
        if name == "ctrl+l":
            self._hard_refresh()
            return

        # --- help ---------------------------------------------------------------
        if name in ("alt+h", "f1"):
            self.overlay = Overlay(kind="help")
            self.dirty = True
            return

        # --- text editing --------------------------------------------------------
        if name == "backspace":
            if self.query_cursor > 0:
                self.view.query = (
                    self.view.query[: self.query_cursor - 1] + self.view.query[self.query_cursor :]
                )
                self.query_cursor -= 1
                self.recompute()
            return
        if name == "delete":
            self.view.query = self.view.query[: self.query_cursor] + self.view.query[self.query_cursor + 1 :]
            self.recompute()
            return
        if name == "ctrl+u":
            self.view.query = self.view.query[self.query_cursor :]
            self.query_cursor = 0
            self.recompute()
            return
        if name == "ctrl+k":
            self.view.query = self.view.query[: self.query_cursor]
            self.recompute()
            return
        if name in ("ctrl+w", "alt+backspace"):
            before = self.view.query[: self.query_cursor].rstrip()
            cut = before.rfind(" ")
            before = before[: cut + 1] if cut != -1 else ""
            self.view.query = before + self.view.query[self.query_cursor :]
            self.query_cursor = len(before)
            self.recompute()
            return
        if name == "ctrl+a":
            self.query_cursor = 0
            self.dirty = True
            return
        if name == "ctrl+e":
            self.query_cursor = len(self.view.query)
            self.dirty = True
            return
        if name == "left":
            self.query_cursor = max(0, self.query_cursor - 1)
            self.dirty = True
            return
        if name == "right":
            self.query_cursor = min(len(self.view.query), self.query_cursor + 1)
            self.dirty = True
            return
        if name == "paste":
            if key.pasted:
                self._insert(" ".join(key.pasted.split()))
            return

        if key.char and not key.ctrl and not key.alt:
            self._insert(key.char)

    def _insert(self, text: str) -> None:
        self.view.query = self.view.query[: self.query_cursor] + text + self.view.query[self.query_cursor :]
        self.query_cursor += len(text)
        self.cursor = self.scroll = 0
        self.recompute()

    def _move(self, delta: int) -> None:
        if not self.result.hits:
            return
        self.cursor = max(0, min(len(self.result.hits) - 1, self.cursor + delta))
        self.preview_scroll = 0
        self.dirty = True

    def _cycle_scope(self, direction: int) -> None:
        index = SCOPE_ORDER.index(self.view.scope)
        nxt = SCOPE_ORDER[(index + direction) % len(SCOPE_ORDER)]
        self.view.scope = nxt
        # An explicit branch/repo filter belongs to a narrower scope; carrying it
        # into "all" would silently defeat the widening the user just asked for.
        if nxt == "all":
            self.view.branch_filter = None
            self.view.repo_filter = None
        self.cursor = self.scroll = 0
        self.recompute()

    def _open_branch_picker(self) -> None:
        found = branches_in(self.sessions, self.anchor, self.view.scope != "all")
        items = [PickerItem(value="", label="(any branch)", hint="clear filter")]
        items += [
            PickerItem(
                value=name,
                label="(detached HEAD)" if name == "HEAD" else name,
                hint=f"{count} records · {relative_time(last, self.now)}",
            )
            for name, count, last in found
        ]

        def pick(item: PickerItem) -> None:
            self.view.branch_filter = item.value or None
            if item.value:
                self.view.scope = "branch"
            self.cursor = self.scroll = 0
            self.recompute()

        overlay = Overlay(kind="picker", title="branch", items=items)
        overlay.on_pick = pick
        self.overlay = overlay
        self.dirty = True

    def _open_project_picker(self) -> None:
        items = [PickerItem(value="", label="(any project)", hint="clear filter")]
        items += [
            PickerItem(value=key, label=name, hint=f"{count} · {relative_time(last, self.now)}")
            for key, name, count, last in projects_in(self.sessions)
        ]

        def pick(item: PickerItem) -> None:
            self.view.repo_filter = item.value or None
            self.view.branch_filter = None
            if item.value:
                self.view.scope = "repo"
            self.cursor = self.scroll = 0
            self.recompute()

        overlay = Overlay(kind="picker", title="project", items=items)
        overlay.on_pick = pick
        self.overlay = overlay
        self.dirty = True

    def _open_viewer(self) -> None:
        session = self.current()
        if session is None:
            return
        self.overlay = Overlay(kind="viewer", session=session, loading=True)
        self.screen.invalidate()
        self.dirty = True
        self._reload_viewer()

    # ------------------------------------------------------------- actions ---

    def _do_resume(self, fork: bool = False, cwd: str | None = None) -> None:
        session = self.current()
        if session is None:
            self.flash("nothing selected", "error")
            return
        try:
            plan = plan_resume(session, fork=fork, cwd=cwd, skip_permissions=self.skip_permissions)
        except ResumeError as err:
            self.flash(str(err).split("\n")[0], "error")
            return
        self.action = AppResult(kind="resume", plan=plan, session=session)

    def _copy_command(self) -> None:
        session = self.current()
        if session is None:
            return
        try:
            text = plan_to_shell(plan_resume(session, skip_permissions=self.skip_permissions))
        except ResumeError:
            flag = "--dangerously-skip-permissions " if self.skip_permissions else ""
            text = f"claude {flag}--resume {session.id}"
        if copy_to_clipboard(text):
            self.flash(f"copied: {truncate(text, self.cols - 12)}", "success")
        else:
            self.flash("no clipboard tool available", "error")

    def _confirm_delete(self) -> None:
        session = self.current()
        if session is None:
            return
        if session.live is not None:
            self.flash("that session is running — close it first", "error")
            return

        def confirm() -> None:
            try:
                entry = trash_session(session)
            except ActionError as err:
                self.flash(str(err), "error")
                return
            self._last_trash = entry
            self.sessions = [s for s in self.sessions if s.file != session.file]
            self.recompute()
            self.flash("moved to trash — alt+u to undo", "success")

        overlay = Overlay(
            kind="confirm",
            text="Move this session to trash?",
            detail=[
                truncate(session_label(session), 60),
                f"{session.turns} prompts · {short_path(session.origin_cwd, HOME, 50)}",
                "",
                "Files move to ~/.claude/sesh/trash and can be restored with alt+u.",
            ],
        )
        overlay.on_yes = confirm
        self.overlay = overlay
        self.dirty = True

    def _undo_delete(self) -> None:
        if self._last_trash is None:
            self.flash("nothing to undo", "error")
            return
        try:
            restore_trash(self._last_trash)
        except ActionError as err:
            self.flash(str(err), "error")
            return
        self._last_trash = None
        self._hard_refresh()
        self.flash("restored", "success")

    def _hard_refresh(self) -> None:
        from ..core.index_store import load_sessions

        result = load_sessions()
        self.sessions = result.sessions
        self.recompute()
        self.flash(f"{len(result.sessions)} sessions", "success")


HELP_LINES = [
    "#launch",
    "enter\tresume the selected session in its own directory",
    "ctrl+f\tfork it — resume under a new session id, leaving the original intact",
    "ctrl+o\tresume from the current directory instead of the session's",
    "",
    "#find",
    "type\tfuzzy filter over titles, prompts, repos, branches and paths",
    "tab / shift+tab\twiden or narrow the scope: branch → repo → dir → all",
    "ctrl+b\tfilter by branch",
    "ctrl+r\tfilter by project",
    "ctrl+s\tcycle sort: recent, relevance, prompts, tokens, size, oldest, title",
    "ctrl+g\tshow or hide sessions with no prompts",
    "esc\tclear the filter, then quit",
    "",
    "#query syntax",
    "branch:main\talso b:  — sessions touching a branch",
    "repo:api\talso r:  — by repository",
    "file:auth.py\talso f:  — sessions that touched a file",
    "dir: tool: model:\tworking directory, tool used, model",
    "age:7d after:2026-07-01\ttime windows; also before:",
    "turns:>5 tokens:>100k\tnumeric filters; also records:",
    "is:live is:compacted\talso is:subagents, is:git, is:empty",
    'text:"cannot read"\tsearch inside full transcripts, not just prompts',
    "'exact  !exclude\tliteral substring, and negation",
    "",
    "#inspect",
    "ctrl+v\topen the full transcript; / searches inside it",
    "ctrl+t\tshow or hide the detail pane",
    "alt+↑ / alt+↓\tscroll the detail pane",
    "",
    "#manage",
    "ctrl+y\tcopy the resume command    alt+y  copy the session id",
    "ctrl+x\tmove a session to trash    alt+u  undo",
    "ctrl+l\treload from disk",
    "",
    "press any key to close",
]
