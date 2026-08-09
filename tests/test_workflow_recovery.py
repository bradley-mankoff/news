from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from automation import workflow_recovery


class WorkflowRecoveryCliTests(unittest.TestCase):
    def test_status_marks_dirty_failed_worktree_as_resume_required(self) -> None:
        run = {
            "id": "run-141",
            "workflow_name": "archon-idea-to-pr",
            "status": "failed",
            "started_at": "2026-08-06 22:00:00",
            "working_path": "/tmp/issue-141",
            "user_message": "Build feature from issue #141",
            "metadata": {
                "error": (
                    "DAG workflow completed with failures: 'validate': "
                    "SDK returned error — Stream ended without finish_reason"
                )
            },
        }
        with (
            patch.object(workflow_recovery, "_load_state", return_value=(None, {})),
            patch.object(workflow_recovery, "fetch_workflow_runs", return_value=[run]),
            patch.object(workflow_recovery, "fetch_workflow_run", return_value=None),
            patch.object(
                workflow_recovery,
                "resolve_worktree_info",
                return_value={"branch": "archon/task-issue-141", "path": "/tmp/issue-141"},
            ),
            patch.object(
                workflow_recovery,
                "inspect_worktree",
                return_value={"path": "/tmp/issue-141", "exists": True, "dirty": True},
            ),
        ):
            payload = workflow_recovery._status_payload({}, {}, 141)
        self.assertEqual(payload["recovery"], "resume_required")
        self.assertEqual(payload["run"]["failed_step"], "validate")
        self.assertTrue(payload["worktree"]["dirty"])
        self.assertEqual(payload["branch"], "archon/task-issue-141")

    def test_status_counts_runs_when_persisted_run_lookup_succeeds(self) -> None:
        run = {
            "id": "run-141",
            "workflow_name": "archon-idea-to-pr",
            "status": "failed",
            "started_at": "2026-08-06 22:00:00",
            "working_path": "/tmp/issue-141",
            "user_message": "Build feature from issue #141",
        }
        state = {"item-141": {"issue_number": 141, "run_id": "run-141"}}
        with (
            patch.object(workflow_recovery, "_load_state", return_value=(None, state)),
            patch.object(workflow_recovery, "fetch_workflow_runs", return_value=[run]),
            patch.object(workflow_recovery, "fetch_workflow_run", return_value=run),
            patch.object(workflow_recovery, "resolve_worktree_info", return_value=None),
            patch.object(
                workflow_recovery,
                "inspect_worktree",
                return_value={"path": "", "exists": False, "dirty": None},
            ),
        ):
            payload = workflow_recovery._status_payload({}, {}, 141)
        self.assertEqual(payload["run"]["run_id"], "run-141")
        self.assertEqual(payload["attempt_count"], 1)

    def test_resume_message_includes_authoritative_issue_context(self) -> None:
        result = CompletedProcess(
            ["gh"], 0,
            stdout=(
                '{"title":"Add delivery","url":"https://github.com/o/r/issues/141",'
                '"body":"## Acceptance criteria\\n- [ ] Local report"}'
            ),
            stderr="",
        )
        with patch.object(workflow_recovery, "gh", return_value=result) as gh:
            message = workflow_recovery._resume_message(
                {"repo": "o/r"}, {}, 141, {"run": {}},
            )
        self.assertIn("Issue: Add delivery", message)
        self.assertIn("Full issue: https://github.com/o/r/issues/141", message)
        self.assertIn("## Acceptance criteria", message)
        gh.assert_called_once()


    def test_resume_persists_marker_and_comments_issue(self) -> None:
        payload = {
            "run": {"status": "failed"},
            "workflow": "archon-idea-to-pr",
            "branch": "archon/task-issue-141",
            "worktree": {"path": "/tmp/issue-141", "exists": True, "dirty": False},
        }
        state = {"item-141": {"issue_number": 141, "run_id": "old"}}
        with (
            patch.object(workflow_recovery, "_status_payload", return_value=payload),
            patch.object(workflow_recovery, "_resume_message", return_value="resume"),
            patch.object(
                workflow_recovery,
                "resume_existing_worktree",
                return_value=(True, "started", 77),
            ),
            patch.object(
                workflow_recovery,
                "_load_state",
                return_value=(Path("state.json"), state),
            ),
            patch.object(workflow_recovery, "_save_state") as save,
            patch.object(workflow_recovery, "gh") as gh,
        ):
            self.assertEqual(
                workflow_recovery._resume({"repo": "o/r"}, {}, 141), 0
            )
        self.assertEqual(state["item-141"]["recovery"]["action"], "resume_requested")
        self.assertNotIn("run_id", state["item-141"])
        save.assert_called_once()
        gh.assert_called_once()

    def test_discard_persists_marker_and_clears_dispatch_state(self) -> None:
        payload = {
            "run": {"status": "failed"},
            "branch": "archon/task-issue-141",
            "worktree": {"path": "/tmp/issue-141", "exists": True, "dirty": False},
        }
        state = {
            "item-141": {
                "issue_number": 141,
                "run_id": "old",
                "dispatch_msg": "run",
                "branch": "archon/task-issue-141",
                "wf": "archon-idea-to-pr",
            }
        }
        with (
            patch.object(workflow_recovery, "_status_payload", return_value=payload),
            patch.object(
                workflow_recovery.subprocess,
                "run",
                return_value=CompletedProcess(["archon"], 0, "", ""),
            ) as run,
            patch.object(
                workflow_recovery,
                "_load_state",
                return_value=(Path("state.json"), state),
            ),
            patch.object(workflow_recovery, "_save_state") as save,
            patch.object(workflow_recovery, "gh") as gh,
        ):
            self.assertEqual(
                workflow_recovery._discard({"repo": "o/r"}, {}, 141, False), 0
            )
        run.assert_called_once()
        self.assertNotIn("dispatch_msg", state["item-141"])
        self.assertEqual(state["item-141"]["recovery"]["action"], "discarded")
        save.assert_called_once()
        gh.assert_called_once()

    def test_resume_refuses_an_active_run(self) -> None:
        payload = {"run": {"status": "running", "run_id": "run-141"}}
        with patch.object(workflow_recovery, "_status_payload", return_value=payload):
            self.assertEqual(workflow_recovery._resume({}, {}, 141), 2)

    def test_discard_refuses_dirty_worktree_without_force(self) -> None:
        payload = {
            "run": {"status": "failed"},
            "branch": "archon/task-issue-141",
            "worktree": {"path": "/tmp/issue-141", "exists": True, "dirty": True},
        }
        with (
            patch.object(workflow_recovery, "_status_payload", return_value=payload),
            patch.object(workflow_recovery.subprocess, "run") as run,
        ):
            self.assertEqual(workflow_recovery._discard({}, {}, 141, False), 2)
        run.assert_not_called()

    def test_mark_recovery_updates_issue_record(self) -> None:
        state = {"item-141": {"issue_number": 141}}
        workflow_recovery._mark_recovery(state, 141, {"action": "discarded"})
        self.assertEqual(state["item-141"]["recovery"]["action"], "discarded")


if __name__ == "__main__":
    unittest.main()
