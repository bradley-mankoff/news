"""Article summary record normalization and compatibility rendering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any


LOW_CONFIDENCE_SUMMARY_PATTERNS = (
    "insufficient to create a substantive summary",
    "contains only the headline",
    "only contains the headline",
    "headline and metadata",
    "metadata wrapper",
    "metadata-only entry",
    "placeholder or metadata-only entry",
    "article text is missing",
    "without any supporting article content",
    "without any substantive reporting content",
    "cannot provide a detailed summary",
    "cannot provide a factual summary",
    "the only concrete information available is the headline",
    "provided article metadata and text only contain a headline",
)

SUMMARY_ARTIFACT_PREFIX_RE = re.compile(
    r"^(?:let me provide|the correct format|header and proper markdown structure)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleSummaryRecord:
    title: str
    source: str
    published: str
    url: str
    article_id: str
    story: str
    summary: str
    raw_entry: str = ""


def build_article_heading(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "Untitled article").strip()
    return re.sub(r"\s+", " ", title)


def format_article_metadata(article: dict[str, Any]) -> str:
    metadata_lines = [
        f"- Source: {article.get('source') or 'Unknown source'}",
        f"- Published: {article.get('pub_date') or 'Unknown publish time'}",
        f"- URL: {article.get('url') or 'N/A'}",
    ]
    if article.get("article_id"):
        metadata_lines.append(f"- Article ID: {article.get('article_id')}")
    if article.get("story_title"):
        metadata_lines.append(f"- Story: {article.get('story_title')}")
    return "\n".join(metadata_lines)


def strip_model_artifacts(text: str) -> str:
    text = text or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|im_(?:start|end)\|>", "", text)
    text = re.sub(r"&lt;/?(?:analysis|content)&gt;", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:analysis|content)>", "", text, flags=re.IGNORECASE)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_structured_entry(text: str, heading_name: str) -> bool:
    clean_text = strip_model_artifacts(text)
    if "DATABASE_ENTRY:" in clean_text:
        return True
    return all(
        marker in clean_text
        for marker in (f"### {heading_name}", "Metadata:", "Summary:")
    )


def _extract_sentences(text: str, limit: int = 5) -> list[str]:
    clean_text = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean_text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    return [sentence.strip() for sentence in sentences if sentence.strip()][:limit]


def fallback_record(article: dict[str, Any]) -> ArticleSummaryRecord:
    sentences = _extract_sentences(str(article.get("text") or ""), limit=5)
    summary = " ".join(sentences).strip()
    if not summary:
        summary = (
            "No reliable summary generated because the article was retrieved but the model "
            "connection failed before a synthesis could be produced."
        )
    return ArticleSummaryRecord(
        title=build_article_heading(article),
        source=str(article.get("source") or "Unknown source"),
        published=str(article.get("pub_date") or "Unknown publish time"),
        url=str(article.get("url") or ""),
        article_id=str(article.get("article_id") or ""),
        story=str(article.get("story_title") or ""),
        summary=summary,
    )


def fallback_entry(article: dict[str, Any]) -> str:
    return render_markdown_entry(fallback_record(article), include_database_entry=True)


def normalize_model_response(article: dict[str, Any], raw_text: str) -> ArticleSummaryRecord:
    clean_text = strip_model_artifacts(raw_text)
    if "DATABASE_ENTRY:" in clean_text:
        clean_text = clean_text.split("DATABASE_ENTRY:", 1)[1].strip()

    heading_name = build_article_heading(article)
    heading_match = re.search(rf"###\s+{re.escape(heading_name)}\b", clean_text)
    if heading_match:
        clean_text = clean_text[heading_match.start():]

    summary = ""
    summary_match = re.search(r"Summary:\s*(.*)", clean_text, flags=re.DOTALL)
    if summary_match:
        summary = summary_match.group(1)

    summary = re.split(r"\n(?:---+|###\s+)", summary, maxsplit=1)[0]
    summary = strip_model_artifacts(summary)

    filtered_lines = []
    for line in summary.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^\*+\s*", "", stripped)
        if re.match(r'^[`"\']?\s*prefix\b', stripped, flags=re.IGNORECASE):
            continue
        if SUMMARY_ARTIFACT_PREFIX_RE.match(stripped):
            continue
        if stripped.startswith(f"{heading_name} -"):
            continue
        if stripped in {"---", "```", "`"}:
            continue
        filtered_lines.append(stripped)

    summary = re.sub(r"\s+", " ", " ".join(filtered_lines)).strip()
    if not summary:
        summary = "No reliable summary generated because the model failed to format its response."

    return ArticleSummaryRecord(
        title=heading_name,
        source=str(article.get("source") or "Unknown source"),
        published=str(article.get("pub_date") or "Unknown publish time"),
        url=str(article.get("url") or ""),
        article_id=str(article.get("article_id") or ""),
        story=str(article.get("story_title") or ""),
        summary=summary,
    )


def parse_markdown_entry(entry: str) -> ArticleSummaryRecord:
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

    return ArticleSummaryRecord(
        title=title_match.group(1).strip() if title_match else "",
        source=metadata.get("source", ""),
        published=metadata.get("published", ""),
        url=url,
        article_id=metadata.get("article id", ""),
        story=metadata.get("story", ""),
        summary=summary,
        raw_entry=clean_entry,
    )


def render_markdown_entry(record: ArticleSummaryRecord, *, include_database_entry: bool = False) -> str:
    lines = []
    if include_database_entry:
        lines.append("DATABASE_ENTRY:")
    lines.extend(
        [
            f"### {record.title or 'Untitled article'}",
            "Metadata:",
            f"- Source: {record.source or 'Unknown source'}",
            f"- Published: {record.published or 'Unknown publish time'}",
            f"- URL: {record.url or 'N/A'}",
        ]
    )
    if record.article_id:
        lines.append(f"- Article ID: {record.article_id}")
    if record.story:
        lines.append(f"- Story: {record.story}")
    lines.extend(["", "Summary:", record.summary])
    return "\n".join(lines)


def ensure_record(value: ArticleSummaryRecord | str | dict[str, Any]) -> ArticleSummaryRecord:
    """Adapt legacy summary shapes into records.

    Dict conversion is intentionally permissive because debug/history rows may
    omit fields added after they were written.
    """
    if isinstance(value, ArticleSummaryRecord):
        return value
    if isinstance(value, str):
        return parse_markdown_entry(value)
    return ArticleSummaryRecord(
        title=str(value.get("title") or ""),
        source=str(value.get("source") or ""),
        published=str(value.get("published") or ""),
        url=str(value.get("url") or ""),
        article_id=str(value.get("article_id") or ""),
        story=str(value.get("story") or ""),
        summary=str(value.get("summary") or ""),
        raw_entry=str(value.get("raw_entry") or ""),
    )


def records_by_article_id(records: list[ArticleSummaryRecord | str]) -> dict[str, ArticleSummaryRecord]:
    lookup: dict[str, ArticleSummaryRecord] = {}
    for value in records:
        record = ensure_record(value)
        if record.article_id:
            lookup[record.article_id] = record
    return lookup


def with_story(record: ArticleSummaryRecord, story_title: Any) -> ArticleSummaryRecord:
    """Attach story title and invalidate stale Markdown adapters."""
    return replace(record, story=str(story_title or ""), raw_entry="")


def summary_text(value: ArticleSummaryRecord | str) -> str:
    return ensure_record(value).summary


def article_id(value: ArticleSummaryRecord | str) -> str:
    return ensure_record(value).article_id


def story_label(value: ArticleSummaryRecord | str) -> str:
    return ensure_record(value).story


def reference_key(value: ArticleSummaryRecord | str) -> str:
    if isinstance(value, ArticleSummaryRecord):
        value = render_markdown_entry(value)
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()


def to_history_record(record: ArticleSummaryRecord, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "title": record.title,
        "source": record.source,
        "published": record.published,
        "url": record.url,
        "article_id": record.article_id,
        "story": record.story,
        "summary": record.summary,
        "raw_entry": record.raw_entry or render_markdown_entry(record),
    }


def to_history_records(records: list[ArticleSummaryRecord | str]) -> list[dict[str, Any]]:
    return [
        to_history_record(ensure_record(record), index)
        for index, record in enumerate(records, start=1)
    ]


def to_citation_source(record: ArticleSummaryRecord) -> dict[str, Any]:
    return {
        "title": record.title,
        "source": record.source,
        "published": record.published,
        "url": record.url,
        "article_id": record.article_id,
        "story": record.story,
        "summary": record.summary,
        "raw_entry": record.raw_entry or render_markdown_entry(record),
    }


def is_low_confidence(value: ArticleSummaryRecord | str) -> bool:
    clean_summary = summary_text(value).lower()
    if not clean_summary and isinstance(value, str):
        clean_summary = str(value or "").lower()
    return any(pattern in clean_summary for pattern in LOW_CONFIDENCE_SUMMARY_PATTERNS)
