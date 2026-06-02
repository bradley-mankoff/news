"""Assign drafted stories to configured topics and build topic-scoped reports."""

from __future__ import annotations

import json
import math
import re
import textwrap
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from . import embeddings as embeddings_stage
from . import citations as citations_stage
from .story_clustering import cosine_similarity, story_similarity_terms
from .story_drafting import article_summary_lookup_by_id, report_summary_text
from .topic_matching import (
    BOILERPLATE_CONTENT_STOPWORDS,
    TOPIC_MATCH_STOPWORDS,
    WEAK_TOPIC_MATCH_TERMS,
)


US_FOCUSED_TOPIC_IDS = {"us_economy", "us_politics"}
STORY_TOPIC_TOPICALITY_VERDICTS = {
    "obviously_topical",
    "not_obvious",
    "obviously_not_topical",
}
STORY_TOPIC_SCALE_VERDICTS = {
    "obviously_large_scale",
    "not_obvious",
    "obviously_small_scale",
}
STORY_TOPIC_DEFAULT_VERDICT = "not_obvious"
STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL = "obviously_not_topical"
STORY_TOPIC_OBVIOUSLY_SMALL_SCALE = "obviously_small_scale"
STORY_TOPIC_OBVIOUSLY_TOPICAL = "obviously_topical"
STORY_TOPIC_OBVIOUSLY_LARGE_SCALE = "obviously_large_scale"
STORY_TOPIC_OVERLAP_SUPPRESS_THRESHOLD = 0.50
STORY_TOPIC_VALIDATION_MAX_TOKENS = 3000
STORY_TOPIC_VALIDATION_BATCH_SIZE = 8
STORY_TOPIC_VALIDATION_TEXT_CHARS = 900
STORY_TOPIC_VALIDATION_SUMMARY_CHARS = 400
DAILY_PUZZLE_STORY_RE = re.compile(
    r"\b(?:(?:today(?:'|’)?s|daily)\b.{0,100}\b)?"
    r"(?:nyt\s+)?(?:connections|strands|wordle|mini\s+crossword|crossword)\b"
    r".{0,100}\b(?:hint|hints|answer|answers|clue|clues|help)\b",
    re.IGNORECASE,
)
US_FOCUS_RE = re.compile(
    r"\b(?:u\.s\.|us(?!\s*\$)|united states|american|america|white house|trump|biden|congress|"
    r"senate|house of representatives|supreme court|scotus|federal reserve|the fed|"
    r"washington|state department|defense department|homeland security|justice department|"
    r"ice|immigration detention|newark)\b",
    re.IGNORECASE,
)
US_ECONOMY_RE = re.compile(
    r"\b(?:economy|economic|inflation|jobs?|labor|workers?|wages?|consumer|households?|"
    r"spending|prices?|fed|federal reserve|interest rates?|gdp|stocks?|bonds?|treasury|"
    r"tariffs?|trade|housing|mortgage|recession|layoffs?|retail sales|manufacturing|"
    r"factory|markets?)\b",
    re.IGNORECASE,
)
FOREIGN_MACRO_RE = re.compile(
    r"\b(?:china|chinese|japan|japanese|india|indian|eurozone|european|germany|german|"
    r"france|french|britain|british|uk|canada|canadian|mexico|mexican|brazil|brazilian|"
    r"russia|russian|south korea|south korean|s\. korea|korea|korean|seoul)\b.{0,180}\b"
    r"(?:factory|manufacturing|pmi|industrial output|"
    r"exports?|imports?|economy|economic|growth|gdp|inflation|consumer prices?|"
    r"activity|output|stocks?|shares?|markets?|kospi|semiconductors?|chips?)\b",
    re.IGNORECASE,
)
FOREIGN_MARKET_RE = re.compile(
    r"\b(?:south korea|south korean|s\. korea|korea|korean|seoul|kospi)\b"
    r".{0,220}\b(?:stocks?|shares?|markets?|exports?|imports?|inflation|consumer prices?|"
    r"economy|economic|growth|gdp|semiconductors?|chips?|ai)\b",
    re.IGNORECASE,
)
US_DOMESTIC_ECONOMIC_EFFECT_RE = re.compile(
    r"\b(?:u\.s\.|us|united states|american)\s+(?:economy|inflation|jobs?|labor|"
    r"workers?|wages?|consumers?|households?|businesses?|companies|markets?|stocks?|"
    r"bonds?|treasury yields?|prices?|costs?|spending|growth|gdp|housing|exports?|"
    r"imports?)\b|\b(?:federal reserve|the fed|fomc|s&p 500|nasdaq|dow jones|"
    r"wall street|treasury yields?)\b",
    re.IGNORECASE,
)
NO_US_DOMESTIC_ECONOMIC_EFFECT_RE = re.compile(
    r"\b(?:no|without)\s+(?:explicit|direct|clear|meaningful|material)?\s*"
    r"(?:effects?|impacts?|consequences?|transmission)\s+(?:on|for)\s+"
    r"(?:u\.s\.|us|united states|american)\b|"
    r"\b(?:does not|doesn't|did not|didn't|do not|don't)\s+"
    r"(?:identify|describe|center|show|cite|report|establish)?\s*"
    r"(?:explicit|direct|clear|meaningful|material)?\s*"
    r"(?:effects?|impacts?|consequences?|transmission)\s+(?:on|for)\s+"
    r"(?:u\.s\.|us|united states|american)\b",
    re.IGNORECASE,
)
US_POLITICS_RE = re.compile(
    r"\b(?:white house|trump|biden|congress|senate|house republicans|house democrats|"
    r"supreme court|scotus|federal|administration|executive order|election|campaign|"
    r"immigration|border|ice|state department|defense department|justice department|"
    r"homeland security|u\.s\. military|us military)\b",
    re.IGNORECASE,
)
GLOBAL_BUSINESS_RE = re.compile(
    r"\b(?:business|company|companies|corporate|market|markets|stocks?|shares?|bond|"
    r"currency|commodity|commodities|oil|gas|shipping|trade|tariffs?|exports?|supply chain|"
    r"semiconductor|chips?|nvidia|earnings|revenue|profit|investment|investors?|finance|"
    r"bank|banking|central bank|ipo|merger|acquisition|deal|startup|valuation)\b",
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
PUBLIC_HEALTH_RE = re.compile(r"\b(?:ebola|outbreak|virus|vaccine|treatment centre)\b", re.IGNORECASE)


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
    story_topic_validation_enabled: bool
    build_chat_model: Callable[..., Any]
    invoke_with_retries: Callable[..., Any]
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


def _normalize_story_topic_topicality_verdict(verdict: str) -> str | None:
    clean_verdict = str(verdict or "").strip().lower().replace("-", "_").replace(" ", "_")
    if clean_verdict in STORY_TOPIC_TOPICALITY_VERDICTS:
        return clean_verdict
    legacy_verdicts = {
        "on_topic": STORY_TOPIC_OBVIOUSLY_TOPICAL,
        "topic_match": STORY_TOPIC_OBVIOUSLY_TOPICAL,
        "us_centered": STORY_TOPIC_OBVIOUSLY_TOPICAL,
        "ambiguous_or_related": STORY_TOPIC_DEFAULT_VERDICT,
        "ambiguous_or_us_related": STORY_TOPIC_DEFAULT_VERDICT,
        "not_sure": STORY_TOPIC_DEFAULT_VERDICT,
        "unclear": STORY_TOPIC_DEFAULT_VERDICT,
        "keep": STORY_TOPIC_DEFAULT_VERDICT,
        "off_topic": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
        "topic_mismatch": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
        "not_topic_match": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
        "obviously_not_us_centered": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
        "reject": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
        "drop": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
    }
    return legacy_verdicts.get(clean_verdict)


def _normalize_story_topic_scale_verdict(verdict: str) -> str | None:
    clean_verdict = str(verdict or "").strip().lower().replace("-", "_").replace(" ", "_")
    if clean_verdict in STORY_TOPIC_SCALE_VERDICTS:
        return clean_verdict
    legacy_verdicts = {
        "large": STORY_TOPIC_OBVIOUSLY_LARGE_SCALE,
        "large_scale": STORY_TOPIC_OBVIOUSLY_LARGE_SCALE,
        "major": STORY_TOPIC_OBVIOUSLY_LARGE_SCALE,
        "big": STORY_TOPIC_OBVIOUSLY_LARGE_SCALE,
        "not_sure": STORY_TOPIC_DEFAULT_VERDICT,
        "unclear": STORY_TOPIC_DEFAULT_VERDICT,
        "ambiguous": STORY_TOPIC_DEFAULT_VERDICT,
        "small": STORY_TOPIC_OBVIOUSLY_SMALL_SCALE,
        "small_scale": STORY_TOPIC_OBVIOUSLY_SMALL_SCALE,
        "minor": STORY_TOPIC_OBVIOUSLY_SMALL_SCALE,
        "local": STORY_TOPIC_OBVIOUSLY_SMALL_SCALE,
    }
    return legacy_verdicts.get(clean_verdict)


def parse_story_topic_screening_response(raw_text: str) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Parse story-topic screening JSON, failing open on unknown verdicts."""
    stats: dict[str, Any] = {
        "parse_failed": False,
        "entry_count": 0,
        "invalid_entry_count": 0,
        "unknown_topicality_count": 0,
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
        if entries is None and any(
            key in payload for key in ("story_key", "topicality", "topic_fit", "verdict", "scale")
        ):
            entries = [payload]
        if entries is None:
            for value in payload.values():
                if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                    entries = value
                    break
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
        stats["parse_error"] = "story-topic screening response was not a list"
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
        topicality = _normalize_story_topic_topicality_verdict(
            str(
                entry.get("topicality")
                or entry.get("topic_fit")
                or entry.get("topic_verdict")
                or entry.get("verdict")
                or ""
            )
        )
        if topicality is None:
            topicality = STORY_TOPIC_DEFAULT_VERDICT
            stats["unknown_topicality_count"] += 1
        scale = _normalize_story_topic_scale_verdict(
            str(entry.get("scale") or entry.get("story_scale") or "")
        )
        if scale is None:
            scale = STORY_TOPIC_DEFAULT_VERDICT
            stats["unknown_scale_count"] += 1
        topic_reason = _compact_gate_text(
            entry.get("topic_reason") or entry.get("reason"),
            300,
        )
        scale_reason = _compact_gate_text(entry.get("scale_reason"), 300)
        verdicts[story_key] = {
            "topicality": topicality,
            "scale": scale,
            "topic_reason": topic_reason,
            "scale_reason": scale_reason,
        }
    stats["entry_count"] = len(verdicts)
    return verdicts, stats


def _topic_screening_prompt_messages(
    topic: dict,
    candidates: list[dict[str, Any]],
) -> list[Any]:
    topic_title = str(topic.get("title") or topic.get("key") or "Unknown topic")
    topic_key = str(topic.get("key") or topic.get("id") or "")
    topic_description = _compact_gate_text(topic.get("description"), 1200)
    topic_rationale = _compact_gate_text(topic.get("rationale"), 500)
    required_context_terms = [
        str(term or "").strip()
        for term in topic.get("required_context_terms", [])[:30]
        if str(term or "").strip()
    ]
    boost_phrases = [
        str(phrase or "").strip()
        for phrase in topic.get("boost_phrases", [])[:30]
        if str(phrase or "").strip()
    ]
    is_us_focused_topic = topic_key in US_FOCUSED_TOPIC_IDS
    story_blocks: list[str] = []
    for index, candidate in enumerate(candidates):
        story_key = _validation_story_key(candidate, index)
        summaries = [
            _compact_gate_text(summary, STORY_TOPIC_VALIDATION_SUMMARY_CHARS)
            for summary in candidate.get("summaries", [])[:4]
            if str(summary or "").strip()
        ]
        story_blocks.append(
            textwrap.dedent(
                f"""
                Story key: {story_key}
                Topic: {topic_title} ({topic_key})
                Story title: {_compact_gate_text(candidate.get("story_title"), 220)}
                Story draft: {_compact_gate_text(candidate.get("paragraph") or candidate.get("story_text"), STORY_TOPIC_VALIDATION_TEXT_CHARS)}
                Article summaries:
                {chr(10).join(f"- {summary}" for summary in summaries) if summaries else "- N/A"}
                """
            ).strip()
        )

    us_focus_guidance = ""
    if is_us_focused_topic:
        us_focus_guidance = textwrap.dedent(
            """

            Extra requirement for this US-focused topic:
            The story must be meaningfully centered on the United States. Mark obviously_not_topical when
            the story is plainly centered on another country, region, economy, election, court,
            government, market, population, company, or conflict and the US is absent or only
            incidental. Do not treat a story as US-centered merely because it mentions global
            markets, presidents, parties, inflation, jobs, elections, courts, sanctions, tariffs,
            trade, diplomacy, dollar prices, investors, NATO, the UN, China, Europe, Russia, or
            a US official unless the supplied story explicitly centers a direct US decision, US
            role, or direct effect on US people, US markets, US law, US voters, US businesses, US
            workers, US households, or US policy.

            For US Economy, mark obviously_not_topical for stories centered on South Korean stocks,
            Seoul shares, KOSPI, South Korean exports, or South Korean inflation. This remains true
            when the story includes US$ dollar amounts, global investors, AI or semiconductor
            optimism, Nvidia/Jensen Huang, exports to the United States, or a US company mention,
            unless the supplied story explicitly centers direct consequences for US consumers,
            workers, households, Fed policy, broad US markets, US law/policy, US businesses, or US
            trade policy. If your topic reason says the story is "not directly centered on the US
            economy," the topicality label must be obviously_not_topical, not not_obvious.
            """
        ).rstrip()

    system_prompt = SystemMessage(
        content=textwrap.dedent(
            f"""
            You are a strict but conservative story-screening editor for a daily news newsletter.

            Your job is to label each drafted story with two conservative conclusions:
            topicality and scale. The labels are used to avoid obvious mistakes, not to separate
            good stories from great stories.

            Topic to validate:
            - Title: {topic_title}
            - ID: {topic_key}
            - Description: {topic_description or "N/A"}
            - Rationale: {topic_rationale or "N/A"}
            - Required context hints: {", ".join(required_context_terms) if required_context_terms else "N/A"}
            - Boost phrase hints: {", ".join(boost_phrases) if boost_phrases else "N/A"}

            Topicality labels:
            - obviously_topical: the story is centrally about this topic as defined above.
            - obviously_not_topical: the story is clearly centered elsewhere, merely adjacent,
              or relies on generic words without the topic's central frame.
            - not_obvious: the story has a plausible direct fit, or the supplied evidence is not
              enough to confidently call it obviously topical or obviously not topical.

            Scale labels:
            - obviously_large_scale: the story has clear broad stakes, such as effects across
              multiple countries, cross-border conflict, major civil war or mass displacement,
              oil, gas, food, semiconductors, shipping lanes, critical minerals, supply chains,
              sanctions, currency or financial markets, global public health, major migration,
              multinational regulation, or a national US political/economic/legal effect.
            - obviously_small_scale: in a global topic, the story is plainly a routine single-
              country domestic matter, local crime, local accident, city/province dispute, or
              ordinary single-company item without broader market, supply-chain, diplomatic, or
              security effects. In a US topic, the story is plainly limited to one state or local
              area without a national, federal, market, legal, or electoral implication.
            - not_obvious: the scale is borderline or the supplied evidence does not justify an
              obvious large/small conclusion.

            Do not mark civil wars, interstate conflicts, landmark court cases, landmark elections,
            NYC mayoral elections, California gubernatorial elections, nationally covered Senate
            races, or state stories with clear federal/national implications as obviously small.

            Be conservative: flag only obvious misses and obvious small-scale stories. Judge
            topic fit from the supplied story draft and article summaries; do not invent missing
            facts or rescue a story based on what might be true outside the supplied evidence.
            {us_focus_guidance}

            Return only valid JSON as an array of objects:
            [{{
              "story_key":"...",
              "topicality":"obviously_topical|not_obvious|obviously_not_topical",
              "scale":"obviously_large_scale|not_obvious|obviously_small_scale",
              "topic_reason":"short topic-fit reason",
              "scale_reason":"short scale reason"
            }}]
            """
        ).strip()
    )
    user_prompt = HumanMessage(
        content=(
            "Validate these candidate stories for the supplied newsletter topic.\n\n"
            + "\n\n---\n\n".join(story_blocks)
        )
    )
    return [system_prompt, user_prompt]


def _topic_screening_fallback_content(candidates: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            {
                "story_key": _validation_story_key(candidate, index),
                "topicality": STORY_TOPIC_DEFAULT_VERDICT,
                "scale": STORY_TOPIC_DEFAULT_VERDICT,
                "topic_reason": "Story-topic screening unavailable; kept fail-open.",
                "scale_reason": "Story-topic screening unavailable; kept fail-open.",
            }
            for index, candidate in enumerate(candidates)
        ]
    )


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


def _deterministic_story_topic_record(topic: dict, candidate: dict[str, Any]) -> dict[str, str]:
    topic_key = str(topic.get("key") or topic.get("id") or "")
    text = _story_screening_text(candidate)
    topicality = STORY_TOPIC_DEFAULT_VERDICT
    scale = STORY_TOPIC_DEFAULT_VERDICT
    topic_reason = "No deterministic obvious topicality exclusion."
    scale_reason = "No deterministic obvious scale exclusion."

    if DAILY_PUZZLE_STORY_RE.search(text):
        return {
            "topicality": STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL,
            "scale": STORY_TOPIC_OBVIOUSLY_SMALL_SCALE,
            "topic_reason": "Daily puzzle hints or answers are not newsletter news stories.",
            "scale_reason": "Daily puzzle help is evergreen service content, not a major news event.",
        }

    if topic_key == "us_economy":
        if not US_FOCUS_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "US Economy requires a story centered on direct US economic effects."
        elif not US_ECONOMY_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "Story mentions the US but is not centered on economic conditions or markets."
        elif (FOREIGN_MACRO_RE.search(text) or FOREIGN_MARKET_RE.search(text)) and (
            not US_DOMESTIC_ECONOMIC_EFFECT_RE.search(text)
            or NO_US_DOMESTIC_ECONOMIC_EFFECT_RE.search(text)
        ):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "Foreign macroeconomic data lacks an explicit direct effect on US households, workers, markets, or policy."
    elif topic_key == "us_politics":
        if not US_FOCUS_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "US Politics requires US federal, national, electoral, court, or policy center."
        elif not US_POLITICS_RE.search(text):
            topicality = STORY_TOPIC_DEFAULT_VERDICT
            topic_reason = "US-centered story lacks an obvious federal political cue; left to LLM/ranking."
    elif topic_key == "global_business_finance":
        if not GLOBAL_BUSINESS_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "Global Business & Finance requires a business, market, trade, or finance channel."
    elif topic_key == "global_crises_conflict":
        if PUBLIC_HEALTH_RE.search(text) and not CONFLICT_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "Public-health update is not centered on armed conflict or state/proxy violence."
        elif LOCAL_UNREST_RE.search(text) and not CONFLICT_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            scale = STORY_TOPIC_OBVIOUSLY_SMALL_SCALE
            topic_reason = "Sports celebration unrest is not an armed conflict or global security crisis."
            scale_reason = "The story is local public disorder around a sporting event."
        elif not CONFLICT_RE.search(text):
            topicality = STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
            topic_reason = "Global Crises & Conflict requires armed conflict, military action, or mass violence."

    return {
        "topicality": topicality,
        "scale": scale,
        "topic_reason": topic_reason,
        "scale_reason": scale_reason,
    }


def _annotated_story_topic_candidate(
    candidate: dict[str, Any],
    verdict_record: dict[str, str],
) -> dict[str, Any]:
    topicality = verdict_record["topicality"]
    scale = verdict_record["scale"]
    topic_reason = verdict_record.get("topic_reason") or ""
    scale_reason = verdict_record.get("scale_reason") or ""
    return {
        **candidate,
        "topic_screening_topicality": topicality,
        "topic_screening_scale": scale,
        "topic_screening_topic_reason": topic_reason,
        "topic_screening_scale_reason": scale_reason,
        "topic_validation_verdict": topicality,
        "topic_validation_reason": topic_reason,
    }


def apply_story_topic_screening(
    topic: dict,
    candidates: list[dict[str, Any]],
    runtime: StoryTopicRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    topic_key = str(topic.get("key") or topic.get("id") or "")
    topic_title = str(topic.get("title") or topic_key or "Unknown topic")
    stats: dict[str, Any] = {
        "enabled": bool(runtime.story_topic_validation_enabled),
        "topic_key": topic_key,
        "topic_title": topic_title,
        "us_focus_required": topic_key in US_FOCUSED_TOPIC_IDS,
        "candidate_count": len(candidates),
        "judged_count": 0,
        "preferred_count": 0,
        "obvious_exclusion_count": 0,
        "fallback_kept_count": 0,
        "missing_verdict_count": 0,
        "unknown_topicality_count": 0,
        "unknown_scale_count": 0,
        "parse_failed": False,
        "topicality_counts": {},
        "scale_counts": {},
    }
    fallback_annotations = [
        {
            **candidate,
            "topic_screening_topicality": STORY_TOPIC_DEFAULT_VERDICT,
            "topic_screening_scale": STORY_TOPIC_DEFAULT_VERDICT,
            "topic_screening_topic_reason": "Story-topic screening unavailable; kept fail-open.",
            "topic_screening_scale_reason": "Story-topic screening unavailable; kept fail-open.",
            "topic_validation_verdict": STORY_TOPIC_DEFAULT_VERDICT,
            "topic_validation_reason": "Story-topic screening unavailable; kept fail-open.",
        }
        for candidate in candidates
    ]
    if not runtime.story_topic_validation_enabled:
        stats["skipped_reason"] = "disabled"
        stats["preferred_count"] = len(candidates)
        stats["topicality_counts"] = {STORY_TOPIC_DEFAULT_VERDICT: len(candidates)}
        stats["scale_counts"] = {STORY_TOPIC_DEFAULT_VERDICT: len(candidates)}
        return fallback_annotations, stats
    if not candidates:
        stats["skipped_reason"] = "no_candidates"
        return candidates, stats

    annotated: list[dict[str, Any]] = []
    topicality_counts: Counter[str] = Counter()
    scale_counts: Counter[str] = Counter()
    judged_count = 0
    missing_count = 0
    fallback_count = 0
    parse_failed_count = 0
    invalid_entry_count = 0
    unknown_topicality_count = 0
    unknown_scale_count = 0
    parse_errors: list[str] = []
    model_errors: list[str] = []
    batch_size = max(1, STORY_TOPIC_VALIDATION_BATCH_SIZE)

    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start : batch_start + batch_size]
        fallback_content = _topic_screening_fallback_content(batch)
        try:
            response = runtime.invoke_with_retries(
                runtime.build_chat_model(
                    max_tokens=STORY_TOPIC_VALIDATION_MAX_TOKENS,
                    task="story_topic_validation",
                ),
                _topic_screening_prompt_messages(topic, batch),
                task_name=f"story-topic screening for {topic_title}",
                fallback_content=fallback_content,
            )
            raw_response = str(getattr(response, "content", response) or "")
        except Exception as error:
            raw_response = ""
            model_errors.append(f"{type(error).__name__}: {error}")

        verdicts, parse_stats = parse_story_topic_screening_response(raw_response)
        parse_failed = bool(parse_stats.get("parse_failed"))
        fallback_response = bool(raw_response.strip()) and raw_response.strip() == fallback_content.strip()
        invalid_entry_count += int(parse_stats.get("invalid_entry_count") or 0)
        unknown_topicality_count += int(parse_stats.get("unknown_topicality_count") or 0)
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
            else:
                fallback_count += 1
                if not verdict_record:
                    missing_count += 1
                verdict_record = _deterministic_story_topic_record(topic, candidate)
                reason_prefix = (
                    "Story-topic screening parse failed; deterministic fallback: "
                    if parse_failed
                    else "Story-topic screening unavailable; deterministic fallback: "
                    if fallback_response or not raw_response.strip()
                    else "Story-topic screening omitted this story; deterministic fallback: "
                )
                verdict_record = {
                    **verdict_record,
                    "topic_reason": reason_prefix + verdict_record.get("topic_reason", ""),
                    "scale_reason": reason_prefix + verdict_record.get("scale_reason", ""),
                }
            topicality_counts[verdict_record["topicality"]] += 1
            scale_counts[verdict_record["scale"]] += 1
            annotated.append(_annotated_story_topic_candidate(candidate, verdict_record))

    stats["judged_count"] = judged_count
    stats["preferred_count"] = sum(1 for candidate in annotated if _story_topic_screening_preferred(candidate))
    stats["obvious_exclusion_count"] = len(annotated) - int(stats["preferred_count"])
    stats["missing_verdict_count"] = missing_count
    stats["fallback_kept_count"] = fallback_count
    stats["deterministic_fallback_count"] = fallback_count
    stats["parse_failed"] = parse_failed_count > 0
    stats["parse_failed_batch_count"] = parse_failed_count
    stats["batch_count"] = (len(candidates) + batch_size - 1) // batch_size
    stats["parse_error"] = "; ".join(parse_errors[:3])
    stats["invalid_entry_count"] = invalid_entry_count
    stats["unknown_topicality_count"] = unknown_topicality_count
    stats["unknown_scale_count"] = unknown_scale_count
    if model_errors:
        stats["model_error"] = "; ".join(model_errors[:3])
    stats["topicality_counts"] = dict(topicality_counts)
    stats["scale_counts"] = dict(scale_counts)
    return annotated, stats


def _story_topic_screening_preferred(story: dict[str, Any]) -> bool:
    return (
        str(story.get("topic_screening_topicality") or STORY_TOPIC_DEFAULT_VERDICT)
        != STORY_TOPIC_OBVIOUSLY_NOT_TOPICAL
        and str(story.get("topic_screening_scale") or STORY_TOPIC_DEFAULT_VERDICT)
        != STORY_TOPIC_OBVIOUSLY_SMALL_SCALE
    )


def _story_topic_current_rank(story: dict[str, Any]) -> tuple:
    return (
        -float(story.get("story_strength_score") or 0.0),
        -int(story.get("source_count") or 0),
        -int(story.get("article_count") or 0),
        -float(story.get("average_similarity") or 0.0),
        int(story.get("story_rank") or 0),
        str(story.get("story_title") or ""),
    )


def _story_topic_queue_rank(story: dict[str, Any]) -> tuple:
    return (
        0 if _story_topic_screening_preferred(story) else 1,
        *_story_topic_current_rank(story),
    )


def _topic_embedding_text(topic: dict[str, Any]) -> str:
    return " ".join(
        str(topic.get(key) or "")
        for key in ("title", "description", "rationale")
    )


def _story_embedding_text(story: dict[str, Any]) -> str:
    return " ".join(
        [
            str(story.get("story_title") or ""),
            str(story.get("paragraph") or story.get("story_text") or ""),
            " ".join(str(summary) for summary in story.get("summaries", [])[:8]),
        ]
    )


def _annotate_embedding_topic_fit_scores(
    topic: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    if not candidates:
        return candidates, ""
    try:
        vectors = embeddings_stage.embed_texts(
            [_topic_embedding_text(topic), *[_story_embedding_text(candidate) for candidate in candidates]]
        )
        topic_vector = vectors[0]
        annotated = []
        for index, candidate in enumerate(candidates, start=1):
            annotated.append(
                {
                    **candidate,
                    "embedding_topic_fit_score": round(float(vectors[index] @ topic_vector), 4),
                }
            )
        return annotated, ""
    except Exception as error:
        return candidates, f"{type(error).__name__}: {error}"


def _story_selection_key(story: dict[str, Any]) -> str:
    story_key = str(story.get("story_key") or "").strip()
    if story_key:
        return story_key
    return f"story-index-{story.get('story_index')}"


def _story_article_id_set(story: dict[str, Any]) -> set[str]:
    return {
        str(article_id or "").strip()
        for article_id in (story.get("cluster_article_ids") or story.get("article_ids") or [])
        if str(article_id or "").strip()
    }


def _story_article_overlap(
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[float, set[str]]:
    left_ids = _story_article_id_set(left)
    right_ids = _story_article_id_set(right)
    if not left_ids or not right_ids:
        return 0.0, set()
    shared_ids = left_ids & right_ids
    return len(shared_ids) / max(1, min(len(left_ids), len(right_ids))), shared_ids


def _story_overlap_owner_rank(story: dict[str, Any]) -> tuple:
    embedding_score = story.get("embedding_topic_fit_score")
    return (
        float(embedding_score) if embedding_score is not None else -1.0,
        1
        if str(story.get("topic_screening_topicality") or "") == STORY_TOPIC_OBVIOUSLY_TOPICAL
        else 0,
        1
        if str(story.get("topic_screening_scale") or "") == STORY_TOPIC_OBVIOUSLY_LARGE_SCALE
        else 0,
        float(story.get("story_strength_score") or 0.0),
        int(story.get("source_count") or 0),
        int(story.get("article_count") or 0),
        -int(story.get("story_rank") or 0),
        str(story.get("story_title") or ""),
    )


def _find_article_overlap_conflict(
    selected_matches: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], float, set[str]] | None:
    best_conflict: tuple[float, int, dict[str, Any], dict[str, Any], set[str]] | None = None
    for left_index, left in enumerate(selected_matches):
        for right in selected_matches[left_index + 1 :]:
            overlap_ratio, shared_ids = _story_article_overlap(left, right)
            if overlap_ratio < STORY_TOPIC_OVERLAP_SUPPRESS_THRESHOLD:
                continue
            conflict_rank = (overlap_ratio, len(shared_ids))
            if best_conflict is None or conflict_rank > (best_conflict[0], best_conflict[1]):
                best_conflict = (overlap_ratio, len(shared_ids), left, right, shared_ids)
    if best_conflict is None:
        return None
    _, _, left, right, shared_ids = best_conflict
    left_rank = _story_overlap_owner_rank(left)
    right_rank = _story_overlap_owner_rank(right)
    winner, loser = (left, right) if left_rank >= right_rank else (right, left)
    return winner, loser, best_conflict[0], shared_ids


def _selected_story_debug_record(story: dict[str, Any]) -> dict[str, Any]:
    return {
        "story_key": story.get("story_key"),
        "story_title": story.get("story_title"),
        "topic_fit_score": story.get("topic_fit_score"),
        "embedding_topic_fit_score": story.get("embedding_topic_fit_score"),
        "owned_topic_key": story.get("owned_topic_key"),
        "owned_topic_title": story.get("owned_topic_title"),
        "min_distance_to_selected": story.get("min_distance_to_selected"),
        "article_count": story.get("article_count"),
        "source_count": story.get("source_count"),
        "story_strength_score": story.get("story_strength_score"),
        "average_similarity": story.get("average_similarity"),
        "topic_screening_topicality": story.get("topic_screening_topicality"),
        "topic_screening_scale": story.get("topic_screening_scale"),
        "topic_screening_topic_reason": story.get("topic_screening_topic_reason"),
        "topic_screening_scale_reason": story.get("topic_screening_scale_reason"),
        "selected_as_screening_fallback": story.get("selected_as_screening_fallback"),
        "article_ids": story.get("article_ids", []),
        "cluster_article_ids": story.get("cluster_article_ids", []),
        "preview": str(story.get("paragraph") or "")[:500],
    }


def classify_story_drafts_for_topics(
    story_drafts: list[dict[str, Any]],
    topics: list[dict],
    runtime: StoryTopicRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    topic_debug: dict[str, dict[str, Any]] = {}
    all_story_scores: list[dict[str, Any]] = []
    topic_order = [str(topic.get("key") or "") for topic in topics]
    topic_by_key = {str(topic.get("key") or ""): topic for topic in topics}
    topic_title_by_key = {
        str(topic.get("key") or ""): str(topic.get("title") or topic.get("key") or "Unknown topic")
        for topic in topics
    }
    topic_key_by_title = {
        str(topic.get("title") or topic.get("key") or "").strip().lower(): str(topic.get("key") or "")
        for topic in topics
    }
    story_vectors = [_story_topic_selection_vector(story) for story in story_drafts]
    topic_screening_debug: dict[str, dict[str, Any]] = {}
    topic_candidate_pools: dict[str, dict[str, Any]] = {}

    for story_index, story in enumerate(story_drafts):
        home_topic_key = str(story.get("topic_key") or "").strip()
        if not home_topic_key:
            home_topic_key = topic_key_by_title.get(
                str(story.get("topic_title") or "").strip().lower(),
                "",
            )
        story_scores: dict[str, int] = {}
        raw_score = 0
        if home_topic_key in topic_by_key:
            story_scores[home_topic_key] = raw_score

        eligible_topic_keys = [home_topic_key] if home_topic_key in topic_by_key else []
        owned_topic_key = home_topic_key if eligible_topic_keys else ""

        all_story_scores.append(
            {
                "story_index": story_index,
                "story_key": story.get("story_key"),
                "story_title": story.get("story_title"),
                "home_topic_key": home_topic_key,
                "home_topic_title": topic_title_by_key.get(home_topic_key, ""),
                "scores_by_topic": story_scores,
                "home_topic_fit_score": raw_score,
                "best_score": raw_score,
                "eligible_topic_keys": eligible_topic_keys,
                "owned_topic_key": owned_topic_key,
                "owned_topic_title": topic_title_by_key.get(owned_topic_key, ""),
                "ownership_reason": (
                    "home_topic_from_article_classification"
                    if owned_topic_key
                    else "home_topic_not_configured"
                    if home_topic_key
                    else "missing_home_topic"
                ),
            }
        )

    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        owned_candidates: list[dict[str, Any]] = []
        eligible_candidate_count = 0
        for story_index, (story, score_record) in enumerate(zip(story_drafts, all_story_scores)):
            home_topic_key = str(score_record.get("home_topic_key") or "")
            if home_topic_key != topic_key:
                continue
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
            eligible_candidate_count += 1
            owned_candidates.append(candidate_record)

        owned_candidates_before_screening = len(owned_candidates)
        owned_candidates, screening_stats = apply_story_topic_screening(
            topic,
            owned_candidates,
            runtime,
        )
        owned_candidates, embedding_error = _annotate_embedding_topic_fit_scores(topic, owned_candidates)
        if embedding_error:
            screening_stats["embedding_topic_fit_error"] = embedding_error
        topic_screening_debug[topic_title] = screening_stats
        ranked_queue = sorted(owned_candidates, key=_story_topic_queue_rank)
        topic_candidate_pools[topic_title] = {
            "topic_key": topic_key,
            "topic_title": topic_title,
            "candidate_count": eligible_candidate_count,
            "owned_candidate_count": owned_candidates_before_screening,
            "candidates": owned_candidates,
            "ranked_queue": ranked_queue,
            "screening_preferred_count": sum(
                1 for candidate in owned_candidates if _story_topic_screening_preferred(candidate)
            ),
            "embedding_topic_fit_error": embedding_error,
        }

    banned_by_topic: dict[str, set[str]] = {topic_title: set() for topic_title in topic_candidate_pools}
    overlap_events: list[dict[str, Any]] = []

    def select_for_topic(topic_title: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pool = topic_candidate_pools.get(topic_title) or {}
        selected_for_topic: list[dict[str, Any]] = []
        selected_vectors: list[Counter[str]] = []
        skipped_for_diversity: list[dict[str, Any]] = []
        banned_story_keys = banned_by_topic.get(topic_title, set())
        for candidate in pool.get("ranked_queue") or []:
            story_key = _story_selection_key(candidate)
            if story_key in banned_story_keys:
                continue
            if not _story_topic_screening_preferred(candidate):
                continue
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
                "selected_as_screening_fallback": False,
            }
            if len(selected_for_topic) >= runtime.max_stories_per_topic:
                continue
            if selected_for_topic and min_distance_to_selected < runtime.diversity_min_distance:
                skipped_for_diversity.append(
                    {
                        **candidate_with_distance,
                        "reason": "below_topic_story_diversity_min_distance",
                    }
                )
                continue
            selected_for_topic.append(candidate_with_distance)
            selected_vectors.append(candidate_vector)
        return selected_for_topic, skipped_for_diversity

    selected_by_topic_matches: dict[str, list[dict[str, Any]]] = {}
    skipped_by_topic: dict[str, list[dict[str, Any]]] = {}

    def recompute_selected_matches() -> None:
        selected_by_topic_matches.clear()
        skipped_by_topic.clear()
        for topic in topics:
            topic_title = str(topic.get("title") or topic.get("key") or "Unknown topic")
            selected, skipped = select_for_topic(topic_title)
            selected_by_topic_matches[topic_title] = selected
            skipped_by_topic[topic_title] = skipped

    recompute_selected_matches()
    max_overlap_iterations = max(1, sum(len(pool.get("candidates") or []) for pool in topic_candidate_pools.values()) + 1)
    for _iteration in range(max_overlap_iterations):
        selected_flat = [
            story
            for topic in topics
            for story in selected_by_topic_matches.get(str(topic.get("title") or topic.get("key") or "Unknown topic"), [])
        ]
        conflict = _find_article_overlap_conflict(selected_flat)
        if conflict is None:
            break
        winner, loser, overlap_ratio, shared_ids = conflict
        loser_topic_title = str(loser.get("topic_title") or loser.get("owned_topic_title") or "")
        loser_key = _story_selection_key(loser)
        if not loser_topic_title or not loser_key:
            break
        banned_by_topic.setdefault(loser_topic_title, set()).add(loser_key)
        overlap_events.append(
            {
                "winner_story_key": winner.get("story_key"),
                "winner_story_title": winner.get("story_title"),
                "winner_topic_title": winner.get("topic_title"),
                "winner_embedding_topic_fit_score": winner.get("embedding_topic_fit_score"),
                "loser_story_key": loser.get("story_key"),
                "loser_story_title": loser.get("story_title"),
                "loser_topic_title": loser.get("topic_title"),
                "loser_embedding_topic_fit_score": loser.get("embedding_topic_fit_score"),
                "overlap_ratio": round(overlap_ratio, 4),
                "shared_article_count": len(shared_ids),
                "shared_article_ids": sorted(shared_ids)[:20],
                "reason": "article_overlap_conflict_lost_to_embedding_topic_fit",
            }
        )
        recompute_selected_matches()

    selected_matches = [
        story
        for topic in topics
        for story in selected_by_topic_matches.get(str(topic.get("title") or topic.get("key") or "Unknown topic"), [])
    ]

    for topic in topics:
        topic_title = str(topic.get("title") or topic.get("key") or "Unknown topic")
        pool = topic_candidate_pools.get(topic_title) or {}
        selected_for_topic = selected_by_topic_matches.get(topic_title, [])
        selected_keys = {_story_selection_key(story) for story in selected_for_topic}
        banned_keys = banned_by_topic.get(topic_title, set())
        diversity_skipped_by_key = {
            _story_selection_key(story): story
            for story in skipped_by_topic.get(topic_title, [])
        }
        rejected: list[dict[str, Any]] = []
        for candidate in pool.get("ranked_queue") or []:
            story_key = _story_selection_key(candidate)
            if story_key in selected_keys:
                continue
            rejected_record = {
                "story_key": candidate.get("story_key"),
                "story_title": candidate.get("story_title"),
                "topic_fit_score": candidate.get("topic_fit_score"),
                "embedding_topic_fit_score": candidate.get("embedding_topic_fit_score"),
                "owned_topic_key": candidate.get("owned_topic_key"),
                "owned_topic_title": candidate.get("owned_topic_title"),
                "topic_screening_topicality": candidate.get("topic_screening_topicality"),
                "topic_screening_scale": candidate.get("topic_screening_scale"),
                "topic_screening_topic_reason": candidate.get("topic_screening_topic_reason"),
                "topic_screening_scale_reason": candidate.get("topic_screening_scale_reason"),
            }
            if story_key in banned_keys:
                rejected_record["reason"] = "article_overlap_conflict_lost"
            elif story_key in diversity_skipped_by_key:
                rejected_record["reason"] = "below_topic_story_diversity_min_distance"
                rejected_record["min_distance_to_selected"] = diversity_skipped_by_key[story_key].get(
                    "min_distance_to_selected"
                )
            elif not _story_topic_screening_preferred(candidate):
                rejected_record["reason"] = "screened_out_by_story_topic_screening"
            else:
                rejected_record["reason"] = "topic_story_limit_reached"
            rejected.append(rejected_record)

        topic_debug[topic_title] = {
            "topic_key": pool.get("topic_key") or str(topic.get("key") or ""),
            "selected_count": len(selected_for_topic),
            "candidate_count": pool.get("candidate_count", 0),
            "owned_candidate_count": pool.get("owned_candidate_count", 0),
            "screening_preferred_candidate_count": pool.get("screening_preferred_count", 0),
            "topic_validation_kept_candidate_count": pool.get("owned_candidate_count", 0),
            "min_score": None,
            "keyword_fit_gate_enabled": False,
            "diversity_min_distance": runtime.diversity_min_distance,
            "embedding_topic_fit_error": pool.get("embedding_topic_fit_error", ""),
            "selected": [_selected_story_debug_record(story) for story in selected_for_topic],
            "rejected": sorted(rejected, key=_story_topic_queue_rank)[:20],
        }

    selected_by_topic = {
        topic_title: details["selected_count"]
        for topic_title, details in topic_debug.items()
    }
    topic_screening_candidate_count = sum(
        int(stats.get("candidate_count") or 0)
        for stats in topic_screening_debug.values()
        if stats.get("enabled")
    )
    topicality_counts: Counter[str] = Counter()
    scale_counts: Counter[str] = Counter()
    for stats in topic_screening_debug.values():
        topicality_counts.update(stats.get("topicality_counts") or {})
        scale_counts.update(stats.get("scale_counts") or {})
    topic_screening_stats = {
        "enabled": bool(runtime.story_topic_validation_enabled),
        "us_focus_topic_ids": sorted(US_FOCUSED_TOPIC_IDS),
        "topic_ids": sorted(US_FOCUSED_TOPIC_IDS),
        "candidate_count": topic_screening_candidate_count,
        "judged_count": sum(
            int(stats.get("judged_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "preferred_count": sum(
            int(stats.get("preferred_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "obvious_exclusion_count": sum(
            int(stats.get("obvious_exclusion_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "fallback_kept_count": sum(
            int(stats.get("fallback_kept_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "deterministic_fallback_count": sum(
            int(stats.get("deterministic_fallback_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "parse_failed_count": sum(
            1
            for stats in topic_screening_debug.values()
            if stats.get("enabled") and stats.get("parse_failed")
        ),
        "parse_failed_batch_count": sum(
            int(stats.get("parse_failed_batch_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "batch_count": sum(
            int(stats.get("batch_count") or 0)
            for stats in topic_screening_debug.values()
            if stats.get("enabled")
        ),
        "topicality_counts": dict(topicality_counts),
        "scale_counts": dict(scale_counts),
        "topics": topic_screening_debug,
    }
    overlap_stats = {
        "enabled": True,
        "threshold": STORY_TOPIC_OVERLAP_SUPPRESS_THRESHOLD,
        "conflicts_resolved": len(overlap_events),
        "banned_story_count": sum(len(keys) for keys in banned_by_topic.values()),
        "events": overlap_events,
    }
    topic_validation_stats = {
        **topic_screening_stats,
        "kept_count": topic_screening_stats.get("candidate_count", 0),
        "dropped_count": 0,
        "verdict_counts": topic_screening_stats.get("topicality_counts", {}),
    }
    return selected_matches, {
        "enabled": True,
        "story_count": len(story_drafts),
        "selected_story_topic_count": len(selected_matches),
        "max_stories_per_topic": runtime.max_stories_per_topic,
        "min_score": None,
        "keyword_fit_gate_enabled": False,
        "topic_story_diversity_min_distance": runtime.diversity_min_distance,
        "selected_by_topic": selected_by_topic,
        "story_topic_screening": topic_screening_stats,
        "story_topic_validation": topic_validation_stats,
        "article_overlap_dedup": overlap_stats,
        "us_topic_country_gate": topic_validation_stats,
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
                f"Story headline: {_story_section_headline(story)}",
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
                f"Story headline: {_story_section_headline(story)}",
                "Story draft:",
                str(story.get("paragraph") or story.get("story_text") or "").strip(),
            ]
            sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)


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
    citation_registry = citations_stage.CitationRegistry()
    citation_diagnostics: list[dict[str, Any]] = []
    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        story_matches = matches_by_topic.get(topic_key) or []
        story_blocks: list[str] = []
        for story in story_matches:
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
                citation_diagnostics.append(
                    {
                        "topic": topic_title,
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
            story_body_parts = [paragraph]
            if rendered_contradiction_paragraph:
                story_body_parts.append(f"Contradictions: {rendered_contradiction_paragraph}")
            story_body = "\n\n".join(part for part in story_body_parts if part.strip())
            story_blocks.append(f"### {display_story_headline}\n\n{story_body}")
            required_story_blocks_by_topic.setdefault(topic_title, []).append(display_story_headline)
            attempts.append(
                {
                    "topic": topic_title,
                    "story": story.get("story_title"),
                    "story_headline": story_headline,
                    "display_story_headline": display_story_headline,
                    "story_level_citation_numbers": story_level_citation_numbers,
                    "contradiction_source_ids": contradiction_source_ids,
                    "contradiction_rendered": bool(rendered_contradiction_paragraph),
                    "valid": True,
                    "reason": "precomputed_story_draft",
                    "word_count": runtime.final_synthesis_word_count(story_body),
                    "topic_fit_score": story.get("topic_fit_score"),
                    "preview": story_body[:500],
                }
            )
        if story_blocks:
            required_topic_titles.append(topic_title)
            sections.append(
                f"## {runtime.format_topic_section_header(topic_title)}\n"
                + "\n\n".join(story_blocks)
            )

    final_synthesis = "\n\n".join(sections)
    citation_sources = citation_registry.sources()
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
        "citation_sources": citation_sources,
        "citation_source_count": len(citation_sources),
    }
    debug = {
        "attempts": attempts,
        "relaxed_guards": runtime.relaxed_final_synthesis_guards,
        "dev_fallback_used": False,
        "synthesis_method": "precomputed_story_drafts_post_topic_classification",
        "citation_diagnostics": citation_diagnostics,
    }
    return final_synthesis, token_stats, debug
