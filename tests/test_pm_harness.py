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

import automation.apply_workflow_edits as workflow_edits
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


class DispatchGuardRetryTest(unittest.TestCase):
    def test_todo_retries_after_transient_lookup_deferral(self) -> None:
        cfg = config()
        board_item = issue(7, "Todo")
        state = {
            "_meta": {"snapshot_done": True},
            "item-7": {
                "issue_number": 7,
                "status": "Todo",
                "dispatch_guard_deferred": True,
            },
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
            patch.object(
                cycle,
                "fresh_issue_dispatch_guard",
                side_effect=[
                    (False, "Archon run lookup unavailable: archon_timeout"),
                    (True, ""),
                ],
            ) as guard,
            patch.object(
                cycle,
                "dispatch",
                return_value=DispatchResult(True, pid=123),
            ) as dispatch,
            patch.object(cycle, "move_to_lane", return_value=True),
            patch.object(cycle, "save_state"),
        ):
            cycle.poll(cfg, {}, state)
            self.assertTrue(state["item-7"]["dispatch_guard_deferred"])
            cycle.poll(cfg, {}, state)

        self.assertEqual(guard.call_count, 2)
        dispatch.assert_called_once()
        self.assertNotIn("dispatch_guard_deferred", state["item-7"])


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


class ShipReviewFlowTest(unittest.TestCase):
    def _context(self, state: dict) -> cycle.PollContext:
        cfg = config()
        cfg["dispatch"]["review"]["merge_ship_on_approve"] = True
        return cycle.PollContext(
            cfg=cfg,
            env={},
            state=state,
            project_id="project",
            field_id="field",
            status_options={"Done": "done-option"},
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

    def test_approved_ship_pr_merges_closes_and_moves_done(self) -> None:
        state = {
            "item-7": {
                "status": "In Review",
                "issue_number": 7,
                "ship_pr": 71,
                "review_msg": "Review PR #71",
            }
        }
        ctx = self._context(state)

        def fake_gh(args: list[str], _env: dict) -> subprocess.CompletedProcess:
            if args[:2] == ["pr", "view"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"number": 71, "state": "OPEN"}), ""
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch.object(cycle, "gh", side_effect=fake_gh) as gh_call,
            patch.object(cycle, "fetch_verdict", return_value=("approve", True)),
            patch.object(cycle, "move_to_lane", return_value=True) as move,
            patch.object(cycle, "DRY_RUN", False),
        ):
            cycle._complete_reviews(ctx, None, None)

        commands = [call_args.args[0] for call_args in gh_call.call_args_list]
        self.assertIn(
            ["pr", "merge", "71", "-R", "example/widgets", "--merge"],
            commands,
        )
        self.assertIn(
            ["issue", "close", "7", "-R", "example/widgets"],
            commands,
        )
        move.assert_called_once_with(
            ctx.cfg, {}, "project", "item-7", "field", "done-option"
        )
        self.assertNotIn("ship_pr", state["item-7"])

    def test_non_approve_verdict_never_merges(self) -> None:
        state = {
            "item-8": {
                "status": "In Review",
                "issue_number": 8,
                "ship_pr": 81,
                "review_msg": "Review PR #81",
            }
        }
        ctx = self._context(state)

        def fake_gh(args: list[str], _env: dict) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"number": 81, "state": "OPEN"})
                if args[:2] == ["pr", "view"] else "",
                "",
            )

        with (
            patch.object(cycle, "gh", side_effect=fake_gh) as gh_call,
            patch.object(cycle, "fetch_verdict", return_value=("request-changes", True)),
            patch.object(cycle, "comment_issue", return_value=True),
            patch.object(cycle, "DRY_RUN", False),
        ):
            cycle._complete_reviews(ctx, None, None)

        commands = [call_args.args[0] for call_args in gh_call.call_args_list]
        self.assertNotIn(
            ["pr", "merge", "81", "-R", "example/widgets", "--merge"],
            commands,
        )
        self.assertTrue(state["item-8"]["review_held"])

    def test_review_entry_creates_and_dispatches_ship_review(self) -> None:
        cfg = config()
        rec: dict = {}
        with (
            patch.object(
                cycle,
                "find_issue_pr",
                return_value=(
                    {"number": 17, "headRefName": "issue-17", "state": "OPEN"},
                    True,
                ),
            ),
            patch.object(cycle, "merge_pr_to_base", return_value=(True, "merged")),
            patch.object(cycle, "branch_empty_vs_main", return_value=False),
            patch.object(
                cycle,
                "find_or_create_ship_pr",
                return_value={"number": 171, "headRefName": "issue-17"},
            ),
            patch.object(cycle, "dispatch", return_value=True) as dispatch,
        ):
            result = cycle.ensure_ship_review(
                cfg, {}, "item-17", 17, "Issue 17", "project", "field",
                {"Done": "done-option"}, "Done", rec,
            )

        self.assertEqual(result, ("ok", "Review PR #171 (ship to main for issue #17: Issue 17).", 171))
        dispatch.assert_called_once()


class GithubAdapterTest(unittest.TestCase):
    @staticmethod
    def _result(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, returncode, stdout, "error" if returncode else "")

    def test_existing_ship_pr_is_reused_and_verdict_shape_is_validated(self) -> None:
        responses = iter(
            [
                self._result([], json.dumps([{"number": 71, "baseRefName": "main"}])),
                self._result([], json.dumps({"comments": [{"body": "VERDICT: approve"}]})),
            ]
        )
        with patch.object(github, "gh", side_effect=lambda *args: next(responses)) as gh_call:
            ship = github.find_or_create_ship_pr(
                config(), {}, "issue-7", "Ship: x", 7, "main"
            )
            verdict = github.fetch_verdict(config(), {}, 71)

        self.assertEqual(ship["number"], 71)
        self.assertEqual(verdict, ("approve", True))
        self.assertEqual(gh_call.call_count, 2)
        self.assertEqual(gh_call.call_args_list[0].args[0][:4], [
            "pr", "list", "-R", "example/widgets"
        ])

    def test_malformed_ship_list_fails_closed_without_creating_duplicate(self) -> None:
        with patch.object(
            github,
            "gh",
            return_value=self._result([], "not-json"),
        ) as gh_call:
            self.assertIsNone(
                github.find_or_create_ship_pr(
                    config(), {}, "issue-7", "Ship: x", 7, "main"
                )
            )
        gh_call.assert_called_once()

    def test_malformed_verdict_and_label_payloads_fail_closed(self) -> None:
        responses = iter([
            self._result([], json.dumps({"comments": {}})),
            self._result([], "not-json"),
        ])
        with patch.object(github, "gh", side_effect=lambda *args: next(responses)):
            self.assertEqual(github.fetch_verdict(config(), {}, 71), (None, False))
            self.assertFalse(github.label_exists(config(), {}, "needs-input"))


class WorkflowEditCoverageTest(unittest.TestCase):
    def test_new_workflow_edit_helpers_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workflow.yaml"
            path.write_text(
                "- id: check\n"
                "- id: create-pr\n"
                "    depends_on: [check]\n",
                encoding="utf-8",
            )
            node = "  - id: sync-with-develop\n    context: fresh"
            self.assertIsNotNone(
                workflow_edits.ensure_sync_node(
                    path, "- id: check", node, "create-pr",
                    "    depends_on: [check]", "    depends_on: [sync-with-develop]",
                )
            )
            first = path.read_text(encoding="utf-8")
            self.assertIsNone(
                workflow_edits.ensure_sync_node(
                    path, "- id: check", node, "create-pr",
                    "    depends_on: [check]", "    depends_on: [sync-with-develop]",
                )
            )
            self.assertEqual(first, path.read_text(encoding="utf-8"))

            review = Path(tmpdir) / "review.yaml"
            sync_block = (
                "  - id: sync\n"
                "    command: archon-sync-pr-with-main\n"
                "    depends_on: [review-scope]\n"
                "    context: fresh\n"
            )
            review.write_text(
                sync_block
                + "\n  - id: synthesize\n"
                + "    depends_on: [code-review, error-handling, test-coverage, comment-quality, docs-impact]\n",
                encoding="utf-8",
            )
            self.assertIsNotNone(workflow_edits.ensure_spec_review(review))
            self.assertIsNone(workflow_edits.ensure_spec_review(review))
            changed = workflow_edits.ensure_rigorous_models(review, ("spec-review",))
            self.assertIsNotNone(changed)
            text = review.read_text(encoding="utf-8")
            self.assertIn("provider: pi", text)
            self.assertIn("model: openai-codex/gpt-5.6-luna", text)
            self.assertIn("spec-review", text)


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

    def test_health_reports_false_ready_and_conflicting_review(self) -> None:
        cfg = config()
        cfg["state_file"] = "state.json"
        cfg["dispatch"]["review"]["conflict_fix_workflow"] = "fix-conflicts"
        items = [issue(21, "Ready for Review"), issue(22, "In Review")]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "state.json").write_text(json.dumps({
                "22": {"issue_number": 22, "status": "In Review", "review_msg": "review"},
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
                    return_value=("project", "field", {}, items),
                ),
                patch.object(board_health, "fetch_workflow_runs", return_value=[]),
                patch.object(board_health, "find_issue_pr", return_value=(None, True)),
                patch.object(
                    board_health,
                    "find_ship_pr",
                    return_value={"number": 220, "mergeable": "CONFLICTING"},
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(board_health.main(), 0)

        text = output.getvalue()
        self.assertIn("false Ready", text)
        self.assertIn("ship PR #220 conflicting", text)




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
