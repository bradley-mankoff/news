from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from news_pipeline.article_collection import (
    ArticleCollectionAdapters,
    ArticleCollectionRequest,
    collect_article_candidates,
)
from news_pipeline.diagnostics import RunDiagnostics


class _Progress:
    def __init__(self) -> None:
        self.details: list[str] = []
        self.warnings: list[str] = []
        self.completed: list[tuple[str | None, int, int | None]] = []
        self.fresh_updates: list[tuple[int, str | None]] = []
        self.finish_details: list[str | None] = []

    def detail(self, message: str) -> None:
        self.details.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def source_completed(
        self,
        source_name: str | None = None,
        *,
        worker_count: int | None = None,
        candidate_articles: int = 0,
    ) -> None:
        self.completed.append((source_name, candidate_articles, worker_count))

    def update_source_fresh_articles(
        self,
        total_articles: int,
        *,
        latest_source: str | None = None,
    ) -> None:
        self.fresh_updates.append((total_articles, latest_source))

    def finish_meter(self, *, detail: str | None = None) -> None:
        self.finish_details.append(detail)


class _Finalizer:
    def __init__(self) -> None:
        self.candidate_articles: list[dict[str, Any]] | None = None

    def record_candidate_articles(self, articles: list[dict[str, Any]]) -> None:
        self.candidate_articles = articles


class ArticleCollectionTests(unittest.TestCase):
    def test_collect_records_candidates_diagnostics_history_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            diagnostics = self._diagnostics(root)
            finalizer = _Finalizer()
            progress = _Progress()
            upsert_calls: list[dict[str, Any]] = []
            appended: list[tuple[str, list[str]]] = []

            def fetch_source_context(source_index: int, source_name: str) -> dict[str, Any]:
                contexts = {
                    "alpha": {
                        "source_index": source_index,
                        "source": source_name,
                        "started_at": "2026-06-01T10:00:00+00:00",
                        "completed_at": "2026-06-01T10:00:01+00:00",
                        "elapsed_seconds": 3.5,
                        "error_reason": "",
                        "direct_context": self._direct_context(
                            [
                                self._article("https://example.com/a?utm_source=x", "Alpha"),
                                self._article("https://history.example.com/old", "Old"),
                                self._article("", "Missing"),
                            ],
                            scrape_status_counts={"scrape_timeout": 1},
                        ),
                    },
                    "beta": {
                        "source_index": source_index,
                        "source": source_name,
                        "started_at": "2026-06-01T10:00:02+00:00",
                        "completed_at": "2026-06-01T10:00:03+00:00",
                        "elapsed_seconds": 0.25,
                        "error_reason": "",
                        "direct_context": self._direct_context(
                            [
                                self._article("https://www.example.com/a", "Duplicate"),
                                self._article("https://example.com/b", "Beta"),
                            ],
                            feed_rejected_counts={"wrong_feed_source": 2},
                        ),
                    },
                    "gamma": {
                        "source_index": source_index,
                        "source": source_name,
                        "started_at": "2026-06-01T10:00:04+00:00",
                        "completed_at": "2026-06-01T10:00:04+00:00",
                        "elapsed_seconds": 0.0,
                        "error_reason": "RuntimeError: feed down",
                        "direct_context": None,
                    },
                }
                return contexts[source_name]

            adapters = ArticleCollectionAdapters(
                fetch_source_context=fetch_source_context,
                blocking_urls=lambda _path: {"https://history.example.com/old"},
                upsert_url_history=lambda *args, **kwargs: upsert_calls.append(
                    {"args": args, "kwargs": kwargs}
                ),
                persist_url_list_debug=lambda urls, _label: (str(root / "candidate_urls.txt"), len(urls)),
                append_unique_urls=lambda path, urls: appended.append((path, urls)),
            )

            result = collect_article_candidates(
                self._request(root, sources=["alpha", "beta", "gamma"]),
                diagnostics,
                finalizer,  # type: ignore[arg-type]
                progress,
                adapters,
            )

            self.assertEqual([article["title"] for article in result.article_candidates], ["Alpha", "Beta"])
            self.assertEqual(result.stats.fresh_article_count, 2)
            self.assertEqual(result.stats.candidate_url_count, 2)
            self.assertEqual(result.stats.rejected_counts["seen_in_history"], 1)
            self.assertEqual(result.stats.rejected_counts["duplicate_this_run"], 1)
            self.assertEqual(result.stats.rejected_counts["missing_url"], 1)
            self.assertEqual(result.stats.rejected_counts["wrong_feed_source"], 2)
            self.assertEqual(finalizer.candidate_articles, result.article_candidates)
            self.assertEqual(diagnostics.artifacts["candidate_urls"]["count"], 2)
            self.assertEqual(len(upsert_calls), 1)
            self.assertEqual(
                upsert_calls[0]["kwargs"]["urls"],
                ["https://example.com/a?utm_source=x", "https://example.com/b"],
            )
            self.assertEqual(appended, [])
            self.assertEqual([run["source"] for run in diagnostics.source_runs], ["alpha", "beta", "gamma"])
            self.assertTrue(diagnostics.source_runs[0]["slow_source"])
            self.assertEqual(diagnostics.source_runs[0]["timeout_count"], 1)
            self.assertEqual(diagnostics.source_runs[2]["status"], "source_error")
            self.assertIn("Source failed: gamma: RuntimeError: feed down", progress.warnings)
            self.assertEqual(progress.finish_details, ["2 fresh articles"])
            event_labels = [event["label"] for event in diagnostics.events]
            self.assertIn("source_scrape_timeout", event_labels)
            self.assertIn("slow_source", event_labels)
            self.assertEqual(event_labels[-1], "article_collection")

    def test_history_write_failure_falls_back_to_url_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            appended: list[tuple[str, list[str]]] = []

            def fail_history(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("history down")

            result = collect_article_candidates(
                self._request(root, sources=["alpha"]),
                self._diagnostics(root),
                _Finalizer(),  # type: ignore[arg-type]
                _Progress(),
                ArticleCollectionAdapters(
                    fetch_source_context=lambda source_index, source_name: {
                        "source_index": source_index,
                        "source": source_name,
                        "started_at": "2026-06-01T10:00:00+00:00",
                        "completed_at": "2026-06-01T10:00:00+00:00",
                        "elapsed_seconds": 0.0,
                        "error_reason": "",
                        "direct_context": self._direct_context([self._article("https://example.com/a", "A")]),
                    },
                    upsert_url_history=fail_history,
                    append_unique_urls=lambda path, urls: appended.append((path, urls)),
                ),
            )

            self.assertEqual(len(result.article_candidates), 1)
            self.assertEqual(appended, [(str(root / "used_urls.txt"), ["https://example.com/a"])])

    def _request(self, root: Path, *, sources: list[str]) -> ArticleCollectionRequest:
        return ArticleCollectionRequest(
            sources=sources,
            source_feeds={},
            config=SimpleNamespace(history_db_path=root / "history.duckdb"),  # type: ignore[arg-type]
            run_id="2026-06-01_10-00-00",
            run_started_at="2026-06-01T10:00:00",
            preset_id="daily",
            run_used_urls_path=str(root / "used_urls.txt"),
            slow_source_warning_seconds=2.0,
            source_collection_concurrency=3,
            url_reuse_blocking_enabled=True,
            write_legacy_diagnostics=False,
        )

    def _diagnostics(self, root: Path) -> RunDiagnostics:
        return RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={
                "preset_id": "daily",
                "source_count": 3,
                "history_db_path": str(root / "history.duckdb"),
            },
        )

    def _direct_context(
        self,
        articles: list[dict[str, Any]],
        *,
        scrape_status_counts: dict[str, int] | None = None,
        feed_rejected_counts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "articles": articles,
            "status": "ok",
            "feed_item_count": len(articles),
            "recent_item_count": len(articles),
            "selected_item_count": len(articles),
            "selected_items": [],
            "scrape_attempts": [],
            "scrape_status_counts": scrape_status_counts or {},
            "feed_rejected_counts": feed_rejected_counts or {},
            "feed_rejections": [],
        }

    def _article(self, url: str, title: str) -> dict[str, Any]:
        return {
            "url": url,
            "resolved_url": url,
            "original_rss_url": url,
            "title": title,
            "pub_date": "2026-06-01",
            "text": f"{title} body",
            "scrape_status": "scraped",
        }


if __name__ == "__main__":
    unittest.main()

