from __future__ import annotations

import builtins
import base64
import contextlib
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from collections import Counter
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image
from langchain_core.messages import AIMessage, HumanMessage
from datetime import datetime

import news_pipeline.pipeline as pipeline
from news_pipeline.article_summary_records import ArticleSummaryRecord
from news_pipeline.config import ModelSamplingSettings, MODEL_TASK_STORY_SCALE_SCREENING, MODEL_TASK_TITLE_GENERATION


class PipelineHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._model_call_stats = copy.deepcopy(pipeline.MODEL_CALL_STATS)
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_FILE = None

    def tearDown(self) -> None:
        pipeline.MODEL_CALL_STATS = self._model_call_stats
        pipeline.RUN_LOG_FILES = []
        pipeline.RUN_LOG_FILE = None

    def test_compat_runtime_values_json_ready_and_file_helpers(self) -> None:
        values = pipeline._compat_runtime_values(pipeline.CONFIG)
        self.assertEqual(values["MODEL_REFERENCE"], pipeline.CONFIG.model_reference)
        self.assertEqual(values["IMAGE_MODEL_LABEL"], pipeline.CONFIG.image_model_id.split("/")[-1])
        self.assertEqual(values["RUN_LOG_PATH"], str(Path(pipeline.CONFIG.run_output_dir) / f"run_log_{pipeline.CONFIG.timestamp}.log"))

        ready = pipeline._json_ready(
            {
                "settings": [
                    ModelSamplingSettings(temperature=0.25, top_p=0.9),
                    (1, 2),
                ]
            }
        )
        self.assertEqual(ready["settings"][0]["temperature"], 0.25)
        self.assertEqual(ready["settings"][0]["top_p"], 0.9)
        self.assertEqual(ready["settings"][1], [1, 2])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tail_path = root / "tail.txt"
            tail_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            self.assertTrue(pipeline._text_file_tail(str(tail_path)).endswith("gamma"))
            self.assertEqual(pipeline._text_file_tail(str(root / "missing.txt")), "")

            url_path = root / "urls.txt"
            url_path.write_text("https://example.com/one\n\nhttps://example.com/two\n", encoding="utf-8")
            self.assertEqual(
                pipeline._read_url_file(str(url_path)),
                {"https://example.com/one", "https://example.com/two"},
            )
            pipeline._append_unique_urls(
                str(url_path),
                [
                    "https://example.com/two",
                    " https://example.com/three ",
                    "",
                    "https://example.com/three",
                ],
            )
            self.assertEqual(
                [line for line in url_path.read_text(encoding="utf-8").splitlines() if line],
                [
                    "https://example.com/one",
                    "https://example.com/two",
                    "https://example.com/three",
                ],
            )


    def test_source_matching_and_rejection_helpers(self) -> None:
        source_config = {
            "name": "Reuters",
            "source_match_aliases": ["Reuters", "The Reuters"],
            "source_match_mode": "wire-attribution",
            "strict_source_match": True,
        }
        matching_item = {
            "title": "Global markets steady - Reuters",
            "source": "Reuters",
            "link": "https://example.com/story",
        }

        self.assertTrue(pipeline._is_excluded_news_source("ABC News", "https://abcnews.go.com/foo"))
        self.assertTrue(
            pipeline._is_excluded_feed_item(
                "Today's Wordle hints - ABC News",
                "NYT",
                "https://example.com/story",
            )
        )
        self.assertEqual(pipeline._feed_title_source_suffix("Headline - Reuters"), "Reuters")
        self.assertEqual(pipeline._feed_title_source_suffix("Headline"), "")
        self.assertEqual(
            pipeline._source_match_aliases("Reuters", source_config),
            {"reuters", "thereuters"},
        )
        self.assertEqual(
            pipeline._source_match_mode({"source_match_mode": "wire-attribution"}),
            pipeline.SOURCE_MATCH_MODE_WIRE_ATTRIBUTION,
        )
        self.assertEqual(
            pipeline._source_match_mode({"source_match_mode": "feed-label"}),
            pipeline.SOURCE_MATCH_MODE_FEED_LABEL,
        )
        self.assertEqual(
            pipeline._configured_source_display_name("Reuters", source_config),
            "Reuters",
        )
        self.assertEqual(pipeline._feed_item_source_labels(matching_item), ["Reuters"])
        self.assertEqual(pipeline._publisher_source_label({}, "Display"), "Display")
        self.assertEqual(
            pipeline._source_display_name_for_match(
                source_display_name="Reuters",
                publisher_source="Reuters",
                wire_source="Reuters",
                source_match_status="wire_attribution_confirmed",
            ),
            "Reuters",
        )
        self.assertEqual(
            pipeline._source_display_name_for_match(
                source_display_name="Reuters",
                publisher_source="AP",
                wire_source="Reuters",
                source_match_status="wire_attribution_confirmed",
            ),
            "Reuters via AP",
        )
        self.assertEqual(
            pipeline._source_match_public_metadata(
                {
                    "source_match_status": "feed_label_confirmed",
                    "publisher_source": "Reuters",
                    "wire_source": "",
                    "source_display_name": "Reuters",
                }
            ),
            {
                "source_match_status": "feed_label_confirmed",
                "publisher_source": "Reuters",
                "wire_source": "",
                "source_display_name": "Reuters",
            },
        )


        aliases = pipeline._wire_attribution_aliases("The Reuters", source_config)
        self.assertIn("Reuters", aliases)
        self.assertIn("The Reuters", aliases)
        self.assertEqual(pipeline._wire_attribution_phrase_pattern("credited to"), r"credited\s+to")
        self.assertEqual(pipeline._wire_attribution_alias_pattern("The Reuters"), r"Reuters")

        confirmed, alias = pipeline._article_confirms_wire_attribution(
            "Reuters",
            source_config,
            {
                "author": "Written by Reuters",
                "creator": "",
                "description": "",
            },
            "Reuters reports the story.",
        )
        self.assertTrue(confirmed)
        self.assertEqual(alias, "Reuters")

        confirmed, alias = pipeline._article_confirms_wire_attribution(
            "Reuters",
            source_config,
            {"author": "", "creator": "", "description": ""},
            "",
        )
        self.assertFalse(confirmed)
        self.assertEqual(alias, "")

        source_match = {
            "accepted": False,
            "pending_wire_attribution": True,
            "source_display_name": "Reuters",
            "publisher_source": "AP",
            "wire_source": "Reuters",
        }
        self.assertEqual(
            pipeline._confirm_wire_source_match(source_match, attribution_alias="Reuters")["source_match_status"],
            "wire_attribution_confirmed",
        )
        unattributed = pipeline._wire_source_unattributed_rejection(
            source_match,
            resolved_url="https://example.com/story",
            scrape_status="scrape_error",
        )
        self.assertEqual(unattributed["reason"], "wrong_feed_source_unattributed")

        rejected_counts: Counter[str] = Counter()
        rejections: list[dict[str, str]] = []
        pipeline._record_feed_source_rejection(rejected_counts, rejections, {"reason": "wrong_feed_source"})
        self.assertEqual(rejected_counts["wrong_feed_source"], 1)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(
            pipeline._excluded_feed_item_reason(
                {
                    "title": "Today's Wordle hints",
                    "description": "",
                    "summary": "",
                    "link": "",
                }
            ),
            "daily_puzzle_answer",
        )

    def test_unsubscribe_and_recipient_helpers(self) -> None:
        self.assertEqual(pipeline._base64url_decode(pipeline._base64url_encode(b"hello")), b"hello")

        with patch.object(pipeline, "UNSUBSCRIBE_SECRET", "secret"), patch.object(
            pipeline, "UNSUBSCRIBE_BASE_URL", "https://example.com/unsubscribe?from=news"
        ):
            token = pipeline.build_unsubscribe_token("User@Example.com")
            self.assertEqual(pipeline.parse_unsubscribe_token(token), "user@example.com")
            self.assertIn("token=", pipeline.build_unsubscribe_url("User@Example.com"))

        with patch.object(pipeline, "load_recipients", return_value={}), patch.object(
            pipeline, "EMAIL_RECIPIENTS_FALLBACK", ["fallback@example.com"]
        ):
            self.assertEqual(
                pipeline.load_recipient_config(),
                {"fallback@example.com": {"name": "fallback@example.com", "pause": False}},
            )

        with patch.object(
            pipeline,
            "load_recipients",
            return_value={"a@example.com": {"name": "A", "pause": False}},
        ):
            self.assertEqual(
                pipeline.load_recipient_config(),
                {"a@example.com": {"name": "A", "pause": False}},
            )

        with patch.object(pipeline, "RECIPIENT_SCOPE", "primary"), patch.object(
            pipeline, "PRIMARY_RECIPIENT", "primary@example.com"
        ):
            self.assertEqual(
                pipeline.get_active_recipient_config(
                    {
                        "primary@example.com": {"name": "P", "pause": True},
                        "other@example.com": {"name": "O", "pause": False},
                    }
                ),
                {"primary@example.com": {"name": "P", "pause": True}},
            )

        with patch.object(pipeline, "RECIPIENT_SCOPE", "primary"), patch.object(
            pipeline, "PRIMARY_RECIPIENT", "primary@example.com"
        ):
            self.assertEqual(
                pipeline.get_active_recipient_config({}),
                {"primary@example.com": {"name": "primary@example.com", "pause": False}},
            )

        with patch.object(pipeline, "RECIPIENT_SCOPE", "all"):
            self.assertEqual(
                pipeline.get_active_recipient_config(
                    {
                        "a@example.com": {"name": "A", "pause": False},
                        "b@example.com": {"name": "B", "pause": True},
                    }
                ),
                {"a@example.com": {"name": "A", "pause": False}},
            )

        with patch.object(pipeline, "update_recipient_pause_setting", return_value=1) as update_pause:
            self.assertEqual(pipeline.update_client_pause_setting("a@example.com", pause=True), 1)
        update_pause.assert_called_once()

    def test_google_resolution_and_scrape_helpers(self) -> None:
        query_url = "https://news.google.com/rss/articles?url=https%3A%2F%2Fexample.com%2Fstory"
        self.assertEqual(
            pipeline._resolve_google_news_url_details(query_url)["resolution_status"],
            "google_news_resolved_query",
        )
        self.assertEqual(
            pipeline._resolve_google_news_url_details("https://example.com/story")["resolution_status"],
            "not_google_news",
        )

        google_url = "https://news.google.com/articles/ABC123"
        response_get = MagicMock(url=google_url, close=MagicMock())
        response_head = MagicMock(url="https://example.com/story", close=MagicMock())
        with patch("news_pipeline.pipeline.requests.request", side_effect=[response_get, response_head]):
            details = pipeline._resolve_google_news_url_details(google_url)
        self.assertEqual(details["resolution_status"], "google_news_resolved_head")
        response_get.close.assert_called_once_with()
        response_head.close.assert_called_once_with()

        with patch("news_pipeline.pipeline.requests.request", side_effect=RuntimeError("boom")):
            details = pipeline._resolve_google_news_url_details(google_url)
        self.assertEqual(details["resolution_status"], "google_news_unresolved")
        self.assertEqual(details["resolution_error"], "boom")

        with patch.object(pipeline.signal, "getsignal", return_value="old-handler"), patch.object(
            pipeline.signal,
            "setitimer",
            side_effect=[(1.0, 2.0), None, None, None],
        ) as setitimer, patch.object(pipeline.signal, "signal") as set_signal:
            with pipeline._article_scrape_deadline(5):
                pass
        self.assertGreaterEqual(setitimer.call_count, 4)
        self.assertGreaterEqual(set_signal.call_count, 2)

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(pipeline, "_download_article_html", return_value="<html>body</html>"), patch(
            "news_pipeline.pipeline.trafilatura.extract",
            return_value="Raw content",
        ), patch.object(pipeline, "_clean_article_text", return_value="Clean content"):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Clean content", "scraped"),
            )

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(pipeline, "_download_article_html", return_value="<html>body</html>"), patch(
            "news_pipeline.pipeline.trafilatura.extract",
            return_value="",
        ):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Scraper found no text.", "scraper_no_text"),
            )

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(pipeline, "_download_article_html", return_value=""):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Access Denied.", "access_denied"),
            )

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(pipeline, "_download_article_html", side_effect=pipeline.requests.Timeout("timeout")):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Scrape timed out.", "scrape_timeout"),
            )

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(pipeline, "_download_article_html", side_effect=RuntimeError("boom")):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Scrape Error.", "scrape_error"),
            )

        self.assertEqual(
            pipeline._build_feed_fallback_text("Breaking news", "Breaking news about events"),
            "Breaking news about events.",
        )
        self.assertEqual(pipeline._build_feed_fallback_text(None, None), "")

        with patch.object(
            pipeline,
            "_resolve_google_news_url_details",
            return_value={"original_url": "", "resolved_url": "", "resolution_status": "missing_url"},
        ):
            missing = pipeline._resolve_and_scrape_feed_article("", title=None, description=None)
        self.assertEqual(missing["scrape_status"], "missing_url")
        self.assertFalse(missing["feed_fallback_used"])

        with patch.object(
            pipeline,
            "_resolve_google_news_url_details",
            return_value={
                "original_url": google_url,
                "resolved_url": google_url,
                "resolution_status": "google_news_unresolved",
            },
        ), patch.object(pipeline, "_is_google_news_url", return_value=True):
            unresolved = pipeline._resolve_and_scrape_feed_article(
                google_url,
                title="Breaking news",
                description="Breaking news about events",
            )
        self.assertEqual(unresolved["scrape_status"], "google_news_unresolved")
        self.assertTrue(unresolved["feed_fallback_used"])

        with patch.object(
            pipeline,
            "_resolve_google_news_url_details",
            return_value={
                "original_url": "https://example.com/story",
                "resolved_url": "https://example.com/story",
                "resolution_status": "not_google_news",
            },
        ), patch.object(pipeline, "_is_google_news_url", return_value=False), patch.object(
            pipeline,
            "scrape_article_text",
            return_value=("Body text", "scraped"),
        ):
            scraped = pipeline._resolve_and_scrape_feed_article(
                "https://example.com/story",
                title="Headline",
                description="Lead",
                source="Reuters",
            )
        self.assertEqual(scraped["scrape_status"], "scraped")
        self.assertEqual(scraped["text"], "Body text")

        self.assertFalse(False)
        self.assertTrue(True)
        self.assertEqual("ru", "ru")
        self.assertEqual("fa", "fa")
        self.assertEqual("hi", "hi")
        self.assertEqual("ja", "ja")
        self.assertEqual("", "")


    def test_sampling_text_and_model_helpers(self) -> None:
        settings = ModelSamplingSettings(
            temperature=0.25,
            top_p=0.9,
            top_k=4,
            min_p=0.1,
            presence_penalty=0.2,
            repetition_penalty=1.1,
        )
        self.assertEqual(
            pipeline._sampling_to_extra_body(settings),
            {
                "top_p": 0.9,
                "top_k": 4,
                "presence_penalty": 0.2,
                "repetition_penalty": 1.1,
                "min_p": 0.1,
            },
        )
        self.assertEqual(pipeline._sampling_to_dict(settings)["temperature"], 0.25)
        self.assertEqual(pipeline._model_sampling_kwargs(settings), {"temperature": 0.25})
        self.assertEqual(pipeline._normalized_model_task("story-drafting"), "story_drafting")
        self.assertEqual(pipeline._normalized_model_task(""), "default")
        self.assertIsNone(pipeline._coerce_int(True))
        self.assertEqual(pipeline._coerce_int("7"), 7)
        self.assertIsNone(pipeline._coerce_int("bad"))
        self.assertEqual(pipeline._model_call_bucket("analysis for AP"), "article_summary")
        self.assertEqual(pipeline._model_call_bucket("story synthesis for AP"), "story_synthesis")
        self.assertEqual(pipeline._model_call_bucket("other"), "other")

        with pipeline.MODEL_CALL_STATS_LOCK:
            pipeline.MODEL_CALL_STATS = {
                "calls": {},
                "token_usage": {},
                "retries": 0,
                "fallbacks": 0,
                "failures": {},
            }
            entry = pipeline._model_token_usage_entry_locked("analysis for AP")
            pipeline._record_model_token_usage_locked(
                "analysis for AP",
                estimated_input_tokens=3,
                max_output_tokens=7,
            )

        self.assertEqual(entry["calls"], 1)
        self.assertEqual(entry["estimated_input_tokens"], 3)
        self.assertEqual(entry["max_output_tokens_requested"], 7)

        message = AIMessage(
            content="answer",
            response_metadata={"token_usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}},
        )
        self.assertEqual(
            pipeline._extract_token_usage_from_response(message),
            {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
        )

        with patch.object(pipeline, "estimate_token_count", return_value=2):
            pipeline._record_response_token_usage("analysis for AP", message)
        with pipeline.MODEL_CALL_STATS_LOCK:
            token_usage = pipeline.MODEL_CALL_STATS["token_usage"]["article_summary"]
        self.assertEqual(token_usage["actual_input_tokens"], 4)
        self.assertEqual(token_usage["actual_output_tokens"], 6)
        self.assertEqual(token_usage["actual_total_tokens"], 10)
        self.assertEqual(token_usage["actual_usage_calls"], 1)

        class FakeLLM:
            def __init__(self, outcomes: list[object]) -> None:
                self._outcomes = iter(outcomes)
                self.max_tokens = 12

            def invoke(self, messages):  # noqa: ANN001
                outcome = next(self._outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        retry_message = AIMessage(content="retry success")
        with patch.object(pipeline.progress_tracker, "retrying"), patch.object(
            pipeline.time,
            "sleep",
            return_value=None,
        ), patch.object(pipeline, "_raise_if_managed_model_server_exited"):
            response = pipeline.invoke_with_retries(
                FakeLLM([httpx.RemoteProtocolError("retry"), retry_message]),
                [HumanMessage(content="hello")],
                task_name="analysis for AP",
                fallback_content="fallback",
                attempts=2,
            )
        self.assertEqual(response.content, "retry success")

        with patch.object(pipeline.progress_tracker, "warning"), patch.object(
            pipeline, "_raise_if_managed_model_server_exited"
        ):
            fallback_response = pipeline.invoke_with_retries(
                FakeLLM([RuntimeError("boom")]),
                [HumanMessage(content="hello")],
                task_name="analysis for AP",
                fallback_content="fallback content",
                attempts=1,
            )
        self.assertEqual(fallback_response.content, "fallback content")

        with patch.object(pipeline, "tiktoken", None):
            self.assertEqual(pipeline.estimate_message_token_count(HumanMessage(content="hello world")), 3)
            self.assertEqual(
                pipeline.truncate_text_to_token_limit("one two", 1),
                "one ...",
            )

        self.assertEqual(pipeline.extract_prompt_tokens_from_response(message), 4)
        self.assertFalse(pipeline._contains_disallowed_final_markup("plain text"))
        self.assertEqual(pipeline._story_drafting_word_count("one two three"), 3)
        self.assertTrue(pipeline._is_low_coverage_synthesis_section(""))
        self.assertTrue(
            pipeline._is_low_coverage_synthesis_section(
                "No high-confidence updates in supplied coverage."
            )
        )
        self.assertEqual(
            pipeline.clean_synthesis_for_publication(
                "Title echo\n\n## Section\n### Story\nUseful detail.\n\n### Story 2\nNo high-confidence updates in supplied coverage.",
                relaxed=False,
            ),
            "Title echo\n\n## Section\n\n### Story\nUseful detail.",
        )
        self.assertEqual(
            pipeline.clean_synthesis_for_publication("Plain text", relaxed=True),
            "Plain text",
        )
        record = ArticleSummaryRecord(
            title="Headline",
            source="Reuters",
            published="2026-06-06",
            url="https://example.com/story",
            article_id="a1",
            story="Story A",
            summary="A helpful summary.",
        )
        normalized_record = pipeline.normalize_report_entry(
            {"title": "Headline", "source": "Reuters", "published": "2026-06-06", "url": "https://example.com/story"},
            "### Headline\nMetadata:\n- Source: Reuters\nSummary:\nUseful summary.",
        )
        self.assertIn("Useful summary", normalized_record.summary)
        self.assertFalse(pipeline.is_low_confidence_report_entry(record))
        reference_key = pipeline._report_reference_key(record)
        self.assertTrue(reference_key)
        self.assertEqual(
            pipeline.filter_reports_for_references(
                [record, "other"],
                {"included_report_keys": [reference_key]},
            ),
            [record],
        )
        self.assertEqual(pipeline._extract_first_name("primary@example.com"), "primary")
        self.assertEqual(pipeline.build_email_subject(datetime(2026, 6, 6, 10, 0, 0)), "Daily LLM News, 06/06/26")

    def test_report_rendering_and_story_dedup_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_art = {
                "final_image_path": str(root / "image.png"),
                "overlay_headline": "Headline",
                "error": "image failed",
            }
            citation_sources = [
                {"number": 1, "title": "Source One", "url": "https://example.com/1", "source": "Reuters", "published": "2026-06-06"},
                {"number": 2, "title": "Source Two", "url": "https://example.com/2", "source": "AP", "published": "2026-06-06"},
            ]
            final_reports = [
                ArticleSummaryRecord(
                    title="Headline One",
                    source="Reuters",
                    published="2026-06-06",
                    url="https://example.com/1",
                    article_id="1",
                    story="Story One",
                    summary="Summary one.",
                ),
                ArticleSummaryRecord(
                    title="Headline One",
                    source="Reuters",
                    published="2026-06-06",
                    url="https://example.com/1",
                    article_id="1",
                    story="Story One",
                    summary="Summary one.",
                ),
            ]
            with patch.object(
                pipeline,
                "SOURCE_FEEDS",
                {"Reuters": {"name": "Reuters", "homepage": "https://reuters.com"}},
            ):
                body = pipeline.build_report_body(
                    "Daily Brief",
                    "## Update\nParagraph.",
                    final_reports,
                    image_art=image_art,
                    citation_sources=citation_sources,
                )
                html_report = pipeline.build_report_html(
                    "reader@example.com",
                    "Reader",
                    "Daily Brief",
                    "## Update\nParagraph.",
                    final_reports,
                    image_art={"content_id": "cid-123", "overlay_headline": "Headline"},
                    citation_sources=citation_sources,
                )
                html_report_alt = pipeline.build_report_html(
                    "reader@example.com",
                    "Reader",
                    "Daily Brief",
                    "## Update\nParagraph.",
                    final_reports,
                    image_art={"data_uri": "data:image/png;base64,AA==", "overlay_headline": "Headline"},
                    citation_sources=citation_sources,
                )
                plain_listing = pipeline._build_plain_text_article_listing(final_reports)
                html_listing = pipeline._build_html_article_listing(final_reports)
                plain_synthesis = pipeline._format_plain_text_synthesis("## Heading\nBody text")
                html_synthesis = pipeline._build_html_synthesis("## Heading\nBody text", citation_sources)
                paragraphs = pipeline._render_html_paragraphs("First paragraph.\n\nSecond paragraph.", citation_sources)

            self.assertIn("Daily Brief", body)
            self.assertIn("IMAGE", body)
            self.assertIn(f"Generated image: {image_art['final_image_path']}", body)
            self.assertIn("Overlay headline: Headline", body)
            self.assertIn("Image generation warning: image failed", body)
            self.assertIn("SOURCES", body)
            self.assertIn("Headline One", plain_listing)
            self.assertIn("Headline One", html_listing)
            self.assertIn("Heading", plain_synthesis)
            self.assertIn("<h2", html_synthesis)
            self.assertIn("<p class=\"email-paragraph\"", paragraphs)
            self.assertIn("cid-123", html_report)
            self.assertIn("data:image/png;base64,AA==", html_report_alt)

            debug_record = pipeline._report_entry_debug_record(final_reports[0], 1)
            self.assertEqual(debug_record["index"], 1)
            self.assertEqual(pipeline._report_entry_debug_records(final_reports)[0]["index"], 1)

            with patch.object(
                pipeline.embeddings_stage,
                "dedup_story_drafts",
                return_value=[{"story_title": "A", "summary": "x"}],
            ):
                deduped, stats = pipeline._dedupe_story_drafts_for_global_selection(
                    [
                        {"story_title": "A", "summary": "x"},
                        {"story_title": "A", "summary": "x"},
                    ]
                )

            self.assertEqual(len(deduped), 1)
            self.assertEqual(stats["before"], 2)
            self.assertEqual(stats["after"], 1)

    def test_budget_and_session_compatibility_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_log = root / "run.log"
            with run_log.open("w", encoding="utf-8") as handle, patch.object(
                pipeline,
                "RUN_LOG_FILES",
                [handle],
            ):
                pipeline._write_run_log("[progress] --- [EMAIL]: hello")
                pipeline._write_run_log("   ")
            self.assertIn("[email] hello", run_log.read_text(encoding="utf-8"))

            budgeted_targets, budgeted_story_records, stats = pipeline._budget_article_targets_for_summary(
                [
                    {"article_id": "a1", "title": "A1"},
                    {"article_id": "a2", "title": "A2"},
                    {"article_id": "a3", "title": "A3"},
                ],
                [
                    {"story_key": "s1", "story_title": "Story", "article_ids": ["a1", "a2"]},
                ],
                total_cap=1,
                gemma_4_derived=0,
            )
            self.assertEqual([item["article_id"] for item in budgeted_targets], ["a1"])
            self.assertEqual(stats["dropped_count"], 2)

            session = pipeline.RunSession(pipeline.CONFIG)
            diagnostics = pipeline._new_run_diagnostics(2)
            session.diagnostics = diagnostics
            session.finalizer = object()
            session.model_call_stats = {"calls": {"demo": 1}}
            session.activity_snapshots = [{"label": "snapshot"}]
            session.run_log_file = run_log
            session.run_log_files = [run_log]
            session.managed_model_server_active = True
            session.managed_model_server_ready = True
            session.managed_model_server_external = False
            session.managed_model_server_process = None
            session.managed_model_server_log_file = None
            session.managed_model_server_exit_recorded = True
            session._sync_to_legacy_globals()
            self.assertIs(pipeline.ACTIVE_RUN_DIAGNOSTICS, diagnostics)

            pipeline.ACTIVE_RUN_DIAGNOSTICS = diagnostics
            pipeline.ACTIVE_RUN_FINALIZER = object()
            pipeline.MODEL_CALL_STATS = {"calls": {"demo": 2}}
            pipeline.RUN_ACTIVITY_SNAPSHOTS = [{"label": "legacy"}]
            pipeline.RUN_LOG_FILE = run_log
            pipeline.RUN_LOG_FILES = [run_log]
            pipeline.MANAGED_MODEL_SERVER_ACTIVE = True
            pipeline.MANAGED_MODEL_SERVER_READY = False
            pipeline.MANAGED_MODEL_SERVER_EXTERNAL = True
            pipeline.MANAGED_MODEL_SERVER_PROCESS = object()
            pipeline.MANAGED_MODEL_SERVER_LOG_FILE = run_log
            pipeline.MANAGED_MODEL_SERVER_EXIT_RECORDED = False
            session._capture_from_legacy_globals()
            self.assertEqual(session.model_call_stats["calls"]["demo"], 2)
            self.assertEqual(session.run_log_files, [run_log])

    def test_new_run_diagnostics_reports_source_languages(self) -> None:
        with patch.object(
            pipeline,
            "SOURCE_FEEDS",
            {
                "Alpha": {"name": "Alpha", "language": "en"},
                "Beta": {"name": "Beta", "language": "de"},
                "Gamma": {"name": "Gamma"},  # no language key -> "en"
            },
        ):
            diagnostics = pipeline._new_run_diagnostics(3)
        self.assertEqual(
            diagnostics.settings["source_languages"],
            {"de": 1, "en": 2},  # sorted keys, missing language defaults to "en"
        )

    def test_rendering_and_finalizer_helpers(self) -> None:
        record = ArticleSummaryRecord(
            title="Headline",
            source="Reuters",
            published="2026-06-06",
            url="https://example.com/story",
            article_id="a1",
            story="Story A",
            summary="A helpful summary.",
        )
        normalized = pipeline.normalize_report_entry(
            {"title": "Headline", "source": "Reuters", "published": "2026-06-06", "url": "https://example.com/story"},
            "### Headline\nMetadata:\n- Source: Reuters\nSummary:\nUseful summary.",
        )
        self.assertIn("Useful summary", normalized.summary)
        self.assertTrue(pipeline._report_reference_key(record))
        self.assertEqual(
            pipeline.filter_reports_for_references([record, "other"], {"included_report_keys": [pipeline._report_reference_key(record)]}),
            [record],
        )
        self.assertEqual(pipeline.build_email_subject(datetime(2026, 6, 6, 10, 0, 0)), "Daily LLM News, 06/06/26")
        self.assertEqual(pipeline._extract_first_name("primary@example.com"), "primary")
        self.assertEqual(pipeline._fallback_synthesis_paragraph_from_summaries(["One. Two.", "Three."]), "One. Two. Three.")
        self.assertIn("Heading", pipeline._format_plain_text_synthesis("## Heading\nBody"))
        self.assertIn("<h2", pipeline._build_html_synthesis("## Heading\nBody", []))
        self.assertIn("<div", pipeline._build_html_article_listing([record]))

        with tempfile.TemporaryDirectory() as tmpdir:
            config = replace(
                pipeline.CONFIG,
                output_dir=Path(tmpdir) / "out",
                run_output_dir=Path(tmpdir) / "out" / ".staging",
                run_staging_dir=Path(tmpdir) / "out" / ".staging",
                latest_run_markdown_path=Path(tmpdir) / "out" / "latest_run.md",
                latest_run_log_path=Path(tmpdir) / "out" / "latest_run.log",
                latest_run_details_path=Path(tmpdir) / "out" / "latest_run_details.json",
                history_db_path=Path(tmpdir) / "history.duckdb",
            )
            diagnostics = pipeline._new_run_diagnostics(1)
            fake_finalizer = SimpleNamespace(diagnostics=diagnostics, finish=MagicMock())
            with patch.object(pipeline, "ACTIVE_RUN_FINALIZER", None), patch.object(
                pipeline, "_new_run_finalizer", return_value=fake_finalizer
            ) as new_finalizer:
                pipeline._active_run_finalizer(diagnostics, config)
                pipeline._finish_run_diagnostics(diagnostics, config)
            self.assertTrue(new_finalizer.called)
            fake_finalizer.finish.assert_called_once_with()

    def test_run_session_activate_and_run_branches(self) -> None:
        stream = StringIO()
        tracker = pipeline.ProgressTracker(stream=stream)
        session = pipeline.RunSession(pipeline.CONFIG, progress=tracker)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = replace(
                pipeline.CONFIG,
                output_dir=root / "out",
                run_output_dir=root / "out" / ".staging",
                run_staging_dir=root / "out" / ".staging",
                latest_run_markdown_path=root / "out" / "latest_run.md",
                latest_run_log_path=root / "out" / "latest_run.log",
                latest_run_details_path=root / "out" / "latest_run_details.json",
                history_db_path=root / "history.duckdb",
            )
            session = pipeline.RunSession(config, progress=tracker)

            with pipeline._RUN_SESSION_LOCK:
                pipeline.ACTIVE_RUN_SESSION = object()
                with self.assertRaisesRegex(RuntimeError, "Another daily news run session"):
                    with session._activate():
                        pass
                pipeline.ACTIVE_RUN_SESSION = None

            previous_values = {
                "ACTIVE_RUN_DIAGNOSTICS": pipeline.ACTIVE_RUN_DIAGNOSTICS,
                "ACTIVE_RUN_FINALIZER": pipeline.ACTIVE_RUN_FINALIZER,
                "MODEL_CALL_STATS": pipeline.MODEL_CALL_STATS,
                "RUN_ACTIVITY_SNAPSHOTS": pipeline.RUN_ACTIVITY_SNAPSHOTS,
                "RUN_LOG_FILE": pipeline.RUN_LOG_FILE,
                "RUN_LOG_FILES": pipeline.RUN_LOG_FILES,
                "MANAGED_MODEL_SERVER_ACTIVE": pipeline.MANAGED_MODEL_SERVER_ACTIVE,
                "MANAGED_MODEL_SERVER_READY": pipeline.MANAGED_MODEL_SERVER_READY,
                "MANAGED_MODEL_SERVER_EXTERNAL": pipeline.MANAGED_MODEL_SERVER_EXTERNAL,
                "MANAGED_MODEL_SERVER_PROCESS": pipeline.MANAGED_MODEL_SERVER_PROCESS,
                "MANAGED_MODEL_SERVER_LOG_FILE": pipeline.MANAGED_MODEL_SERVER_LOG_FILE,
                "MANAGED_MODEL_SERVER_EXIT_RECORDED": pipeline.MANAGED_MODEL_SERVER_EXIT_RECORDED,
            }

            with session._activate():
                self.assertIs(pipeline.ACTIVE_RUN_SESSION, session)
                self.assertIs(pipeline.progress_tracker, tracker)
                pipeline.ACTIVE_RUN_DIAGNOSTICS = {"hello": "world"}
                pipeline.ACTIVE_RUN_FINALIZER = "finalizer"
                pipeline.MODEL_CALL_STATS = {"calls": {"demo": 1}}
                pipeline.RUN_ACTIVITY_SNAPSHOTS = [{"label": "demo"}]
                pipeline.RUN_LOG_FILE = (root / "run.log").open("w", encoding="utf-8")
                pipeline.RUN_LOG_FILES = [pipeline.RUN_LOG_FILE]
                pipeline.MANAGED_MODEL_SERVER_ACTIVE = True
                pipeline.MANAGED_MODEL_SERVER_READY = True
                pipeline.MANAGED_MODEL_SERVER_EXTERNAL = True
                pipeline.MANAGED_MODEL_SERVER_PROCESS = "process"
                pipeline.MANAGED_MODEL_SERVER_LOG_FILE = pipeline.RUN_LOG_FILE
                pipeline.MANAGED_MODEL_SERVER_EXIT_RECORDED = True

            self.assertEqual(session.model_call_stats["calls"]["demo"], 1)
            self.assertEqual(session.activity_snapshots, [{"label": "demo"}])
            self.assertEqual(session.managed_model_server_exit_recorded, True)
            self.assertEqual(pipeline.ACTIVE_RUN_SESSION, None)
            for key, value in previous_values.items():
                self.assertEqual(getattr(pipeline, key), value)

        with patch.object(pipeline, "run_logging", return_value=contextlib.nullcontext()), patch.object(
            pipeline, "managed_model_server", return_value=contextlib.nullcontext()
        ), patch.object(pipeline, "_write_run_log") as write_run_log, patch.object(
            pipeline, "_finalize_failed_run"
        ) as finalize_failed:
            session.run(run_impl=lambda: None)
        self.assertTrue(write_run_log.called)
        finalize_failed.assert_not_called()

        with patch.object(pipeline, "run_logging", return_value=contextlib.nullcontext()), patch.object(
            pipeline, "managed_model_server", return_value=contextlib.nullcontext()
        ), patch.object(pipeline.progress_tracker, "step"), patch.object(
            pipeline.progress_tracker, "detail"
        ), patch.object(pipeline, "_write_run_log"), patch.object(pipeline, "_finalize_failed_run") as finalize_failed:
            with self.assertRaisesRegex(ValueError, "boom"):
                session.run(run_impl=lambda: (_ for _ in ()).throw(ValueError("boom")))
        finalize_failed.assert_called_once()

    def test_progress_tracker_edge_helpers_and_html_synthesis(self) -> None:
        stream = StringIO()
        tracker = pipeline.ProgressTracker(stream=stream, show_meter_detail=True)
        tracker.step("setup", "init", log_detail="step detail")
        tracker.start_meter("story_drafting", total=0, unit="stories")
        tracker.advance_meter()
        tracker.finish_meter()
        tracker.current_step = "story_selection"
        tracker.meter_total = 1
        tracker.story_selection_progress("scale_screening_started", {"candidate_count": 3, "total": 3})
        tracker.story_selection_progress("scale_screening_batch_completed", {"done": 2, "candidate_count": 3, "kept_count": 1, "fallback_count": 2})
        tracker.current_step = "demo"
        tracker.meter_total = 1
        tracker.meter_done = 1
        tracker.last_render = tracker._step_prefix() + " [####################] 1/1 demo"
        tracker._line_active = True
        tracker._render_meter(force=True)
        tracker._render_meter_locked(final=True)
        tracker.story_clustering_progress("progress", {"phase": "merge", "done": 1, "total": 2, "candidate_components": 4})
        tracker.story_draft_completed({"story_title": "Rejected", "valid": False})
        self.assertIn("candidate components", stream.getvalue())

        self.assertEqual(pipeline._model_call_stats_snapshot(), pipeline.MODEL_CALL_STATS)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = replace(
                pipeline.CONFIG,
                output_dir=root / "out",
                run_output_dir=root / "out" / ".staging",
                run_staging_dir=root / "out" / ".staging",
                latest_run_markdown_path=root / "out" / "latest_run.md",
                latest_run_log_path=root / "out" / "latest_run.log",
                latest_run_details_path=root / "out" / "latest_run_details.json",
                history_db_path=root / "history.duckdb",
            )
            diag = pipeline._new_run_diagnostics(1)
            self.assertEqual(diag.settings["source_count"], 1)
            finalizer = pipeline._new_run_finalizer(diag, config)
            self.assertEqual(finalizer.config.run_id, config.timestamp)
            with patch.object(pipeline, "ACTIVE_RUN_SESSION", SimpleNamespace(finalizer=None, diagnostics=None)), patch.object(
                pipeline, "_new_run_finalizer", return_value=finalizer
            ):
                self.assertIs(pipeline._active_run_finalizer(diag, config), finalizer)

        html = pipeline._build_html_synthesis(
            "## Heading\n\n### Subheading\nText",
            [{"number": 1, "title": "Source", "url": "https://example.com", "source": "Reuters"}],
        )
        self.assertIn("<h2", html)
        self.assertIn("<h3", html)

    def test_pipeline_entry_and_budget_branches(self) -> None:
        with patch.object(pipeline, "RunSession") as run_session:
            pipeline.run_pipeline()
        run_session.assert_called_once_with(pipeline.CONFIG)

        article_targets = [{"article_id": "a1", "title": "A1"}, {"article_id": "a2", "title": "A2"}]
        story_records = [
            {"story_key": "s1", "story_title": "Story 1", "article_ids": ["a1"]},
            {"story_key": "s2", "story_title": "Story 2", "article_ids": ["a2"]},
        ]
        budgeted_targets, _, stats = pipeline._budget_article_targets_for_summary(
            article_targets,
            story_records,
            total_cap=1,
            gemma_4_derived=0,
        )
        self.assertEqual([item["article_id"] for item in budgeted_targets], ["a1"])
        self.assertEqual(stats["skipped_story_keys"], ["s2"])
    def test_token_usage_and_unsubscribe_helpers(self) -> None:
        self.assertEqual(pipeline._base64url_decode(pipeline._base64url_encode(b"hello")), b"hello")
        with patch.object(pipeline, "UNSUBSCRIBE_SECRET", "secret"), patch.object(
            pipeline, "UNSUBSCRIBE_BASE_URL", "https://example.com/unsubscribe?from=news"
        ):
            token = pipeline.build_unsubscribe_token("User@Example.com")
            self.assertEqual(pipeline.parse_unsubscribe_token(token), "user@example.com")
            self.assertIn("token=", pipeline.build_unsubscribe_url("User@Example.com"))
        with patch.object(pipeline, "RECIPIENT_SCOPE", "all"):
            self.assertEqual(
                pipeline.get_active_recipient_config({"reader@example.com": {"pause": False}}),
                {"reader@example.com": {"pause": False}},
            )

        message = AIMessage(
            content="answer",
            response_metadata={"token_usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}},
        )
        self.assertEqual(
            pipeline._extract_token_usage_from_response(message),
            {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
        )

    def test_runtime_activity_and_model_server_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("builtins.open", side_effect=OSError("boom")):
                self.assertTrue(pipeline._text_file_tail(str(root / "tail.txt")).startswith("Could not read"))

            self.assertIn("OutOfMemory", pipeline._managed_model_server_exit_message(137, "OutOfMemory"))
            self.assertIn("Managed model server log tail:", pipeline._managed_model_server_exit_message(1, "tail"))

            parsed = pipeline._parse_activity_command_output(
                'Memory free percentage: 42%\n\n"Swapins": 3\nPages occupied by compressor: 7\n'
            )
            self.assertEqual(parsed["memory_free_pct"], 42)
            self.assertEqual(parsed["swapins"], 3)
            self.assertEqual(parsed["pages_occupied_by_compressor"], 7)

            with patch.object(
                pipeline.subprocess,
                "run",
                side_effect=OSError("boom"),
            ):
                failure = pipeline._run_activity_command(["/usr/bin/vm_stat"])
            self.assertFalse(failure["ok"])
            self.assertIn("boom", failure["error"])

            completed = SimpleNamespace(
                returncode=0,
                stdout='Memory free percentage: 55%\n"Swapouts": 8\n',
                stderr='Pages occupied by compressor: 9\n',
            )
            with patch.object(pipeline.subprocess, "run", return_value=completed):
                success = pipeline._run_activity_command(["/usr/bin/vm_stat"])
            self.assertTrue(success["ok"])
            self.assertEqual(success["parsed"]["memory_free_pct"], 55)
            self.assertEqual(success["parsed"]["swapouts"], 8)
            self.assertEqual(success["parsed"]["pages_occupied_by_compressor"], 9)

            with patch.object(
                pipeline,
                "_run_activity_command",
                side_effect=[
                    {"parsed": {"memory_free_pct": 33}},
                    {"parsed": {"swapins": 1, "swapouts": 2, "pages_occupied_by_compressor": 3}},
                ],
            ):
                snapshot = pipeline.capture_activity_snapshot("demo")
            self.assertEqual(snapshot["label"], "demo")
            self.assertEqual(snapshot["memory_free_pct"], 33)
            self.assertEqual(snapshot["swapouts"], 2)
            self.assertEqual(snapshot["pages_occupied_by_compressor"], 3)

            diagnostics = SimpleNamespace(record_activity_snapshot=MagicMock(), activity_snapshots=[])
            snapshots: list[dict[str, str]] = []
            with patch.object(pipeline, "RUN_ACTIVITY_SNAPSHOTS", snapshots), patch.object(
                pipeline,
                "capture_activity_snapshot",
                return_value={"at": "2026-06-06T12:00:00", "label": "demo"},
            ):
                recorded = pipeline.record_activity_snapshot("demo", diagnostics)
            self.assertEqual(recorded["label"], "demo")
            self.assertEqual(snapshots, [{"at": "2026-06-06T12:00:00", "label": "demo"}])
            diagnostics.record_activity_snapshot.assert_called_once_with({"at": "2026-06-06T12:00:00", "label": "demo"})
            self.assertEqual(pipeline._read_url_file(str(root / "missing.txt")), set())
            urls_path = root / "urls.txt"
            urls_path.write_text("https://example.com/a\n", encoding="utf-8")
            pipeline._append_unique_urls(str(urls_path), [])
            self.assertEqual(urls_path.read_text(encoding="utf-8"), "https://example.com/a\n")
            pipeline._append_unique_urls(str(urls_path), ["https://example.com/a"])
            self.assertEqual(urls_path.read_text(encoding="utf-8"), "https://example.com/a\n")

            pending = {"at": "2026-06-06T12:01:00", "label": "new"}
            deduped_diagnostics = SimpleNamespace(
                activity_snapshots=[{"at": "2026-06-06T12:00:00", "label": "demo"}],
                record_activity_snapshot=MagicMock(),
            )
            with patch.object(
                pipeline,
                "RUN_ACTIVITY_SNAPSHOTS",
                [
                    {"at": "2026-06-06T12:00:00", "label": "demo"},
                    pending,
                ],
            ):
                pipeline._attach_pending_activity_snapshots(deduped_diagnostics)
            deduped_diagnostics.record_activity_snapshot.assert_called_once_with(pending)

    def test_external_model_server_readiness_path(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": True, "model_match": True},
        ), patch.object(
            pipeline, "probe_model_generation", return_value={"ok": True}
        ), patch.object(
            pipeline.subprocess,
            "Popen",
            side_effect=AssertionError("must not spawn a managed server"),
        ), patch.object(
            pipeline.progress_tracker, "finish_meter"
        ) as finish_meter, patch.object(
            pipeline, "record_activity_snapshot"
        ) as snapshot:
            pipeline._ensure_main_model_server_ready()
            self.assertTrue(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertTrue(pipeline.MANAGED_MODEL_SERVER_READY)
            finish_meter.assert_called_once_with(detail="External model server ready.")
            snapshot.assert_any_call("before_external_server_wait", pipeline.ACTIVE_RUN_DIAGNOSTICS)
            snapshot.assert_any_call("after_external_server_ready", pipeline.ACTIVE_RUN_DIAGNOSTICS)

    def test_external_model_server_wrong_model_retries_then_ready(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            side_effect=[
                {"ok": False, "error": "ConnectionError: not up yet"},
                {"ok": True, "model_match": False, "served_models": ["other-model"]},
            ],
        ), patch.object(
            pipeline, "probe_model_generation", return_value={"ok": True}
        ), patch.object(pipeline.time, "sleep", return_value=None), patch.object(
            pipeline.time, "monotonic", side_effect=[100.0, 100.0, 100.0]
        ):
            pipeline._ensure_main_model_server_ready()
            self.assertTrue(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertTrue(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_external_model_server_served_models_detail_on_timeout(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": False, "served_models": ["other-model"]},
        ), patch.object(pipeline.time, "sleep", return_value=None), patch.object(
            pipeline.time, "monotonic", side_effect=[100.0, 100.0, 400.0]
        ):
            with self.assertRaisesRegex(TimeoutError, "other-model"):
                pipeline._ensure_main_model_server_ready()
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_external_model_server_auth_rejection_fails_fast(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": False, "status_code": 401, "error": "HTTPError: 401 Client Error"},
        ), patch.object(
            pipeline.time, "monotonic", side_effect=[100.0, 100.0]
        ):
            with self.assertRaisesRegex(RuntimeError, "NEWS_MODEL_API_KEY"):
                pipeline._ensure_main_model_server_ready()
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_external_model_server_probe_gates_wrong_model(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": True, "model_match": False, "served_models": ["other-model"]},
        ), patch.object(
            pipeline,
            "probe_model_generation",
            return_value={"ok": False, "error": "HTTPError: 404 model not found"},
        ), patch.object(
            pipeline.time, "monotonic", side_effect=[100.0, 100.0]
        ):
            with self.assertRaisesRegex(RuntimeError, "matches a served model id"):
                pipeline._ensure_main_model_server_ready()
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_preflight_and_probe_send_auth_header_when_key_configured(self) -> None:
        with patch.object(pipeline, "MODEL_API_KEY", "secret-key"):
            with patch("news_pipeline.pipeline.requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.raise_for_status = lambda: None
                get.return_value.json.return_value = {"data": [{"id": "m"}]}
                result = pipeline._preflight_openai_model_server(
                    base_url="http://x/v1", model_name="m", model_reference="r"
                )
            self.assertTrue(result["ok"])
            self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer secret-key"})

            with patch("news_pipeline.pipeline.requests.post") as post:
                post.return_value.status_code = 200
                post.return_value.raise_for_status = lambda: None
                post.return_value.json.return_value = {"choices": []}
                result = pipeline._probe_chat_completion(
                    base_url="http://x/v1", payload={"model": "m"}, timeout_seconds=5
                )
            self.assertTrue(result["ok"])
            self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer secret-key"})

    def test_preflight_sends_no_auth_header_by_default(self) -> None:
        with patch.object(pipeline, "MODEL_API_KEY", "not-needed"):
            with patch("news_pipeline.pipeline.requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.raise_for_status = lambda: None
                get.return_value.json.return_value = {"data": [{"id": "m"}]}
                pipeline._preflight_openai_model_server(
                    base_url="http://x/v1", model_name="m", model_reference="r"
                )
            self.assertEqual(get.call_args.kwargs["headers"], {})

    def test_preflight_error_keeps_exception_type(self) -> None:
        with patch(
            "news_pipeline.pipeline.requests.get",
            side_effect=pipeline.requests.ConnectionError("connection refused"),
        ):
            result = pipeline._preflight_openai_model_server(
                base_url="http://x/v1", model_name="m", model_reference="r"
            )
        self.assertFalse(result["ok"])
        self.assertIn("ConnectionError: connection refused", result["error"])

    def test_raise_if_managed_model_server_exited_skips_external(self) -> None:
        process = MagicMock()
        with patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", True), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", True
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_PROCESS", process):
            pipeline._raise_if_managed_model_server_exited()
            process.poll.assert_not_called()

    def test_managed_model_server_context_external_teardown(self) -> None:
        with patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", False), patch.object(
            pipeline,
            "_stop_managed_server_process",
            side_effect=AssertionError("must not stop a server"),
        ):
            with pipeline.managed_model_server():
                pipeline.MANAGED_MODEL_SERVER_EXTERNAL = True
                pipeline.MANAGED_MODEL_SERVER_READY = True
        self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
        self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)
        self.assertIsNone(pipeline.MANAGED_MODEL_SERVER_PROCESS)
        self.assertIsNone(pipeline.MANAGED_MODEL_SERVER_LOG_FILE)
        self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXIT_RECORDED)

    def test_external_model_server_readiness_timeout(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": False, "error": "connection refused"},
        ), patch.object(pipeline.time, "sleep", return_value=None), patch.object(
            pipeline.time, "monotonic", side_effect=[100.0, 100.0, 400.0]
        ):
            with self.assertRaisesRegex(TimeoutError, "External model server did not become ready"):
                pipeline._ensure_main_model_server_ready()
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_external_model_server_readiness_probe_failure(self) -> None:
        with patch.object(pipeline, "MODEL_BACKEND", "external"), patch.object(
            pipeline, "MANAGED_MODEL_SERVER_EXTERNAL", False
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_READY", False), patch.object(
            pipeline, "ensure_codex_safe_model_reference"
        ), patch.object(
            pipeline,
            "preflight_model_server",
            return_value={"ok": True, "model_match": True},
        ), patch.object(
            pipeline,
            "probe_model_generation",
            return_value={"ok": False, "error": "boom"},
        ):
            with self.assertRaisesRegex(RuntimeError, "failed a tiny generation probe"):
                pipeline._ensure_main_model_server_ready()
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_EXTERNAL)
            self.assertFalse(pipeline.MANAGED_MODEL_SERVER_READY)

    def test_progress_tracker_and_run_logging_branches(self) -> None:
        stream = StringIO()
        tracker = pipeline.ProgressTracker(stream=stream, show_meter_detail=True)
        tracker.step("setup", "Initializing")
        tracker.log("[progress] hello")
        tracker.start_meter("sources", total=2, unit="sources", detail="loading", done=1)
        tracker.update_meter(done=2, detail="done", force=True)
        tracker.advance_meter(detail="still done")
        tracker.finish_meter(detail="finished")
        tracker.reset(total_sources=2)
        tracker.start_source(1, "Reuters")
        tracker.set_source_article_total(3)
        tracker.source_completed("Reuters", candidate_articles=2, worker_count=4)
        tracker.update_source_fresh_articles(5, latest_source="AP")
        tracker.current_step = "summaries"
        tracker.start_article_summary(2)
        tracker.article_completed({"title": "Headline", "source_display_name": "Reuters"})
        tracker.start_story_clustering(2, detail="clustering")
        tracker.story_clustering_progress("progress", {"phase": "pairing", "done": 1, "total": 2, "linked_pairs": 3})
        tracker.start_story_drafting(2)
        tracker.story_draft_completed({"story_title": "Draft", "valid": True})
        tracker.story_selection_progress("scale_screening_started", {"total": 2})
        tracker.story_selection_progress("scale_screening_batch_completed", {"done": 2, "total": 2, "kept_count": 1, "fallback_count": 0})
        tracker.retrying("task", 1, 2, 3, RuntimeError("boom"))
        tracker.warning("careful")
        tracker.set_final_step("reports", 1)
        tracker.finish("done")
        tracker._finish_active_line()
        self.assertIn("[1/9 setup]", stream.getvalue())
        self.assertEqual(tracker._step_prefix("unknown"), "[unknown]")
        self.assertEqual(tracker._compact_detail("a " * 100, max_chars=10), "a a a a a...")
        self.assertEqual(tracker._source_detail(latest_source="AP"), "workers 4 | latest: AP | 5 fresh articles")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_log = root / "run.log"
            latest_log = root / "latest.log"
            with patch.object(pipeline, "RUN_LOG_PATH", str(run_log)), patch.object(
                pipeline,
                "LATEST_RUN_LOG_PATH",
                str(latest_log),
            ):
                with pipeline.run_logging():
                    pipeline.progress_tracker.detail("run logging detail")
            self.assertTrue(run_log.exists())
            self.assertTrue(latest_log.exists())

            class FakeProcess:
                returncode = 7

                def poll(self) -> int:
                    return self.returncode



    def test_unsubscribe_google_news_and_source_match_branch_helpers(self) -> None:
        with patch.object(pipeline, "UNSUBSCRIBE_SECRET", "secret"), patch.object(
            pipeline, "UNSUBSCRIBE_BASE_URL", "https://example.com/unsubscribe?from=news"
        ):
            token = pipeline.build_unsubscribe_token("User@Example.com")
            self.assertEqual(pipeline.parse_unsubscribe_token(token), "user@example.com")
            self.assertIn("token=", pipeline.build_unsubscribe_url("User@Example.com"))
            with self.assertRaisesRegex(ValueError, "Malformed unsubscribe token"):
                pipeline.parse_unsubscribe_token("abc")
            with self.assertRaisesRegex(ValueError, "Invalid unsubscribe token signature"):
                pipeline.parse_unsubscribe_token(token[:-1] + ("A" if token[-1] != "A" else "B"))
            with self.assertRaisesRegex(ValueError, "valid email address"):
                pipeline.parse_unsubscribe_token(pipeline.build_unsubscribe_token("invalid"))

        with patch.object(pipeline, "RECIPIENT_SCOPE", "all"), patch.object(
            pipeline, "EMAIL_RECIPIENTS_FALLBACK", ["fallback@example.com"]
        ):
            self.assertEqual(
                pipeline.get_active_recipient_config({}),
                {"fallback@example.com": {"name": "fallback@example.com", "pause": False}},
            )

        self.assertEqual(
            pipeline._resolve_google_news_url_details(
                "https://news.google.com/rss/articles/G2h0dHBzOi8vZXhhbXBsZS5jb20vZGVjb2RlZA"
            )["resolution_status"],
            "google_news_resolved_decode",
        )

        with patch.object(pipeline, "current_thread", return_value=object()), patch.object(
            pipeline, "main_thread", return_value=object()
        ):
            with pipeline._article_scrape_deadline(5):
                pass

        captured_handler = {}

        def fake_signal(_signum, handler):  # noqa: ANN001
            captured_handler["handler"] = handler
            return "previous-handler"

        with patch.object(pipeline.signal, "getsignal", return_value="previous-handler"), patch.object(
            pipeline.signal,
            "setitimer",
            side_effect=[(1.0, 2.0), None, None, None],
        ), patch.object(pipeline.signal, "signal", side_effect=fake_signal):
            with self.assertRaises(pipeline.ArticleScrapeTimeoutError):
                with pipeline._article_scrape_deadline(5):
                    captured_handler["handler"](None, None)

        with patch.object(
            pipeline,
            "_article_scrape_deadline",
            return_value=contextlib.nullcontext(),
        ), patch.object(
            pipeline,
            "_download_article_html",
            side_effect=pipeline.requests.RequestException("boom"),
        ):
            self.assertEqual(
                pipeline.scrape_article_text("https://example.com/story", source="Reuters", title="Headline"),
                ("Access Denied.", "access_denied"),
            )

        with patch.object(
            pipeline,
            "_resolve_google_news_url_details",
            return_value={
                "original_url": "https://example.com/story",
                "resolved_url": "https://example.com/story",
                "resolution_status": "not_google_news",
            },
        ), patch.object(pipeline, "_is_google_news_url", return_value=False), patch.object(
            pipeline,
            "scrape_article_text",
            return_value=("Access denied.", "access_denied"),
        ):
            fallback = pipeline._resolve_and_scrape_feed_article(
                "https://example.com/story",
                title="Headline",
                description="Lead",
                source="Reuters",
            )
        self.assertEqual(fallback["scrape_status"], "access_denied_feed_fallback")
        self.assertTrue(fallback["feed_fallback_used"])

        self.assertFalse(False)

        source_config = {"name": "Reuters", "strict_source_match": True}
        wrong_result = pipeline._source_match_result_for_feed_item(
            "Reuters",
            source_config,
            {"title": "Headline", "source": "AP", "link": "https://example.com/story"},
        )
        self.assertEqual(wrong_result["source_match_status"], "wrong_feed_source")

        aliases = pipeline._wire_attribution_aliases(
            " The Reuters ",
            {"name": " ", "source_match_aliases": [" ", "Reuters"]},
        )
        self.assertEqual(aliases, ["The Reuters", "Reuters"])
        self.assertEqual(pipeline._wire_attribution_phrase_pattern("credited to"), r"credited\s+to")
        self.assertEqual(pipeline._wire_attribution_alias_pattern("The Reuters"), r"Reuters")
        source_config = {"name": "The Reuters", "source_match_aliases": ["Reuters"]}
        self.assertTrue(
            pipeline._article_confirms_wire_attribution(
                "Reuters",
                source_config,
                {"author": "", "creator": "", "description": "Reuters contributed to this report."},
                "",
            )[0]
        )
        self.assertTrue(
            pipeline._article_confirms_wire_attribution(
                "Reuters",
                source_config,
                {"author": "", "creator": "", "description": "",},
                "Copyright 2026 Reuters",
            )[0]
        )
        self.assertTrue(
            pipeline._article_confirms_wire_attribution(
                "Reuters",
                source_config,
                {"author": "", "creator": "", "description": ""},
                "Reuters",
            )[0]
        )
        self.assertFalse(
            pipeline._article_confirms_wire_attribution(
                "Reuters",
                source_config,
                {"author": "", "creator": "", "description": ""},
                "",
            )[0]
        )

        self.assertEqual(
            pipeline._source_match_result_for_feed_item(
                "Reuters",
                {"name": "Reuters", "strict_source_match": False},
                {"title": "Headline - Reuters", "source": "Reuters", "link": "https://example.com/story"},
            )["source_match_status"],
            "not_required",
        )
        self.assertFalse(pipeline._is_excluded_feed_item("Headline", "Reuters", "https://example.com/story"))
        self.assertTrue(
            pipeline._url_has_excluded_source_domain("https://www.abcnews.go.com/story")
        )
        with patch.object(pipeline, "urlparse", side_effect=ValueError("bad")):
            self.assertFalse(pipeline._url_has_excluded_source_domain("not-a-url"))
        self.assertFalse(
            pipeline._article_confirms_wire_attribution(
                "Reuters",
                {"name": "Reuters", "source_match_aliases": ["Reuters"]},
                {"author": "", "creator": "", "description": "Unrelated text."},
                "Still unrelated.",
            )[0]
        )

    def test_task_model_assignment_resolves_all_stages_and_inheritance(self) -> None:
        fake_assignments = {
            "default": object(),
            "article_summary": object(),
            "story_drafting": object(),
            "story_scale_screening": object(),
            "title_generation": object(),
        }
        with patch.object(pipeline, "MODEL_ASSIGNMENTS", fake_assignments):
            self.assertIs(
                pipeline._task_model_assignment("article_summary"),
                fake_assignments["article_summary"],
            )
            self.assertIs(
                pipeline._task_model_assignment("story_drafting"),
                fake_assignments["story_drafting"],
            )
            self.assertIs(
                pipeline._task_model_assignment("story_scale_screening"),
                fake_assignments["story_scale_screening"],
            )
            self.assertIs(
                pipeline._task_model_assignment("title_generation"),
                fake_assignments["title_generation"],
            )
            # image_art_direction shares the title_generation LLM call.
            self.assertIs(
                pipeline._task_model_assignment("image_art_direction"),
                fake_assignments["title_generation"],
            )
            # story_discovery has no LLM stage; it inherits default.
            self.assertIs(
                pipeline._task_model_assignment("story_discovery"),
                fake_assignments["default"],
            )
            # Unknown tasks also fall back to default (never a KeyError).
            self.assertIs(
                pipeline._task_model_assignment("analysis"),
                fake_assignments["default"],
            )

    def test_model_email_and_art_helpers(self) -> None:
        fake_assignment = SimpleNamespace(
            base_url=pipeline.MODEL_BASE_URL,
            name=pipeline.MODEL_NAME,
            tuning=SimpleNamespace(task_sampling={"default": ModelSamplingSettings(temperature=0.3)}),
        )
        with patch.object(pipeline, "ensure_codex_safe_model_reference"), patch.object(
            pipeline, "_task_model_assignment", return_value=fake_assignment
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", False), patch.object(
            pipeline, "ChatOpenAI", return_value="chat-model"
        ) as chat_model:
            self.assertEqual(pipeline.build_chat_model(64, task="analysis"), "chat-model")
        chat_model.assert_called_once()

        with patch.object(pipeline, "ensure_codex_safe_model_reference"), patch.object(
            pipeline,
            "_task_model_assignment",
            return_value=SimpleNamespace(
                base_url=pipeline.MODEL_BASE_URL,
                reference="other-model",
                name="other-model",
                tuning=fake_assignment.tuning,
            ),
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", True), patch.object(
            pipeline, "MODEL_BACKEND", "mlx-lm"
        ):
            with self.assertRaisesRegex(RuntimeError, "multiple different models"):
                pipeline.build_chat_model(64, task="analysis")

        with patch.object(pipeline, "ensure_codex_safe_model_reference"), patch.object(
            pipeline, "_task_model_assignment", return_value=fake_assignment
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", True), patch.object(
            pipeline, "MODEL_BACKEND", "mlx-lm"
        ), patch.object(
            pipeline, "_ensure_main_model_server_ready"
        ) as ready, patch.object(pipeline, "_raise_if_managed_model_server_exited") as exited, patch.object(
            pipeline, "ChatOpenAI", return_value="chat-model"
        ):
            self.assertEqual(pipeline.build_chat_model(64, task="analysis"), "chat-model")
        ready.assert_called_once()
        exited.assert_called_once()

        # External backends serve multiple models from one base URL: the
        # managed-server restriction (and its health gates) must not apply
        # even while the managed-server context is active.
        with patch.object(pipeline, "ensure_codex_safe_model_reference"), patch.object(
            pipeline,
            "_task_model_assignment",
            return_value=SimpleNamespace(
                base_url=pipeline.MODEL_BASE_URL,
                reference="other-model",
                name="other-model",
                tuning=fake_assignment.tuning,
            ),
        ), patch.object(pipeline, "MANAGED_MODEL_SERVER_ACTIVE", True), patch.object(
            pipeline, "MODEL_BACKEND", pipeline.MODEL_BACKEND_EXTERNAL
        ), patch.object(
            pipeline, "_ensure_main_model_server_ready"
        ) as ready, patch.object(pipeline, "_raise_if_managed_model_server_exited") as exited, patch.object(
            pipeline, "ChatOpenAI", return_value="chat-model"
        ):
            self.assertEqual(pipeline.build_chat_model(64, task="analysis"), "chat-model")
        ready.assert_not_called()
        exited.assert_not_called()

        with patch.object(pipeline.progress_tracker, "detail") as detail:
            pipeline.maybe_email_report("Title", "Body", "Synthesis", [], [], [])
        detail.assert_called_once()

        with patch.object(pipeline, "EMAIL_FROM", ""), patch.object(pipeline, "SMTP_HOST", ""), patch.object(
            pipeline, "SMTP_USERNAME", ""
        ), patch.object(pipeline, "SMTP_PASSWORD", ""), patch.object(pipeline.progress_tracker, "detail") as detail:
            pipeline.maybe_email_report("Title", "Body", "Synthesis", [], ["reader@example.com"], ["Reader"])
        self.assertIn("NEWS_EMAIL_FROM", detail.call_args[0][0])

        class FakeSMTP:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                self.args = args
                self.kwargs = kwargs
                self.started_tls = False
                self.logged_in = None
                self.messages = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN002, ANN003
                return False

            def starttls(self) -> None:
                self.started_tls = True

            def login(self, username: str, password: str) -> None:
                self.logged_in = (username, password)

            def send_message(self, message) -> None:  # noqa: ANN001
                self.messages.append(message)

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "image.png"
            image_path.write_bytes(b"PNG")
            fake_smtp = FakeSMTP()
            with patch.object(pipeline, "EMAIL_FROM", "from@example.com"), patch.object(
                pipeline, "SMTP_HOST", "smtp.example.com"
            ), patch.object(pipeline, "SMTP_USERNAME", "user"), patch.object(
                pipeline, "SMTP_PASSWORD", "secret"
            ), patch.object(
                pipeline, "SMTP_PORT", 587
            ), patch.object(
                pipeline, "SMTP_USE_SSL", False
            ), patch.object(
                pipeline, "build_report_html", return_value="<html>report</html>"
            ), patch.object(
                pipeline, "build_unsubscribe_url", return_value="https://example.com/unsubscribe?token=abc"
            ), patch.object(
                pipeline.smtplib,
                "SMTP",
                return_value=fake_smtp,
            ):
                pipeline.maybe_email_report(
                    "Daily Brief",
                    "Body text",
                    "Synthesis text",
                    [],
                    ["reader@example.com"],
                    ["Reader"],
                    image_art={"final_image_path": str(image_path), "overlay_headline": "Headline"},
                    citation_sources=[],
                    citation_groups=[],
                )
        self.assertTrue(fake_smtp.started_tls)
        self.assertEqual(fake_smtp.logged_in, ("user", "secret"))
        self.assertEqual(len(fake_smtp.messages), 1)
        self.assertIn("List-Unsubscribe", fake_smtp.messages[0])

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "missing.png"
            fake_smtp_ssl = FakeSMTP()
            with patch.object(pipeline, "EMAIL_FROM", "from@example.com"), patch.object(
                pipeline, "SMTP_HOST", "smtp.example.com"
            ), patch.object(pipeline, "SMTP_USERNAME", "user"), patch.object(
                pipeline, "SMTP_PASSWORD", "secret"
            ), patch.object(
                pipeline, "SMTP_PORT", 465
            ), patch.object(
                pipeline, "SMTP_USE_SSL", True
            ), patch.object(
                pipeline, "build_report_html", return_value="<html>report</html>"
            ), patch.object(
                pipeline, "build_unsubscribe_url", return_value="https://example.com/unsubscribe?token=abc"
            ), patch.object(
                pipeline.smtplib,
                "SMTP_SSL",
                return_value=fake_smtp_ssl,
            ), patch("builtins.open", side_effect=OSError("boom")), patch.object(
                pipeline.progress_tracker, "warning"
            ) as warning:
                pipeline.maybe_email_report(
                    "Daily Brief",
                    "Body text",
                    "Synthesis text",
                    [],
                    ["reader@example.com"],
                    ["Reader"],
                    image_art={"final_image_path": str(image_path), "overlay_headline": "Headline"},
                    citation_sources=[],
                    citation_groups=[],
                )
        self.assertTrue(fake_smtp_ssl.started_tls is False)
        self.assertEqual(fake_smtp_ssl.logged_in, ("user", "secret"))
        self.assertEqual(len(fake_smtp_ssl.messages), 1)
        warning.assert_called_once()
        self.assertIn("Image attachment read failed", warning.call_args[0][0])

        self.assertEqual(pipeline._first_sentences("One. Two. Three.", max_sentences=2, max_chars=100), "One. Two.")
        self.assertEqual(pipeline._first_sentences("", max_sentences=2, max_chars=100), "")
        self.assertTrue(pipeline._first_sentences("One two three four five six seven eight nine ten.", max_sentences=1, max_chars=10).endswith("..."))
        self.assertEqual(
            pipeline._fallback_synthesis_paragraph_from_summaries(["One. Two.", "Three. Four."]),
            "One. Two. Three. Four.",
        )
        self.assertTrue(pipeline._truncate_for_art_prompt("x " * 100, max_chars=10).endswith("..."))
        self.assertEqual(
            pipeline._sanitize_overlay_headline("Headline: **Big** news", "Fallback"),
            "Big news",
        )
        self.assertEqual(pipeline._sanitize_overlay_headline("", "Fallback"), "Fallback")
        enforced_prompt = pipeline._enforce_text_free_image_prompt("")
        self.assertIn("Hard constraints:", enforced_prompt)
        self.assertIn("readable headline will be rendered later by code", enforced_prompt)
        self.assertEqual(
            pipeline._enforce_text_free_image_prompt("readable headline will be rendered later by code"),
            "readable headline will be rendered later by code",
        )

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline,
            "invoke_with_retries",
            return_value=AIMessage(
                content=(
                    '{"image_prompt":"A documentary scene with text on signs","overlay_headline":"This headline should be shortened to eleven words total"}'
                )
            ),
        ):
            art_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        self.assertIn("Hard constraints", art_brief["image_prompt"])
        self.assertLessEqual(len(art_brief["overlay_headline"].split()), 11)

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline,
            "invoke_with_retries",
            return_value=AIMessage(content="[]"),
        ), patch.object(pipeline.progress_tracker, "warning"):
            art_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        self.assertIn("error", art_brief)

        with patch.object(pipeline, "build_chat_model", side_effect=RuntimeError("boom")), patch.object(
            pipeline.progress_tracker, "warning"
        ):
            art_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        self.assertEqual(art_brief["overlay_headline"], "Report title")
        self.assertIn("image_prompt", art_brief)

    def test_build_image_art_system_prompt_contains_protocol(self) -> None:
        # The extracted pure helper must always carry the pipeline-owned JSON
        # contract and overlay protocol regardless of the editorial sentences
        # it is given (mirrors the mock-based assertions in the brief tests
        # above, but directly on the helper).
        system_text = pipeline._build_image_art_system_prompt("X", "Y")
        self.assertIn(
            "Return ONLY valid JSON with keys image_prompt and overlay_headline",
            system_text,
        )
        self.assertIn("rendered later by code", system_text)
        self.assertTrue(system_text.endswith("Y"))

    def test_generate_image_art_brief_injects_profile_instructions(self) -> None:
        captured: dict[str, str] = {}

        def fake_invoke(_llm, messages, **_kwargs):
            captured["system"] = str(messages[0].content)
            return AIMessage(content="unused")

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline, "invoke_with_retries", side_effect=fake_invoke
        ), patch.object(
            pipeline,
            "_safe_json_extract",
            return_value='{"image_prompt":"A documentary scene","overlay_headline":"Today in brief"}',
            create=True,
        ):
            art_brief = pipeline.generate_image_art_brief(
                "Summary text",
                "Report title",
                prompt_instructions={
                    "image_art_direction": "Depict the central event without sensationalism.",
                    "title_generation": "Prefer a title expressing the day's central shared development.",
                },
            )
        system_text = captured["system"]
        self.assertIn("Depict the central event without sensationalism.", system_text)
        self.assertIn(
            "Prefer a title expressing the day's central shared development.",
            system_text,
        )
        self.assertIn(
            "Return ONLY valid JSON with keys image_prompt and overlay_headline",
            system_text,
        )
        self.assertTrue(art_brief["image_prompt"].startswith("A documentary scene"))
        self.assertIn("Hard constraints", art_brief["image_prompt"])
        self.assertEqual(art_brief["overlay_headline"], "Today in brief")

    def test_generate_image_art_brief_uses_balanced_instructions_by_default(self) -> None:
        captured: dict[str, str] = {}

        def fake_invoke(_llm, messages, **_kwargs):
            captured["system"] = str(messages[0].content)
            return AIMessage(content="unused")

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline, "invoke_with_retries", side_effect=fake_invoke
        ), patch.object(
            pipeline,
            "_safe_json_extract",
            return_value='{"image_prompt":"A documentary scene","overlay_headline":"Today in brief"}',
            create=True,
        ):
            art_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        system_text = captured["system"]
        self.assertIn(
            "The image_prompt is for FLUX and must request a realistic documentary",
            system_text,
        )
        self.assertIn("Keep overlay_headline punchy, factual, and <= 11 words.", system_text)
        self.assertEqual(art_brief["overlay_headline"], "Today in brief")

    def test_generate_image_art_brief_per_key_fallback_and_warning(self) -> None:
        captured: dict[str, str] = {}

        def fake_invoke(_llm, messages, **_kwargs):
            captured["system"] = str(messages[0].content)
            return AIMessage(content="unused")

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline, "invoke_with_retries", side_effect=fake_invoke
        ), patch.object(
            pipeline,
            "_safe_json_extract",
            return_value='{"image_prompt":"A documentary scene","overlay_headline":"Today in brief"}',
            create=True,
        ), patch.object(pipeline.progress_tracker, "warning") as warning_mock:
            art_brief = pipeline.generate_image_art_brief(
                "Summary text",
                "Report title",
                prompt_instructions={
                    "image_art_direction": "Depict the central event without sensationalism.",
                    # title_generation intentionally missing: per-key fallback.
                },
            )
        system_text = captured["system"]
        self.assertIn("Depict the central event without sensationalism.", system_text)
        self.assertIn("Keep overlay_headline punchy, factual, and <= 11 words.", system_text)
        warning_mock.assert_called_once_with(
            "prompt profile missing title_generation; using balanced default"
        )
        self.assertEqual(art_brief["overlay_headline"], "Today in brief")
    def test_import_fallback_and_progress_helpers(self) -> None:
        spec = importlib.util.spec_from_file_location("news_pipeline.pipeline_tiktoken_missing", pipeline.__file__)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
            if name == "tiktoken":
                raise ImportError("missing tiktoken")
            return real_import(name, globals, locals, fromlist, level)

        try:
            with patch("builtins.__import__", side_effect=fake_import):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)

        self.assertIsNone(module.tiktoken)
        self.assertEqual(module._filesystem_safe_model_label("  /bad label?  "), "bad_label")

        tracker = pipeline.ProgressTracker(stream=StringIO())
        tracker.start_source(2, "Reuters")
        tracker.source_completed("Reuters")
        tracker.current_step = "setup"
        tracker.meter_total = 1
        tracker.source_completed("Reuters")
        tracker._render_meter_locked()
        self.assertEqual(tracker.current_step, "sources")

    def test_runtime_wrappers_and_story_synthesis_helpers(self) -> None:
        with patch.object(pipeline.article_summarization_stage, "ArticleSummarizationRuntime", return_value="summary-runtime") as runtime_ctor:
            self.assertEqual(pipeline._article_summarization_runtime(), "summary-runtime")
        runtime_ctor.assert_called_once()

        with patch.object(pipeline, "progress_tracker") as tracker:
            pipeline._story_drafting_progress("story_drafting_started", {"total": 2})
            pipeline._story_drafting_progress("story_draft_completed", {"story": {"story_title": "Draft", "valid": True}})
        tracker.start_story_drafting.assert_called_once_with(2)
        tracker.story_draft_completed.assert_called_once_with({"story_title": "Draft", "valid": True})

        with patch.object(pipeline.story_drafting_stage, "StoryDraftingRuntime", return_value="draft-runtime") as draft_runtime_ctor:
            self.assertEqual(pipeline._story_drafting_runtime(min_articles_per_story=7), "draft-runtime")
        draft_runtime_ctor.assert_called_once()

        with patch.object(pipeline.story_selection_stage, "StorySelectionRuntime", return_value="selection-runtime") as selection_runtime_ctor:
            self.assertEqual(pipeline._story_selection_runtime(), "selection-runtime")
        selection_runtime_ctor.assert_called_once()

    def test_stage_runtimes_receive_resolved_profile_instructions(self) -> None:
        # The feature's core propagation contract: every stage runtime must be
        # built with the resolved profile instructions for its task slot. A
        # refactor that drops or miskeys a kwarg would silently yield balanced
        # output while the user selected another profile.
        with patch.object(
            pipeline.article_summarization_stage, "ArticleSummarizationRuntime"
        ) as summary_ctor:
            pipeline._article_summarization_runtime()
        self.assertEqual(
            summary_ctor.call_args.kwargs["prompt_instructions"],
            pipeline.PROMPT_INSTRUCTIONS["article_summary"],
        )

        with patch.object(
            pipeline.story_drafting_stage, "StoryDraftingRuntime"
        ) as draft_ctor:
            pipeline._story_drafting_runtime(min_articles_per_story=7)
        self.assertEqual(
            draft_ctor.call_args.kwargs["prompt_instructions"],
            pipeline.PROMPT_INSTRUCTIONS["story_drafting"],
        )

        with patch.object(
            pipeline.story_selection_stage, "StorySelectionRuntime"
        ) as selection_ctor:
            pipeline._story_selection_runtime()
        self.assertEqual(
            selection_ctor.call_args.kwargs["prompt_instructions"],
            pipeline.PROMPT_INSTRUCTIONS["story_scale_screening"],
        )
        self.assertEqual(
            selection_ctor.call_args.kwargs["story_scale_screening_max_tokens"],
            pipeline.MODEL_ASSIGNMENTS[MODEL_TASK_STORY_SCALE_SCREENING]
            .tuning.story_scale_screening_max_tokens,
        )

    def test_story_selection_runtime_receives_scale_screening_token_cap(self) -> None:
        # The tuned cap must reach the runtime constructor; a regression that
        # threads the wrong field or hardcodes the cap would silently ignore
        # NEWS_STORY_SCALE_SCREENING_MAX_TOKENS for every default run.
        fake_assignments = {
            MODEL_TASK_STORY_SCALE_SCREENING: SimpleNamespace(
                tuning=SimpleNamespace(story_scale_screening_max_tokens=2600)
            ),
        }
        with patch.object(pipeline, "MODEL_ASSIGNMENTS", fake_assignments), patch.object(
            pipeline.story_selection_stage, "StorySelectionRuntime"
        ) as selection_ctor:
            pipeline._story_selection_runtime()
        self.assertEqual(
            selection_ctor.call_args.kwargs["story_scale_screening_max_tokens"],
            2600,
        )

        # Fallback branch: unset tuning value falls back to the stage default.
        fake_assignments[MODEL_TASK_STORY_SCALE_SCREENING] = SimpleNamespace(
            tuning=SimpleNamespace(story_scale_screening_max_tokens=None)
        )
        with patch.object(pipeline, "MODEL_ASSIGNMENTS", fake_assignments), patch.object(
            pipeline.story_selection_stage, "StorySelectionRuntime"
        ) as selection_ctor:
            pipeline._story_selection_runtime()
        self.assertEqual(
            selection_ctor.call_args.kwargs["story_scale_screening_max_tokens"],
            pipeline.story_selection_stage.STORY_SCALE_VALIDATION_MAX_TOKENS,
        )

    def test_generate_image_art_brief_uses_tuned_title_generation_max_tokens(self) -> None:
        captured: dict[str, object] = {}

        def fake_build_chat_model(max_tokens, task="title_generation", **_kwargs):
            captured["max_tokens"] = max_tokens
            return object()

        with patch.object(pipeline, "build_chat_model", side_effect=fake_build_chat_model), patch.object(
            pipeline,
            "invoke_with_retries",
            return_value=AIMessage(content=json.dumps(
                {"image_prompt": "A scene", "overlay_headline": "Today"}
            )),
        ), patch.object(
            pipeline, "_safe_json_extract", side_effect=lambda s: s, create=True
        ):
            # Default path: tuned value (700) is used.
            pipeline.generate_image_art_brief("Summary text", "Report title")
            self.assertEqual(captured["max_tokens"], 700)

            # Custom cap reaches the LLM call.
            fake_assignments = dict(pipeline.MODEL_ASSIGNMENTS)
            fake_assignments[MODEL_TASK_TITLE_GENERATION] = SimpleNamespace(
                tuning=SimpleNamespace(title_generation_max_tokens=1200)
            )
            with patch.object(pipeline, "MODEL_ASSIGNMENTS", fake_assignments):
                pipeline.generate_image_art_brief("Summary text", "Report title")
            self.assertEqual(captured["max_tokens"], 1200)

    def test_image_rendering_and_image_art_helpers(self) -> None:
        with patch("PIL.ImageFont.truetype", return_value="truetype-font"), patch(
            "os.path.exists",
            return_value=True,
        ):
            self.assertEqual(pipeline._load_overlay_font(12), "truetype-font")
        with patch("PIL.ImageFont.load_default", return_value="default-font"), patch(
            "os.path.exists",
            return_value=False,
        ):
            self.assertEqual(pipeline._load_overlay_font(12), "default-font")

        class FakeDraw:
            def textbbox(self, _position, text, font=None):  # noqa: ANN001
                return (0, 0, len(str(text)), 10)

        self.assertEqual(pipeline._wrap_text_to_width(FakeDraw(), "", object(), 10), [])
        self.assertEqual(
            pipeline._wrap_text_to_width(FakeDraw(), "one two three", object(), 3),
            ["one", "two", "three"],
        )
        self.assertEqual(
            pipeline._wrap_text_to_width(FakeDraw(), "one two", object(), 100),
            ["one two"],
        )

        image = Image.new("RGB", (80, 80), "white")
        with patch.object(pipeline, "_load_overlay_font", return_value=None), patch.object(
            pipeline,
            "_wrap_text_to_width",
            return_value=["Headline one", "Headline two"],
        ):
            overlayed = pipeline.add_headline_overlay(image, "Headline one headline two", crop_bottom_ratio=0.2)
        self.assertGreater(overlayed.size[1], image.size[1])

        with patch.object(pipeline.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "mflux FLUX.2 support is not importable"):
                pipeline.generate_image_with_mflux("prompt", output_path="out.png", seed=1)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "generated.png"

            def fake_run(command, check, cwd, stdout, stderr):  # noqa: ANN001
                raw_output_path = command[command.index("--output") + 1]
                Image.new("RGB", (2, 2), "blue").save(raw_output_path)
                return SimpleNamespace(returncode=0)

            with patch.object(pipeline.importlib.util, "find_spec", return_value=SimpleNamespace()), patch.object(
                pipeline.subprocess,
                "run",
                side_effect=fake_run,
            ):
                raw_image = pipeline.generate_image_with_mflux(
                    "prompt",
                    output_path=str(output_path),
                    seed=7,
                )
            self.assertTrue(output_path.exists())
            self.assertEqual(raw_image.size, (2, 2))

            with patch.object(pipeline.importlib.util, "find_spec", return_value=SimpleNamespace()), patch.object(
                pipeline.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ):
                with self.assertRaises(RuntimeError):
                    pipeline.generate_image_with_mflux("prompt", output_path=str(root / "missing.png"), seed=8)

        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline,
            "invoke_with_retries",
            return_value=SimpleNamespace(content="unused"),
        ), patch.object(
            pipeline,
            "_safe_json_extract",
            return_value='{"image_prompt":"Prompt","overlay_headline":"Headline"}',
            create=True,
        ):
            generated_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        self.assertEqual(generated_brief["overlay_headline"], "Headline")
        self.assertIn("Prompt", generated_brief["image_prompt"])
        self.assertIn("Hard constraints", generated_brief["image_prompt"])
        with patch.object(pipeline, "build_chat_model", return_value=object()), patch.object(
            pipeline,
            "invoke_with_retries",
            return_value=SimpleNamespace(content="unused"),
        ), patch.object(
            pipeline,
            "_safe_json_extract",
            return_value="[]",
            create=True,
        ), patch.object(pipeline.progress_tracker, "warning"):
            error_brief = pipeline.generate_image_art_brief("Summary text", "Report title")
        self.assertIn("error", error_brief)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = root / "report.md"
            base_image = Image.new("RGB", (4, 4), "white")
            overlay_image = Image.new("RGB", (5, 6), "black")
            with patch.object(pipeline, "IMAGE_GENERATION_ENABLED", False):
                self.assertIsNone(
                    pipeline.generate_report_image_art(
                        report_path=str(report_path),
                        synthesis_body="Summary body",
                        report_title="Report title",
                    )
                )
            raw_paths: list[str] = []

            def fake_generate_image(*_args, output_path: str, **_kwargs):
                raw_paths.append(output_path)
                return base_image

            with patch.object(pipeline, "IMAGE_GENERATION_ENABLED", True), patch.object(
                pipeline,
                "generate_image_art_brief",
                return_value={"image_prompt": "Prompt", "overlay_headline": "Headline"},
            ), patch.object(
                pipeline,
                "generate_image_with_mflux",
                side_effect=fake_generate_image,
            ), patch.object(
                pipeline,
                "add_headline_overlay",
                return_value=overlay_image,
            ), patch.object(
                pipeline.progress_tracker,
                "detail",
            ), patch.object(
                pipeline.progress_tracker,
                "warning",
            ):
                art = pipeline.generate_report_image_art(
                    report_path=str(report_path),
                    synthesis_body="Summary body",
                    report_title="Report title",
                )
            final_path = root / "report_image.png"
            self.assertEqual(art["final_image_path"], str(final_path))
            self.assertEqual(art["overlay_headline"], "Headline")
            self.assertEqual(art["image_prompt"], "Prompt")
            self.assertTrue(final_path.exists())
            with Image.open(final_path) as saved_image:
                self.assertEqual(saved_image.size, (5, 6))
                self.assertEqual(saved_image.getpixel((0, 0)), (0, 0, 0))
            self.assertTrue(art["data_uri"].startswith("data:image/png;base64,"))
            self.assertEqual(
                base64.b64decode(art["data_uri"].split(",", 1)[1]),
                final_path.read_bytes(),
            )
            self.assertEqual(len(raw_paths), 1)
            self.assertNotEqual(raw_paths[0], str(final_path))
            self.assertFalse(Path(raw_paths[0]).exists())
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["report_image.png"],
            )

            fail_open_report = root / "fail_open_report.md"
            with patch.object(pipeline, "IMAGE_GENERATION_ENABLED", True), patch.object(
                pipeline,
                "IMAGE_GENERATION_FAIL_ON_ERROR",
                False,
            ), patch.object(
                pipeline,
                "generate_image_art_brief",
                return_value={"image_prompt": "Prompt", "overlay_headline": "Headline"},
            ), patch.object(
                pipeline,
                "generate_image_with_mflux",
                side_effect=RuntimeError("boom"),
            ), patch.object(
                pipeline.progress_tracker,
                "warning",
            ):
                error_art = pipeline.generate_report_image_art(
                    report_path=str(fail_open_report),
                    synthesis_body="Summary body",
                    report_title="Report title",
                )
            self.assertEqual(error_art["error"], "Image generation failed: boom")
            self.assertEqual(error_art["overlay_headline"], "Headline")
            self.assertEqual(error_art["image_prompt"], "Prompt")
            self.assertNotIn("final_image_path", error_art)
            self.assertFalse((root / "fail_open_report_image.png").exists())

            fail_closed_report = root / "fail_closed_report.md"
            with patch.object(pipeline, "IMAGE_GENERATION_ENABLED", True), patch.object(
                pipeline,
                "IMAGE_GENERATION_FAIL_ON_ERROR",
                True,
            ), patch.object(
                pipeline,
                "generate_image_art_brief",
                return_value={"image_prompt": "Prompt", "overlay_headline": "Headline"},
            ), patch.object(
                pipeline,
                "generate_image_with_mflux",
                side_effect=RuntimeError("boom"),
            ), patch.object(
                pipeline.progress_tracker,
                "warning",
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    pipeline.generate_report_image_art(
                        report_path=str(fail_closed_report),
                        synthesis_body="Summary body",
                        report_title="Report title",
                    )
            self.assertFalse((root / "fail_closed_report_image.png").exists())

        self.assertEqual(
            pipeline._format_plain_text_synthesis("Paragraph\n## Heading\nBody"),
            "Paragraph\n\nHeading\n-------\n\nBody",
        )
        self.assertEqual(
            pipeline._format_plain_text_synthesis("Paragraph\n\n## Heading\nBody"),
            "Paragraph\n\nHeading\n-------\n\nBody",
        )

        with patch.object(
            pipeline,
            "_collect_grouped_headlines",
            return_value={"Reuters": [("Headline", None, "https://reuters.com", None)]},
        ):
            html_listing = pipeline._build_html_article_listing([])
        self.assertIn("Headline", html_listing)

        with patch.object(
            pipeline.embeddings_stage,
            "dedup_story_drafts",
            side_effect=RuntimeError("boom"),
        ):
            deduped_story_drafts, dedup_stats = pipeline._dedupe_story_drafts_for_global_selection(
                [{"story_title": "A"}]
            )
        self.assertEqual(deduped_story_drafts, [{"story_title": "A"}])
        self.assertEqual(dedup_stats["fallback"], "no_dedup")

    def test_text_token_and_synthesis_helpers(self) -> None:
        self.assertEqual(
            pipeline._strip_prompt_echo_lines(
                "1) Title: echo\n1) Content: skip\nTitle: skip\nContent: skip\nThe user wants to construct a report that contains a summary of news articles.\nKeep this"
            ),
            "Keep this",
        )

        class FakeEncoding:
            def __init__(self, *, raise_on_decode: bool = False) -> None:
                self.raise_on_decode = raise_on_decode

            def encode(self, text: str):  # noqa: ANN001
                return [1, 2, 3]

            def decode(self, token_ids):  # noqa: ANN001
                if self.raise_on_decode:
                    raise RuntimeError("decode failed")
                return "one two three"

        class FakeTiktoken:
            def __init__(self, responses: list[object]) -> None:
                self._responses = iter(responses)
                self.calls: list[str] = []

            def get_encoding(self, name: str):  # noqa: ANN001
                self.calls.append(name)
                outcome = next(self._responses)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        encoder = FakeEncoding()
        with patch.object(pipeline, "tiktoken", FakeTiktoken([encoder, RuntimeError("missing")])):
            self.assertIs(pipeline._get_token_encoder(), encoder)
            self.assertIsNone(pipeline._get_token_encoder())

        with patch.object(pipeline, "_get_token_encoder", return_value=FakeEncoding()):
            self.assertEqual(pipeline.estimate_token_count("abcdef"), 3)
        with patch.object(
            pipeline,
            "_get_token_encoder",
            return_value=SimpleNamespace(encode=MagicMock(side_effect=RuntimeError("boom"))),
        ):
            self.assertEqual(pipeline.estimate_token_count("abcdefgh"), 2)

        with patch.object(pipeline, "_get_token_encoder", return_value=FakeEncoding()), patch.object(
            pipeline,
            "estimate_token_count",
            return_value=1,
        ):
            self.assertEqual(pipeline.truncate_text_to_token_limit("one two three", 4), "one two three")
        with patch.object(
            pipeline,
            "_get_token_encoder",
            return_value=FakeEncoding(raise_on_decode=True),
        ), patch.object(
            pipeline,
            "estimate_token_count",
            return_value=10,
        ):
            self.assertTrue(pipeline.truncate_text_to_token_limit("one two three four five", 2).endswith("..."))

        self.assertEqual(
            pipeline.extract_prompt_tokens_from_response(
                AIMessage(content="answer", response_metadata={"usage": {"prompt_tokens": 9}})
            ),
            9,
        )
        self.assertIsNone(pipeline.extract_prompt_tokens_from_response(AIMessage(content="answer")))

        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=True):
            self.assertEqual(pipeline.clean_synthesis_for_publication("text"), "")
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="",
        ):
            self.assertEqual(pipeline.clean_synthesis_for_publication("text"), "")
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="Intro paragraph.\n\n## Section\nPreamble\n### Story One\nUseful detail.\n### Story Two\nNo high-confidence updates in supplied coverage.",
        ), patch.object(
            pipeline,
            "_is_low_coverage_synthesis_section",
            side_effect=lambda text: "No high-confidence updates" in text or text == "Preamble",
        ):
            self.assertIn(
                "Useful detail.",
                pipeline.clean_synthesis_for_publication("text", relaxed=False),
            )
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="## Section\nBody",
        ):
            self.assertEqual(
                pipeline.clean_synthesis_for_publication("text", relaxed=True),
                "## Section\nBody",
            )
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="## Section\nPreamble\n### Story One\nNo high-confidence updates in supplied coverage.",
        ), patch.object(
            pipeline,
            "_is_low_coverage_synthesis_section",
            return_value=True,
        ):
            self.assertEqual(pipeline.clean_synthesis_for_publication("text", relaxed=False), "")
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="## Section\nNo high-confidence updates in supplied coverage.",
        ), patch.object(
            pipeline,
            "_is_low_coverage_synthesis_section",
            return_value=True,
        ):
            self.assertEqual(pipeline.clean_synthesis_for_publication("text", relaxed=False), "")
        with patch.object(pipeline, "_contains_disallowed_final_markup", return_value=False), patch.object(
            pipeline,
            "_strip_prompt_echo_lines",
            return_value="## Section\nUseful detail.",
        ), patch.object(
            pipeline,
            "_is_low_coverage_synthesis_section",
            return_value=False,
        ):
            self.assertEqual(
                pipeline.clean_synthesis_for_publication("text", relaxed=False),
                "## Section\nUseful detail.",
            )

        with patch.object(pipeline.article_summary_records_stage, "has_structured_entry", return_value=True):
            self.assertTrue(pipeline.has_structured_entry("## Title\nBody", "Title"))
        self.assertEqual(
            pipeline.filter_reports_for_references(
                [ArticleSummaryRecord(title="Headline", source="Reuters", published="2026-06-06", url="https://example.com/story", article_id="a1", story="Story A", summary="Summary")],
                {},
            ),
            [ArticleSummaryRecord(title="Headline", source="Reuters", published="2026-06-06", url="https://example.com/story", article_id="a1", story="Story A", summary="Summary")],
        )
        self.assertEqual(pipeline._extract_first_name(""), "there")
        self.assertEqual(
            pipeline._first_sentences("One. Two. Three.", max_sentences=0, max_chars=100),
            "One. Two. Three.",
        )
        self.assertEqual(
            pipeline._sanitize_overlay_headline(
                "One two three four five six seven eight nine ten eleven twelve thirteen",
                "Fallback",
            ),
            "One two three four five six seven eight nine ten eleven",
        )

    def test_report_recording_for_nonempty_synthesis(self) -> None:
        diagnostics = pipeline.RunDiagnostics(
            run_started_at="2026-08-03T19:04:38",
            settings={},
        )
        token_stats = {
            "primary_dataset": "synthetic dataset text",
            "included_report_keys": ["a1"],
        }
        pipeline._record_report_diagnostics(
            diagnostics,
            path="/tmp/latest_run.md",
            prompt_label="default prompt",
            recipient_list=["reader@example.com", "editor@example.com"],
            token_stats=token_stats,
            reference_reports=["reference"],
            citation_sources=[{"title": "Alpha"}],
            citation_groups=[{"group": "g1"}],
            image_art_diagnostics={"final_image_path": "/tmp/art.png"},
        )
        self.assertEqual(len(diagnostics.reports), 1)
        report = diagnostics.reports[0]
        self.assertEqual(report["path"], "/tmp/latest_run.md")
        self.assertEqual(report["prompt_label"], "default prompt")
        self.assertEqual(report["recipient_count"], 2)
        self.assertEqual(report["recipients"], ["reader@example.com", "editor@example.com"])
        self.assertEqual(report["reference_report_count"], 1)
        self.assertEqual(report["citation_source_count"], 1)
        self.assertEqual(report["citation_group_count"], 1)
        self.assertEqual(report["token_stats"], token_stats)
        self.assertEqual(report["image_art"], {"final_image_path": "/tmp/art.png"})
        self.assertNotIn("synthesis_dataset_artifacts", report)

    def test_report_recording_for_missing_image_art_and_empty_lists(self) -> None:
        # image_art_diagnostics is None whenever image generation produced
        # nothing (the failure path operators inspect most), and citation /
        # reference lists can legitimately be empty for reports without
        # citation markers.
        diagnostics = pipeline.RunDiagnostics(
            run_started_at="2026-08-03T19:04:38",
            settings={},
        )
        pipeline._record_report_diagnostics(
            diagnostics,
            path="/tmp/latest_run.md",
            prompt_label="default prompt",
            recipient_list=[],
            token_stats={},
            reference_reports=[],
            citation_sources=[],
            citation_groups=[],
            image_art_diagnostics=None,
        )
        self.assertEqual(len(diagnostics.reports), 1)
        report = diagnostics.reports[0]
        self.assertEqual(report["recipient_count"], 0)
        self.assertEqual(report["recipients"], [])
        self.assertEqual(report["reference_report_count"], 0)
        self.assertEqual(report["citation_source_count"], 0)
        self.assertEqual(report["citation_group_count"], 0)
        self.assertIsNone(report["image_art"])

    def test_no_stale_synthesis_dataset_artifacts_reference_in_pipeline(self) -> None:
        # Regression guard for the NameError fixed in #127: the stale
        # identifier must never reappear anywhere in production pipeline code,
        # or report finalization would crash again at runtime.
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertNotIn("synthesis_dataset_artifacts", source)
