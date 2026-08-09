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
from unittest.mock import patch

import news_pipeline.pipeline as pipeline
from news_pipeline.history_store import connect


class FailedRunLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        for log_file in list(pipeline.RUN_LOG_FILES):
            if not log_file.closed:
                log_file.close()
        if pipeline.RUN_LOG_FILE is not None and not pipeline.RUN_LOG_FILE.closed:
            pipeline.RUN_LOG_FILE.close()
        for diagnostic_file in list(pipeline.RUN_DIAGNOSTIC_FILES):
            if not diagnostic_file.closed:
                diagnostic_file.close()
        if pipeline.RUN_DIAGNOSTIC_FILE is not None and not pipeline.RUN_DIAGNOSTIC_FILE.closed:
            pipeline.RUN_DIAGNOSTIC_FILE.close()
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_FILE = None
        pipeline.RUN_DIAGNOSTIC_FILES = []
        pipeline.RUN_DIAGNOSTIC_FILE = None
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
            self.assertTrue((output_dir / "latest_run_diagnostics.log").exists())
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

            # Raw diagnostics: every intermediate meter and the full traceback
            # survive in the rolling and durable transcripts, while the staging
            # source is removed only after the durable copy exists.
            rolling_diagnostics = output_dir / "latest_run_diagnostics.log"
            self.assertTrue(rolling_diagnostics.exists())
            rolling_text = rolling_diagnostics.read_text(encoding="utf-8")
            self.assertIn("101000/200000 steps", rolling_text)
            self.assertIn("71000/200000 steps", rolling_text)
            self.assertIn("Traceback", rolling_text)
            self.assertIn("RuntimeError: synthetic clustering failure", rolling_text)
            self.assertNotIn("\r", rolling_text)
            self.assertNotIn("\033", rolling_text)
            durable_diagnostics = history_db_path.parent / "diagnostics" / f"run_diagnostics_{timestamp}.log"
            self.assertTrue(durable_diagnostics.exists())
            durable_text = durable_diagnostics.read_text(encoding="utf-8")
            self.assertIn("101000/200000 steps", durable_text)
            self.assertIn("synthetic clustering failure", durable_text)
            self.assertFalse((run_output_dir / f"run_diagnostics_{timestamp}.log").exists())

            details = json.loads(latest_details.read_text(encoding="utf-8"))
            self.assertEqual(details["events"][-1]["label"], "failed")
            self.assertIn("synthetic clustering failure", details["events"][-1]["traceback"])
            run_diagnostics_artifact = details["artifacts"].get("run_diagnostics")
            self.assertIsNotNone(run_diagnostics_artifact)
            self.assertEqual(
                run_diagnostics_artifact["path"],
                str(durable_diagnostics),
            )
            self.assertEqual(run_diagnostics_artifact["representation"], "diagnostic")
            self.assertEqual(run_diagnostics_artifact["policy"], "raw_backend_transcript")
            self.assertEqual(
                run_diagnostics_artifact["rolling"],
                str(rolling_diagnostics),
            )
            self.assertEqual(
                run_diagnostics_artifact["source"],
                str(run_output_dir / f"run_diagnostics_{timestamp}.log"),
            )

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


if __name__ == "__main__":
    unittest.main()
