from __future__ import annotations

import importlib
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import news_pipeline.story_clustering as sc


def _article(
    article_id: str,
    source: str,
    title: str,
    text: str,
    *,
    description: str | None = None,
    pub_date: str = "Mon, 01 Jun 2026 12:00:00 GMT",
    relevance_score: int = 1,
) -> dict[str, object]:
    return {
        "article_id": article_id,
        "source": source,
        "title": title,
        "description": description or title,
        "text": text,
        "pub_date": pub_date,
        "url": f"https://example.com/{article_id}",
        "relevance_score": relevance_score,
    }


class StoryClusteringTests(unittest.TestCase):
    def test_env_and_text_helpers_and_reload_branches(self) -> None:
        original_module = sc
        try:
            with patch.dict(
                os.environ,
                {
                    "NEWS_MIN_ARTICLES_PER_STORY": "bad",
                    "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD": "bad",
                    "NEWS_STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD": "bad",
                },
                clear=False,
            ):
                reloaded = importlib.reload(original_module)

            self.assertEqual(reloaded.MIN_ARTICLES_PER_STORY, 2)
            self.assertEqual(reloaded.STORY_CLUSTER_SIMILARITY_THRESHOLD, 0.30)
            self.assertEqual(reloaded.STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD, 0.34)
            self.assertEqual(reloaded._int_env("NEWS_MISSING", 7), 7)
            with patch.dict(os.environ, {"NEWS_INT_VALUE": "bad"}, clear=False):
                self.assertEqual(reloaded._int_env("NEWS_INT_VALUE", 7), 7)
            with patch.dict(os.environ, {"NEWS_INT_VALUE": "11"}, clear=False):
                self.assertEqual(reloaded._int_env("NEWS_INT_VALUE", 7), 11)
            self.assertEqual(reloaded._bounded_env_float("NEWS_MISSING_FLOAT", 0.5), 0.5)
            with patch.dict(os.environ, {"NEWS_FLOAT_VALUE": "bad"}, clear=False):
                self.assertEqual(reloaded._bounded_env_float("NEWS_FLOAT_VALUE", 0.5), 0.5)
            with patch.dict(os.environ, {"NEWS_FLOAT_VALUE": "1.5"}, clear=False):
                self.assertEqual(reloaded._bounded_env_float("NEWS_FLOAT_VALUE", 0.5), 1.0)

            self.assertEqual(
                reloaded.strip_inline_markdown("Use [link](https://x.test) and `code` **bold** *italics*"),
                "Use link (https://x.test) and code bold italics",
            )
            aware_article = {"pub_date": "Mon, 01 Jun 2026 12:34:56 GMT"}
            self.assertEqual(
                reloaded._article_sort_datetime(aware_article),
                datetime(2026, 6, 1, 12, 34, 56),
            )
            self.assertEqual(reloaded._article_sort_datetime({}), datetime.min.replace(tzinfo=None))
            self.assertEqual(reloaded._article_time_rank(aware_article)[1:], (45296, 0))
            self.assertEqual(reloaded._story_slug("  Hello, world!  "), "hello_world")
            self.assertEqual(reloaded._story_slug("!!!"), "story")
            self.assertEqual(reloaded.cosine_similarity({}, 0.0, {"a": 1}, 1.0), 0.0)
            self.assertEqual(
                reloaded.cosine_similarity({"a": 1, "b": 1}, 1.0, {"a": 1}, 1.0),
                1.0,
            )
            self.assertEqual(
                reloaded.cosine_similarity({"a": 1}, 1.0, {"b": 1}, 1.0),
                0.0,
            )

            long_title = "Port strike talks resume after union vote delays cargo shipments across regional terminals and ports"
            cleaned_title = reloaded._clean_story_title(
                "Port Talks",
                {"title": "Port Talks"},
                [{"title": long_title}],
            )
            self.assertLessEqual(len(cleaned_title.split()), 14)
            self.assertTrue(cleaned_title.startswith("Port strike talks"))

            stopwords = reloaded._story_similarity_stopwords(
                {"key": "World", "title": "World News", "rationale": "World coverage"}
            )
            self.assertIn("article", stopwords)
            self.assertIn("story", stopwords)
            self.assertIn("world", stopwords)
            self.assertEqual(reloaded._normalize_story_similarity_token("hits"), "hit")
            self.assertEqual(reloaded._normalize_story_similarity_token("cities"), "city")
            self.assertEqual(reloaded._normalize_story_similarity_token("virus"), "virus")
            self.assertEqual(reloaded._normalize_story_similarity_token("boss's"), "boss")

            terms = reloaded.story_similarity_terms(
                "Port-workers discuss supply-chain issues and 123 numbers.",
                {"and"},
            )
            self.assertIn("port", terms)
            self.assertIn("worker", terms)
            self.assertIn("supply", terms)
            self.assertIn("chain", terms)
            self.assertNotIn("123", terms)

            counts = reloaded._story_weighted_term_counts(
                {
                    "title": "Port strike",
                    "description": "Port strike",
                    "text": "Port strike port strike",
                    "source": "Fixture Wire",
                    "url": "https://example.com/ports",
                },
                {"title": "World", "key": "world", "rationale": "World"},
            )
            self.assertGreaterEqual(counts["port"], 6)
        finally:
            importlib.reload(original_module)

    def test_metric_helpers_pruning_and_split_branches(self) -> None:
        similarities = {
            (0, 1): 0.9,
            (0, 2): 0.6,
            (1, 2): 0.8,
            (0, 3): 0.9,
            (1, 3): 0.9,
            (2, 3): 0.9,
            (0, 4): 0.9,
            (1, 4): 0.9,
            (2, 4): 0.9,
            (3, 4): 0.9,
        }
        source_ids = ["s1", "s2", "s1", "s2", "s1"]

        self.assertEqual(sc._story_pair_key(2, 0), (0, 2))
        self.assertEqual(sc._story_pair_similarity(similarities, 1, 1), 1.0)
        self.assertEqual(sc._story_pair_similarity(similarities, 0, 9), 0.0)
        self.assertEqual(sc._story_component_average_similarity([0], similarities), 0.0)
        self.assertGreater(sc._story_component_average_similarity([0, 1, 2], similarities), 0.0)
        self.assertEqual(sc._story_component_pair_count([0, 1, 2]), 3)
        self.assertEqual(sc._story_component_edge_count([0, 1, 2], similarities, 0.5), 3)
        self.assertEqual(sc._story_component_best_similarities([0, 1, 2], similarities), [0.9, 0.9, 0.8])
        self.assertEqual(sc._story_component_best_similarities([0], similarities), [0.0])
        self.assertGreater(sc._story_member_average_similarity(0, [0, 1, 2], similarities), 0.0)
        self.assertEqual(sc._story_member_average_similarity(0, [0], similarities), 0.0)
        self.assertEqual(sc._story_member_edge_degree(0, [0, 1, 2], similarities, 0.5), 2)
        self.assertEqual(sc._story_member_cohesion_floor(0.5), 0.375)
        member_metrics = sc._story_component_member_cohesion_metrics([0, 1, 2], similarities, 0.5)
        self.assertEqual(member_metrics["member_edge_degree_floor"], 1)
        self.assertEqual(sc._minimum_story_edge_density(2), 1.0)
        self.assertEqual(sc._minimum_story_edge_density(3), 2.0 / 3.0)
        self.assertEqual(sc._minimum_story_edge_density(4), 0.50)
        self.assertEqual(sc._minimum_story_edge_density(6), 0.45)
        connectedness = sc._story_component_connectedness_metrics([0, 1, 2], similarities, 0.5)
        self.assertEqual(connectedness["pair_count"], 3)
        self.assertEqual(connectedness["edge_count"], 3)
        self.assertEqual(sc._story_cluster_relevance_score([], []), 0.0)
        self.assertEqual(sc._story_cluster_recency_rank([], []), (0, 0, 0))
        self.assertEqual(
            sc._story_component_diagnostics([0, 1, 2], similarities, 0.5)["min_member_edge_degree"],
            2,
        )
        self.assertEqual(sc._story_component_source_count([0, 1, 2], source_ids), 2)
        self.assertTrue(sc._story_component_has_source_diversity([0, 1, 2], source_ids))
        self.assertEqual(sc._article_source_identity({}), "unknown")
        self.assertEqual(
            sc._story_subcomponents_from_adjacency(
                [0, 1, 2, 3],
                {0: {1}, 1: {0}, 2: {3}, 3: {2}},
            ),
            [[0, 1], [2, 3]],
        )
        self.assertEqual(
            sc._story_similarity_edges([0, 1, 2], similarities, 0.5),
            [(0.9, 0, 1), (0.6, 0, 2), (0.8, 1, 2)],
        )
        self.assertFalse(
            sc._story_component_meets_connectedness_floor(
                [0],
                source_ids,
                similarities,
                0.5,
                min_articles_per_story=2,
            )
        )
        self.assertFalse(
            sc._story_component_meets_connectedness_floor(
                [0, 1],
                ["s1", "s1"],
                {(0, 1): 0.9},
                0.5,
                min_articles_per_story=2,
            )
        )
        star_similarities = {(0, index): 0.9 for index in range(1, 5)}
        self.assertFalse(
            sc._story_component_meets_connectedness_floor(
                [0, 1, 2, 3, 4],
                ["s1", "s2", "s1", "s2", "s1"],
                star_similarities,
                0.5,
                min_articles_per_story=2,
            )
        )
        self.assertTrue(
            sc._story_component_meets_connectedness_floor(
                [0, 1],
                ["s1", "s2"],
                {(0, 1): 0.9},
                0.5,
                min_articles_per_story=2,
            )
        )
        with patch.object(
            sc,
            "_story_component_connectedness_metrics",
            return_value={"min_best_similarity": 0.9, "edge_density": 1.0},
        ), patch.object(
            sc,
            "_story_component_member_cohesion_metrics",
            return_value={
                "min_member_edge_degree": 0,
                "min_member_average_similarity": 0.5,
                "member_cohesion_floor": 0.4,
            },
        ):
            self.assertFalse(
                sc._story_component_meets_connectedness_floor(
                    [0, 1],
                    ["s1", "s2"],
                    {(0, 1): 0.9},
                    0.5,
                    min_articles_per_story=2,
                )
            )
        self.assertEqual(
            sc._story_component_prune_rank(0, [0, 1, 2], similarities, 0.5),
            (2.0, 0.75, 0.9, 0),
        )

        pruned_current, pruned_indexes, prune_reason = sc._prune_story_component_by_member_cohesion(
            [0, 1, 2],
            ["s1", "s2", "s1"],
            {},
            0.5,
            min_articles_per_story=2,
        )
        self.assertEqual(pruned_current, [])
        self.assertEqual(pruned_indexes, [0, 1, 2])
        self.assertEqual(prune_reason, "removed_islands")

        weak_member_similarities = {
            (0, 1): 0.9,
            (0, 2): 0.9,
            (0, 3): 0.9,
            (0, 4): 0.9,
            (1, 2): 0.9,
            (1, 3): 0.9,
            (2, 3): 0.9,
        }
        pruned_current, pruned_indexes, prune_reason = sc._prune_story_component_by_member_cohesion(
            [0, 1, 2, 3, 4],
            ["s1", "s2", "s1", "s2", "s1"],
            weak_member_similarities,
            0.5,
            min_articles_per_story=2,
        )
        self.assertEqual(pruned_current, [0, 1, 2, 3])
        self.assertEqual(pruned_indexes, [4])
        self.assertEqual(prune_reason, "removed_weak_members")

        with patch.object(
            sc,
            "_story_component_meets_connectedness_floor",
            side_effect=[False, True],
        ):
            pruned_current, pruned_indexes, prune_reason = sc._prune_story_component_by_member_cohesion(
                [0, 1, 2],
                ["s1", "s2", "s1"],
                {(0, 1): 0.9, (1, 2): 0.9, (0, 2): 0.9},
                0.5,
                min_articles_per_story=2,
            )
        self.assertEqual(pruned_current, [0, 1])
        self.assertEqual(pruned_indexes, [2])
        self.assertEqual(prune_reason, "removed_weakest_member")

        self.assertEqual(
            sc._split_story_component_by_weak_bridges([0], ["s1"], {}, 0.5, min_articles_per_story=2),
            [],
        )
        with patch.object(
            sc,
            "_prune_story_component_by_member_cohesion",
            return_value=([], [0], "pruned"),
        ):
            self.assertEqual(
                sc._split_story_component_by_weak_bridges(
                    [0, 1],
                    ["s1", "s2"],
                    {},
                    0.5,
                    min_articles_per_story=2,
                ),
                [],
            )
        with patch.object(
            sc,
            "_prune_story_component_by_member_cohesion",
            return_value=([0, 1], [2], "pruned"),
        ), patch.object(sc, "_story_similarity_edges", return_value=[]):
            self.assertEqual(
                sc._split_story_component_by_weak_bridges(
                    [0, 1, 2],
                    ["s1", "s2", "s1"],
                    {(0, 1): 0.9},
                    0.5,
                    min_articles_per_story=2,
                ),
                [],
            )
        with patch.object(
            sc,
            "_prune_story_component_by_member_cohesion",
            return_value=([0, 1], [], "pruned"),
        ), patch.object(sc, "_story_similarity_edges", return_value=[(0.9, 0, 1)]), patch.object(
            sc,
            "_story_subcomponents_from_adjacency",
            return_value=[[0, 1]],
        ), patch.object(sc, "_story_component_meets_connectedness_floor", return_value=False):
            self.assertEqual(
                sc._split_story_component_by_weak_bridges(
                    [0, 1],
                    ["s1", "s2"],
                    {(0, 1): 0.9},
                    0.5,
                    min_articles_per_story=2,
                ),
                [],
            )

        natural_split_records = sc._split_story_component_by_weak_bridges(
            [0, 1, 2, 3],
            ["s1", "s2", "s1", "s2"],
            {(0, 1): 0.9, (2, 3): 0.9},
            0.5,
            min_articles_per_story=2,
        )
        self.assertEqual(natural_split_records[0]["component"], [0, 1])
        self.assertEqual(natural_split_records[0]["pruned_indexes"], [2, 3])

        with patch.object(
            sc,
            "_prune_story_component_by_member_cohesion",
            side_effect=lambda component, *_args, **_kwargs: (list(component), [], "pruned"),
        ), patch.object(
            sc,
            "_story_component_connectedness_metrics",
            side_effect=lambda component, *_args, **_kwargs: {
                "story_strength_score": 1.0 if len(component) == 4 else 0.7,
            },
        ), patch.object(
            sc,
            "_story_component_meets_connectedness_floor",
            side_effect=lambda component, *_args, **_kwargs: len(component) >= 2,
        ), patch.object(
            sc,
            "_story_similarity_edges",
            side_effect=lambda component, *_args, **_kwargs: (
                [(0.9, 0, 1), (0.8, 2, 3)] if len(component) == 4 else [(0.9, 0, 1)]
            ),
        ), patch.object(
            sc,
            "_story_subcomponents_from_adjacency",
            side_effect=lambda component, *_args, **_kwargs: ([[0, 1], [2, 3]] if len(component) == 4 else [[0, 1]]),
        ):
            recursive_split_records = sc._split_story_component_by_weak_bridges(
                [0, 1, 2, 3],
                ["s1", "s2", "s1", "s2"],
                {(0, 1): 0.9, (2, 3): 0.9},
                0.5,
                min_articles_per_story=2,
            )
        self.assertEqual([record["component"] for record in recursive_split_records], [[0, 1], [2, 3]])

        with patch.object(
            sc,
            "_prune_story_component_by_member_cohesion",
            side_effect=lambda component, *_args, **_kwargs: (list(component), [], "pruned"),
        ), patch.object(
            sc,
            "_story_component_connectedness_metrics",
            side_effect=lambda component, *_args, **_kwargs: {
                "story_strength_score": 1.0 if len(component) == 4 else 0.5,
            },
        ), patch.object(
            sc,
            "_story_component_meets_connectedness_floor",
            side_effect=lambda component, *_args, **_kwargs: len(component) >= 2,
        ), patch.object(
            sc,
            "_story_similarity_edges",
            side_effect=lambda component, *_args, **_kwargs: (
                [(0.9, 0, 1), (0.8, 2, 3)] if len(component) == 4 else [(0.9, 0, 1)]
            ),
        ), patch.object(
            sc,
            "_story_subcomponents_from_adjacency",
            side_effect=lambda component, *_args, **_kwargs: ([[0, 1], [2, 3]] if len(component) == 4 else [[0, 1]]),
        ):
            low_split_records = sc._split_story_component_by_weak_bridges(
                [0, 1, 2, 3],
                ["s1", "s2", "s1", "s2"],
                {(0, 1): 0.9, (2, 3): 0.9},
                0.5,
                min_articles_per_story=2,
            )
        self.assertEqual(low_split_records[0]["component"], [0, 1, 2, 3])

        def fake_connectedness_floor(component, *_args, **_kwargs):
            return tuple(component) in {(0, 1), (2, 3)}

        def fake_connectedness_metrics(component, *_args, **_kwargs):
            return {"story_strength_score": 1.0 if len(component) == 4 else 0.7}

        def fake_similarity_edges(component, *_args, **_kwargs):
            if len(component) == 4:
                return [(0.9, 0, 1), (0.8, 2, 3)]
            if len(component) == 2:
                return [(0.9, 0, 1)]
            return []

        def fake_subcomponents(component, *_args, **_kwargs):
            if len(component) == 4:
                return [[0, 1], [2, 3]]
            if len(component) == 2:
                return [[0, 1]]
            return [list(component)]

        with patch.object(sc, "_story_component_meets_connectedness_floor", side_effect=fake_connectedness_floor), patch.object(
            sc,
            "_story_component_connectedness_metrics",
            side_effect=fake_connectedness_metrics,
        ), patch.object(sc, "_story_similarity_edges", side_effect=fake_similarity_edges), patch.object(
            sc,
            "_story_subcomponents_from_adjacency",
            side_effect=fake_subcomponents,
        ):
            split_records = sc._split_story_component_by_weak_bridges(
                [0, 1, 2, 3],
                ["s1", "s2", "s1", "s2"],
                weak_member_similarities,
                0.5,
                min_articles_per_story=2,
            )
        self.assertGreaterEqual(len(split_records), 1)
        self.assertTrue(any(len(record["component"]) == 2 for record in split_records))

        self.assertEqual(sc._story_index_average_similarity(0, [0, 1, 2], similarities), 0.75)
        medoid_index = sc._story_component_medoid_index(
            [0, 1, 2],
            [
                {"relevance_score": 2, "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT"},
                {"relevance_score": 3, "pub_date": "Mon, 01 Jun 2026 13:00:00 GMT"},
                {"relevance_score": 1, "pub_date": "Mon, 01 Jun 2026 11:00:00 GMT"},
            ],
            similarities,
        )
        self.assertIn(medoid_index, {0, 1, 2})
        self.assertEqual(
            sc._story_cluster_relevance_score(
                [0, 1, 2],
                [
                    {"relevance_score": 2},
                    {"relevance_score": 4},
                    {"relevance_score": 6},
                ],
            ),
            4.0,
        )
        self.assertEqual(
            sc._story_cluster_recency_rank(
                [0, 1, 2],
                [
                    {"pub_date": "Mon, 01 Jun 2026 12:00:00 GMT"},
                    {"pub_date": "Mon, 01 Jun 2026 13:00:00 GMT"},
                    {"pub_date": "Mon, 01 Jun 2026 11:00:00 GMT"},
                ],
            ),
            (739768, 46800, 0),
        )
        self.assertEqual(
            sc._article_source_identity({"source": " Fixture Wire "}),
            "fixture wire",
        )
        self.assertEqual(
            sc._article_source_identity({"url": "https://www.example.com/path"}),
            "example.com",
        )
        self.assertEqual(
            sc._select_story_article_indexes(
                [0, 1, 2],
                1,
                [
                    {"source": "Fixture Wire", "relevance_score": 3, "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT"},
                    {"source": "Fixture Wire", "relevance_score": 2, "pub_date": "Mon, 01 Jun 2026 13:00:00 GMT"},
                    {"source": "Second Source", "relevance_score": 1, "pub_date": "Mon, 01 Jun 2026 11:00:00 GMT"},
                ],
                similarities,
            ),
            [1, 2, 0],
        )
        self.assertEqual(sc._story_component_overlap_ratio(set(), {1, 2}), 0.0)
        self.assertAlmostEqual(sc._story_component_overlap_ratio({0, 1}, {1, 2}), 0.5)
        rank_tuple = sc._story_component_rank_tuple(
            [0, 1, 2],
            [
                {"relevance_score": 2, "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT"},
                {"relevance_score": 4, "pub_date": "Mon, 01 Jun 2026 13:00:00 GMT"},
                {"relevance_score": 6, "pub_date": "Mon, 01 Jun 2026 11:00:00 GMT"},
            ],
            source_ids,
            similarities,
            0.5,
        )
        self.assertEqual(len(rank_tuple), 8)

    def test_cluster_organize_and_budget_paths(self) -> None:
        articles = [
            _article(
                "a1",
                "Source A",
                "Port strike talks resume",
                "Port strike talks and cargo delays ripple through the port.",
                relevance_score=4,
            ),
            _article(
                "a2",
                "Source B",
                "Port strike talks resume",
                "Port strike talks and cargo delays ripple through the port.",
                relevance_score=3,
            ),
            _article(
                "a3",
                "Source C",
                "Port strike talks resume",
                "Port strike talks and cargo delays ripple through the port.",
                relevance_score=2,
            ),
            _article(
                "a4",
                "Source D",
                "Port strike talks resume",
                "Port strike talks and cargo delays ripple through the port.",
                relevance_score=1,
            ),
        ]
        progress_events: list[tuple[str, dict]] = []

        def fake_split(component, *_args, **_kwargs):
            key = list(component)
            if key == [0, 1, 2, 3]:
                return [
                    {"component": [0], "pruned_indexes": [3], "prune_reason": "small"},
                    {"component": [0, 1], "pruned_indexes": [], "prune_reason": "weak"},
                    {"component": [0, 1, 2], "pruned_indexes": [3], "prune_reason": "bridge"},
                ]
            if key == [1, 2, 3]:
                return [
                    {"component": [1, 2, 3], "pruned_indexes": [], "prune_reason": "bridge"},
                ]
            return []

        def fake_floor(component, *_args, **_kwargs):
            return tuple(component) in {(0, 1, 2), (1, 2, 3)}

        with patch.object(
            sc,
            "_build_story_tfidf_vectors",
            return_value=([{} for _ in articles], [1.0] * len(articles)),
        ), patch.object(
            sc,
            "cosine_similarity",
            side_effect=[0.9, 0.0, 0.0, 0.9, 0.0, 0.9],
        ), patch.object(
            sc,
            "_split_story_component_by_weak_bridges",
            side_effect=fake_split,
        ), patch.object(
            sc,
            "_story_component_meets_connectedness_floor",
            side_effect=fake_floor,
        ):
            stories = sc.cluster_global_stories_by_similarity(
                articles,
                min_articles_per_story=2,
                similarity_threshold=0.5,
                progress_callback=lambda event, payload: progress_events.append((event, payload)),
            )

        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["article_count"], 3)
        self.assertEqual(progress_events[-1][0], "components_ranked")
        self.assertEqual(progress_events[-1][1]["done"], progress_events[-1][1]["total"])

        self.assertEqual(sc.cluster_global_stories_by_similarity([], min_articles_per_story=2), [])

        with patch.object(
            sc,
            "_build_story_tfidf_vectors",
            return_value=([{} for _ in articles[:3]], [1.0, 1.0, 1.0]),
        ), patch.object(
            sc,
            "cosine_similarity",
            side_effect=[0.9, 0.0, 0.0],
        ):
            self.assertEqual(
                sc.cluster_global_stories_by_similarity(
                    articles[:3],
                    min_articles_per_story=3,
                    similarity_threshold=0.5,
                ),
                [],
            )

        with patch.object(
            sc,
            "cluster_global_stories_by_similarity",
            return_value=[
                {
                    "title": "Story One",
                    "article_ids": ["a1", "a2"],
                    "cluster_article_ids": ["a1", "a2", "missing"],
                    "article_count": 2,
                    "average_similarity": 0.9,
                    "connectedness_score": 0.8,
                    "story_strength_score": 0.7,
                    "edge_density": 0.6,
                    "edge_count": 1,
                    "mean_best_similarity": 0.9,
                    "min_best_similarity": 0.8,
                    "min_member_average_similarity": 0.7,
                    "min_member_edge_degree": 1,
                    "member_cohesion_floor": 0.5,
                    "member_edge_degree_floor": 1,
                    "pruned_article_ids": ["a4"],
                    "prune_reason": "pruned",
                    "source_count": 2,
                    "relevance_score": 4.0,
                    "_pair_debug": [{"linked": True}],
                },
                {
                    "title": "Tiny story",
                    "article_ids": ["a3"],
                    "cluster_article_ids": ["a3"],
                    "article_count": 1,
                    "average_similarity": 0.2,
                    "connectedness_score": 0.1,
                    "story_strength_score": 0.1,
                    "edge_density": 0.0,
                    "edge_count": 0,
                    "mean_best_similarity": 0.2,
                    "min_best_similarity": 0.2,
                    "min_member_average_similarity": 0.2,
                    "min_member_edge_degree": 0,
                    "member_cohesion_floor": 0.5,
                    "member_edge_degree_floor": 1,
                    "pruned_article_ids": [],
                    "prune_reason": "",
                    "source_count": 1,
                    "relevance_score": 1.0,
                    "_pair_debug": [],
                },
            ],
        ):
            selected_targets, story_records, stats = sc.organize_article_targets_into_global_stories(
                articles[:3],
                min_articles_per_story=2,
                similarity_threshold=0.5,
            )

        self.assertEqual([article["article_id"] for article in selected_targets], ["a1", "a2"])
        self.assertEqual(len(story_records), 1)
        self.assertEqual(stats["dropped_articles"][0]["article_id"], "a3")

        singleton_progress: list[tuple[str, dict]] = []
        singleton_targets, singleton_records, singleton_stats = sc.organize_article_targets_into_global_stories(
            articles[:2],
            min_articles_per_story=1,
            similarity_threshold=0.5,
            progress_callback=lambda event, payload: singleton_progress.append((event, payload)),
        )
        self.assertEqual(singleton_targets, articles[:2])
        self.assertEqual(len(singleton_records), 2)
        self.assertFalse(singleton_stats["enabled"])
        self.assertEqual(singleton_progress[0][0], "singletons")

        class FalseyArticle(dict):
            def __bool__(self) -> bool:
                return False

        falsey_articles = [
            FalseyArticle(
                {
                    "article_id": "ghost",
                    "title": "Ghost article",
                    "source": "Source Ghost",
                    "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                    "url": "https://example.com/ghost",
                }
            ),
            _article(
                "keep",
                "Source Keep",
                "Keep article",
                "Keep article text",
            ),
        ]
        with patch.object(
            sc,
            "cluster_global_stories_by_similarity",
            return_value=[
                {
                    "title": "Falsey story",
                    "article_ids": ["ghost", "keep"],
                    "cluster_article_ids": ["ghost", "keep"],
                    "article_count": 2,
                    "average_similarity": 0.5,
                    "connectedness_score": 0.5,
                    "story_strength_score": 0.5,
                    "edge_density": 0.5,
                    "edge_count": 1,
                    "mean_best_similarity": 0.5,
                    "min_best_similarity": 0.5,
                    "min_member_average_similarity": 0.5,
                    "min_member_edge_degree": 1,
                    "member_cohesion_floor": 0.5,
                    "member_edge_degree_floor": 1,
                    "pruned_article_ids": [],
                    "prune_reason": "",
                    "source_count": 2,
                    "relevance_score": 1.0,
                }
            ],
        ):
            falsey_selected, falsey_stories, falsey_stats = sc.organize_article_targets_into_global_stories(
                falsey_articles,
                min_articles_per_story=2,
                similarity_threshold=0.5,
            )
        self.assertEqual([article["article_id"] for article in falsey_selected], ["ghost", "keep"])
        self.assertEqual(len(falsey_stories), 1)
        self.assertEqual(falsey_stats["dropped_count"], 0)

        budgeted, budget_stats = sc.filter_budgeted_targets_by_story_floor(
            [
                {"article_id": "a1", "story_key": "alpha"},
                {"article_id": "a2", "story_key": "alpha"},
                {"article_id": "a3", "story_key": "beta"},
            ],
            min_articles_per_story=2,
        )
        self.assertEqual([article["article_id"] for article in budgeted], ["a1", "a2"])
        self.assertEqual(budget_stats["eligible_story_count"], 1)
        self.assertEqual(budget_stats["dropped_article_ids"], ["a3"])

        singleton_budgeted, singleton_budget_stats = sc.filter_budgeted_targets_by_story_floor(
            articles[:2],
            min_articles_per_story=1,
        )
        self.assertEqual(singleton_budgeted, articles[:2])
        self.assertFalse(singleton_budget_stats["enabled"])

        budgeted_with_blank_key, blank_key_stats = sc.filter_budgeted_targets_by_story_floor(
            [
                {"article_id": "skip", "story_key": ""},
                {"article_id": "keep1", "story_key": "alpha"},
                {"article_id": "keep2", "story_key": "alpha"},
            ],
            min_articles_per_story=2,
        )
        self.assertEqual([article["article_id"] for article in budgeted_with_blank_key], ["keep1", "keep2"])
        self.assertEqual(blank_key_stats["candidate_count"], 3)


if __name__ == "__main__":
    unittest.main()
