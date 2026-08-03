"""Global story selection and story-first report assembly."""

from __future__ import annotations

import json
import re
import textwrap
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from . import citations as citations_stage
from .article_summary_records import (
    ArticleSummaryRecord,
    records_by_article_id,
    with_story,
)
from .story_records import (
    ensure_story_record,
    story_article_overlap,
    story_debug_record,
    story_rank_key,
)
from .config import MODEL_TASK_STORY_SCALE_SCREENING
from .prompt_catalog import DEFAULT_PROMPT_INSTRUCTIONS
from .prompt_contracts import STORY_SCALE_SCREENING_JSON_CONTRACT


STORY_SCALE_VERDICTS = {
    "obviously_large_scale",
    "not_obvious",
    "obviously_small_scale",
}
STORY_SCALE_OBVIOUSLY_LARGE = "obviously_large_scale"
STORY_SCALE_DEFAULT_VERDICT = "not_obvious"
STORY_SCALE_OBVIOUSLY_SMALL = "obviously_small_scale"
# Also the tuning default (config.DEFAULT_STORY_SCALE_SCREENING_MAX_TOKENS);
# keep both in sync.
STORY_SCALE_VALIDATION_MAX_TOKENS = 3000
STORY_SCALE_VALIDATION_BATCH_SIZE = 8
STORY_SCALE_VALIDATION_TEXT_CHARS = 900
STORY_SCALE_VALIDATION_SUMMARY_CHARS = 400

DAILY_PUZZLE_STORY_RE = re.compile(
    r"\b(?:(?:today(?:'|')?s|daily)\b.{0,100}\b)?"
    r"(?:nyt\s+)?(?:connections|strands|wordle|mini\s+crossword|crossword)\b"
    r".{0,100}\b(?:hint|hints|answer|answers|clue|clues|help)\b",
    re.IGNORECASE,
)
CONFLICT_RE = re.compile(
    r"\b(?:war|armed conflict|civil war|military|troops?|airstrike|missile|drone|"
    r"bombing|shelling|invasion|ceasefire|rebel|militia|militants?|proxy|nuclear|"
    r"cross-border|offensive|strike|strikes|explosion|explosives|casualties|killed)\b",
    re.IGNORECASE,
)
LOCAL_UNREST_RE = re.compile(
    r"\b(?:psg|champions league|football|soccer|victory celebrations?|riots?|clashes|"
    r"vandalism|arrested)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StorySelectionRuntime:
    story_scale_screening_enabled: bool
    model_max_input_tokens: int
    model_label: str
    model_reference: str
    model_name: str
    model_backend: str
    relaxed_story_drafting_guards: bool
    build_chat_model: Callable[..., Any]
    invoke_with_retries: Callable[..., Any]
    build_article_heading: Callable[[dict], str]
    format_article_metadata: Callable[[dict], str]
    story_drafting_word_count: Callable[[str], int]
    is_low_confidence_report_entry: Callable[[str], bool]
    report_reference_key: Callable[[str], str]
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None
    prompt_instructions: str | None = None
    story_scale_screening_max_tokens: int = STORY_SCALE_VALIDATION_MAX_TOKENS


def _compact_gate_text(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].strip()


def _validation_story_key(candidate: dict[str, Any], index: int) -> str:
    story_key = str(candidate.get("story_key") or "").strip()
    if story_key:
        return story_key
    return f"story-{index + 1}"


def _json_block_from_text(text: str) -> str:
    clean_text = str(text or "").strip()
    if not clean_text:
        return ""
    fence_match = re.search(
        r"```(?:json)?\s*(.*?)```",
        clean_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence_match:
        clean_text = fence_match.group(1).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start = clean_text.find(opener)
        if start < 0:
            continue
        depth = 0
        for index in range(start, len(clean_text)):
            char = clean_text[index]
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return clean_text[start : index + 1]
    return clean_text


def _normalize_story_scale_verdict(verdict: str) -> str | None:
    clean_verdict = str(verdict or "").strip().lower().replace("-", "_").replace(" ", "_")
    if clean_verdict in STORY_SCALE_VERDICTS:
        return clean_verdict
    legacy_verdicts = {
        "large": "obviously_large_scale",
        "large_scale": "obviously_large_scale",
        "major": "obviously_large_scale",
        "big": "obviously_large_scale",
        "not_sure": STORY_SCALE_DEFAULT_VERDICT,
        "unclear": STORY_SCALE_DEFAULT_VERDICT,
        "ambiguous": STORY_SCALE_DEFAULT_VERDICT,
        "small": STORY_SCALE_OBVIOUSLY_SMALL,
        "small_scale": STORY_SCALE_OBVIOUSLY_SMALL,
        "minor": STORY_SCALE_OBVIOUSLY_SMALL,
        "local": STORY_SCALE_OBVIOUSLY_SMALL,
    }
    return legacy_verdicts.get(clean_verdict)


def parse_story_scale_screening_response(raw_text: str) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Parse story-scale screening JSON, failing open on unknown verdicts."""
    stats: dict[str, Any] = {
        "parse_failed": False,
        "entry_count": 0,
        "invalid_entry_count": 0,
        "unknown_scale_count": 0,
    }
    json_text = _json_block_from_text(raw_text)
    try:
        payload = json.loads(json_text)
    except Exception as error:
        stats["parse_failed"] = True
        stats["parse_error"] = str(error)
        return {}, stats

    if isinstance(payload, dict):
        entries = payload.get("stories") or payload.get("results") or payload.get("verdicts")
        if isinstance(entries, dict):
            entries = [
                {"story_key": str(story_key), **record}
                for story_key, record in entries.items()
                if isinstance(record, dict)
            ]
        if entries is None and any(key in payload for key in ("story_key", "id", "scale")):
            entries = [payload]
        if entries is None:
            keyed_entries: list[dict[str, Any]] = []
            for story_key, record in payload.items():
                if isinstance(record, dict):
                    keyed_entries.append({"story_key": str(story_key), **record})
            entries = keyed_entries if keyed_entries else None
    else:
        entries = payload

    if not isinstance(entries, list):
        stats["parse_failed"] = True
        stats["parse_error"] = "story-scale screening response was not a list"
        return {}, stats

    verdicts: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            stats["invalid_entry_count"] += 1
            continue
        story_key = str(entry.get("story_key") or entry.get("id") or "").strip()
        if not story_key:
            stats["invalid_entry_count"] += 1
            continue
        scale = _normalize_story_scale_verdict(str(entry.get("scale") or entry.get("story_scale") or ""))
        if scale is None:
            scale = STORY_SCALE_DEFAULT_VERDICT
            stats["unknown_scale_count"] += 1
        verdicts[story_key] = {
            "scale": scale,
            "scale_reason": _compact_gate_text(entry.get("scale_reason") or entry.get("reason"), 300),
        }
    stats["entry_count"] = len(verdicts)
    return verdicts, stats


def _story_screening_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(candidate.get("story_title") or ""),
            str(candidate.get("paragraph") or candidate.get("story_text") or ""),
            " ".join(str(summary or "") for summary in candidate.get("summaries", [])[:6]),
        )
        if part.strip()
    )


def _scale_screening_fallback_content(candidates: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            {
                "story_key": _validation_story_key(candidate, index),
                "scale": STORY_SCALE_DEFAULT_VERDICT,
                "scale_reason": (
                    "Story scale screening unavailable; not eligible because only "
                    "obviously_large_scale stories may reach final output."
                ),
            }
            for index, candidate in enumerate(candidates)
        ]
    )


def _annotated_story_candidate(
    candidate: dict[str, Any],
    verdict_record: dict[str, str],
) -> dict[str, Any]:
    return {
        **candidate,
        "scale_screening_scale": verdict_record["scale"],
        "scale_screening_reason": verdict_record.get("scale_reason") or "",
    }


def _global_scale_screening_prompt_messages(
    candidates: list[dict[str, Any]],
    *,
    prompt_instructions: str | None = None,
) -> list[Any]:
    story_blocks: list[str] = []
    for index, candidate in enumerate(candidates):
        story_key = _validation_story_key(candidate, index)
        summaries = [
            _compact_gate_text(summary, STORY_SCALE_VALIDATION_SUMMARY_CHARS)
            for summary in candidate.get("summaries", [])[:4]
            if str(summary or "").strip()
        ]
        story_blocks.append(
            textwrap.dedent(
                f"""
                Story key: {story_key}
                Story title: {_compact_gate_text(candidate.get("story_title"), 220)}
                Story draft: {_compact_gate_text(candidate.get("paragraph") or candidate.get("story_text"), STORY_SCALE_VALIDATION_TEXT_CHARS)}
                Article summaries:
                {chr(10).join(f"- {summary}" for summary in summaries) if summaries else "- N/A"}
                """
            ).strip()
        )

    # The JSON contract is injected as a .format() VALUE (inserted verbatim,
    # never re-parsed), so its single braces are safe here; only the template
    # itself must stay an f-string-free .format() block.
    # .format() must run after dedent() because the injected screening_guidance
    # is multi-line (byte-identity drift-guard: tests/test_prompt_catalog.py).
    # User-entered guidance (prompt overrides) may contain literal braces.
    # str.format() never re-parses substituted values, so the value is escaped
    # before injection ({{ }}) and unescaped afterwards to keep user text
    # byte-identical; the template's own JSON braces are already single { }
    # by then, so the unescape touches only the injected guidance.
    system_prompt = SystemMessage(
        content=textwrap.dedent(
            """
            You are a strict but conservative scale-screening editor for a global daily
            news newsletter.

            Your job is to label each drafted story by substantive news scale. The labels
            are used to avoid obvious small local stories, not to separate good stories
            from great stories.

            Scale labels:
            - obviously_large_scale: the story has clear broad stakes, such as effects across
              multiple countries, cross-border conflict, major civil war or mass displacement,
              oil, gas, food, semiconductors, shipping lanes, critical minerals, supply chains,
              sanctions, currency or financial markets, global public health, major migration,
              multinational regulation, national politics, national economic effects, major
              national legal effects, or major geopolitical/security implications.
            - obviously_small_scale: the story is plainly a routine single-country domestic
              matter, local crime, local accident, city/province dispute, provincial or municipal
              politics, or ordinary single-company item without broader market, supply-chain,
              diplomatic, humanitarian, legal, national political, or security effects.
            - not_obvious: the scale is borderline or the supplied evidence does not justify an
              obvious large/small conclusion.

            {screening_guidance}

            {scale_contract}
            """
        ).format(
            screening_guidance=(
                (prompt_instructions
                 or DEFAULT_PROMPT_INSTRUCTIONS["story_scale_screening"])
                .replace("{", "{{")
                .replace("}", "}}")
            ),
            scale_contract=STORY_SCALE_SCREENING_JSON_CONTRACT,
        ).replace("{{", "{").replace("}}", "}").strip()
    )
    user_prompt = HumanMessage(
        content=(
            "Screen these candidate stories for global-news scale.\n\n"
            + "\n\n---\n\n".join(story_blocks)
        )
    )
    return [system_prompt, user_prompt]


def _deterministic_global_scale_record(candidate: dict[str, Any]) -> dict[str, str]:
    text = _story_screening_text(candidate)
    if DAILY_PUZZLE_STORY_RE.search(text):
        return {
            "scale": STORY_SCALE_OBVIOUSLY_SMALL,
            "scale_reason": "Daily puzzle hints or answers are evergreen service content, not a major news event.",
        }
    if LOCAL_UNREST_RE.search(text) and not CONFLICT_RE.search(text):
        return {
            "scale": STORY_SCALE_OBVIOUSLY_SMALL,
            "scale_reason": "The story is local public disorder around a sporting event.",
        }
    return {
        "scale": STORY_SCALE_DEFAULT_VERDICT,
        "scale_reason": "No deterministic obvious scale exclusion.",
    }


def _global_scale_screening_eligible(story: dict[str, Any]) -> bool:
    return (
        str(story.get("scale_screening_scale") or STORY_SCALE_DEFAULT_VERDICT)
        == STORY_SCALE_OBVIOUSLY_LARGE
    )


def _selected_global_story_debug_record(story: dict[str, Any]) -> dict[str, Any]:
    return story_debug_record(story)


def apply_global_story_scale_screening(
    story_drafts: list[dict[str, Any]],
    runtime: StorySelectionRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stats: dict[str, Any] = {
        "enabled": bool(runtime.story_scale_screening_enabled),
        "required_scale": STORY_SCALE_OBVIOUSLY_LARGE,
        "candidate_count": len(story_drafts),
        "judged_count": 0,
        "kept_count": 0,
        "dropped_count": 0,
        "eligible_count": 0,
        "ineligible_count": 0,
        "fallback_count": 0,
        "fallback_kept_count": 0,
        "missing_verdict_count": 0,
        "unknown_scale_count": 0,
        "parse_failed": False,
        "scale_counts": {},
        "dropped": [],
    }
    if not runtime.story_scale_screening_enabled:
        dropped = [
            _annotated_story_candidate(
                story,
                {
                    "scale": STORY_SCALE_DEFAULT_VERDICT,
                    "scale_reason": (
                        "Story scale screening disabled; not eligible because only "
                        "obviously_large_scale stories may reach final output."
                    ),
                },
            )
            for story in story_drafts
        ]
        stats["skipped_reason"] = "disabled"
        stats["dropped_count"] = len(dropped)
        stats["ineligible_count"] = len(dropped)
        stats["scale_counts"] = {STORY_SCALE_DEFAULT_VERDICT: len(story_drafts)}
        stats["dropped"] = [_selected_global_story_debug_record(story) for story in dropped]
        return [], stats
    if not story_drafts:
        stats["skipped_reason"] = "no_candidates"
        return story_drafts, stats

    annotated: list[dict[str, Any]] = []
    scale_counts: Counter[str] = Counter()
    judged_count = 0
    missing_count = 0
    fallback_count = 0
    fallback_kept_count = 0
    parse_failed_count = 0
    unknown_scale_count = 0
    parse_errors: list[str] = []
    model_errors: list[str] = []
    batch_size = max(1, STORY_SCALE_VALIDATION_BATCH_SIZE)
    if runtime.progress_callback:
        runtime.progress_callback(
            "scale_screening_started",
            {
                "total": len(story_drafts),
                "candidate_count": len(story_drafts),
            },
        )

    for batch_start in range(0, len(story_drafts), batch_size):
        batch = story_drafts[batch_start : batch_start + batch_size]
        fallback_content = _scale_screening_fallback_content(batch)
        try:
            response = runtime.invoke_with_retries(
                runtime.build_chat_model(
                    max_tokens=runtime.story_scale_screening_max_tokens,
                    task=MODEL_TASK_STORY_SCALE_SCREENING,
                ),
                _global_scale_screening_prompt_messages(
                    batch,
                    prompt_instructions=runtime.prompt_instructions,
                ),
                task_name="global story scale screening",
                fallback_content=fallback_content,
            )
            raw_response = str(getattr(response, "content", response) or "")
        except Exception as error:
            raw_response = ""
            model_errors.append(f"{type(error).__name__}: {error}")

        verdicts, parse_stats = parse_story_scale_screening_response(raw_response)
        parse_failed = bool(parse_stats.get("parse_failed"))
        fallback_response = bool(raw_response.strip()) and raw_response.strip() == fallback_content.strip()
        unknown_scale_count += int(parse_stats.get("unknown_scale_count") or 0)
        if parse_failed:
            parse_failed_count += 1
            parse_error = str(parse_stats.get("parse_error") or "parse_failed")
            if parse_error and parse_error not in parse_errors:
                parse_errors.append(parse_error)

        for batch_index, candidate in enumerate(batch):
            story_key = _validation_story_key(candidate, batch_index)
            verdict_record = verdicts.get(story_key)
            if verdict_record and not fallback_response:
                judged_count += 1
                fallback_record = False
            else:
                fallback_count += 1
                fallback_record = True
                if not verdict_record:
                    missing_count += 1
                verdict_record = _deterministic_global_scale_record(candidate)
                reason_prefix = (
                    "Story scale screening parse failed; deterministic fallback: "
                    if parse_failed
                    else "Story scale screening unavailable; deterministic fallback: "
                    if fallback_response or not raw_response.strip()
                    else "Story scale screening omitted this story; deterministic fallback: "
                )
                verdict_record = {
                    **verdict_record,
                    "scale_reason": reason_prefix + verdict_record.get("scale_reason", ""),
                }
            scale_counts[verdict_record["scale"]] += 1
            annotated_story = _annotated_story_candidate(candidate, verdict_record)
            if fallback_record and _global_scale_screening_eligible(annotated_story):
                fallback_kept_count += 1
            annotated.append(annotated_story)
        if runtime.progress_callback:
            runtime.progress_callback(
                "scale_screening_batch_completed",
                {
                    "done": len(annotated),
                    "total": len(story_drafts),
                    "kept_count": sum(
                        1
                        for story in annotated
                        if _global_scale_screening_eligible(story)
                    ),
                    "fallback_count": fallback_count,
                },
            )

    kept = [story for story in annotated if _global_scale_screening_eligible(story)]
    dropped = [story for story in annotated if not _global_scale_screening_eligible(story)]
    stats["judged_count"] = judged_count
    stats["kept_count"] = len(kept)
    stats["dropped_count"] = len(dropped)
    stats["eligible_count"] = len(kept)
    stats["ineligible_count"] = len(dropped)
    stats["fallback_count"] = fallback_count
    stats["fallback_kept_count"] = fallback_kept_count
    stats["missing_verdict_count"] = missing_count
    stats["deterministic_fallback_count"] = fallback_count
    stats["parse_failed"] = parse_failed_count > 0
    stats["parse_failed_batch_count"] = parse_failed_count
    stats["batch_count"] = (len(story_drafts) + batch_size - 1) // batch_size
    stats["parse_error"] = "; ".join(parse_errors[:3])
    stats["unknown_scale_count"] = unknown_scale_count
    if model_errors:
        stats["model_error"] = "; ".join(model_errors[:3])
    stats["scale_counts"] = dict(scale_counts)
    stats["dropped"] = [_selected_global_story_debug_record(story) for story in dropped]
    return kept, stats




def _story_article_overlap(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[float, set[str]]:
    return story_article_overlap(left, right)


def _global_story_rank(story: dict[str, Any]) -> tuple:
    return story_rank_key(story)


def select_global_story_drafts(
    story_drafts: list[dict[str, Any]],
    *,
    max_stories: int,
    overlap_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_stories = max(0, int(max_stories or 0))
    overlap_threshold = max(0.0, float(overlap_threshold or 0.0))
    ranked_drafts = [
        {
            **story,
            "story_index": int(story.get("story_index") or index),
        }
        for index, story in enumerate(story_drafts)
    ]
    ranked_drafts = sorted(
        ranked_drafts,
        key=lambda story: (
            _global_story_rank(story),
            int(story.get("story_index") or 0),
        ),
    )

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    overlap_events: list[dict[str, Any]] = []
    for candidate in ranked_drafts:
        if len(selected) >= max_stories:
            rejected.append(
                {
                    **_selected_global_story_debug_record(candidate),
                    "reason": "global_story_limit_reached",
                }
            )
            continue

        conflicts: list[dict[str, Any]] = []
        for selected_story in selected:
            overlap_ratio, shared_ids = _story_article_overlap(candidate, selected_story)
            if overlap_ratio > overlap_threshold:
                conflicts.append(
                    {
                        "selected_story_key": selected_story.get("story_key"),
                        "selected_story_title": selected_story.get("story_title"),
                        "overlap_ratio": round(overlap_ratio, 4),
                        "shared_article_count": len(shared_ids),
                        "shared_article_ids": sorted(shared_ids)[:20],
                    }
                )
        if conflicts:
            event = {
                "rejected_story_key": candidate.get("story_key"),
                "rejected_story_title": candidate.get("story_title"),
                "overlap_threshold": round(overlap_threshold, 4),
                "conflicts": conflicts,
                "reason": "article_overlap_above_global_threshold",
            }
            overlap_events.append(event)
            rejected.append(
                {
                    **_selected_global_story_debug_record(candidate),
                    "reason": event["reason"],
                    "overlap_conflicts": conflicts,
                }
            )
            continue

        selected.append(
            {
                **candidate,
                "global_selection_rank": len(selected) + 1,
            }
        )

    stats = {
        "enabled": True,
        "story_count": len(story_drafts),
        "selected_story_count": len(selected),
        "max_stories": max_stories,
        "overlap_threshold": overlap_threshold,
        "selected": [_selected_global_story_debug_record(story) for story in selected],
        "rejected": rejected,
        "article_overlap_dedup": {
            "enabled": True,
            "threshold": overlap_threshold,
            "conflicts_resolved": len(overlap_events),
            "banned_story_count": len(overlap_events),
            "events": overlap_events,
        },
    }
    return selected, stats


def _annotate_summary_entry_for_story(
    record: ArticleSummaryRecord,
    story_match: dict[str, Any],
) -> ArticleSummaryRecord:
    return with_story(record, ensure_story_record(story_match).story_title)


def build_story_assigned_article_reports(
    selected_story_matches: list[dict[str, Any]],
    article_summary_reports: list[ArticleSummaryRecord | str],
    article_targets: list[dict],
    runtime: StorySelectionRuntime,
) -> tuple[list[ArticleSummaryRecord], dict[str, Any]]:
    summary_lookup = records_by_article_id(article_summary_reports)
    article_lookup = {
        str(article.get("article_id") or ""): article
        for article in article_targets
        if article.get("article_id")
    }

    reports: list[ArticleSummaryRecord] = []
    selected_article_ids: set[str] = set()
    missing_summary_ids: list[str] = []
    seen_records: set[tuple[str, str]] = set()
    for match in selected_story_matches:
        story_match = ensure_story_record(match)
        story_key = story_match.story_key
        for article_id in story_match.article_ids:
            clean_article_id = str(article_id or "").strip()
            if not clean_article_id:
                continue
            dedupe_key = (story_key, clean_article_id)
            if dedupe_key in seen_records:
                continue
            seen_records.add(dedupe_key)
            record = summary_lookup.get(clean_article_id)
            article = article_lookup.get(clean_article_id)
            if not record or not article:
                missing_summary_ids.append(clean_article_id)
                continue
            reports.append(_annotate_summary_entry_for_story(record, match))
            selected_article_ids.add(clean_article_id)

    return reports, {
        "candidate_story_count": len(selected_story_matches),
        "included_report_count": len(reports),
        "selected_unique_article_count": len(selected_article_ids),
        "missing_summary_article_ids": sorted(set(missing_summary_ids)),
    }


def _story_section_headline(story: dict[str, Any]) -> str:
    raw_headline = (
        story.get("story_headline")
        or story.get("display_story_headline")
        or story.get("story_title")
        or "Story update"
    )
    clean_headline = re.sub(r"(?m)^##+\s*", "", str(raw_headline or ""))
    clean_headline = re.sub(
        r"(?mi)^\s*(?:story\s+headline|headline)\s*:\s*",
        "",
        clean_headline,
    )
    clean_headline = re.sub(r"\[[0-9,\s]+\]", "", clean_headline)
    clean_headline = re.sub(r"\[\[[^\]]+\]\]", "", clean_headline)
    clean_headline = re.sub(r"[\r\n]+", " ", clean_headline)
    clean_headline = re.sub(r"\s+", " ", clean_headline).strip(" \"'")
    clean_headline = re.sub(r"\.+$", "", clean_headline).strip()
    words = clean_headline.split()
    if len(words) > 12:
        clean_headline = " ".join(words[:12])
    return clean_headline[:110].strip() or "Story update"


def _distinct_cited_source_ids(cited_sentences: list[dict[str, Any]]) -> list[str]:
    source_ids: list[str] = []
    for sentence in cited_sentences:
        for source_id in sentence.get("source_ids") or []:
            clean_source_id = re.sub(r"\s+", "", str(source_id or "")).upper()
            if clean_source_id and clean_source_id not in source_ids:
                source_ids.append(clean_source_id)
    return source_ids




def build_precomputed_global_story_synthesis(
    selected_story_matches: list[dict[str, Any]],
    reference_reports: list[str],
    runtime: StorySelectionRuntime,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    story_blocks: list[str] = []
    story_headlines: list[str] = []
    citation_groups: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    citation_registry = citations_stage.CitationRegistry()
    citation_diagnostics: list[dict[str, Any]] = []

    def add_group_citation_numbers(target: list[int], numbers: list[Any]) -> None:
        for raw_number in numbers:
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in target:
                target.append(number)

    for story in selected_story_matches:
        paragraph = str(
            story.get("main_story_paragraph")
            or story.get("paragraph")
            or story.get("story_text")
            or ""
        ).strip()
        if not paragraph:
            continue
        story_headline = _story_section_headline(story)
        display_story_headline = story_headline
        story_level_citation_numbers: list[int] = []
        story_citation_numbers: list[int] = []
        contradiction_paragraph = str(story.get("contradictions_paragraph") or "").strip()
        contradiction_source_ids = _distinct_cited_source_ids(
            list(story.get("contradiction_cited_sentences") or [])
        )
        rendered_contradiction_paragraph = ""
        if story.get("cited_sentences") and story.get("citation_sources"):
            rendered_story = citations_stage.render_cited_story(
                list(story.get("cited_sentences") or []),
                list(story.get("citation_sources") or []),
                citation_registry,
            )
            cited_paragraph = str(rendered_story.get("paragraph") or "").strip()
            if cited_paragraph:
                paragraph = cited_paragraph
            headline_citation_text = str(rendered_story.get("headline_citation_text") or "").strip()
            if headline_citation_text:
                display_story_headline = f"{story_headline} {headline_citation_text}"
            story_level_citation_numbers = [
                int(number)
                for number in rendered_story.get("story_level_source_numbers") or []
                if isinstance(number, int)
            ]
            add_group_citation_numbers(
                story_citation_numbers,
                list(rendered_story.get("story_citation_numbers") or []),
            )
            citation_diagnostics.append(
                {
                    "story": story.get("story_title"),
                    "source_count": len(story.get("citation_sources") or []),
                    "sentence_count": len(story.get("cited_sentences") or []),
                    "contradiction_sentence_count": len(
                        story.get("contradiction_cited_sentences") or []
                    ),
                    "contradiction_source_ids": contradiction_source_ids,
                    "story_level_citation_numbers": story_level_citation_numbers,
                    "story_level_source_sentence_counts": rendered_story.get(
                        "story_level_source_sentence_counts"
                    )
                    or {},
                    "render_citation_precedence_diagnostics": rendered_story.get(
                        "citation_precedence_diagnostics"
                    )
                    or {},
                    "diagnostics": story.get("citation_diagnostics") or {},
                    "contradiction_diagnostics": story.get(
                        "contradiction_citation_diagnostics"
                    )
                    or {},
                }
            )
        if (
            contradiction_paragraph
            and story.get("contradiction_cited_sentences")
            and story.get("citation_sources")
            and len(contradiction_source_ids) >= 2
        ):
            rendered_contradiction = citations_stage.render_cited_story(
                list(story.get("contradiction_cited_sentences") or []),
                list(story.get("citation_sources") or []),
                citation_registry,
                story_level_citation_sentence_threshold=None,
                apply_precedence=False,
            )
            rendered_contradiction_paragraph = str(
                rendered_contradiction.get("paragraph") or ""
            ).strip()
            add_group_citation_numbers(
                story_citation_numbers,
                list(rendered_contradiction.get("story_citation_numbers") or []),
            )
        story_body_parts = [paragraph]
        if rendered_contradiction_paragraph:
            story_body_parts.append(f"Contradictions: {rendered_contradiction_paragraph}")
        story_body = "\n\n".join(part for part in story_body_parts if part.strip())
        story_blocks.append(f"### {display_story_headline}\n\n{story_body}")
        story_headlines.append(display_story_headline)
        clean_story_headline = citations_stage.strip_citation_markers(story_headline)
        if story_citation_numbers:
            citation_groups.append(
                {
                    "story": clean_story_headline or "Untitled story",
                    "story_headline": clean_story_headline or "Untitled story",
                    "citation_numbers": story_citation_numbers,
                }
            )
        attempts.append(
            {
                "story": story.get("story_title"),
                "story_headline": story_headline,
                "display_story_headline": display_story_headline,
                "story_level_citation_numbers": story_level_citation_numbers,
                "story_citation_numbers": story_citation_numbers,
                "contradiction_source_ids": contradiction_source_ids,
                "contradiction_rendered": bool(rendered_contradiction_paragraph),
                "valid": True,
                "reason": "precomputed_story_draft",
                "word_count": runtime.story_drafting_word_count(story_body),
                "preview": story_body[:500],
            }
        )

    story_drafting = "\n\n".join(story_blocks)
    citation_sources = citation_registry.sources()
    token_stats = {
        "synthesis_method": "precomputed_global_story_drafts",
        "total_reports": len(reference_reports),
        "reports_included_in_synthesis": len(reference_reports),
        "reports_omitted_from_synthesis": 0,
        "high_confidence_reports": len(
            [entry for entry in reference_reports if not runtime.is_low_confidence_report_entry(entry)]
        ),
        "low_confidence_reports": len(
            [entry for entry in reference_reports if runtime.is_low_confidence_report_entry(entry)]
        ),
        "story_blocks_included": len(story_blocks),
        "model_max_input_tokens": runtime.model_max_input_tokens,
        "model_label": runtime.model_label,
        "model": runtime.model_reference,
        "model_name": runtime.model_name,
        "model_backend": runtime.model_backend,
        "required_story_headlines": story_headlines,
        "eligible_story_block_count": len(story_blocks),
        "explicit_story_mode": True,
        "included_report_keys": [runtime.report_reference_key(entry) for entry in reference_reports],
        "citation_sources": citation_sources,
        "citation_source_count": len(citation_sources),
        "citation_groups": citation_groups,
        "citation_group_count": len(citation_groups),
    }
    debug = {
        "attempts": attempts,
        "relaxed_guards": runtime.relaxed_story_drafting_guards,
        "fallback_synthesis_used": False,
        "synthesis_method": "precomputed_global_story_drafts",
        "citation_diagnostics": citation_diagnostics,
    }
    return story_drafting, token_stats, debug
