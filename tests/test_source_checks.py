from __future__ import annotations

import argparse
import builtins
import contextlib
import gzip
import io
import json
import sys
import types
import unittest
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

from news_pipeline import source_checks as sc
from news_pipeline.config import ROOT_DIR
from news_pipeline.source_catalog import DeleteSources, SetSourceLanguages


def _rss_feed(*items: str) -> bytes:
    body = "".join(items)
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<rss><channel>"
        f"{body}"
        "</channel></rss>"
    ).encode("utf-8")


def _atom_feed(*entries: str) -> bytes:
    body = "".join(entries)
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<feed xmlns=\"http://www.w3.org/2005/Atom\">"
        f"{body}"
        "</feed>"
    ).encode("utf-8")


def _make_source(
    key: str,
    *,
    url: str = "https://example.com/feed",
    fetcher: str = "rss",
    name: str | None = None,
    section: str = "sources",
    language: str = "",
) -> dict[str, object]:
    return {
        "key": key,
        "name": name or key,
        "section": section,
        "url": url,
        "fetcher": fetcher,
        "language": language,
    }


class SourceChecksHelpersTests(unittest.TestCase):
    def test_decompress_and_fetch_url_helpers(self) -> None:
        plain = b"plain"
        gzipped = gzip.compress(b"gzip")
        deflated = zlib.compress(b"deflate")

        self.assertEqual(sc._decompress_response_body(plain, ""), plain)
        self.assertEqual(sc._decompress_response_body(gzipped, "gzip"), b"gzip")
        self.assertEqual(sc._decompress_response_body(deflated, "deflate"), b"deflate")

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self._body = body
                self.headers = {"Content-Encoding": "gzip"}

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        with patch("news_pipeline.source_checks.urllib.request.urlopen", return_value=FakeResponse(gzipped)) as urlopen:
            body, status = sc._fetch_url_once("https://example.com/feed", 5)

        self.assertEqual(body, b"gzip")
        self.assertEqual(status, 200)
        self.assertEqual(urlopen.call_count, 1)

        with patch("news_pipeline.source_checks._fetch_url_once", side_effect=[ValueError("boom"), (b"ok", 204)]) as fetch_once, patch(
            "news_pipeline.source_checks.time.sleep"
        ) as sleep:
            body, status = sc._fetch_url("https://example.com/feed", 5, retries=1)

        self.assertEqual(body, b"ok")
        self.assertEqual(status, 204)
        self.assertEqual(fetch_once.call_count, 2)
        sleep.assert_called_once_with(0.25)

        with patch("news_pipeline.source_checks._fetch_url_once", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                sc._fetch_url("https://example.com/feed", 5, retries=0)

    def test_recent_probe_and_datetime_helpers(self) -> None:
        updated = sc._recent_probe_url("https://news.google.com/rss/search?q=foo&hl=en", 7)
        parsed = urllib.parse.urlsplit(updated)
        self.assertEqual(parsed.netloc, "news.google.com")
        self.assertIn("when%3A7d", parsed.query)
        self.assertIn("hl=en", parsed.query)

        rewritten = sc._recent_probe_url("https://news.google.com/rss/search?q=foo+when%3A1d", 3)
        self.assertIn("when%3A3d", urllib.parse.urlsplit(rewritten).query)
        self.assertNotIn("when%3A1d", urllib.parse.urlsplit(rewritten).query)
        self.assertIn(
            "when%3A7d",
            urllib.parse.urlsplit(sc._recent_probe_url("https://news.google.com/rss/search?hl=en", 7)).query,
        )
        self.assertEqual(sc._recent_probe_url("https://example.com/feed", 7), "https://example.com/feed")

        self.assertIsNone(sc._parse_unix_datetime(None))
        self.assertIsNone(sc._parse_unix_datetime(True))
        self.assertIsNone(sc._parse_unix_datetime("bad"))
        self.assertEqual(
            sc._parse_unix_datetime("1710000000").isoformat(),  # type: ignore[union-attr]
            "2024-03-09T16:00:00+00:00",
        )
        self.assertEqual(
            sc._parse_unix_datetime("1710000000000").isoformat(),  # type: ignore[union-attr]
            "2024-03-09T16:00:00+00:00",
        )
        self.assertIsNone(sc._parse_unix_datetime("1e20"))
        self.assertIsNone(sc._parse_unix_datetime(-1))
        self.assertIsNone(sc._format_feed_datetime(None))
        self.assertEqual(
            sc._format_feed_datetime(datetime(2026, 6, 1, 12, 0, 0)),
            "2026-06-01T12:00:00Z",
        )
        self.assertIsNone(sc._json_record_datetime({}))

        json_bytes = json.dumps(
            {
                "data": {
                    "children": [
                        {"data": {"created_utc": 1710000000}},
                        {"data": {"created": 1710003600}},
                    ]
                },
                "items": [{"published": "2026-06-01T12:00:00Z"}],
            }
        ).encode("utf-8")
        self.assertEqual(len(sc._json_item_datetimes(json_bytes)), 3)
        self.assertEqual(len(sc._json_item_datetimes(json.dumps([{"created_utc": 1710000000}, "skip"]).encode("utf-8"))), 1)
        self.assertEqual(
            len(sc._json_item_datetimes(json.dumps({"data": [{"created_utc": 1710000000}]}).encode("utf-8"))),
            1,
        )
        self.assertEqual(sc._json_item_datetimes(json.dumps({"items": "oops"}).encode("utf-8")), [])

        rss_bytes = _rss_feed(
            "<item><title>First</title><pubDate>Mon, 01 Jun 2026 12:00:00 GMT</pubDate></item>",
            "<item><title>Second</title><updated>Mon, 08 Jun 2026 12:00:00 GMT</updated></item>",
        )
        rss_datetimes, rss_format = sc._xml_item_datetimes(rss_bytes)
        self.assertEqual(rss_format, "rss")
        self.assertEqual(len(rss_datetimes), 2)

        atom_bytes = _atom_feed(
            "<entry><title>Atom</title><updated>2026-06-02T12:00:00Z</updated></entry>",
        )
        atom_datetimes, atom_format = sc._xml_item_datetimes(atom_bytes)
        self.assertEqual(atom_format, "atom")
        self.assertEqual(len(atom_datetimes), 1)

        self.assertEqual(sc._item_datetimes(json_bytes, "reddit")[1], "json")
        fallback_datetimes, fallback_format = sc._item_datetimes(b"{\"items\": []}", "rss")
        self.assertEqual(fallback_format, "json")
        self.assertEqual(fallback_datetimes, [])
        self.assertEqual(sc._xml_feed_format(ElementTree.fromstring(b"<root />")), "xml")

        summary = sc._summarize_items(
            _rss_feed(
                "<item><title>Recent</title><pubDate>Mon, 23 Jun 2026 12:00:00 GMT</pubDate></item>",
                "<item><title>Old</title><pubDate>Mon, 01 Jun 2026 12:00:00 GMT</pubDate></item>",
                "<item><title>Undated</title></item>",
            ),
            "rss",
            recent_days=7,
            now_utc=datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["format"], "rss")
        self.assertEqual(summary["item_count"], 3)
        self.assertEqual(summary["recent_item_count"], 1)
        self.assertEqual(summary["undated_item_count"], 1)
        self.assertEqual(summary["newest_item_at"], "2026-06-23T12:00:00Z")

    def test_remaining_low_level_branches(self) -> None:
        with patch("news_pipeline.source_checks.load_source_rows", return_value=[{"key": "alpha"}]) as load_rows:
            self.assertEqual(sc._source_rows(Path("config/sources.yaml")), [{"key": "alpha"}])
        load_rows.assert_called_once_with(Path("config/sources.yaml"))

        with patch("news_pipeline.source_checks.range", new=lambda *_args: iter(())):
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                sc._fetch_url("https://example.com/feed", 5)

        json_children = json.dumps(
            {
                "data": {
                    "children": [
                        "skip",
                        {"data": {"title": "One", "description": "", "selftext": ""}},
                    ]
                }
            }
        ).encode("utf-8")
        self.assertEqual(sc._json_language_samples(json_children, 1), ["One"])

        json_items = json.dumps(
            {
                "items": [
                    "skip",
                    {"title": "Two", "summary": "", "content_text": "", "content_html": ""},
                ]
            }
        ).encode("utf-8")
        self.assertEqual(sc._json_language_samples(json_items, 1), ["Two"])

        xml_items = _rss_feed(
            "<item><title>First</title><description>Desc</description></item>",
            "<item><title>Second</title><description>Desc</description></item>",
        )
        self.assertEqual(sc._xml_language_samples(xml_items, 1), ["First Desc"])

        self.assertEqual(
            sc._xml_feed_article_urls(
                _rss_feed(
                    "<item><link href=\"https://example.com/a\"/></item>",
                    "<item><link href=\"https://example.com/b\"/></item>",
                ),
                1,
            ),
            ["https://example.com/a"],
        )
        self.assertEqual(sc._json_feed_article_urls(json.dumps({"data": {"children": "oops"}}).encode("utf-8"), 1), [])
        self.assertEqual(
            sc._json_feed_article_urls(
                json.dumps(
                    {
                        "data": {
                            "children": [
                                {"data": {"url": "https://example.com/a"}},
                                "skip",
                                {"data": {"url": "https://example.com/b"}},
                            ]
                        }
                    }
                ).encode("utf-8"),
                1,
            ),
            ["https://example.com/a"],
        )

    def test_language_sample_helpers_and_extract_fallback(self) -> None:
        self.assertEqual(sc._clean_sample_text("<p> Hello&nbsp;world </p>"), "Hello world")
        self.assertEqual(sc._local_xml_name("{ns}Tag"), "tag")

        encoded = b"<feed><title>caf\xe9</title><subtitle>  sub  </subtitle></feed>"
        root = sc._xml_root_from_content(encoded)
        self.assertEqual(sc._direct_child_text(root, {"title", "subtitle"}), "café sub")

        reddit_bytes = json.dumps(
            {
                "data": {
                    "children": [
                        {"data": {"title": "Hello", "description": "<p>World</p>", "selftext": ""}},
                    ]
                }
            }
        ).encode("utf-8")
        self.assertEqual(sc._json_language_samples(reddit_bytes, 2), ["Hello World"])

        items_bytes = json.dumps(
            {
                "items": [
                    {
                        "title": "Alpha",
                        "summary": "Beta",
                        "content_text": "Gamma",
                        "content_html": "<p>Delta</p>",
                    }
                ]
            }
        ).encode("utf-8")
        self.assertEqual(sc._json_language_samples(items_bytes, 2), ["Alpha Beta Gamma Delta"])

        rss_bytes = _rss_feed(
            "<item><title>RSS Title</title><description>RSS Desc</description></item>",
        )
        self.assertEqual(sc._xml_language_samples(rss_bytes, 2), ["RSS Title RSS Desc"])

        atom_bytes = _atom_feed(
            "<entry><title>Atom Title</title><summary>Atom Summary</summary></entry>",
        )
        self.assertEqual(sc._xml_language_samples(atom_bytes, 2), ["Atom Title Atom Summary"])

        fallback_bytes = b"<feed><title>Root Title</title><subtitle>Root Sub</subtitle></feed>"
        self.assertEqual(sc._xml_language_samples(fallback_bytes, 2), ["Root Title Root Sub"])

        malformed_xml = b"{\"items\": [{\"title\": \"Json title\", \"summary\": \"Json summary\"}]}"
        self.assertEqual(sc.extract_language_samples(malformed_xml, "rss", 2), ["Json title Json summary"])
        self.assertEqual(sc.extract_language_samples(reddit_bytes, "reddit", 2), ["Hello World"])

    def test_best_language_label_and_detect_language_from_samples(self) -> None:
        self.assertEqual(sc._best_language_label(None), ("", 0.0))
        self.assertEqual(sc._best_language_label([]), ("", 0.0))
        self.assertEqual(sc._best_language_label({"label": "EN", "score": "0.7"}), ("en", 0.7))
        self.assertEqual(
            sc._best_language_label([{"label": "fr", "score": 0.4}, {"label": "de", "score": 0.9}]),
            ("de", 0.9),
        )
        self.assertEqual(sc._best_language_label(["bad"]), ("", 0.0))
        self.assertEqual(sc._best_language_label({"label": "de", "score": "bad"}), ("de", 0.0))

        self.assertEqual(
            sc.detect_language_from_samples([], lambda *_a, **_k: None),
            {"language": None, "confidence": None, "scores": {}},
        )

        def dict_detector(samples: list[str], **_kwargs: object) -> dict[str, object]:
            return {"label": "es", "score": 0.8}

        def list_detector(samples: list[str], **_kwargs: object) -> list[dict[str, object]]:
            return [{"label": "fr", "score": 0.5}, {"label": "fr", "score": 0.3}]

        def low_confidence_detector(samples: list[str], **_kwargs: object) -> dict[str, object]:
            return {"label": "it", "score": 0.1}

        def bad_label_detector(samples: list[str], **_kwargs: object) -> list[dict[str, object]]:
            return [{"label": "", "score": 0.8}, {"label": None, "score": 0.2}]  # type: ignore[list-item]

        def one_arg_detector(samples: list[str]) -> dict[str, object]:
            return {"label": "pt", "score": 0.9}

        self.assertEqual(
            sc.detect_language_from_samples(["hola"], dict_detector),
            {"language": "es", "confidence": 0.8, "scores": {"es": 0.8}},
        )
        self.assertEqual(
            sc.detect_language_from_samples(["bonjour"], list_detector),
            {"language": "fr", "confidence": 0.4, "scores": {"fr": 0.8}},
        )
        low_confidence = sc.detect_language_from_samples(["ciao"], low_confidence_detector, min_confidence=0.5)
        self.assertEqual(low_confidence["language"], None)
        self.assertAlmostEqual(low_confidence["confidence"], 0.1)
        self.assertEqual(sc.detect_language_from_samples(["text"], bad_label_detector), {"language": None, "confidence": None, "scores": {}})
        self.assertEqual(
            sc.detect_language_from_samples(["ola"], one_arg_detector),
            {"language": "pt", "confidence": 0.9, "scores": {"pt": 0.9}},
        )

    def test_language_detector_loader_branches(self) -> None:
        fake_transformers = types.ModuleType("transformers")
        captured: dict[str, object] = {}

        def fake_pipeline(task: str, model: str, tokenizer: str) -> object:
            captured.update({"task": task, "model": model, "tokenizer": tokenizer})
            return {"pipeline": "ok"}

        fake_transformers.pipeline = fake_pipeline  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"transformers": fake_transformers}):
            detector = sc._load_language_detector("model-name")

        self.assertEqual(detector, {"pipeline": "ok"})
        self.assertEqual(
            captured,
            {
                "task": "text-classification",
                "model": "model-name",
                "tokenizer": "model-name",
            },
        )

        fake_transformers.pipeline = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("load failed"))  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"transformers": fake_transformers}):
            with self.assertRaisesRegex(RuntimeError, "Could not load language detection model"):
                sc._load_language_detector("model-name")

        original_import = builtins.__import__

        def fake_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] | list[str] = (),
            level: int = 0,
        ) -> object:
            if name == "transformers":
                raise ImportError("missing")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "Language detection requires the transformers package"):
                sc._load_language_detector("model-name")


class SourceChecksRuntimeTests(unittest.TestCase):
    def test_detect_source_language_covers_success_and_error_paths(self) -> None:
        source = _make_source("alpha", url="https://example.com/feed")
        detector = lambda samples, **_kwargs: {"label": "fr", "score": 0.9}

        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"body", 200)), patch(
            "news_pipeline.source_checks.extract_language_samples", return_value=["bonjour"]
        ):
            result = sc.detect_source_language(source, 5, detector)

        self.assertTrue(result["ok"])
        self.assertEqual(result["language"], "fr")
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["error"], None)

        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"body", 200)), patch(
            "news_pipeline.source_checks.extract_language_samples", return_value=[]
        ):
            result = sc.detect_source_language(source, 5, detector)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "No feed item text found.")

        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"body", 200)), patch(
            "news_pipeline.source_checks.extract_language_samples", return_value=["bonjour"]
        ), patch(
            "news_pipeline.source_checks.detect_language_from_samples",
            return_value={"language": None, "confidence": None, "scores": {}},
        ):
            result = sc.detect_source_language(source, 5, detector)
        self.assertEqual(result["error"], "Detector returned no language labels.")

        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"body", 200)), patch(
            "news_pipeline.source_checks.extract_language_samples", return_value=["bonjour"]
        ), patch(
            "news_pipeline.source_checks.detect_language_from_samples",
            return_value={"language": None, "confidence": 0.2, "scores": {"fr": 0.2}},
        ):
            result = sc.detect_source_language(source, 5, detector)
        self.assertEqual(result["error"], "Language confidence below 0.35.")

        for exc, expected in [
            (urllib.error.HTTPError("https://example.com/feed", 503, "Down", hdrs=None, fp=None), "HTTP 503 Down"),
            (urllib.error.URLError("offline"), "URLError: offline"),
            (TimeoutError(), "Timed out after 5s"),
            (Exception("boom"), "boom"),
        ]:
            with self.subTest(expected=expected):
                with patch("news_pipeline.source_checks._fetch_url", side_effect=exc):
                    result = sc.detect_source_language(source, 5, detector)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], expected)

    def test_feed_article_extractors_and_probe_article_body(self) -> None:
        rss_bytes = _rss_feed(
            "<item><link href=\"https://example.com/a\"/></item>",
            "<item><guid>https://example.com/b</guid></item>",
        )
        json_bytes = json.dumps(
            {"data": {"children": [{"data": {"url": "https://example.com/c"}}, {"data": {"url": "ftp://skip"}}]}}
        ).encode("utf-8")
        self.assertEqual(sc._xml_feed_article_urls(rss_bytes, 3), ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(sc._json_feed_article_urls(json_bytes, 3), ["https://example.com/c"])
        self.assertEqual(sc._extract_feed_article_urls(json_bytes, "reddit", 3), ["https://example.com/c"])
        self.assertEqual(sc._extract_feed_article_urls(json_bytes, "rss", 3), ["https://example.com/c"])
        self.assertEqual(
            sc._json_feed_article_urls(
                json.dumps({"data": {"children": ["skip", {"data": {"url": "https://example.com/c"}}]}}).encode("utf-8"),
                3,
            ),
            ["https://example.com/c"],
        )
        self.assertEqual(
            sc._json_feed_article_urls(
                json.dumps(
                    {
                        "data": {
                            "children": [
                                {"data": {"url": "https://example.com/c"}},
                                {"data": {"url": "https://example.com/d"}},
                            ]
                        }
                    }
                ).encode("utf-8"),
                1,
            ),
            ["https://example.com/c"],
        )

        google_url = "https://news.google.com/rss/articles/CBMi?url=https%3A%2F%2Fexample.com%2Farticle"
        fake_trafilatura = types.ModuleType("trafilatura")
        fake_trafilatura.extract = lambda text, url=None: " article body "  # type: ignore[attr-defined]

        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"<html>body</html>", 200)) as fetch_url, patch.dict(
            sc.sys.modules, {"trafilatura": fake_trafilatura}
        ):
            has_body, status = sc._probe_article_body(google_url, 9)

        self.assertTrue(has_body)
        self.assertEqual(status, "scraped")
        fetch_url.assert_called_once()
        self.assertEqual(fetch_url.call_args.args[0], "https://example.com/article")

        fake_trafilatura.extract = lambda text, url=None: "   "  # type: ignore[attr-defined]
        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"<html>body</html>", 200)), patch.dict(
            sc.sys.modules, {"trafilatura": fake_trafilatura}
        ):
            self.assertEqual(sc._probe_article_body("https://example.com/article", 9), (False, "no_text"))

        fake_trafilatura.extract = lambda text, url=None: (_ for _ in ()).throw(RuntimeError("bad"))  # type: ignore[attr-defined]
        with patch("news_pipeline.source_checks._fetch_url", return_value=(b"<html>body</html>", 200)), patch.dict(
            sc.sys.modules, {"trafilatura": fake_trafilatura}
        ):
            self.assertEqual(sc._probe_article_body("https://example.com/article", 9), (False, "trafilatura_error"))

        original_import = builtins.__import__

        def fake_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] | list[str] = (),
            level: int = 0,
        ) -> object:
            if name == "trafilatura":
                raise ImportError("missing")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import), patch(
            "news_pipeline.source_checks._fetch_url", return_value=(b"<html>body</html>", 200)
        ):
            self.assertEqual(sc._probe_article_body("https://example.com/article", 9), (False, "trafilatura_not_installed"))

        for exc, expected in [
            (urllib.error.HTTPError("https://example.com/article", 404, "Missing", hdrs=None, fp=None), "http_404"),
            (urllib.error.URLError("offline"), "url_error: offline"),
            (TimeoutError(), "timeout_9s"),
            (Exception("boom"), "error: boom"),
        ]:
            with self.subTest(expected=expected):
                with patch("news_pipeline.source_checks._fetch_url", side_effect=exc):
                    self.assertEqual(sc._probe_article_body("https://example.com/article", 9), (False, expected))

    def test_probe_source_covers_success_and_error_paths(self) -> None:
        source = _make_source("alpha", url="https://news.google.com/rss/search?q=foo", fetcher="rss")
        content = _rss_feed("<item><title>One</title></item>")
        summary = {
            "format": "rss",
            "item_count": 2,
            "recent_item_count": 1,
            "undated_item_count": 0,
            "newest_item_at": "2026-06-23T12:00:00Z",
        }

        with patch("news_pipeline.source_checks._fetch_url", return_value=(content, 200)), patch(
            "news_pipeline.source_checks._summarize_items", return_value=summary
        ), patch("news_pipeline.source_checks._extract_feed_article_urls", return_value=["https://a", "https://b"]), patch(
            "news_pipeline.source_checks._probe_article_body", side_effect=[(True, "scraped"), (False, "no_text")]
        ):
            result = sc.probe_source(source, 9, recent_days=7, probe_articles=2)

        self.assertTrue(result["ok"])
        self.assertIn("when%3A7d", result["probe_url"])
        self.assertEqual(result["article_probe_count"], 2)
        self.assertEqual(result["article_probe_successes"], 1)

        stale_cases = [
            (
                {
                    "format": "rss",
                    "item_count": 0,
                    "recent_item_count": 0,
                    "undated_item_count": 0,
                    "newest_item_at": None,
                },
                "Feed parsed but returned 0 items.",
            ),
            (
                {
                    "format": "rss",
                    "item_count": 2,
                    "recent_item_count": 0,
                    "undated_item_count": 0,
                    "newest_item_at": "2026-06-01T12:00:00Z",
                },
                "No feed items dated within last 7 day(s); newest is 2026-06-01T12:00:00Z.",
            ),
            (
                {
                    "format": "rss",
                    "item_count": 2,
                    "recent_item_count": 0,
                    "undated_item_count": 0,
                    "newest_item_at": None,
                },
                "No feed items dated within last 7 day(s).",
            ),
            (
                {
                    "format": "rss",
                    "item_count": 2,
                    "recent_item_count": 0,
                    "undated_item_count": 2,
                    "newest_item_at": None,
                },
                "Feed items had no parseable publish/update dates.",
            ),
        ]
        for item_summary, expected in stale_cases:
            with self.subTest(expected=expected):
                with patch("news_pipeline.source_checks._fetch_url", return_value=(content, 200)), patch(
                    "news_pipeline.source_checks._summarize_items", return_value=item_summary
                ):
                    result = sc.probe_source(source, 9, recent_days=7)
                self.assertTrue(result["stale"])
                self.assertEqual(result["error"], expected)

        for exc, expected_status, expected_error in [
            (urllib.error.HTTPError(source["url"], 503, "Down", hdrs=None, fp=None), 503, "HTTP 503 Down"),
            (urllib.error.URLError("offline"), None, "URLError: offline"),
            (TimeoutError(), None, "Timed out after 9s"),
            (Exception("boom"), None, "boom"),
        ]:
            with self.subTest(expected_error=expected_error):
                with patch("news_pipeline.source_checks._fetch_url", side_effect=exc):
                    result = sc.probe_source(source, 9, recent_days=7)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], expected_error)
                if expected_status is not None:
                    self.assertEqual(result["http_status"], expected_status)

    def test_wrappers_and_rendering_helpers(self) -> None:
        with patch("news_pipeline.source_checks.apply_source_catalog_patch") as patch_catalog:
            patch_catalog.return_value = SimpleNamespace(edit_count=2)
            written = sc.write_source_languages(
                Path("/tmp/sources.yaml"),
                [
                    {"key": "alpha", "ok": True, "language": "fr"},
                    {"key": "beta", "ok": False, "language": "de"},
                    {"key": "gamma", "ok": True, "language": None},
                    {"key": "delta", "ok": True, "language": "es", "skipped": True},
                ],
                overwrite=True,
            )

        self.assertEqual(written, 2)
        patch_catalog.assert_called_once_with(
            Path("/tmp/sources.yaml"),
            [SetSourceLanguages({"alpha": "fr"}, overwrite=True)],
        )

        with patch("news_pipeline.source_checks.apply_source_catalog_patch") as patch_catalog:
            patch_catalog.return_value = SimpleNamespace(edit_count=1)
            removed = sc.remove_source_blocks(Path("/tmp/sources.yaml"), {"alpha", "beta"})

        self.assertEqual(removed, 1)
        patch_catalog.assert_called_once_with(Path("/tmp/sources.yaml"), [DeleteSources({"alpha", "beta"})])

        self.assertEqual(sc._status({"ok": True, "stale": False, "http_status": None}), "OK")
        self.assertEqual(sc._status({"ok": False, "stale": True, "http_status": None}), "STALE")
        self.assertEqual(sc._status({"ok": False, "stale": False, "http_status": 500}), "HTTP 500")
        self.assertEqual(sc._status({"ok": False, "stale": False, "http_status": None}), "FAIL")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sc.print_table(
                [
                    {
                        "section": "sources",
                        "key": "alpha",
                        "name": "Alpha News",
                        "latency_ms": 10,
                        "item_count": 2,
                        "recent_item_count": 1,
                        "newest_item_at": "2026-06-23T12:00:00Z",
                        "format": "rss",
                        "article_probe_count": 2,
                        "article_probe_successes": 1,
                        "ok": True,
                        "stale": False,
                        "http_status": None,
                        "error": None,
                    },
                    {
                        "section": "sources",
                        "key": "beta",
                        "name": "Beta News",
                        "latency_ms": None,
                        "item_count": 0,
                        "recent_item_count": 0,
                        "newest_item_at": None,
                        "format": None,
                        "article_probe_count": None,
                        "article_probe_successes": None,
                        "ok": False,
                        "stale": True,
                        "http_status": None,
                        "error": "Feed parsed but returned 0 items.",
                    },
                ]
            )
        table_output = stdout.getvalue()
        self.assertIn("STATUS", table_output)
        self.assertIn("SCRAPE", table_output)
        self.assertIn("Alpha News", table_output)
        self.assertIn("Feed parsed but returned 0 items.", table_output)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sc.print_language_table(
                [
                    {
                        "key": "alpha",
                        "name": "Alpha News",
                        "language": "fr",
                        "confidence": 0.91,
                        "sample_count": 2,
                        "ok": True,
                        "skipped": False,
                        "error": None,
                    },
                    {
                        "key": "beta",
                        "name": "Beta News",
                        "language": None,
                        "confidence": None,
                        "sample_count": 0,
                        "ok": False,
                        "skipped": True,
                        "error": "language already set",
                    },
                ]
            )
        language_output = stdout.getvalue()
        self.assertIn("LANG", language_output)
        self.assertIn("SKIP", language_output)
        self.assertIn("Alpha News", language_output)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertIsNone(sc.print_table([]))
        self.assertEqual(stdout.getvalue(), "")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertIsNone(sc.print_language_table([]))
        self.assertEqual(stdout.getvalue(), "")


class SourceChecksCliTests(unittest.TestCase):
    def test_parser_run_options_and_validation_paths(self) -> None:
        parser = sc.build_parser()
        args = parser.parse_args(
            [
                "--sources-yaml",
                "config/sources.yaml",
                "--timeout",
                "17",
                "--concurrency",
                "3",
                "--recent-days",
                "5",
                "--prune-stale",
                "--probe-articles",
                "--prune-unscrapable",
                "--only-failures",
                "--detect-languages",
                "--write-languages",
                "--overwrite-languages",
                "--language-model",
                "model-x",
                "--language-samples",
                "4",
                "--min-language-confidence",
                "0.42",
                "--limit",
                "8",
                "--section",
                "sources",
                "--json",
            ]
        )

        self.assertEqual(args.sources_yaml, Path("config/sources.yaml"))
        self.assertEqual(args.timeout, 17)
        self.assertEqual(args.concurrency, 3)
        self.assertEqual(args.recent_days, 5)
        self.assertTrue(args.prune_inactive)
        self.assertTrue(args.probe_articles)
        self.assertTrue(args.prune_unscrapable)
        self.assertTrue(args.only_failures)
        self.assertTrue(args.detect_languages)
        self.assertTrue(args.write_languages)
        self.assertTrue(args.overwrite_languages)
        self.assertEqual(args.language_model, "model-x")
        self.assertEqual(args.language_samples, 4)
        self.assertEqual(args.min_language_confidence, 0.42)
        self.assertEqual(args.limit, 8)
        self.assertEqual(args.section, "sources")
        self.assertTrue(args.json_output)

        self.assertEqual(sc.main(["--sources-yaml", "missing.yaml"]), 2)

        with patch("news_pipeline.source_checks._source_rows", return_value=[]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sc.main(["--sources-yaml", "config/sources.yaml", "--recent-days", "0"]), 2)

        with patch("news_pipeline.source_checks._source_rows", return_value=[_make_source("alpha")]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    sc.main([
                        "--sources-yaml",
                        "config/sources.yaml",
                        "--detect-languages",
                        "--prune-inactive",
                    ]),
                    2,
                )

        with patch("news_pipeline.source_checks._source_rows", return_value=[_make_source("alpha")]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    sc.main([
                        "--sources-yaml",
                        "config/sources.yaml",
                        "--prune-unscrapable",
                    ]),
                    2,
                )

        with patch("news_pipeline.source_checks._source_rows", return_value=[_make_source("alpha", section="other")]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    sc.main([
                        "--sources-yaml",
                        "config/sources.yaml",
                        "--section",
                        "sources",
                    ]),
                    2,
                )

        with patch("news_pipeline.source_checks._source_rows", return_value=[_make_source("alpha")]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    sc.main([
                        "--sources-yaml",
                        "config/sources.yaml",
                        "--limit",
                        "0",
                    ]),
                    2,
                )

    def test_probe_and_language_detection_workers_and_main(self) -> None:
        sources = [_make_source("alpha"), _make_source("beta", language="fr")]
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        sources_yaml = Path(tempdir.name) / "sources.yaml"
        sources_yaml.write_text("")

        args = SimpleNamespace(
            limit=None,
            overwrite_languages=False,
            json_output=True,
            language_model="model-x",
            timeout=5,
            language_samples=3,
            min_language_confidence=0.35,
            sources_yaml=sources_yaml,
            write_languages=True,
        )
        detected_results = [
            {
                "key": "alpha",
                "name": "alpha",
                "section": "sources",
                "url": "https://example.com/feed",
                "ok": True,
                "skipped": False,
                "language": "es",
                "confidence": 0.9,
                "sample_count": 2,
                "latency_ms": 1,
                "error": None,
            }
        ]

        with patch("news_pipeline.source_checks._load_language_detector", return_value=object()) as load_detector, patch(
            "news_pipeline.source_checks.detect_source_language", return_value=detected_results[0]
        ) as detect_language, patch("news_pipeline.source_checks.write_source_languages", return_value=1) as write_languages, patch(
            "news_pipeline.source_checks.json.dumps", wraps=json.dumps
        ) as dumps:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc._run_language_detection(args, sources)

        self.assertEqual(exit_code, 0)
        load_detector.assert_called_once_with("model-x")
        detect_language.assert_called_once()
        write_languages.assert_called_once()
        self.assertIn('"written": 1', stdout.getvalue())
        dumps.assert_called()

        probe_results = [
            {
                "key": "alpha",
                "name": "alpha",
                "section": "sources",
                "url": "https://example.com/feed",
                "ok": False,
                "stale": True,
                "http_status": None,
                "latency_ms": 1,
                "format": "rss",
                "item_count": 2,
                "recent_item_count": 1,
                "undated_item_count": 0,
                "newest_item_at": "2026-06-23T12:00:00Z",
                "recent_days": 7,
                "error": "Feed parsed but returned 0 items.",
                "article_probe_count": 5,
                "article_probe_successes": 0,
            }
        ]
        probe_args = SimpleNamespace(
            sources_yaml=sources_yaml,
            timeout=5,
            concurrency=1,
            recent_days=7,
            prune_inactive=True,
            prune_unscrapable=True,
            probe_articles=True,
            only_failures=False,
            json_output=True,
            detect_languages=False,
            limit=None,
            section="all",
            language_model="model-x",
            language_samples=3,
            min_language_confidence=0.35,
            write_languages=False,
            overwrite_languages=False,
        )
        with patch("news_pipeline.source_checks._source_rows", return_value=sources), patch(
            "news_pipeline.source_checks.probe_source", side_effect=probe_results
        ) as probe_source, patch("news_pipeline.source_checks.remove_source_blocks", return_value=1) as remove_blocks, patch(
            "news_pipeline.source_checks._probe_sources", return_value=probe_results
        ) as probe_sources:
            with patch.object(argparse.ArgumentParser, "parse_args", return_value=probe_args):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = sc.main([])

        self.assertEqual(exit_code, 0)
        probe_sources.assert_called_once_with(
            sources,
            timeout=5,
            concurrency=1,
            recent_days=7,
            probe_articles=5,
        )
        self.assertEqual(remove_blocks.call_count, 2)
        self.assertTrue(all(call.args[1] == {"alpha"} for call in remove_blocks.call_args_list))
        self.assertIn('"results":', stdout.getvalue())

        with patch("news_pipeline.source_checks.probe_source", side_effect=lambda source, timeout, recent_days=7, probe_articles=0: {"key": source["key"]}), patch(
            "news_pipeline.source_checks._probe_sources", wraps=sc._probe_sources
        ):
            results = sc._probe_sources(sources, timeout=5, concurrency=1, recent_days=7)
        self.assertEqual([row["key"] for row in results], ["alpha", "beta"])

        with patch("news_pipeline.source_checks.probe_source", side_effect=lambda source, timeout, recent_days=7, probe_articles=0: {"key": source["key"]}) as probe_source:
            results = sc._probe_sources(sources, timeout=5, concurrency=2, recent_days=7)
        self.assertEqual([row["key"] for row in results], ["alpha", "beta"])
        self.assertEqual(probe_source.call_count, 2)

    def test_run_language_detection_and_main_summary_branches(self) -> None:
        sources = [_make_source("alpha"), _make_source("beta", language="fr")]
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        sources_yaml = Path(tempdir.name) / "sources.yaml"
        sources_yaml.write_text("")
        detector_result = {
            "key": "alpha",
            "name": "alpha",
            "section": "sources",
            "url": "https://example.com/feed",
            "ok": True,
            "skipped": False,
            "language": "es",
            "confidence": 0.9,
            "sample_count": 2,
            "latency_ms": 1,
            "error": None,
        }

        empty_args = SimpleNamespace(
            limit=None,
            overwrite_languages=False,
            json_output=False,
            language_model="model-x",
            timeout=5,
            language_samples=3,
            min_language_confidence=0.35,
            sources_yaml=sources_yaml,
            write_languages=False,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(sc._run_language_detection(empty_args, []), 2)
        self.assertIn("No sources to check.", stderr.getvalue())

        with patch("news_pipeline.source_checks._load_language_detector", side_effect=RuntimeError("load failed")):
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                self.assertEqual(sc._run_language_detection(empty_args, [_make_source("alpha")]), 2)
        self.assertIn("ERROR:", stderr.getvalue())

        non_json_args = SimpleNamespace(**{**empty_args.__dict__, "json_output": False, "write_languages": True})

        with patch("news_pipeline.source_checks._load_language_detector", return_value=object()) as load_detector, patch(
            "news_pipeline.source_checks.detect_source_language", return_value=detector_result
        ) as detect_language, patch("news_pipeline.source_checks.write_source_languages", return_value=1) as write_languages:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc._run_language_detection(non_json_args, sources)
        self.assertEqual(exit_code, 0)
        load_detector.assert_called_once_with("model-x")
        detect_language.assert_called_once()
        write_languages.assert_called_once()
        self.assertIn("Loading language detector", stdout.getvalue())
        self.assertIn("1 skipped", stdout.getvalue())
        self.assertIn("1 detected", stdout.getvalue())

        limited_args = SimpleNamespace(**{**empty_args.__dict__, "limit": 1, "json_output": False})
        with patch("news_pipeline.source_checks._load_language_detector", return_value=object()) as load_detector, patch(
            "news_pipeline.source_checks.detect_source_language", return_value=detector_result
        ) as detect_language:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc._run_language_detection(limited_args, sources)
        self.assertEqual(exit_code, 0)
        self.assertEqual(detect_language.call_count, 1)
        self.assertIn("Detecting languages for 1 source(s)", stdout.getvalue())

        failed_args = SimpleNamespace(**{**empty_args.__dict__, "json_output": False, "write_languages": False})
        failed_result = dict(detector_result)
        failed_result.update({"ok": False, "language": None, "confidence": None, "error": "Detector returned no language labels."})
        with patch("news_pipeline.source_checks._load_language_detector", return_value=object()), patch(
            "news_pipeline.source_checks.detect_source_language", return_value=failed_result
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc._run_language_detection(failed_args, [_make_source("alpha")])
        self.assertEqual(exit_code, 1)
        self.assertIn("1 failed", stdout.getvalue())

        detect_args = SimpleNamespace(
            sources_yaml=sources_yaml,
            timeout=5,
            concurrency=1,
            recent_days=7,
            prune_inactive=False,
            prune_unscrapable=False,
            probe_articles=False,
            only_failures=False,
            json_output=True,
            detect_languages=True,
            limit=None,
            section="all",
            language_model="model-x",
            language_samples=3,
            min_language_confidence=0.35,
            write_languages=True,
            overwrite_languages=False,
        )
        with patch("news_pipeline.source_checks._source_rows", return_value=sources), patch(
            "news_pipeline.source_checks._load_language_detector", return_value=object()
        ), patch("news_pipeline.source_checks.detect_source_language", return_value=detector_result), patch(
            "news_pipeline.source_checks.write_source_languages", return_value=1
        ) as write_languages, patch.object(argparse.ArgumentParser, "parse_args", return_value=detect_args):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc.main([])
        self.assertEqual(exit_code, 0)
        write_languages.assert_called_once()
        self.assertIn('"results":', stdout.getvalue())

        probe_results = [
            {
                "key": "alpha",
                "name": "alpha",
                "section": "sources",
                "url": "https://example.com/feed",
                "ok": True,
                "stale": False,
                "http_status": None,
                "latency_ms": 1,
                "format": "rss",
                "item_count": 2,
                "recent_item_count": 1,
                "undated_item_count": 0,
                "newest_item_at": "2026-06-23T12:00:00Z",
                "recent_days": 7,
                "error": None,
                "article_probe_count": 5,
                "article_probe_successes": 1,
            },
            {
                "key": "beta",
                "name": "beta",
                "section": "sources",
                "url": "https://example.com/other",
                "ok": False,
                "stale": False,
                "http_status": 500,
                "latency_ms": 2,
                "format": "rss",
                "item_count": 0,
                "recent_item_count": 0,
                "undated_item_count": 0,
                "newest_item_at": None,
                "recent_days": 7,
                "error": "HTTP 500 Down",
                "article_probe_count": 5,
                "article_probe_successes": 0,
            },
        ]
        probe_args = SimpleNamespace(
            sources_yaml=sources_yaml,
            timeout=5,
            concurrency=1,
            recent_days=7,
            prune_inactive=False,
            prune_unscrapable=False,
            probe_articles=True,
            only_failures=True,
            json_output=False,
            detect_languages=False,
            limit=None,
            section="all",
            language_model="model-x",
            language_samples=3,
            min_language_confidence=0.35,
            write_languages=False,
            overwrite_languages=False,
        )
        with patch("news_pipeline.source_checks._source_rows", return_value=sources), patch(
            "news_pipeline.source_checks._probe_sources", return_value=probe_results
        ), patch.object(argparse.ArgumentParser, "parse_args", return_value=probe_args):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc.main([])
        self.assertEqual(exit_code, 1)
        probe_output = stdout.getvalue()
        self.assertIn("Checking 2 source(s)", probe_output)
        self.assertIn("SCRAPE", probe_output)
        self.assertIn("1/2 sources active", probe_output)
        self.assertIn("1 failed", probe_output)
        self.assertIn("1 unscrapable", probe_output)

        all_ok_probe_args = SimpleNamespace(
            sources_yaml=sources_yaml,
            timeout=5,
            concurrency=1,
            recent_days=7,
            prune_inactive=False,
            prune_unscrapable=False,
            probe_articles=True,
            only_failures=True,
            json_output=False,
            detect_languages=False,
            limit=None,
            section="all",
            language_model="model-x",
            language_samples=3,
            min_language_confidence=0.35,
            write_languages=False,
            overwrite_languages=False,
        )
        all_ok_results = [
            {
                "key": "alpha",
                "name": "alpha",
                "section": "sources",
                "url": "https://example.com/feed",
                "ok": True,
                "stale": False,
                "http_status": None,
                "latency_ms": 1,
                "format": "rss",
                "item_count": 2,
                "recent_item_count": 1,
                "undated_item_count": 0,
                "newest_item_at": "2026-06-23T12:00:00Z",
                "recent_days": 7,
                "error": None,
                "article_probe_count": 5,
                "article_probe_successes": 5,
            }
        ]
        with patch("news_pipeline.source_checks._source_rows", return_value=sources), patch(
            "news_pipeline.source_checks._probe_sources", return_value=all_ok_results
        ), patch.object(argparse.ArgumentParser, "parse_args", return_value=all_ok_probe_args):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc.main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("All sources passed.", stdout.getvalue())

        prune_probe_args = SimpleNamespace(**{**all_ok_probe_args.__dict__, "only_failures": False, "prune_inactive": True, "prune_unscrapable": True})
        prune_results = [
            {
                "key": "alpha",
                "name": "alpha",
                "section": "sources",
                "url": "https://example.com/feed",
                "ok": False,
                "stale": True,
                "http_status": None,
                "latency_ms": 1,
                "format": "rss",
                "item_count": 0,
                "recent_item_count": 0,
                "undated_item_count": 0,
                "newest_item_at": None,
                "recent_days": 7,
                "error": "Feed parsed but returned 0 items.",
                "article_probe_count": 5,
                "article_probe_successes": 0,
            }
        ]
        with patch("news_pipeline.source_checks._source_rows", return_value=sources), patch(
            "news_pipeline.source_checks._probe_sources", return_value=prune_results
        ), patch("news_pipeline.source_checks.remove_source_blocks", return_value=1) as remove_blocks, patch.object(
            argparse.ArgumentParser, "parse_args", return_value=prune_probe_args
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = sc.main([])
        self.assertEqual(exit_code, 0)
        self.assertEqual(remove_blocks.call_count, 2)
        self.assertIn("pruned (inactive)", stdout.getvalue())
        self.assertIn("pruned (unscrapable)", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
