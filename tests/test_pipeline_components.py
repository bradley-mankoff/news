from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.pipeline_component_fixtures import (
    ARTICLE_SUMMARIES,
    ARTICLE_TARGETS,
    CHIP_ARTICLE_TEXT,
    IMAGE_ART,
    NOW_UTC,
    RECIPIENT_NAMES,
    RECIPIENTS,
    RSS_FEED,
    SCRAPED_ARTICLE_TEXT,
    SYNTHESIS_BODY,
    TOP_FUNNEL_STORIES,
    TOPICS,
)

from news_pipeline.pipeline import (
    _extract_feed_items,
    _select_per_topic_feed_items,
    budget_article_targets,
    build_email_subject,
    build_report_body,
    build_report_html,
    build_top_funnel_article_targets_for_coverage_gaps,
    generate_image_with_mflux,
    gather_article_targets_for_source,
    maybe_email_report,
)


def fake_scrape(url: str) -> str:
    return CHIP_ARTICLE_TEXT if "chip" in url else SCRAPED_ARTICLE_TEXT


class FakeRSSResponse:
    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}
    text = RSS_FEED

    def raise_for_status(self) -> None:
        return None


class CapturingSMTP:
    instances: list["CapturingSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_args: tuple[str, str] | None = None
        self.messages = []
        CapturingSMTP.instances.append(self)

    def __enter__(self) -> "CapturingSMTP":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message) -> None:
        self.messages.append(message)


class PipelineComponentTests(unittest.TestCase):
    def test_email_subject_uses_fixed_daily_llm_news_format(self) -> None:
        from datetime import datetime

        self.assertEqual(
            build_email_subject(datetime(2026, 5, 17, 20, 25, 52)),
            "Daily LLM News, 05/17/26",
        )

    def test_feed_parsing_and_topic_selection_component(self) -> None:
        items = _extract_feed_items(RSS_FEED)
        selected = _select_per_topic_feed_items(items, TOPICS, now_utc=NOW_UTC)

        self.assertEqual(len(items), 3)
        self.assertEqual(
            {item["topic_key"] for item in selected},
            {"climate_resilience", "chip_exports"},
        )
        self.assertEqual(
            [item["title"] for item in selected],
            [
                "City expands flood defenses after river levee warnings",
                "New chip export controls target advanced AI accelerators",
            ],
        )
        self.assertGreaterEqual(selected[0]["relevance_score"], TOPICS[0]["min_score"])

    def test_source_article_target_gathering_component(self) -> None:
        source_feeds = {
            "Fixture Wire": {
                "name": "Fixture Wire",
                "url": "https://example.com/rss.xml",
                "homepage": "https://example.com/",
            }
        }
        with patch("news_pipeline.pipeline.SOURCE_FEEDS", source_feeds):
            with patch("news_pipeline.pipeline.requests.get", return_value=FakeRSSResponse()):
                with patch("news_pipeline.pipeline._resolve_google_news_url", side_effect=lambda url: url):
                    with patch("news_pipeline.pipeline.web_scrape", side_effect=fake_scrape):
                        with patch(
                            "news_pipeline.pipeline._translate_if_needed",
                            side_effect=lambda text, title="": text,
                        ):
                            with patch(
                                "news_pipeline.pipeline._is_within_recent_window",
                                return_value=True,
                            ):
                                targets, new_urls, source_run = gather_article_targets_for_source(
                                    "Fixture Wire",
                                    TOPICS,
                                    seen_urls=set(),
                                    run_seen_urls=set(),
                                )

        self.assertEqual(source_run["status"], "ok")
        self.assertEqual(source_run["feed_item_count"], 3)
        self.assertEqual(source_run["selected_item_count"], 2)
        self.assertEqual(len(targets), 2)
        self.assertEqual(
            [target["topic_key"] for target in targets],
            ["climate_resilience", "chip_exports"],
        )
        self.assertEqual(
            new_urls,
            ["https://example.com/climate-levee", "https://example.com/chip-controls"],
        )
        self.assertIn("levee", targets[0]["text"].lower())

    def test_duplicate_and_history_filtering_component(self) -> None:
        direct_context = {
            "status": "ok",
            "feed_item_count": 5,
            "selected_item_count": 5,
            "selected_items": [],
            "selected_by_topic": {"Climate Resilience": 5},
            "articles": [
                {**ARTICLE_TARGETS[0], "url": "https://example.com/fresh"},
                {**ARTICLE_TARGETS[0], "url": "https://example.com/fresh"},
                {**ARTICLE_TARGETS[0], "url": "https://example.com/history"},
                {**ARTICLE_TARGETS[0], "url": ""},
            ],
        }

        with patch("news_pipeline.pipeline.SHARED_URL_HISTORY_ENABLED", True):
            with patch("news_pipeline.pipeline.get_direct_source_context", return_value=direct_context):
                targets, new_urls, source_run = gather_article_targets_for_source(
                    "Fixture Wire",
                    TOPICS,
                    seen_urls={"https://example.com/history"},
                    run_seen_urls=set(),
                )

        self.assertEqual(len(targets), 1)
        self.assertEqual(new_urls, ["https://example.com/fresh"])
        self.assertEqual(source_run["rejected_counts"]["duplicate_this_run"], 1)
        self.assertEqual(source_run["rejected_counts"]["seen_in_history"], 1)
        self.assertEqual(source_run["rejected_counts"]["missing_url"], 1)

    def test_history_filtering_can_be_disabled_for_local_prod_review(self) -> None:
        direct_context = {
            "status": "ok",
            "feed_item_count": 1,
            "selected_item_count": 1,
            "selected_items": [],
            "selected_by_topic": {"Climate Resilience": 1},
            "articles": [
                {**ARTICLE_TARGETS[0], "url": "https://example.com/history"},
            ],
        }

        with patch("news_pipeline.pipeline.SHARED_URL_HISTORY_ENABLED", False):
            with patch("news_pipeline.pipeline.get_direct_source_context", return_value=direct_context):
                targets, new_urls, source_run = gather_article_targets_for_source(
                    "Fixture Wire",
                    TOPICS,
                    seen_urls={"https://example.com/history"},
                    run_seen_urls=set(),
                )

        self.assertEqual(len(targets), 1)
        self.assertEqual(new_urls, ["https://example.com/history"])
        self.assertEqual(source_run["rejected_counts"]["seen_in_history"], 0)

    def test_mflux_generation_uses_current_python_module_entrypoint(self) -> None:
        captured_command: list[str] = []

        def fake_run(command: list[str], **kwargs) -> None:
            captured_command[:] = command
            output_path = Path(command[command.index("--output") + 1])
            from PIL import Image

            Image.new("RGB", (1, 1), (255, 255, 255)).save(output_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "raw.png"
            with patch("news_pipeline.pipeline.importlib.util.find_spec", return_value=object()):
                with patch("news_pipeline.pipeline.subprocess.run", side_effect=fake_run):
                    generate_image_with_mflux(
                        "documentary scene",
                        output_path=str(output_path),
                        seed=123,
                    )

        self.assertEqual(
            captured_command[:3],
            [sys.executable, "-m", "mflux.models.flux2.cli.flux2_generate"],
        )

    def test_article_budget_component(self) -> None:
        candidates = [
            *ARTICLE_TARGETS,
            {
                **ARTICLE_TARGETS[0],
                "article_id": "Fixture Wire-climate_resilience-2",
                "url": "https://example.com/extra-climate",
                "relevance_score": 3,
            },
        ]

        selected, stats = budget_article_targets(
            candidates,
            TOPICS,
            total_cap=2,
            per_topic_cap=1,
            per_source_topic_cap=1,
        )

        self.assertEqual(len(selected), 2)
        self.assertEqual({target["topic_key"] for target in selected}, {"climate_resilience", "chip_exports"})
        self.assertEqual(stats["included_count"], 2)
        self.assertEqual(stats["dropped_count"], 2)
        self.assertIn("Fixture Wire-climate_resilience-2", stats["dropped_article_ids"])

    def test_top_funnel_coverage_fallback_component(self) -> None:
        existing_targets = [ARTICLE_TARGETS[1]]
        with patch("news_pipeline.pipeline.DEV", False):
            with patch("news_pipeline.pipeline._resolve_google_news_url", side_effect=lambda url: url):
                with patch("news_pipeline.pipeline.web_scrape", side_effect=fake_scrape):
                    with patch(
                        "news_pipeline.pipeline._translate_if_needed",
                        side_effect=lambda text, title="": text,
                    ):
                        targets, new_urls, stats = build_top_funnel_article_targets_for_coverage_gaps(
                            TOPICS,
                            TOP_FUNNEL_STORIES,
                            existing_targets,
                            seen_urls=set(),
                            run_seen_urls=set(),
                        )

        self.assertEqual(stats["added_count"], 1)
        self.assertEqual(stats["filled_topics"], {"Climate Resilience": 1})
        self.assertEqual(new_urls, ["https://example.com/top-climate"])
        self.assertEqual(targets[0]["topic_key"], "climate_resilience")
        self.assertTrue(targets[0]["coverage_fallback"])

    def test_report_rendering_component(self) -> None:
        report_body = build_report_body(
            "Fixture Daily Brief",
            SYNTHESIS_BODY,
            ARTICLE_SUMMARIES,
            TOPICS,
            IMAGE_ART,
        )
        report_html = build_report_html(
            RECIPIENTS[0],
            RECIPIENT_NAMES[0],
            "Fixture Daily Brief",
            SYNTHESIS_BODY,
            ARTICLE_SUMMARIES,
            TOPICS,
            IMAGE_ART,
        )

        self.assertIn("Fixture Daily Brief", report_body)
        self.assertIn("IMAGE", report_body)
        self.assertIn("ARTICLES BY SOURCE", report_body)
        self.assertIn("Climate Resilience", report_body)
        self.assertIn("<h1", report_html)
        self.assertIn("Fixture Daily Brief", report_html)
        self.assertIn("width=device-width", report_html)
        self.assertIn("email-content", report_html)
        self.assertIn("data:image/png;base64", report_html)
        self.assertIn("Unsubscribe", report_html)

    def test_email_mime_construction_component_uses_patched_smtp(self) -> None:
        CapturingSMTP.instances = []
        with patch("news_pipeline.pipeline.EMAIL_FROM", "sender@example.com"):
            with patch("news_pipeline.pipeline.SMTP_HOST", "smtp.example.com"):
                with patch("news_pipeline.pipeline.SMTP_PORT", 465):
                    with patch("news_pipeline.pipeline.SMTP_USERNAME", "sender@example.com"):
                        with patch("news_pipeline.pipeline.SMTP_PASSWORD", "secret"):
                            with patch("news_pipeline.pipeline.SMTP_USE_SSL", True):
                                with patch("news_pipeline.pipeline.smtplib.SMTP_SSL", CapturingSMTP):
                                    with redirect_stdout(StringIO()):
                                        maybe_email_report(
                                            "Fixture Daily Brief",
                                            build_report_body(
                                                "Fixture Daily Brief",
                                                SYNTHESIS_BODY,
                                                ARTICLE_SUMMARIES,
                                                TOPICS,
                                            ),
                                            SYNTHESIS_BODY,
                                            ARTICLE_SUMMARIES,
                                            TOPICS,
                                            RECIPIENTS,
                                            RECIPIENT_NAMES,
                                            None,
                                        )

        smtp = CapturingSMTP.instances[0]
        self.assertEqual(smtp.login_args, ("sender@example.com", "secret"))
        self.assertEqual(len(smtp.messages), 1)
        message = smtp.messages[0]
        self.assertEqual(message["Subject"], build_email_subject())
        self.assertEqual(message["To"], RECIPIENTS[0])
        self.assertIn("List-Unsubscribe", message)
        self.assertTrue(message.is_multipart())
        self.assertIn("text/html", [part.get_content_type() for part in message.walk()])


if __name__ == "__main__":
    unittest.main()
