"""Unit tests for automation/board_poller.py label/resume helpers.

Covers the Blocked-lane + NEEDS INPUT resume flow added for issue #25:
- `resolve_worktree_branch` parsing of `archon isolation list` output
- `resume_issue` spawn + label removal semantics (incl. fail-closed paths)
- `issue_has_label` fail-closed tri-state (True/False/None)
- `poll()` resume-vs-dispatch branch selection and the completion
  reconciliation blocked-lane gate

External CLIs (gh, archon) and filesystem side effects are mocked; no network
or tooling is required.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import automation.board_poller as poller


def _gh_ok(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class ResolveWorktreeBranchTest(unittest.TestCase):
    """`archon isolation list` stdout parsing (anchored, log-line safe)."""

    REALISH_OUTPUT = (
        '{"level":30,"msg":"db.connection_sqlite_selected"}\n'
        '{"level":30,"msg":"db.sqlite_schema_initialized"}\n'
        "\n"
        "https://github.com/bradley-mankoff/news.git:\n"
        "  archon/task-issue-25\n"
        "    Path: /Users/x/worktrees/archon/task-issue-25\n"
        "    Type: task | Platform: cli | Last activity: 0d ago\n"
    )

    def test_parses_branch_from_isolation_list(self) -> None:
        with patch("automation.board_poller.subprocess.run",
                   return_value=_gh_ok(self.REALISH_OUTPUT)):
            self.assertEqual(poller.resolve_worktree_branch({}, 25),
                             "archon/task-issue-25")

    def test_skips_json_log_lines_and_path_type_lines(self) -> None:
        # A JSON log line mentioning the pattern must not be treated as a
        # branch; Path/Type lines are skipped by the anchored match.
        with patch("automation.board_poller.subprocess.run",
                   return_value=_gh_ok(
                       '{"level":30,"msg":"worktree lookup task-issue-31"}\n'
                       "Path: /tmp/wt\n"
                       "Type: worktree\n"
                       "  archon/task-issue-31\n")):
            self.assertEqual(poller.resolve_worktree_branch({}, 31),
                             "archon/task-issue-31")

    def test_anchored_match_rejects_variant_branches(self) -> None:
        with patch("automation.board_poller.subprocess.run",
                   return_value=_gh_ok(
                       "  archon/task-issue-315\n"
                       "  archon/task-issue-31\n")):
            self.assertEqual(poller.resolve_worktree_branch({}, 31),
                             "archon/task-issue-31")

    def test_none_when_no_match(self) -> None:
        with patch("automation.board_poller.subprocess.run",
                   return_value=_gh_ok("  archon/task-issue-38\n")):
            self.assertIsNone(poller.resolve_worktree_branch({}, 31))

    def test_none_on_subprocess_failure(self) -> None:
        with patch("automation.board_poller.subprocess.run",
                   return_value=_gh_ok("", returncode=1)) as run:
            self.assertIsNone(poller.resolve_worktree_branch({}, 31))
            run.assert_called_once()


class ResumeIssueTest(unittest.TestCase):
    """`archon continue` spawn + needs-input label lifecycle."""

    def test_resumes_with_resolved_branch_and_removes_label(self) -> None:
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value="archon/task-issue-31"), \
             patch("automation.board_poller.gh",
                   side_effect=[_gh_ok("the human answer"), _gh_ok("")]) as gh, \
             patch("automation.board_poller.subprocess.Popen") as popen, \
             patch("automation.board_poller.log"), \
             patch("builtins.open"):
            ok, msg = poller.resume_issue(
                {"repo": "bradley-mankoff/news"}, {}, "issue-31",
                "archon-idea-to-pr", 31)
        self.assertTrue(ok)
        self.assertIn("Resuming issue #31", msg)
        self.assertIn("the human answer", msg)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "archon")
        self.assertEqual(cmd[1], "continue")
        self.assertEqual(cmd[2], "archon/task-issue-31")
        self.assertEqual(cmd[3], "--workflow")
        remove_cmd = gh.call_args_list[1].args[0]
        self.assertIn("--remove-label", remove_cmd)
        self.assertIn("needs-input", remove_cmd)
        self.assertIn("31", " ".join(remove_cmd))

    def test_skips_resume_when_worktree_unresolved(self) -> None:
        """Fail-closed: never resume with the shorthand branch."""
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value=None), \
             patch("automation.board_poller.subprocess.Popen") as popen, \
             patch("automation.board_poller.gh") as gh, \
             patch("automation.board_poller.log") as log, \
             patch("builtins.open"):
            ok, msg = poller.resume_issue(
                {"repo": "bradley-mankoff/news"}, {}, "issue-31",
                "archon-idea-to-pr", 31)
        self.assertFalse(ok)
        self.assertEqual(msg, "")
        popen.assert_not_called()
        gh.assert_not_called()  # label stays; caller falls back to fresh dispatch
        self.assertIn("RESUME SKIPPED issue=31", log.call_args.args[0])

    def test_oserror_on_spawn_returns_false_with_message(self) -> None:
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value="archon/task-issue-31"), \
             patch("automation.board_poller.gh",
                   side_effect=[_gh_ok("answer")]), \
             patch("automation.board_poller.subprocess.Popen",
                   side_effect=OSError("boom")), \
             patch("automation.board_poller.log"), \
             patch("builtins.open"):
            ok, msg = poller.resume_issue(
                {"repo": "bradley-mankoff/news"}, {}, "issue-31",
                "archon-idea-to-pr", 31)
        self.assertFalse(ok)
        self.assertIn("Resuming issue #31", msg)

    def test_label_removal_failure_is_logged_not_fatal(self) -> None:
        """The resume already happened; a lingering label must at least be loud."""
        with patch("automation.board_poller.resolve_worktree_branch",
                   return_value="archon/task-issue-31"), \
             patch("automation.board_poller.gh",
                   side_effect=[_gh_ok("answer"), _gh_ok("", returncode=1)]), \
             patch("automation.board_poller.subprocess.Popen"), \
             patch("automation.board_poller.log") as log, \
             patch("builtins.open"):
            ok, _ = poller.resume_issue(
                {"repo": "bradley-mankoff/news"}, {}, "issue-31",
                "archon-idea-to-pr", 31)
        self.assertTrue(ok)
        logged = " ".join(c.args[0] for c in log.call_args_list)
        self.assertIn("LABEL REMOVAL FAILED issue=31", logged)


class IssueHasLabelTest(unittest.TestCase):
    """Tri-state label check: True / False / None (gh failure)."""

    def test_true_when_label_present(self) -> None:
        with patch("automation.board_poller.gh",
                   return_value=_gh_ok("needs-input\nbug\n")):
            self.assertTrue(poller.issue_has_label(
                {"repo": "bradley-mankoff/news"}, {}, 31, "needs-input"))

    def test_false_when_label_absent(self) -> None:
        with patch("automation.board_poller.gh", return_value=_gh_ok("bug\n")):
            self.assertFalse(poller.issue_has_label(
                {"repo": "bradley-mankoff/news"}, {}, 31, "needs-input"))

    def test_none_and_log_on_gh_failure(self) -> None:
        with patch("automation.board_poller.gh",
                   return_value=_gh_ok("", returncode=1)) as gh, \
             patch("automation.board_poller.log") as log:
            self.assertIsNone(poller.issue_has_label(
                {"repo": "bradley-mankoff/news"}, {}, 31, "needs-input"))
        self.assertIn("LABEL CHECK FAILED issue=31", log.call_args.args[0])
        gh.assert_called_once()


class PollFlowTest(unittest.TestCase):
    """poll() branch selection and completion reconciliation for the resume flow."""

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
        return stack, move

    # -- completion reconciliation -----------------------------------------

    def test_label_check_failure_leaves_item_in_progress(self) -> None:
        """gh failure (None) must not merge or move a NEEDS INPUT issue."""
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=None)
        with stack:
            poller.poll(self._cfg(), {}, state)
        move.assert_not_called()
        # dispatch_msg kept -> retried next poll
        self.assertEqual(state["item-1"]["dispatch_msg"], self.MSG)

    def test_needs_input_label_moves_item_to_blocked(self) -> None:
        state = self._in_progress_state()
        stack, move = self._poll_patches(label_state=True)
        with stack:
            poller.poll(self._cfg(), {}, state)
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
            poller.poll(self._cfg(), {}, state)
        move.assert_called_once()
        self.assertEqual(move.call_args.args[5], "r")  # Ready for Review
        self.assertNotIn("dispatch_msg", state["item-1"])

    # -- todo-lane resume-vs-dispatch selection ----------------------------

    def _todo_state(self) -> dict:
        return {
            "_meta": {"snapshot_done": True},
            "item-1": {"status": "Blocked", "branch": "issue-31",
                       "wf": "archon-idea-to-pr"},
        }

    def _poll_todo(self, state: dict, resume_result=None):
        cfg = self._cfg()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(
                       [self._item(status="Todo")])), \
             patch("automation.board_poller.resume_issue",
                   return_value=resume_result) as resume, \
             patch("automation.board_poller.dispatch") as dispatch, \
             patch("automation.board_poller.move_to_lane", return_value=True), \
             patch("automation.board_poller.log"):
            poller.poll(cfg, {}, state)
        return resume, dispatch

    def test_todo_entry_with_needs_input_label_resumes(self) -> None:
        resume, dispatch = self._poll_todo(
            self._todo_state(), resume_result=(True, "Resuming issue #31 after human input."))
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
             patch("automation.board_poller.log"):
            poller.poll(self._cfg(), {}, state)
        resume.assert_not_called()
        dispatch.assert_called_once()

    def test_failed_resume_falls_back_to_fresh_dispatch(self) -> None:
        state = self._todo_state()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(
                       [self._item(status="Todo")])), \
             patch("automation.board_poller.resume_issue",
                   return_value=(False, "")), \
             patch("automation.board_poller.dispatch",
                   return_value=True) as dispatch, \
             patch("automation.board_poller.move_to_lane", return_value=True), \
             patch("automation.board_poller.log"):
            poller.poll(self._cfg(), {}, state)
        dispatch.assert_called_once()

    @staticmethod
    def _cfg() -> dict:
        return poller.load_config()


if __name__ == "__main__":
    unittest.main()
