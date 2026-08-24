from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import news_pipeline.pipeline as pipeline
from news_pipeline import run_log
from news_pipeline.history_store import connect


class FailedRunLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        for log_file in list(pipeline.RUN_LOG_FILES):
            if not log_file.closed:
                log_file.close()
        if pipeline.RUN_LOG_FILE is not None and not pipeline.RUN_LOG_FILE.closed:
            pipeline.RUN_LOG_FILE.close()
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_FILE = None
        pipeline.ACTIVE_RUN_DIAGNOSTICS = None
        pipeline.ACTIVE_RUN_FINALIZER = None
        pipeline.ACTIVE_RUN_SESSION = None
        pipeline.RUN_ACTIVITY_SNAPSHOTS = []
        pipeline.MODEL_CALL_STATS = {
            "calls": {},
            "token_usage": {},
            "retries": 0,
            "fallbacks": 0,
            "failures": {},
        }
        pipeline.MANAGED_MODEL_SERVER_ACTIVE = False
        pipeline.MANAGED_MODEL_SERVER_READY = False
        pipeline.MANAGED_MODEL_SERVER_EXTERNAL = False
        pipeline.MANAGED_MODEL_SERVER_PROCESS = None
        pipeline.MANAGED_MODEL_SERVER_LOG_FILE = None
        for state in list(pipeline.MANAGED_MODEL_SERVERS.values()):
            if state.log_file is not None and not state.log_file.closed:
                state.log_file.close()
        pipeline.MANAGED_MODEL_SERVERS.clear()

    def test_run_logging_creates_missing_parents_and_tees_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_started_at = datetime(2026, 6, 6, 10, 0, 0)
            run_log = root / "missing" / "staging" / "run_log_2026-06-06_10-00-00.log"
            latest_log = root / "missing" / "daily_outputs" / "latest_run.log"
            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                timestamp="2026-06-06_10-00-00",
                output_dir=latest_log.parent,
                run_output_dir=run_log.parent,
                run_staging_dir=run_log.parent,
                latest_run_log_path=latest_log,
            )

            with redirect_stdout(StringIO()):
                pipeline.RunSession(test_config).run(
                    lambda: pipeline.progress_tracker.detail("synthetic progress detail")
                )

            self.assertTrue(run_log.exists())
            self.assertTrue(latest_log.exists())
            self.assertIn("synthetic progress detail", run_log.read_text(encoding="utf-8"))
            self.assertIn("synthetic progress detail", latest_log.read_text(encoding="utf-8"))

    def test_failed_run_writes_rolling_artifacts_and_history_status(self) -> None:
        timestamp = "2026-06-06_10-00-00"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_output_dir = output_dir / ".staging" / timestamp
            history_db_path = root / "history" / "news_history.duckdb"
            latest_markdown = output_dir / "latest_run.md"
            latest_log = output_dir / "latest_run.log"
            latest_details = output_dir / "latest_run_details.json"
            run_log = run_output_dir / f"run_log_{timestamp}.log"
            run_started_at = datetime(2026, 6, 6, 10, 0, 0)

            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                run_date="2026-06-06",
                timestamp=timestamp,
                output_dir=output_dir,
                run_output_dir=run_output_dir,
                run_staging_dir=run_output_dir,
                latest_run_markdown_path=latest_markdown,
                latest_run_log_path=latest_log,
                latest_run_details_path=latest_details,
                history_db_path=history_db_path,
                run_used_urls_path=run_output_dir / "tracked_urls.txt",
            )

            def fail_after_diagnostics() -> None:
                diagnostics = pipeline._new_run_diagnostics(1)
                pipeline.ACTIVE_RUN_DIAGNOSTICS = diagnostics
                pipeline.progress_tracker.detail("synthetic detail before failure")
                raise RuntimeError("synthetic failure")

            with patch.object(pipeline, "timestamp", "wrong-module-timestamp"):
                with redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                        pipeline.RunSession(test_config).run(fail_after_diagnostics)

            self.assertTrue(latest_markdown.exists())
            self.assertTrue(latest_log.exists())
            self.assertTrue(latest_details.exists())
            self.assertIn("| Status | failed |", latest_markdown.read_text(encoding="utf-8"))
            latest_log_text = latest_log.read_text(encoding="utf-8")
            self.assertIn("Traceback", latest_log_text)
            self.assertIn("RuntimeError: synthetic failure", latest_log_text)

            details = json.loads(latest_details.read_text(encoding="utf-8"))
            self.assertEqual(details["events"][-1]["label"], "failed")
            self.assertIn("synthetic failure", details["events"][-1]["traceback"])

            with connect(history_db_path) as con:
                run_id, status = con.execute("SELECT run_id, status FROM runs").fetchone()
                self.assertEqual(run_id, timestamp)
                self.assertEqual(status, "failed")

    def test_failed_run_with_running_servers_preserves_root_error_and_artifacts(self) -> None:
        """A failed run that owns multiple managed servers must stop every
        server and close every log during teardown without replacing the
        root error or losing failure artifacts (issue #133)."""
        timestamp = "2026-06-07_09-30-00"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_output_dir = output_dir / ".staging" / timestamp
            history_db_path = root / "history" / "news_history.duckdb"
            latest_markdown = output_dir / "latest_run.md"
            latest_log = output_dir / "latest_run.log"
            latest_details = output_dir / "latest_run_details.json"
            run_log = run_output_dir / f"run_log_{timestamp}.log"
            run_started_at = datetime(2026, 6, 7, 9, 30, 0)

            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                run_date="2026-06-07",
                timestamp=timestamp,
                output_dir=output_dir,
                run_output_dir=run_output_dir,
                run_staging_dir=run_output_dir,
                latest_run_markdown_path=latest_markdown,
                latest_run_log_path=latest_log,
                latest_run_details_path=latest_details,
                history_db_path=history_db_path,
                run_used_urls_path=run_output_dir / "tracked_urls.txt",
            )

            def fail_with_running_servers() -> None:
                diagnostics = pipeline._new_run_diagnostics(1)
                pipeline.ACTIVE_RUN_DIAGNOSTICS = diagnostics
                pipeline.progress_tracker.detail("synthetic detail before failure")
                default_assignment = pipeline.MODEL_ASSIGNMENTS["default"]
                secondary_assignment = SimpleNamespace(
                    task="article_summary",
                    backend="mlx-lm",
                    base_url="http://127.0.0.1:8090/v1",
                    reference="secondary-ref",
                    name="secondary-name",
                    server_command="cmd",
                    tuning=SimpleNamespace(task_sampling={}),
                )
                default_state = pipeline.ManagedModelServerState(
                    assignment=default_assignment,
                    endpoint_key=pipeline.canonical_model_endpoint(default_assignment.base_url),
                )
                secondary_state = pipeline.ManagedModelServerState(
                    assignment=secondary_assignment,
                    endpoint_key=pipeline.canonical_model_endpoint(secondary_assignment.base_url),
                )
                default_state.process = MagicMock()
                secondary_state.process = MagicMock()
                default_state.log_file = (run_output_dir / "model_server.log").open("w", encoding="utf-8")
                secondary_state.log_file = (run_output_dir / "secondary.log").open("w", encoding="utf-8")
                default_state.ready = True
                secondary_state.ready = True
                pipeline.MANAGED_MODEL_SERVERS[default_state.endpoint_key] = default_state
                pipeline.MANAGED_MODEL_SERVERS[secondary_state.endpoint_key] = secondary_state
                raise RuntimeError("synthetic failure")

            with patch.object(pipeline, "_stop_managed_server_process") as stop, patch.object(
                pipeline, "timestamp", "wrong-module-timestamp"
            ):
                with redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                        pipeline.RunSession(test_config).run(fail_with_running_servers)

            # Every owned server was stopped and the registry cleared.
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(pipeline.MANAGED_MODEL_SERVERS, {})
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_ACTIVE)

            # The failure artifacts still record the original root error.
            self.assertTrue(latest_markdown.exists())
            self.assertIn("| Status | failed |", latest_markdown.read_text(encoding="utf-8"))
            latest_log_text = latest_log.read_text(encoding="utf-8")
            self.assertIn("RuntimeError: synthetic failure", latest_log_text)
            details = json.loads(latest_details.read_text(encoding="utf-8"))
            self.assertEqual(details["events"][-1]["label"], "failed")
            self.assertIn("synthetic failure", details["events"][-1]["traceback"])

    def test_failed_run_logs_are_concise_and_preserve_traceback(self) -> None:
        timestamp = "2026-06-06_13-00-00"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_output_dir = output_dir / ".staging" / timestamp
            history_db_path = root / "history" / "news_history.duckdb"
            latest_log = output_dir / "latest_run.log"
            run_log = run_output_dir / f"run_log_{timestamp}.log"
            latest_details = output_dir / "latest_run_details.json"
            run_started_at = datetime(2026, 6, 6, 13, 0, 0)

            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                run_date="2026-06-06",
                timestamp=timestamp,
                output_dir=output_dir,
                run_output_dir=run_output_dir,
                run_staging_dir=run_output_dir,
                latest_run_markdown_path=output_dir / "latest_run.md",
                latest_run_log_path=latest_log,
                latest_run_details_path=latest_details,
                history_db_path=history_db_path,
            )

            def fail_during_clustering() -> None:
                diagnostics = pipeline._new_run_diagnostics(1)
                pipeline.ACTIVE_RUN_DIAGNOSTICS = diagnostics
                pipeline.progress_tracker.start_story_clustering(200_000, detail="Clustering.")
                for done in range(1_000, 200_001, 10_000):
                    pipeline.progress_tracker.story_clustering_progress(
                        "similarity_pair",
                        {
                            "phase": "pairwise similarity",
                            "done": done,
                            "total": 200_000,
                            "linked_pairs": done // 10_000,
                        },
                    )
                pipeline.progress_tracker.update_meter(done=150_000, force=True)
                raise RuntimeError("synthetic clustering failure")

            with redirect_stdout(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "synthetic clustering failure"):
                    pipeline.RunSession(test_config).run(fail_during_clustering)

            # The staging per-run log is cleaned up after history ingest (the
            # existing dual-file lifecycle); the rolling log is the survivor.
            self.assertFalse(run_log.exists())
            text = latest_log.read_text(encoding="utf-8")
            self.assertNotIn("\r", text)
            self.assertNotIn("\033", text)
            # Initial snapshot and the failure-flushed pending snapshot.
            self.assertIn("0/200000 steps", text)
            self.assertIn("150000/200000 steps", text)
            # Intermediate meters and duplicates are suppressed.
            self.assertNotIn("100000/200000 steps", text)
            self.assertNotIn("70000/200000 steps", text)
            # The failure path keeps the header, summary, and traceback.
            self.assertIn("Daily news run failed", text)
            self.assertIn("Traceback", text)
            self.assertIn("RuntimeError: synthetic clustering failure", text)
            self.assertIn("Run log saved:", text)

            details = json.loads(latest_details.read_text(encoding="utf-8"))
            self.assertEqual(details["events"][-1]["label"], "failed")
            self.assertIn("synthetic clustering failure", details["events"][-1]["traceback"])

            with connect(history_db_path) as con:
                run_id, status = con.execute("SELECT run_id, status FROM runs").fetchone()
                self.assertEqual(run_id, timestamp)
                self.assertEqual(status, "failed")
                log_row = con.execute("SELECT byte_count, content FROM run_logs").fetchone()
                self.assertIn("Traceback", log_row[1])
                self.assertNotIn("\r", log_row[1])
                self.assertNotIn("\033", log_row[1])
                self.assertIn("150000/200000 steps", log_row[1])
                self.assertEqual(log_row[0], len(log_row[1].encode("utf-8")))

    def test_session_finalizer_survives_compat_global_drift(self) -> None:
        timestamp = "2026-06-06_11-00-00"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_output_dir = output_dir / ".staging" / timestamp
            history_db_path = root / "history" / "news_history.duckdb"
            run_started_at = datetime(2026, 6, 6, 11, 0, 0)

            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                run_date="2026-06-06",
                timestamp=timestamp,
                output_dir=output_dir,
                run_output_dir=run_output_dir,
                run_staging_dir=run_output_dir,
                latest_run_markdown_path=output_dir / "latest_run.md",
                latest_run_log_path=output_dir / "latest_run.log",
                latest_run_details_path=output_dir / "latest_run_details.json",
                history_db_path=history_db_path,
            )

            def finish_after_candidate_collection() -> None:
                diagnostics = pipeline._new_run_diagnostics(1)
                finalizer = pipeline._active_run_finalizer(diagnostics, test_config)
                finalizer.record_candidate_articles(
                    [{"url": "https://example.com/a", "title": "A", "source": "Example"}]
                )
                pipeline.ACTIVE_RUN_FINALIZER = None
                diagnostics.event("aborted", reason="no_article_candidates")
                pipeline._finish_run_diagnostics(diagnostics, test_config)

            with redirect_stdout(StringIO()):
                pipeline.RunSession(test_config).run(finish_after_candidate_collection)

            with connect(history_db_path) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM run_articles").fetchone()[0], 1)

    def test_run_pipeline_delegates_to_run_session(self) -> None:
        with patch.object(pipeline.RunSession, "run") as run:
            pipeline.run_pipeline()

        run.assert_called_once_with()

    def test_session_and_diagnostics_wrappers_cover_compatibility_branches(self) -> None:
        timestamp = "2026-06-06_12-00-00"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_started_at = datetime(2026, 6, 6, 12, 0, 0)
            test_config = replace(
                pipeline.CONFIG,
                run_started_at=run_started_at,
                run_date="2026-06-06",
                timestamp=timestamp,
                output_dir=root / "daily_outputs",
                run_output_dir=root / "daily_outputs" / ".staging" / timestamp,
                run_staging_dir=root / "daily_outputs" / ".staging" / timestamp,
                latest_run_markdown_path=root / "daily_outputs" / "latest_run.md",
                latest_run_log_path=root / "daily_outputs" / "latest_run.log",
                latest_run_details_path=root / "daily_outputs" / "latest_run_details.json",
                history_db_path=root / "history" / "news_history.duckdb",
            )

            pipeline.ACTIVE_RUN_SESSION = object()
            with self.assertRaisesRegex(RuntimeError, "Another daily news run session is already active in this process."):
                with pipeline.RunSession(test_config)._activate():
                    pass

            pipeline.ACTIVE_RUN_SESSION = None
            run_log = root / "run.log"
            with run_log.open("w", encoding="utf-8") as handle:
                pipeline.RUN_LOG_FILES = [handle]
                pipeline._write_run_log("   ")
                pipeline._write_run_log("visible message")

            self.assertEqual(run_log.read_text(encoding="utf-8").count("visible message"), 1)

            pipeline.RUN_ACTIVITY_SNAPSHOTS = [{"at": "2026-06-06T12:00:00", "label": "existing"}]
            diagnostics = pipeline._new_run_diagnostics(3)
            self.assertEqual(diagnostics.settings["source_count"], 3)
            self.assertEqual(len(diagnostics.activity_snapshots), 1)

            pipeline.MODEL_CALL_STATS["calls"]["demo"] = 2
            snapshot = pipeline._model_call_stats_snapshot()
            pipeline.MODEL_CALL_STATS["calls"]["demo"] = 99
            self.assertEqual(snapshot["calls"]["demo"], 2)

            finalizer = pipeline._new_run_finalizer(diagnostics, test_config)
            self.assertIs(finalizer.diagnostics, diagnostics)

            pipeline.ACTIVE_RUN_SESSION = SimpleNamespace(diagnostics=None, finalizer=None)
            active_finalizer = pipeline._active_run_finalizer(diagnostics, test_config)
            self.assertIs(pipeline.ACTIVE_RUN_SESSION.finalizer, active_finalizer)
            pipeline.ACTIVE_RUN_SESSION = None
            self.assertIs(pipeline._active_run_finalizer(diagnostics, test_config), active_finalizer)

            with patch.object(active_finalizer, "finish") as finish, patch.object(
                pipeline,
                "_active_run_finalizer",
                return_value=active_finalizer,
            ) as active_finalizer_factory:
                pipeline._finish_run_diagnostics(diagnostics, test_config)

            finish.assert_called_once_with()
            active_finalizer_factory.assert_called_once_with(diagnostics, test_config)


class RunLogSinkIsolationTests(unittest.TestCase):
    """DN-22: a dead run-log sink must not replace the run outcome."""

    def tearDown(self) -> None:
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_WRITER = None
        pipeline.RUN_LOG_SINK_FAILURES = []

    def test_dead_sink_does_not_mask_messages_and_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dead_path = root / "dead.log"
            dead = dead_path.open("w", encoding="utf-8")
            dead.close()  # writes now raise ValueError: closed file
            healthy_path = root / "survivor.log"
            with healthy_path.open("w", encoding="utf-8") as healthy:
                pipeline.RUN_LOG_FILES = [dead, healthy]
                pipeline._write_run_log("visible message")

            # The dead sink was dropped from the active sinks.
            self.assertEqual(
                [log_file.name for log_file in pipeline.RUN_LOG_FILES],
                [str(healthy_path)],
            )
            # Which sink failed, and why, is recorded.
            failures = "\n".join(pipeline.RUN_LOG_SINK_FAILURES)
            self.assertIn(str(dead_path), failures)
            self.assertIn("ValueError", failures)
            # The surviving sink kept the stream and records the failure too.
            text = healthy_path.read_text(encoding="utf-8")
            self.assertIn("visible message", text)
            self.assertIn("dead.log", text)

    def test_sink_dying_midrun_keeps_streaming_to_survivors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staging_path = root / "run_log_staging.log"
            latest_path = root / "latest_run.log"
            staging = staging_path.open("w", encoding="utf-8")
            latest = latest_path.open("w", encoding="utf-8")
            pipeline.RUN_LOG_FILES = [staging, latest]
            pipeline.RUN_LOG_WRITER = run_log.ConciseLogWriter(
                write_line=pipeline._write_run_log_line
            )
            try:
                pipeline._write_run_log("[2/9 sources] starting collection")
                staging.close()  # the staging sink dies mid-run
                pipeline._write_run_log("WARNING: low coverage")
                pipeline._write_run_log("[3/9 clustering] pairwise similarity")
            finally:
                pipeline.RUN_LOG_WRITER.flush()
                pipeline.RUN_LOG_FILES = []
                pipeline.RUN_LOG_WRITER = None
            latest.close()

            text = latest_path.read_text(encoding="utf-8")
            self.assertIn("[2/9 sources] starting collection", text)
            self.assertIn("WARNING: low coverage", text)
            self.assertIn("[3/9 clustering] pairwise similarity", text)
            self.assertIn("run_log_staging.log", text)
            self.assertIn("ValueError", text)
            self.assertEqual(
                [log_file.name for log_file in pipeline.RUN_LOG_FILES], []
            )


if __name__ == "__main__":
    unittest.main()
