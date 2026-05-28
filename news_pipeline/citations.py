"""Citation helpers for story-level source attribution."""

from __future__ import annotations

import html
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urldefrag
from zoneinfo import ZoneInfo


TEMPORARY_CITATION_RE = re.compile(r"\[\[([A-Za-z0-9_,;\s-]+)\]\]")
DISPLAY_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
DEFAULT_STORY_LEVEL_CITATION_SENTENCE_THRESHOLD = 2

_COMMON_ABBREVIATION_RE = re.compile(
    r"\b(?:U\.S|U\.K|E\.U|U\.N|Mr|Mrs|Ms|Dr|Prof|Sen|Rep|Gov|St|No|Inc|Ltd|Co|Corp|vs)\.$",
    flags=re.IGNORECASE,
)
_INITIALISM_RE = re.compile(r"(?:\b[A-Z]\.){2,}$")
_TOKEN_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "because",
    "before",
    "but",
    "can",
    "could",
    "for",
    "from",
    "had",
    "has",
    "have",
    "into",
    "its",
    "more",
    "new",
    "not",
    "official",
    "officials",
    "over",
    "said",
    "says",
    "that",
    "the",
    "their",
    "this",
    "was",
    "were",
    "while",
    "will",
    "with",
}
EASTERN_TIME = ZoneInfo("America/New_York")


def parse_article_report_entry(entry: str) -> dict[str, Any]:
    """Extract stable citation metadata from a normalized article summary."""

    clean_entry = str(entry or "")
    title_match = re.search(r"^###\s+(.+)$", clean_entry, flags=re.MULTILINE)
    metadata: dict[str, str] = {}
    for label, value in re.findall(r"^-\s*([^:]+):\s*(.+)$", clean_entry, flags=re.MULTILINE):
        metadata[label.strip().lower()] = value.strip()

    summary = ""
    summary_match = re.search(r"Summary:\s*(.*)", clean_entry, flags=re.DOTALL)
    if summary_match:
        summary = re.split(r"\n(?:---+|###\s+)", summary_match.group(1), maxsplit=1)[0]
        summary = re.sub(r"\s+", " ", summary).strip()

    url = metadata.get("url", "")
    if url == "N/A":
        url = ""

    return {
        "title": title_match.group(1).strip() if title_match else "",
        "source": metadata.get("source", ""),
        "published": metadata.get("published", ""),
        "url": url,
        "article_id": metadata.get("article id", ""),
        "topic": metadata.get("topic", ""),
        "story": metadata.get("story", ""),
        "summary": summary,
        "raw_entry": clean_entry,
    }


def strip_citation_markers(text: str) -> str:
    """Remove temporary and final numeric citation markers while preserving prose."""

    clean_text = TEMPORARY_CITATION_RE.sub("", str(text or ""))
    clean_text = DISPLAY_CITATION_RE.sub("", clean_text)
    clean_text = re.sub(r"[ \t]+([,.;:!?])", r"\1", clean_text)
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    return clean_text.strip()


def _normalize_source_id(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _marker_source_ids(text: str) -> list[str]:
    source_ids: list[str] = []
    for marker_text in TEMPORARY_CITATION_RE.findall(text or ""):
        for raw_id in re.split(r"[,;\s]+", marker_text):
            source_id = _normalize_source_id(raw_id)
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
    return source_ids


def _remove_temporary_markers(text: str) -> str:
    clean_text = TEMPORARY_CITATION_RE.sub("", str(text or ""))
    clean_text = re.sub(r"[ \t]+([,.;:!?])", r"\1", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    return clean_text.strip()


def _looks_like_abbreviation(prefix: str) -> bool:
    tail = prefix[-32:]
    return bool(_COMMON_ABBREVIATION_RE.search(tail) or _INITIALISM_RE.search(tail))


def _consume_temporary_markers_after(text: str, index: int) -> int:
    position = index
    while True:
        marker_position = position
        while marker_position < len(text) and text[marker_position].isspace():
            marker_position += 1
        marker_match = TEMPORARY_CITATION_RE.match(text, marker_position)
        if not marker_match:
            return position
        position = marker_match.end()


def split_cited_sentences(text: str) -> list[str]:
    """Split prose into sentence-sized chunks while keeping trailing [[S1]] markers."""

    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean_text:
        return []

    segments: list[str] = []
    start = 0
    index = 0
    while index < len(clean_text):
        if clean_text[index] not in ".!?":
            index += 1
            continue
        citation_end = _consume_temporary_markers_after(clean_text, index + 1)
        has_trailing_citation = citation_end > index + 1
        has_sentence_boundary_spacing = citation_end >= len(clean_text) or clean_text[citation_end].isspace()
        next_position = citation_end
        while next_position < len(clean_text) and clean_text[next_position].isspace():
            next_position += 1
        if (
            (has_trailing_citation or has_sentence_boundary_spacing)
            and (
                next_position >= len(clean_text)
                or not _looks_like_abbreviation(clean_text[start : index + 1])
            )
        ):
            segment = clean_text[start:citation_end].strip()
            if segment:
                segments.append(segment)
            start = next_position
            index = start
            continue
        index += 1

    final_segment = clean_text[start:].strip()
    if final_segment:
        segments.append(final_segment)
    return segments


def _tokenize_for_matching(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", str(text or "").lower()):
        token = token.strip("'")
        if token and token not in _TOKEN_STOPWORDS and not token.isdigit():
            tokens.add(token)
    return tokens


def _fallback_source_ids(sentence: str, citation_sources: list[dict[str, Any]]) -> list[str]:
    if not citation_sources:
        return []

    sentence_tokens = _tokenize_for_matching(sentence)
    ranked: list[tuple[float, int, str]] = []
    for index, source in enumerate(citation_sources):
        local_id = _normalize_source_id(str(source.get("local_id") or ""))
        if not local_id:
            continue
        source_text = " ".join(
            str(source.get(key) or "")
            for key in ("title", "source", "published", "summary")
        )
        source_tokens = _tokenize_for_matching(source_text)
        if sentence_tokens and source_tokens:
            overlap = len(sentence_tokens & source_tokens)
            score = overlap / math.sqrt(len(sentence_tokens) * len(source_tokens))
        else:
            score = 0.0
        ranked.append((score, -index, local_id))

    if not ranked:
        return []
    ranked.sort(reverse=True)
    return [ranked[0][2]]


def validate_cited_story_text(
    marked_text: str,
    citation_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return clean story text plus validated sentence-to-source metadata."""

    valid_source_ids = {
        _normalize_source_id(str(source.get("local_id") or ""))
        for source in citation_sources
        if source.get("local_id")
    }
    cited_sentences: list[dict[str, Any]] = []
    unknown_source_ids: list[str] = []
    repaired_sentence_count = 0

    for segment in split_cited_sentences(marked_text):
        sentence_text = _remove_temporary_markers(segment)
        if not sentence_text:
            continue
        raw_source_ids = _marker_source_ids(segment)
        source_ids: list[str] = []
        sentence_unknown_ids: list[str] = []
        for source_id in raw_source_ids:
            if source_id in valid_source_ids:
                if source_id not in source_ids:
                    source_ids.append(source_id)
            else:
                sentence_unknown_ids.append(source_id)
                unknown_source_ids.append(source_id)
        cited_sentences.append(
            {
                "text": sentence_text,
                "source_ids": source_ids,
                "raw_source_ids": raw_source_ids,
                "repaired": bool(sentence_unknown_ids and not source_ids),
            }
        )

    clean_paragraph = " ".join(sentence["text"] for sentence in cited_sentences).strip()
    return {
        "paragraph": clean_paragraph,
        "cited_sentences": cited_sentences,
        "diagnostics": {
            "sentence_count": len(cited_sentences),
            "temporary_marker_count": len(TEMPORARY_CITATION_RE.findall(marked_text or "")),
            "malformed_marker_count": (marked_text or "").count("[[") - len(TEMPORARY_CITATION_RE.findall(marked_text or "")),
            "unknown_source_ids": sorted(set(unknown_source_ids)),
            "uncited_sentence_count": sum(1 for s in cited_sentences if not s["source_ids"]),
            "source_count": len(citation_sources),
        },
    }


def _canonical_source_key(source: dict[str, Any]) -> tuple[str, str]:
    url = str(source.get("url") or "").strip()
    if url:
        defragged_url, _fragment = urldefrag(url)
        return ("url", defragged_url.rstrip("/").lower())
    article_id = str(source.get("article_id") or "").strip()
    if article_id:
        return ("article_id", article_id.lower())
    return (
        "metadata",
        "|".join(
            str(source.get(key) or "").strip().lower()
            for key in ("source", "title", "published")
        ),
    )


def _parse_published_datetime(raw_value: Any) -> datetime | None:
    value = str(raw_value or "").strip()
    if not value:
        return None

    candidates = [value]
    if value.endswith("Z"):
        candidates.append(value[:-1] + "+00:00")

    for candidate in candidates:
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
        if parsed is not None:
            return parsed

        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed

    return None


def format_source_published_timestamp(raw_value: Any) -> str:
    """Render source timestamps as Eastern time for report footers."""

    value = str(raw_value or "").strip()
    parsed = _parse_published_datetime(value)
    if parsed is None:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    eastern_time = parsed.astimezone(EASTERN_TIME)
    return eastern_time.strftime("%m/%d/%y, %I:%M %p %Z")


class CitationRegistry:
    """Assign global citation numbers in first-use order."""

    def __init__(self) -> None:
        self._key_to_number: dict[tuple[str, str], int] = {}
        self._sources: list[dict[str, Any]] = []

    def register(self, source: dict[str, Any]) -> int:
        key = _canonical_source_key(source)
        existing_number = self._key_to_number.get(key)
        if existing_number is not None:
            return existing_number
        number = len(self._sources) + 1
        self._key_to_number[key] = number
        self._sources.append(
            {
                "number": number,
                "title": str(source.get("title") or "Untitled article").strip(),
                "source": str(source.get("source") or "Unknown source").strip(),
                "published": str(source.get("published") or "").strip(),
                "url": str(source.get("url") or "").strip(),
                "article_id": str(source.get("article_id") or "").strip(),
            }
        )
        return number

    def sources(self) -> list[dict[str, Any]]:
        return [dict(source) for source in self._sources]


def render_cited_story_text(
    cited_sentences: list[dict[str, Any]],
    citation_sources: list[dict[str, Any]],
    registry: CitationRegistry,
) -> str:
    return str(
        render_cited_story(
            cited_sentences,
            citation_sources,
            registry,
            story_level_citation_sentence_threshold=None,
        ).get("paragraph")
        or ""
    )


def _normalized_sentence_source_ids(sentence: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    for source_id in sentence.get("source_ids") or []:
        normalized_source_id = _normalize_source_id(str(source_id))
        if normalized_source_id and normalized_source_id not in source_ids:
            source_ids.append(normalized_source_id)
    return source_ids


def render_cited_story(
    cited_sentences: list[dict[str, Any]],
    citation_sources: list[dict[str, Any]],
    registry: CitationRegistry,
    *,
    story_level_citation_sentence_threshold: int | None = DEFAULT_STORY_LEVEL_CITATION_SENTENCE_THRESHOLD,
) -> dict[str, Any]:
    source_by_local_id = {
        _normalize_source_id(str(source.get("local_id") or "")): source
        for source in citation_sources
        if source.get("local_id")
    }
    source_sentence_counts: dict[str, int] = {}
    source_first_seen_order: list[str] = []
    for sentence in cited_sentences:
        for source_id in _normalized_sentence_source_ids(sentence):
            if source_id not in source_by_local_id:
                continue
            source_sentence_counts[source_id] = source_sentence_counts.get(source_id, 0) + 1
            if source_id not in source_first_seen_order:
                source_first_seen_order.append(source_id)

    if story_level_citation_sentence_threshold is None:
        story_level_source_ids = []
    else:
        story_level_source_ids = [
            source_id
            for source_id in source_first_seen_order
            if source_sentence_counts.get(source_id, 0) > story_level_citation_sentence_threshold
        ]
    story_level_source_id_set = set(story_level_source_ids)
    story_level_numbers: list[int] = []
    for source_id in story_level_source_ids:
        source = source_by_local_id.get(source_id)
        if not source:
            continue
        number = registry.register(source)
        if number not in story_level_numbers:
            story_level_numbers.append(number)

    rendered_sentences: list[str] = []
    for sentence in cited_sentences:
        sentence_text = strip_citation_markers(str(sentence.get("text") or ""))
        numbers: list[int] = []
        for source_id in _normalized_sentence_source_ids(sentence):
            if source_id in story_level_source_id_set:
                continue
            source = source_by_local_id.get(source_id)
            if not source:
                continue
            number = registry.register(source)
            if number not in numbers:
                numbers.append(number)
        if numbers:
            sentence_text = f"{sentence_text}{''.join(f'[{number}]' for number in numbers)}"
        if sentence_text:
            rendered_sentences.append(sentence_text)
    return {
        "paragraph": " ".join(rendered_sentences).strip(),
        "headline_citation_text": "".join(f"[{number}]" for number in story_level_numbers),
        "story_level_source_numbers": story_level_numbers,
        "story_level_source_ids": story_level_source_ids,
        "story_level_source_sentence_counts": {
            source_id: source_sentence_counts[source_id]
            for source_id in story_level_source_ids
            if source_id in source_sentence_counts
        },
    }


def render_plain_text_sources(citation_sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for source in citation_sources:
        number = int(source.get("number") or 0)
        if number <= 0:
            continue
        title = str(source.get("title") or "Untitled article").strip()
        details = [
            str(source.get("source") or "").strip(),
            format_source_published_timestamp(source.get("published")),
        ]
        detail_text = ", ".join(detail for detail in details if detail)
        line = f"[{number}] {title}"
        if detail_text:
            line = f"{line} - {detail_text}"
        url = str(source.get("url") or "").strip()
        if url:
            line = f"{line}\n    {url}"
        lines.append(line)
    return "\n\n".join(lines)


def _valid_citation_numbers(citation_sources: list[dict[str, Any]]) -> set[int]:
    numbers: set[int] = set()
    for source in citation_sources:
        try:
            number = int(source.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.add(number)
    return numbers


def _citation_source_url_by_number(citation_sources: list[dict[str, Any]]) -> dict[int, str]:
    urls: dict[int, str] = {}
    for source in citation_sources:
        try:
            number = int(source.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        url = str(source.get("url") or "").strip()
        if url:
            urls[number] = url
    return urls


def render_html_text_with_citations(text: str, citation_sources: list[dict[str, Any]]) -> str:
    valid_numbers = _valid_citation_numbers(citation_sources)
    if not valid_numbers:
        return html.escape(str(text or "")).replace("\n", "<br>")
    source_url_by_number = _citation_source_url_by_number(citation_sources)

    rendered_parts: list[str] = []
    position = 0
    for match in DISPLAY_CITATION_RE.finditer(str(text or "")):
        rendered_parts.append(html.escape(str(text or "")[position : match.start()]))
        numbers = [
            int(number)
            for number in re.findall(r"\d+", match.group(1))
            if int(number) in valid_numbers
        ]
        if numbers:
            for number in numbers:
                href = source_url_by_number.get(number) or f"#source-{number}"
                rendered_parts.append(
                    "<sup style=\"font-size:11px; line-height:0; vertical-align:super;\">"
                    "["
                    f"<a href=\"{html.escape(href, quote=True)}\" "
                    f"style=\"color:#2563eb; text-decoration:none;\">{number}</a>"
                    "]</sup>"
                )
        else:
            rendered_parts.append(html.escape(match.group(0)))
        position = match.end()
    rendered_parts.append(html.escape(str(text or "")[position:]))
    return "".join(rendered_parts).replace("\n", "<br>")


def render_html_sources(citation_sources: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for source in citation_sources:
        number = int(source.get("number") or 0)
        if number <= 0:
            continue
        title = html.escape(str(source.get("title") or "Untitled article").strip())
        url = str(source.get("url") or "").strip()
        if url:
            title_html = (
                f"<a href=\"{html.escape(url, quote=True)}\" "
                f"style=\"color:#2563eb; text-decoration:none;\">{title}</a>"
            )
        else:
            title_html = title
        details = [
            str(source.get("source") or "").strip(),
            format_source_published_timestamp(source.get("published")),
        ]
        detail_text = html.escape(", ".join(detail for detail in details if detail))
        detail_html = (
            f"<div style=\"margin:1px 0 0; font-size:12px; line-height:1.3; color:#6b7280;\">{detail_text}</div>"
            if detail_text
            else ""
        )
        items.append(
            f"<li id=\"source-{number}\" style=\"margin:0 0 6px; padding-left:2px; font-size:13px; line-height:1.35; color:#374151;\">"
            f"{title_html}{detail_html}</li>"
        )
    if not items:
        return "<p style=\"margin:0; font-size:13px; line-height:1.3; color:#6b7280;\">No citation sources available.</p>"
    return "<ol style=\"margin:0; padding-left:20px;\">" + "".join(items) + "</ol>"
