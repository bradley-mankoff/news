"""Story clustering for article targets.

This module owns the story-similarity knobs and the graph-style grouping used
before article summarization.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from .text_cleaning import clean_article_text, clean_content_text
from .topic_matching import (
    BOILERPLATE_CONTENT_STOPWORDS,
    TOPIC_MATCH_STOPWORDS,
    WEAK_TOPIC_MATCH_TERMS,
    clean_topic_source_title,
    ordered_topic_match_terms,
)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bounded_env_float(name: str, default: float, *, lower: float = 0.0, upper: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(upper, max(lower, value))


try:
    MIN_ARTICLES_PER_STORY = max(2, int(os.getenv("NEWS_MIN_ARTICLES_PER_STORY", "4")))
except ValueError:
    MIN_ARTICLES_PER_STORY = 2
try:
    MAX_ARTICLES_PER_STORY = max(
        MIN_ARTICLES_PER_STORY,
        int(os.getenv("NEWS_MAX_ARTICLES_PER_STORY", "4")),
    )
except ValueError:
    MAX_ARTICLES_PER_STORY = MIN_ARTICLES_PER_STORY

STORY_CLUSTER_SIMILARITY_THRESHOLD = _bounded_env_float(
    "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD",
    0.30,
)
STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD = _bounded_env_float(
    "NEWS_STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD",
    0.34,
)
STORY_SIMILARITY_TITLE_WEIGHT = 1
STORY_SIMILARITY_DESCRIPTION_WEIGHT = 1
STORY_SIMILARITY_TEXT_WEIGHT = 4
STORY_MIN_SOURCE_COUNT = 2
STORY_PAIR_DEBUG_LIMIT = 250


def strip_model_artifacts(text: str) -> str:
    text = text or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|im_(?:start|end)\|>", "", text)
    text = re.sub(r"&lt;/?(?:analysis|content)&gt;", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:analysis|content)>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"&lt;/?topic&gt;", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?topic>", "", text, flags=re.IGNORECASE)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_inline_markdown(text: str) -> str:
    clean_text = str(text or "")
    clean_text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", clean_text)
    clean_text = re.sub(r"`([^`]*)`", r"\1", clean_text)
    clean_text = re.sub(r"\*\*(.*?)\*\*", r"\1", clean_text)
    clean_text = re.sub(r"\*(.*?)\*", r"\1", clean_text)
    clean_text = re.sub(r"__(.*?)__", r"\1", clean_text)
    clean_text = re.sub(r"_(.*?)_", r"\1", clean_text)
    return clean_text


def _parse_feed_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = parsedate_to_datetime(raw_value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _article_sort_datetime(article: dict) -> datetime:
    parsed = _parse_feed_datetime(article.get("pub_date"))
    return parsed or datetime.min.replace(tzinfo=None)


def _article_time_rank(article: dict) -> tuple[int, int, int]:
    parsed = _article_sort_datetime(article)
    return (
        parsed.toordinal(),
        parsed.hour * 3600 + parsed.minute * 60 + parsed.second,
        parsed.microsecond,
    )


def _story_slug(value: str, fallback: str = "story") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or fallback


def _clean_story_title(value: str, topic: dict, articles: list[dict]) -> str:
    clean_value = strip_inline_markdown(strip_model_artifacts(value or ""))
    clean_value = re.sub(r"[\r\n]+", " ", clean_value)
    clean_value = re.sub(r"\s+", " ", clean_value).strip(" \"'")
    topic_title = str(topic.get("title") or "").strip()
    if not clean_value or clean_value.lower() == topic_title.lower():
        first_title = str((articles[0] if articles else {}).get("title") or "").strip()
        clean_value = clean_topic_source_title(first_title) or topic_title or "News update"
    words = clean_value.split()
    if len(words) > 14:
        clean_value = " ".join(words[:14])
    return clean_value[:120].strip() or "News update"


def _story_similarity_stopwords(topic: dict) -> set[str]:
    broad_topic_terms = set(
        ordered_topic_match_terms(topic.get("key"), topic.get("title"), topic.get("rationale"))
    )
    generic_terms = TOPIC_MATCH_STOPWORDS | {
        "article",
        "briefing",
        "coverage",
        "daily",
        "development",
        "developments",
        "newsletter",
        "official",
        "officials",
        "reported",
        "report",
        "reports",
        "said",
        "says",
        "source",
        "story",
        "update",
        "updates",
    }
    return broad_topic_terms | generic_terms | WEAK_TOPIC_MATCH_TERMS | BOILERPLATE_CONTENT_STOPWORDS


def _normalize_story_similarity_token(token: str) -> str:
    normalized = token.strip("'")
    if normalized.endswith("'s"):
        normalized = normalized[:-2]
    if normalized == "hits":
        normalized = "hit"
    elif len(normalized) > 4 and normalized.endswith("ies"):
        normalized = normalized[:-3] + "y"
    elif (
        len(normalized) > 4
        and normalized.endswith("s")
        and not normalized.endswith(("ss", "virus"))
    ):
        normalized = normalized[:-1]
    return normalized


def story_similarity_terms(text: str, stopwords: set[str]) -> list[str]:
    terms: list[str] = []
    clean_text = clean_content_text(text)
    for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", clean_text.lower()):
        token_variants = [token]
        if "-" in token:
            token_variants.extend(part for part in token.split("-") if part)
        for variant in token_variants:
            normalized = _normalize_story_similarity_token(variant)
            if len(normalized) < 3 or normalized.isdigit() or normalized in stopwords:
                continue
            terms.append(normalized)
    return terms


def _story_weighted_term_counts(article: dict, topic: dict) -> Counter[str]:
    stopwords = _story_similarity_stopwords(topic)
    article_body_text = clean_article_text(
        str(article.get("text") or ""),
        source=str(article.get("source") or ""),
        url=str(article.get("resolved_url") or article.get("url") or ""),
        title=str(article.get("title") or ""),
    )
    weighted_parts = [
        (
            clean_content_text(clean_topic_source_title(str(article.get("title") or ""))),
            STORY_SIMILARITY_TITLE_WEIGHT,
        ),
        (clean_content_text(str(article.get("description") or "")), STORY_SIMILARITY_DESCRIPTION_WEIGHT),
        (article_body_text, STORY_SIMILARITY_TEXT_WEIGHT),
    ]
    counts: Counter[str] = Counter()
    for text, weight in weighted_parts:
        for term in story_similarity_terms(text, stopwords):
            counts[term] += weight
    return counts


def _build_story_tfidf_vectors(
    topic: dict,
    articles: list[dict],
) -> tuple[list[dict[str, float]], list[float]]:
    document_counts = [_story_weighted_term_counts(article, topic) for article in articles]
    document_frequency: Counter[str] = Counter()
    for counts in document_counts:
        document_frequency.update(counts.keys())

    total_documents = max(1, len(document_counts))
    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for counts in document_counts:
        vector: dict[str, float] = {}
        for term, count in counts.items():
            tf = 1.0 + math.log(max(1, count))
            idf = math.log((1 + total_documents) / (1 + document_frequency[term])) + 1.0
            vector[term] = tf * idf
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        vectors.append(vector)
        norms.append(norm)
    return vectors, norms


def cosine_similarity(
    left: dict[str, float],
    left_norm: float,
    right: dict[str, float],
    right_norm: float,
) -> float:
    if not left_norm or not right_norm:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot_product = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    if dot_product <= 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)


def _story_pair_key(left_index: int, right_index: int) -> tuple[int, int]:
    return (left_index, right_index) if left_index < right_index else (right_index, left_index)


def _story_pair_similarity(
    similarities: dict[tuple[int, int], float],
    left_index: int,
    right_index: int,
) -> float:
    if left_index == right_index:
        return 1.0
    return similarities.get(_story_pair_key(left_index, right_index), 0.0)


def _story_component_average_similarity(
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> float:
    if len(component) < 2:
        return 0.0
    total = 0.0
    pair_count = 0
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            total += _story_pair_similarity(similarities, left_index, right_index)
            pair_count += 1
    return total / pair_count if pair_count else 0.0


def _story_component_pair_count(component: list[int]) -> int:
    size = len(component)
    return (size * (size - 1)) // 2


def _story_component_edge_count(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> int:
    edge_count = 0
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            if _story_pair_similarity(similarities, left_index, right_index) >= similarity_threshold:
                edge_count += 1
    return edge_count


def _story_component_best_similarities(
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> list[float]:
    best_scores: list[float] = []
    for index in component:
        other_scores = [
            _story_pair_similarity(similarities, index, other_index)
            for other_index in component
            if other_index != index
        ]
        best_scores.append(max(other_scores, default=0.0))
    return best_scores


def _minimum_story_edge_density(component_size: int) -> float:
    if component_size <= 2:
        return 1.0
    if component_size == 3:
        return 2.0 / 3.0
    if component_size <= 5:
        return 0.50
    return 0.45


def _story_component_connectedness_metrics(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> dict[str, float | int]:
    pair_count = _story_component_pair_count(component)
    edge_count = _story_component_edge_count(
        component,
        similarities,
        similarity_threshold,
    )
    average_similarity = _story_component_average_similarity(component, similarities)
    best_scores = _story_component_best_similarities(component, similarities)
    mean_best_similarity = sum(best_scores) / len(best_scores) if best_scores else 0.0
    min_best_similarity = min(best_scores, default=0.0)
    edge_density = edge_count / pair_count if pair_count else 0.0
    connectedness_score = (
        (mean_best_similarity * 0.40)
        + (average_similarity * 0.25)
        + (edge_density * 0.20)
        + (min_best_similarity * 0.15)
    )
    support_multiplier = math.log1p(max(1, len(component)))
    story_strength_score = connectedness_score * support_multiplier
    return {
        "pair_count": pair_count,
        "edge_count": edge_count,
        "edge_density": edge_density,
        "average_similarity": average_similarity,
        "mean_best_similarity": mean_best_similarity,
        "min_best_similarity": min_best_similarity,
        "connectedness_score": connectedness_score,
        "story_strength_score": story_strength_score,
    }


def _story_component_source_count(component: list[int], source_identities: list[str]) -> int:
    return len({source_identities[index] for index in component})


def _story_component_has_source_diversity(component: list[int], source_identities: list[str]) -> bool:
    return _story_component_source_count(component, source_identities) >= min(
        STORY_MIN_SOURCE_COUNT,
        len(component),
    )


def _story_subcomponents_from_adjacency(
    component: list[int],
    adjacency: dict[int, set[int]],
) -> list[list[int]]:
    component_set = set(component)
    subcomponents: list[list[int]] = []
    visited: set[int] = set()
    for start_index in sorted(component):
        if start_index in visited:
            continue
        stack = [start_index]
        visited.add(start_index)
        subcomponent: list[int] = []
        while stack:
            index = stack.pop()
            subcomponent.append(index)
            for neighbor in adjacency.get(index, set()):
                if neighbor not in component_set or neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)
        subcomponents.append(sorted(subcomponent))
    return subcomponents


def _story_similarity_edges(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> list[tuple[float, int, int]]:
    edges: list[tuple[float, int, int]] = []
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            score = _story_pair_similarity(similarities, left_index, right_index)
            if score >= similarity_threshold:
                edges.append((score, left_index, right_index))
    return edges


def _story_component_meets_connectedness_floor(
    component: list[int],
    source_identities: list[str],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
    *,
    min_articles_per_story: int,
) -> bool:
    if len(component) < min_articles_per_story:
        return False
    if not _story_component_has_source_diversity(component, source_identities):
        return False
    metrics = _story_component_connectedness_metrics(
        component,
        similarities,
        similarity_threshold,
    )
    if float(metrics["min_best_similarity"]) < similarity_threshold:
        return False
    return float(metrics["edge_density"]) >= _minimum_story_edge_density(len(component))


def _split_story_component_by_weak_bridges(
    component: list[int],
    source_identities: list[str],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
    *,
    min_articles_per_story: int,
) -> list[list[int]]:
    if len(component) < min_articles_per_story:
        return []
    edges = _story_similarity_edges(
        component,
        similarities,
        similarity_threshold,
    )
    if not edges:
        return []

    base_metrics = _story_component_connectedness_metrics(
        component,
        similarities,
        similarity_threshold,
    )
    base_strength = float(base_metrics["story_strength_score"])
    for _, left_index, right_index in sorted(edges, key=lambda edge: (edge[0], edge[1], edge[2])):
        adjacency: dict[int, set[int]] = {index: set() for index in component}
        for _edge_score, edge_left, edge_right in edges:
            if (edge_left, edge_right) == (left_index, right_index):
                continue
            adjacency[edge_left].add(edge_right)
            adjacency[edge_right].add(edge_left)
        subcomponents = _story_subcomponents_from_adjacency(component, adjacency)
        if len(subcomponents) <= 1:
            continue
        retained = [
            subcomponent
            for subcomponent in subcomponents
            if _story_component_meets_connectedness_floor(
                subcomponent,
                source_identities,
                similarities,
                similarity_threshold,
                min_articles_per_story=min_articles_per_story,
            )
        ]
        if len(retained) < 2:
            continue
        split_strength = sum(
            float(
                _story_component_connectedness_metrics(
                    subcomponent,
                    similarities,
                    similarity_threshold,
                )["story_strength_score"]
            )
            for subcomponent in retained
        )
        if split_strength <= base_strength * 1.05:
            continue
        split_components: list[list[int]] = []
        for subcomponent in retained:
            split_components.extend(
                _split_story_component_by_weak_bridges(
                    subcomponent,
                    source_identities,
                    similarities,
                    similarity_threshold,
                    min_articles_per_story=min_articles_per_story,
                )
            )
        return split_components

    if _story_component_meets_connectedness_floor(
        component,
        source_identities,
        similarities,
        similarity_threshold,
        min_articles_per_story=min_articles_per_story,
    ):
        return [component]
    return []


def _story_index_average_similarity(
    index: int,
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> float:
    other_indexes = [other_index for other_index in component if other_index != index]
    if not other_indexes:
        return 0.0
    return sum(
        _story_pair_similarity(similarities, index, other_index)
        for other_index in other_indexes
    ) / len(other_indexes)


def _story_component_medoid_index(
    component: list[int],
    articles: list[dict],
    similarities: dict[tuple[int, int], float],
) -> int:
    def rank(index: int) -> tuple:
        article = articles[index]
        return (
            -_story_index_average_similarity(index, component, similarities),
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            index,
        )

    return sorted(component, key=rank)[0]


def _story_cluster_relevance_score(component: list[int], articles: list[dict]) -> float:
    if not component:
        return 0.0
    return sum(float(articles[index].get("relevance_score") or 0.0) for index in component) / len(component)


def _story_cluster_recency_rank(component: list[int], articles: list[dict]) -> tuple[int, int, int]:
    if not component:
        return (0, 0, 0)
    return max(_article_time_rank(articles[index]) for index in component)


def _article_source_identity(article: dict) -> str:
    source = str(article.get("source") or "").strip().lower()
    if source:
        return re.sub(r"\s+", " ", source)
    for key in ("resolved_url", "url", "original_rss_url"):
        parsed = urlparse(str(article.get(key) or ""))
        host = parsed.netloc.lower().removeprefix("www.")
        if host:
            return host
    return "unknown"


def _select_story_article_indexes(
    component: list[int],
    medoid_index: int,
    articles: list[dict],
    similarities: dict[tuple[int, int], float],
    *,
    max_articles_per_story: int,
) -> list[int]:
    limit = max(1, max_articles_per_story)

    def article_rank(index: int) -> tuple:
        article = articles[index]
        return (
            0 if index == medoid_index else 1,
            -_story_index_average_similarity(index, component, similarities),
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            str(article.get("source") or ""),
            index,
        )

    ranked_indexes = sorted(component, key=article_rank)
    source_count = len({_article_source_identity(articles[index]) for index in component})
    selected: list[int] = []
    selected_sources: set[str] = set()

    for index in ranked_indexes:
        if len(selected) >= limit:
            break
        source = _article_source_identity(articles[index])
        if source in selected_sources and len(selected_sources) < source_count:
            continue
        selected.append(index)
        selected_sources.add(source)

    for index in ranked_indexes:
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)

    return selected


def cluster_topic_stories_by_similarity(
    topic: dict,
    articles: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    max_articles_per_story: int = MAX_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> list[dict]:
    if len(articles) < min_articles_per_story:
        return []

    vectors, norms = _build_story_tfidf_vectors(topic, articles)
    source_identities = [_article_source_identity(article) for article in articles]
    similarities: dict[tuple[int, int], float] = {}
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(articles))}
    max_similarity_by_index: dict[int, float] = {index: 0.0 for index in range(len(articles))}
    pair_debug: list[dict[str, Any]] = []
    for left_index in range(len(articles)):
        for right_index in range(left_index + 1, len(articles)):
            score = cosine_similarity(
                vectors[left_index],
                norms[left_index],
                vectors[right_index],
                norms[right_index],
            )
            if score > 0.0:
                similarities[(left_index, right_index)] = score
            max_similarity_by_index[left_index] = max(max_similarity_by_index[left_index], score)
            max_similarity_by_index[right_index] = max(max_similarity_by_index[right_index], score)
            distinct_source_pair = source_identities[left_index] != source_identities[right_index]
            linked = score >= similarity_threshold
            if linked:
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)
            if score > 0.0 or linked:
                pair_debug.append(
                    {
                        "left_article_id": articles[left_index].get("article_id"),
                        "right_article_id": articles[right_index].get("article_id"),
                        "similarity": round(score, 4),
                        "similarity_threshold": round(similarity_threshold, 4),
                        "linked": linked,
                        "distinct_source_pair": distinct_source_pair,
                    }
                )

    components: list[list[int]] = []
    visited: set[int] = set()
    eligible_indexes = {
        index
        for index, max_similarity in max_similarity_by_index.items()
        if max_similarity >= similarity_threshold
    }
    for start_index in range(len(articles)):
        if start_index not in eligible_indexes or start_index in visited:
            continue
        stack = [start_index]
        component: list[int] = []
        visited.add(start_index)
        while stack:
            index = stack.pop()
            component.append(index)
            for neighbor in adjacency[index]:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)
        if len(component) >= min_articles_per_story:
            components.extend(
                _split_story_component_by_weak_bridges(
                    sorted(component),
                    source_identities,
                    similarities,
                    similarity_threshold,
                    min_articles_per_story=min_articles_per_story,
                )
            )

    article_id_by_index = {
        index: str(article.get("article_id") or "")
        for index, article in enumerate(articles)
    }
    story_groups: list[dict] = []
    for component in components:
        if not _story_component_meets_connectedness_floor(
            component,
            source_identities,
            similarities,
            similarity_threshold,
            min_articles_per_story=min_articles_per_story,
        ):
            continue
        metrics = _story_component_connectedness_metrics(
            component,
            similarities,
            similarity_threshold,
        )
        source_count = _story_component_source_count(component, source_identities)
        medoid_index = _story_component_medoid_index(component, articles, similarities)
        selected_indexes = _select_story_article_indexes(
            component,
            medoid_index,
            articles,
            similarities,
            max_articles_per_story=max(max_articles_per_story, min_articles_per_story),
        )
        if len(selected_indexes) < min_articles_per_story:
            continue
        story_groups.append(
            {
                "title": _clean_story_title(
                    clean_topic_source_title(str(articles[medoid_index].get("title") or "")),
                    topic,
                    [articles[medoid_index]],
                ),
                "article_ids": [
                    article_id_by_index[index]
                    for index in selected_indexes
                    if article_id_by_index[index]
                ],
                "cluster_article_ids": [
                    article_id_by_index[index]
                    for index in component
                    if article_id_by_index[index]
                ],
                "article_count": len(component),
                "selected_article_count": len(selected_indexes),
                "average_similarity": round(float(metrics["average_similarity"]), 4),
                "connectedness_score": round(float(metrics["connectedness_score"]), 4),
                "story_strength_score": round(float(metrics["story_strength_score"]), 4),
                "edge_density": round(float(metrics["edge_density"]), 4),
                "edge_count": int(metrics["edge_count"]),
                "mean_best_similarity": round(float(metrics["mean_best_similarity"]), 4),
                "min_best_similarity": round(float(metrics["min_best_similarity"]), 4),
                "source_count": source_count,
                "relevance_score": _story_cluster_relevance_score(component, articles),
                "latest_rank": _story_cluster_recency_rank(component, articles),
                "_medoid_index": medoid_index,
            }
        )

    def story_rank(story: dict) -> tuple:
        return (
            -float(story.get("story_strength_score") or 0.0),
            -float(story.get("connectedness_score") or 0.0),
            -int(story.get("article_count") or 0),
            -int(story.get("source_count") or 0),
            -float(story.get("relevance_score") or 0.0),
            tuple(-value for value in story.get("latest_rank", (0, 0, 0))),
            int(story.get("_medoid_index") or 0),
        )

    ranked_groups = sorted(story_groups, key=story_rank)
    for story in ranked_groups:
        story.pop("_medoid_index", None)
        story.pop("latest_rank", None)
    ranked_groups_debug = sorted(
        pair_debug,
        key=lambda entry: (
            not entry.get("linked"),
            -float(entry.get("similarity") or 0.0),
            str(entry.get("left_article_id") or ""),
            str(entry.get("right_article_id") or ""),
        ),
    )
    for story in ranked_groups:
        story["_pair_debug"] = ranked_groups_debug[:STORY_PAIR_DEBUG_LIMIT]
    return ranked_groups


def _story_component_overlap_ratio(left: set[int], right: set[int]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _story_component_rank_tuple(
    component: list[int],
    articles: list[dict],
    source_identities: list[str],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> tuple:
    metrics = _story_component_connectedness_metrics(
        component,
        similarities,
        similarity_threshold,
    )
    source_count = _story_component_source_count(component, source_identities)
    relevance_score = _story_cluster_relevance_score(component, articles)
    recency_rank = _story_cluster_recency_rank(component, articles)
    return (
        -float(metrics["story_strength_score"]),
        -float(metrics["connectedness_score"]),
        -len(component),
        -source_count,
        -float(metrics["average_similarity"]),
        -float(relevance_score),
        tuple(-value for value in recency_rank),
        tuple(component),
    )


def cluster_global_stories_by_similarity(
    articles: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> list[dict]:
    if len(articles) < min_articles_per_story:
        return []

    neutral_topic = {
        "key": "global_story_pool",
        "title": "Global story pool",
        "rationale": "",
        "keywords": [],
        "boost_phrases": [],
    }
    vectors, norms = _build_story_tfidf_vectors(neutral_topic, articles)
    source_identities = [_article_source_identity(article) for article in articles]
    similarities: dict[tuple[int, int], float] = {}
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(articles))}
    max_similarity_by_index: dict[int, float] = {index: 0.0 for index in range(len(articles))}
    pair_debug: list[dict[str, Any]] = []

    for left_index in range(len(articles)):
        for right_index in range(left_index + 1, len(articles)):
            score = cosine_similarity(
                vectors[left_index],
                norms[left_index],
                vectors[right_index],
                norms[right_index],
            )
            if score > 0.0:
                similarities[(left_index, right_index)] = score
            max_similarity_by_index[left_index] = max(max_similarity_by_index[left_index], score)
            max_similarity_by_index[right_index] = max(max_similarity_by_index[right_index], score)
            linked = score >= similarity_threshold
            if linked:
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)
            if score > 0.0 or linked:
                pair_debug.append(
                    {
                        "left_article_id": articles[left_index].get("article_id"),
                        "left_title": articles[left_index].get("title"),
                        "left_source": articles[left_index].get("source"),
                        "right_article_id": articles[right_index].get("article_id"),
                        "right_title": articles[right_index].get("title"),
                        "right_source": articles[right_index].get("source"),
                        "similarity": round(score, 4),
                        "similarity_threshold": round(similarity_threshold, 4),
                        "linked": linked,
                        "distinct_source_pair": source_identities[left_index] != source_identities[right_index],
                    }
                )

    candidate_components: dict[tuple[int, ...], list[int]] = {}

    def add_candidate(component: list[int]) -> None:
        clean_component = sorted(set(component))
        if len(clean_component) < min_articles_per_story:
            return
        if not _story_component_meets_connectedness_floor(
            clean_component,
            source_identities,
            similarities,
            similarity_threshold,
            min_articles_per_story=min_articles_per_story,
        ):
            return
        signature = tuple(clean_component)
        candidate_components.setdefault(signature, clean_component)

    eligible_indexes = {
        index
        for index, max_similarity in max_similarity_by_index.items()
        if max_similarity >= similarity_threshold
    }

    visited: set[int] = set()
    for start_index in range(len(articles)):
        if start_index not in eligible_indexes or start_index in visited:
            continue
        stack = [start_index]
        component: list[int] = []
        visited.add(start_index)
        while stack:
            index = stack.pop()
            component.append(index)
            for neighbor in adjacency[index]:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)
        for split_component in _split_story_component_by_weak_bridges(
            sorted(component),
            source_identities,
            similarities,
            similarity_threshold,
            min_articles_per_story=min_articles_per_story,
        ):
            add_candidate(split_component)

    for seed_index in sorted(eligible_indexes):
        ego_component = sorted({seed_index, *adjacency.get(seed_index, set())})
        if len(ego_component) < min_articles_per_story:
            continue
        split_components = _split_story_component_by_weak_bridges(
            ego_component,
            source_identities,
            similarities,
            similarity_threshold,
            min_articles_per_story=min_articles_per_story,
        )
        for split_component in split_components:
            if seed_index in split_component:
                add_candidate(split_component)

    ranked_components = sorted(
        candidate_components.values(),
        key=lambda component: _story_component_rank_tuple(
            component,
            articles,
            source_identities,
            similarities,
            similarity_threshold,
        ),
    )

    retained_components: list[list[int]] = []
    retained_sets: list[set[int]] = []
    for component in ranked_components:
        component_set = set(component)
        if any(
            _story_component_overlap_ratio(component_set, existing)
            >= STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD
            for existing in retained_sets
        ):
            continue
        retained_components.append(component)
        retained_sets.append(component_set)

    article_id_by_index = {
        index: str(article.get("article_id") or "")
        for index, article in enumerate(articles)
    }
    ranked_pair_debug = sorted(
        pair_debug,
        key=lambda entry: (
            not entry.get("linked"),
            -float(entry.get("similarity") or 0.0),
            str(entry.get("left_article_id") or ""),
            str(entry.get("right_article_id") or ""),
        ),
    )

    story_groups: list[dict] = []
    for component in retained_components:
        metrics = _story_component_connectedness_metrics(
            component,
            similarities,
            similarity_threshold,
        )
        source_count = _story_component_source_count(component, source_identities)
        medoid_index = _story_component_medoid_index(component, articles, similarities)

        def article_rank(index: int) -> tuple:
            article = articles[index]
            return (
                0 if index == medoid_index else 1,
                -_story_index_average_similarity(index, component, similarities),
                tuple(-value for value in _article_time_rank(article)),
                str(article.get("source") or ""),
                index,
            )

        ordered_indexes = sorted(component, key=article_rank)
        story_groups.append(
            {
                "title": _clean_story_title(
                    clean_topic_source_title(str(articles[medoid_index].get("title") or "")),
                    neutral_topic,
                    [articles[medoid_index]],
                ),
                "article_ids": [
                    article_id_by_index[index]
                    for index in ordered_indexes
                    if article_id_by_index[index]
                ],
                "cluster_article_ids": [
                    article_id_by_index[index]
                    for index in component
                    if article_id_by_index[index]
                ],
                "article_count": len(component),
                "selected_article_count": len(component),
                "average_similarity": round(float(metrics["average_similarity"]), 4),
                "connectedness_score": round(float(metrics["connectedness_score"]), 4),
                "story_strength_score": round(float(metrics["story_strength_score"]), 4),
                "edge_density": round(float(metrics["edge_density"]), 4),
                "edge_count": int(metrics["edge_count"]),
                "mean_best_similarity": round(float(metrics["mean_best_similarity"]), 4),
                "min_best_similarity": round(float(metrics["min_best_similarity"]), 4),
                "source_count": source_count,
                "relevance_score": _story_cluster_relevance_score(component, articles),
                "latest_rank": _story_cluster_recency_rank(component, articles),
                "_pair_debug": ranked_pair_debug[:STORY_PAIR_DEBUG_LIMIT],
            }
        )

    def story_rank(story: dict) -> tuple:
        return (
            -float(story.get("story_strength_score") or 0.0),
            -float(story.get("connectedness_score") or 0.0),
            -int(story.get("article_count") or 0),
            -int(story.get("source_count") or 0),
            -float(story.get("average_similarity") or 0.0),
            tuple(-value for value in story.get("latest_rank", (0, 0, 0))),
            str(story.get("title") or ""),
        )

    ranked_groups = sorted(story_groups, key=story_rank)
    for story in ranked_groups:
        story.pop("latest_rank", None)
    return ranked_groups


def organize_article_targets_into_global_stories(
    article_targets: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    if min_articles_per_story <= 1:
        story_records = [
            {
                "story_key": f"global-story-{index:02d}",
                "story_title": article.get("title") or "News update",
                "article_count": 1,
                "selected_article_count": 1,
                "article_ids": [article.get("article_id")],
                "cluster_article_ids": [article.get("article_id")],
                "source_count": 1,
            }
            for index, article in enumerate(article_targets, start=1)
        ]
        return article_targets, story_records, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "story_count": len(story_records),
            "min_articles_per_story": min_articles_per_story,
            "similarity_threshold": similarity_threshold,
            "component_overlap_suppress_threshold": STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD,
            "clustering_method": "topicless_singletons",
        }

    story_groups = cluster_global_stories_by_similarity(
        article_targets,
        min_articles_per_story=min_articles_per_story,
        similarity_threshold=similarity_threshold,
    )
    article_lookup = {
        str(article.get("article_id") or ""): article
        for article in article_targets
        if article.get("article_id")
    }
    story_records: list[dict] = []
    memberships_by_article_id: dict[str, list[dict[str, Any]]] = {}
    pair_debug: list[dict[str, Any]] = []

    for story_index, story in enumerate(story_groups, start=1):
        story_title = _clean_story_title(str(story.get("title") or ""), {}, article_targets)
        story_key = f"global-story-{story_index:02d}-{_story_slug(story_title)}"
        article_ids = [
            str(article_id)
            for article_id in story.get("article_ids", [])
            if str(article_id) in article_lookup
        ]
        if len(article_ids) < min_articles_per_story:
            continue
        cluster_article_ids = [
            str(article_id)
            for article_id in story.get("cluster_article_ids", article_ids)
            if str(article_id) in article_lookup
        ]
        story_record = {
            "story_key": story_key,
            "story_title": story_title,
            "article_count": int(story.get("article_count") or len(cluster_article_ids)),
            "cluster_article_count": int(story.get("article_count") or len(cluster_article_ids)),
            "selected_article_count": len(article_ids),
            "article_ids": article_ids,
            "cluster_article_ids": cluster_article_ids,
            "average_similarity": story.get("average_similarity"),
            "connectedness_score": story.get("connectedness_score"),
            "story_strength_score": story.get("story_strength_score"),
            "edge_density": story.get("edge_density"),
            "mean_best_similarity": story.get("mean_best_similarity"),
            "min_best_similarity": story.get("min_best_similarity"),
            "source_count": story.get("source_count"),
        }
        story_records.append(story_record)
        if story.get("_pair_debug") and not pair_debug:
            pair_debug = story.get("_pair_debug", [])[:STORY_PAIR_DEBUG_LIMIT]
        for article_id in cluster_article_ids:
            memberships_by_article_id.setdefault(article_id, []).append(
                {
                    "story_key": story_key,
                    "story_title": story_title,
                    "story_strength_score": story.get("story_strength_score"),
                    "average_similarity": story.get("average_similarity"),
                }
            )

    selected_targets: list[dict] = []
    dropped_articles: list[dict[str, Any]] = []
    for article in article_targets:
        article_id = str(article.get("article_id") or "")
        memberships = memberships_by_article_id.get(article_id, [])
        if memberships:
            selected_targets.append(
                {
                    **article,
                    "story_memberships": memberships,
                }
            )
        else:
            dropped_articles.append(
                {
                    "article_id": article.get("article_id"),
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "reason": "no_supported_story_cluster",
                }
            )

    story_debug_records: list[dict[str, Any]] = []
    for story in story_records:
        article_details = []
        for article_id in story.get("cluster_article_ids", []):
            article = article_lookup.get(str(article_id))
            if not article:
                continue
            article_details.append(
                {
                    "article_id": article_id,
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "url": article.get("url"),
                    "pub_date": article.get("pub_date"),
                }
            )
        story_debug_records.append({**story, "articles": article_details})

    stats = {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected_targets),
        "dropped_count": len(article_targets) - len(selected_targets),
        "min_articles_per_story": min_articles_per_story,
        "similarity_threshold": similarity_threshold,
        "component_overlap_suppress_threshold": STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD,
        "clustering_method": "topicless_overlapping_cleaned_body_weighted_tfidf_similarity_graph",
        "story_count": len(story_records),
        "stories": story_debug_records,
        "article_story_memberships": {
            article_id: memberships
            for article_id, memberships in sorted(memberships_by_article_id.items())
        },
        "dropped_articles": dropped_articles,
        "pair_debug": pair_debug,
    }
    return selected_targets, story_records, stats


def organize_article_targets_into_stories(
    article_targets: list[dict],
    topics: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    max_stories_per_topic: int = 4,
    max_articles_per_story: int = MAX_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> tuple[list[dict], dict[str, Any]]:
    if min_articles_per_story <= 1:
        return article_targets, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "min_articles_per_story": min_articles_per_story,
            "max_stories_per_topic": max_stories_per_topic,
            "max_articles_per_story": max_articles_per_story,
            "similarity_threshold": similarity_threshold,
        }

    topic_by_key = {str(topic.get("key") or ""): topic for topic in topics}
    topic_order = [str(topic.get("key") or "") for topic in topics]
    articles_by_topic: dict[str, list[dict]] = {key: [] for key in topic_order}
    for article in article_targets:
        topic_key = str(article.get("topic_key") or "")
        articles_by_topic.setdefault(topic_key, []).append(article)

    annotated_by_original_id: dict[int, dict] = {}
    stories_by_topic: dict[str, list[dict]] = {}
    pair_debug_by_topic: dict[str, list[dict[str, Any]]] = {}
    dropped_articles_by_topic: dict[str, list[dict[str, Any]]] = {}
    dropped_by_topic: Counter[str] = Counter()

    for topic_key in topic_order + sorted(key for key in articles_by_topic if key not in topic_order):
        articles = articles_by_topic.get(topic_key) or []
        if not articles:
            continue
        topic = topic_by_key.get(
            topic_key,
            {
                "key": topic_key,
                "title": articles[0].get("topic_title") or topic_key or "Unknown topic",
                "keywords": [],
                "boost_phrases": [],
            },
        )
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        all_story_groups = cluster_topic_stories_by_similarity(
            topic,
            articles,
            min_articles_per_story=min_articles_per_story,
            max_articles_per_story=max_articles_per_story,
            similarity_threshold=similarity_threshold,
        )
        story_groups = all_story_groups[:max_stories_per_topic]
        if all_story_groups:
            pair_debug_by_topic[topic_title] = (
                all_story_groups[0].get("_pair_debug", [])[:STORY_PAIR_DEBUG_LIMIT]
            )
        if not story_groups:
            dropped_by_topic[topic_title] += len(articles)
            dropped_articles_by_topic[topic_title] = [
                {
                    "article_id": article.get("article_id"),
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "relevance_score": article.get("relevance_score"),
                    "reason": "no_supported_story_cluster",
                }
                for article in articles
            ]
            continue

        article_lookup = {str(article.get("article_id") or ""): article for article in articles}
        topic_story_records: list[dict] = []
        for story_index, story in enumerate(story_groups, start=1):
            story_title = _clean_story_title(str(story.get("title") or ""), topic, articles)
            article_ids = [
                str(article_id)
                for article_id in story.get("article_ids", [])
                if str(article_id) in article_lookup
            ]
            if len(article_ids) < min_articles_per_story:
                continue
            story_key = (
                f"{_story_slug(topic_key or topic_title, 'topic')}"
                f"-story-{story_index:02d}-{_story_slug(story_title)}"
            )
            topic_story_records.append(
                {
                    "story_key": story_key,
                    "story_title": story_title,
                    "article_count": len(article_ids),
                    "cluster_article_count": int(story.get("article_count") or len(article_ids)),
                    "selected_article_count": len(article_ids),
                    "article_ids": article_ids,
                    "cluster_article_ids": story.get("cluster_article_ids", article_ids),
                    "average_similarity": story.get("average_similarity"),
                    "connectedness_score": story.get("connectedness_score"),
                    "story_strength_score": story.get("story_strength_score"),
                    "edge_density": story.get("edge_density"),
                    "mean_best_similarity": story.get("mean_best_similarity"),
                    "min_best_similarity": story.get("min_best_similarity"),
                    "source_count": story.get("source_count"),
                }
            )
            for article_id in article_ids:
                original_article = article_lookup[article_id]
                annotated_by_original_id[id(original_article)] = {
                    **original_article,
                    "story_key": story_key,
                    "story_title": story_title,
                    "story_article_count": int(story.get("article_count") or len(article_ids)),
                    "story_selected_article_count": len(article_ids),
                    "story_average_similarity": story.get("average_similarity"),
                    "story_connectedness_score": story.get("connectedness_score"),
                    "story_strength_score": story.get("story_strength_score"),
                    "story_edge_density": story.get("edge_density"),
                    "story_source_count": story.get("source_count"),
                }
        included_ids = {
            id(article_lookup[article_id])
            for record in topic_story_records
            for article_id in record["article_ids"]
            if article_id in article_lookup
        }
        dropped_by_topic[topic_title] += len([article for article in articles if id(article) not in included_ids])
        dropped_articles = [article for article in articles if id(article) not in included_ids]
        if dropped_articles:
            dropped_articles_by_topic[topic_title] = [
                {
                    "article_id": article.get("article_id"),
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "relevance_score": article.get("relevance_score"),
                    "reason": "outside_retained_story_or_article_cap",
                }
                for article in dropped_articles
            ]
        if topic_story_records:
            stories_by_topic[topic_title] = topic_story_records

    selected_targets = [
        annotated_by_original_id[id(article)]
        for article in article_targets
        if id(article) in annotated_by_original_id
    ]
    stats = {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected_targets),
        "dropped_count": len(article_targets) - len(selected_targets),
        "min_articles_per_story": min_articles_per_story,
        "max_stories_per_topic": max_stories_per_topic,
        "max_articles_per_story": max_articles_per_story,
        "similarity_threshold": similarity_threshold,
        "clustering_method": "cleaned_body_weighted_tfidf_similarity_graph",
        "story_count": sum(len(stories) for stories in stories_by_topic.values()),
        "stories_by_topic": stories_by_topic,
        "dropped_by_topic": dict(dropped_by_topic),
        "dropped_articles_by_topic": dropped_articles_by_topic,
        "pair_debug_by_topic": pair_debug_by_topic,
    }
    return selected_targets, stats


def filter_budgeted_targets_by_story_floor(
    article_targets: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
) -> tuple[list[dict], dict[str, Any]]:
    if min_articles_per_story <= 1:
        return article_targets, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "min_articles_per_story": min_articles_per_story,
        }

    grouped: dict[str, list[dict]] = {}
    for article in article_targets:
        story_key = str(article.get("story_key") or "").strip()
        if not story_key:
            continue
        grouped.setdefault(story_key, []).append(article)

    eligible_story_keys = {
        story_key
        for story_key, story_articles in grouped.items()
        if len(story_articles) >= min_articles_per_story
    }
    selected = [
        article
        for article in article_targets
        if str(article.get("story_key") or "").strip() in eligible_story_keys
    ]
    dropped = [
        article
        for article in article_targets
        if str(article.get("story_key") or "").strip() not in eligible_story_keys
    ]
    return selected, {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected),
        "dropped_count": len(dropped),
        "min_articles_per_story": min_articles_per_story,
        "eligible_story_count": len(eligible_story_keys),
        "dropped_article_ids": [article.get("article_id") for article in dropped],
    }
