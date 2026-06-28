from __future__ import annotations

import unittest
from unittest.mock import patch

from news_pipeline.story_clustering import organize_article_targets_into_global_stories


def _article(article_id: str, source: str, title_suffix: str) -> dict:
    return {
        "article_id": article_id,
        "source": source,
        "title": f"Port strike talks resume {title_suffix}",
        "pub_date": f"Mon, 01 Jun 2026 12:0{title_suffix}:00 GMT",
        "url": f"https://example.com/{article_id.lower()}",
        "description": (
            "Trade officials said the port strike talks resumed after a maritime cargo "
            "disruption."
        ),
        "text": (
            "Trade officials said the port strike talks resumed after a maritime cargo "
            "disruption. Negotiators discussed shipping lanes, port workers, cargo delays, "
            "and emergency logistics."
        ),
        "relevance_score": 100 - int(title_suffix),
    }


class StoryClusteringTests(unittest.TestCase):
    def test_story_cluster_caps_articles_from_same_source(self) -> None:
        articles = [
            _article("Yonhap-1", "Yonhap", "1"),
            _article("Yonhap-2", "Yonhap", "2"),
            _article("Yonhap-3", "Yonhap", "3"),
            _article("Yonhap-4", "Yonhap", "4"),
            _article("Reuters-1", "Reuters", "5"),
            _article("AP-1", "AP", "6"),
        ]

        with patch(
            "news_pipeline.story_clustering.STORY_MAX_ARTICLES_PER_SOURCE_PER_STORY",
            2,
        ):
            selected_targets, story_records, stats = organize_article_targets_into_global_stories(
                articles,
                min_articles_per_story=2,
                similarity_threshold=0.05,
            )

        self.assertEqual(stats["max_articles_per_source_per_story"], 2)
        self.assertTrue(story_records)

        for story in story_records:
            yonhap_article_ids = [
                article_id
                for article_id in story["cluster_article_ids"]
                if article_id.startswith("Yonhap-")
            ]
            self.assertLessEqual(len(yonhap_article_ids), 2, story)

        matching_stories = [
            story
            for story in story_records
            if {
                article_id
                for article_id in story["cluster_article_ids"]
                if not article_id.startswith("Yonhap-")
            } == {"Reuters-1", "AP-1"}
            and {
                article_id
                for article_id in story["cluster_article_ids"]
                if article_id.startswith("Yonhap-")
            } == {"Yonhap-1", "Yonhap-2"}
        ]
        self.assertTrue(matching_stories)

        main_story = matching_stories[0]
        self.assertEqual(main_story["article_count"], 4)
        self.assertEqual(main_story["cluster_article_count"], 4)
        self.assertEqual(main_story["selected_article_count"], 4)
        self.assertEqual(
            set(main_story["cluster_article_ids"]),
            {"Yonhap-1", "Yonhap-2", "Reuters-1", "AP-1"},
        )
        self.assertTrue(
            any(article_id.startswith("Yonhap-") for article_id in main_story["pruned_article_ids"]),
            main_story,
        )

        self.assertTrue(selected_targets)


if __name__ == "__main__":
    unittest.main()
