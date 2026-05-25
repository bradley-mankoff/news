"""Assign drafted stories to configured topics and build topic-scoped reports."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from .story_clustering import cosine_similarity, story_similarity_terms
from .story_drafting import article_summary_lookup_by_id, report_summary_text
from .topic_matching import (
    BOILERPLATE_CONTENT_STOPWORDS,
    TOPIC_MATCH_STOPWORDS,
    WEAK_TOPIC_MATCH_TERMS,
    score_topic_text_match,
)


@dataclass(frozen=True)
class StoryTopicRuntime:
    max_stories_per_topic: int
    min_score: int
    diversity_min_distance: float
    model_max_input_tokens: int
    model_profile_key: str
    model_reference: str
    model_name: str
    model_backend: str
    relaxed_final_synthesis_guards: bool
    build_article_heading: Callable[[dict], str]
    format_article_metadata: Callable[[dict], str]
    format_topic_section_header: Callable[[str], str]
    final_synthesis_word_count: Callable[[str], int]
    is_low_confidence_report_entry: Callable[[str], bool]
    report_reference_key: Callable[[str], str]


def _story_topic_relevance_text(story: dict[str, Any]) -> str:
    return " ".join(
        [
            str(story.get("story_title") or ""),
            str(story.get("paragraph") or story.get("story_text") or ""),
            " ".join(str(summary) for summary in story.get("summaries", [])[:6]),
        ]
    )


def _story_topic_selection_vector(story: dict[str, Any]) -> Counter[str]:
    stopwords = (
        TOPIC_MATCH_STOPWORDS
        | WEAK_TOPIC_MATCH_TERMS
        | BOILERPLATE_CONTENT_STOPWORDS
        | {
            "article",
            "briefing",
            "coverage",
            "daily",
            "development",
            "developments",
            "newsletter",
            "official",
            "officials",
            "paragraph",
            "reported",
            "report",
            "reports",
            "said",
            "says",
            "source",
            "story",
            "summary",
            "update",
            "updates",
        }
    )
    return Counter(story_similarity_terms(_story_topic_relevance_text(story), stopwords))


def _story_topic_selection_similarity(
    left_vector: Counter[str],
    right_vector: Counter[str],
) -> float:
    if not left_vector or not right_vector:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_vector.values()))
    right_norm = math.sqrt(sum(value * value for value in right_vector.values()))
    return cosine_similarity(
        dict(left_vector),
        left_norm,
        dict(right_vector),
        right_norm,
    )


def _story_topic_selection_distance(
    left_vector: Counter[str],
    right_vector: Counter[str],
) -> float:
    return 1.0 - _story_topic_selection_similarity(left_vector, right_vector)


def classify_story_drafts_for_topics(
    story_drafts: list[dict[str, Any]],
    topics: list[dict],
    runtime: StoryTopicRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_matches: list[dict[str, Any]] = []
    topic_debug: dict[str, dict[str, Any]] = {}
    all_story_scores: list[dict[str, Any]] = []
    topic_order = [str(topic.get("key") or "") for topic in topics]
    topic_title_by_key = {
        str(topic.get("key") or ""): str(topic.get("title") or topic.get("key") or "Unknown topic")
        for topic in topics
    }
    topic_index_by_key = {topic_key: index for index, topic_key in enumerate(topic_order)}
    story_vectors = [_story_topic_selection_vector(story) for story in story_drafts]

    for story_index, story in enumerate(story_drafts):
        story_scores: dict[str, int] = {}
        story_text = _story_topic_relevance_text(story)
        for topic in topics:
            topic_key = str(topic.get("key") or "")
            raw_score = score_topic_text_match(topic, story_text)
            story_scores[topic_key] = raw_score

        eligible_topic_keys = [
            topic_key
            for topic_key in topic_order
            if int(story_scores.get(topic_key) or 0) >= runtime.min_score
        ]
        owned_topic_key = ""
        if eligible_topic_keys:
            owned_topic_key = sorted(
                eligible_topic_keys,
                key=lambda topic_key: (
                    -int(story_scores.get(topic_key) or 0),
                    topic_index_by_key.get(topic_key, len(topic_order)),
                ),
            )[0]

        all_story_scores.append(
            {
                "story_index": story_index,
                "story_key": story.get("story_key"),
                "story_title": story.get("story_title"),
                "scores_by_topic": story_scores,
                "best_score": max(story_scores.values(), default=0),
                "eligible_topic_keys": eligible_topic_keys,
                "owned_topic_key": owned_topic_key,
                "owned_topic_title": topic_title_by_key.get(owned_topic_key, ""),
                "ownership_reason": (
                    "highest_eligible_topic_fit_score"
                    if owned_topic_key
                    else "no_topic_above_story_topic_fit_min_score"
                ),
            }
        )

    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        owned_candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        eligible_candidate_count = 0
        for story_index, (story, score_record) in enumerate(zip(story_drafts, all_story_scores)):
            raw_score = int((score_record.get("scores_by_topic") or {}).get(topic_key) or 0)
            owned_topic_key = str(score_record.get("owned_topic_key") or "")
            candidate_record = {
                **story,
                "story_index": story_index,
                "topic_key": topic_key,
                "topic_title": topic_title,
                "topic_fit_score": raw_score,
                "owned_topic_key": owned_topic_key,
                "owned_topic_title": score_record.get("owned_topic_title", ""),
            }
            if raw_score < runtime.min_score:
                rejected.append(
                    {
                        "story_key": story.get("story_key"),
                        "story_title": story.get("story_title"),
                        "topic_fit_score": raw_score,
                        "owned_topic_key": owned_topic_key,
                        "owned_topic_title": score_record.get("owned_topic_title", ""),
                        "reason": "below_story_topic_fit_min_score",
                    }
                )
                continue

            eligible_candidate_count += 1
            if owned_topic_key != topic_key:
                rejected.append(
                    {
                        "story_key": story.get("story_key"),
                        "story_title": story.get("story_title"),
                        "topic_fit_score": raw_score,
                        "owned_topic_key": owned_topic_key,
                        "owned_topic_title": score_record.get("owned_topic_title", ""),
                        "owned_topic_score": int(
                            (score_record.get("scores_by_topic") or {}).get(owned_topic_key) or 0
                        ),
                        "reason": "assigned_to_better_topic",
                    }
                )
                continue

            owned_candidates.append(candidate_record)

        def candidate_rank(story: dict[str, Any]) -> tuple:
            return (
                -int(story.get("topic_fit_score") or 0),
                -float(story.get("story_strength_score") or 0.0),
                -int(story.get("source_count") or 0),
                -int(story.get("article_count") or 0),
                -float(story.get("average_similarity") or 0.0),
                int(story.get("story_rank") or 0),
                str(story.get("story_title") or ""),
            )

        ranked = sorted(owned_candidates, key=candidate_rank)
        selected_for_topic: list[dict[str, Any]] = []
        selected_vectors: list[Counter[str]] = []
        for candidate in ranked:
            story_index = int(candidate.get("story_index") or 0)
            candidate_vector = story_vectors[story_index] if story_index < len(story_vectors) else Counter()
            distances = [
                _story_topic_selection_distance(candidate_vector, selected_vector)
                for selected_vector in selected_vectors
            ]
            min_distance_to_selected = min(distances, default=1.0)
            candidate_with_distance = {
                **candidate,
                "min_distance_to_selected": round(min_distance_to_selected, 4)
                if selected_for_topic
                else None,
            }
            if len(selected_for_topic) >= runtime.max_stories_per_topic:
                rejected.append(
                    {
                        "story_key": candidate.get("story_key"),
                        "story_title": candidate.get("story_title"),
                        "topic_fit_score": candidate.get("topic_fit_score"),
                        "owned_topic_key": candidate.get("owned_topic_key"),
                        "owned_topic_title": candidate.get("owned_topic_title"),
                        "min_distance_to_selected": candidate_with_distance["min_distance_to_selected"],
                        "reason": "topic_story_limit_reached",
                    }
                )
                continue
            if (
                selected_for_topic
                and min_distance_to_selected < runtime.diversity_min_distance
            ):
                rejected.append(
                    {
                        "story_key": candidate.get("story_key"),
                        "story_title": candidate.get("story_title"),
                        "topic_fit_score": candidate.get("topic_fit_score"),
                        "owned_topic_key": candidate.get("owned_topic_key"),
                        "owned_topic_title": candidate.get("owned_topic_title"),
                        "min_distance_to_selected": round(min_distance_to_selected, 4),
                        "reason": "below_topic_story_diversity_min_distance",
                    }
                )
                continue
            selected_for_topic.append(candidate_with_distance)
            selected_vectors.append(candidate_vector)

        selected_matches.extend(selected_for_topic)
        topic_debug[topic_title] = {
            "topic_key": topic_key,
            "selected_count": len(selected_for_topic),
            "candidate_count": eligible_candidate_count,
            "owned_candidate_count": len(owned_candidates),
            "min_score": runtime.min_score,
            "diversity_min_distance": runtime.diversity_min_distance,
            "selected": [
                {
                    "story_key": story.get("story_key"),
                    "story_title": story.get("story_title"),
                    "topic_fit_score": story.get("topic_fit_score"),
                    "owned_topic_key": story.get("owned_topic_key"),
                    "owned_topic_title": story.get("owned_topic_title"),
                    "min_distance_to_selected": story.get("min_distance_to_selected"),
                    "article_count": story.get("article_count"),
                    "source_count": story.get("source_count"),
                    "story_strength_score": story.get("story_strength_score"),
                    "average_similarity": story.get("average_similarity"),
                    "article_ids": story.get("article_ids", []),
                    "preview": str(story.get("paragraph") or "")[:500],
                }
                for story in selected_for_topic
            ],
            "rejected": sorted(
                rejected,
                key=lambda item: (-int(item.get("topic_fit_score") or 0), str(item.get("story_title") or "")),
            )[:20],
        }

    selected_by_topic = {
        topic_title: details["selected_count"]
        for topic_title, details in topic_debug.items()
    }
    return selected_matches, {
        "enabled": True,
        "story_count": len(story_drafts),
        "selected_story_topic_count": len(selected_matches),
        "max_stories_per_topic": runtime.max_stories_per_topic,
        "min_score": runtime.min_score,
        "topic_story_diversity_min_distance": runtime.diversity_min_distance,
        "selected_by_topic": selected_by_topic,
        "topics": topic_debug,
        "all_story_scores": all_story_scores,
    }


def _annotate_summary_entry_for_topic_story(
    entry: str,
    article: dict,
    story_match: dict[str, Any],
    runtime: StoryTopicRuntime,
) -> str:
    title_match = re.search(r"^###\s+(.+)$", entry or "", flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else runtime.build_article_heading(article)
    summary_text = report_summary_text(entry)
    annotated_article = {
        **article,
        "topic_key": story_match.get("topic_key"),
        "topic_title": story_match.get("topic_title"),
        "story_key": story_match.get("story_key"),
        "story_title": story_match.get("story_title"),
    }
    return (
        f"### {title}\n"
        "Metadata:\n"
        f"{runtime.format_article_metadata(annotated_article)}\n\n"
        "Summary:\n"
        f"{summary_text}"
    )


def build_topic_assigned_article_reports(
    selected_story_topic_matches: list[dict[str, Any]],
    article_summary_reports: list[str],
    article_targets: list[dict],
    topics: list[dict],
    runtime: StoryTopicRuntime,
) -> tuple[list[str], dict[str, Any]]:
    summary_lookup = article_summary_lookup_by_id(article_summary_reports)
    article_lookup = {
        str(article.get("article_id") or ""): article
        for article in article_targets
        if article.get("article_id")
    }
    matches_by_topic: dict[str, list[dict[str, Any]]] = {}
    for match in selected_story_topic_matches:
        matches_by_topic.setdefault(str(match.get("topic_key") or ""), []).append(match)

    reports: list[str] = []
    selected_article_ids: set[str] = set()
    missing_summary_ids: list[str] = []
    seen_records: set[tuple[str, str, str]] = set()
    for topic in topics:
        topic_key = str(topic.get("key") or "")
        for match in matches_by_topic.get(topic_key, []):
            story_key = str(match.get("story_key") or "")
            for article_id in match.get("article_ids", []):
                clean_article_id = str(article_id or "").strip()
                if not clean_article_id:
                    continue
                dedupe_key = (topic_key, story_key, clean_article_id)
                if dedupe_key in seen_records:
                    continue
                seen_records.add(dedupe_key)
                entry = summary_lookup.get(clean_article_id)
                article = article_lookup.get(clean_article_id)
                if not entry or not article:
                    missing_summary_ids.append(clean_article_id)
                    continue
                reports.append(_annotate_summary_entry_for_topic_story(entry, article, match, runtime))
                selected_article_ids.add(clean_article_id)

    return reports, {
        "candidate_story_topic_count": len(selected_story_topic_matches),
        "included_report_count": len(reports),
        "selected_unique_article_count": len(selected_article_ids),
        "missing_summary_article_ids": sorted(set(missing_summary_ids)),
    }


def _story_topic_primary_dataset(
    selected_story_topic_matches: list[dict[str, Any]],
    topics: list[dict],
) -> str:
    topic_order = [str(topic.get("key") or "") for topic in topics]
    matches_by_topic: dict[str, list[dict[str, Any]]] = {}
    for match in selected_story_topic_matches:
        matches_by_topic.setdefault(str(match.get("topic_key") or ""), []).append(match)

    sections: list[str] = []
    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        for story in matches_by_topic.get(topic_key, []):
            lines = [
                f"Topic: {topic_title}",
                f"Story: {story.get('story_title')}",
                f"Topic fit score: {story.get('topic_fit_score')}",
                f"Article IDs: {', '.join(str(article_id) for article_id in story.get('article_ids', []))}",
                "Story draft:",
                str(story.get("paragraph") or story.get("story_text") or "").strip(),
            ]
            sections.append("\n".join(lines))

    remaining_topic_keys = sorted(key for key in matches_by_topic if key not in topic_order)
    for topic_key in remaining_topic_keys:
        for story in matches_by_topic[topic_key]:
            lines = [
                f"Topic: {story.get('topic_title') or topic_key}",
                f"Story: {story.get('story_title')}",
                f"Topic fit score: {story.get('topic_fit_score')}",
                "Story draft:",
                str(story.get("paragraph") or story.get("story_text") or "").strip(),
            ]
            sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)


def build_precomputed_story_synthesis(
    selected_story_topic_matches: list[dict[str, Any]],
    topics: list[dict],
    reference_reports: list[str],
    runtime: StoryTopicRuntime,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    matches_by_topic: dict[str, list[dict[str, Any]]] = {}
    for match in selected_story_topic_matches:
        matches_by_topic.setdefault(str(match.get("topic_key") or ""), []).append(match)

    sections: list[str] = []
    required_topic_titles: list[str] = []
    required_story_blocks_by_topic: dict[str, list[str]] = {}
    attempts: list[dict[str, Any]] = []
    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        story_matches = matches_by_topic.get(topic_key) or []
        paragraphs: list[str] = []
        for story in story_matches:
            paragraph = str(story.get("paragraph") or story.get("story_text") or "").strip()
            if not paragraph:
                continue
            paragraphs.append(paragraph)
            required_story_blocks_by_topic.setdefault(topic_title, []).append(
                str(story.get("story_title") or "Story update")
            )
            attempts.append(
                {
                    "topic": topic_title,
                    "story": story.get("story_title"),
                    "valid": True,
                    "reason": "precomputed_story_draft",
                    "word_count": runtime.final_synthesis_word_count(paragraph),
                    "topic_fit_score": story.get("topic_fit_score"),
                    "preview": paragraph[:500],
                }
            )
        if paragraphs:
            required_topic_titles.append(topic_title)
            sections.append(
                f"## {runtime.format_topic_section_header(topic_title)}\n"
                + "\n\n".join(paragraphs)
            )

    final_synthesis = "\n\n".join(sections)
    token_stats = {
        "synthesis_method": "precomputed_story_drafts_post_topic_classification",
        "total_reports": len(reference_reports),
        "reports_included_in_synthesis": len(reference_reports),
        "reports_omitted_from_synthesis": 0,
        "high_confidence_reports": len([entry for entry in reference_reports if not runtime.is_low_confidence_report_entry(entry)]),
        "low_confidence_reports": len([entry for entry in reference_reports if runtime.is_low_confidence_report_entry(entry)]),
        "story_blocks_included": sum(len(stories) for stories in required_story_blocks_by_topic.values()),
        "model_max_input_tokens": runtime.model_max_input_tokens,
        "model_profile": runtime.model_profile_key,
        "model": runtime.model_reference,
        "model_name": runtime.model_name,
        "model_backend": runtime.model_backend,
        "topic_count": len(topics),
        "required_topic_titles": required_topic_titles,
        "required_topic_headings": [
            runtime.format_topic_section_header(topic_title)
            for topic_title in required_topic_titles
        ],
        "required_story_blocks_by_topic": required_story_blocks_by_topic,
        "eligible_story_block_count": sum(len(stories) for stories in required_story_blocks_by_topic.values()),
        "explicit_story_mode": True,
        "primary_dataset": _story_topic_primary_dataset(selected_story_topic_matches, topics),
        "included_report_keys": [runtime.report_reference_key(entry) for entry in reference_reports],
    }
    debug = {
        "attempts": attempts,
        "relaxed_guards": runtime.relaxed_final_synthesis_guards,
        "dev_fallback_used": False,
        "synthesis_method": "precomputed_story_drafts_post_topic_classification",
    }
    return final_synthesis, token_stats, debug
