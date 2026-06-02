from __future__ import annotations

import json
import unittest
from typing import Any

from news_pipeline.story_topic_assignment import (
    StoryTopicRuntime,
    classify_story_drafts_for_topics,
    parse_story_topic_screening_response,
)


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


def _runtime(
    screening_payload: list[dict[str, str]] | None = None,
    *,
    screening_error: Exception | None = None,
    max_stories_per_topic: int = 10,
) -> StoryTopicRuntime:
    def invoke_with_retries(*_args: Any, fallback_content: str, **_kwargs: Any) -> FakeResponse:
        if screening_error is not None:
            raise screening_error
        return FakeResponse(json.dumps(screening_payload if screening_payload is not None else []))

    return StoryTopicRuntime(
        max_stories_per_topic=max_stories_per_topic,
        min_score=1,
        diversity_min_distance=0.0,
        model_max_input_tokens=1000,
        model_profile_key="test",
        model_reference="test",
        model_name="test",
        model_backend="test",
        relaxed_final_synthesis_guards=True,
        story_topic_validation_enabled=True,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=invoke_with_retries,
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda _article: "",
        format_topic_section_header=lambda title: title.upper(),
        final_synthesis_word_count=lambda text: len(str(text or "").split()),
        is_low_confidence_report_entry=lambda _entry: False,
        report_reference_key=lambda entry: entry,
    )


def _story(
    story_key: str,
    title: str,
    paragraph: str,
    *,
    topic_key: str = "us_economy",
    topic_title: str = "US Economy",
) -> dict[str, Any]:
    return {
        "story_key": story_key,
        "topic_key": topic_key,
        "topic_title": topic_title,
        "story_title": title,
        "paragraph": paragraph,
        "summaries": [paragraph],
        "article_ids": [f"{story_key}-a1", f"{story_key}-a2"],
        "article_count": 2,
        "source_count": 2,
        "story_strength_score": 1.0,
        "average_similarity": 0.8,
    }


class StoryTopicScreeningParsingTests(unittest.TestCase):
    def test_parse_screening_response_handles_fenced_json_and_unknown_verdicts(self) -> None:
        verdicts, stats = parse_story_topic_screening_response(
            "```json\n"
            "["
            "{\"story_key\":\"s1\",\"topicality\":\"obviously_not_topical\",\"scale\":\"not_obvious\",\"topic_reason\":\"France centered\"},"
            "{\"story_key\":\"s2\",\"topicality\":\"mystery\",\"scale\":\"mystery\",\"topic_reason\":\"unknown label\"}"
            "]\n"
            "```"
        )

        self.assertFalse(stats["parse_failed"])
        self.assertEqual(verdicts["s1"]["topicality"], "obviously_not_topical")
        self.assertEqual(verdicts["s2"]["topicality"], "not_obvious")
        self.assertEqual(verdicts["s2"]["scale"], "not_obvious")
        self.assertEqual(stats["unknown_topicality_count"], 1)
        self.assertEqual(stats["unknown_scale_count"], 1)

    def test_parse_screening_response_reports_malformed_json(self) -> None:
        verdicts, stats = parse_story_topic_screening_response("not json")

        self.assertEqual(verdicts, {})
        self.assertTrue(stats["parse_failed"])


class StoryTopicScreeningClassificationTests(unittest.TestCase):
    def test_us_economy_screening_rejects_only_obvious_non_us_story(self) -> None:
        topics = [
            {
                "key": "us_economy",
                "title": "US Economy",
                "keywords": ["economy", "consumer"],
                "boost_phrases": [],
            }
        ]
        story_drafts = [
            _story(
                "us-jobs",
                "US job market cools",
                "The US economy showed slower job growth and American households pulled back.",
            ),
            _story(
                "france-inflation",
                "French inflation eases",
                "France's economy reported lower inflation and weaker consumer spending.",
            ),
            _story(
                "tariff-impact",
                "Tariff impact remains unclear",
                "A trade dispute could affect US consumers, but the direct impact remains unclear.",
            ),
        ]

        selected, stats = classify_story_drafts_for_topics(
            story_drafts,
            topics,
            _runtime(
                [
                    {
                        "story_key": "us-jobs",
                        "topicality": "obviously_topical",
                        "scale": "not_obvious",
                        "topic_reason": "US workers and households are central.",
                    },
                    {
                        "story_key": "france-inflation",
                        "topicality": "obviously_not_topical",
                        "scale": "not_obvious",
                        "topic_reason": "France is the central affected country.",
                    },
                    {
                        "story_key": "tariff-impact",
                        "topicality": "not_obvious",
                        "scale": "not_obvious",
                        "topic_reason": "Possible direct US consumer impact.",
                    },
                ]
            ),
        )

        self.assertEqual(
            {story["story_key"] for story in selected},
            {"us-jobs", "tariff-impact"},
        )
        screening_stats = stats["story_topic_screening"]
        self.assertEqual(screening_stats["obvious_exclusion_count"], 1)
        self.assertEqual(screening_stats["topicality_counts"]["obviously_not_topical"], 1)
        rejected = stats["topics"]["US Economy"]["rejected"]
        self.assertEqual(rejected[0]["story_key"], "france-inflation")
        self.assertEqual(rejected[0]["reason"], "screened_out_by_story_topic_screening")
        self.assertEqual(rejected[0]["topic_screening_topicality"], "obviously_not_topical")

    def test_us_politics_screening_rejects_foreign_politics_with_incidental_us_mentions(self) -> None:
        topics = [
            {
                "key": "us_politics",
                "title": "US Politics",
                "keywords": ["government"],
                "boost_phrases": [],
            }
        ]
        story_drafts = [
            _story(
                "us-court",
                "US court reviews election rule",
                "The US government and a federal court reviewed an election rule.",
                topic_key="us_politics",
                topic_title="US Politics",
            ),
            _story(
                "uk-election",
                "UK election campaign turns to trade",
                "The UK government debated trade policy and mentioned US relations in passing.",
                topic_key="us_politics",
                topic_title="US Politics",
            ),
        ]

        selected, stats = classify_story_drafts_for_topics(
            story_drafts,
            topics,
            _runtime(
                [
                    {
                        "story_key": "us-court",
                        "topicality": "obviously_topical",
                        "scale": "not_obvious",
                        "topic_reason": "US legal jurisdiction and election rules are central.",
                    },
                    {
                        "story_key": "uk-election",
                        "topicality": "obviously_not_topical",
                        "scale": "not_obvious",
                        "topic_reason": "The UK election is central; US relations are incidental.",
                    },
                ]
            ),
        )

        self.assertEqual([story["story_key"] for story in selected], ["us-court"])
        self.assertEqual(stats["story_topic_screening"]["obvious_exclusion_count"], 1)
        rejected = stats["topics"]["US Politics"]["rejected"]
        self.assertEqual(rejected[0]["reason"], "screened_out_by_story_topic_screening")

    def test_deterministic_fallback_rejects_south_korea_market_story_with_us_dollars(self) -> None:
        topics = [
            {
                "key": "us_economy",
                "title": "US Economy",
                "keywords": ["economy", "market"],
                "boost_phrases": [],
            }
        ]
        story_drafts = [
            _story(
                "seoul-shares",
                "South Korean Stocks Rally on AI Tech Optimism",
                "South Korean stocks reached a new high as KOSPI rallied on AI and semiconductor optimism. "
                "South Korean exports rose 53 percent to US$87.8 billion, while Nvidia CEO Jensen Huang "
                "prepared to meet executives in Seoul. The story does not identify direct effects on US "
                "consumers, workers, households, Fed policy, broad US markets, or US trade policy.",
            )
        ]

        selected, stats = classify_story_drafts_for_topics(
            story_drafts,
            topics,
            _runtime(screening_error=RuntimeError("model unavailable")),
        )

        self.assertEqual(selected, [])
        screening_stats = stats["story_topic_screening"]
        self.assertEqual(screening_stats["fallback_kept_count"], 1)
        self.assertEqual(screening_stats["deterministic_fallback_count"], 1)
        self.assertEqual(screening_stats["obvious_exclusion_count"], 1)
        rejected = stats["topics"]["US Economy"]["rejected"]
        self.assertEqual(rejected[0]["story_key"], "seoul-shares")
        self.assertEqual(rejected[0]["topic_screening_topicality"], "obviously_not_topical")
        self.assertEqual(rejected[0]["reason"], "screened_out_by_story_topic_screening")


if __name__ == "__main__":
    unittest.main()
