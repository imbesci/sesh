import os
import shutil
import tempfile
import unittest

from sesh.core.paths import encode_project_dir
from sesh.core.resume import ResumeError, ResumePlan, plan_resume, plan_to_shell, resume_targets
from sesh.core.types import CwdStat, LiveSession

from .fixtures import NOW, session


class RealDirSession:
    """A session whose recorded directory really exists and encodes correctly."""

    def __init__(self, **over):
        self.dir = tempfile.mkdtemp(prefix="sesh-resume-")
        self.meta = session(
            id="sess-1",
            origin_cwd=self.dir,
            project_dir=encode_project_dir(self.dir),
            cwds=[CwdStat(path=self.dir, count=5, last_seen=NOW, repo_root=self.dir, repo_key=self.dir)],
            **over,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        shutil.rmtree(self.dir, ignore_errors=True)


class EncodeProjectDir(unittest.TestCase):
    def test_matches_claude_codes_encoding(self):
        self.assertEqual(encode_project_dir("/Users/alice/dev/api"), "-Users-alice-dev-api")

    def test_is_lossy_which_is_why_cwd_comes_from_records(self):
        self.assertEqual(encode_project_dir("/a/b-c"), encode_project_dir("/a/b/c"))


class ResumeTargets(unittest.TestCase):
    def test_prefers_existing_directory_with_matching_project(self):
        with RealDirSession() as fixture:
            fixture.meta.cwds.append(
                CwdStat(path="/gone/elsewhere", count=1, last_seen=NOW, repo_root=None, repo_key=None)
            )
            targets = resume_targets(fixture.meta)
            self.assertEqual(targets[0].cwd, fixture.dir)
            self.assertTrue(targets[0].exact_project)
            self.assertTrue(targets[0].exists)

    def test_falls_back_to_other_directories(self):
        with RealDirSession() as fixture:
            fixture.meta.origin_cwd = "/gone/first"
            fixture.meta.cwds = [
                CwdStat(path="/gone/first", count=9, last_seen=NOW, repo_root=None, repo_key=None),
                CwdStat(path=fixture.dir, count=1, last_seen=NOW, repo_root=fixture.dir, repo_key=fixture.dir),
            ]
            self.assertEqual(resume_targets(fixture.meta)[0].cwd, fixture.dir)


class PlanResume(unittest.TestCase):
    def test_builds_expected_invocation(self):
        with RealDirSession() as fixture:
            plan = plan_resume(fixture.meta)
            self.assertEqual(plan.command, "claude")
            self.assertEqual(plan.args, ["--resume", "sess-1"])
            self.assertEqual(plan.cwd, fixture.dir)
            self.assertEqual(plan.warnings, [])

    def test_adds_fork_session(self):
        with RealDirSession() as fixture:
            plan = plan_resume(fixture.meta, fork=True)
            self.assertEqual(plan.args, ["--resume", "sess-1", "--fork-session"])

    def test_skip_permissions_leads_the_invocation(self):
        with RealDirSession() as fixture:
            plan = plan_resume(fixture.meta, skip_permissions=True)
            self.assertEqual(plan.args, ["--dangerously-skip-permissions", "--resume", "sess-1"])

    def test_skip_permissions_composes_with_fork(self):
        with RealDirSession() as fixture:
            plan = plan_resume(fixture.meta, fork=True, skip_permissions=True)
            self.assertEqual(
                plan.args, ["--dangerously-skip-permissions", "--resume", "sess-1", "--fork-session"]
            )

    def test_defaults_to_no_skip_flag(self):
        # The pure function stays explicit: callers opt in. The default-on
        # behaviour lives in the App and CLI, not here.
        with RealDirSession() as fixture:
            self.assertNotIn("--dangerously-skip-permissions", plan_resume(fixture.meta).args)

    def test_errors_when_directory_is_gone(self):
        meta = session(origin_cwd="/definitely/not/here", cwds=[])
        with self.assertRaises(ResumeError):
            plan_resume(meta)

    def test_warns_when_override_directory_would_not_resolve(self):
        with RealDirSession() as fixture:
            plan = plan_resume(fixture.meta, cwd="/some/other/place")
            self.assertEqual(plan.cwd, "/some/other/place")
            self.assertIn("scopes session lookup by directory", " ".join(plan.warnings))

    def test_warns_when_session_is_already_open(self):
        live = LiveSession(
            pid=99, session_id="sess-1", cwd="/x", started_at=NOW, updated_at=NOW,
            version="v", kind="interactive", entrypoint="cli",
        )
        with RealDirSession(live=live) as fixture:
            plan = plan_resume(fixture.meta)
            self.assertIn("99", " ".join(plan.warnings))


class PlanToShell(unittest.TestCase):
    def test_produces_runnable_command(self):
        plan = ResumePlan(command="claude", args=["--resume", "abc"], cwd="/repo/api")
        self.assertEqual(plan_to_shell(plan), "cd /repo/api && claude --resume abc")

    def test_quotes_paths_with_spaces(self):
        plan = ResumePlan(command="claude", args=["--resume", "abc"], cwd="/My Projects/api")
        self.assertEqual(plan_to_shell(plan), "cd '/My Projects/api' && claude --resume abc")

    def test_escapes_embedded_quote(self):
        plan = ResumePlan(command="claude", args=["--resume", "abc"], cwd="/it's/here")
        self.assertIn("'/it'\"'\"'s/here'", plan_to_shell(plan))


if __name__ == "__main__":
    unittest.main()
