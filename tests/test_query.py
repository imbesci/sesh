import unittest

from sesh.core.fuzzy import fuzzy_match, substring_match
from sesh.core.query import evaluate, parse_query
from sesh.core.types import LiveSession, PromptEntry

from .fixtures import NOW, session


def run(sess=None, query=""):
    return evaluate(sess if sess is not None else session(), parse_query(query), NOW)


class FuzzyMatch(unittest.TestCase):
    def test_matches_subsequence_with_positions(self):
        match = fuzzy_match("plan-editor", "pe")
        self.assertIsNotNone(match)
        self.assertEqual(len(match.positions), 2)

    def test_rejects_absent_or_out_of_order(self):
        self.assertIsNone(fuzzy_match("plan-editor", "zz"))
        self.assertIsNone(fuzzy_match("abc", "cba"))

    def test_ranks_word_boundary_above_mid_word(self):
        boundary = fuzzy_match("plan-editor", "ed")
        mid_word = fuzzy_match("planxedxitor", "ed")
        self.assertGreater(boundary.score, mid_word.score)

    def test_ranks_short_haystack_above_long(self):
        short = fuzzy_match("auth", "auth")
        long = fuzzy_match("auth" + "x" * 400, "auth")
        self.assertGreater(short.score, long.score)

    def test_empty_needle_matches_anything(self):
        match = fuzzy_match("anything", "")
        self.assertEqual((match.score, match.positions), (0, []))

    def test_substring_requires_contiguity(self):
        self.assertIsNotNone(substring_match("plan-editor", "an-ed"))
        self.assertIsNone(substring_match("plan-editor", "pe"))


class MatchQuality(unittest.TestCase):
    def test_rejects_letters_scattered_across_a_long_path(self):
        # The bug this guards: "parser" subsequence-matching a deep file path.
        self.assertIsNone(fuzzy_match("/Users/alice/dev/api/tests/test_widget.py", "parser"))

    def test_accepts_genuine_abbreviations(self):
        self.assertIsNotNone(fuzzy_match("plan-editor", "pe"))
        self.assertIsNotNone(fuzzy_match("session-manager", "sm"))
        self.assertIsNotNone(fuzzy_match("computeView", "cv"))

    def test_session_not_matched_via_incidental_path_subsequence(self):
        sess = session(
            ai_title="Unrelated", prompts=[], files=["/Users/alice/dev/api/tests/test_widget.py"]
        )
        self.assertIsNone(run(sess, "parser"))

    def test_real_filename_still_matches(self):
        sess = session(ai_title="Unrelated", prompts=[], files=["/repo/api/src/parser.py"])
        self.assertIsNotNone(run(sess, "parser"))


class ParseQuery(unittest.TestCase):
    def test_splits_field_terms_from_free_text(self):
        query = parse_query("branch:main fix the bug")
        term = query.terms[0]
        self.assertEqual((term.field, term.value, term.negated, term.exact), ("branch", "main", False, False))
        self.assertEqual(query.free_text, "fix the bug")

    def test_aliases_negation_and_exact(self):
        query = parse_query("b:main !draft 'exact")
        self.assertEqual(query.terms[0].field, "branch")
        self.assertTrue(query.terms[1].negated)
        self.assertTrue(query.terms[2].exact)

    def test_keeps_quoted_values_together(self):
        query = parse_query('dir:"/My Projects/api"')
        self.assertEqual(query.terms[0].field, "dir")
        self.assertEqual(query.terms[0].value, "/My Projects/api")

    def test_unknown_prefix_is_free_text(self):
        query = parse_query("http://example.com/x")
        self.assertIsNone(query.terms[0].field)
        self.assertEqual(query.free_text, "http://example.com/x")


class Evaluate(unittest.TestCase):
    def test_matches_title(self):
        self.assertIsNotNone(run(session(ai_title="Fix auth redirect"), "auth"))

    def test_matches_text_only_in_an_earlier_prompt(self):
        sess = session(
            ai_title="Unrelated title",
            prompts=[PromptEntry(text="please fix the kerberos handshake", at=NOW, branch="main")],
        )
        self.assertIsNotNone(run(sess, "kerberos"))

    def test_title_hit_outranks_prompt_hit(self):
        titled = run(session(ai_title="kerberos work"), "kerberos")
        prompted = run(
            session(ai_title="zzz", prompts=[PromptEntry(text="kerberos", at=NOW, branch=None)]),
            "kerberos",
        )
        self.assertGreater(titled.score, prompted.score)

    def test_all_terms_must_match(self):
        sess = session(ai_title="auth work", last_branch="main")
        self.assertIsNotNone(run(sess, "auth branch:main"))
        self.assertIsNone(run(sess, "auth branch:release"))

    def test_negation_excludes(self):
        self.assertIsNone(run(session(ai_title="auth work"), "!auth"))
        self.assertIsNotNone(run(session(ai_title="auth work"), "!database"))

    def test_file_field(self):
        sess = session(files=["/repo/api/src/auth.py"])
        self.assertIsNotNone(run(sess, "file:auth.py"))
        self.assertIsNone(run(sess, "file:missing.py"))

    def test_branch_checks_every_branch_touched(self):
        from sesh.core.types import BranchStat

        sess = session(
            last_branch="main",
            branches=[
                BranchStat(name="main", count=5, last_seen=NOW),
                BranchStat(name="feature/x", count=3, last_seen=NOW - 1000),
            ],
        )
        self.assertIsNotNone(run(sess, "branch:feature/x"))

    def test_numeric_comparisons_and_suffixes(self):
        sess = session(turns=12, output_tokens=250_000)
        self.assertIsNotNone(run(sess, "turns:>5"))
        self.assertIsNone(run(sess, "turns:>50"))
        self.assertIsNotNone(run(sess, "tokens:>100k"))
        self.assertIsNone(run(sess, "tokens:>1m"))

    def test_age_bounds_by_recency(self):
        old = session(ended_at=NOW - 30 * 86400)
        self.assertIsNone(run(old, "age:7d"))
        self.assertIsNotNone(run(old, "age:60d"))

    def test_is_live(self):
        self.assertIsNone(run(session(), "is:live"))
        live = session(
            live=LiveSession(
                pid=1, session_id="x", cwd="/repo/api", started_at=NOW, updated_at=NOW,
                version="x", kind="interactive", entrypoint="cli",
            )
        )
        self.assertIsNotNone(run(live, "is:live"))

    def test_id_prefix(self):
        sess = session(id="3f9a1b2c-0000-0000-0000-000000000000")
        self.assertIsNotNone(run(sess, "id:3f9a"))
        self.assertIsNone(run(sess, "id:beef"))

    def test_reports_where_the_match_landed(self):
        hit = run(session(ai_title="Fix auth redirect"), "redirect")
        self.assertEqual(hit.highlight.text, "Fix auth redirect")
        self.assertEqual(len(hit.highlight.positions), 8)


if __name__ == "__main__":
    unittest.main()
