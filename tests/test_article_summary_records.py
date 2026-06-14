from __future__ import annotations

import unittest

from news_pipeline.article_summary_records import (
    ArticleSummaryRecord,
    fallback_record,
    is_low_confidence,
    normalize_model_response,
    parse_markdown_entry,
    records_by_article_id,
    render_markdown_entry,
    to_citation_source,
    to_history_records,
    with_story,
)


class ArticleSummaryRecordTests(unittest.TestCase):
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
        history_row = to_history_records([story_record])[0]
        citation_source = to_citation_source(story_record)

        self.assertEqual(parsed.article_id, "ports-1")
        self.assertEqual(story_record.story, "Story ports")
        self.assertIs(lookup["ports-1"], story_record)
        self.assertEqual(history_row["summary"], "Negotiators resumed talks.")
        self.assertEqual(citation_source["raw_entry"], render_markdown_entry(story_record))

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
