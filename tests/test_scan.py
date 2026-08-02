import unittest

from sesh.core.scan import clean_prompt_text, scan_path

from .fixtures import Transcript, assistant_message, tool_result, user_prompt


class CleanPromptText(unittest.TestCase):
    def test_prefers_slash_command_arguments(self):
        raw = (
            "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
            "<command-args>build a session picker</command-args>"
        )
        self.assertEqual(clean_prompt_text(raw), "/goal build a session picker")

    def test_keeps_command_name_without_arguments(self):
        self.assertEqual(clean_prompt_text("<command-name>/compact</command-name>"), "/compact")

    def test_strips_system_reminders(self):
        raw = "real text <system-reminder>ignore me</system-reminder> more"
        self.assertEqual(clean_prompt_text(raw), "real text more")

    def test_leaves_ordinary_prose_alone(self):
        text = "fix the bug in a < b comparison"
        self.assertEqual(clean_prompt_text(text), text)


class ScanSession(unittest.TestCase):
    def test_extracts_envelope_type_despite_nested_message(self):
        with Transcript(
            [
                user_prompt("hello there"),
                assistant_message(
                    [
                        {"type": "text", "text": "hi"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]
                ),
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.tool_calls, 1)
        self.assertEqual(meta.output_tokens, 50)
        self.assertEqual(meta.models, ["claude-opus-5"])
        self.assertEqual([(t.name, t.count) for t in meta.tools], [("Bash", 1)])

    def test_counts_typed_prompts_only(self):
        with Transcript(
            [
                user_prompt("first question"),
                assistant_message([{"type": "text", "text": "answer"}]),
                tool_result("some command output"),
                user_prompt("injected", isMeta=True),
                user_prompt("second question"),
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.turns, 2)
        self.assertEqual([p.text for p in meta.prompts], ["first question", "second question"])
        self.assertEqual(meta.first_prompt, "first question")

    def test_quoted_content_cannot_spoof_metadata(self):
        # A tool result containing a whole JSON record -- this tool's own
        # sessions do exactly this -- must not contribute branch, cwd or prompt.
        import json

        spoof = json.dumps(
            {
                "type": "user",
                "cwd": "/evil",
                "gitBranch": "attacker",
                "message": {"role": "user", "content": "spoofed"},
            }
        )
        with Transcript([user_prompt("real prompt"), tool_result(spoof)]) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual([p.text for p in meta.prompts], ["real prompt"])
        self.assertEqual([b.name for b in meta.branches], ["main"])
        self.assertEqual([c.path for c in meta.cwds], ["/repo/api"])

    def test_tracks_every_branch_and_cwd(self):
        with Transcript(
            [
                user_prompt("a", gitBranch="main", cwd="/repo/api"),
                user_prompt("b", gitBranch="feature", cwd="/repo/api"),
                user_prompt("c", gitBranch="feature", cwd="/repo/other"),
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual([b.name for b in meta.branches], ["feature", "main"])
        self.assertEqual(meta.primary_branch, "feature")
        self.assertEqual(meta.origin_cwd, "/repo/api")
        self.assertEqual(sorted(c.path for c in meta.cwds), ["/repo/api", "/repo/other"])

    def test_excludes_subagents_from_branch_attribution(self):
        records = [user_prompt("main thread", gitBranch="main")]
        records += [
            assistant_message([{"type": "text", "text": "sub"}], isSidechain=True, gitBranch="sidebranch")
            for _ in range(20)
        ]
        with Transcript(records) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.primary_branch, "main")
        self.assertNotIn("sidebranch", [b.name for b in meta.branches])
        self.assertEqual(meta.sidechain_records, 20)

    def test_survives_truncated_final_line(self):
        with Transcript([user_prompt("complete record")]) as fixture:
            fixture.append_raw('{"type":"assistant","message":{"conte')
            meta = scan_path(fixture.path)

        self.assertEqual(meta.turns, 1)
        self.assertEqual(meta.first_prompt, "complete record")

    def test_last_ai_title_wins_and_compaction_is_recorded(self):
        with Transcript(
            [
                user_prompt("x"),
                {"type": "ai-title", "aiTitle": "First guess", "sessionId": "s1"},
                {"type": "summary", "summary": "compacted history", "leafUuid": "u1"},
                {"type": "ai-title", "aiTitle": "Better title", "sessionId": "s1"},
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.ai_title, "Better title")
        self.assertTrue(meta.compacted)

    def test_collects_file_paths_from_editing_tools(self):
        with Transcript(
            [
                user_prompt("edit things"),
                assistant_message(
                    [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/api/src/auth.py"}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/api/README.md"}},
                    ]
                ),
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(sorted(meta.files), ["/repo/api/README.md", "/repo/api/src/auth.py"])


class SyntheticPromptFiltering(unittest.TestCase):
    def test_task_notifications_are_not_prompts(self):
        notification = (
            "<task-notification>\n<task-id>a469e88361b26bc8b</task-id>\n"
            "<tool-use-id>toolu_01UudgDRiuzcAUewMJomMQZv</tool-use-id>\n"
            "<status>completed</status>\n</task-notification>"
        )
        with Transcript(
            [user_prompt("a real question"), user_prompt(notification), user_prompt("another real question")]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.turns, 2)
        self.assertEqual([p.text for p in meta.prompts], ["a real question", "another real question"])

    def test_compaction_preambles_and_interrupts_excluded(self):
        with Transcript(
            [
                user_prompt("Caveat: The messages below were generated by a hook"),
                user_prompt("[Request interrupted by user]"),
                user_prompt("This session is being continued from a previous conversation"),
                user_prompt("the only real prompt"),
            ]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual([p.text for p in meta.prompts], ["the only real prompt"])

    def test_reminder_only_turn_is_not_a_prompt(self):
        with Transcript(
            [user_prompt("<system-reminder>background context</system-reminder>"), user_prompt("real one")]
        ) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual([p.text for p in meta.prompts], ["real one"])

    def test_prompt_merely_mentioning_a_tag_is_kept(self):
        # The tag appears mid-sentence, so this is a human asking about it.
        with Transcript([user_prompt("why does <task-notification> show up in my list?")]) as fixture:
            meta = scan_path(fixture.path)

        self.assertEqual(meta.turns, 1)
        self.assertIn("why does", meta.prompts[0].text)


if __name__ == "__main__":
    unittest.main()
