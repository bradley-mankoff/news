from __future__ import annotations

import unittest

from news_pipeline.article_summarization import (
    ArticleSummarizationRuntime,
    build_article_summary_prompt_messages,
)
from news_pipeline.story_drafting import (
    article_summary_lookup_by_id,
    build_story_synthesis_prompt_messages,
    story_summary_blocks_from_clusters,
)
from news_pipeline.topic_context import build_topic_context


def _topic() -> dict:
    return {
        "key": "science_space_tech",
        "id": "science_space_tech",
        "title": "Science, Space & Tech",
        "description": (
            "Scientific discoveries, space missions, AI, cybersecurity, computing, "
            "hardware, and research with public impact."
        ),
        "rationale": "Stories where science, space, or technology is central.",
        "frame_tags": ["global", "innovation"],
        "boost_phrases": [
            "lunar mission",
            "ai chip supply",
            "cybersecurity breach",
            "mars launch",
        ],
        "keywords": [
            "science policy",
            "space agency",
            "telescope data",
            "quantum computer",
            "extra term",
        ],
    }


def _article_runtime() -> ArticleSummarizationRuntime:
    return ArticleSummarizationRuntime(
        source_feeds={"Fixture Wire": {"name": "Fixture Wire"}},
        recent_window_hours=24,
        article_summary_concurrency=1,
        article_summary_max_tokens=1000,
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda _article: "",
        build_article_fallback_entry=lambda _article: "",
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        has_structured_entry=lambda *_args: True,
        normalize_report_entry=lambda _article, text: text,
        article_completed=lambda: None,
    )


def _summary_entry(article_id: str, summary: str) -> str:
    return (
        "### Fixture article\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Sat, 30 May 2026 15:30:00 GMT\n"
        f"- URL: https://example.com/{article_id}\n"
        f"- Article ID: {article_id}\n\n"
        "Summary:\n"
        f"{summary}"
    )


class TopicContextPromptTests(unittest.TestCase):
    def test_topic_context_is_bounded_to_eight_combined_terms(self) -> None:
        context = build_topic_context(_topic())

        self.assertIn("- Topic id: science_space_tech", context)
        self.assertIn("- Description: Scientific discoveries", context)
        self.assertIn("- Editorial rationale: Stories where science", context)
        self.assertIn("- Frame tags: global, innovation", context)
        self.assertIn("lunar mission", context)
        self.assertIn("quantum computer", context)
        self.assertNotIn("extra term", context)

    def test_article_summary_prompt_uses_topic_context_for_moderate_steering(self) -> None:
        article = {
            "title": "Agency launches lunar relay",
            "source": "Fixture Wire",
            "pub_date": "Sat, 30 May 2026 15:30:00 GMT",
            "url": "https://example.com/lunar-relay",
            "topic_key": "science_space_tech",
            "topic_title": "Science, Space & Tech",
            "topic_context": build_topic_context(_topic()),
            "description": "A space agency launched a lunar communications relay.",
            "text": "The launch supports new lunar surface communications.",
        }

        messages = build_article_summary_prompt_messages(
            article,
            "May 30, 2026",
            _article_runtime(),
        )
        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertIn("Topic context:", prompt_text)
        self.assertIn("- Topic id: science_space_tech", prompt_text)
        self.assertIn("lunar mission", prompt_text)
        self.assertIn("prioritize facts relevant to this topic", prompt_text)
        self.assertIn("do not invent topic relevance", prompt_text)
        self.assertIn("DATABASE_ENTRY:", prompt_text)

    def test_story_blocks_reuse_topic_context_from_article_targets(self) -> None:
        context = build_topic_context(_topic())
        reports = [
            _summary_entry("a1", "The agency launched a lunar relay."),
            _summary_entry("a2", "Engineers confirmed the relay reached orbit."),
        ]
        blocks = story_summary_blocks_from_clusters(
            [
                {
                    "topic_key": "science_space_tech",
                    "topic_title": "Science, Space & Tech",
                    "story_title": "Lunar relay launches",
                    "cluster_article_ids": ["a1", "a2"],
                }
            ],
            article_summary_lookup_by_id(reports),
            {
                "a1": {
                    "article_id": "a1",
                    "topic_context": context,
                    "text": "The relay launched toward the moon.",
                },
                "a2": {
                    "article_id": "a2",
                    "topic_context": context,
                    "text": "The relay reached orbit.",
                },
            },
            min_articles_per_story=2,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["topic_context"], context)

    def test_story_prompt_uses_topic_context_without_losing_output_contract(self) -> None:
        messages = build_story_synthesis_prompt_messages(
            {
                "topic_key": "science_space_tech",
                "topic_title": "Science, Space & Tech",
                "topic_context": build_topic_context(_topic()),
                "story_title": "Lunar relay launches",
                "summaries": ["The agency launched a lunar relay."],
                "citation_sources": [
                    {
                        "local_id": "S1",
                        "title": "Agency launches lunar relay",
                        "source": "Fixture Wire",
                        "published": "Sat, 30 May 2026 15:30:00 GMT",
                        "url": "https://example.com/lunar-relay",
                        "article_id": "a1",
                        "summary": "The agency launched a lunar relay.",
                    }
                ],
            },
            "May 30, 2026",
        )
        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertIn("Topic context:", prompt_text)
        self.assertIn("- Topic id: science_space_tech", prompt_text)
        self.assertIn("prioritize the headline, lede, and details", prompt_text)
        self.assertIn("do not invent topic relevance", prompt_text)
        self.assertIn("Return exactly this format:", prompt_text)
        self.assertIn("Main story: <story paragraph with sentence-end source markers>", prompt_text)
        self.assertIn("Contradictions: NONE", prompt_text)

    def test_story_prompt_falls_back_to_unknown_topic_context(self) -> None:
        messages = build_story_synthesis_prompt_messages(
            {
                "summaries": ["Officials reported a development."],
                "citation_sources": [],
            },
            "May 30, 2026",
        )
        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertIn("Topic: Unknown topic", prompt_text)
        self.assertIn("- Topic: Unknown topic", prompt_text)
        self.assertIn("Story: Story update", prompt_text)


if __name__ == "__main__":
    unittest.main()
