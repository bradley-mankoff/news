"""Shared text vocabulary helpers for story matching."""

from __future__ import annotations

import re
from typing import Any


TEXT_MATCH_STOPWORDS = {
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

SHORT_TEXT_MATCH_STOPWORDS = TEXT_MATCH_STOPWORDS | {
    "am",
    "do",
    "go",
    "he",
    "if",
    "so",
}

WEAK_MATCH_TERMS = {
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

def clean_source_title(title: str) -> str:
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


def normalize_match_token(token: str) -> str:
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


def ordered_match_terms(
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
            normalized = normalize_match_token(variant)
            is_short = len(normalized) < 3
            if is_short and normalized in SHORT_TEXT_MATCH_STOPWORDS:
                continue
            if (
                (is_short and not collect_short_terms and normalized not in allowed_short_terms)
                or normalized.isdigit()
                or normalized in TEXT_MATCH_STOPWORDS
            ):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
    return terms


def match_terms(
    *values: Any,
    allowed_short_terms: set[str] | None = None,
    collect_short_terms: bool = False,
) -> set[str]:
    return set(
        ordered_match_terms(
            *values,
            allowed_short_terms=allowed_short_terms,
            collect_short_terms=collect_short_terms,
        )
    )
