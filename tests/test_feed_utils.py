from __future__ import annotations

import unittest
from datetime import timezone

from news_pipeline.feed_utils import google_news_query_target, parse_feed_datetime, resolve_google_news_url


class FeedUtilsTests(unittest.TestCase):
    def test_google_news_query_target_extracts_article_url(self) -> None:
        url = "https://news.google.com/rss/articles/CBMi?url=https%3A%2F%2Fexample.com%2Fstory"

        self.assertEqual(google_news_query_target(url), "https://example.com/story")
        self.assertEqual(resolve_google_news_url(url), "https://example.com/story")

    def test_parse_feed_datetime_supports_rfc_and_iso_z(self) -> None:
        for raw in ("Mon, 01 Jun 2026 12:00:00 GMT", "2026-06-01T12:00:00Z"):
            with self.subTest(raw=raw):
                parsed = parse_feed_datetime(raw)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.tzinfo, timezone.utc)
                self.assertEqual(parsed.isoformat(), "2026-06-01T12:00:00+00:00")

    def test_parse_feed_datetime_normalizes_portuguese_tokens(self) -> None:
        parsed = parse_feed_datetime("Seg, 01 Jun 2026 12:00:00 GMT")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.isoformat(), "2026-06-01T12:00:00+00:00")
