"""Shared topic vocabulary and relevance scoring helpers."""

from __future__ import annotations

import re
from typing import Any


TOPIC_MATCH_STOPWORDS = {
    "a",
    "about",
    "after",
    "against",
    "amid",
    "among",
    "an",
    "and",
    "are",
    "around",
    "as",
    "at",
    "auto-generated",
    "auto",
    "be",
    "for",
    "fallback",
    "from",
    "generated",
    "has",
    "have",
    "headline",
    "her",
    "him",
    "his",
    "into",
    "in",
    "is",
    "its",
    "it",
    "being",
    "just",
    "may",
    "me",
    "my",
    "new",
    "no",
    "not",
    "now",
    "of",
    "off",
    "one",
    "on",
    "or",
    "over",
    "provider",
    "providers",
    "reported",
    "report",
    "reports",
    "says",
    "said",
    "say",
    "seed",
    "some",
    "source",
    "sources",
    "support",
    "that",
    "the",
    "them",
    "their",
    "they",
    "this",
    "through",
    "tells",
    "tell",
    "told",
    "to",
    "topic",
    "under",
    "up",
    "was",
    "we",
    "were",
    "who",
    "with",
    "abc",
    "apnews",
    "associated",
    "bbc",
    "breaking",
    "cnn",
    "com",
    "exclusive",
    "investing",
    "latest",
    "live",
    "news",
    "npr",
    "photos",
    "press",
    "reuters",
    "update",
    "updates",
    "video",
    "watch",
}

SHORT_TOPIC_MATCH_STOPWORDS = TOPIC_MATCH_STOPWORDS | {
    "am",
    "do",
    "go",
    "he",
    "if",
    "so",
}

WEAK_TOPIC_MATCH_TERMS = {
    "attack",
    "bond",
    "business",
    "cash",
    "commodity",
    "crude",
    "currency",
    "drone",
    "energy",
    "gold",
    "market",
    "military",
    "oil",
    "price",
    "rate",
    "report",
    "stock",
    "strike",
    "trade",
    "war",
}

BOILERPLATE_CONTENT_STOPWORDS = {
    "about",
    "amp",
    "aria",
    "april",
    "august",
    "blank",
    "body",
    "class",
    "click",
    "com",
    "content",
    "css",
    "data",
    "december",
    "div",
    "february",
    "font",
    "friday",
    "google",
    "height",
    "href",
    "html",
    "http",
    "https",
    "img",
    "january",
    "july",
    "june",
    "link",
    "march",
    "monday",
    "nbsp",
    "noopener",
    "noreferrer",
    "november",
    "october",
    "px",
    "rel",
    "rss",
    "saturday",
    "script",
    "september",
    "share",
    "span",
    "src",
    "style",
    "sunday",
    "target",
    "text",
    "thursday",
    "tuesday",
    "utm",
    "wednesday",
    "width",
    "www",
}


def normalize_topic_key(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return slug or "topic"


def clean_topic_source_title(title: str) -> str:
    """Remove common aggregator source suffixes without rewriting the headline."""
    clean_title = re.sub(r"\s+", " ", (title or "").strip())
    if " - " in clean_title:
        clean_title = clean_title.rsplit(" - ", 1)[0].strip()
    return clean_title


def compact_dotted_acronyms(text: str) -> str:
    return re.sub(
        r"\b(?:[A-Za-z]\.){2,}",
        lambda match: match.group(0).replace(".", ""),
        text,
    )


def normalize_topic_token(token: str) -> str:
    normalized = token.lower().replace("&", "").strip("'")
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


def ordered_topic_match_terms(
    *values: Any,
    allowed_short_terms: set[str] | None = None,
    collect_short_terms: bool = False,
) -> list[str]:
    text = " ".join(str(value or "") for value in values)
    text = compact_dotted_acronyms(text)
    allowed_short_terms = allowed_short_terms or set()
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9](?:[a-z0-9'&-]*[a-z0-9])?", text.lower()):
        token_variants = [token]
        if "-" in token:
            token_variants.extend(part for part in token.split("-") if part)
        for variant in token_variants:
            normalized = normalize_topic_token(variant)
            is_short = len(normalized) < 3
            if is_short and normalized in SHORT_TOPIC_MATCH_STOPWORDS:
                continue
            if (
                (is_short and not collect_short_terms and normalized not in allowed_short_terms)
                or normalized.isdigit()
                or normalized in TOPIC_MATCH_STOPWORDS
            ):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
    return terms


def topic_match_terms(
    *values: Any,
    allowed_short_terms: set[str] | None = None,
    collect_short_terms: bool = False,
) -> set[str]:
    return set(
        ordered_topic_match_terms(
            *values,
            allowed_short_terms=allowed_short_terms,
            collect_short_terms=collect_short_terms,
        )
    )


def topic_vocabulary_values(topic: dict) -> tuple[Any, ...]:
    return (
        topic.get("title"),
        topic.get("rationale"),
        " ".join(str(k) for k in topic.get("keywords", [])),
        " ".join(str(p) for p in topic.get("boost_phrases", [])),
    )


def topic_allowed_short_match_terms(topic: dict) -> set[str]:
    return {
        term
        for term in topic_match_terms(*topic_vocabulary_values(topic), collect_short_terms=True)
        if len(term) < 3
    }


def is_fallback_topic(topic: dict) -> bool:
    source = str(topic.get("topic_source") or "").lower()
    rationale = str(topic.get("rationale") or "").lower()
    return source.startswith("fallback") or "fallback topic" in rationale


def topic_phrase_required_overlap(term_count: int) -> int:
    if term_count <= 2:
        return term_count
    return max(2, (term_count * 3 + 3) // 4)


def normalize_phrase_match_text(value: str) -> str:
    compact = compact_dotted_acronyms(str(value or "")).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", compact)).strip()


def text_has_normalized_phrase(text: str, phrase: str) -> bool:
    normalized_text = normalize_phrase_match_text(text)
    normalized_phrase = normalize_phrase_match_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    return f" {normalized_phrase} " in f" {normalized_text} "


def topic_has_required_context(topic: dict, text: str) -> bool:
    required_terms = [
        str(term or "").strip()
        for term in (topic.get("required_context_terms") or [])
        if str(term or "").strip()
    ]
    if not required_terms:
        return True
    return any(text_has_normalized_phrase(text, term) for term in required_terms)


def score_topic_text_match(topic: dict, text: str) -> int:
    allowed_short_terms = topic_allowed_short_match_terms(topic)
    text_terms = topic_match_terms(text, allowed_short_terms=allowed_short_terms)
    if not text_terms:
        return 0

    matched_terms: set[str] = set()
    phrase_score = 0
    boost_score = 0

    for keyword in topic.get("keywords", []) or []:
        keyword_terms = topic_match_terms(keyword, allowed_short_terms=allowed_short_terms)
        if not keyword_terms:
            continue
        overlap = keyword_terms & text_terms
        if len(keyword_terms) == 1:
            if overlap:
                matched_terms.update(overlap)
            continue
        if len(overlap) >= topic_phrase_required_overlap(len(keyword_terms)):
            matched_terms.update(overlap)
            phrase_score += 2 + len(overlap)

    for phrase in topic.get("boost_phrases", []) or []:
        phrase_terms = topic_match_terms(phrase, allowed_short_terms=allowed_short_terms)
        if len(phrase_terms) < 2:
            continue
        overlap = phrase_terms & text_terms
        if len(overlap) >= topic_phrase_required_overlap(len(phrase_terms)):
            matched_terms.update(overlap)
            boost_score += 4

    strict_score = (len(matched_terms) * 2) + phrase_score + boost_score
    lenient_score = lenient_topic_overlap_score(topic, text)
    score = max(strict_score, lenient_score)
    if score <= 0:
        return 0

    topic_terms = topic_match_terms(
        *topic_vocabulary_values(topic),
        allowed_short_terms=allowed_short_terms,
    )
    overlap_terms = topic_terms & text_terms
    strong_overlap = overlap_terms - WEAK_TOPIC_MATCH_TERMS
    if not strong_overlap and phrase_score <= 0 and boost_score <= 0:
        return 0
    if not topic_has_required_context(topic, text):
        return 0
    return score


def lenient_topic_overlap_score(topic: dict, text: str) -> int:
    allowed_short_terms = topic_allowed_short_match_terms(topic)
    topic_terms = topic_match_terms(
        *topic_vocabulary_values(topic),
        allowed_short_terms=allowed_short_terms,
    )
    text_terms = topic_match_terms(text, allowed_short_terms=allowed_short_terms)
    overlap = topic_terms & text_terms
    required_overlap = 4 if is_fallback_topic(topic) else 3
    if len(overlap) < required_overlap:
        return 0
    return len(overlap)
