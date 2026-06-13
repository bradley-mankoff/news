from __future__ import annotations

import unittest
from typing import Any

from news_pipeline.pipeline import build_report_body, build_report_html
from news_pipeline.story_selection import (
    StorySelectionRuntime,
    build_precomputed_global_story_synthesis,
)


def _citation_source(number: int, title: str) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "number": number,
        "title": title,
        "source": "Fixture Wire",
        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
        "url": f"https://example.com/{slug}",
    }


def _story_source(local_id: str, title: str) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "local_id": local_id,
        "title": title,
        "source": "Fixture Wire",
        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
        "url": f"https://example.com/{slug}",
        "article_id": slug,
        "summary": f"Reported facts for {title}.",
    }


def _story_runtime() -> StorySelectionRuntime:
    return StorySelectionRuntime(
        story_scale_screening_enabled=False,
        model_max_input_tokens=1000,
        model_profile_key="test",
        model_reference="test",
        model_name="test",
        model_backend="test",
        relaxed_final_synthesis_guards=True,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda _article: "",
        final_synthesis_word_count=lambda text: len(str(text or "").split()),
        is_low_confidence_report_entry=lambda _entry: False,
        report_reference_key=lambda entry: entry,
    )


class GroupedCitationReferenceTests(unittest.TestCase):
    def test_plain_text_references_split_by_story_and_keep_flat_fallback(self) -> None:
        citation_sources = [
            _citation_source(1, "Port talks article"),
            _citation_source(2, "Union vote article"),
            _citation_source(3, "Grid repairs article"),
        ]
        citation_groups = [
            {"story": "Port Talks Resume", "citation_numbers": [1, 2, 2]},
            {"story": "Grid Repairs Accelerate", "citation_numbers": [2]},
        ]

        grouped_body = build_report_body(
            "Daily News",
            "### Port Talks Resume\n\nNegotiators resumed talks.[1]",
            [],
            citation_sources=citation_sources,
            citation_groups=citation_groups,
        )
        flat_body = build_report_body(
            "Daily News",
            "### Port Talks Resume\n\nNegotiators resumed talks.[1]",
            [],
            citation_sources=citation_sources,
        )

        self.assertIn("SOURCES", grouped_body)
        self.assertRegex(grouped_body, r"Port Talks Resume\n-+\n\n\[1\] Port talks article")
        self.assertRegex(
            grouped_body,
            r"Grid Repairs Accelerate\n-+\n\n\[2\] Union vote article",
        )
        self.assertIn("Additional Sources", grouped_body)
        self.assertIn("[3] Grid repairs article", grouped_body)
        self.assertEqual(grouped_body.count("[2] Union vote article"), 2)
        self.assertNotIn("Port Talks Resume\n-", flat_body)
        self.assertNotIn("Additional Sources", flat_body)
        self.assertIn("[1] Port talks article", flat_body)

    def test_html_references_group_by_story_and_anchor_each_source_once(self) -> None:
        citation_sources = [
            _citation_source(1, "Port talks article"),
            _citation_source(2, "Union vote article"),
            _citation_source(3, "Grid repairs article"),
        ]
        citation_groups = [
            {"story": "Port Talks Resume[1]", "citation_numbers": [1, 2]},
            {"story": "Grid Repairs Accelerate", "citation_numbers": [2]},
        ]

        report_html = build_report_html(
            "reader@example.com",
            "Reader",
            "Daily News",
            "### Port Talks Resume\n\nNegotiators resumed talks.[1]",
            [],
            citation_sources=citation_sources,
            citation_groups=citation_groups,
        )

        self.assertIn("Port Talks Resume", report_html)
        self.assertNotIn("Port Talks Resume[1]", report_html)
        self.assertIn("Grid Repairs Accelerate", report_html)
        self.assertIn("Additional Sources", report_html)
        self.assertIn("[1]</span>", report_html)
        self.assertEqual(report_html.count('id="source-1"'), 1)
        self.assertEqual(report_html.count('id="source-2"'), 1)
        self.assertEqual(report_html.count('id="source-3"'), 1)

    def test_global_story_synthesis_emits_citation_groups(self) -> None:
        selected_stories = [
            {
                "story_key": "ports",
                "story_title": "Port talks",
                "story_headline": "Port Talks Resume[99]",
                "paragraph": "Negotiators resumed talks. Union leaders opened a vote.",
                "cited_sentences": [
                    {"text": "Negotiators resumed talks.", "source_ids": ["S1"]},
                    {"text": "Union leaders opened a vote.", "source_ids": ["S2"]},
                ],
                "citation_sources": [
                    _story_source("S1", "Port talks article"),
                    _story_source("S2", "Union vote article"),
                ],
            },
            {
                "story_key": "grid",
                "story_title": "Grid repairs",
                "story_headline": "Grid Repairs Accelerate",
                "paragraph": "Crews accelerated grid repairs.",
                "cited_sentences": [
                    {"text": "Crews accelerated grid repairs.", "source_ids": ["S3"]},
                ],
                "citation_sources": [
                    _story_source("S3", "Grid repairs article"),
                ],
            },
        ]

        synthesis, token_stats, debug = build_precomputed_global_story_synthesis(
            selected_stories,
            ["report-1", "report-2"],
            _story_runtime(),
        )

        self.assertIn("### Port Talks Resume", synthesis)
        self.assertEqual(token_stats["citation_source_count"], 3)
        self.assertEqual(token_stats["citation_group_count"], 2)
        self.assertEqual(
            [
                (group["story"], group["citation_numbers"])
                for group in token_stats["citation_groups"]
            ],
            [
                ("Port Talks Resume", [1, 2]),
                ("Grid Repairs Accelerate", [3]),
            ],
        )
        self.assertEqual(debug["attempts"][0]["story_citation_numbers"], [1, 2])


if __name__ == "__main__":
    unittest.main()
