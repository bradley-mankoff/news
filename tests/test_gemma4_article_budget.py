from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from news_pipeline.config import (
    CODEX_TEST_MODEL_ALIAS,
    CODEX_TEST_MODEL_NAME,
    DEFAULT_MODEL_ALIAS,
    GEMMA_4_ARTICLE_SUMMARY_CAP,
    configured_model_profile,
    is_gemma_4_model_reference,
    load_runtime_config,
)
from news_pipeline.pipeline import _budget_article_targets_for_summary


def _article(article_id: str) -> dict:
    return {
        "article_id": article_id,
        "title": f"Article {article_id}",
        "source": "Fixture Wire",
        "url": f"https://example.com/{article_id}",
    }


def _story(story_key: str, article_ids: list[str]) -> dict:
    return {
        "story_key": story_key,
        "story_title": f"Story {story_key}",
        "article_ids": article_ids,
        "cluster_article_ids": article_ids,
        "article_count": len(article_ids),
        "cluster_article_count": len(article_ids),
        "selected_article_count": len(article_ids),
    }


def _article_ids(count: int, *, prefix: str = "a") -> list[str]:
    return [f"{prefix}{index:02d}" for index in range(1, count + 1)]


class Gemma4ArticleBudgetTests(unittest.TestCase):
    def test_gemma_4_detection_uses_alias_and_resolved_name(self) -> None:
        self.assertTrue(is_gemma_4_model_reference(DEFAULT_MODEL_ALIAS))
        self.assertTrue(
            is_gemma_4_model_reference(
                "mlx-community/gemma-4-26B-A4B-it-heretic-4bit"
            )
        )
        self.assertTrue(is_gemma_4_model_reference(CODEX_TEST_MODEL_ALIAS))
        self.assertTrue(is_gemma_4_model_reference(CODEX_TEST_MODEL_NAME))
        self.assertFalse(is_gemma_4_model_reference("qwen-9b-dense"))

    def test_gemma_4_cap_is_40_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_runtime_config(materialize_outputs=False)

        self.assertTrue(is_gemma_4_model_reference(config.model_reference))
        self.assertEqual(
            config.model_profile.total_article_summary_cap,
            GEMMA_4_ARTICLE_SUMMARY_CAP,
        )

    def test_large_gemma_4_uses_article_parallelism_only(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL": DEFAULT_MODEL_ALIAS,
            },
            clear=True,
        ):
            config = load_runtime_config(materialize_outputs=False)

        self.assertEqual(config.article_summary_concurrency, 4)
        self.assertEqual(config.story_synthesis_concurrency, 1)
        self.assertEqual(config.model_concurrency, 4)

    def test_tiny_gemma_uses_higher_article_parallelism_and_story_synthesis_two(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
            },
            clear=True,
        ):
            config = load_runtime_config(materialize_outputs=False)

        self.assertEqual(config.article_summary_concurrency, 8)
        self.assertEqual(config.story_synthesis_concurrency, 2)
        self.assertEqual(config.model_concurrency, 8)

    def test_non_gemma_preserves_four_by_four_stage_concurrency(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL": "qwen-9b-dense",
            },
            clear=True,
        ):
            config = load_runtime_config(materialize_outputs=False)

        self.assertEqual(config.article_summary_concurrency, 4)
        self.assertEqual(config.story_synthesis_concurrency, 4)
        self.assertEqual(config.model_concurrency, 4)

    def test_stage_concurrency_env_vars_do_not_override_model_profile(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL": "qwen-9b-dense",
                "NEWS_ARTICLE_SUMMARY_CONCURRENCY": "3",
                "NEWS_STORY_SYNTHESIS_CONCURRENCY": "6",
            },
            clear=True,
        ):
            config = load_runtime_config(materialize_outputs=False)

        self.assertEqual(config.article_summary_concurrency, 4)
        self.assertEqual(config.story_synthesis_concurrency, 4)
        self.assertEqual(config.model_concurrency, 4)

    def test_non_gemma_keeps_profile_cap_and_explicit_override_wins(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            qwen_profile = configured_model_profile("qwen-9b-dense")
        self.assertEqual(qwen_profile.total_article_summary_cap, 36)

        with patch.dict(os.environ, {"NEWS_TOTAL_ARTICLE_SUMMARY_CAP": "55"}, clear=True):
            gemma_profile = configured_model_profile(DEFAULT_MODEL_ALIAS)
        self.assertEqual(gemma_profile.total_article_summary_cap, 55)

    def test_budget_keeps_all_articles_below_cap(self) -> None:
        article_ids = _article_ids(4)
        articles = [_article(article_id) for article_id in article_ids]
        stories = [_story("one", article_ids)]

        budgeted_articles, budgeted_stories, stats = _budget_article_targets_for_summary(
            articles,
            stories,
            total_cap=40,
            gemma_4_derived=True,
        )

        self.assertEqual([article["article_id"] for article in budgeted_articles], article_ids)
        self.assertEqual(budgeted_stories, stories)
        self.assertTrue(stats["enabled"])
        self.assertTrue(stats["gemma_4_derived"])
        self.assertEqual(stats["dropped_count"], 0)

    def test_budget_keeps_whole_ranked_stories_when_possible(self) -> None:
        first_story_ids = _article_ids(30, prefix="a")
        too_large_for_remaining_ids = _article_ids(15, prefix="b")
        fitting_story_ids = _article_ids(10, prefix="c")
        all_ids = first_story_ids + too_large_for_remaining_ids + fitting_story_ids
        articles = [_article(article_id) for article_id in all_ids]
        stories = [
            _story("first", first_story_ids),
            _story("skip", too_large_for_remaining_ids),
            _story("fits", fitting_story_ids),
        ]

        budgeted_articles, budgeted_stories, stats = _budget_article_targets_for_summary(
            articles,
            stories,
            total_cap=40,
            gemma_4_derived=True,
        )

        self.assertEqual(
            [article["article_id"] for article in budgeted_articles],
            first_story_ids + fitting_story_ids,
        )
        self.assertEqual([story["story_key"] for story in budgeted_stories], ["first", "fits"])
        self.assertEqual(stats["included_count"], 40)
        self.assertEqual(stats["dropped_count"], 15)
        self.assertEqual(stats["skipped_story_keys"], ["skip"])

    def test_budget_truncates_single_story_larger_than_cap(self) -> None:
        large_story_ids = _article_ids(45, prefix="a")
        articles = [_article(article_id) for article_id in large_story_ids]
        stories = [_story("large", large_story_ids)]

        budgeted_articles, budgeted_stories, stats = _budget_article_targets_for_summary(
            articles,
            stories,
            total_cap=40,
            gemma_4_derived=True,
        )

        self.assertEqual(
            [article["article_id"] for article in budgeted_articles],
            large_story_ids[:40],
        )
        self.assertEqual(budgeted_stories[0]["article_ids"], large_story_ids[:40])
        self.assertEqual(budgeted_stories[0]["article_count"], 40)
        self.assertEqual(stats["included_count"], 40)
        self.assertEqual(stats["dropped_count"], 5)


if __name__ == "__main__":
    unittest.main()
