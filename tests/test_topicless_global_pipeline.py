from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from news_pipeline.article_summarization import (
    ArticleSummarizationRuntime,
    build_article_summary_prompt_messages,
)
from news_pipeline.article_summary_records import render_markdown_entry
from news_pipeline.config import load_sources
from news_pipeline.pipeline import build_report_body, build_report_html
from news_pipeline.story_drafting import build_story_synthesis_prompt_messages
from news_pipeline.story_selection import (
    StorySelectionRuntime,
    build_precomputed_global_story_synthesis,
    build_story_assigned_article_reports,
    select_global_story_drafts,
)


def _article_runtime() -> ArticleSummarizationRuntime:
    return ArticleSummarizationRuntime(
        source_feeds={"Fixture Wire": {"name": "Fixture Wire"}},
        recent_window_hours=24,
        article_summary_concurrency=1,
        article_summary_max_tokens=1000,
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda article: "\n".join(
            line
            for line in (
                f"- Source: {article.get('source') or 'Unknown source'}",
                f"- Published: {article.get('pub_date') or 'Unknown publish time'}",
                f"- URL: {article.get('url') or 'N/A'}",
                f"- Article ID: {article.get('article_id')}" if article.get("article_id") else "",
                f"- Topic: {article.get('topic_title')}" if article.get("topic_title") else "",
                f"- Story: {article.get('story_title')}" if article.get("story_title") else "",
            )
            if line
        ),
        build_article_fallback_entry=lambda _article: "",
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        has_structured_entry=lambda *_args: True,
        normalize_report_entry=lambda _article, text: text,
        article_completed=lambda: None,
    )


def _story_runtime() -> StorySelectionRuntime:
    return StorySelectionRuntime(
        story_scale_screening_enabled=False,
        model_max_input_tokens=1000,
        model_label="test",
        model_reference="test",
        model_name="test",
        model_backend="test",
        relaxed_story_drafting_guards=True,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=_article_runtime().format_article_metadata,
        story_drafting_word_count=lambda text: len(str(text or "").split()),
        is_low_confidence_report_entry=lambda _entry: False,
        report_reference_key=lambda entry: entry,
    )


def _draft(
    key: str,
    strength: float,
    article_ids: list[str],
    *,
    source_count: int = 4,
    article_count: int | None = None,
) -> dict:
    return {
        "story_key": key,
        "story_title": f"Story {key}",
        "story_headline": f"Headline {key}",
        "paragraph": f"Draft paragraph for story {key}.",
        "story_strength_score": strength,
        "source_count": source_count,
        "article_count": article_count or len(article_ids),
        "average_similarity": strength / 20,
        "story_rank": 1,
        "article_ids": article_ids,
        "cluster_article_ids": article_ids,
    }


def _summary(article_id: str) -> str:
    return (
        f"### Article {article_id}\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Mon, 01 Jun 2026 12:00:00 GMT\n"
        f"- URL: https://example.com/{article_id}\n"
        f"- Article ID: {article_id}\n\n"
        "Summary:\n"
        f"Reported facts for article {article_id}."
    )


class TopiclessGlobalPipelineTests(unittest.TestCase):
    def test_source_loading_rejects_removed_topic_scope_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "sources": [
                            {
                                "key": "Scoped",
                                "url": "https://example.com/scoped.xml",
                                "language": "en",
                                "tier": "core",
                                "allowed_topic_ids": ["old_topic"],
                            },
                            {
                                "key": "Unscoped",
                                "url": "https://example.com/unscoped.xml",
                                "language": "en",
                                "tier": "core",
                            },
                            {
                                "key": "Spanish",
                                "url": "https://example.com/es.xml",
                                "language": "es",
                                "tier": "core",
                                "allowed_topic_ids": ["old_topic"],
                            },
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "removed topic field"):
                load_sources(path, source_scope="core")

    def test_global_story_selection_caps_four_and_rejects_overlap_above_threshold(self) -> None:
        drafts = [
            _draft("a", 10, ["a1", "a2", "a3", "a4"]),
            _draft("b", 9, ["a1", "a2", "b3", "b4"]),
            _draft("c", 8, ["a1", "c2", "c3", "c4"]),
            _draft("d", 7, ["d1", "d2", "d3", "d4"]),
            _draft("e", 6, ["e1", "e2", "e3", "e4"]),
            _draft("f", 5, ["f1", "f2", "f3", "f4"]),
        ]

        selected, stats = select_global_story_drafts(
            drafts,
            max_stories=4,
            overlap_threshold=0.25,
        )

        self.assertEqual([story["story_key"] for story in selected], ["a", "c", "d", "e"])
        self.assertEqual(stats["selected_story_count"], 4)
        self.assertEqual(stats["article_overlap_dedup"]["conflicts_resolved"], 1)
        rejected_by_overlap = [
            story for story in stats["rejected"]
            if story.get("reason") == "article_overlap_above_global_threshold"
        ]
        self.assertEqual([story["story_key"] for story in rejected_by_overlap], ["b"])
        self.assertIn("a1", selected[0]["article_ids"])
        self.assertIn("a1", selected[1]["article_ids"])

    def test_topicless_prompts_omit_topic_payloads(self) -> None:
        article_messages = build_article_summary_prompt_messages(
            {
                "title": "Port strike talks resume",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/ports",
                "description": "Negotiators resumed talks.",
                "text": "Negotiators resumed talks after a strike deadline.",
            },
            "June 01, 2026",
            _article_runtime(),
        )
        story_messages = build_story_synthesis_prompt_messages(
            {
                "story_title": "Port strike talks resume",
                "summaries": ["Negotiators resumed talks after a strike deadline."],
                "citation_sources": [],
            },
            "June 01, 2026",
        )
        prompt_text = "\n\n".join(
            str(message.content)
            for message in [*article_messages, *story_messages]
        )

        self.assertNotIn("Topic:", prompt_text)
        self.assertNotIn("Topic context:", prompt_text)
        self.assertIn("story discovery, selection, and synthesis", prompt_text)
        self.assertIn("Story: Port strike talks resume", prompt_text)

    def test_story_assigned_reports_and_global_synthesis_have_no_topic_sections(self) -> None:
        selected = [
            {
                **_draft("ports", 10, ["a1", "a2"]),
                "story_headline": "Port Talks Resume",
                "main_story_paragraph": "Negotiators resumed port strike talks.[1]",
            }
        ]
        article_reports = [_summary("a1"), _summary("a2")]
        article_targets = [
            {
                "article_id": "a1",
                "title": "Article a1",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/a1",
            },
            {
                "article_id": "a2",
                "title": "Article a2",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/a2",
            },
        ]

        reports, report_stats = build_story_assigned_article_reports(
            selected,
            article_reports,
            article_targets,
            _story_runtime(),
        )
        synthesis, token_stats, _debug = build_precomputed_global_story_synthesis(
            selected,
            reports,
            _story_runtime(),
        )

        self.assertEqual(report_stats["included_report_count"], 2)
        rendered_reports = "\n".join(render_markdown_entry(report) for report in reports)
        self.assertNotIn("- Topic:", rendered_reports)
        self.assertIn("- Story: Story ports", rendered_reports)
        self.assertNotIn("Topic:", token_stats["primary_dataset"])
        self.assertNotRegex(synthesis, r"(?m)^##\s+")
        self.assertIn("### Port Talks Resume", synthesis)

    def test_report_rendering_uses_story_listing_without_topic_grouping(self) -> None:
        summaries = [
            (
                "### Port talks article\n"
                "Metadata:\n"
                "- Source: Fixture Wire\n"
                "- Published: Mon, 01 Jun 2026 12:00:00 GMT\n"
                "- URL: https://example.com/ports\n"
                "- Story: Port Talks Resume\n\n"
                "Summary:\n"
                "Negotiators resumed talks."
            )
        ]
        report_body = build_report_body(
            "Daily News",
            "### Port Talks Resume\n\nNegotiators resumed port strike talks.",
            summaries,
            [],
            None,
        )
        report_html = build_report_html(
            "reader@example.com",
            "Reader",
            "Daily News",
            "### Port Talks Resume\n\nNegotiators resumed port strike talks.",
            summaries,
            [],
            None,
        )

        self.assertIn("[Port Talks Resume] Port talks article", report_body)
        self.assertNotIn("Topic", report_body)
        self.assertIn("Port Talks Resume", report_html)
        self.assertNotIn("Topic", report_html)


if __name__ == "__main__":
    unittest.main()
