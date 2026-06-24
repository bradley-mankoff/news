from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news_pipeline.diagnostics import RunDiagnostics, run_status_from_events
from news_pipeline.history_store import (
    blocking_urls,
    cleanup_outputs,
    connect,
    ensure_schema,
    export_history_csvs,
    upsert_url_history,
    write_run_history,
)


class HistoryStoreTests(unittest.TestCase):
    def test_write_run_history_is_idempotent_and_exports_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history" / "news_history.duckdb"
            first = self._diagnostics("2026-06-01T10:00:00", preset_id="scratch")
            second = self._diagnostics("2026-06-02T10:00:00", preset_id="daily", blocking=True)

            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=first,
                candidate_articles=[self._article("https://example.com/a", "A")],
                article_summary_records=[self._summary("https://example.com/a", "A")],
                export_csv=True,
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=first,
                candidate_articles=[self._article("https://example.com/a", "A")],
                article_summary_records=[self._summary("https://example.com/a", "A")],
                export_csv=True,
            )
            write_run_history(
                db_path,
                run_id="2026-06-02_10-00-00",
                diagnostics=second,
                candidate_articles=[self._article("https://example.com/b", "B")],
                article_summary_records=[self._summary("https://example.com/b", "B")],
                export_csv=True,
            )

            with connect(db_path) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM article_summaries").fetchone()[0], 2)
                columns = [row[1] for row in con.execute("PRAGMA table_info('runs')").fetchall()]
                self.assertIn("preset_id", columns)
                self.assertNotIn("run" "_mode", columns)

            runs_csv = (db_path.parent / "runs.csv").read_text(encoding="utf-8").splitlines()
            self.assertIn("2026-06-02_10-00-00", runs_csv[1])
            self.assertIn("2026-06-01_10-00-00", runs_csv[2])

    def test_url_history_blocks_only_when_url_reuse_blocking_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            upsert_url_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                run_started_at="2026-06-01T10:00:00",
                preset_id="scratch",
                url_reuse_blocking_enabled=False,
                urls=["https://example.com/draft"],
            )
            upsert_url_history(
                db_path,
                run_id="2026-06-02_10-00-00",
                run_started_at="2026-06-02T10:00:00",
                preset_id="daily",
                url_reuse_blocking_enabled=True,
                urls=["https://example.com/send"],
            )

            self.assertEqual(blocking_urls(db_path), {"https://example.com/send"})

    def test_cleanup_leaves_only_latest_readable_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_dir = output_dir / "2026-06-01"
            run_dir.mkdir(parents=True)
            latest = output_dir / "latest_run.md"
            latest_log = output_dir / "latest_run.log"
            latest_details = output_dir / "latest_run_details.json"
            replaceable = run_dir / "run_details_2026-06-01_10-00-00.json"
            report = run_dir / "news_report_2026-06-01_10-00-00_tiny_default_prompt.txt"
            raw_image = run_dir / "news_report_2026-06-01_10-00-00_tiny_default_prompt_raw.png"
            final_image = run_dir / "news_report_2026-06-01_10-00-00_tiny_default_prompt_image.png"
            latest.write_text("current", encoding="utf-8")
            latest_log.write_text("current log", encoding="utf-8")
            latest_details.write_text("{}", encoding="utf-8")
            replaceable.write_text("{}", encoding="utf-8")
            report.write_text("report", encoding="utf-8")
            raw_image.write_bytes(b"raw")
            final_image.write_bytes(b"final")
            db_path = root / "history.duckdb"

            dry_run = cleanup_outputs(output_dir, db_path, dry_run=True)
            self.assertEqual(dry_run.file_count, 4)
            self.assertTrue(replaceable.exists())
            self.assertTrue(raw_image.exists())

            applied = cleanup_outputs(output_dir, db_path, dry_run=False)
            self.assertEqual(applied.deleted_count, 4)
            self.assertTrue(latest.exists())
            self.assertTrue(latest_log.exists())
            self.assertTrue(latest_details.exists())
            self.assertFalse(replaceable.exists())
            self.assertFalse(raw_image.exists())
            self.assertFalse(report.exists())
            self.assertFalse(final_image.exists())
            self.assertFalse(run_dir.exists())

    def test_run_status_event_precedence(self) -> None:
        self.assertEqual(run_status_from_events([]), "unknown")
        self.assertEqual(
            run_status_from_events([{"label": "completed"}]),
            "completed",
        )
        self.assertEqual(
            run_status_from_events([{"label": "completed"}, {"label": "aborted"}]),
            "aborted",
        )
        self.assertEqual(
            run_status_from_events(
                [{"label": "completed"}, {"label": "aborted"}, {"label": "failed"}]
            ),
            "failed",
        )

    def test_failed_run_history_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily", blocking=True)
            diagnostics.events.append(
                {
                    "at": "2026-06-01T10:01:00",
                    "label": "failed",
                    "error_type": "RuntimeError",
                    "error_message": "synthetic failure",
                }
            )

            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=True,
            )

            with connect(db_path) as con:
                self.assertEqual(
                    con.execute("SELECT status FROM runs").fetchone()[0],
                    "failed",
                )

    def test_write_run_history_migrates_existing_runs_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            ensure_schema(db_path)
            with connect(db_path) as con:
                con.execute("ALTER TABLE runs DROP COLUMN preset_id")

            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            with connect(db_path) as con:
                self.assertEqual(
                    con.execute("SELECT preset_id FROM runs").fetchone()[0],
                    "daily",
                )

    def test_run_review_markdown_includes_kpis_and_report_preview(self) -> None:
        diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="scratch")
        markdown = diagnostics.to_run_review_markdown(
            report_body="Daily News Summary\n==================\n\nA useful preview."
        )

        self.assertIn("# Latest News Run Review", markdown)
        self.assertIn("## Run Settings", markdown)
        self.assertIn("## Top-Level KPIs", markdown)
        self.assertIn("## Funnel Stats", markdown)
        self.assertIn("## Final Output Stats", markdown)
        self.assertIn("## Final Report Preview", markdown)
        self.assertIn("A useful preview.", markdown)

    def test_run_review_write_keeps_existing_file_when_render_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir) / "latest_run.md"
            latest.write_text("previous", encoding="utf-8")
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="scratch")

            with patch.object(
                diagnostics,
                "to_run_review_markdown",
                side_effect=RuntimeError("render failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    diagnostics.write_run_review_markdown(latest, report_body="new")

            self.assertEqual(latest.read_text(encoding="utf-8"), "previous")

    def test_export_history_csvs_creates_empty_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            ensure_schema(db_path)
            exports = export_history_csvs(db_path)
            self.assertTrue(exports)
            self.assertTrue((db_path.parent / "runs.csv").exists())

    def _diagnostics(self, started_at: str, *, preset_id: str, blocking: bool = False) -> RunDiagnostics:
        return RunDiagnostics(
            run_started_at=started_at,
            settings={
                "preset_id": preset_id,
                "url_reuse_blocking_enabled": blocking,
                "source_count": 1,
                "model": "gemma-e2b-tiny",
                "model_label": "default_model",
                "story_cluster_similarity_threshold": 0.31,
                "story_selection_overlap_threshold": 0.25,
                "story_embedding_dedup_threshold": 0.85,
                "min_articles_per_story": 2,
                "max_stories": 4,
            },
            source_runs=[
                {
                    "source_index": 1,
                    "source": "Example",
                    "status": "ok",
                    "feed_item_count": 3,
                    "selected_item_count": 2,
                    "fresh_article_count": 1,
                }
            ],
            article_summary_count=1,
            events=[
                {"at": started_at, "label": "story_clustering", "story_count": 1},
                {"at": started_at, "label": "completed"},
            ],
        )

    def _article(self, url: str, title: str) -> dict[str, str]:
        return {
            "url": url,
            "title": title,
            "source": "Example",
            "pub_date": "2026-06-01",
            "article_id": title.lower(),
        }

    def _summary(self, url: str, title: str) -> dict[str, str | int]:
        return {
            "index": 1,
            "url": url,
            "title": title,
            "source": "Example",
            "published": "2026-06-01",
            "article_id": title.lower(),
            "summary": f"{title} summary.",
        }


if __name__ == "__main__":
    unittest.main()
