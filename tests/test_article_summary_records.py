from __future__ import annotations

import unittest

from news_pipeline.article_summary_records import (
    ArticleSummaryRecord,
    fallback_record,
    format_article_metadata,
    has_structured_entry,
    is_low_confidence,
    normalize_model_response,
    parse_markdown_entry,
    records_by_article_id,
    render_markdown_entry,
    to_citation_source,
    with_story,
)


class ArticleSummaryRecordTests(unittest.TestCase):
    def test_format_metadata_and_structured_entry_helpers(self) -> None:
        article = {
            "article_id": "fixture-1",
            "story_title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
        }

        metadata = format_article_metadata(article)

        self.assertIn("- Article ID: fixture-1", metadata)
        self.assertIn("- Story: Flood plan expands", metadata)
        self.assertTrue(has_structured_entry("DATABASE_ENTRY:\nignored", "Flood plan expands"))
        self.assertTrue(
            has_structured_entry(
                "### Flood plan expands\nMetadata:\nSummary:\nDone.",
                "Flood plan expands",
            )
        )

    def test_normalize_model_response_uses_article_metadata_and_cleans_summary(self) -> None:
        article = {
            "article_id": "fixture-1",
            "title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
        }

        record = normalize_model_response(
            article,
            (
                "DATABASE_ENTRY:\n"
                "### Flood plan expands\n"
                "Metadata:\n"
                "- Source: Wrong Source\n\n"
                "Summary:\n"
                "* Officials approved repairs.\n"
                "```\n"
                "Trailing junk"
            ),
        )

        self.assertEqual(record.title, "Flood plan expands")
        self.assertEqual(record.source, "Fixture Wire")
        self.assertEqual(record.article_id, "fixture-1")
        self.assertEqual(record.summary, "Officials approved repairs. Trailing junk")

    def test_normalize_model_response_keeps_non_prefix_artifact_words(self) -> None:
        article = {
            "article_id": "fixture-1",
            "title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
        }

        record = normalize_model_response(
            article,
            (
                "DATABASE_ENTRY:\n"
                "### Flood plan expands\n"
                "Metadata:\n"
                "- Source: Fixture Wire\n\n"
                "Summary:\n"
                "Outletme provide updates after officials approved repairs."
            ),
        )

        self.assertEqual(
            record.summary,
            "Outletme provide updates after officials approved repairs.",
        )

    def test_normalize_model_response_skips_internal_blank_lines(self) -> None:
        article = {
            "article_id": "fixture-1",
            "title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
        }

        record = normalize_model_response(
            article,
            (
                "DATABASE_ENTRY:\n"
                "### Flood plan expands\n"
                "Metadata:\n"
                "- Source: Fixture Wire\n\n"
                "Summary:\n"
                "* Officials approved repairs.\n"
                "\n"
                "The city opened cooling centers."
            ),
        )

        self.assertEqual(
            record.summary,
            "Officials approved repairs. The city opened cooling centers.",
        )

    def test_normalize_model_response_falls_back_when_summary_filters_to_empty(self) -> None:
        article = {
            "article_id": "fixture-1",
            "title": "Flood plan expands",
            "source": "Fixture Wire",
            "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
            "url": "https://example.com/flood",
        }

        record = normalize_model_response(
            article,
            (
                "DATABASE_ENTRY:\n"
                "### Flood plan expands\n"
                "Metadata:\n"
                "- Source: Fixture Wire\n\n"
                "Summary:\n"
                "\n"
                "prefix artifact\n"
                "The correct format is broken\n"
                "Flood plan expands - stale heading\n"
                "---\n"
                "```\n"
            ),
        )

        self.assertEqual(
            record.summary,
            "No reliable summary generated because the model failed to format its response.",
        )

    def test_fallback_record_uses_article_sentences(self) -> None:
        record = fallback_record(
            {
                "title": "Port talks",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/ports",
                "article_id": "ports-1",
                "text": "First sentence. Second sentence. Third sentence.",
            }
        )

        self.assertEqual(record.summary, "First sentence. Second sentence. Third sentence.")

    def test_parse_render_lookup_and_adapters(self) -> None:
        original = ArticleSummaryRecord(
            title="Port talks article",
            source="Fixture Wire",
            published="Mon, 01 Jun 2026 12:00:00 GMT",
            url="https://example.com/ports",
            article_id="ports-1",
            story="Port Talks Resume",
            summary="Negotiators resumed talks.",
        )

        rendered = render_markdown_entry(original)
        parsed = parse_markdown_entry(rendered)
        story_record = with_story(parsed, "Story ports")
        lookup = records_by_article_id([story_record])
        citation_source = to_citation_source(story_record)

        self.assertEqual(parsed.article_id, "ports-1")
        self.assertEqual(story_record.story, "Story ports")
        self.assertIs(lookup["ports-1"], story_record)
        self.assertEqual(citation_source["raw_entry"], render_markdown_entry(story_record))

    def test_parse_markdown_entry_tolerates_na_url_and_string_low_confidence_fallback(self) -> None:
        parsed = parse_markdown_entry(
            "### Port talks article\n"
            "Metadata:\n"
            "- Source: Fixture Wire\n"
            "- Published: Mon, 01 Jun 2026 12:00:00 GMT\n"
            "- URL: N/A\n\n"
            "Summary:\n"
            "placeholder or metadata-only entry"
        )

        self.assertEqual(parsed.url, "")
        self.assertTrue(is_low_confidence("placeholder or metadata-only entry"))

    def test_low_confidence_uses_summary_text(self) -> None:
        record = ArticleSummaryRecord(
            title="Thin article",
            source="Fixture Wire",
            published="",
            url="",
            article_id="thin-1",
            story="",
            summary="This is a metadata-only entry with no reporting.",
        )

        self.assertTrue(is_low_confidence(record))


if __name__ == "__main__":
    unittest.main()
