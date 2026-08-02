"""Unit tests for automation/board_poller.py.

Covers the NEEDS INPUT / Blocked-lane / in-place resume machinery and the
review fixes:

- run_status_for: substring matcher for `archon continue` "Prior Context"
  preambles (the resume-never-reconciles bug, CRITICAL)
- issue_has_label: tri-state so a gh failure can never be misread as
  "label absent" (a blocked issue could otherwise ship past the human, HIGH)
- resolve_worktree_branch: repo-scoped parsing with logged failures and no
  silent fallback to the invalid shorthand (HIGH)
- resume_issue: defers when the worktree branch can't be resolved, verifies
  the spawned run actually started, and only removes the needs-input label
  after a verified spawn and successful label edit (HIGH)

Follows the repo's unittest + MagicMock conventions (see tests/test_pipeline_helpers.py).
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, mock_open, patch

from automation import board_poller as bp

REPO = "bradley-mankoff/news"


def _cfg() -> dict:
    return {
        "repo": REPO,
        "project_owner": "bradley-mankoff",
        "project_number": 1,
        "state_file": "automation/state.json",
        "default_lane": "Backlog",
        "lanes": {
            "Backlog": "backlog",
            "Todo": "todo",
            "In Progress": "in_progress",
            "Blocked": "blocked",
            "Ready for Review": "ready",
            "In Review": "review",
            "Done": "done",
        },
        "dispatch": {
            "todo": {
                "default": "archon-fix-github-issue",
                "move_to": "In Progress",
                "complete_move_to": "Ready for Review",
                "merge_develop_base": "develop",
                "label_overrides": {"feature": "archon-idea-to-pr"},
            },
            "review": {
                "workflow": "archon-smart-pr-review",
                "ship_to": "main",
                "merge_ship_on_review_complete": True,
                "done_lane": "Done",
            },
        },
    }


ISOLATION_LIST = (
    "https://github.com/someone/other.git:\n"
    "  archon/task-issue-21\n"          # same-named worktree, wrong repo
    "    Path: /tmp/other/worktrees/archon/task-issue-21\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    f"https://github.com/{REPO}.git:\n"
    "  archon/task-issue-20\n"
    "    Path: /tmp/news/worktrees/archon/task-issue-20\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    "  archon/task-issue-21\n"          # the target worktree
    "    Path: /tmp/news/worktrees/archon/task-issue-21\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    "  archon/task-issue-210\n"         # must NOT match issue 21 (\b boundary)
    "    Path: /tmp/news/worktrees/archon/task-issue-210\n"
    "  archon/task-review-issue-21\n"   # must NOT match (review worktree)
    "    Path: /tmp/news/worktrees/archon/task-review-issue-21\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
)


class RunStatusForTests(unittest.TestCase):
    """CRITICAL: resumed runs must reconcile despite the 'Prior Context'
    preamble that `archon continue` prepends to the run message."""

    def test_matches_run_message_with_continue_preamble(self) -> None:
        runs = {
            "Prior Context: ...\n\nResuming issue #21 after human input.": "completed",
        }
        self.assertEqual(
            bp.run_status_for(runs, "Resuming issue #21 after human input."),
            "completed")

    def test_matches_dispatch_message_without_preamble(self) -> None:
        runs = {"Implement GitHub issue #21: Choose a license": "completed"}
        self.assertEqual(
            bp.run_status_for(runs, "Implement GitHub issue #21: Choose a license"),
            "completed")

    def test_returns_none_when_no_run_contains_message(self) -> None:
        self.assertIsNone(bp.run_status_for({"Unrelated run": "completed"},
                                            "Resuming issue #21"))

    def test_fetch_runs_by_message_newest_wins_per_message(self) -> None:
        body = {"runs": [
            {"user_message": "same message", "status": "failed",
             "started_at": "2026-08-02T10:00:00Z"},
            {"user_message": "same message", "status": "completed",
             "started_at": "2026-08-02T11:00:00Z"},
            {"user_message": "Prior Context: ...\nResuming issue #21",
             "status": "completed", "started_at": "2026-08-02T12:00:00Z"},
        ]}
        fake = MagicMock(returncode=0, stdout=__import__("json").dumps(body))
        with patch.object(bp.subprocess, "run", return_value=fake):
            runs = bp.fetch_runs_by_message({})
        self.assertEqual(runs["same message"], "completed")
        self.assertEqual(
            bp.run_status_for(runs, "Resuming issue #21"), "completed")


class IssueHasLabelTests(unittest.TestCase):
    """HIGH: a gh failure must return None (undetermined), never False —
    the caller defers instead of routing a blocked issue into the normal
    completion path (develop merge + ship PR)."""

    def test_true_when_label_present(self) -> None:
        fake = MagicMock(returncode=0, stdout="bug\nneeds-input\n")
        with patch.object(bp, "gh", return_value=fake):
            self.assertTrue(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))

    def test_false_when_label_absent(self) -> None:
        fake = MagicMock(returncode=0, stdout="bug\n")
        with patch.object(bp, "gh", return_value=fake):
            self.assertFalse(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))

    def test_none_and_logged_when_gh_fails(self) -> None:
        fake = MagicMock(returncode=1, stdout="", stderr="rate limit exceeded")
        with patch.object(bp, "gh", return_value=fake), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))
        log.assert_called_once()
        self.assertIn("LABEL CHECK FAILED", log.call_args[0][0])

    def test_none_when_gh_times_out(self) -> None:
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("gh", timeout=90)

        with patch.object(bp, "gh", side_effect=boom), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))
        log.assert_called_once()
        self.assertIn("LABEL CHECK TIMEOUT", log.call_args[0][0])


class ResolveWorktreeBranchTests(unittest.TestCase):
    """HIGH: repo-scoped parse; failures logged; no silent wrong match."""

    def _fake(self, stdout: str = ISOLATION_LIST, returncode: int = 0) -> MagicMock:
        return MagicMock(returncode=returncode, stdout=stdout)

    def test_parses_own_repo_section_only(self) -> None:
        with patch.object(bp.subprocess, "run", return_value=self._fake()):
            self.assertEqual(
                bp.resolve_worktree_branch({}, 21, REPO),
                "archon/task-issue-21")

    def test_ignores_same_named_worktree_in_other_repo(self) -> None:
        """The other repo's archon/task-issue-21 appears FIRST in the listing;
        the scoped parse must skip it and still find this repo's."""
        with patch.object(bp.subprocess, "run", return_value=self._fake()):
            result = bp.resolve_worktree_branch({}, 21, REPO)
        self.assertEqual(result, "archon/task-issue-21")
        self.assertNotEqual(result, "archon/task-issue-21\n    Path: /tmp/other/...")

    def test_returns_none_when_repo_section_missing(self) -> None:
        listing = "https://github.com/someone/other.git:\n  archon/task-issue-21\n"
        with patch.object(bp.subprocess, "run",
                          return_value=self._fake(stdout=listing)):
            self.assertIsNone(bp.resolve_worktree_branch({}, 21, REPO))

    def test_returns_none_and_logs_on_archon_failure(self) -> None:
        with patch.object(bp.subprocess, "run",
                          return_value=self._fake(returncode=1)), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.resolve_worktree_branch({}, 21, REPO))
        log.assert_called_once()
        self.assertIn("WORKTREE LIST FAILED", log.call_args[0][0])

    def test_returns_none_on_empty_output(self) -> None:
        with patch.object(bp.subprocess, "run", return_value=self._fake(stdout="")):
            self.assertIsNone(bp.resolve_worktree_branch({}, 21, REPO))


class ResumeIssueTests(unittest.TestCase):
    """HIGH: defer when the branch can't be resolved; verify the spawn;
    only remove the needs-input label after verified spawn + successful edit."""

    def test_deferred_when_worktree_branch_not_found(self) -> None:
        with patch.object(bp, "resolve_worktree_branch", return_value=None), \
             patch.object(bp, "gh") as gh, \
             patch.object(bp, "subprocess") as sub:
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertEqual(full, None)
        gh.assert_not_called()
        sub.Popen.assert_not_called()

    def test_happy_path_spawns_continue_and_removes_label(self) -> None:
        proc = MagicMock()
        proc.pid = 42
        proc.poll.return_value = None  # still running after grace period
        comment = MagicMock(returncode=0, stdout="The answer is: Apache-2.0")
        label = MagicMock(returncode=0, stdout="")
        gh = MagicMock(side_effect=[comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time") as time, \
             patch.object(bp.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(bp, "gh", gh), \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertTrue(ok)
        self.assertEqual(full, "archon/task-issue-21")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:4],
                         ["archon", "continue", "archon/task-issue-21",
                          "--workflow"])
        self.assertIn("The answer is: Apache-2.0", msg)
        time.sleep.assert_called_once_with(2)
        # label removal invoked with --remove-label needs-input
        remove = [c for c in gh.call_args_list
                  if "--remove-label" in c.args[0]][0].args[0]
        self.assertIn("needs-input", remove)

    def test_fails_when_continue_exits_immediately(self) -> None:
        """A run that dies instantly must not be reported as resumed, and the
        needs-input label must stay (it is the recovery signal)."""
        proc = MagicMock()
        proc.poll.return_value = 1
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time"), \
             patch.object(bp.subprocess, "Popen", return_value=proc), \
             patch.object(bp, "gh") as gh, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        # the comment fetch may have happened, but the needs-input label
        # removal must NOT have been attempted
        self.assertFalse(any("--remove-label" in c.args[0]
                             for c in gh.call_args_list))

    def test_fails_when_label_removal_fails(self) -> None:
        """A failed label removal would re-block the next completed run; the
        resume must not be reported as successful."""
        proc = MagicMock()
        proc.poll.return_value = None
        comment = MagicMock(returncode=0, stdout="answer")
        label = MagicMock(returncode=1, stdout="", stderr="boom")
        gh_calls = iter([comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time"), \
             patch.object(bp.subprocess, "Popen", return_value=proc), \
             patch.object(bp, "gh", side_effect=lambda *a, **k: next(gh_calls)), \
             patch.object(bp, "log") as log, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        self.assertTrue(any("LABEL REMOVE FAILED" in c.args[0]
                            for c in log.call_args_list))

    def test_fails_on_oserror_from_popen(self) -> None:
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp.subprocess, "Popen", side_effect=OSError("no archon")), \
             patch.object(bp, "gh",
                          return_value=MagicMock(returncode=0, stdout="")), \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)


class PollResumeBranchTests(unittest.TestCase):
    """HIGH (caller side): on a successful resume, state must record the FULL
    namespaced branch so a second resume round never falls back to the
    shorthand; a fresh dispatch keeps the shorthand."""

    def _item(self, lane: str = "Todo", labels: list[str] | None = None) -> dict:
        return {
            "id": "item1",
            "content": {
                "__typename": "Issue",
                "number": 21,
                "title": "Choose a project license",
                "url": f"https://github.com/{REPO}/issues/21",
                "repository": {"nameWithOwner": REPO},
                "labels": {"nodes": [{"name": n} for n in (labels or [])]},
            },
            "fieldValueByName": {"name": lane},
        }

    def _project(self, items: list[dict]) -> tuple:
        options = {name: f"opt-{i}" for i, name in enumerate(
            ["Backlog", "Todo", "In Progress", "Blocked",
             "Ready for Review", "In Review", "Done"])}
        return "proj", "field", options, items

    def test_resume_stores_full_branch_in_state(self) -> None:
        cfg = _cfg()
        state = {"_meta": {"snapshot_done": True},
                 "item1": {"status": "In Progress", "branch": "issue-21",
                           "wf": "archon-fix-github-issue",
                           "dispatch_msg": "old-msg", "issue_number": 21}}
        items = [self._item(labels=["needs-input"])]
        with patch.object(bp, "fetch_project", return_value=self._project(items)), \
             patch.object(bp, "resume_issue",
                          return_value=(True, "Resuming issue #21 after human input.",
                                       "archon/task-issue-21")), \
             patch.object(bp, "dispatch") as dispatch, \
             patch.object(bp, "move_to_lane", return_value=True), \
             patch.object(bp, "save_state"):
            bp.poll(cfg, {}, state)
        rec = state["item1"]
        self.assertEqual(rec["branch"], "archon/task-issue-21")
        self.assertEqual(rec["status"], "Todo")
        dispatch.assert_not_called()

    def test_fresh_dispatch_keeps_shorthand_branch(self) -> None:
        cfg = _cfg()
        state = {"_meta": {"snapshot_done": True},
                 "item1": {"status": "In Progress"}}
        items = [self._item(labels=["feature"])]
        with patch.object(bp, "fetch_project", return_value=self._project(items)), \
             patch.object(bp, "resume_issue") as resume, \
             patch.object(bp, "dispatch", return_value=True) as dispatch, \
             patch.object(bp, "move_to_lane", return_value=True), \
             patch.object(bp, "save_state"):
            bp.poll(cfg, {}, state)
        rec = state["item1"]
        self.assertEqual(rec["branch"], "issue-21")
        resume.assert_not_called()
        dispatch.assert_called_once()
        self.assertIn("issue-21", dispatch.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
