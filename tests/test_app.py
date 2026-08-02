import os
import unittest

from sesh.core.types import BranchStat, CwdStat, LiveSession, PromptEntry
from sesh.tui.ansi import set_color_enabled
from sesh.tui.app import App
from sesh.tui.io import CaptureFrameBuffer, HeadlessTerminal

from .fixtures import NOW, Transcript, anchor, assistant_message, session, user_prompt

set_color_enabled(False)


class Harness:
    """A booted picker wired to a headless terminal."""

    def __init__(self, sessions, anchor_value=None, query="", rows=30, cols=140, skip_permissions=True):
        self.terminal = HeadlessTerminal(rows=rows, cols=cols)
        self.screen = CaptureFrameBuffer()
        self.app = App(
            sessions,
            anchor_value if anchor_value is not None else anchor(),
            query,
            terminal=self.terminal,
            screen=self.screen,
            auto_refresh=False,
            # Pinned rather than inherited from the environment so the assertions
            # below are deterministic regardless of SESH_SAFE.
            skip_permissions=skip_permissions,
        )
        self.app.begin()

    def press(self, *names):
        for name in names:
            self.terminal.queue(name)
            for key in self.terminal.read_keys():
                self.app.handle_key(key)

    def type(self, text):
        self.press(*list(text))

    def text(self):
        return self.screen.text()

    def selected(self):
        return self.screen.selected_row()

    def left(self):
        return "\n".join(self.screen.left_pane())


class Startup(unittest.TestCase):
    def test_paints_a_frame_with_sessions(self):
        harness = Harness([session(ai_title="Fix the parser")])
        self.assertGreater(harness.screen.frames, 0)
        self.assertIn("Fix the parser", harness.text())

    def test_shows_anchor_repo_and_branch(self):
        harness = Harness([session()])
        self.assertIn("api", harness.text())
        self.assertIn("main", harness.text())

    def test_widens_scope_rather_than_opening_empty(self):
        elsewhere = session(
            id="elsewhere",
            ai_title="Other repo work",
            origin_cwd="/repo/web",
            repo_key="/repo/web",
            repo_root="/repo/web",
            repo_name="web",
            cwds=[CwdStat(path="/repo/web", count=1, last_seen=NOW, repo_root="/repo/web", repo_key="/repo/web")],
            branches=[BranchStat(name="trunk", count=1, last_seen=NOW)],
            last_branch="trunk",
        )
        harness = Harness([elsewhere])
        self.assertIn("Other repo work", harness.text())
        self.assertIn("scope:all", harness.text())

    def test_applies_query_from_command_line(self):
        harness = Harness(
            [session(id="a", ai_title="alpha work"), session(id="b", ai_title="beta work")],
            query="alpha",
        )
        self.assertIn("alpha work", harness.text())
        self.assertNotIn("beta work", harness.text())


class Filtering(unittest.TestCase):
    def sessions(self):
        return [
            session(id="a", ai_title="Fix auth redirect"),
            session(id="b", ai_title="Rewrite the parser"),
            session(id="c", ai_title="Update the docs"),
        ]

    def test_typing_narrows(self):
        harness = Harness(self.sessions())
        harness.type("parser")
        self.assertIn("Rewrite the parser", harness.text())
        self.assertNotIn("Fix auth redirect", harness.text())

    def test_backspace_widens_again(self):
        harness = Harness(self.sessions())
        harness.type("parser")
        harness.press(*["backspace"] * 6)
        self.assertIn("Fix auth redirect", harness.text())

    def test_escape_clears_query_before_quitting(self):
        harness = Harness(self.sessions())
        harness.type("parser")
        harness.press("escape")
        self.assertIn("Fix auth redirect", harness.text())
        harness.press("escape")
        self.assertIsNotNone(harness.app.action)
        self.assertEqual(harness.app.action.kind, "quit")

    def test_ctrl_u_clears_query(self):
        harness = Harness(self.sessions())
        harness.type("parser")
        harness.press("ctrl+u")
        self.assertIn("Fix auth redirect", harness.text())

    def test_field_queries_from_the_input_box(self):
        on_feature = session(
            id="feat",
            ai_title="Feature work",
            branches=[BranchStat(name="feature", count=1, last_seen=NOW)],
            last_branch="feature",
        )
        harness = Harness([*self.sessions(), on_feature])
        harness.press("tab")  # branch -> repo, so both branches are in scope
        harness.type("branch:feature")
        self.assertIn("Feature work", harness.text())
        self.assertNotIn("Fix auth redirect", harness.text())

    def test_empty_result_explains_recovery(self):
        harness = Harness(self.sessions())
        harness.type("zzzznothing")
        self.assertIn("no sessions match", harness.text())
        self.assertIn("esc clears the filter", harness.text())

    def test_shows_why_a_row_matched(self):
        sess = session(
            ai_title="Unrelated title",
            prompts=[PromptEntry(text="please fix the kerberos handshake", at=NOW, branch="main")],
        )
        harness = Harness([sess])
        harness.type("kerberos")
        self.assertIn("Unrelated title", harness.text())
        self.assertIn("kerberos", harness.text())


class ScopeControl(unittest.TestCase):
    IN_REPO = session(id="in", ai_title="In repo")
    OUTSIDE = session(
        id="out",
        ai_title="Outside repo",
        origin_cwd="/elsewhere",
        repo_key=None,
        repo_root=None,
        repo_name=None,
        cwds=[CwdStat(path="/elsewhere", count=1, last_seen=NOW, repo_root=None, repo_key=None)],
        branches=[BranchStat(name="other", count=1, last_seen=NOW)],
        last_branch="other",
    )

    def test_tab_widens_to_everything(self):
        harness = Harness([self.IN_REPO, self.OUTSIDE])
        self.assertIn("scope:branch", harness.text())
        self.assertNotIn("Outside repo", harness.text())
        harness.press("tab", "tab", "tab")
        self.assertIn("scope:all", harness.text())
        self.assertIn("Outside repo", harness.text())

    def test_widening_preserves_the_query(self):
        harness = Harness([self.IN_REPO, self.OUTSIDE])
        harness.type("repo")
        harness.press("tab", "tab", "tab")
        self.assertIn("Outside repo", harness.text())

    def test_shift_tab_narrows(self):
        harness = Harness([self.IN_REPO, self.OUTSIDE])
        harness.press("tab", "shift+tab")
        self.assertIn("scope:branch", harness.text())

    def test_ctrl_s_cycles_sort(self):
        harness = Harness([self.IN_REPO])
        self.assertIn("sort:recent", harness.text())
        harness.press("ctrl+s")
        self.assertIn("sort:relevance", harness.text())

    def test_ctrl_g_reveals_empty_sessions(self):
        empty = session(id="empty", ai_title="Nothing happened", turns=0, prompts=[])
        harness = Harness([self.IN_REPO, empty])
        self.assertNotIn("Nothing happened", harness.text())
        harness.press("ctrl+g")
        self.assertIn("Nothing happened", harness.text())


class Navigation(unittest.TestCase):
    def many(self):
        return [
            session(id=f"s{i}", ai_title=f"Session number {i}", ended_at=NOW - i)
            for i in range(50)
        ]

    def test_arrows_move_selection(self):
        harness = Harness(self.many())
        harness.press("down", "down")
        self.assertIn("Session number 2", harness.selected())

    def test_scrolls_past_the_fold(self):
        harness = Harness(self.many())
        harness.press(*["down"] * 40)
        self.assertIn("Session number 40", harness.selected())
        self.assertIn("Session number 40", harness.left())

    def test_end_and_home(self):
        harness = Harness(self.many())
        harness.press("end")
        self.assertIn("Session number 49", harness.selected())
        harness.press("home")
        self.assertIn("Session number 0", harness.selected())

    def test_selection_cannot_run_off_either_end(self):
        harness = Harness(self.many())
        harness.press(*["up"] * 100)
        self.assertIn("Session number 0", harness.selected())
        harness.press(*["down"] * 200)
        self.assertIn("Session number 49", harness.selected())

    def test_scrolling_back_up_keeps_selection_visible(self):
        harness = Harness(self.many())
        harness.press("end")
        harness.press(*["up"] * 30)
        self.assertIn("Session number 19", harness.selected())


class Resuming(unittest.TestCase):
    REAL_DIR = os.getcwd()

    def resumable(self, **over):
        # Resume planning refuses to target a directory that no longer exists,
        # so these fixtures must point somewhere real.
        return session(
            id="abc",
            origin_cwd=self.REAL_DIR,
            project_dir="".join(c if c.isalnum() else "-" for c in self.REAL_DIR),
            cwds=[CwdStat(path=self.REAL_DIR, count=5, last_seen=NOW, repo_root=self.REAL_DIR, repo_key=self.REAL_DIR)],
            **over,
        )

    def test_enter_targets_the_sessions_own_directory(self):
        harness = Harness([self.resumable()])
        harness.press("enter")
        action = harness.app.action
        self.assertEqual(action.kind, "resume")
        self.assertEqual(action.plan.args, ["--dangerously-skip-permissions", "--resume", "abc"])
        self.assertEqual(action.plan.cwd, self.REAL_DIR)
        self.assertEqual(action.plan.warnings, [])

    def test_safe_mode_omits_the_skip_permissions_flag(self):
        harness = Harness([self.resumable()], skip_permissions=False)
        harness.press("enter")
        self.assertEqual(harness.app.action.plan.args, ["--resume", "abc"])

    def test_ctrl_f_forks(self):
        harness = Harness([self.resumable()])
        harness.press("ctrl+f")
        args = harness.app.action.plan.args
        self.assertIn("--fork-session", args)
        # The skip flag still leads the invocation when forking.
        self.assertEqual(args[0], "--dangerously-skip-permissions")

    def test_ctrl_o_resumes_when_the_current_directory_resolves(self):
        # The real use of ctrl+o: the session's recorded directory is gone, but
        # you are standing in one that encodes to the same project.
        gone = session(
            id="abc", origin_cwd="/gone/place",
            project_dir="".join(c if c.isalnum() else "-" for c in self.REAL_DIR), cwds=[],
        )
        harness = Harness([gone], anchor(cwd=self.REAL_DIR))
        harness.press("ctrl+o")
        self.assertEqual(harness.app.action.plan.cwd, self.REAL_DIR)

    def test_ctrl_o_refuses_a_directory_that_cannot_resolve(self):
        # Resuming from a directory that encodes to a different project is a
        # guaranteed "No conversation found", so ctrl+o refuses and points at
        # the directory that works instead of launching a doomed command.
        harness = Harness([self.resumable()], anchor(cwd="/somewhere/else"))
        harness.press("ctrl+o")
        self.assertIsNone(harness.app.action)
        self.assertIn("won't resolve this session", harness.text())

    def test_refuses_when_directory_is_gone(self):
        harness = Harness([session(id="abc", origin_cwd="/definitely/not/here")])
        harness.press("enter")
        self.assertIn("no longer exists", harness.text())
        self.assertIsNone(harness.app.action)

    def test_running_session_warns(self):
        live = LiveSession(
            pid=42, session_id="abc", cwd=self.REAL_DIR, started_at=NOW, updated_at=NOW,
            version="x", kind="interactive", entrypoint="cli", status="busy",
        )
        harness = Harness([self.resumable(live=live)])
        harness.press("enter")
        self.assertIn("42", " ".join(harness.app.action.plan.warnings))

    def test_enter_on_empty_list_does_nothing(self):
        harness = Harness([session(ai_title="only one")])
        harness.type("zzzznothing")
        harness.press("enter")
        self.assertIn("nothing selected", harness.text())
        self.assertIsNone(harness.app.action)


class Overlays(unittest.TestCase):
    def test_help_opens_and_closes(self):
        harness = Harness([session()])
        harness.press("alt+h")
        self.assertIn("query syntax", harness.text())
        harness.press("x")
        self.assertNotIn("query syntax", harness.text())

    def test_branch_picker_lists_observed_branches(self):
        sess = session(
            branches=[
                BranchStat(name="main", count=5, last_seen=NOW),
                BranchStat(name="feature/login", count=2, last_seen=NOW),
            ]
        )
        harness = Harness([sess])
        harness.press("ctrl+b")
        self.assertIn("feature/login", harness.text())
        harness.press("escape")

    def test_choosing_a_branch_applies_a_filter(self):
        on_main = session(id="m", ai_title="Main work")
        on_feature = session(
            id="f",
            ai_title="Feature work",
            branches=[BranchStat(name="feature", count=1, last_seen=NOW)],
            last_branch="feature",
        )
        harness = Harness([on_main, on_feature])
        harness.press("ctrl+b")
        harness.type("feature")
        harness.press("enter")
        self.assertIn("Feature work", harness.text())
        self.assertNotIn("Main work", harness.text())

    def test_project_picker(self):
        harness = Harness(
            [session(id="a"), session(id="b", repo_key="/repo/web", repo_name="web", origin_cwd="/repo/web")]
        )
        harness.press("ctrl+r")
        self.assertIn("web", harness.text())
        harness.press("escape")

    def test_delete_asks_first_and_cancels_cleanly(self):
        harness = Harness([session(ai_title="Precious session")])
        harness.press("ctrl+x")
        self.assertIn("Move this session to trash?", harness.text())
        harness.press("n")
        self.assertIn("Precious session", harness.text())

    def test_running_session_refuses_deletion(self):
        live = LiveSession(
            pid=7, session_id="x", cwd="/repo/api", started_at=NOW, updated_at=NOW,
            version="x", kind="interactive", entrypoint="cli",
        )
        harness = Harness([session(ai_title="Busy session", live=live)])
        harness.press("ctrl+x")
        self.assertIn("running", harness.text())


class Layout(unittest.TestCase):
    def test_wide_terminal_shows_detail_pane(self):
        harness = Harness([session(ai_title="Wide")], cols=160)
        self.assertIn("prompts", harness.text())

    def test_narrow_terminal_drops_detail_pane(self):
        harness = Harness([session(ai_title="Narrow")], cols=70)
        self.assertIn("Narrow", harness.text())
        self.assertNotIn("stats", harness.text())

    def test_ctrl_t_toggles_detail_pane(self):
        harness = Harness([session()], cols=160)
        harness.press("ctrl+t")
        self.assertNotIn("stats", harness.text())
        harness.press("ctrl+t")
        self.assertIn("stats", harness.text())

    def test_no_line_exceeds_terminal_width(self):
        long = session(
            ai_title="A ludicrously long session title that goes on and on " * 6,
            origin_cwd="/very/deep/nested/path/that/keeps/going/and/going/further/still",
        )
        harness = Harness([long, session()], cols=100, rows=24)
        for line in harness.screen.plain():
            self.assertLessEqual(len(line), 100)

    def test_resizing_repaints_at_the_new_width(self):
        harness = Harness([session(ai_title="Resizable")], cols=160)
        harness.terminal.resize(20, 80)
        harness.app.rows, harness.app.cols = 20, 80
        harness.app.dirty = True
        harness.app.draw()
        for line in harness.screen.plain():
            self.assertLessEqual(len(line), 80)
        self.assertIn("Resizable", harness.text())

    def test_very_short_terminal_still_renders(self):
        harness = Harness([session()], cols=60, rows=6)
        self.assertGreater(harness.screen.frames, 0)


class TranscriptViewer(unittest.TestCase):
    def test_opens_and_returns_to_the_list(self):
        with Transcript(
            [
                user_prompt("what does the parser do?"),
                assistant_message(
                    [
                        {"type": "text", "text": "It tokenises the input."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "cat parser.py"}},
                    ]
                ),
                user_prompt("thanks"),
            ]
        ) as fixture:
            harness = Harness([session(ai_title="Parser chat", file=fixture.path)])
            harness.press("ctrl+v")
            text = harness.text()
            self.assertIn("what does the parser do?", text)
            self.assertIn("It tokenises the input.", text)
            # Tool calls become an identifying one-liner, not raw output.
            self.assertIn("Bash(cat parser.py)", text)

            harness.press("escape")
            self.assertIn("Parser chat", harness.text())

    def test_scrolls_and_searches(self):
        records = [user_prompt(f"question number {i}") for i in range(60)]
        with Transcript(records) as fixture:
            harness = Harness([session(file=fixture.path)])
            harness.press("ctrl+v")
            self.assertIn("question number 0", harness.text())

            harness.press("end")
            self.assertIn("question number 59", harness.text())

            harness.press("/")
            harness.type("number 42")
            harness.press("enter")
            text = harness.text()
            self.assertIn("question number 42", text)
            self.assertNotIn("question number 43", text)

    def test_enter_inside_viewer_resumes(self):
        real_dir = os.getcwd()
        with Transcript([user_prompt("hello")]) as fixture:
            sess = session(
                id="vabc",
                file=fixture.path,
                origin_cwd=real_dir,
                project_dir="".join(c if c.isalnum() else "-" for c in real_dir),
                cwds=[CwdStat(path=real_dir, count=1, last_seen=NOW, repo_root=real_dir, repo_key=real_dir)],
            )
            harness = Harness([sess])
            harness.press("ctrl+v")
            harness.press("enter")
            self.assertEqual(harness.app.action.plan.args, ["--dangerously-skip-permissions", "--resume", "vabc"])


class BodySearch(unittest.TestCase):
    def test_text_narrows_to_transcripts_containing_the_phrase(self):
        with Transcript([user_prompt("the kerberos handshake failed")], "a") as first, Transcript(
            [user_prompt("something entirely different")], "b"
        ) as second:
            harness = Harness(
                [
                    session(id="a", ai_title="Session A", file=first.path),
                    session(id="b", ai_title="Session B", file=second.path),
                ]
            )
            harness.type("text:kerberos")
            harness.app.settle()
            harness.app.draw()

            text = harness.text()
            self.assertIn("Session A", text)
            self.assertNotIn("Session B", text)

    def test_list_stays_populated_while_search_is_in_flight(self):
        harness = Harness([session(ai_title="Session A")])
        harness.type("text:something")
        # Immediately after typing, nothing has been searched yet.
        self.assertIn("searching transcripts…", harness.text())
        self.assertIn("Session A", harness.text())

    def test_widening_scope_researches_newly_included_sessions(self):
        with Transcript([user_prompt("nothing relevant here")], "in") as inside, Transcript(
            [user_prompt("the kerberos handshake failed")], "out"
        ) as outside:
            in_repo = session(id="in", ai_title="In repo", file=inside.path)
            out_repo = session(
                id="out",
                ai_title="Outside repo",
                file=outside.path,
                origin_cwd="/elsewhere",
                repo_key=None,
                repo_root=None,
                repo_name=None,
                cwds=[CwdStat(path="/elsewhere", count=1, last_seen=NOW, repo_root=None, repo_key=None)],
                branches=[BranchStat(name="other", count=1, last_seen=NOW)],
                last_branch="other",
            )
            harness = Harness([in_repo, out_repo])
            harness.type("text:kerberos")
            harness.app.settle()
            harness.app.draw()
            self.assertIn("no sessions match", harness.text())

            harness.press("tab", "tab", "tab")
            harness.app.settle()
            harness.app.draw()
            self.assertIn("Outside repo", harness.text())

    def test_subagent_transcripts_are_attributed_to_the_parent(self):
        with Transcript([user_prompt("main thread only")], "parent") as fixture:
            fixture.add_subagent([user_prompt("the kerberos handshake failed")])
            sess = session(id="p", ai_title="Parent session", file=fixture.path, has_subagents=True)
            harness = Harness([sess])
            harness.type("text:kerberos")
            harness.app.settle()
            harness.app.draw()
            self.assertIn("Parent session", harness.text())


class RelatedAndOpen(unittest.TestCase):
    def _pair(self):
        ref = session(
            id="ref", ai_title="The auth work",
            branches=[BranchStat(name="main", count=5, last_seen=NOW)], files=["/repo/api/auth.py"],
        )
        sibling = session(
            id="sib", ai_title="More auth on a branch",
            branches=[BranchStat(name="feature", count=5, last_seen=NOW)], files=["/repo/api/auth.py"],
        )
        stranger = session(
            id="stranger", ai_title="Unrelated thing",
            repo_key="/repo/web", repo_name="web", origin_cwd="/repo/web", files=["/repo/web/main.py"],
        )
        return ref, sibling, stranger

    def test_alt_r_shows_related_and_esc_clears(self):
        ref, sibling, stranger = self._pair()
        harness = Harness([ref, sibling, stranger], anchor_value=anchor())
        harness.press("tab", "tab", "tab")  # scope:all so all three are visible first
        self.assertIn("Unrelated thing", harness.text())
        harness.press("alt+r")  # relate to the top row (ref)
        self.assertIn("related:", harness.text())
        self.assertIn("More auth on a branch", harness.text())
        self.assertNotIn("Unrelated thing", harness.text())
        harness.press("escape")
        self.assertNotIn("related:", harness.text())
        self.assertIn("Unrelated thing", harness.text())

    def test_alt_o_on_missing_directory_reports_gracefully(self):
        # Fixture cwds do not exist on disk, so no editor is launched.
        harness = Harness([session(ai_title="Some work")])
        harness.press("alt+o")
        self.assertIn("no longer exists", harness.text())


class TimeGrouping(unittest.TestCase):
    def test_headers_appear_under_recent_sort(self):
        today = session(id="t", ai_title="Fresh work", ended_at=NOW)
        old = session(id="o", ai_title="Old work", ended_at=NOW - 20 * 86400)
        harness = Harness([today, old])
        harness.press("tab", "tab", "tab")
        text = harness.text()
        self.assertIn("Today", text)
        self.assertIn("Past month", text)

    def test_headers_vanish_when_sorted_otherwise(self):
        today = session(id="t", ai_title="Fresh work", ended_at=NOW)
        old = session(id="o", ai_title="Old work", ended_at=NOW - 20 * 86400)
        harness = Harness([today, old])
        harness.press("tab", "tab", "tab", "ctrl+s")  # off "recent"
        self.assertNotIn("Past month", harness.text())

    def test_cursor_stays_visible_scrolling_through_groups(self):
        # Grouping adds header lines; the scroll math must still keep the
        # selected row on screen.
        many = [session(id=f"s{i}", ai_title=f"Session {i}", ended_at=NOW - i * 86400) for i in range(40)]
        harness = Harness(many, rows=16)
        harness.press("tab", "tab", "tab")
        harness.press(*["down"] * 30)
        self.assertIn("Session 30", harness.selected())


if __name__ == "__main__":
    unittest.main()
