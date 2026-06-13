from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from news_pipeline.diagnostics import RunDiagnostics
from news_pipeline.history_store import connect
from news_pipeline.run_finalizer import RunFinalizer, RunFinalizerAdapters, RunFinalizerConfig


class _Progress:
    def __init__(self) -> None:
        self.details: list[str] = []
        self.warnings: list[str] = []

    def detail(self, message: str) -> None:
        self.details.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class RunFinalizerTests(unittest.TestCase):
    def test_finish_writes_recorded_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.record_candidate_articles([self._article("https://example.com/a", "A")])
            finalizer.record_summarized_articles([self._article("https://example.com/a", "A")])
            finalizer.record_selected_articles([self._article("https://example.com/a", "A")])
            finalizer.record_article_summary_records([self._summary("https://example.com/a", "A")])
            finalizer.record_story_summary_records([self._summary("https://example.com/a", "A")])
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            self.assertTrue(paths["latest_details"].exists())
            self.assertIn("A useful report.", paths["latest_markdown"].read_text(encoding="utf-8"))
            with connect(paths["history_db"]) as con:
                self.assertEqual(con.execute("SELECT status FROM runs").fetchone()[0], "completed")
                self.assertEqual(con.execute("SELECT COUNT(*) FROM run_articles").fetchone()[0], 3)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM article_summaries").fetchone()[0], 2)

    def test_finish_failed_records_failed_status_and_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.record_candidate_articles([self._article("https://example.com/a", "A")])

            finalizer.finish_failed(RuntimeError("synthetic failure"), "Traceback text")

            details = json.loads(paths["latest_details"].read_text(encoding="utf-8"))
            self.assertEqual(details["events"][-1]["label"], "failed")
            self.assertEqual(details["events"][-1]["error_message"], "synthetic failure")
            self.assertEqual(details["events"][-1]["traceback"], "Traceback text")
            with connect(paths["history_db"]) as con:
                self.assertEqual(con.execute("SELECT status FROM runs").fetchone()[0], "failed")

    def test_finish_continues_after_independent_writer_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, progress = self._finalizer(tmpdir)

            def fail_history(*_args, **_kwargs) -> None:
                raise RuntimeError("history down")

            finalizer.adapters = RunFinalizerAdapters(
                model_call_stats_snapshot=lambda: {"calls": {"summary": 1}},
                write_run_history=fail_history,
                progress=progress,
            )
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            self.assertTrue(paths["latest_details"].exists())
            self.assertTrue(paths["latest_markdown"].exists())
            self.assertIn("Run history write failed: history down", progress.warnings)

    def _finalizer(self, tmpdir: str) -> tuple[RunFinalizer, dict[str, Path], _Progress]:
        root = Path(tmpdir)
        output_dir = root / "daily_outputs"
        latest_details = output_dir / "latest_run_details.json"
        latest_markdown = output_dir / "latest_run.md"
        latest_log = output_dir / "latest_run.log"
        latest_log.parent.mkdir(parents=True)
        latest_log.write_text("log", encoding="utf-8")
        history_db = root / "history" / "news_history.duckdb"
        diagnostics = RunDiagnostics(
            run_started_at=datetime(2026, 6, 1, 10, 0, 0).isoformat(timespec="seconds"),
            settings={
                "preset_id": "daily",
                "source_count": 1,
                "history_db_path": str(history_db),
                "latest_run_markdown_path": str(latest_markdown),
                "run_staging_dir": str(output_dir / ".staging"),
                "url_reuse_blocking_enabled": True,
            },
        )
        progress = _Progress()
        finalizer = RunFinalizer(
            diagnostics=diagnostics,
            config=RunFinalizerConfig(
                run_id="2026-06-01_10-00-00",
                latest_run_details_path=latest_details,
                latest_run_markdown_path=latest_markdown,
                latest_run_log_path=latest_log,
                history_db_path=history_db,
                output_dir=output_dir,
                run_log_path=str(latest_log),
            ),
            adapters=RunFinalizerAdapters(
                model_call_stats_snapshot=lambda: {"calls": {"summary": 1}},
                progress=progress,
            ),
        )
        return (
            finalizer,
            {
                "latest_details": latest_details,
                "latest_markdown": latest_markdown,
                "history_db": history_db,
            },
            progress,
        )

    def _article(self, url: str, title: str) -> dict[str, str]:
        return {"url": url, "title": title, "source": "Example"}

    def _summary(self, url: str, title: str) -> dict[str, str]:
        return {"url": url, "title": title, "summary": "Short summary"}


if __name__ == "__main__":
    unittest.main()
