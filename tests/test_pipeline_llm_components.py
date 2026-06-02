from __future__ import annotations

import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from tests.pipeline_component_fixtures import (
    ARTICLE_SUMMARIES,
    ARTICLE_SUMMARIES_CLIMATE_PAIR,
    ARTICLE_TARGETS,
    CHIP_ARTICLE_TEXT,
    NOW_UTC,
    RSS_FEED,
    SCRAPED_ARTICLE_TEXT,
    SYNTHESIS_BODY,
    TOP_FUNNEL_STORIES,
    TOPICS,
)

from news_pipeline.pipeline import (
    MODEL_CALL_STATS,
    MODEL_CALL_STATS_LOCK,
    _extract_feed_items,
    _select_per_topic_feed_items,
    _story_topic_runtime,
    budget_article_targets,
    build_report_body,
    build_top_funnel_article_targets_for_coverage_gaps,
    generate_image_art_brief,
    generate_report_title,
    llm_cluster_top_topics,
    managed_model_server,
    run_article_summary_pass,
    run_per_story_synthesis,
)
from news_pipeline.config import load_predefined_topics
from news_pipeline.story_topic_assignment import classify_story_drafts_for_topics


def fake_scrape(url: str) -> str:
    return CHIP_ARTICLE_TEXT if "chip" in url else SCRAPED_ARTICLE_TEXT


def _reset_model_call_stats() -> None:
    with MODEL_CALL_STATS_LOCK:
        MODEL_CALL_STATS["calls"] = {}
        MODEL_CALL_STATS["retries"] = 0
        MODEL_CALL_STATS["fallbacks"] = 0
        MODEL_CALL_STATS["failures"] = {}


def _model_call_stats_snapshot() -> dict:
    with MODEL_CALL_STATS_LOCK:
        return {
            "calls": dict(MODEL_CALL_STATS.get("calls") or {}),
            "retries": MODEL_CALL_STATS.get("retries", 0),
            "fallbacks": MODEL_CALL_STATS.get("fallbacks", 0),
            "failures": dict(MODEL_CALL_STATS.get("failures") or {}),
        }


class PipelineLLMComponentTests(unittest.TestCase):
    server_context = None

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("NEWS_TEST_LOCAL_LLM") != "1":
            raise unittest.SkipTest("Set NEWS_TEST_LOCAL_LLM=1 to run local-LLM component probes.")
        cls.server_context = managed_model_server()
        cls.server_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server_context is not None:
            cls.server_context.__exit__(None, None, None)
            cls.server_context = None

    def setUp(self) -> None:
        _reset_model_call_stats()

    def assertNoModelFallbacks(self) -> None:
        stats = _model_call_stats_snapshot()
        self.assertEqual(stats["fallbacks"], 0, stats)
        self.assertEqual(stats["failures"], {}, stats)

    def test_dynamic_topic_clustering_component(self) -> None:
        with redirect_stdout(StringIO()):
            topics = llm_cluster_top_topics(TOP_FUNNEL_STORIES, num_topics=2, probes_each=2)

        self.assertGreaterEqual(len(topics), 1)
        self.assertTrue(all(topic.get("title") for topic in topics))
        self.assertTrue(all(topic.get("keywords") for topic in topics))
        self.assertNoModelFallbacks()

    @unittest.skip("_translate_if_needed removed in disaggregation; translation now batched via translate_article_candidates")
    def test_translation_component(self) -> None:
        pass

    def test_article_summary_component(self) -> None:
        with patch("news_pipeline.pipeline.ARTICLE_SUMMARY_CONCURRENCY", 1):
            with redirect_stdout(StringIO()):
                summaries = run_article_summary_pass([ARTICLE_TARGETS[0]], TOPICS)

        self.assertEqual(len(summaries), 1)
        self.assertIn("### City expands flood defenses", summaries[0])
        self.assertIn("Summary:", summaries[0])
        self.assertIn("Climate Resilience", summaries[0])
        self.assertNoModelFallbacks()

    def test_story_synthesis_component(self) -> None:
        story_records = [
            {
                "topic_title": "Climate Resilience",
                "story_title": "Cities act on flood defenses and heat preparedness",
                "cluster_article_ids": [
                    "Fixture Wire-climate_resilience-1",
                    "Second Source-climate_resilience-1",
                ],
            }
        ]
        with redirect_stdout(StringIO()):
            synthesis = run_per_story_synthesis(ARTICLE_SUMMARIES_CLIMATE_PAIR, story_records, TOPICS[:1])

        self.assertIsInstance(synthesis, str)
        self.assertGreater(len(synthesis.split()), 20)
        self.assertIn("climate", synthesis.lower())
        self.assertNoModelFallbacks()

    def test_story_topic_validation_rejects_plain_south_korea_for_us_economy(self) -> None:
        topics = load_predefined_topics(topic_ids=["us_economy"])
        topic_title = topics[0]["title"]
        story_drafts = [
            {
                "story_key": "powell-fed-independence",
                "topic_key": "us_economy",
                "topic_title": topic_title,
                "story_title": "Powell uses JFK award speech to defend Fed from political pressure",
                "paragraph": (
                    "Federal Reserve Chair Jerome Powell used a public award speech to defend the Fed's "
                    "independence from White House pressure, warning that politicized interest-rate "
                    "decisions would damage public trust in the central bank. The story centers on Fed "
                    "policy, US interest rates, and broad implications for American households, borrowers, "
                    "businesses, investors, and US financial markets."
                ),
                "summaries": [
                    "Powell defended Federal Reserve independence from White House pressure and linked "
                    "politicized rate decisions to risks for US households, businesses, and markets."
                ],
                "article_ids": ["fed-1", "fed-2", "fed-3", "fed-4"],
                "cluster_article_ids": ["fed-1", "fed-2", "fed-3", "fed-4"],
                "article_count": 4,
                "source_count": 4,
                "story_strength_score": 2.0,
                "average_similarity": 0.8,
            },
            {
                "story_key": "seoul-shares-ai-optimism",
                "topic_key": "us_economy",
                "topic_title": topic_title,
                "story_title": "South Korean Stocks Rally on AI Tech Optimism",
                "paragraph": (
                    "South Korean stocks reached a new high as investor interest in technology and "
                    "artificial intelligence drove market gains. The Korea Composite Stock Price Index "
                    "(KOSPI) climbed after optimism that the AI-driven semiconductor boom would persist. "
                    "South Korean exports rose 53 percent from the prior year to a monthly high of "
                    "US$87.8 billion, supported by the semiconductor supercycle. Robot and AI-related "
                    "stocks also gained before Nvidia CEO Jensen Huang's visit to South Korea."
                ),
                "summaries": [
                    "Seoul shares and the KOSPI rallied on AI and semiconductor optimism, while South "
                    "Korean exports reached US$87.8 billion and Nvidia CEO Jensen Huang prepared to "
                    "visit local executives."
                ],
                "article_ids": ["seoul-1", "seoul-2", "seoul-3", "seoul-4"],
                "cluster_article_ids": ["seoul-1", "seoul-2", "seoul-3", "seoul-4"],
                "article_count": 4,
                "source_count": 4,
                "story_strength_score": 3.0,
                "average_similarity": 0.9,
            },
        ]

        with redirect_stdout(StringIO()):
            selected, stats = classify_story_drafts_for_topics(
                story_drafts,
                topics,
                _story_topic_runtime(),
            )

        selected_keys = {story["story_key"] for story in selected}
        self.assertIn("powell-fed-independence", selected_keys, stats)
        self.assertNotIn("seoul-shares-ai-optimism", selected_keys, stats)
        rejected = stats["topics"][topic_title]["rejected"]
        korea_rejection = next(
            record for record in rejected if record["story_key"] == "seoul-shares-ai-optimism"
        )
        self.assertEqual(
            korea_rejection["topic_screening_topicality"],
            "obviously_not_topical",
            stats,
        )
        self.assertEqual(korea_rejection["reason"], "screened_out_by_story_topic_screening")
        self.assertNoModelFallbacks()

    def test_title_generation_component(self) -> None:
        with redirect_stdout(StringIO()):
            title = generate_report_title(SYNTHESIS_BODY, "2026-05-16_18-00-00")

        self.assertIsInstance(title, str)
        self.assertGreaterEqual(len(title.split()), 2)
        self.assertLessEqual(len(title), 120)
        self.assertNoModelFallbacks()

    def test_image_art_prompt_generation_component_uses_text_llm_only(self) -> None:
        with redirect_stdout(StringIO()):
            brief = generate_image_art_brief(SYNTHESIS_BODY, "Fixture Daily Brief")

        self.assertIn("image_prompt", brief)
        self.assertIn("overlay_headline", brief)
        self.assertNotIn("error", brief)
        self.assertIn("no text", brief["image_prompt"].lower())
        self.assertIn("readable headline will be rendered later by code", brief["image_prompt"].lower())
        self.assertLessEqual(len(brief["overlay_headline"].split()), 11)
        self.assertNoModelFallbacks()

    def test_staged_sample_pipeline_component(self) -> None:
        items = _extract_feed_items(RSS_FEED)
        selected_items = _select_per_topic_feed_items(items, TOPICS[:1], now_utc=NOW_UTC)
        self.assertEqual(len(selected_items), 1)

        article_target = {
            "article_id": "Fixture Wire-climate_resilience-1",
            "source": "Fixture Wire",
            "title": selected_items[0]["title"],
            "pub_date": selected_items[0]["pub_date"],
            "url": selected_items[0]["link"],
            "description": selected_items[0]["description"],
            "text": SCRAPED_ARTICLE_TEXT,
            "topic_key": selected_items[0]["topic_key"],
            "topic_title": selected_items[0]["topic_title"],
            "relevance_score": selected_items[0]["relevance_score"],
        }
        with patch("news_pipeline.pipeline._resolve_google_news_url", side_effect=lambda url: url):
            with patch("news_pipeline.pipeline.web_scrape", side_effect=fake_scrape):
                fallback_targets, _, fallback_stats = build_top_funnel_article_targets_for_coverage_gaps(
                    TOPICS,
                    TOP_FUNNEL_STORIES,
                    [article_target],
                    seen_urls=set(),
                    run_seen_urls=set(),
                )
        self.assertGreaterEqual(fallback_stats["added_count"], 1)

        budgeted, budget_stats = budget_article_targets(
            [article_target, *fallback_targets],
            TOPICS,
            total_cap=2,
            per_topic_cap=1,
            per_source_topic_cap=1,
        )
        self.assertEqual(budget_stats["included_count"], 2)

        with patch("news_pipeline.pipeline.ARTICLE_SUMMARY_CONCURRENCY", 1):
            with patch("news_pipeline.pipeline._resolve_google_news_url", side_effect=lambda url: url):
                with patch("news_pipeline.pipeline.web_scrape", side_effect=fake_scrape):
                    with redirect_stdout(StringIO()):
                        summaries = run_article_summary_pass(budgeted, TOPICS)

        story_records = [
            {
                "topic_title": article.get("topic_title") or "News",
                "story_title": article.get("title") or "News update",
                "cluster_article_ids": [article.get("article_id")],
            }
            for article in budgeted
        ]
        with redirect_stdout(StringIO()):
            synthesis = run_per_story_synthesis(summaries, story_records, TOPICS, min_articles_per_story=1)

        self.assertIsInstance(synthesis, str)
        self.assertGreater(len(synthesis), 0, "Expected non-empty synthesis from per-story drafting")

        report_body = build_report_body("Fixture Daily Brief", synthesis, summaries, TOPICS, None)

        self.assertEqual(len(summaries), 2)
        self.assertIn("ARTICLES BY SOURCE", report_body)
        self.assertIn("Climate Resilience", report_body)
        self.assertIn("Chip Export Controls", report_body)
        self.assertNoModelFallbacks()


if __name__ == "__main__":
    unittest.main()
