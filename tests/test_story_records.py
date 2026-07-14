from __future__ import annotations

import unittest

from news_pipeline.story_records import (
    StoryRecord,
    ensure_story_record,
    ordered_unique_article_ids,
    story_article_id_set,
    story_article_ids,
    story_article_overlap,
    story_debug_record,
    story_rank_key,
    to_story_dict,
    with_budgeted_article_ids,
)


class StoryRecordTests(unittest.TestCase):
    def test_ordered_unique_article_ids_filters_blanks_duplicates_and_non_strings(self) -> None:
        self.assertEqual(
            ordered_unique_article_ids([None, "  a  ", 1, "1", "", "a", " ", 2]),
            ["a", "1", "2"],
        )

    def test_ensure_story_record_adapts_legacy_dicts_and_defaults(self) -> None:
        legacy = {
            "article_ids": ["alpha", "alpha", "beta"],
            "source_count": "bad",
            "average_similarity": "",
            "connectedness_score": "1.5",
            "story_strength_score": "2.5",
            "edge_density": "bad",
            "mean_best_similarity": "0.7",
            "min_best_similarity": None,
            "min_member_average_similarity": "bad",
            "min_member_edge_degree": "4",
            "member_cohesion_floor": "0.2",
            "member_edge_degree_floor": "bad",
            "pruned_article_ids": ["p1", "p1", ""],
            "story_rank": "9",
            "global_selection_rank": "8",
            "note": "kept",
        }

        record = ensure_story_record(legacy, index=-1)

        self.assertEqual(record.story_key, "")
        self.assertEqual(record.story_title, "News update")
        self.assertEqual(record.article_ids, ("alpha", "beta"))
        self.assertEqual(record.cluster_article_ids, ("alpha", "beta"))
        self.assertEqual(record.article_count, 2)
        self.assertEqual(record.cluster_article_count, 2)
        self.assertEqual(record.selected_article_count, 2)
        self.assertIsNone(record.source_count)
        self.assertIsNone(record.average_similarity)
        self.assertEqual(record.connectedness_score, 1.5)
        self.assertEqual(record.story_strength_score, 2.5)
        self.assertIsNone(record.edge_density)
        self.assertEqual(record.mean_best_similarity, 0.7)
        self.assertIsNone(record.min_best_similarity)
        self.assertIsNone(record.min_member_average_similarity)
        self.assertEqual(record.min_member_edge_degree, 4)
        self.assertEqual(record.member_cohesion_floor, 0.2)
        self.assertIsNone(record.member_edge_degree_floor)
        self.assertEqual(record.pruned_article_ids, ("p1",))
        self.assertEqual(record.story_rank, 9)
        self.assertEqual(record.global_selection_rank, 8)
        self.assertEqual(record.extras, {"note": "kept"})

    def test_ensure_story_record_prefers_cluster_article_ids_when_articles_missing(self) -> None:
        legacy = {
            "cluster_article_ids": ["gamma", "", "delta"],
            "title": "",
            "story_key": "",
            "extra": "value",
        }

        record = ensure_story_record(legacy)

        self.assertEqual(record.story_key, "global-story-01")
        self.assertEqual(record.story_title, "News update")
        self.assertEqual(record.article_ids, ("gamma", "delta"))
        self.assertEqual(record.cluster_article_ids, ("gamma", "delta"))
        self.assertEqual(record.extras, {"extra": "value"})

    def test_ensure_story_record_returns_existing_record(self) -> None:
        record = StoryRecord(
            story_key="story-1",
            story_title="Headline",
            article_ids=("a",),
            cluster_article_ids=("a",),
            article_count=1,
            cluster_article_count=1,
            selected_article_count=1,
        )

        self.assertIs(ensure_story_record(record), record)

    def test_story_dict_and_overlap_helpers_cover_fallbacks(self) -> None:
        cluster_only = StoryRecord(
            story_key="cluster-only",
            story_title="Cluster",
            article_ids=(),
            cluster_article_ids=("c1", "c2"),
            article_count=2,
            cluster_article_count=2,
            selected_article_count=0,
        )
        article_only = StoryRecord(
            story_key="article-only",
            story_title="Article",
            article_ids=("a1", "a2"),
            cluster_article_ids=(),
            article_count=0,
            cluster_article_count=0,
            selected_article_count=2,
        )
        empty = StoryRecord(
            story_key="empty",
            story_title="Empty",
            article_ids=(),
            cluster_article_ids=(),
            article_count=0,
            cluster_article_count=0,
            selected_article_count=0,
        )

        self.assertEqual(story_article_ids(cluster_only), ["c1", "c2"])
        self.assertEqual(story_article_id_set(cluster_only), {"c1", "c2"})
        self.assertEqual(story_article_ids(article_only), ["a1", "a2"])
        self.assertEqual(story_article_id_set(article_only), {"a1", "a2"})
        self.assertEqual(story_article_overlap(empty, cluster_only), (0.0, set()))
        self.assertEqual(story_article_overlap(cluster_only, article_only), (0.0, set()))

    def test_to_story_dict_round_trips_fields_and_extras(self) -> None:
        record = StoryRecord(
            story_key="story-2",
            story_title="Title",
            article_ids=("a1", "a2"),
            cluster_article_ids=("a1", "a2"),
            article_count=2,
            cluster_article_count=2,
            selected_article_count=2,
            source_count=3,
            average_similarity=0.8,
            connectedness_score=0.5,
            story_strength_score=0.9,
            edge_density=0.4,
            mean_best_similarity=0.7,
            min_best_similarity=0.6,
            min_member_average_similarity=0.55,
            min_member_edge_degree=1,
            member_cohesion_floor=0.2,
            member_edge_degree_floor=1,
            pruned_article_ids=("p1",),
            prune_reason="too small",
            story_rank=7,
            global_selection_rank=4,
            extras={"custom": "value"},
        )

        story = to_story_dict(record)

        self.assertEqual(story["custom"], "value")
        self.assertEqual(story["story_key"], "story-2")
        self.assertEqual(story["article_ids"], ["a1", "a2"])
        self.assertEqual(story["cluster_article_ids"], ["a1", "a2"])
        self.assertEqual(story["pruned_article_ids"], ["p1"])
        self.assertEqual(story["prune_reason"], "too small")
        self.assertEqual(story["story_rank"], 7)
        self.assertEqual(story["global_selection_rank"], 4)
        self.assertEqual(story["source_count"], 3)
        self.assertEqual(story["average_similarity"], 0.8)

    def test_with_budgeted_article_ids_and_rank_and_debug_helpers(self) -> None:
        record = StoryRecord(
            story_key="story-3",
            story_title="Ranked Story",
            article_ids=("a1", "a2"),
            cluster_article_ids=("a1", "a2"),
            article_count=2,
            cluster_article_count=2,
            selected_article_count=2,
            source_count=5,
            average_similarity=0.75,
            story_strength_score=0.9,
        )

        budgeted = with_budgeted_article_ids(record, ["b1", "b1", "b2"])
        filtered = with_budgeted_article_ids(record, ["a2", "x"])

        self.assertEqual(budgeted.article_ids, ("b1", "b2"))
        self.assertEqual(budgeted.cluster_article_ids, ("b1", "b2"))
        self.assertEqual(budgeted.article_count, 2)
        self.assertEqual(budgeted.selected_article_count, 2)
        self.assertEqual(filtered.article_ids, ("a2",))
        self.assertEqual(filtered.cluster_article_ids, ("a2",))
        self.assertEqual(
            story_rank_key(
                StoryRecord(
                    story_key="story-4",
                    story_title="Alpha",
                    article_ids=("a1", "a2", "a3", "a4"),
                    cluster_article_ids=("a1", "a2", "a3", "a4"),
                    article_count=4,
                    cluster_article_count=4,
                    selected_article_count=4,
                    source_count=2,
                    average_similarity=0.8,
                    story_strength_score=3.0,
                    story_rank=5,
                )
            ),
            (-3.0, -2, -4, -0.8, 5, "Alpha"),
        )

        debug = story_debug_record(
            StoryRecord(
                story_key="story-5",
                story_title="Debug Story",
                article_ids=("d1", "d2"),
                cluster_article_ids=("d1", "d2"),
                article_count=2,
                cluster_article_count=2,
                selected_article_count=2,
                source_count=6,
                average_similarity=0.66,
                story_strength_score=0.77,
                global_selection_rank=9,
                extras={
                    "scale_screening_scale": "medium",
                    "scale_screening_reason": "fit",
                    "paragraph": "x" * 600,
                },
            )
        )

        self.assertEqual(debug["scale_screening_scale"], "medium")
        self.assertEqual(debug["scale_screening_reason"], "fit")
        self.assertEqual(debug["global_selection_rank"], 9)
        self.assertEqual(debug["preview"], "x" * 500)


if __name__ == "__main__":
    unittest.main()
