from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
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
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_FILE = None
        pipeline.ACTIVE_RUN_DIAGNOSTICS = None
        pipeline.ACTIVE_RUN_FINALIZER = None
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


if __name__ == "__main__":
    unittest.main()
