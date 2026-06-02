"""Compact topic context for model prompts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


TOPIC_CONTEXT_DESCRIPTION_CHARS = 360
TOPIC_CONTEXT_RATIONALE_CHARS = 240
TOPIC_CONTEXT_TERM_CHARS = 80


def _compact_text(value: Any, *, max_chars: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if max_chars is None or len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{truncated.rstrip(',;:')}..." if truncated else text[:max_chars].strip()


def _coerce_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, Iterable):
        candidates = [str(item or "") for item in value]
    else:
        candidates = []
    return [
        clean
        for item in candidates
        if (clean := _compact_text(item, max_chars=TOPIC_CONTEXT_TERM_CHARS))
    ]


def topic_records_by_key(topics: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    for topic in topics or []:
        key = _compact_text(topic.get("key") or topic.get("id"))
        if key:
            records[key] = topic
    return records


def build_topic_context(
    topic: Mapping[str, Any] | None,
    *,
    fallback_title: str | None = None,
    max_terms: int = 0,
) -> str:
    topic = topic or {}
    title = _compact_text(topic.get("title") or fallback_title or "Unknown topic")
    topic_id = _compact_text(topic.get("key") or topic.get("id"))
    description = _compact_text(
        topic.get("description"),
        max_chars=TOPIC_CONTEXT_DESCRIPTION_CHARS,
    )
    rationale = _compact_text(
        topic.get("rationale"),
        max_chars=TOPIC_CONTEXT_RATIONALE_CHARS,
    )
    frame_tags = _coerce_text_list(topic.get("frame_tags"))

    lines = [f"- Topic: {title}"]
    if topic_id:
        lines.append(f"- Topic id: {topic_id}")
    if description:
        lines.append(f"- Description: {description}")
    if rationale:
        lines.append(f"- Editorial rationale: {rationale}")
    if frame_tags:
        lines.append(f"- Frame tags: {', '.join(frame_tags)}")
    return "\n".join(lines)
