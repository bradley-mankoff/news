from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from typing import Any

from news_pipeline.article_summarization import (
    ArticleSummarizationRuntime,
    _notify_article_completed,
    _summarize_single_article,
    build_article_summary_prompt_messages,
    run_article_summary_pass,
)
from news_pipeline.article_summary_records import ArticleSummaryRecord
from news_pipeline import prompt_contracts
from news_pipeline.prompt_templates import PromptTemplate


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

    def test_prompt_instructions_injected_with_contract_intact(self) -> None:
        runtime, _ = _runtime()
        runtime = replace(runtime, prompt_instructions="Playful summary guidance.")
        messages = build_article_summary_prompt_messages(
            {
                "title": "Flood plan expands",
                "source": "Fixture Wire",
                "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/flood",
                "text": "ignored",
            },
            "May 30, 2026",
            runtime,
        )
        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertIn("Playful summary guidance.", prompt_text)
        self.assertIn("DATABASE_ENTRY:", prompt_text)
        self.assertIn("Do not call tools", prompt_text)
        self.assertIn("7. Playful summary guidance.", prompt_text)

    def test_custom_full_template_controls_structure_with_dynamic_payload(self) -> None:
        # A validated custom template must replace the system structure while
        # still receiving the article payload and the code-owned contract;
        # omitting $editorial_instructions bypasses the profile sentence.
        custom = PromptTemplate(
            task="article_summary",
            label="Custom summary",
            system=(
                "Summarize this article for the daily brief. Today: $now_label. "
                "Window: $recent_window_hours hours.\n$output_contract"
            ),
            user="ARTICLE: $article_payload",
            required_placeholders=(
                "now_label",
                "recent_window_hours",
                "article_payload",
                "output_contract",
            ),
            optional_placeholders=("editorial_instructions",),
        )
        runtime, _ = _runtime()
        runtime = replace(
            runtime,
            prompt_instructions="This sentence must NOT appear.",
            prompt_template=custom,
        )
        messages = build_article_summary_prompt_messages(
            {
                "title": "Flood plan expands",
                "source": "Fixture Wire",
                "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/flood",
                "description": "Flooding reported.",
                "text": "Flood waters rose.",
            },
            "May 30, 2026",
            runtime,
        )
        system_text = str(messages[0].content)
        user_text = str(messages[1].content)
        self.assertIn("Summarize this article for the daily brief.", system_text)
        self.assertIn("Today: May 30, 2026.", system_text)
        self.assertIn("Window: 24 hours.", system_text)
        self.assertIn("Start your response with 'DATABASE_ENTRY:'", system_text)
        self.assertNotIn("This sentence must NOT appear.", system_text)
        self.assertIn("ARTICLE: Selected article:", user_text)
        self.assertIn("DATABASE_ENTRY:", user_text)
        self.assertIn("### Flood plan expands", user_text)
        # The rendered pair still satisfies the machine contract.
        prompt_contracts.assert_prompt_contract(
            "article_summary", f"{system_text}\n\n{user_text}"
        )

    def test_custom_full_template_includes_editorial_instructions_when_used(self) -> None:
        custom = PromptTemplate(
            task="article_summary",
            label="Custom with editorial",
            system=(
                "You summarize. Today: $now_label (window $recent_window_hours h). "
                "$editorial_instructions\n$output_contract"
            ),
            user="$article_payload",
            required_placeholders=(
                "now_label",
                "recent_window_hours",
                "article_payload",
                "output_contract",
            ),
            optional_placeholders=("editorial_instructions",),
        )
        runtime, _ = _runtime()
        runtime = replace(
            runtime,
            prompt_instructions="Profile sentence wins.",
            prompt_template=custom,
        )
        messages = build_article_summary_prompt_messages(
            {"title": "T", "source": "Fixture Wire"},
            "May 30, 2026",
            runtime,
        )
        self.assertIn("Profile sentence wins.", str(messages[0].content))


if __name__ == "__main__":
    unittest.main()
