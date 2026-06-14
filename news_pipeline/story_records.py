"""Story record normalization and lifecycle projections."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class StoryRecord:
    story_key: str
    story_title: str
    article_ids: tuple[str, ...]
    cluster_article_ids: tuple[str, ...]
    article_count: int
    cluster_article_count: int
    selected_article_count: int
    source_count: int | None = None
    average_similarity: float | None = None
    connectedness_score: float | None = None
    story_strength_score: float | None = None
    edge_density: float | None = None
    mean_best_similarity: float | None = None
    min_best_similarity: float | None = None
    min_member_average_similarity: float | None = None
    min_member_edge_degree: int | None = None
    member_cohesion_floor: float | None = None
    member_edge_degree_floor: int | None = None
    pruned_article_ids: tuple[str, ...] = ()
    prune_reason: str = ""
    story_rank: int = 0
    global_selection_rank: int | None = None
    extras: dict[str, Any] | None = None


def ordered_unique_article_ids(raw_article_ids: list[Any] | tuple[Any, ...]) -> list[str]:
    seen_article_ids: set[str] = set()
    article_ids: list[str] = []
    for article_id in raw_article_ids:
        clean_article_id = str(article_id or "").strip()
        if not clean_article_id or clean_article_id in seen_article_ids:
            continue
        seen_article_ids.add(clean_article_id)
        article_ids.append(clean_article_id)
    return article_ids


def ensure_story_record(value: StoryRecord | dict[str, Any], *, index: int = 0) -> StoryRecord:
    """Adapt legacy story dicts into StoryRecord.

    Dict conversion is permissive because diagnostics and debug artifacts may
    omit fields added after they were written.
    """
    if isinstance(value, StoryRecord):
        return value

    article_ids = ordered_unique_article_ids(list(value.get("article_ids") or []))
    cluster_article_ids = ordered_unique_article_ids(list(value.get("cluster_article_ids") or []))
    if not article_ids:
        article_ids = list(cluster_article_ids)
    if not cluster_article_ids:
        cluster_article_ids = list(article_ids)

    story_title = str(value.get("story_title") or value.get("title") or "News update")
    story_key = str(value.get("story_key") or "").strip()
    if not story_key:
        story_key = f"global-story-{index + 1:02d}" if index >= 0 else ""

    extras = {
        key: item
        for key, item in value.items()
        if key not in _STORY_RECORD_KEYS
    }
    return StoryRecord(
        story_key=story_key,
        story_title=story_title,
        article_ids=tuple(article_ids),
        cluster_article_ids=tuple(cluster_article_ids),
        article_count=int(value.get("article_count") or len(cluster_article_ids)),
        cluster_article_count=int(value.get("cluster_article_count") or len(cluster_article_ids)),
        selected_article_count=int(value.get("selected_article_count") or len(article_ids)),
        source_count=_optional_int(value.get("source_count")),
        average_similarity=_optional_float(value.get("average_similarity")),
        connectedness_score=_optional_float(value.get("connectedness_score")),
        story_strength_score=_optional_float(value.get("story_strength_score")),
        edge_density=_optional_float(value.get("edge_density")),
        mean_best_similarity=_optional_float(value.get("mean_best_similarity")),
        min_best_similarity=_optional_float(value.get("min_best_similarity")),
        min_member_average_similarity=_optional_float(value.get("min_member_average_similarity")),
        min_member_edge_degree=_optional_int(value.get("min_member_edge_degree")),
        member_cohesion_floor=_optional_float(value.get("member_cohesion_floor")),
        member_edge_degree_floor=_optional_int(value.get("member_edge_degree_floor")),
        pruned_article_ids=tuple(ordered_unique_article_ids(list(value.get("pruned_article_ids") or []))),
        prune_reason=str(value.get("prune_reason") or ""),
        story_rank=int(value.get("story_rank") or 0),
        global_selection_rank=_optional_int(value.get("global_selection_rank")),
        extras=extras or None,
    )


def to_story_dict(record: StoryRecord | dict[str, Any]) -> dict[str, Any]:
    story = ensure_story_record(record)
    result: dict[str, Any] = dict(story.extras or {})
    result.update(
        {
            "story_key": story.story_key,
            "story_title": story.story_title,
            "article_count": story.article_count,
            "cluster_article_count": story.cluster_article_count,
            "selected_article_count": story.selected_article_count,
            "article_ids": list(story.article_ids),
            "cluster_article_ids": list(story.cluster_article_ids),
        }
    )
    _set_optional(result, "source_count", story.source_count)
    _set_optional(result, "average_similarity", story.average_similarity)
    _set_optional(result, "connectedness_score", story.connectedness_score)
    _set_optional(result, "story_strength_score", story.story_strength_score)
    _set_optional(result, "edge_density", story.edge_density)
    _set_optional(result, "mean_best_similarity", story.mean_best_similarity)
    _set_optional(result, "min_best_similarity", story.min_best_similarity)
    _set_optional(result, "min_member_average_similarity", story.min_member_average_similarity)
    _set_optional(result, "min_member_edge_degree", story.min_member_edge_degree)
    _set_optional(result, "member_cohesion_floor", story.member_cohesion_floor)
    _set_optional(result, "member_edge_degree_floor", story.member_edge_degree_floor)
    if story.pruned_article_ids:
        result["pruned_article_ids"] = list(story.pruned_article_ids)
    if story.prune_reason:
        result["prune_reason"] = story.prune_reason
    if story.story_rank:
        result["story_rank"] = story.story_rank
    _set_optional(result, "global_selection_rank", story.global_selection_rank)
    return result


def story_article_ids(record: StoryRecord | dict[str, Any]) -> list[str]:
    story = ensure_story_record(record)
    if story.article_ids:
        return list(story.article_ids)
    return list(story.cluster_article_ids)


def with_budgeted_article_ids(
    record: StoryRecord | dict[str, Any],
    article_ids: list[str],
) -> StoryRecord:
    story = ensure_story_record(record)
    budgeted_ids = ordered_unique_article_ids(article_ids)
    budgeted_id_set = set(budgeted_ids)
    story_article_ids = [
        article_id
        for article_id in story.article_ids
        if article_id in budgeted_id_set
    ] or budgeted_ids
    cluster_article_ids = [
        article_id
        for article_id in story.cluster_article_ids
        if article_id in budgeted_id_set
    ] or story_article_ids
    article_count = len(cluster_article_ids)
    return replace(
        story,
        article_ids=tuple(story_article_ids),
        cluster_article_ids=tuple(cluster_article_ids),
        article_count=article_count,
        cluster_article_count=article_count,
        selected_article_count=len(story_article_ids),
    )


def story_article_id_set(record: StoryRecord | dict[str, Any]) -> set[str]:
    story = ensure_story_record(record)
    return set(story.cluster_article_ids or story.article_ids)


def story_article_overlap(
    left: StoryRecord | dict[str, Any],
    right: StoryRecord | dict[str, Any],
) -> tuple[float, set[str]]:
    left_ids = story_article_id_set(left)
    right_ids = story_article_id_set(right)
    if not left_ids or not right_ids:
        return 0.0, set()
    shared_ids = left_ids & right_ids
    return len(shared_ids) / max(1, min(len(left_ids), len(right_ids))), shared_ids


def story_rank_key(record: StoryRecord | dict[str, Any]) -> tuple:
    story = ensure_story_record(record)
    return (
        -float(story.story_strength_score or 0.0),
        -int(story.source_count or 0),
        -int(story.article_count or 0),
        -float(story.average_similarity or 0.0),
        int(story.story_rank or 0),
        story.story_title,
    )


def story_debug_record(record: StoryRecord | dict[str, Any]) -> dict[str, Any]:
    story = ensure_story_record(record)
    extras = story.extras or {}
    result = {
        "story_key": story.story_key,
        "story_title": story.story_title,
        "global_selection_rank": story.global_selection_rank,
        "article_count": story.article_count,
        "source_count": story.source_count,
        "story_strength_score": story.story_strength_score,
        "average_similarity": story.average_similarity,
        "scale_screening_scale": extras.get("scale_screening_scale"),
        "scale_screening_reason": extras.get("scale_screening_reason"),
        "article_ids": list(story.article_ids),
        "cluster_article_ids": list(story.cluster_article_ids),
        "preview": str(extras.get("paragraph") or "")[:500],
    }
    return result


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_optional(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


_STORY_RECORD_KEYS = {
    "story_key",
    "story_title",
    "title",
    "article_count",
    "cluster_article_count",
    "selected_article_count",
    "article_ids",
    "cluster_article_ids",
    "source_count",
    "average_similarity",
    "connectedness_score",
    "story_strength_score",
    "edge_density",
    "mean_best_similarity",
    "min_best_similarity",
    "min_member_average_similarity",
    "min_member_edge_degree",
    "member_cohesion_floor",
    "member_edge_degree_floor",
    "pruned_article_ids",
    "prune_reason",
    "story_rank",
    "global_selection_rank",
}
