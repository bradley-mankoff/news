from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from news_pipeline import ui as ui_module
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

    def test_finish_writes_okf_bundle_from_recorded_story_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.record_article_summary_records(
                [
                    {
                        "title": "A daily article",
                        "source": "Example",
                        "published": "2026-06-01T09:00:00Z",
                        "url": "https://example.com/daily",
                        "article_id": "article-daily",
                        "story": "Daily update",
                        "summary": "A concise summary.",
                    }
                ]
            )
            finalizer.record_story_records(
                [
                    {
                        "story_key": "story-daily",
                        "story_title": "Daily update",
                        "article_ids": ["article-daily"],
                        "cluster_article_ids": ["article-daily"],
                        "article_count": 1,
                        "cluster_article_count": 1,
                        "selected_article_count": 1,
                    }
                ]
            )
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            bundle = paths["history_db"].parent / "okf" / "2026-06-01_10-00-00"
            self.assertTrue((bundle / "report.md").is_file())
            self.assertTrue((bundle / "index.md").is_file())
            self.assertTrue((bundle / "stories" / "daily-update.md").is_file())
            self.assertIn(
                "A useful report.",
                (bundle / "report.md").read_text(encoding="utf-8"),
            )

    def test_okf_failure_warns_without_blocking_details_history_or_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, progress = self._finalizer(tmpdir)

            def fail_okf(*_args, **_kwargs) -> Path:
                raise RuntimeError("okf down")

            finalizer.adapters = RunFinalizerAdapters(
                model_call_stats_snapshot=lambda: {"calls": {"summary": 1}},
                write_okf_run_bundle=fail_okf,
                progress=progress,
            )
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            self.assertTrue(paths["latest_details"].is_file())
            self.assertIn(
                "A useful report.",
                paths["latest_markdown"].read_text(encoding="utf-8"),
            )
            with connect(paths["history_db"]) as con:
                self.assertEqual(
                    con.execute("SELECT status FROM runs").fetchone()[0],
                    "completed",
                )
            self.assertIn("OKF Run Bundle write failed: okf down", progress.warnings)


    def test_finish_writes_beehiiv_paste_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            paste_dir = paths["beehiiv_paste_dir"]
            self.assertTrue(paste_dir.is_dir())
            paste_file = paste_dir / "2026-06-01.md"
            self.assertTrue(paste_file.exists())
            self.assertEqual(
                paste_file.read_text(encoding="utf-8"),
                "Daily News Summary\n\nA useful report.",
            )
            self.assertEqual(list((paste_dir.parent / "daily_outputs").rglob("2026-06-01.md")), [])

    def test_beehiiv_paste_skipped_when_report_body_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            paste_dir = paths["beehiiv_paste_dir"]
            self.assertFalse(paste_dir.exists())

    def test_beehiiv_paste_write_failure_does_not_block_other_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, progress = self._finalizer(tmpdir)
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            with patch("pathlib.Path.write_text", side_effect=RuntimeError("disk down")):
                finalizer.finish()

            self.assertTrue(paths["latest_details"].exists())
            self.assertIn("Beehiiv paste file write failed: disk down", progress.warnings)

    def test_individual_write_steps_report_failures_without_stopping_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, _paths, progress = self._finalizer(tmpdir)

            with patch.object(
                finalizer.diagnostics,
                "write_details_json",
                side_effect=RuntimeError("details down"),
            ):
                finalizer._write_details()
            self.assertIn("Latest run details write failed: details down", progress.warnings)

            with patch.object(
                finalizer.diagnostics,
                "write_run_review_markdown",
                side_effect=RuntimeError("review down"),
            ):
                finalizer._write_review()
            self.assertIn("Latest readable run review write failed: review down", progress.warnings)

            def fail_cleanup(*_args, **_kwargs) -> tuple[int, int]:
                raise RuntimeError("cleanup down")

            finalizer.adapters = RunFinalizerAdapters(
                model_call_stats_snapshot=lambda: {"calls": {"summary": 1}},
                cleanup_visible_outputs=fail_cleanup,
                progress=progress,
            )
            finalizer._cleanup_visible_outputs()
            self.assertIn("Visible daily output cleanup failed: cleanup down", progress.warnings)

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

            review_paths = {
                "output_dir": paths["latest_details"].parent,
                "history_db": paths["history_db"],
                "latest_run_markdown": paths["latest_markdown"],
                "latest_run_details": paths["latest_details"],
            }
            with patch.object(ui_module, "_review_paths", return_value=review_paths):
                payload = ui_module.latest_review_payload()
            self.assertEqual(payload["run_status"], "failed")
            self.assertEqual(payload["report_status"], "not_generated")

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

    def test_finish_keeps_completed_report_with_skipped_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.diagnostics.record_delivery(
                "skipped: not_configured",
                recipients=[],
                reason="missing configuration: NEWS_EMAIL_FROM",
            )
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            review = paths["latest_markdown"].read_text(encoding="utf-8")
            self.assertIn("A useful report.", review)
            self.assertIn("| Delivery | skipped: not_configured |", review)
            details = json.loads(paths["latest_details"].read_text(encoding="utf-8"))
            self.assertEqual(details["delivery"]["status"], "skipped: not_configured")
            with connect(paths["history_db"]) as con:
                self.assertEqual(
                    con.execute("SELECT status FROM runs").fetchone()[0],
                    "completed",
                )
                self.assertEqual(
                    con.execute("SELECT delivery_status FROM runs").fetchone()[0],
                    "skipped: not_configured",
                )

    def test_finish_keeps_completed_report_with_failed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer, paths, _progress = self._finalizer(tmpdir)
            finalizer.diagnostics.record_delivery(
                "failed",
                recipients=["reader@example.com"],
                error_type="RuntimeError",
                error_message="smtp down",
            )
            finalizer.record_report_body("Daily News Summary\n\nA useful report.")
            finalizer.diagnostics.event("completed")

            finalizer.finish()

            review = paths["latest_markdown"].read_text(encoding="utf-8")
            self.assertIn("A useful report.", review)
            self.assertIn("| Delivery | failed |", review)
            with connect(paths["history_db"]) as con:
                self.assertEqual(
                    con.execute("SELECT status FROM runs").fetchone()[0],
                    "completed",
                )
                self.assertEqual(
                    con.execute("SELECT delivery_status FROM runs").fetchone()[0],
                    "failed",
                )

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
                beehiiv_paste_dir=root / "beehiiv",
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
                "beehiiv_paste_dir": root / "beehiiv",
            },
            progress,
        )

    def _article(self, url: str, title: str) -> dict[str, str]:
        return {"url": url, "title": title, "source": "Example"}

    def _summary(self, url: str, title: str) -> dict[str, str]:
        return {"url": url, "title": title, "summary": "Short summary"}


if __name__ == "__main__":
    unittest.main()
