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

    def test_run_logs_projection_is_normalized_and_byte_count_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            log_path = Path(tmpdir) / "run_log_2026-06-01_10-00-00.log"
            log_path.write_text(
                "\r[3/9 clustering] [###-----------------] 1000/200000 steps\033[K\n"
                "[3/9 clustering] [##------------------] 2000/200000 steps\033[K\n"
                "[3/9 clustering] [#-------------------] 3000/200000 steps\033[K\n"
                "WARNING: low coverage\n"
                "[3/9 clustering] [####################] 200000/200000 steps\n"
                "Traceback (most recent call last):\n"
                "RuntimeError: synthetic failure\n",
                encoding="utf-8",
            )
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                run_log_path=str(log_path),
                export_csv=False,
            )

            with connect(db_path) as con:
                row = con.execute("SELECT path, byte_count, content FROM run_logs").fetchone()
            self.assertEqual(row[0], str(log_path))
            self.assertNotIn("\r", row[2])
            self.assertNotIn("\033", row[2])
            self.assertNotIn("2000/200000 steps", row[2])
            self.assertIn("1000/200000 steps", row[2])
            self.assertIn("3000/200000 steps", row[2])
            self.assertIn("200000/200000 steps", row[2])
            self.assertIn("WARNING: low coverage", row[2])
            self.assertIn("Traceback (most recent call last):", row[2])
            self.assertIn("RuntimeError: synthetic failure", row[2])
            self.assertEqual(row[1], len(row[2].encode("utf-8")))

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

    def test_translation_columns_migrate_lazily_and_legacy_rows_decode_null(self) -> None:
        """Legacy DuckDB files gain nullable translation columns on the next
        schema pass and old rows decode with null/empty values (issue #172)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            ensure_schema(db_path)
            with connect(db_path) as con:
                columns = {
                    row[1]
                    for row in con.execute("PRAGMA table_info('run_articles')").fetchall()
                }
            self.assertIn("translation_status", columns)
            self.assertIn("translation_reason", columns)
            self.assertIn("translation_source_language", columns)
            self.assertIn("translation_target_language", columns)
            self.assertIn("translation_model", columns)
            self.assertIn("translation_original_text_preview", columns)
            self.assertIn("translation_text_preview", columns)

            # Simulate a legacy database created before translation existed:
            # drop the columns and write a legacy-shaped row.
            with connect(db_path) as con:
                for column in (
                    "translation_status",
                    "translation_reason",
                    "translation_source_language",
                    "translation_target_language",
                    "translation_model",
                    "translation_original_text_preview",
                    "translation_text_preview",
                ):
                    con.execute(f"ALTER TABLE run_articles DROP COLUMN {column}")

            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            legacy_article = {
                "url": "https://example.com/legacy",
                "title": "Legacy",
                "source": "Example",
            }
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                candidate_articles=[legacy_article],
                export_csv=False,
            )
            with connect(db_path) as con:
                row = con.execute(
                    "SELECT translation_status, translation_source_language, "
                    "translation_original_text_preview FROM run_articles"
                ).fetchone()
                self.assertEqual(row, (None, None, None))

    def test_translated_stage_rows_persist_bounded_previews(self) -> None:
        """The translated stage stores bounded previews and provenance, never
        full article bodies (issue #172)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                candidate_articles=[
                    {"url": "https://example.com/a", "title": "A", "source": "El Pais"}
                ],
                translated_articles=[
                    {
                        "url": "https://example.com/a",
                        "title": "A",
                        "source": "El Pais",
                        "translation_status": "translated",
                        "translation_reason": "translated",
                        "translation_source_language": "es",
                        "translation_target_language": "en",
                        "translation_model": "translategemma-4b-it-4bit",
                        "translation_original_text_preview": "Hola mundo",
                        "translation_text_preview": "Hello world",
                    }
                ],
                export_csv=False,
            )
            with connect(db_path) as con:
                rows = con.execute(
                    "SELECT stage, translation_status, translation_model, "
                    "translation_original_text_preview, translation_text_preview "
                    "FROM run_articles ORDER BY stage"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("candidate", None, None, None, None),
                        (
                            "translated",
                            "translated",
                            "translategemma-4b-it-4bit",
                            "Hola mundo",
                            "Hello world",
                        ),
                    ],
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

    def test_prompt_snapshots_persist_decode_and_idempotent_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_prompt_snapshot(
                {
                    "captured_at": "2026-06-01T10:00:01Z",
                    "task": "article_summary",
                    "task_name": "analysis for Headline",
                    "model_task": "article_summary",
                    "max_tokens": 1000,
                    "estimated_input_tokens": 120,
                    "messages": [
                        {"type": "system", "content": "Summarize exactly."},
                        {"type": "human", "content": "Article body"},
                    ],
                    "retry_attempts": 1,
                    "used_fallback": False,
                }
            )
            diagnostics.record_prompt_snapshot(
                {
                    "captured_at": "2026-06-01T10:00:02Z",
                    "task": "story_drafting",
                    "task_name": "story synthesis for Story",
                    "messages": [{"type": "system", "content": "Draft."}],
                }
            )
            diagnostics.settings.update(
                {
                    "prompt_profile_id": "balanced",
                    "prompt_instruction_overrides": {},
                    "prompt_instructions": {"article_summary": "Summarize."},
                    "model_snapshots": {
                        "default": {
                            "reference": "gemma-4-12b-it-4bit",
                            "repository": "mlx-community/gemma-4-12B-it-4bit",
                            "model_id": "mlx-community/gemma-4-12B-it-4bit",
                            "revision": "sha123",
                            "revision_status": "resolved",
                        }
                    },
                    "translation_policy": {
                        "enabled": False,
                        "status": "disabled_not_implemented",
                        "target_language": "en",
                    },
                }
            )

            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=True,
            )
            # Same-run rewrite must remain idempotent (no stale snapshots).
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=True,
            )

            with connect(db_path) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                columns = [row[1] for row in con.execute("PRAGMA table_info('runs')").fetchall()]
                self.assertIn("prompt_snapshots_json", columns)
                row = con.execute(
                    "SELECT settings_json, prompt_snapshots_json FROM runs"
                ).fetchone()
            self.assertIn("prompt_profile_id", row[0])
            self.assertIn("model_snapshots", row[0])
            self.assertIn("translation_policy", row[0])
            self.assertIn("Summarize exactly.", row[1])
            self.assertIn("retry_attempts", row[1])

            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(len(details["prompt_snapshots"]), 2)
            self.assertEqual(details["prompt_snapshots"][0]["sequence"], 1)
            self.assertEqual(details["prompt_snapshots"][0]["task"], "article_summary")
            self.assertEqual(
                details["prompt_snapshots"][0]["messages"][0],
                {"type": "system", "content": "Summarize exactly."},
            )
            self.assertEqual(details["settings"]["prompt_profile_id"], "balanced")
            self.assertEqual(
                details["settings"]["model_snapshots"]["default"]["revision"],
                "sha123",
            )
            self.assertFalse(details["settings"]["translation_policy"]["enabled"])

            runs_csv = (db_path.parent / "runs.csv").read_text(encoding="utf-8").splitlines()
            self.assertIn("prompt_snapshots_json", runs_csv[0])
            self.assertTrue(any("Summarize exactly." in line for line in runs_csv[1:]))

    def test_prompt_snapshots_column_migrates_and_old_rows_decode_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            ensure_schema(db_path)
            with connect(db_path) as con:
                con.execute("ALTER TABLE runs DROP COLUMN prompt_snapshots_json")

            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_prompt_snapshot(
                {
                    "task": "article_summary",
                    "messages": [{"type": "human", "content": "hello"}],
                }
            )
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )

            with connect(db_path) as con:
                columns = [row[1] for row in con.execute("PRAGMA table_info('runs')").fetchall()]
                self.assertIn("prompt_snapshots_json", columns)
                raw = con.execute(
                    "SELECT prompt_snapshots_json FROM runs"
                ).fetchone()[0]
            self.assertIn("hello", raw)
            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details["prompt_snapshots"][0]["messages"][0]["content"], "hello")

    def test_get_run_details_returns_empty_prompt_snapshots_for_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )
            with connect(db_path) as con:
                con.execute("UPDATE runs SET prompt_snapshots_json = NULL")

            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details["prompt_snapshots"], [])
            self.assertEqual(details["metadata_read_errors"], {})

    def test_get_run_details_reports_malformed_fields_and_preserves_valid_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            diagnostics = self._diagnostics("2026-06-01T10:00:00", preset_id="daily")
            diagnostics.record_delivery(
                "sent",
                recipients=["reader@example.com"],
                phase="send",
            )
            diagnostics.record_prompt_snapshot(
                {
                    "task": "article_summary",
                    "messages": [{"type": "human", "content": "hello"}],
                }
            )
            diagnostics.record_report(path="output/daily_outputs/latest_run.md")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )
            okf_report = db_path.parent / "okf" / "2026-06-01_10-00-00" / "report.md"
            okf_report.parent.mkdir(parents=True)
            okf_report.write_text("Report body", encoding="utf-8")
            with connect(db_path) as con:
                con.execute(
                    """
                    UPDATE runs SET
                        settings_json = '{broken',
                        delivery_json = '["not", "a", "dict"]',
                        events_json = '{"not": "a list"}',
                        reports_json = '{bad',
                        artifacts_json = 'not json'
                    """
                )

            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(
                set(details["metadata_read_errors"]),
                {"settings", "delivery", "events", "reports", "artifacts"},
            )
            self.assertIn("invalid JSON", details["metadata_read_errors"]["settings"])
            self.assertEqual(
                details["metadata_read_errors"]["delivery"],
                "expected object metadata",
            )
            self.assertEqual(
                details["metadata_read_errors"]["events"],
                "expected list metadata",
            )
            self.assertIn("invalid JSON", details["metadata_read_errors"]["reports"])
            self.assertIn("invalid JSON", details["metadata_read_errors"]["artifacts"])
            # Diagnostics are bounded and never echo raw persisted JSON.
            for message in details["metadata_read_errors"].values():
                self.assertNotIn("{broken", message)
                self.assertNotIn("{bad", message)
                self.assertNotIn("not json", message)
            # Scalar status/delivery and OKF-derived report availability survive.
            self.assertEqual(details["run_status"], "completed")
            self.assertEqual(details["delivery_status"], "sent")
            self.assertEqual(details["report_status"], "available")
            self.assertEqual(details["report_count"], 1)
            # Relational artifact rows survive malformed artifacts_json.
            self.assertTrue(
                any(artifact["family"] == "final_report" for artifact in details["artifacts"])
            )
            # Safe defaults apply per field; valid siblings stay decoded.
            self.assertEqual(details["settings"], {})
            self.assertEqual(details["delivery"], {})
            self.assertEqual(details["events"], [])
            self.assertEqual(details["reports"], [])
            self.assertEqual(details["stats"]["report_count"], 1)
            self.assertEqual(details["prompt_snapshots"][0]["task"], "article_summary")

            with connect(db_path) as con:
                con.execute(
                    """
                    UPDATE runs SET delivery_json = '{"status":"failed", "accepted_recipients":"reader@example.com", "rejected_recipients":{"recipient":"editor@example.com"}}'
                    """
                )
            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details["delivery"]["accepted_recipients"], [])
            self.assertEqual(details["delivery"]["rejected_recipients"], [])
            self.assertEqual(
                details["metadata_read_errors"]["delivery.accepted_recipients"],
                "expected a JSON list",
            )
            self.assertEqual(
                details["metadata_read_errors"]["delivery.rejected_recipients"],
                "expected a JSON list",
            )

            # NULL/empty legacy columns remain non-errors with existing defaults.
            with connect(db_path) as con:
                con.execute("UPDATE runs SET stats_json = NULL, events_json = ''")
            details = get_run_details(db_path, "2026-06-01_10-00-00")
            self.assertIsNotNone(details)
            assert details is not None
            self.assertNotIn("stats", details["metadata_read_errors"])
            self.assertNotIn("events", details["metadata_read_errors"])
            self.assertEqual(details["stats"], {})
            self.assertEqual(details["events"], [])

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
