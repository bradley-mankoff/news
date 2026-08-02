from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import board_poller as poller

ROOT = Path(__file__).resolve().parent.parent

CFG = {"repo": "bradley-mankoff/news"}


class RunStatusTests(unittest.TestCase):
    def test_run_status_for_matches_substring_and_takes_first(self) -> None:
        # `archon continue` prepends a "Prior Context" preamble, so the
        # resumed run's message CONTAINS (not equals) the dispatch message.
        runs = {
            "Prior Context...\nResuming issue #7 after human input. Latest comment: yes":
                "running",
            "Implement GitHub issue #7: old run": "completed",
        }
        self.assertEqual(
            poller.run_status_for(runs, "Resuming issue #7 after human input."),
            "running",
        )

    def test_run_status_for_returns_none_when_no_message_contains(self) -> None:
        self.assertIsNone(poller.run_status_for({"other": "completed"}, "dispatch msg"))

    def test_fetch_runs_by_message_keeps_newest_run_per_message(self) -> None:
        payload = json.dumps({"runs": [
            {"user_message": "Implement #7", "status": "completed",
             "started_at": "2026-08-02T10:00:00Z"},
            {"user_message": "Implement #7", "status": "running",
             "started_at": "2026-08-02T11:00:00Z"},
            {"user_message": "other", "status": "failed",
             "started_at": "2026-08-02T09:00:00Z"},
        ]})
        with patch("automation.board_poller.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=payload)
            result = poller.fetch_runs_by_message({})
        self.assertEqual(result, {"Implement #7": "running", "other": "failed"})

    def test_fetch_runs_by_message_returns_empty_on_gh_failure(self) -> None:
        with patch("automation.board_poller.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stderr="boom")
            self.assertEqual(poller.fetch_runs_by_message({}), {})


class IssueLabelTests(unittest.TestCase):
    def test_issue_has_label_parses_label_names(self) -> None:
        r = subprocess.CompletedProcess([], 0, stdout="bug\nneeds-input\n")
        with patch("automation.board_poller.gh", return_value=r):
            self.assertTrue(poller.issue_has_label(CFG, {}, 7, "needs-input"))
        with patch("automation.board_poller.gh", return_value=r):
            self.assertFalse(poller.issue_has_label(CFG, {}, 7, "enhancement"))

    def test_issue_has_label_false_when_gh_fails(self) -> None:
        r = subprocess.CompletedProcess([], 1, stdout="", stderr="rate limited")
        with patch("automation.board_poller.gh", return_value=r):
            self.assertFalse(poller.issue_has_label(CFG, {}, 7, "needs-input"))


class WorktreeBranchTests(unittest.TestCase):
    def test_resolve_worktree_branch_skips_header_lines(self) -> None:
        out = "Path /x\nType worktree\narchon/task-issue-7\n"
        with patch("automation.board_poller.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=out)
            self.assertEqual(poller.resolve_worktree_branch({}, 7),
                             "archon/task-issue-7")

    def test_resolve_worktree_branch_ignores_other_issues(self) -> None:
        out = "archon/task-issue-6\narchon/task-issue-8\n"
        with patch("automation.board_poller.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=out)
            self.assertIsNone(poller.resolve_worktree_branch({}, 7))

    def test_resolve_worktree_branch_none_on_failure(self) -> None:
        with patch("automation.board_poller.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stderr="boom")
            self.assertIsNone(poller.resolve_worktree_branch({}, 7))


class ResumeIssueTests(unittest.TestCase):
    def test_resume_issue_removes_label_after_spawn(self) -> None:
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value="archon/task-issue-7"), \
             patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.subprocess.Popen") as popen:
            gh.return_value = subprocess.CompletedProcess([], 0, stdout="a comment\n")
            ok, msg = poller.resume_issue(CFG, {}, "issue-7", "wf", 7)
        self.assertTrue(ok)
        self.assertIn("issue #7", msg)
        popen.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "archon")
        self.assertEqual(args[1], "continue")
        self.assertEqual(args[2], "archon/task-issue-7")
        remove_calls = [c.args[0] for c in gh.call_args_list
                        if "--remove-label" in c.args[0]]
        self.assertTrue(remove_calls, "needs-input label must be removed")

    def test_resume_issue_falls_back_to_shorthand_branch(self) -> None:
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value=None), \
             patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.subprocess.Popen") as popen:
            gh.return_value = subprocess.CompletedProcess([], 0, stdout="")
            ok, _ = poller.resume_issue(CFG, {}, "issue-7", "wf", 7)
        self.assertTrue(ok)
        self.assertEqual(popen.call_args.args[0][2], "issue-7")

    def test_resume_issue_returns_false_when_spawn_fails(self) -> None:
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value="archon/task-issue-7"), \
             patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.subprocess.Popen",
                   side_effect=OSError("no archon")):
            gh.return_value = subprocess.CompletedProcess([], 0, stdout="")
            ok, _ = poller.resume_issue(CFG, {}, "issue-7", "wf", 7)
        self.assertFalse(ok)


class MergePrToBaseTests(unittest.TestCase):
    def test_merge_pr_to_base_reopens_auto_closed_issue(self) -> None:
        with patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.time.sleep") as sleep:
            gh.side_effect = [
                subprocess.CompletedProcess([], 0),           # pr ready
                subprocess.CompletedProcess([], 0),           # pr merge
                subprocess.CompletedProcess([], 0, stdout='{"state": "CLOSED"}'),
                subprocess.CompletedProcess([], 0),           # reopen
            ]
            ok, note = poller.merge_pr_to_base(
                CFG, {}, {"number": 1, "state": "OPEN", "baseRefName": "develop"},
                "develop", 7)
        self.assertTrue(ok)
        self.assertIn("reopened", note)
        sleep.assert_called_once_with(6)

    def test_merge_pr_to_base_skips_reopen_when_issue_stays_open(self) -> None:
        with patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.time.sleep") as sleep:
            gh.side_effect = [
                subprocess.CompletedProcess([], 0),           # pr ready
                subprocess.CompletedProcess([], 0),           # pr merge
                subprocess.CompletedProcess([], 0, stdout='{"state": "OPEN"}'),
            ]
            ok, note = poller.merge_pr_to_base(
                CFG, {}, {"number": 1, "state": "OPEN", "baseRefName": "develop"},
                "develop", 7)
        self.assertTrue(ok)
        self.assertNotIn("reopened", note)

    def test_merge_pr_to_base_returns_false_on_merge_failure(self) -> None:
        with patch("automation.board_poller.gh") as gh:
            gh.side_effect = [
                subprocess.CompletedProcess([], 0),           # pr ready
                subprocess.CompletedProcess([], 1, stderr="merge conflict"),
            ]
            ok, note = poller.merge_pr_to_base(
                CFG, {}, {"number": 1, "state": "OPEN", "baseRefName": "develop"},
                "develop", None)
        self.assertFalse(ok)
        self.assertIn("merge conflict", note)

    def test_merge_pr_to_base_short_circuits_when_already_merged(self) -> None:
        with patch("automation.board_poller.gh") as gh:
            ok, note = poller.merge_pr_to_base(
                CFG, {}, {"number": 1, "state": "MERGED", "baseRefName": "develop"},
                "develop", None)
        self.assertTrue(ok)
        self.assertEqual(note, "already merged")
        gh.assert_not_called()


class ShipPrTests(unittest.TestCase):
    def test_find_or_create_ship_pr_reuses_existing_open_pr(self) -> None:
        r = subprocess.CompletedProcess([], 0, stdout=json.dumps([
            {"number": 5, "headRefName": "archon/task-issue-7",
             "baseRefName": "main", "state": "OPEN"},
        ]))
        with patch("automation.board_poller.gh", return_value=r) as gh:
            pr = poller.find_or_create_ship_pr(CFG, {}, "archon/task-issue-7",
                                               "Ship #7", 7, "main")
        self.assertEqual(pr["number"], 5)
        gh.assert_called_once()

    def test_find_or_create_ship_pr_creates_when_missing(self) -> None:
        r = subprocess.CompletedProcess([], 0, stdout="[]")
        created = subprocess.CompletedProcess([], 0, stdout="https://github.com/o/r/pull/9")
        with patch("automation.board_poller.gh", side_effect=[r, created]) as gh:
            pr = poller.find_or_create_ship_pr(CFG, {}, "archon/task-issue-7",
                                               "Ship #7", 7, "main")
        self.assertEqual(pr["number"], 9)
        create_call = [c.args[0] for c in gh.call_args_list
                       if c.args[0][:2] == ["pr", "create"]]
        self.assertTrue(create_call)

    def test_find_or_create_ship_pr_returns_none_on_create_failure(self) -> None:
        r = subprocess.CompletedProcess([], 0, stdout="[]")
        failed = subprocess.CompletedProcess([], 1, stderr="auth expired")
        with patch("automation.board_poller.gh", side_effect=[r, failed]):
            pr = poller.find_or_create_ship_pr(CFG, {}, "archon/task-issue-7",
                                               "Ship #7", 7, "main")
        self.assertIsNone(pr)


if __name__ == "__main__":
    unittest.main()
