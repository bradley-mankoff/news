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
    _source_match_result_for_feed_item,
    budget_article_targets,
    build_email_subject,
    build_report_body,
    build_report_html,
    build_top_funnel_article_targets_for_coverage_gaps,
    generate_image_with_mflux,
    gather_article_candidates_for_source,
    gather_article_targets_for_source,
    maybe_email_report,
)


def fake_scrape(url: str) -> str:
    if "chip" in url:
        return CHIP_ARTICLE_TEXT
    if "sports" in url:
        return ""
    return SCRAPED_ARTICLE_TEXT


class FakeRSSResponse:
    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}
    text = RSS_FEED

    def raise_for_status(self) -> None:
        return None


class RSSResponse:
    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}

    def __init__(self, text: str) -> None:
        self.text = text

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
                "New chip export controls advanced AI accelerators",
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
                    with patch(
                        "news_pipeline.pipeline.scrape_article_text",
                        side_effect=lambda url, **kwargs: (fake_scrape(url), "scraped"),
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

    def test_exact_wire_feed_label_is_accepted_without_body_attribution(self) -> None:
        result = _source_match_result_for_feed_item(
            "AP News",
            {
                "name": "Associated Press",
                "strict_source_match": True,
                "source_match_mode": "wire_attribution",
                "source_match_aliases": ["AP", "AP News", "Associated Press"],
            },
            {
                "title": "Magic finalize coach deal, AP source says - AP News",
                "source": "AP News",
                "link": "https://news.google.com/rss/articles/example",
            },
        )

        self.assertTrue(result["accepted"])
        self.assertFalse(result["pending_wire_attribution"])
        self.assertEqual(result["source_match_status"], "feed_label_confirmed")

    def test_non_wire_strict_source_rejects_mismatched_feed_label_pre_scrape(self) -> None:
        result = _source_match_result_for_feed_item(
            "SCMP",
            {
                "name": "South China Morning Post",
                "strict_source_match": True,
                "source_match_aliases": ["SCMP", "South China Morning Post"],
            },
            {
                "title": "Regional tensions rise - Affiliate News",
                "source": "Affiliate News",
                "link": "https://news.google.com/rss/articles/example",
            },
        )

        self.assertFalse(result["accepted"])
        self.assertFalse(result["pending_wire_attribution"])
        self.assertEqual(result["reason"], "wrong_feed_source")

    def test_wire_attributed_affiliate_item_is_accepted_after_scrape(self) -> None:
        feed_xml = """
        <rss><channel>
          <item>
            <title>US disables ship heading to Iran - KING5.com</title>
            <link>https://example.com/ap-affiliate</link>
            <source>KING5.com</source>
            <pubDate>Sat, 30 May 2026 18:00:00 GMT</pubDate>
            <description>US officials reported the ship was disabled.</description>
          </item>
        </channel></rss>
        """
        source_feeds = {
            "AP News": {
                "name": "Associated Press",
                "url": "https://example.com/ap.xml",
                "strict_source_match": True,
                "source_match_mode": "wire_attribution",
                "source_match_aliases": ["AP", "AP News", "Associated Press"],
            }
        }

        with patch("news_pipeline.pipeline.SOURCE_FEEDS", source_feeds):
            with patch("news_pipeline.pipeline.requests.get", return_value=RSSResponse(feed_xml)):
                with patch(
                    "news_pipeline.pipeline.scrape_article_text",
                    return_value=(
                        "By The Associated Press\n"
                        "The United States disabled a commercial ship in the Gulf of Oman.",
                        "scraped",
                    ),
                ):
                    with patch("news_pipeline.pipeline._is_within_recent_window", return_value=True):
                        targets, new_urls, source_run = gather_article_candidates_for_source(
                            "AP News",
                            seen_urls=set(),
                            run_seen_urls=set(),
                        )

        self.assertEqual(new_urls, ["https://example.com/ap-affiliate"])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["source"], "AP News")
        self.assertEqual(targets[0]["source_match_status"], "wire_attribution_confirmed")
        self.assertEqual(targets[0]["publisher_source"], "KING5.com")
        self.assertEqual(targets[0]["wire_source"], "Associated Press")
        self.assertEqual(targets[0]["source_display_name"], "Associated Press via KING5.com")
        self.assertEqual(source_run["selected_items"][0]["source_match_status"], "wire_attribution_confirmed")

    def test_wire_title_only_ap_source_phrase_is_not_attribution(self) -> None:
        feed_xml = """
        <rss><channel>
          <item>
            <title>Magic finalizing coach deal, AP source says - KING5.com</title>
            <link>https://example.com/ap-source-says</link>
            <source>KING5.com</source>
            <pubDate>Sat, 30 May 2026 18:00:00 GMT</pubDate>
            <description>The team is finalizing a deal.</description>
          </item>
        </channel></rss>
        """
        source_feeds = {
            "AP News": {
                "name": "Associated Press",
                "url": "https://example.com/ap.xml",
                "strict_source_match": True,
                "source_match_mode": "wire_attribution",
                "source_match_aliases": ["AP", "AP News", "Associated Press"],
            }
        }

        with patch("news_pipeline.pipeline.SOURCE_FEEDS", source_feeds):
            with patch("news_pipeline.pipeline.requests.get", return_value=RSSResponse(feed_xml)):
                with patch(
                    "news_pipeline.pipeline.scrape_article_text",
                    return_value=(
                        "The team is finalizing a deal after an AP source says talks advanced.",
                        "scraped",
                    ),
                ):
                    with patch("news_pipeline.pipeline._is_within_recent_window", return_value=True):
                        targets, _new_urls, source_run = gather_article_candidates_for_source(
                            "AP News",
                            seen_urls=set(),
                            run_seen_urls=set(),
                        )

        self.assertEqual(targets, [])
        self.assertEqual(source_run["rejected_counts"]["wrong_feed_source_unattributed"], 1)
        self.assertEqual(source_run["scrape_attempts"][0]["source_match_status"], "wrong_feed_source_unattributed")

    def test_pending_wire_attribution_rejects_feed_fallback_without_body(self) -> None:
        feed_xml = """
        <rss><channel>
          <item>
            <title>US disables ship heading to Iran - KING5.com</title>
            <link>https://example.com/ap-access-denied</link>
            <source>KING5.com</source>
            <pubDate>Sat, 30 May 2026 18:00:00 GMT</pubDate>
            <description>By The Associated Press. US officials reported the ship was disabled.</description>
          </item>
        </channel></rss>
        """
        source_feeds = {
            "AP News": {
                "name": "Associated Press",
                "url": "https://example.com/ap.xml",
                "strict_source_match": True,
                "source_match_mode": "wire_attribution",
                "source_match_aliases": ["AP", "AP News", "Associated Press"],
            }
        }

        with patch("news_pipeline.pipeline.SOURCE_FEEDS", source_feeds):
            with patch("news_pipeline.pipeline.requests.get", return_value=RSSResponse(feed_xml)):
                with patch(
                    "news_pipeline.pipeline.scrape_article_text",
                    return_value=("Access Denied.", "access_denied"),
                ):
                    with patch("news_pipeline.pipeline._is_within_recent_window", return_value=True):
                        targets, _new_urls, source_run = gather_article_candidates_for_source(
                            "AP News",
                            seen_urls=set(),
                            run_seen_urls=set(),
                        )

        self.assertEqual(targets, [])
        self.assertEqual(source_run["rejected_counts"]["wrong_feed_source_unattributed"], 1)
        self.assertEqual(source_run["scrape_attempts"][0]["scrape_status"], "access_denied_feed_fallback")

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
                with patch(
                    "news_pipeline.pipeline.scrape_article_text",
                    side_effect=lambda url, **kwargs: (fake_scrape(url), "scraped"),
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
