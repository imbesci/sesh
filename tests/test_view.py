import unittest
from dataclasses import replace

from sesh.core.types import BranchStat, CwdStat, LiveSession
from sesh.core.view import branches_in, compute_view, default_view, projects_in

from .fixtures import NOW, anchor, session


def ids(result):
    return [hit.session.id for hit in result.hits]


ON_MAIN = session(id="main-1", branches=[BranchStat(name="main", count=5, last_seen=NOW)], last_branch="main")
ON_FEATURE = session(
    id="feat-1", branches=[BranchStat(name="feature", count=5, last_seen=NOW)], last_branch="feature"
)
OTHER_REPO = session(
    id="other-1",
    origin_cwd="/repo/web",
    repo_key="/repo/web",
    repo_root="/repo/web",
    repo_name="web",
    cwds=[CwdStat(path="/repo/web", count=3, last_seen=NOW, repo_root="/repo/web", repo_key="/repo/web")],
)


class Scope(unittest.TestCase):
    ALL = [ON_MAIN, ON_FEATURE, OTHER_REPO]

    def test_branch_scope_keeps_only_current_branch(self):
        view = replace(default_view(anchor()), scope="branch")
        self.assertEqual(ids(compute_view(self.ALL, view, anchor(), NOW)), ["main-1"])

    def test_repo_scope_keeps_every_branch_in_repo(self):
        view = replace(default_view(anchor()), scope="repo")
        self.assertEqual(sorted(ids(compute_view(self.ALL, view, anchor(), NOW))), ["feat-1", "main-1"])

    def test_all_scope_keeps_everything(self):
        view = replace(default_view(anchor()), scope="all")
        self.assertEqual(sorted(ids(compute_view(self.ALL, view, anchor(), NOW))), ["feat-1", "main-1", "other-1"])

    def test_dir_scope_includes_subdirectories(self):
        nested = session(
            id="nested",
            origin_cwd="/repo/api/src/deep",
            cwds=[CwdStat(path="/repo/api/src/deep", count=1, last_seen=NOW, repo_root="/repo/api", repo_key="/repo/api")],
        )
        view = replace(default_view(anchor()), scope="dir")
        self.assertEqual(ids(compute_view([nested, OTHER_REPO], view, anchor(), NOW)), ["nested"])

    def test_session_that_wandered_into_the_repo_counts(self):
        wanderer = session(
            id="wanderer",
            origin_cwd="/home/user",
            repo_key=None,
            repo_root=None,
            repo_name=None,
            cwds=[
                CwdStat(path="/home/user", count=10, last_seen=NOW, repo_root=None, repo_key=None),
                CwdStat(path="/repo/api", count=2, last_seen=NOW, repo_root="/repo/api", repo_key="/repo/api"),
            ],
        )
        view = replace(default_view(anchor()), scope="repo")
        self.assertEqual(ids(compute_view([wanderer], view, anchor(), NOW)), ["wanderer"])

    def test_worktrees_share_a_scope(self):
        worktree = session(
            id="wt",
            origin_cwd="/repo/api-wt",
            repo_root="/repo/api-wt",
            repo_key="/repo/api",
            cwds=[CwdStat(path="/repo/api-wt", count=4, last_seen=NOW, repo_root="/repo/api-wt", repo_key="/repo/api")],
        )
        view = replace(default_view(anchor()), scope="repo")
        self.assertEqual(ids(compute_view([worktree], view, anchor(), NOW)), ["wt"])


class Defaults(unittest.TestCase):
    def test_branch_scope_inside_a_repo(self):
        self.assertEqual(default_view(anchor()).scope, "branch")

    def test_dir_scope_outside_a_repo(self):
        self.assertEqual(default_view(anchor(repo_key=None, repo_name=None, branch=None)).scope, "dir")

    def test_repo_scope_on_detached_head(self):
        self.assertEqual(default_view(anchor(branch=None)).scope, "repo")


class Ordering(unittest.TestCase):
    def test_recent_first_by_default(self):
        older = session(id="old", ended_at=NOW - 100_000)
        newer = session(id="new", ended_at=NOW)
        view = replace(default_view(anchor()), scope="all")
        self.assertEqual(ids(compute_view([older, newer], view, anchor(), NOW)), ["new", "old"])

    def test_query_switches_to_relevance(self):
        recent = session(id="recent", ai_title="zzz kerberos zzz padding padding", ended_at=NOW)
        old_exact = session(id="old", ai_title="kerberos", ended_at=NOW - 10_000_000)
        view = replace(default_view(anchor()), scope="all", query="kerberos")
        self.assertEqual(ids(compute_view([recent, old_exact], view, anchor(), NOW))[0], "old")

    def test_explicit_sort_not_overridden(self):
        first = session(id="a", ai_title="kerberos", ended_at=NOW - 1000)
        second = session(id="b", ai_title="kerberos padding padding", ended_at=NOW)
        view = replace(default_view(anchor()), scope="all", query="kerberos", sort="oldest")
        self.assertEqual(ids(compute_view([first, second], view, anchor(), NOW)), ["a", "b"])

    def test_running_sessions_float_to_top(self):
        live = session(
            id="live",
            ended_at=NOW - 10_000_000,
            live=LiveSession(
                pid=1, session_id="live", cwd="/repo/api", started_at=NOW, updated_at=NOW,
                version="x", kind="interactive", entrypoint="cli",
            ),
        )
        recent = session(id="recent", ended_at=NOW)
        view = replace(default_view(anchor()), scope="all")
        self.assertEqual(ids(compute_view([recent, live], view, anchor(), NOW))[0], "live")

    def test_empty_sessions_hidden_but_reachable(self):
        empty = session(id="empty", turns=0, prompts=[])
        view = replace(default_view(anchor()), scope="all")
        self.assertEqual(ids(compute_view([empty], view, anchor(), NOW)), [])
        self.assertEqual(ids(compute_view([empty], replace(view, hide_empty=False), anchor(), NOW)), ["empty"])

    def test_counts_distinguish_scoped_from_matched(self):
        first = session(id="a", ai_title="alpha")
        second = session(id="b", ai_title="beta")
        view = replace(default_view(anchor()), scope="all", query="alpha")
        result = compute_view([first, second], view, anchor(), NOW)
        self.assertEqual((len(result.hits), result.in_scope_count, result.total_count), (1, 2, 2))


class Facets(unittest.TestCase):
    def test_branches_in_aggregates_most_recent_first(self):
        first = session(branches=[BranchStat(name="main", count=5, last_seen=NOW - 1000)])
        second = session(branches=[BranchStat(name="feature", count=2, last_seen=NOW)])
        self.assertEqual([b[0] for b in branches_in([first, second], anchor(), False)], ["feature", "main"])

    def test_projects_in_groups_by_repo_key(self):
        found = projects_in(
            [session(id="a"), session(id="b"), session(id="c", repo_key="/repo/web", repo_name="web", origin_cwd="/repo/web")]
        )
        by_key = {key: count for key, _name, count, _last in found}
        self.assertEqual(by_key["/repo/api"], 2)
        self.assertEqual(by_key["/repo/web"], 1)


if __name__ == "__main__":
    unittest.main()
