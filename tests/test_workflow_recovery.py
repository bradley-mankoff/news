from __future__ import annotations

import unittest
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
            patch.object(
                workflow_recovery, "fetch_workflow_run", return_value=None
            ),
            patch.object(workflow_recovery, "fetch_workflow_runs", return_value=[run]),
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
        resolved_run = {
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
        snapshot_runs = [
            {
                "id": "run-141-b",
                "workflow_name": "archon-idea-to-pr",
                "status": "failed",
                "started_at": "2026-08-06 23:00:00",
                "working_path": "/tmp/issue-141",
                "user_message": "Retry build feature from issue #141",
                "metadata": {"error": "transient poller failure"},
            },
            resolved_run,
        ]
        with (
            patch.object(
                workflow_recovery,
                "_load_state",
                return_value=(
                    None,
                    {"item-141": {"issue_number": 141, "run_id": "run-141"}},
                ),
            ),
            patch.object(
                workflow_recovery, "fetch_workflow_run", return_value=resolved_run
            ) as direct_lookup,
            patch.object(
                workflow_recovery,
                "fetch_workflow_runs",
                return_value=snapshot_runs,
            ) as list_fetch,
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
        direct_lookup.assert_called_once_with({}, "run-141")
        self.assertEqual(payload["run"]["run_id"], "run-141")
        self.assertEqual(payload["attempt_count"], 2)
        list_fetch.assert_called_once()

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
