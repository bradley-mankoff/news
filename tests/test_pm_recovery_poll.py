from __future__ import annotations

import contextlib
import json
import unittest
from unittest.mock import patch

import automation.pm_harness.cycle as cycle_adapter
import automation.pm_harness.dispatch as dispatch_adapter
import automation.pm_harness.recovery as recovery_adapter
from automation.pm_harness.model import DispatchResult, WorkflowRuns, WorkflowRunStatusMap

class TerminalRecoveryPollTest(unittest.TestCase):
    """Poll-level regression: a cancelled/transient In Progress run records a
    bounded recovery decision, keeps its dispatch marker, never re-dispatches
    dirty worktrees, retries a clean worktree at most once, and logs each
    terminal run ID at most once."""

    def _item(self, item_id="item-92", number=92):
        return {
            "id": item_id,
            "status": "In Progress",
            "content": {
                "__typename": "Issue",
                "number": number,
                "title": "Anchor curated-match prefix",
                "url": f"https://github.com/o/r/issues/{number}",
                "body": "",
                "state": "OPEN",
                "repository": {"nameWithOwner": "o/r"},
                "labels": {"nodes": []},
            },
        }

    def _cfg(self):
        return {
            "repo": "o/r",
            "state_file": "state.json",
            "lanes": {
                "Backlog": "backlog",
                "Todo": "todo",
                "In Progress": "in_progress",
                "Ready for Review": "ready",
                "In Review": "review",
                "Done": "done",
            },
            "dispatch": {
                "todo": {
                    "complete_move_to": "Ready for Review",
                    "merge_develop_base": "develop",
                },
                "review": {
                    "merge_ship_on_approve": False,
                    "ship_to": "main",
                    "done_lane": "Done",
                },
            },
            "deferred_work": {"enabled": True},
        }

    def _state(self, item_id="item-92"):
        return {
            "_meta": {"snapshot_done": True},
            item_id: {
                "status": "In Progress",
                "issue_number": 92,
                "dispatch_msg": "run",
                "wf": "archon-idea-to-pr",
                "branch": "issue-92",
            },
        }

    def _run(self, status="cancelled", run_id="run-1", error=""):
        return {
            "id": run_id,
            "workflow_name": "archon-idea-to-pr",
            "user_message": "run",
            "status": status,
            "metadata": {"error": error},
            "started_at": "2026-08-07T10:00:00Z",
            "completed_at": "2026-08-07T10:05:00Z",
            "working_path": "/work/news",
        }

    def _as_runs(self, runs):
        if isinstance(runs, WorkflowRuns):
            return runs
        if isinstance(runs, list):
            return WorkflowRuns(runs)
        return WorkflowRuns(list(runs or []))

    def _poll(self, state, runs, dirty):
        return [
            patch.object(cycle_adapter, "fetch_project",
                         return_value=("p", "f", {"Ready for Review": "ready"},
                                       [self._item()])),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "fetch_workflow_runs",
                         return_value=self._as_runs(runs)),
            patch.object(cycle_adapter, "fetch_workflow_run", return_value=None),
            patch.object(recovery_adapter, "inspect_worktree", return_value=dirty),
            patch.object(cycle_adapter, "save_state"),
        ]

    def _run_poll(self, state, runs, dirty, dispatch_result):
        stack = contextlib.ExitStack()
        for p in self._poll(state, runs, dirty):
            entered = stack.enter_context(p)
            if getattr(p, "attribute", None) == "fetch_workflow_runs":
                stack.fetch_workflow_runs = entered
            elif getattr(p, "attribute", None) == "inspect_worktree":
                stack.inspect_worktree = entered
        if not isinstance(dispatch_result, DispatchResult):
            dispatch_result = DispatchResult(bool(dispatch_result))
        stack.dispatch = stack.enter_context(
            patch.object(recovery_adapter, "dispatch", return_value=dispatch_result))
        stack.enter_context(
            patch.object(recovery_adapter, "comment_issue", return_value=True))
        stack.enter_context(
            patch.object(recovery_adapter, "note_capacity_deferred", return_value=None))
        return stack

    def test_cancelled_clean_run_records_retry_and_logs_once(self):
        state = self._state()
        with self._run_poll(state, [self._run()], False, False) as stack:
            log = stack.enter_context(patch.object(cycle_adapter, "log"))
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["dispatch_msg"], "run")
        self.assertEqual(rec["recovery"]["action"], "retry_available")
        self.assertEqual(rec["recovery"]["run_id"], "run-1")
        self.assertEqual(rec["recovery"]["failure_class"], "transient")
        self.assertEqual(rec["recovery"]["failed_step"], "")
        self.assertEqual(rec["recovery"]["worktree"]["dirty"], False)
        self.assertEqual(rec["recovery"]["worktree"]["path"], "/work/news")
        self.assertEqual(rec["recovery_logged_run_id"], "run-1")
        recovery_lines = [c.args[0] for c in log.call_args_list
                          if "RUN CANCELLED" in c.args[0]]
        self.assertEqual(len(recovery_lines), 1)
        self.assertIn("transient -> retry_available", recovery_lines[0])

    def test_cancelled_dirty_run_requires_resume_and_never_dispatches(self):
        state = self._state()
        with self._run_poll(state, [self._run()], True, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "resume_required")
        self.assertEqual(rec["dispatch_msg"], "run")
        stack.dispatch.assert_not_called()

    def test_clean_retry_dispatches_once_then_waits(self):
        state = self._state()
        with self._run_poll(state, [self._run()], False, True) as stack:
            log = stack.enter_context(patch.object(cycle_adapter, "log"))
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "retrying")
        self.assertEqual(rec["automatic_retry_count"], 1)
        self.assertTrue(rec["retrying"])
        stack.dispatch.assert_called_once_with(
            self._cfg(), {}, "archon-idea-to-pr", "issue-92", "run",
            "item-92", 92)
        recovery_lines = [c.args[0] for c in log.call_args_list
                          if "RUN CANCELLED" in c.args[0]]
        self.assertEqual(len(recovery_lines), 1)
        self.assertIn("automatic retry dispatched", recovery_lines[0])

    def test_registered_active_retry_keeps_retrying_state(self):
        state = self._state()
        first = self._run(run_id="run-1")
        retry = self._run(status="running", run_id="run-2")
        retry["started_at"] = "2026-08-07T10:10:00Z"
        with self._run_poll(state, [first], False, True) as stack:
            stack.fetch_workflow_runs.side_effect = [
                self._as_runs([first]),
                self._as_runs([first, retry]),
            ]
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertTrue(rec["retrying"])
        self.assertEqual(rec["automatic_retry_count"], 1)
        self.assertEqual(rec["recovery"]["run_id"], "run-1")
        stack.dispatch.assert_called_once()

    def test_registered_second_terminal_run_requires_manual_review(self):
        state = self._state()
        first = self._run(run_id="run-1")
        second = self._run(status="failed", run_id="run-2",
                           error="connection reset by peer")
        second["started_at"] = "2026-08-07T10:10:00Z"
        with self._run_poll(state, [first], False, True) as stack:
            stack.fetch_workflow_runs.side_effect = [
                self._as_runs([first]),
                self._as_runs([first, second]),
            ]
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["automatic_retry_count"], 1)
        self.assertEqual(rec["recovery"]["run_id"], "run-2")
        self.assertEqual(rec["recovery"]["action"], "manual_review")
        stack.dispatch.assert_called_once()

    def test_status_and_recovery_use_newest_same_timestamp_run(self):
        state = self._state()
        terminal = self._run(run_id="run-1")
        active = self._run(status="running", run_id="run-2")
        with self._run_poll(state, [terminal, active], False, True) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertNotIn("recovery", rec)
        stack.dispatch.assert_not_called()

    def test_failed_transient_run_retries_via_poll(self):
        state = self._state()
        run = self._run(status="failed", error="connection reset by peer")
        run["metadata"] = json.dumps({"error": "connection reset by peer"})
        with self._run_poll(state, [run], False, True) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["failure_class"], "transient")
        self.assertEqual(rec["recovery"]["action"], "retrying")
        stack.dispatch.assert_called_once()

    def test_dispatch_budget_failure_keeps_retry_available(self):
        state = self._state()
        with contextlib.ExitStack() as stack:
            for p in self._poll(state, [self._run()], False):
                stack.enter_context(p)
            stack.enter_context(patch.object(dispatch_adapter, "DRY_RUN", False))
            stack.enter_context(patch.object(dispatch_adapter, "_DISPATCH_BUDGET", 0))
            stack.enter_context(
                patch.object(recovery_adapter, "note_capacity_deferred", return_value=None))
            stack.enter_context(
                patch.object(recovery_adapter, "comment_issue", return_value=True))
            popen = stack.enter_context(
                patch("automation.pm_harness.dispatch.subprocess.Popen"))
            log = stack.enter_context(patch.object(cycle_adapter, "log"))
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "retry_available")
        self.assertNotIn("retrying", rec)
        self.assertNotIn("automatic_retry_count", rec)
        self.assertEqual(rec["dispatch_msg"], "run")
        self.assertEqual(rec["recovery_logged_run_id"], "run-1")
        popen.assert_not_called()
        recovery_lines = [c.args[0] for c in log.call_args_list
                          if "RUN CANCELLED" in c.args[0]]
        self.assertEqual(len(recovery_lines), 1)
        self.assertIn("transient -> retry_available", recovery_lines[0])
    def test_missing_recovery_identity_is_manual_review(self):
        state = self._state()
        del state["item-92"]["wf"]
        with self._run_poll(state, [self._run()], False, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "manual_review")
        self.assertEqual(rec["dispatch_msg"], "run")
        stack.dispatch.assert_not_called()

    def test_missing_issue_number_is_manual_review(self):
        state = self._state()
        del state["item-92"]["issue_number"]
        with self._run_poll(state, [self._run()], False, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "manual_review")
        self.assertEqual(rec["dispatch_msg"], "run")
        stack.dispatch.assert_not_called()

    def test_new_dispatch_clears_recovery_episode_markers(self):
        state = self._state()
        state["item-92"].update({
            "recovery": {"action": "resume_required", "run_id": "run-1"},
            "retrying": True,
            "automatic_retry_count": 1,
            "recovery_logged_run_id": "run-1",
        })
        item = self._item()
        item["status"] = "Todo"
        with (
            patch.object(cycle_adapter, "fetch_project",
                         return_value=("p", "f", {"Ready for Review": "ready"},
                                       [item])),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "pick_workflow",
                         return_value="archon-idea-to-pr"),
            patch.object(
                cycle_adapter,
                "dispatch",
                return_value=DispatchResult(True),
            ),
            patch.object(cycle_adapter, "save_state"),
        ):
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertTrue(
            rec["dispatch_msg"].startswith("Build feature from issue #92"))
        for marker in ("recovery", "retrying", "automatic_retry_count",
                       "recovery_logged_run_id"):
            self.assertNotIn(marker, rec)

    def test_new_episode_ignores_historical_runs_after_baseline(self):
        state = self._state()
        state["item-92"]["last_observed_run_id"] = "run-2"
        todo = self._item()
        todo["status"] = "Todo"
        in_progress = self._item()
        message = ("Build feature from issue #92: Anchor curated-match prefix "
                   "(o/r). Full issue: https://github.com/o/r/issues/92")
        old = self._run(run_id="run-2")
        old["user_message"] = message
        new = self._run(run_id="run-3")
        new["started_at"] = "2026-08-07T10:10:00Z"
        new["user_message"] = message
        with (
            patch.object(cycle_adapter, "fetch_project",
                         side_effect=[
                             ("p", "f", {"Ready for Review": "ready"}, [todo]),
                             ("p", "f", {"Ready for Review": "ready"}, [in_progress]),
                             ("p", "f", {"Ready for Review": "ready"}, [in_progress]),
                         ]),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "pick_workflow",
                         return_value="archon-idea-to-pr"),
            patch.object(
                cycle_adapter,
                "dispatch",
                return_value=DispatchResult(True),
            ) as dispatch_mock,
            patch.object(
                recovery_adapter,
                "dispatch",
                return_value=DispatchResult(True),
            ) as retry_dispatch,
            patch.object(cycle_adapter, "fetch_workflow_run", return_value=None),
            patch.object(
                cycle_adapter,
                "fetch_workflow_runs",
                side_effect=[
                    self._as_runs([old]),
                    self._as_runs([old, new]),
                ],
            ),
            patch.object(recovery_adapter, "inspect_worktree", return_value=False),
            patch.object(recovery_adapter, "comment_issue", return_value=True),
            patch.object(cycle_adapter, "save_state"),
        ):
            cycle_adapter.poll(self._cfg(), {}, state)
            cycle_adapter.poll(self._cfg(), {}, state)
            rec = state["item-92"]
            self.assertNotIn("recovery", rec)
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["run_id"], "run-3")
        self.assertEqual(rec["recovery"]["action"], "retrying")
        self.assertEqual(rec["automatic_retry_count"], 1)
        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertEqual(retry_dispatch.call_count, 1)

    def test_non_transient_failure_is_manual_review_without_dispatch(self):
        state = self._state()
        with self._run_poll(state, [self._run(status="failed",
                                              error="unit tests failed")],
                            False, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "manual_review")
        self.assertEqual(rec["recovery"]["failure_class"], "validation")
        self.assertEqual(rec["dispatch_msg"], "run")
        stack.dispatch.assert_not_called()

    def test_unknown_worktree_fails_closed(self):
        state = self._state()
        with self._run_poll(state, [self._run()], None, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["recovery"]["action"], "manual_review")
        self.assertEqual(rec["recovery"]["worktree"]["dirty"], None)
        self.assertEqual(rec["dispatch_msg"], "run")
        stack.dispatch.assert_not_called()

    def test_unavailable_run_lookup_retains_marker_and_does_not_recover(self):
        state = self._state()
        unavailable = WorkflowRuns(error="archon_timeout")
        with self._run_poll(state, unavailable, False, False) as stack:
            log = stack.enter_context(patch.object(cycle_adapter, "log"))
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["dispatch_msg"], "run")
        self.assertNotIn("recovery", rec)
        self.assertNotIn("last_observed_run_id", rec)
        stack.dispatch.assert_not_called()
        stack.inspect_worktree.assert_not_called()
        self.assertTrue(any(
            "RUN LOOKUP UNAVAILABLE: archon_timeout" in c.args[0]
            for c in log.call_args_list))

    def test_unavailable_lookup_holds_ship_conflict_update(self):
        cfg = self._cfg()
        cfg["dispatch"]["review"]["conflict_fix_workflow"] = "archon-fix-ship-conflicts"
        state = self._state()
        state["item-92"].update({"status": "In Review", "review_msg": "review"})
        item = self._item()
        item["status"] = "In Review"
        unavailable = WorkflowRunStatusMap(error="archon_timeout")
        with (
            patch.object(cycle_adapter, "fetch_project",
                         return_value=("p", "f", {"Ready for Review": "ready"}, [item])),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "find_ship_pr",
                         return_value={"number": 153, "mergeable": "CONFLICTING",
                                       "headRefName": "issue-92"}),
            patch.object(cycle_adapter, "fetch_runs_by_message",
                         return_value=unavailable),
            patch.object(cycle_adapter, "try_merge_base_into_head") as merge,
            patch.object(cycle_adapter, "dispatch") as dispatch_mock,
            patch.object(cycle_adapter, "save_state"),
        ):
            cycle_adapter.poll(cfg, {}, state)
        merge.assert_not_called()
        dispatch_mock.assert_not_called()

    def test_missing_run_details_fail_closed(self):
        state = self._state()
        with self._run_poll(state, [], False, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["dispatch_msg"], "run")
        self.assertNotIn("recovery", rec)
        self.assertNotIn("recovery_logged_run_id", rec)
        stack.dispatch.assert_not_called()

    def test_run_without_id_defers_recovery(self):
        state = self._state()
        run = self._run()
        run.pop("id")
        with self._run_poll(state, [run], False, False) as stack:
            cycle_adapter.poll(self._cfg(), {}, state)
        rec = state["item-92"]
        self.assertEqual(rec["dispatch_msg"], "run")
        self.assertNotIn("recovery", rec)
        stack.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
