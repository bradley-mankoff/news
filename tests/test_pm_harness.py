from __future__ import annotations

import io
import json
import signal
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

import automation.pm_harness.cycle as cycle
import automation.board_health as board_health
import automation.pm_harness.engine as engine
import automation.pm_harness.github as github
import automation.pm_harness.recovery as recovery
from automation.pm_harness.model import DispatchResult
from automation.pm_harness.runtime import (
    hydrate_state_for_items,
    load_config,
    persisted_state,
)


def config() -> dict:
    return {
        "repo": "example/widgets",
        "default_lane": "Backlog",
        "max_concurrent_workflows": 2,
        "runnable_label": "runnable",
        "decision_only": {
            "label": "decision-only",
            "move_to": "Needs Input",
        },
        "deferred_work": {"enabled": False, "fallback_warn": True},
        "lanes": {
            "Backlog": "backlog",
            "Todo": "todo",
            "In Progress": "in_progress",
            "Blocked": "blocked",
            "Needs Input": "needs_input",
            "Ready for Review": "ready",
            "In Review": "review",
            "Done": "done",
        },
        "dispatch": {
            "todo": {
                "default": "implement-issue",
                "move_to": "In Progress",
                "complete_move_to": "Ready for Review",
                "merge_develop_base": "develop",
                "label_overrides": {},
            },
            "review": {
                "workflow": "review-pr",
                "ship_to": "main",
                "merge_ship_on_approve": False,
                "done_lane": "Done",
            },
        },
    }


def issue(number: int, status: str, *, labels: list[str] | None = None) -> dict:
    return {
        "id": f"item-{number}",
        "status": status,
        "content": {
            "__typename": "Issue",
            "number": number,
            "title": f"Issue {number}",
            "url": f"https://github.com/example/widgets/issues/{number}",
            "body": "## Depends on\nNone.",
            "state": "OPEN",
            "repository": {"nameWithOwner": "example/widgets"},
            "labels": {"nodes": [{"name": name} for name in (labels or [])]},
        },
    }


class ConfigTest(unittest.TestCase):
    def test_example_config_defines_the_portable_interface(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "automation"
            / "pm_harness"
            / "example_config.json"
        )
        cfg = load_config(path)

        self.assertEqual(cfg["repo"], "OWNER/REPOSITORY")
        self.assertEqual(cfg["lanes"]["Todo"], "todo")

    def test_load_config_rejects_an_incomplete_interface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"repo": "example/widgets"}))

            with self.assertRaisesRegex(ValueError, "missing project_owner"):
                load_config(path)


class StateSchemaTest(unittest.TestCase):
    def test_state_round_trips_by_issue_number_across_board_item_ids(self) -> None:
        state = {
            "_meta": {"snapshot_done": True},
            "92": {"issue_number": 92, "status": "In Progress", "run_id": "r-92"},
            "old-project-item": {"status": "In Progress"},
        }
        hydrate_state_for_items(state, [issue(92, "In Progress")])
        self.assertNotIn("92", state)
        self.assertEqual(state["item-92"]["run_id"], "r-92")

        stored = persisted_state(state)
        self.assertNotIn("item-92", stored)
        self.assertNotIn("old-project-item", stored)
        self.assertEqual(stored["92"]["run_id"], "r-92")
        self.assertEqual(stored["_meta"]["schema_version"], 2)


class SyntheticProjectTest(unittest.TestCase):
    def test_config_driven_todo_transition_dispatches_without_news_assumptions(self) -> None:
        cfg = config()
        board_item = issue(7, "Todo")
        state = {
            "_meta": {"snapshot_done": True},
            "item-7": {"issue_number": 7, "status": "Backlog"},
        }
        with (
            patch.object(
                cycle,
                "fetch_project",
                return_value=(
                    "project",
                    "field",
                    {"In Progress": "in-progress-option"},
                    [board_item],
                ),
            ),
            patch.object(cycle, "prepare_dispatch_budget"),
            patch.object(cycle, "reconcile_untracked_runs"),
            patch.object(cycle, "sync_runnable_labels"),
            patch.object(cycle, "fresh_issue_dispatch_guard", return_value=(True, "")),
            patch.object(
                cycle,
                "dispatch",
                return_value=DispatchResult(True, pid=123),
            ) as dispatch,
            patch.object(cycle, "move_to_lane", return_value=True) as move,
            patch.object(cycle, "save_state"),
        ):
            cycle.poll(cfg, {}, state)

        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[2], "implement-issue")
        self.assertIn("example/widgets", dispatch.call_args.args[4])
        move.assert_called_once_with(
            cfg,
            {},
            "project",
            "item-7",
            "field",
            "in-progress-option",
        )


class ShipReviewTest(unittest.TestCase):
    def _context(self, cfg, state, status_options):
        return cycle.PollContext(
            cfg=cfg,
            env={},
            state=state,
            project_id="project",
            field_id="field",
            status_options=status_options,
            items=[],
            first_run=False,
            done_lane_name="Done",
            todo_lane_name="Todo",
            blocked_lane_name="Blocked",
            ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={},
            number_state={},
            seen=set(),
            fresh_dispatched=set(),
        )

    def test_approved_ship_pr_merges_closes_and_moves_to_done(self):
        cfg = config()
        cfg["dispatch"]["review"]["merge_ship_on_approve"] = True
        state = {
            "item-92": {
                "status": "In Review",
                "issue_number": 92,
                "ship_pr": 153,
                "review_msg": "Review PR #153",
            }
        }
        result = Mock(returncode=0, stdout='{"number":153,"state":"OPEN"}', stderr="")
        with (
            patch.object(cycle, "gh", side_effect=[
                result,
                Mock(returncode=0, stdout="", stderr=""),
                Mock(returncode=0, stdout="", stderr=""),
            ]) as gh,
            patch.object(cycle, "fetch_verdict", return_value=("approve", True)),
            patch.object(cycle, "move_to_lane", return_value=True) as move,
        ):
            cycle._complete_reviews(
                self._context(cfg, state, {"Done": "done-option"}), None, None
            )

        self.assertNotIn("ship_pr", state["item-92"])
        self.assertNotIn("review_msg", state["item-92"])
        move.assert_called_once_with(
            cfg, {}, "project", "item-92", "field", "done-option"
        )
        self.assertEqual(gh.call_args_list[1].args[0][:3], ["pr", "merge", "153"])
        self.assertEqual(gh.call_args_list[2].args[0][:3], ["issue", "close", "92"])

    def test_ship_review_defers_when_develop_pr_lookup_fails(self):
        cfg = config()
        with (
            patch.object(cycle, "find_issue_pr", return_value=(None, False)),
            patch.object(cycle, "branch_empty_vs_main") as empty,
        ):
            result = cycle.ensure_ship_review(
                cfg, {}, "item-92", 92, "Issue 92", "project", "field", {},
                "Done", {},
            )
        self.assertIsNone(result)
        empty.assert_not_called()

    def test_ship_review_defers_when_develop_merge_fails(self):
        cfg = config()
        pr = {"number": 153, "state": "OPEN", "headRefName": "issue-92"}
        with (
            patch.object(cycle, "find_issue_pr", return_value=(pr, True)),
            patch.object(cycle, "merge_pr_to_base", return_value=(False, "permission denied")),
            patch.object(cycle, "branch_empty_vs_main") as empty,
        ):
            result = cycle.ensure_ship_review(
                cfg, {}, "item-92", 92, "Issue 92", "project", "field", {},
                "Done", {},
            )
        self.assertIsNone(result)
        empty.assert_not_called()

    def test_failed_develop_merge_retains_retry_marker(self):
        cfg = config()
        run = {
            "id": "run-92",
            "status": "completed",
            "user_message": "Implement issue #92",
            "started_at": "2026-08-08T10:00:00Z",
        }
        state = {
            "item-92": {
                "status": "In Progress",
                "issue_number": 92,
                "dispatch_msg": "Implement issue #92",
                "run_id": "run-92",
            }
        }
        ctx = self._context(cfg, state, {"Ready for Review": "ready-option"})
        with (
            patch.object(cycle, "fetch_workflow_run", return_value=run),
            patch.object(cycle, "fetch_workflow_runs", return_value=cycle.WorkflowRuns([run])),
            patch.object(cycle, "issue_has_label", return_value=False),
            patch.object(cycle, "find_issue_pr", return_value=({"number": 153}, True)),
            patch.object(cycle, "merge_pr_to_base", return_value=(False, "temporary GitHub error")),
            patch.object(github, "comment_issue", return_value=True),
            patch.object(cycle, "move_to_lane") as move,
        ):
            cycle._reconcile_completions(ctx)
        rec = state["item-92"]
        self.assertEqual(rec["dispatch_msg"], "Implement issue #92")
        self.assertTrue(rec["awaiting_integration"])
        move.assert_not_called()


class DecisionOnlyTest(unittest.TestCase):
    def test_todo_decision_moves_to_input_without_implementation_dispatch(self) -> None:
        cfg = config()
        board_item = issue(8, "Todo", labels=["decision-only"])
        state = {
            "_meta": {"snapshot_done": True},
            "item-8": {"issue_number": 8, "status": "Backlog"},
        }
        with (
            patch.object(
                cycle,
                "fetch_project",
                return_value=(
                    "project",
                    "field",
                    {"Needs Input": "needs-input-option"},
                    [board_item],
                ),
            ),
            patch.object(cycle, "prepare_dispatch_budget"),
            patch.object(cycle, "reconcile_untracked_runs"),
            patch.object(cycle, "sync_runnable_labels"),
            patch.object(cycle, "comment_issue", return_value=True),
            patch.object(cycle, "move_to_lane", return_value=True) as move,
            patch.object(cycle, "dispatch") as dispatch,
            patch.object(cycle, "save_state"),
        ):
            cycle.poll(cfg, {}, state)

        dispatch.assert_not_called()
        move.assert_called_once_with(
            cfg,
            {},
            "project",
            "item-8",
            "field",
            "needs-input-option",
        )
        self.assertEqual(state["item-8"]["status"], "Needs Input")


class RecoveryTest(unittest.TestCase):
    def test_cancelled_clean_run_retries_once_and_logs_once_per_run(self) -> None:
        rec = {
            "wf": "implement-issue",
            "branch": "issue-9",
            "dispatch_msg": "Implement issue #9",
        }
        run = {
            "id": "cancelled-9",
            "status": "cancelled",
            "workflow_name": "implement-issue",
            "user_message": "Implement issue #9",
        }
        with patch.object(
            recovery,
            "inspect_worktree",
            return_value={"path": "/tmp/9", "exists": True, "dirty": False},
        ):
            details, worktree, action = recovery.update_recovery_state(
                rec, run, branch="issue-9")
        self.assertEqual(action, "retry_available")

        with (
            patch.object(
                recovery,
                "dispatch",
                return_value=DispatchResult(True, pid=9),
            ),
            patch.object(recovery, "comment_issue", return_value=True) as comment,
        ):
            self.assertTrue(
                recovery.auto_retry_transient_failure(
                    config(), {}, "item-9", 9, rec, details, worktree, [run]))
            self.assertTrue(
                recovery.notify_workflow_recovery(
                    config(), {}, 9, rec, details, worktree, "retrying"))

        self.assertEqual(rec["automatic_retry_count"], 1)
        self.assertEqual(rec["recovery"]["action"], "retrying")
        comment.assert_called_once()


class CapacityVisibilityTest(unittest.TestCase):
    def test_capacity_hold_is_commented_once_per_episode(self) -> None:
        rec: dict = {}
        with patch.object(github, "comment_issue", return_value=True) as comment:
            github.note_capacity_deferred(config(), {}, 10, rec)
            github.note_capacity_deferred(config(), {}, 10, rec)
        comment.assert_called_once()
        self.assertIn("capacity_deferred", rec)


class ConflictResolutionTest(unittest.TestCase):
    def test_resolved_conflict_without_verdict_requeues_review(self) -> None:
        cfg = config()
        cfg["dispatch"]["review"]["conflict_fix_workflow"] = "fix-conflicts"
        state = {
            "item-11": {
                "status": "In Review",
                "issue_number": 11,
                "review_held": True,
                "review_held_notice": "none",
                "conflict_fix_msg": "fix",
                "conflict_mech_failed": True,
                "conflict_fix_noted": True,
            }
        }
        ctx = cycle.PollContext(
            cfg=cfg,
            env={},
            state=state,
            project_id="project",
            field_id="field",
            status_options={},
            items=[],
            first_run=False,
            done_lane_name="Done",
            todo_lane_name="Todo",
            blocked_lane_name="Blocked",
            ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={},
            number_state={},
            seen=set(),
            fresh_dispatched=set(),
        )
        with (
            patch.object(
                cycle,
                "find_ship_pr",
                return_value={
                    "number": 111,
                    "mergeable": "MERGEABLE",
                    "headRefName": "issue-11",
                },
            ),
            patch.object(cycle, "fetch_verdict", return_value=(None, True)),
        ):
            cycle._remediate_ship_conflicts(ctx, {}, "In Review", "main")

        rec = state["item-11"]
        self.assertNotIn("review_held", rec)
        self.assertNotIn("review_held_notice", rec)
        self.assertNotIn("conflict_fix_msg", rec)
        self.assertEqual(rec["ship_pr"], 111)


class BoardHealthTest(unittest.TestCase):
    def test_issue_keyed_capacity_hold_is_visible(self) -> None:
        cfg = config()
        cfg["state_file"] = "state.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "state.json").write_text(json.dumps({
                "_meta": {"schema_version": 2},
                "10": {
                    "issue_number": 10,
                    "status": "Todo",
                    "capacity_deferred": "2026-08-08T10:00:00+00:00",
                },
            }))
            output = io.StringIO()
            with (
                patch.object(board_health, "ROOT", root),
                patch.object(board_health, "load_config", return_value=cfg),
                patch.object(
                    board_health,
                    "gh",
                    return_value=subprocess.CompletedProcess([], 1, "", ""),
                ),
                patch.object(
                    board_health,
                    "fetch_project",
                    return_value=("project", "field", {}, [issue(10, "Todo")]),
                ),
                patch.object(board_health, "fetch_workflow_runs", return_value=[]),
                redirect_stdout(output),
            ):
                self.assertEqual(board_health.main(), 0)

        self.assertIn("workflow capacity is full", output.getvalue())




class PollSupervisorTest(unittest.TestCase):
    def test_timeout_terminates_isolated_poll_process_group(self) -> None:
        process = Mock(pid=4242, returncode=None)
        process.wait.side_effect = [
            subprocess.TimeoutExpired("poll", 1),
            None,
        ]
        with (
            patch.object(engine.subprocess, "Popen", return_value=process),
            patch.object(engine.os, "killpg") as killpg,
            patch.object(engine, "log") as log,
        ):
            result = engine.run_poll_process({"poll_timeout_seconds": 1})

        self.assertEqual(result, 124)
        killpg.assert_called_once_with(4242, signal.SIGTERM)
        self.assertIn("POLL TIMEOUT", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
