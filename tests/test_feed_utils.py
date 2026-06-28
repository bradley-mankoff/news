from __future__ import annotations

import unittest
from datetime import timezone

import base64

from news_pipeline.feed_utils import (
    decode_google_news_article_path,
    google_news_query_target,
    is_google_news_url,
    parse_feed_datetime,
    resolve_google_news_url,
)


class FeedUtilsTests(unittest.TestCase):
    def test_google_news_query_target_extracts_article_url(self) -> None:
        url = "https://news.google.com/rss/articles/CBMi?url=https%3A%2F%2Fexample.com%2Fstory"

        self.assertEqual(google_news_query_target(url), "https://example.com/story")
        self.assertEqual(resolve_google_news_url(url), "https://example.com/story")

    def test_google_news_url_helpers_reject_empty_and_google_targets(self) -> None:
        self.assertFalse(is_google_news_url(None))
        self.assertEqual(google_news_query_target("https://news.google.com/rss/articles/CBMi"), "")
        self.assertEqual(
            google_news_query_target("https://news.google.com/rss/articles/CBMi?u=https://news.google.com/story"),
            "",
        )

    def test_decode_google_news_article_path_extracts_encoded_article_url(self) -> None:
        target = "https://example.com/decoded"
        article_id = base64.urlsafe_b64encode(bytes([len(target)]) + target.encode("latin1")).decode(
            "ascii"
        ).rstrip("=")

        self.assertEqual(
            decode_google_news_article_path(f"https://news.google.com/rss/articles/{article_id}"),
            target,
        )

    def test_decode_google_news_article_path_strips_google_news_wrappers(self) -> None:
        target = "https://example.com/wrapped"
        payload = b"\x08\x13\x22" + bytes([len(target)]) + target.encode("latin1") + b"\xd2\x01\x00"
        article_id = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

        self.assertEqual(
            decode_google_news_article_path(f"https://news.google.com/rss/articles/{article_id}"),
            target,
        )

    def test_decode_google_news_article_path_returns_empty_for_bad_path(self) -> None:
        self.assertEqual(decode_google_news_article_path("https://news.google.com/rss/articles/"), "")
        self.assertEqual(decode_google_news_article_path("not-base64"), "")
        self.assertEqual(decode_google_news_article_path(None), "")  # type: ignore[arg-type]
        self.assertEqual(
            decode_google_news_article_path("https://news.google.com/rss/articles/AA"),
            "",
        )

    def test_resolve_google_news_url_leaves_non_google_urls_alone(self) -> None:
        self.assertEqual(resolve_google_news_url(" https://example.com/story "), "https://example.com/story")

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

    def test_parse_feed_datetime_handles_empty_bad_and_naive_iso_values(self) -> None:
        self.assertIsNone(parse_feed_datetime(""))
        self.assertIsNone(parse_feed_datetime("not a date"))
        self.assertEqual(
            parse_feed_datetime("2026-06-01T12:00:00").isoformat(),  # type: ignore[union-attr]
            "2026-06-01T12:00:00+00:00",
        )
