from __future__ import annotations

from types import SimpleNamespace
import unittest
from typing import Any

from news_pipeline.article_summarization import (
    ArticleSummarizationRuntime,
    _notify_article_completed,
    _summarize_single_article,
    run_article_summary_pass,
)
from news_pipeline.article_summary_records import ArticleSummaryRecord


def _runtime(*, structured: bool = True, fallback_summary: str = "fallback") -> tuple[ArticleSummarizationRuntime, list[Any]]:
    calls: list[Any] = []

    def build_chat_model(**_kwargs: object) -> object:
        calls.append(("build_chat_model", _kwargs))
        return object()

    def invoke_with_retries(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append(("invoke_with_retries", _kwargs))
        return SimpleNamespace(content="DATABASE_ENTRY:\n### Article\nMetadata:\nSummary:\nDone.")

    def normalize_report_entry(article: dict, text: str) -> ArticleSummaryRecord:
        return ArticleSummaryRecord(
            title=str(article.get("title") or ""),
            source=str(article.get("source") or ""),
            published=str(article.get("pub_date") or ""),
            url=str(article.get("url") or ""),
            article_id=str(article.get("article_id") or ""),
            story=str(article.get("story_title") or ""),
            summary=text if text else fallback_summary,
        )

    return (
        ArticleSummarizationRuntime(
            source_feeds={"Fixture Wire": {"name": "Fixture Wire"}},
            recent_window_hours=24,
            article_summary_concurrency=1,
            article_summary_max_tokens=800,
            build_article_heading=lambda article: str(article.get("title") or "Untitled article"),
            format_article_metadata=lambda article: f"Source: {article.get('source')}",
            build_article_fallback_entry=lambda _article: fallback_summary,
            build_chat_model=build_chat_model,
            invoke_with_retries=invoke_with_retries,
            has_structured_entry=(lambda _content, _target: structured),
            normalize_report_entry=normalize_report_entry,
            article_completed=lambda article=None: calls.append(("article_completed", article)),
        ),
        calls,
    )


class ArticleSummarizationTests(unittest.TestCase):
    def test_notify_article_completed_handles_typeerror_fallback(self) -> None:
        calls: list[object | None] = []

        def article_completed(article: object | None = None) -> None:
            calls.append(article)
            if article is not None:
                raise TypeError("legacy signature")

        runtime = ArticleSummarizationRuntime(
            source_feeds={},
            recent_window_hours=24,
            article_summary_concurrency=1,
            article_summary_max_tokens=800,
            build_article_heading=lambda article: str(article.get("title") or ""),
            format_article_metadata=lambda article: "",
            build_article_fallback_entry=lambda article: "fallback",
            build_chat_model=lambda **_kwargs: object(),
            invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content=""),
            has_structured_entry=lambda *_args: False,
            normalize_report_entry=lambda article, text: ArticleSummaryRecord(
                title=str(article.get("title") or ""),
                source=str(article.get("source") or ""),
                published=str(article.get("pub_date") or ""),
                url=str(article.get("url") or ""),
                article_id=str(article.get("article_id") or ""),
                story="",
                summary=text,
            ),
            article_completed=article_completed,
        )

        _notify_article_completed(runtime, {"title": "Flood plan"})

        self.assertEqual(calls, [{"title": "Flood plan"}, None])

    def test_summarize_single_article_uses_structured_response_and_falls_back_after_three_attempts(self) -> None:
        article = {
            "article_id": "a1",
            "title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
            "text": "ignored",
        }

        runtime, calls = _runtime(structured=True)
        summary = _summarize_single_article(runtime, article)

        self.assertEqual(summary.summary, "DATABASE_ENTRY:\n### Article\nMetadata:\nSummary:\nDone.")
        self.assertEqual(
            [call[0] for call in calls if isinstance(call, tuple)],
            ["build_chat_model", "invoke_with_retries", "article_completed"],
        )

        fallback_runtime, fallback_calls = _runtime(structured=False, fallback_summary="fallback entry")
        fallback_summary = _summarize_single_article(fallback_runtime, article)

        self.assertEqual(fallback_summary.summary, "fallback entry")
        self.assertEqual(
            [call[0] for call in fallback_calls if isinstance(call, tuple)].count("build_chat_model"),
            3,
        )

    def test_run_article_summary_pass_handles_empty_and_concurrent_inputs(self) -> None:
        runtime, _calls = _runtime(structured=True)
        runtime = ArticleSummarizationRuntime(
            **{
                **runtime.__dict__,
                "article_summary_concurrency": 2,
                "normalize_report_entry": lambda article, text: ArticleSummaryRecord(
                    title=str(article.get("title") or ""),
                    source=str(article.get("source") or ""),
                    published=str(article.get("pub_date") or ""),
                    url=str(article.get("url") or ""),
                    article_id=str(article.get("article_id") or ""),
                    story="",
                    summary=text,
                ),
            }
        )

        self.assertEqual(run_article_summary_pass([], runtime), [])

        articles = [
            {"article_id": "a1", "title": "One", "source": "Fixture Wire", "pub_date": "", "url": ""},
            {"article_id": "a2", "title": "Two", "source": "Fixture Wire", "pub_date": "", "url": ""},
        ]
        results = run_article_summary_pass(articles, runtime)

        self.assertEqual([record.article_id for record in results], ["a1", "a2"])


if __name__ == "__main__":
    unittest.main()
