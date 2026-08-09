from __future__ import annotations

import json
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
    get_run_details,
    list_recent_run_summaries,
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

    def test_delivery_fields_migrate_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            ensure_schema(db_path)
            with connect(db_path) as con:
                con.execute("ALTER TABLE runs DROP COLUMN delivery_status")
                con.execute("ALTER TABLE runs DROP COLUMN delivery_json")

            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_delivery(
                "sent",
                recipients=["reader@example.com"],
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            with connect(db_path) as con:
                row = con.execute(
                    "SELECT delivery_status, delivery_json FROM runs"
                ).fetchone()
            self.assertEqual(row[0], "sent")
            self.assertIn("reader@example.com", row[1])

    def test_delivery_rich_fields_survive_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_delivery(
                "failed",
                recipients=["reader@example.com", "editor@example.com"],
                reason="delivery refused for: editor@example.com",
                error_type="SMTPRecipientsRefused",
                error_message="refused recipient",
                phase="send",
                accepted_recipients=["reader@example.com"],
                rejected_recipients=["editor@example.com"],
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            details = get_run_details(
                db_path, run_id="2026-06-01_10-00-00"
            )
            self.assertEqual(details["delivery_status"], "failed")
            self.assertEqual(details["delivery"]["phase"], "send")
            self.assertEqual(
                details["delivery"]["accepted_recipients"],
                ["reader@example.com"],
            )
            self.assertEqual(
                details["delivery"]["rejected_recipients"],
                ["editor@example.com"],
            )
            # The durable JSON never stores refusal payloads raw; only the
            # redacted reason/error text and address lists are kept.
            serialized = json.dumps(details["delivery"])
            self.assertNotIn("relay denied", serialized)
            self.assertNotIn("secret", serialized.lower())

    def test_delivery_failed_persists_independent_from_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_delivery(
                "failed",
                recipients=["reader@example.com"],
                reason="delivery failed after report construction",
                error_type="RuntimeError",
                error_message="smtp down",
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            with connect(db_path) as con:
                self.assertEqual(
                    con.execute("SELECT status FROM runs").fetchone()[0],
                    "completed",
                )
                self.assertEqual(
                    con.execute("SELECT delivery_status FROM runs").fetchone()[0],
                    "failed",
                )

    def test_list_recent_run_summaries_orders_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            first = self._diagnostics("2026-06-01T10:00:00", preset_id="scratch")
            second = self._diagnostics("2026-06-02T10:00:00", preset_id="daily")
            second.record_delivery(
                "skipped: not_configured",
                recipients=[],
                reason="missing configuration: NEWS_EMAIL_FROM",
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=first,
                export_csv=False,
            )
            write_run_history(
                db_path,
                run_id="2026-06-02_10-00-00",
                diagnostics=second,
                export_csv=False,
            )

            summaries = list_recent_run_summaries(db_path)
            self.assertEqual(
                [summary["run_id"] for summary in summaries],
                ["2026-06-02_10-00-00", "2026-06-01_10-00-00"],
            )
            self.assertEqual(summaries[0]["delivery_status"], "skipped: not_configured")
            # Old rows without delivery data display "not recorded".
            self.assertEqual(summaries[1]["delivery_status"], "not recorded")
            # Completed runs without a generated report are not generated.
            self.assertEqual(summaries[0]["report_status"], "not_generated")
            self.assertEqual(
                summaries[0]["okf_path"],
                str(db_path.parent / "okf" / "2026-06-02_10-00-00"),
            )

            limited = list_recent_run_summaries(db_path, limit=1)
            self.assertEqual(len(limited), 1)
            self.assertEqual(limited[0]["run_id"], "2026-06-02_10-00-00")
            # The limit is bounded even when a caller requests a huge value.
            self.assertEqual(len(list_recent_run_summaries(db_path, limit=999)), 2)
            self.assertEqual(len(list_recent_run_summaries(db_path, limit="bad")), 2)
            self.assertEqual(list_recent_run_summaries(db_path.parent / "missing.duckdb"), [])

    def test_summary_report_status_from_okf_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            completed = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            completed.record_report(path="output/daily_outputs/latest_run.md")
            failed = self._diagnostics("2026-06-02T10:00:00", preset_id="daily")
            failed.events.append(
                {
                    "at": "2026-06-02T10:01:00",
                    "label": "failed",
                    "error_type": "RuntimeError",
                    "error_message": "boom",
                }
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=completed,
                export_csv=False,
            )
            write_run_history(
                db_path,
                run_id="2026-06-02_10-00-00",
                diagnostics=failed,
                export_csv=False,
            )

            okf_report = db_path.parent / "okf" / "2026-06-01_10-00-00" / "report.md"
            okf_report.parent.mkdir(parents=True)
            okf_report.write_text("Report body", encoding="utf-8")

            summaries = {
                summary["run_id"]: summary
                for summary in list_recent_run_summaries(db_path)
            }
            self.assertEqual(summaries["2026-06-01_10-00-00"]["report_status"], "available")
            self.assertEqual(summaries["2026-06-02_10-00-00"]["report_status"], "not_generated")

    def test_get_run_details_decodes_and_returns_none_for_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_delivery(
                "failed",
                recipients=["reader@example.com"],
                error_type="RuntimeError",
                error_message="smtp down",
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            self.assertIsNone(get_run_details(db_path, "missing-run"))
            self.assertIsNone(get_run_details(db_path.parent / "missing.duckdb", "2026-06-01_10-00-00"))

            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details["run_id"], "2026-06-01_10-00-00")
            self.assertEqual(details["run_status"], "completed")
            self.assertEqual(details["delivery_status"], "failed")
            self.assertEqual(details["delivery"]["error_message"], "smtp down")
            self.assertEqual(details["report_status"], "not_generated")
            self.assertEqual(
                details["okf_path"],
                str(db_path.parent / "okf" / "2026-06-01_10-00-00"),
            )
            self.assertEqual(details["settings"]["preset_id"], "daily")
            self.assertEqual(details["events"][-1]["label"], "completed")
            self.assertEqual(details["artifacts"], [])
            self.assertEqual(details["delivery"]["recipients"], ["reader@example.com"])

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
