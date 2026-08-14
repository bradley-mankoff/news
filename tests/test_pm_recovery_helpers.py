from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import automation.pm_harness.archon as archon
import automation.pm_harness.cycle as cycle
from automation.pm_harness.model import (
    WorkflowRuns,
    classify_workflow_failure,
    parse_run_metadata,
    recovery_action,
)


class RecoveryClassificationTest(unittest.TestCase):
    def test_cancelled_is_transient(self) -> None:
        self.assertEqual(classify_workflow_failure("cancelled", ""), "transient")
        self.assertEqual(classify_workflow_failure("Cancelled", "tests failed"), "transient")

    def test_failed_transport_errors_are_transient(self) -> None:
        for error in (
            "Stream ended without finish_reason",
            "request timed out",
            "rate limit exceeded",
            "connection reset by peer",
            "SDK returned error — WebSocket closed 1006 Connection ended",
        ):
            with self.subTest(error=error):
                self.assertEqual(classify_workflow_failure("failed", error), "transient")

    def test_failed_product_errors_are_bucketed(self) -> None:
        self.assertEqual(
            classify_workflow_failure("failed", "merge conflict in PR branch"),
            "orchestration",
        )
        self.assertEqual(
            classify_workflow_failure(
                "failed", "could not open the pull request for the branch"),
            "orchestration",
        )
        self.assertEqual(
            classify_workflow_failure("failed", "lint errors: 4 warnings"),
            "validation",
        )
        self.assertEqual(
            classify_workflow_failure("failed", "type-check failed"),
            "validation",
        )
        self.assertEqual(
            classify_workflow_failure("failed", "unit tests failed: 2 failures"),
            "validation",
        )
        self.assertEqual(
            classify_workflow_failure("failed", "something odd happened"),
            "unknown",
        )

    def test_non_failed_status_is_unknown(self) -> None:
        self.assertEqual(classify_workflow_failure("running", ""), "unknown")

    def test_recovery_action_matrix(self) -> None:
        self.assertEqual(recovery_action("cancelled", "transient", True), "resume_required")
        self.assertEqual(recovery_action("failed", "transient", False), "retry_available")
        self.assertEqual(recovery_action("cancelled", "transient", None), "manual_review")
        self.assertEqual(recovery_action("failed", "orchestration", False), "manual_review")
        self.assertEqual(recovery_action("running", "", False), "monitoring")
        self.assertEqual(recovery_action("completed", "", None), "monitoring")


class ParseRunMetadataTest(unittest.TestCase):
    def test_dict_and_json_metadata(self) -> None:
        self.assertEqual(
            parse_run_metadata({
                "metadata": {"error": "Stream ended", "failed_step": "implement"},
                "status": "failed",
            }),
            ("Stream ended", "implement"),
        )
        error, step = parse_run_metadata({
            "metadata": json.dumps({"error": "Stream ended without finish_reason"}),
            "status": "failed",
        })
        self.assertIn("Stream ended", error)
        self.assertEqual(step, "")

    def test_malformed_and_bounds(self) -> None:
        self.assertEqual(
            parse_run_metadata({"metadata": "{not json", "error": "rate limit"}),
            ("rate limit", ""),
        )
        self.assertEqual(parse_run_metadata({"status": "failed"}), ("", ""))
        error, step = parse_run_metadata({
            "metadata": {"error": "x" * 2000, "failed_step": "s" * 500},
        })
        self.assertEqual(len(error), 500)
        self.assertEqual(len(step), 200)

    def test_step_fallback_precedence(self) -> None:
        self.assertEqual(
            parse_run_metadata({
                "metadata": {"step": "metadata-step", "stage": "metadata-stage"},
                "failed_step": "top-level-step",
            })[1],
            "metadata-step",
        )
        self.assertEqual(
            parse_run_metadata({
                "metadata": {"stage": "metadata-stage"},
                "failed_step": "top-level-step",
            })[1],
            "metadata-stage",
        )
        self.assertEqual(
            parse_run_metadata({"metadata": {}, "failed_step": "top-level-step"})[1],
            "top-level-step",
        )


class LatestWorkflowRunTest(unittest.TestCase):
    def _run(self, run_id: str, started: str, msg: str, status: str = "failed") -> dict:
        return {
            "id": run_id,
            "started_at": started,
            "user_message": msg,
            "status": status,
        }

    def test_newest_and_issue_filters(self) -> None:
        runs = [
            self._run("r1", "2026-08-07T10:00:00Z", "Build feature from issue #7"),
            self._run("r2", "2026-08-07T10:05:00Z", "Prior Context Build feature from issue #7"),
            self._run("r3", "2026-08-07T10:06:00Z", "Build feature from issue #8"),
        ]
        self.assertEqual(archon.latest_workflow_run(runs, message="issue #7")["id"], "r2")
        self.assertEqual(archon.latest_workflow_run(runs, issue_number=7)["id"], "r2")
        self.assertEqual(archon.latest_workflow_run(runs, issue_number=8)["id"], "r3")

    def test_malformed_match_defers(self) -> None:
        older = self._run("r1", "2026-08-07T10:00:00Z", "m")
        malformed = self._run("r2", "not-a-timestamp", "m")
        self.assertIsNone(archon.latest_workflow_run([older, malformed], message="m"))
        self.assertIsNone(
            archon.latest_workflow_run(
                [self._run({"unexpected": True}, "2026-08-07T10:01:00Z", "m")],
                message="m",
            )
        )
    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(
            archon.latest_workflow_run(
                [self._run("r1", "2026-08-07T10:00:00Z", "other")],
                message="m",
            )
        )
        self.assertIsNone(archon.latest_workflow_run([], message="m"))


class InspectWorktreeTest(unittest.TestCase):
    def test_missing_and_file_paths_are_unknown(self) -> None:
        with patch.object(archon, "_run_git", side_effect=AssertionError("must not probe")):
            missing = archon.inspect_worktree("/no/such/dir-177")
        self.assertFalse(missing["exists"])
        self.assertIsNone(missing["dirty"])
        with tempfile.NamedTemporaryFile() as handle:
            file_path = archon.inspect_worktree(handle.name)
        self.assertFalse(file_path["exists"])
        self.assertIsNone(file_path["dirty"])

    def test_clean_and_dirty_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                archon,
                "_run_git",
                side_effect=[
                    subprocess.CompletedProcess(["git"], 0, "", ""),
                    subprocess.CompletedProcess(["git"], 0, "abc123\n", ""),
                ],
            ):
                clean = archon.inspect_worktree(tmp)
            self.assertTrue(clean["exists"])
            self.assertFalse(clean["dirty"])
            with patch.object(
                archon,
                "_run_git",
                return_value=subprocess.CompletedProcess(
                    ["git"], 0, " M automation/board_poller.py\n", ""),
            ):
                dirty = archon.inspect_worktree(tmp)
            self.assertTrue(dirty["dirty"])


class FetchWorkflowRunsTest(unittest.TestCase):
    def _fake_run(self, payload: str = "", returncode: int = 0):
        def fake(cmd, **kwargs):
            stdout = kwargs.get("stdout")
            if hasattr(stdout, "write"):
                stdout.write(payload)
                stdout.flush()
            return subprocess.CompletedProcess(cmd, returncode, "", "")

        return fake

    def test_tolerates_command_failure_and_timeout(self) -> None:
        with patch(
            "automation.pm_harness.archon.subprocess.run",
            side_effect=self._fake_run(returncode=1),
        ):
            failed = archon.fetch_workflow_runs({})
        self.assertEqual(failed, [])
        self.assertEqual(failed.error, "archon_command_failed")
        with patch(
            "automation.pm_harness.archon.subprocess.run",
            side_effect=subprocess.TimeoutExpired("archon", 60),
        ):
            timed_out = archon.fetch_workflow_runs({})
        self.assertEqual(timed_out.error, "archon_timeout")

    def test_rejects_malformed_and_incomplete_payloads(self) -> None:
        cases = [
            ("{not json", "archon_json", 0),
            (json.dumps({"runs": "not-a-list"}), "archon_runs_shape", 0),
            (
                json.dumps({"total": 151, "runs": [{"id": "r1", "user_message": "m", "started_at": "2026-08-07T10:00:00Z"}]}),
                "run_list_incomplete",
                1,
            ),
            (json.dumps({"total": "151", "runs": []}), "archon_total_shape", 0),
        ]
        for payload, error, expected_len in cases:
            with self.subTest(error=error):
                with patch(
                    "automation.pm_harness.archon.subprocess.run",
                    side_effect=self._fake_run(payload),
                ):
                    records = archon.fetch_workflow_runs({})
                self.assertEqual(len(records), expected_len)
                self.assertEqual(records.error, error)

    def test_file_capture_returns_complete_large_payload(self) -> None:
        payload = json.dumps({
            "total": 2,
            "runs": [
                {
                    "id": "r1",
                    "status": "failed",
                    "user_message": "Build feature from issue #141",
                    "started_at": "2026-08-07T10:00:00Z",
                },
                {
                    "id": "r2",
                    "status": "completed",
                    "user_message": "Build feature from issue #142",
                    "started_at": "2026-08-07T11:00:00Z",
                },
            ],
        })
        with patch(
            "automation.pm_harness.archon.subprocess.run",
            side_effect=self._fake_run(payload),
        ):
            records = archon.fetch_workflow_runs({})
        self.assertIsNone(records.error)
        self.assertEqual([r["id"] for r in records], ["r1", "r2"])

    def test_limit_500_parses_complete_snapshot_over_200_rows(self) -> None:
        rows = [
            {
                "id": f"r{i}",
                "status": "completed" if i % 2 else "failed",
                "user_message": f"Build feature from issue #{i}",
                "started_at": "2026-08-07T10:00:00Z",
            }
            for i in range(1, 202)
        ]
        payload = json.dumps({"total": len(rows), "runs": rows})
        seen_cmd: list[str] = []

        def fake(cmd, **kwargs):
            seen_cmd.extend(cmd)
            stdout = kwargs.get("stdout")
            if hasattr(stdout, "write"):
                stdout.write(payload)
                stdout.flush()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch(
            "automation.pm_harness.archon.subprocess.run",
            side_effect=fake,
        ):
            records = archon.fetch_workflow_runs({})
        self.assertEqual(
            seen_cmd,
            ["archon", "workflow", "runs", "--json", "--limit", "500"],
        )
        self.assertIsNone(records.error)
        self.assertEqual(len(records), 201)
        self.assertEqual(records[0]["id"], "r1")
        self.assertEqual(records[200]["id"], "r201")
        # The complete snapshot is searchable by the existing status helpers.
        newest = archon.latest_workflow_run(records, issue_number=201)
        self.assertEqual(newest["id"], "r201")
        by_message = archon.runs_by_message_from(records)
        self.assertIsNone(by_message.error)
        self.assertEqual(
            by_message["Build feature from issue #201"], "completed")

    def test_normalizes_unusable_match_fields(self) -> None:
        payload = json.dumps({
            "runs": [{
                "id": {"unexpected": True},
                "user_message": 123,
                "started_at": 42,
                "completed_at": [],
                "working_path": [],
            }],
        })
        with patch(
            "automation.pm_harness.archon.subprocess.run",
            side_effect=self._fake_run(payload),
        ):
            records = archon.fetch_workflow_runs({})
        self.assertIsNone(records[0]["id"])
        self.assertIsNone(records[0]["user_message"])
        self.assertIsNone(records[0]["started_at"])
        self.assertIsNone(records[0]["completed_at"])
        self.assertIsNone(records[0]["working_path"])

    def test_fetch_runs_by_message_preserves_lookup_health(self) -> None:
        unavailable = WorkflowRuns(error="archon_timeout")
        with patch.object(cycle, "fetch_workflow_runs", return_value=unavailable):
            mapped = cycle.fetch_runs_by_message({})
        self.assertEqual(mapped, {})
        self.assertEqual(mapped.error, "archon_timeout")

    def test_latest_run_works_on_salvaged_partial_rows(self) -> None:
        runs = WorkflowRuns(
            [{
                "id": "r1",
                "user_message": "Build feature from issue #141",
                "started_at": "2026-08-07T10:00:00Z",
                "status": "failed",
            }],
            error="archon_json",
            partial=True,
        )
        chosen = archon.latest_workflow_run(runs, issue_number=141)
        self.assertEqual(chosen["id"], "r1")


if __name__ == "__main__":
    unittest.main()
