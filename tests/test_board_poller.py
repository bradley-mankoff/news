"""Unit tests for automation/board_poller.py.

Covers the NEEDS INPUT / Blocked-lane / in-place resume machinery and the
review fixes:

- run_status_for: substring matcher for `archon continue` "Prior Context"
  preambles (the resume-never-reconciles bug, CRITICAL)
- issue_has_label: tri-state so a gh failure can never be misread as
  "label absent" (a blocked issue could otherwise ship past the human, HIGH)
- find_issue_pr: tri-state so a gh failure can never be misread as "no PR"
  (the develop merge / ship-PR head choice would otherwise silently skip the
  develop integration gate, HIGH)
- resolve_worktree_branch: repo-scoped parsing with logged failures and no
  silent fallback to the invalid shorthand (HIGH)
- resume_issue: defers when the worktree branch can't be resolved, removes
  the needs-input label BEFORE spawning (a failed label edit leaves no child,
  so the caller's fresh-dispatch fallback can never double-run the issue),
  and verifies the spawned run actually started (HIGH)
- poll(): todo-lane resume-vs-dispatch branch selection and the completion
  reconciliation blocked-lane gate (label None -> stays put, True ->
  Blocked, False -> develop merge)

Follows the repo's unittest + MagicMock conventions (see tests/test_pipeline_helpers.py).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from contextlib import ExitStack
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


class FindIssuePrTests(unittest.TestCase):
    """HIGH: a gh failure must return "error" (undetermined), never None —
    the callers gate the develop merge and the ship-PR head choice on this,
    and "no PR" would silently skip the develop integration merge."""

    def _fake(self, returncode: int = 0, stdout: str = "[]",
              stderr: str = "") -> MagicMock:
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_returns_pr_when_body_references_issue(self) -> None:
        prs = json.dumps([{"number": 60, "title": "Ship: license",
                           "body": "Implements the license.\n\nFixes #21",
                           "headRefName": "archon/task-issue-21",
                           "baseRefName": "develop", "state": "OPEN"}])
        with patch.object(bp, "gh", return_value=self._fake(stdout=prs)):
            pr = bp.find_issue_pr(_cfg(), {}, 21)
        self.assertIsNotNone(pr)
        self.assertEqual(pr["number"], 60)

    def test_returns_pr_when_title_references_issue(self) -> None:
        prs = json.dumps([{"number": 61, "title": "Ship: license (#21)",
                           "body": "", "headRefName": "archon/task-issue-21",
                           "baseRefName": "main", "state": "OPEN"}])
        with patch.object(bp, "gh", return_value=self._fake(stdout=prs)):
            pr = bp.find_issue_pr(_cfg(), {}, 21)
        self.assertEqual(pr["number"], 61)

    def test_returns_none_when_no_pr_references_issue(self) -> None:
        prs = json.dumps([{"number": 62, "title": "Unrelated",
                           "body": "No reference here.",
                           "headRefName": "x", "baseRefName": "develop",
                           "state": "OPEN"}])
        with patch.object(bp, "gh", return_value=self._fake(stdout=prs)):
            self.assertIsNone(bp.find_issue_pr(_cfg(), {}, 21))

    def test_returns_error_and_logs_when_gh_fails(self) -> None:
        with patch.object(bp, "gh",
                          return_value=self._fake(
                              returncode=1, stderr="rate limit exceeded")), \
             patch.object(bp, "log") as log:
            self.assertEqual(bp.find_issue_pr(_cfg(), {}, 21), "error")
        log.assert_called_once()
        self.assertIn("PR LIST FAILED", log.call_args[0][0])


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

    def test_skips_json_log_lines_inside_repo_section(self) -> None:
        """archon's JSON log lines land on stdout and must never be read as
        the worktree branch (merged from the main-side review hardening)."""
        listing = (
            f"https://github.com/{REPO}.git:\n"
            "  archon/task-issue-20\n"
            '    {"ts": "2026-08-02T10:00:00Z", "msg": "resuming archon/task-issue-21"}\n'
            "  archon/task-issue-21\n"
            "    Path: /tmp/news/worktrees/archon/task-issue-21\n"
        )
        with patch.object(bp.subprocess, "run", return_value=self._fake(listing)):
            self.assertEqual(
                bp.resolve_worktree_branch({}, 21, REPO),
                "archon/task-issue-21")

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
        """A run that dies instantly must not be reported as resumed. The
        label is already removed by then (it gates the spawn), so the caller's
        fresh dispatch replaces the dead run without a re-block cycle — but a
        second run is only ever started after the child is confirmed dead."""
        proc = MagicMock()
        proc.poll.return_value = 1
        comment = MagicMock(returncode=0, stdout="answer")
        label = MagicMock(returncode=0, stdout="")
        gh_calls = iter([comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time"), \
             patch.object(bp.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(bp, "gh",
                          side_effect=lambda *a, **k: next(gh_calls)) as gh, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        popen.assert_called_once()
        # the label edit (the spawn gate) happened before the dead child
        self.assertTrue(any("--remove-label" in c.args[0]
                            for c in gh.call_args_list))

    def test_fails_when_label_removal_fails(self) -> None:
        """The label edit gates the spawn: a failed removal must leave NO
        child running, so the caller's fresh-dispatch fallback cannot start a
        second concurrent run for the same issue/worktree (the double-run
        hazard the reorder eliminates)."""
        comment = MagicMock(returncode=0, stdout="answer")
        label = MagicMock(returncode=1, stdout="", stderr="boom")
        gh_calls = iter([comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp.subprocess, "Popen") as popen, \
             patch.object(bp, "gh", side_effect=lambda *a, **k: next(gh_calls)), \
             patch.object(bp, "log") as log, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        popen.assert_not_called()  # no child -> caller fallback is safe
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


class PollFlowTest(unittest.TestCase):
    """poll() branch selection and completion reconciliation for the resume
    flow (ported from the main-side review hardening)."""

    def _item(self, item_id: str = "item-1", number: int = 31,
              status: str = "In Progress") -> dict:
        return {
            "id": item_id,
            "fieldValueByName": {"name": status},
            "content": {
                "__typename": "Issue",
                "number": number,
                "title": f"Issue {number}",
                "url": f"https://github.com/bradley-mankoff/news/issues/{number}",
                "repository": {"nameWithOwner": "bradley-mankoff/news"},
                "labels": {"nodes": [{"name": "needs-input"}]},
            },
        }

    def _project(self, items: list[dict]) -> tuple[str, str, dict, list[dict]]:
        return ("project-1", "field-1",
                {"Backlog": "b", "Todo": "t", "In Progress": "ip",
                 "Blocked": "bl", "Ready for Review": "r",
                 "In Review": "rv", "Done": "d"},
                items)

    MSG = "Implement GitHub issue #31: Some issue (bradley-mankoff/news). Full issue: https://github.com/bradley-mankoff/news/issues/31"

    def _in_progress_state(self, item_id: str = "item-1") -> dict:
        return {
            "_meta": {"snapshot_done": True},
            item_id: {"status": "In Progress", "dispatch_msg": self.MSG,
                      "issue_number": 31},
        }

    def _poll_patches(self, completed: bool = True, label_state=None):
        """Entered via ExitStack; returns (stack, move_to_lane_mock)."""
        stack = ExitStack()
        stack.enter_context(patch(
            "automation.board_poller.fetch_project",
            return_value=self._project([self._item()])))
        stack.enter_context(patch(
            "automation.board_poller.fetch_runs_by_message",
            return_value={self.MSG: "completed" if completed else "running"}))
        stack.enter_context(patch(
            "automation.board_poller.issue_has_label", return_value=label_state))
        move = stack.enter_context(patch(
            "automation.board_poller.move_to_lane", return_value=True))
        stack.enter_context(patch("automation.board_poller.log"))
        stack.enter_context(patch("automation.board_poller.save_state"))
        return stack, move

    # -- completion reconciliation -----------------------------------------

    def test_label_check_failure_leaves_item_in_progress(self) -> None:
        """gh failure (None) must not merge or move a NEEDS INPUT issue."""
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=None)
        with stack:
            bp.poll(_cfg(), {}, state)
        move.assert_not_called()
        # dispatch_msg kept -> retried next poll
        self.assertEqual(state["item-1"]["dispatch_msg"], self.MSG)

    def test_needs_input_label_moves_item_to_blocked(self) -> None:
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=True)
        with stack:
            bp.poll(_cfg(), {}, state)
        move.assert_called_once()
        self.assertEqual(move.call_args.args[3], "item-1")
        self.assertEqual(move.call_args.args[5], "bl")  # Blocked option id
        self.assertNotIn("dispatch_msg", state["item-1"])

    def test_no_label_proceeds_to_develop_merge(self) -> None:
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=False)
        stack.enter_context(patch(
            "automation.board_poller.find_issue_pr",
            return_value={"number": 60, "headRefName": "archon/task-issue-31"}))
        stack.enter_context(patch(
            "automation.board_poller.merge_pr_to_base", return_value=(True, "merged")))
        with stack:
            bp.poll(_cfg(), {}, state)
        move.assert_called_once()
        self.assertEqual(move.call_args.args[5], "r")  # Ready for Review
        self.assertNotIn("dispatch_msg", state["item-1"])

    def test_pr_lookup_failure_leaves_item_in_progress(self) -> None:
        """gh failure on PR lookup ("error") must not be read as "no PR":
        the item stays put, dispatch_msg is retained, nothing merges."""
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=False)
        stack.enter_context(patch(
            "automation.board_poller.find_issue_pr", return_value="error"))
        merge = stack.enter_context(patch(
            "automation.board_poller.merge_pr_to_base"))
        with stack:
            bp.poll(_cfg(), {}, state)
        move.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(state["item-1"]["dispatch_msg"], self.MSG)

    def test_pr_lookup_failure_defers_review_lane(self) -> None:
        """gh failure on PR lookup must not create a ship PR from a guessed
        head branch; the item's status is not recorded, so the review lane is
        re-entered (and the lookup retried) next poll."""
        state = {"_meta": {"snapshot_done": True},
                 "item-1": {"status": "Ready for Review"}}
        item = self._item(status="In Review")
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project([item])), \
             patch("automation.board_poller.find_issue_pr",
                   return_value="error"), \
             patch("automation.board_poller.merge_pr_to_base") as merge, \
             patch("automation.board_poller.find_or_create_ship_pr") as ship, \
             patch("automation.board_poller.dispatch") as dispatch, \
             patch("automation.board_poller.save_state"), \
             patch("automation.board_poller.log") as log:
            bp.poll(_cfg(), {}, state)
        merge.assert_not_called()
        ship.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(state["item-1"]["status"], "Ready for Review")
        self.assertTrue(any("PR LOOKUP DEFERRED" in c.args[0]
                            for c in log.call_args_list))

    # -- todo-lane resume-vs-dispatch selection ----------------------------

    def _todo_state(self) -> dict:
        return {
            "_meta": {"snapshot_done": True},
            "item-1": {"status": "Blocked", "branch": "issue-31",
                       "wf": "archon-idea-to-pr"},
        }

    def _poll_todo(self, state: dict, resume_result=None):
        cfg = _cfg()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(
                       [self._item(status="Todo")])), \
             patch("automation.board_poller.resume_issue",
                   return_value=resume_result) as resume, \
             patch("automation.board_poller.dispatch") as dispatch, \
             patch("automation.board_poller.move_to_lane", return_value=True), \
             patch("automation.board_poller.save_state"), \
             patch("automation.board_poller.log"):
            bp.poll(cfg, {}, state)
        return resume, dispatch

    def test_todo_entry_with_needs_input_label_resumes(self) -> None:
        resume, dispatch = self._poll_todo(
            self._todo_state(),
            resume_result=(True, "Resuming issue #31 after human input.",
                           "archon/task-issue-31"))
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[2], "issue-31")
        self.assertEqual(resume.call_args.args[3], "archon-idea-to-pr")
        dispatch.assert_not_called()

    def test_todo_entry_without_label_dispatches_fresh(self) -> None:
        state = self._todo_state()
        item = self._item(status="Todo")
        item["content"]["labels"] = {"nodes": []}
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project([item])), \
             patch("automation.board_poller.resume_issue") as resume, \
             patch("automation.board_poller.dispatch",
                   return_value=True) as dispatch, \
             patch("automation.board_poller.move_to_lane", return_value=True), \
             patch("automation.board_poller.save_state"), \
             patch("automation.board_poller.log"):
            bp.poll(_cfg(), {}, state)
        resume.assert_not_called()
        dispatch.assert_called_once()

    def test_failed_resume_falls_back_to_fresh_dispatch(self) -> None:
        state = self._todo_state()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(
                       [self._item(status="Todo")])), \
             patch("automation.board_poller.resume_issue",
                   return_value=(False, "", None)), \
             patch("automation.board_poller.dispatch",
                   return_value=True) as dispatch, \
             patch("automation.board_poller.move_to_lane", return_value=True), \
             patch("automation.board_poller.save_state"), \
             patch("automation.board_poller.log"):
            bp.poll(_cfg(), {}, state)
        dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
