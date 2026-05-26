from __future__ import annotations

import unittest

from news_pipeline import citations as citations_stage
from news_pipeline.pipeline import build_report_body, build_report_html
from news_pipeline.story_topic_assignment import (
    StoryTopicRuntime,
    build_precomputed_story_synthesis,
)


def _source(local_id: str, **overrides):
    record = {
        "local_id": local_id,
        "title": f"Fixture article {local_id}",
        "source": "Fixture Wire",
        "published": "Sat, 16 May 2026 15:30:00 GMT",
        "url": f"https://example.com/{local_id.lower()}",
        "article_id": local_id.lower(),
        "summary": "Officials approved levee repairs, backup pumps, and cooling centers.",
    }
    record.update(overrides)
    return record


class CitationHelperTests(unittest.TestCase):
    def test_parse_article_report_entry_extracts_metadata(self) -> None:
        entry = (
            "### Flood plan expands\n"
            "Metadata:\n"
            "- Source: Fixture Wire\n"
            "- Published: Sat, 16 May 2026 15:30:00 GMT\n"
            "- URL: https://example.com/flood\n"
            "- Article ID: fixture-1\n"
            "- Topic: Climate Resilience\n\n"
            "Summary:\n"
            "Officials approved repairs."
        )

        parsed = citations_stage.parse_article_report_entry(entry)

        self.assertEqual(parsed["title"], "Flood plan expands")
        self.assertEqual(parsed["source"], "Fixture Wire")
        self.assertEqual(parsed["url"], "https://example.com/flood")
        self.assertEqual(parsed["article_id"], "fixture-1")
        self.assertEqual(parsed["summary"], "Officials approved repairs.")

    def test_validate_cited_story_text_flags_unknown_markers_and_leaves_uncited_sentences(self) -> None:
        sources = [
            _source("S1", summary="U.S. officials approved levee repairs and backup pumps."),
            _source("S2", summary="The city opened cooling centers before the summer heat season."),
        ]
        marked_text = (
            "U.S. officials approved levee repairs and backup pumps[[S1]]. "
            "The city opened cooling centers[[S9]]. "
            "Road closures may still happen during extreme storms."
        )

        result = citations_stage.validate_cited_story_text(marked_text, sources)

        self.assertEqual(len(result["cited_sentences"]), 3)
        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(result["cited_sentences"][1]["source_ids"], [])
        self.assertEqual(result["cited_sentences"][2]["source_ids"], [])
        self.assertEqual(result["diagnostics"]["unknown_source_ids"], ["S9"])
        self.assertEqual(result["diagnostics"]["uncited_sentence_count"], 2)
        self.assertNotIn("[[", result["paragraph"])

    def test_render_cited_story_reuses_duplicate_url_numbers(self) -> None:
        registry = citations_stage.CitationRegistry()
        sources = [
            _source("S1", url="https://example.com/shared"),
            _source("S2", url="https://example.com/other"),
            _source("S3", url="https://example.com/shared"),
        ]
        cited_sentences = [
            {"text": "The first sentence cites two distinct stories.", "source_ids": ["S1", "S2"]},
            {"text": "The repeated source keeps the same number.", "source_ids": ["S3"]},
        ]

        rendered = citations_stage.render_cited_story_text(cited_sentences, sources, registry)

        self.assertEqual(
            rendered,
            "The first sentence cites two distinct stories.[1][2] "
            "The repeated source keeps the same number.[1]",
        )
        self.assertEqual([source["number"] for source in registry.sources()], [1, 2])

    def test_html_citation_rendering_links_to_escaped_bottom_sources(self) -> None:
        citation_sources = [
            {
                "number": 1,
                "title": "Flood <plan>",
                "source": "Wire & Co",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/flood?a=1&b=2",
            }
        ]

        inline_html = citations_stage.render_html_text_with_citations(
            "Officials approved repairs.[1]",
            citation_sources,
        )
        sources_html = citations_stage.render_html_sources(citation_sources)

        self.assertIn("<sup", inline_html)
        self.assertIn('href="https://example.com/flood?a=1&amp;b=2"', inline_html)
        self.assertIn('[<a href="https://example.com/flood?a=1&amp;b=2"', inline_html)
        self.assertIn(">1</a>]", inline_html)
        self.assertNotIn(">[1]</a>", inline_html)
        self.assertIn('id="source-1"', sources_html)
        self.assertIn("Flood &lt;plan&gt;", sources_html)
        self.assertIn("Wire &amp; Co", sources_html)
        self.assertIn("a=1&amp;b=2", sources_html)

    def test_html_citation_rendering_falls_back_to_internal_anchor_without_url(self) -> None:
        inline_html = citations_stage.render_html_text_with_citations(
            "Officials approved repairs.[1]",
            [{"number": 1, "title": "Flood plan", "source": "Wire", "url": ""}],
        )

        self.assertIn('[<a href="#source-1"', inline_html)


class CitationIntegrationTests(unittest.TestCase):
    def test_precomputed_story_synthesis_assigns_global_citations(self) -> None:
        sources = [
            _source("S1", title="Levee repairs approved", url="https://example.com/levee"),
            _source("S2", title="Cooling centers open", url="https://example.com/cooling"),
        ]
        selected_story_topic_matches = [
            {
                "topic_key": "climate_resilience",
                "topic_title": "Climate Resilience",
                "story_key": "story-1",
                "story_title": "City resilience plan",
                "paragraph": "Officials approved levee repairs. The city opened cooling centers.",
                "cited_sentences": [
                    {"text": "Officials approved levee repairs.", "source_ids": ["S1"]},
                    {"text": "The city opened cooling centers.", "source_ids": ["S2"]},
                ],
                "citation_sources": sources,
                "citation_diagnostics": {"repaired_sentence_count": 0},
                "topic_fit_score": 9,
            }
        ]
        topics = [{"key": "climate_resilience", "title": "Climate Resilience"}]
        runtime = StoryTopicRuntime(
            max_stories_per_topic=2,
            min_score=1,
            diversity_min_distance=0.5,
            model_max_input_tokens=1000,
            model_profile_key="test",
            model_reference="test",
            model_name="test",
            model_backend="test",
            relaxed_final_synthesis_guards=True,
            build_article_heading=lambda article: str(article.get("title") or ""),
            format_article_metadata=lambda article: "",
            format_topic_section_header=lambda title: title.upper(),
            final_synthesis_word_count=lambda text: len(str(text).split()),
            is_low_confidence_report_entry=lambda entry: False,
            report_reference_key=lambda entry: entry,
        )

        synthesis, token_stats, debug = build_precomputed_story_synthesis(
            selected_story_topic_matches,
            topics,
            ["report-1"],
            runtime,
        )

        self.assertIn("Officials approved levee repairs.[1]", synthesis)
        self.assertIn("The city opened cooling centers.[2]", synthesis)
        self.assertEqual(token_stats["citation_source_count"], 2)
        self.assertEqual(
            [source["title"] for source in token_stats["citation_sources"]],
            ["Levee repairs approved", "Cooling centers open"],
        )
        self.assertEqual(len(debug["citation_diagnostics"]), 1)

    def test_report_renderers_use_sources_section_when_citations_exist(self) -> None:
        citation_sources = [
            {
                "number": 1,
                "title": "Levee repairs approved",
                "source": "Fixture Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/levee",
            }
        ]
        synthesis_body = "## Climate Resilience\nOfficials approved levee repairs.[1]"

        report_body = build_report_body(
            "Fixture Daily Brief",
            synthesis_body,
            [],
            [],
            citation_sources=citation_sources,
        )
        report_html = build_report_html(
            "reader@example.com",
            "Reader Example",
            "Fixture Daily Brief",
            synthesis_body,
            [],
            [],
            citation_sources=citation_sources,
        )

        self.assertIn("SOURCES", report_body)
        self.assertNotIn("ARTICLES BY SOURCE", report_body)
        self.assertIn("[1] Levee repairs approved", report_body)
        self.assertIn('href="https://example.com/levee"', report_html)
        self.assertIn('id="source-1"', report_html)


if __name__ == "__main__":
    unittest.main()
