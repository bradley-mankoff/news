from __future__ import annotations

import unittest

from news_pipeline.pipeline import _extract_feed_items
from news_pipeline.text_cleaning import clean_article_text, clean_content_text

from tests.pipeline_component_fixtures import RSS_FEED


class MarkupParsingTests(unittest.TestCase):
    def test_clean_content_text_strips_html_noise_and_unescapes_entities(self) -> None:
        cleaned = clean_content_text(
            """
            <div>
              <p>Alpha <strong>Beta</strong> &amp; Gamma</p>
              <style>body { color: red; }</style>
              <script>window.alert("nope")</script>
              <p>See https://example.com/article and www.example.org</p>
            </div>
            """
        )

        self.assertIn("Alpha Beta & Gamma", cleaned)
        self.assertIn("See and", cleaned)
        self.assertNotIn("window.alert", cleaned)
        self.assertNotIn("color: red", cleaned)
        self.assertNotIn("https://example.com/article", cleaned)
        self.assertNotIn("www.example.org", cleaned)

    def test_clean_article_text_handles_xml_markup(self) -> None:
        cleaned = clean_article_text(
            """<?xml version="1.0" encoding="UTF-8"?>
            <article>
              <title>Headline <em>One</em></title>
              <body>First <b>second</b> part.</body>
            </article>
            """
        )

        self.assertIn("Headline One", cleaned)
        self.assertIn("First second part.", cleaned)

    def test_extract_feed_items_reads_rss_fixture_fields(self) -> None:
        items = _extract_feed_items(RSS_FEED)

        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(
            items[0],
            {
                "title": "City expands flood defenses after river levee warnings",
                "link": "https://example.com/climate-levee",
                "description": (
                    "Officials approved climate resilience funding for pumps, "
                    "levee repairs, and neighborhood cooling centers."
                ),
                "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
                "published_at": items[0]["published_at"],
                "source": "Fixture Wire",
            },
        )

    def test_extract_feed_items_reads_atom_entries_and_link_href(self) -> None:
        items = _extract_feed_items(
            """<?xml version="1.0" encoding="UTF-8"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
              <entry>
                <title>Atom headline</title>
                <link href="https://example.com/atom-story" />
                <summary>Atom summary text.</summary>
                <published>2026-05-16T18:15:00Z</published>
              </entry>
            </feed>
            """
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Atom headline")
        self.assertEqual(items[0]["link"], "https://example.com/atom-story")
        self.assertEqual(items[0]["description"], "Atom summary text.")
        self.assertEqual(items[0]["pub_date"], "2026-05-16T18:15:00Z")
        self.assertEqual(items[0]["source"], "")


if __name__ == "__main__":
    unittest.main()
