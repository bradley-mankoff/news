from __future__ import annotations

import builtins
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import news_pipeline.history_store as history_module
from news_pipeline.diagnostics import RunDiagnostics


class HistoryStoreHelperTests(unittest.TestCase):
    def test_scalar_and_format_helpers_cover_edge_branches(self) -> None:
        result = history_module.HistoryCommandResult(
            action="export",
            dry_run=False,
            db_path=Path("/tmp/history.duckdb"),
            run_count=3,
            file_count=2,
            byte_count=2048,
            deleted_count=1,
            deleted_bytes=1024,
            details=["- detail line"],
        )

        formatted = result.format()
        self.assertIn("History export", formatted)
        self.assertIn("- CSV exports: 2 (2.0 KB)", formatted)
        self.assertIn("- Deleted: 1 (1.0 KB)", formatted)
        self.assertIn("- detail line", formatted)

        self.assertEqual(history_module.normalize_url_for_history(""), "")
        self.assertEqual(history_module.normalize_url_for_history("https://example.com/"), "https://example.com")
        self.assertEqual(history_module._history_scope("daily", False), "daily")
        self.assertEqual(history_module._history_scope("daily", True), "global")
        self.assertEqual(history_module._run_started_at_from_id("2026-06-01_10-00-00"), "2026-06-01T10:00:00")
        self.assertEqual(
            history_module._run_started_at_from_id("run_2026-06-01_10-00-00_done"),
            "2026-06-01T10:00:00",
        )
        self.assertEqual(history_module._last_event([{"label": "done"}, {"label": "failed"}], "failed"), {"label": "failed"})
        self.assertEqual(history_module._last_event_at([{"at": ""}, {"at": "2026-06-01T10:00:00"}]), "2026-06-01T10:00:00")
        self.assertEqual(history_module._last_event_at([{"label": "done"}]), "")
        self.assertEqual(history_module._duration_seconds("bad", "bad"), 0)
        self.assertEqual(
            history_module._duration_seconds("2026-06-01T10:00:00", "2026-06-01T10:01:30"),
            90,
        )

        class CustomValue:
            def __str__(self) -> str:
                return "custom"

        self.assertEqual(history_module._json(CustomValue()), '"custom"')
        self.assertEqual(history_module._int("bad"), 0)
        self.assertEqual(history_module._float("bad"), 0.0)
        self.assertEqual(history_module._bytes_label(10), "10 bytes")
        self.assertEqual(history_module._bytes_label(2048), "2.0 KB")
        self.assertEqual(history_module._bytes_label(2 * 1024 * 1024), "2.0 MB")

        class FakeCon:
            def __init__(self, table_info_rows: list[tuple[int, str, str]] | None = None) -> None:
                self.table_info_rows = table_info_rows or []
                self.calls: list[tuple[str, list[object] | None]] = []
                self._fetchall_rows: list[tuple[int, str, str]] = []
                self._fetchone_row: tuple[object, ...] | None = None

            def execute(self, sql: str, params: list[object] | None = None):
                self.calls.append((sql, params))
                if sql.startswith("PRAGMA table_info"):
                    self._fetchall_rows = self.table_info_rows
                return self

            def fetchall(self):
                return self._fetchall_rows

            def fetchone(self):
                return self._fetchone_row

        fake_con = FakeCon([(0, "existing", "INTEGER")])
        history_module._ensure_columns(fake_con, "runs", {"missing": "VARCHAR"})
        self.assertTrue(any("ALTER TABLE runs ADD COLUMN missing VARCHAR" in sql for sql, _ in fake_con.calls))

    def test_insert_and_upsert_helpers_cover_edge_branches(self) -> None:
        class FakeCon:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[object] | None]] = []
                self._fetchall_rows: list[tuple[object, ...]] = []
                self._fetchone_row: tuple[object, ...] | None = None

            def execute(self, sql: str, params: list[object] | None = None):
                self.calls.append((sql, params))
                if sql.startswith("SELECT first_seen_run_id"):
                    self._fetchone_row = self._fetchone_row
                return self

            def fetchall(self):
                return self._fetchall_rows

            def fetchone(self):
                return self._fetchone_row

        fake_con = FakeCon()
        history_module._insert_run_articles(
            fake_con,
            "2026-06-01_10-00-00",
            "candidate",
            [
                {"url": "https://example.com/a/", "article_id": "A", "title": "One"},
                {"url": "https://example.com/a", "article_id": "A", "title": "Duplicate"},
                {"url": "https://example.com/b", "article_id": "B", "title": "Two"},
            ],
        )
        inserted_run_articles = [sql for sql, _ in fake_con.calls if sql.startswith("INSERT INTO run_articles")]
        self.assertEqual(len(inserted_run_articles), 2)

        fake_con = FakeCon()
        history_module._insert_artifacts(
            fake_con,
            "2026-06-01_10-00-00",
            {
                "details": {"path": "output/run_details_2026-06-01_10-00-00.json", "note": "kept"},
                "image": "output/news_report_2026-06-01_10-00-00_image.png",
                "empty": "",
            },
            imported=False,
        )
        artifact_inserts = [sql for sql, _ in fake_con.calls if sql.startswith("INSERT INTO artifacts")]
        self.assertEqual(len(artifact_inserts), 2)

        fake_con = FakeCon()
        history_module._insert_reports_as_artifacts(
            fake_con,
            "2026-06-01_10-00-00",
            [
                {
                    "path": "output/news_report_2026-06-01_10-00-00.txt",
                    "image_art": {"final_image_path": "output/news_report_2026-06-01_10-00-00_image.png"},
                }
            ],
        )
        report_artifact_inserts = [sql for sql, _ in fake_con.calls if sql.startswith("INSERT INTO artifacts")]
        self.assertEqual(len(report_artifact_inserts), 2)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "run_log_2026-06-01_10-00-00.log"
            path.write_text("log line", encoding="utf-8")
            fake_con = FakeCon()
            history_module._insert_run_log(fake_con, "2026-06-01_10-00-00", str(path))
            self.assertTrue(any(sql.startswith("INSERT INTO run_logs") for sql, _ in fake_con.calls))
            fake_con = FakeCon()
            history_module._insert_run_log(fake_con, "2026-06-01_10-00-00", str(path.parent / "missing.log"))
            self.assertFalse(any(sql.startswith("INSERT INTO run_logs") for sql, _ in fake_con.calls))

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.duckdb"
            history_module.upsert_url_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                run_started_at="2026-06-01T10:00:00",
                preset_id="daily",
                url_reuse_blocking_enabled=True,
                urls=["https://example.com/a/", "https://example.com/a", " "],
                articles=[
                    {
                        "url": "https://example.com/a/",
                        "title": "First title",
                        "source": "Example",
                        "pub_date": "2026-06-01",
                        "article_id": "A",
                    }
                ],
            )
            history_module.upsert_url_history(
                db_path,
                run_id="2026-06-02_10-00-00",
                run_started_at="2026-06-02T10:00:00",
                preset_id="daily",
                url_reuse_blocking_enabled=True,
                urls=["https://example.com/a"],
                articles=[
                    {
                        "url": "https://example.com/a",
                        "title": "Updated title",
                        "source": "Example",
                        "pub_date": "2026-06-02",
                        "article_id": "A",
                    }
                ],
            )
            with history_module.connect(db_path) as con:
                row = con.execute(
                    "SELECT title, published, blocks_reuse FROM url_history WHERE normalized_url = ?",
                    ["https://example.com/a"],
                ).fetchone()
            self.assertEqual(row[0], "Updated title")
            self.assertEqual(row[1], "2026-06-02")
            self.assertTrue(row[2])

        original_import = builtins.__import__

        def import_side_effect(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "duckdb":
                raise ImportError("missing duckdb")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=import_side_effect):
            with self.assertRaises(RuntimeError):
                history_module._duckdb()

    def test_backfill_and_file_helpers_cover_edge_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            run_id = "2026-06-01_10-00-00"
            run_dir = output_dir / run_id[:10]
            run_dir.mkdir(parents=True)

            diagnostics = RunDiagnostics(
                run_started_at="2026-06-01T10:00:00",
                settings={
                    "preset_id": "daily",
                    "url_reuse_blocking_enabled": True,
                    "source_count": 1,
                    "model": "gemma-e2b-tiny",
                    "model_name": "gemma-e2b-tiny",
                    "model_backend": "mlx-lm",
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
                        "fresh_articles": [
                            {
                                "url": "https://example.com/a",
                                "title": "A",
                                "article_id": "a",
                            },
                            "skip",
                        ],
                    }
                ],
                article_summary_count=1,
                events=[
                    {"at": "2026-06-01T10:00:00", "label": "completed"},
                ],
                reports=[
                    {
                        "path": str(run_dir / "news_report_2026-06-01_10-00-00.txt"),
                        "image_art": {
                            "final_image_path": str(run_dir / "news_report_2026-06-01_10-00-00_image.png"),
                        },
                    }
                ],
                artifacts={
                    "details": {"path": str(run_dir / "run_details_2026-06-01_10-00-00.json"), "note": "kept"},
                    "raw": str(run_dir / "news_report_2026-06-01_10-00-00_raw.png"),
                    "empty": "",
                },
            )
            details_path = run_dir / "run_details_2026-06-01_10-00-00.json"
            details_path.write_text(json.dumps(diagnostics.to_dict()), encoding="utf-8")

            (run_dir / "article_summaries_2026-06-01_10-00-00.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-01T10:01:00",
                        "summaries": [
                            {
                                "index": 1,
                                "url": "https://example.com/a",
                                "title": "A",
                                "source": "Example",
                                "published": "2026-06-01",
                                "article_id": "a",
                                "summary": "A summary.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "topic_assigned_article_summaries_2026-06-01_10-00-00.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-01T10:02:00",
                        "summaries": [
                            {
                                "index": 2,
                                "url": "https://example.com/b",
                                "title": "B",
                                "source": "Example",
                                "published": "2026-06-01",
                                "article_id": "b",
                                "summary": "B summary.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "candidate_urls_2026-06-01_10-00-00.txt").write_text(
                "https://example.com/a\n\nhttps://example.com/c\nhttps://example.com/a\n",
                encoding="utf-8",
            )
            (run_dir / "selected_article_urls_2026-06-01_10-00-00.txt").write_text(
                "https://example.com/selected\n",
                encoding="utf-8",
            )
            (run_dir / "run_log_2026-06-01_10-00-00.log").write_text("log line", encoding="utf-8")
            (run_dir / "news_report_2026-06-01_10-00-00.txt").write_text("report", encoding="utf-8")
            (run_dir / "news_report_2026-06-01_10-00-00_primary_dataset.txt").write_text("dataset", encoding="utf-8")
            (run_dir / "news_report_2026-06-01_10-00-00_image_prompt.txt").write_text("prompt", encoding="utf-8")
            (run_dir / "news_report_2026-06-01_10-00-00_raw.png").write_bytes(b"raw")
            (run_dir / "news_report_2026-06-01_10-00-00_image.png").write_bytes(b"final")
            (run_dir / "notes.txt").write_text("wrong timestamp", encoding="utf-8")
            (run_dir / "untimed.txt").write_text("untimed", encoding="utf-8")

            self.assertEqual(history_module._discover_run_ids(output_dir), {run_id})
            self.assertEqual(history_module._find_run_file(output_dir, run_id, details_path.name), details_path)
            self.assertIsNone(history_module._find_run_file(output_dir, run_id, "missing.json"))
            self.assertEqual(
                history_module._load_diagnostics_for_backfill("2026-06-02_10-00-00", None).settings["preset_id"],
                "imported",
            )
            self.assertEqual(history_module._load_summary_files(root / "missing", run_id), {})
            self.assertEqual(history_module._load_url_list_articles(root / "missing", run_id), {})
            self.assertEqual(history_module._read_url_lines(run_dir / "candidate_urls_2026-06-01_10-00-00.txt"), [
                "https://example.com/a",
                "https://example.com/c",
                "https://example.com/a",
            ])
            self.assertEqual(history_module._read_url_lines(run_dir / "missing.txt"), [])
            self.assertEqual(history_module._articles_from_diagnostics(diagnostics), [{"source": "Example", "url": "https://example.com/a", "title": "A", "article_id": "a"}])
            self.assertEqual(
                history_module._summarized_articles(
                    {
                        "summarized": {
                            "summaries": [
                                {"url": "https://example.com/a", "source": "Example", "title": "A", "published": "2026-06-01", "article_id": "a"}
                            ]
                        }
                    }
                ),
                [{"url": "https://example.com/a", "source": "Example", "title": "A", "published": "2026-06-01", "article_id": "a"}],
            )
            self.assertEqual(
                history_module._merge_article_lists(
                    [{"url": "https://example.com/a", "title": "A"}],
                    [
                        {"url": "https://example.com/a", "title": "A duplicate"},
                        {"url": "https://example.com/b", "title": "B"},
                    ],
                ),
                [{"url": "https://example.com/a", "title": "A"}, {"url": "https://example.com/b", "title": "B"}],
            )
            self.assertEqual(history_module._artifact_family("run_details_2026-06-01_10-00-00.json"), "run_details")
            self.assertEqual(history_module._artifact_family("news_report_2026-06-01_10-00-00_primary_dataset.txt"), "primary_dataset")
            self.assertEqual(history_module._artifact_family("news_report_2026-06-01_10-00-00_raw.png"), "raw_image")
            self.assertEqual(history_module._artifact_family("news_report_2026-06-01_10-00-00_image.png"), "final_image")
            self.assertTrue(history_module._is_replaceable_file(Path("run_log_2026-06-01_10-00-00.log")))
            self.assertTrue(history_module._is_raw_image_with_final(run_dir / "news_report_2026-06-01_10-00-00_raw.png"))
            self.assertEqual(history_module._insert_backfill_file_artifacts(object(), output_dir / "missing", run_id), None)
            mismatch_path = run_dir / "run_details_2026-06-02_10-00-00.json"
            mismatch_path.write_text("{}", encoding="utf-8")
            with patch.object(history_module, "_insert_dict", return_value=None):
                history_module._insert_backfill_file_artifacts(object(), output_dir, run_id)
            mismatch_path.unlink()

            db_path = root / "history.duckdb"
            dry_run = history_module.backfill_outputs(output_dir, db_path, dry_run=True)
            self.assertEqual(dry_run.run_count, 1)
            self.assertGreater(len(dry_run.details), 0)

            applied = history_module.backfill_outputs(output_dir, db_path, dry_run=False)
            self.assertEqual(applied.run_count, 1)

            with history_module.connect(db_path) as con:
                self.assertGreater(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM run_articles").fetchone()[0], 0)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM article_summaries").fetchone()[0], 0)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)

            self.assertTrue(history_module._imported_artifact_paths(db_path))

            with patch.object(
                history_module,
                "_insert_dict",
                side_effect=[None, RuntimeError("boom")],
            ):
                history_module._insert_backfill_file_artifacts(
                    object(),  # type: ignore[arg-type]
                    output_dir,
                    run_id,
                )

    def test_cleanup_and_parse_helpers_cover_edge_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir(parents=True)
            keep = output_dir / "latest_run.md"
            keep.write_text("keep", encoding="utf-8")
            (output_dir / "latest_run.log").write_text("log", encoding="utf-8")
            (output_dir / "latest_run_details.json").write_text("{}", encoding="utf-8")
            candidate_paths = [output_dir / f"candidate_{index}.txt" for index in range(81)]
            for path in candidate_paths[:-1]:
                path.write_text("data", encoding="utf-8")
            missing_path = candidate_paths[-1]

            with patch.object(
                history_module,
                "_visible_output_cleanup_candidates",
                return_value=candidate_paths,
            ):
                dry_run = history_module.cleanup_outputs(output_dir, root / "history.duckdb", dry_run=True)
                self.assertEqual(dry_run.file_count, 81)
                self.assertIn("and 1 more file(s)", "\n".join(dry_run.details))
                applied = history_module.cleanup_outputs(output_dir, root / "history.duckdb", dry_run=False)
                self.assertGreaterEqual(applied.deleted_count, 1)

            visible_existing = output_dir / "visible.txt"
            visible_existing.write_text("visible", encoding="utf-8")
            with patch.object(
                history_module,
                "_visible_output_cleanup_candidates",
                return_value=[missing_path, visible_existing],
            ):
                deleted_count, deleted_bytes = history_module.cleanup_visible_outputs(output_dir)
            self.assertEqual(deleted_count, 1)
            self.assertGreater(deleted_bytes, 0)

            empty_dir = output_dir / "nested" / "deeper"
            empty_dir.mkdir(parents=True)
            history_module._remove_empty_output_dirs(output_dir)
            self.assertFalse(empty_dir.exists())

            self.assertEqual(
                history_module._visible_output_cleanup_candidates(root / "missing", keep_paths=[keep]),
                [],
            )
            self.assertEqual(history_module._cleanup_candidates(root / "missing", imported_paths=set()), [])

            with self.assertRaisesRegex(ValueError, "Usage:"):
                history_module.parse_history_args(["-h"], output_dir=output_dir, db_path=root / "history.duckdb", export_csv=False)

            with self.assertRaisesRegex(ValueError, "Usage:"):
                history_module.parse_history_args([], output_dir=output_dir, db_path=root / "history.duckdb", export_csv=False)

            with self.assertRaisesRegex(ValueError, "Usage:"):
                history_module.parse_history_args(["bogus"], output_dir=output_dir, db_path=root / "history.duckdb", export_csv=False)

            export_result = history_module.parse_history_args(
                ["export"],
                output_dir=output_dir,
                db_path=root / "history.duckdb",
                export_csv=True,
            )
            self.assertEqual(export_result.action, "export")

            self.assertEqual(history_module.run_status_from_events([{"label": "failed"}]), "failed")

    def test_remaining_history_helpers_cover_unseen_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing_dir = root / "missing"
            self.assertEqual(history_module._discover_run_ids(missing_dir), set())
            self.assertEqual(history_module._cleanup_candidates(missing_dir, imported_paths=set()), [])
            self.assertEqual(history_module._visible_output_cleanup_candidates(missing_dir, keep_paths=[]), [])
            history_module._remove_empty_output_dirs(missing_dir)
            self.assertEqual(history_module._run_started_at_from_id("not-a-run"), "")
            self.assertEqual(history_module._artifact_family("run_summary_2026-06-01_10-00-00.json"), "run_summary")
            self.assertEqual(history_module._artifact_family("topics_2026-06-01_10-00-00.json"), "topics")
            self.assertEqual(history_module._artifact_family("news_report_2026-06-01_10-00-00.txt"), "final_report")
            self.assertEqual(history_module._artifact_family("plain.txt"), "other")
            self.assertFalse(history_module._is_raw_image_with_final(root / "plain.png"))

            cleanup_dir = root / "cleanup"
            cleanup_dir.mkdir()
            imported_log = cleanup_dir / "run_log_2026-06-01_10-00-00.log"
            imported_log.write_text("log", encoding="utf-8")
            raw_image = cleanup_dir / "news_report_2026-06-01_10-00-00_raw.png"
            raw_image.write_bytes(b"raw")
            (cleanup_dir / "news_report_2026-06-01_10-00-00_image.png").write_bytes(b"final")
            (cleanup_dir / "notes.txt").write_text("ignore", encoding="utf-8")
            (cleanup_dir / ".DS_Store").write_text("ignore", encoding="utf-8")
            (cleanup_dir / "nested_dir").mkdir()
            cleanup_candidates = history_module._cleanup_candidates(cleanup_dir, imported_paths={imported_log})
            self.assertEqual(set(cleanup_candidates), {imported_log, raw_image})

            visible_dir = root / "visible"
            visible_dir.mkdir()
            keep = visible_dir / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            (visible_dir / "latest_run.md").write_text("latest", encoding="utf-8")
            (visible_dir / "latest_run.log").write_text("log", encoding="utf-8")
            (visible_dir / "latest_run_details.json").write_text("{}", encoding="utf-8")
            drop = visible_dir / "drop.txt"
            drop.write_text("drop", encoding="utf-8")
            (visible_dir / "nested_dir").mkdir()
            self.assertEqual(
                history_module._visible_output_cleanup_candidates(visible_dir, keep_paths=[keep]),
                [drop],
            )

            empty_root = root / "empty"
            nested = empty_root / "nested"
            deeper = nested / "deeper"
            deeper.mkdir(parents=True)
            (nested / "keep.txt").write_text("keep", encoding="utf-8")
            history_module._remove_empty_output_dirs(empty_root)
            self.assertFalse(deeper.exists())
            self.assertTrue(nested.exists())
            self.assertEqual(history_module.blocking_urls(root / "missing.duckdb"), set())

            write_history_db = root / "write_history.duckdb"
            write_history_diagnostics = RunDiagnostics(
                run_started_at="2026-06-01T10:00:00",
                settings={
                    "preset_id": "daily",
                    "url_reuse_blocking_enabled": True,
                },
                source_runs=[],
                events=[],
                reports=[],
                artifacts={},
            )
            history_module.write_run_history(
                write_history_db,
                run_id="2026-06-01_10-00-00",
                diagnostics=write_history_diagnostics,
                candidate_articles=[],
                summarized_articles=[],
                selected_articles=[],
                article_summary_records=[],
                story_summary_records=[],
                run_log_path="",
                export_csv=False,
            )
            blocking_db = root / "blocking_history.duckdb"
            blocking_diagnostics = RunDiagnostics(
                run_started_at="2026-06-01T11:00:00",
                settings={
                    "preset_id": "daily",
                    "url_reuse_blocking_enabled": True,
                },
                source_runs=[],
                events=[],
                reports=[],
                artifacts={},
            )
            history_module.write_run_history(
                blocking_db,
                run_id="2026-06-01_11-00-00",
                diagnostics=blocking_diagnostics,
                candidate_articles=[
                    {
                        "url": "https://example.com/blocked",
                        "title": "Blocked",
                        "source": "Example",
                        "pub_date": "2026-06-01",
                        "article_id": "blocked",
                    }
                ],
                summarized_articles=[],
                selected_articles=[],
                article_summary_records=[],
                story_summary_records=[],
                run_log_path="",
                export_csv=True,
            )
            self.assertEqual(history_module.blocking_urls(blocking_db), {"https://example.com/blocked"})

            with patch.object(
                history_module,
                "_discover_run_ids",
                return_value={f"2026-06-01_10-00-{index:02d}" for index in range(41)},
            ):
                dry_run = history_module.backfill_outputs(root / "backfill", root / "backfill.duckdb", dry_run=True)
            self.assertIn("- ...and 1 more run(s)", dry_run.details)

            backfill_result = history_module.HistoryCommandResult(
                action="backfill",
                dry_run=True,
                db_path=write_history_db,
                run_count=1,
            )
            cleanup_result = history_module.HistoryCommandResult(
                action="cleanup",
                dry_run=True,
                db_path=write_history_db,
                file_count=1,
            )
            with patch.object(history_module, "backfill_outputs", return_value=backfill_result) as backfill_mock:
                self.assertIs(
                    history_module.parse_history_args(
                        ["backfill", "--dry-run"],
                        output_dir=visible_dir,
                        db_path=write_history_db,
                        export_csv=False,
                    ),
                    backfill_result,
                )
                backfill_mock.assert_called_once_with(visible_dir, write_history_db, dry_run=True, export_csv=False)
            with patch.object(history_module, "cleanup_outputs", return_value=cleanup_result) as cleanup_mock:
                self.assertIs(
                    history_module.parse_history_args(
                        ["cleanup", "--dry-run"],
                        output_dir=visible_dir,
                        db_path=write_history_db,
                        export_csv=False,
                    ),
                    cleanup_result,
                )
                cleanup_mock.assert_called_once_with(visible_dir, write_history_db, dry_run=True)

            class FakeConnection:
                def __init__(self) -> None:
                    self.executed: list[str] = []

                def __enter__(self) -> "FakeConnection":
                    return self

                def __exit__(self, exc_type, exc, tb) -> bool:
                    return False

                def execute(self, sql: str):
                    self.executed.append(sql)
                    return self

                def fetchall(self):
                    return [("",), ("/tmp/imported",)]

            with patch.object(history_module, "ensure_schema", return_value=None), patch.object(
                history_module,
                "connect",
                return_value=FakeConnection(),
            ):
                self.assertEqual(
                    history_module._imported_artifact_paths(root / "history.duckdb"),
                    {Path("/tmp/imported")},
                )


if __name__ == "__main__":
    unittest.main()
