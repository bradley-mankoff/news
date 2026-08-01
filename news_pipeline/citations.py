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
BRACKETED_TEMPORARY_CITATION_LIST_RE = re.compile(
    r"\[(\s*\[[A-Za-z][A-Za-z0-9_-]*\](?:\s*[,;]\s*\[[A-Za-z][A-Za-z0-9_-]*\])+\s*)\]"
)
DISPLAY_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
DEFAULT_STORY_LEVEL_CITATION_SENTENCE_THRESHOLD = 2
PRIMARY_CITATION_RANK = 0
NEUTRAL_CITATION_RANK = 1
DERIVATIVE_CITATION_RANK = 2
CITATION_PRECEDENCE_PRIMARY_OVERLAP_MIN_TOKENS = 2
CITATION_PRECEDENCE_PRIMARY_OVERLAP_MIN_SCORE = 0.08

_ORG_ALIAS_TO_CANONICAL = {
    "afp": "afp",
    "agence france presse": "afp",
    "ap": "associated press",
    "ap news": "associated press",
    "associated press": "associated press",
    "the associated press": "associated press",
    "reuters": "reuters",
    "reuters com": "reuters",
}
_PRIMARY_WIRE_ORGS = {"afp", "associated press", "reuters"}
_WIRE_ORG_ALIASES = {
    "afp": ("afp", "agence france presse"),
    "associated press": ("ap", "ap news", "associated press", "the associated press"),
    "reuters": ("reuters", "reuters com"),
}
_ATTRIBUTION_BEFORE_ORG = (
    "according to",
    "based on",
    "citing",
    "credited to",
    "distributed by",
    "from",
    "published by",
    "reported by",
    "via",
)
_ATTRIBUTION_AFTER_ORG = (
    "confirmed",
    "contributed",
    "distributed",
    "provided",
    "reported",
    "said",
    "says",
    "wrote",
)
_SAME_ORG_REFERENCE_RE = re.compile(
    r"\b(?:previously|earlier)\s+reported\b|\breported\s+(?:previously|earlier)\b"
    r"|\bas\s+(?:we|the\s+outlet|[a-z0-9 ]{2,48})\s+reported\s+(?:previously|earlier)\b"
    r"|\b(?:previous|earlier)\s+(?:story|report|article|coverage)\b",
    flags=re.IGNORECASE,
)

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


def _fold_label(value: Any) -> str:
    clean_value = str(value or "").lower().replace("&", " and ")
    clean_value = re.sub(r"[^a-z0-9]+", " ", clean_value)
    clean_value = re.sub(r"\s+", " ", clean_value).strip()
    if clean_value.startswith("plenary "):
        clean_value = clean_value.removeprefix("plenary ").strip()
    return clean_value


def _normalize_org_label(value: Any) -> str:
    folded = _fold_label(value)
    if not folded:
        return ""
    if folded in _ORG_ALIAS_TO_CANONICAL:
        return _ORG_ALIAS_TO_CANONICAL[folded]
    for alias, canonical in _ORG_ALIAS_TO_CANONICAL.items():
        if len(alias) > 3 and folded.startswith(alias + " "):
            return canonical
    if folded.startswith("yahoo news "):
        return "yahoo news"
    if folded.startswith("yahoo finance "):
        return "yahoo finance"
    return folded


def _source_org(source: dict[str, Any]) -> str:
    return _normalize_org_label(source.get("source"))


def _source_text_for_precedence(source: dict[str, Any]) -> str:
    return " ".join(
        str(source.get(key) or "")
        for key in ("title", "source", "summary", "body_evidence")
    )


def _fold_source_text(source: dict[str, Any]) -> str:
    return _fold_label(_source_text_for_precedence(source))


def _contains_attribution_to_org(folded_text: str, canonical_org: str) -> bool:
    aliases = _WIRE_ORG_ALIASES.get(canonical_org, (canonical_org,))
    for alias in aliases:
        folded_alias = _fold_label(alias)
        if not folded_alias:
            continue
        for phrase in _ATTRIBUTION_BEFORE_ORG:
            folded_phrase = _fold_label(phrase)
            if re.search(
                rf"\b{re.escape(folded_phrase)}\s+(?:the\s+)?{re.escape(folded_alias)}\b",
                folded_text,
            ):
                return True
        for phrase in _ATTRIBUTION_AFTER_ORG:
            folded_phrase = _fold_label(phrase)
            if re.search(
                rf"\b(?:the\s+)?{re.escape(folded_alias)}\s+{re.escape(folded_phrase)}\b",
                folded_text,
            ):
                return True
        if re.search(rf"\bby\s+(?:the\s+)?{re.escape(folded_alias)}\b", folded_text):
            return True
    return False


def _attributed_wire_orgs(source: dict[str, Any]) -> list[str]:
    folded_text = _fold_source_text(source)
    orgs = [
        org
        for org in sorted(_PRIMARY_WIRE_ORGS)
        if _contains_attribution_to_org(folded_text, org)
    ]
    return orgs


def _has_same_org_reference(source: dict[str, Any]) -> bool:
    return bool(_SAME_ORG_REFERENCE_RE.search(_source_text_for_precedence(source)))


def _source_match_score(left_text: str, right_text: str) -> float:
    left_tokens = _tokenize_for_matching(left_text)
    right_tokens = _tokenize_for_matching(right_text)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / math.sqrt(len(left_tokens) * len(right_tokens))


def _source_local_id(source: dict[str, Any]) -> str:
    return _normalize_source_id(str(source.get("local_id") or ""))


def _source_order(source: dict[str, Any], fallback: int = 0) -> int:
    try:
        return int(source.get("citation_precedence_order"))
    except (TypeError, ValueError):
        return fallback


def _source_rank(source: dict[str, Any]) -> int:
    try:
        return int(source.get("citation_precedence_rank"))
    except (TypeError, ValueError):
        return NEUTRAL_CITATION_RANK


def _same_org_primary_id(
    derivative: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    derivative_text = _source_text_for_precedence(derivative)
    derivative_published = _parse_published_datetime(derivative.get("published"))
    ranked: list[tuple[float, int, int, int, str]] = []
    for index, candidate in enumerate(candidates):
        candidate_id = _source_local_id(candidate)
        if not candidate_id or candidate_id == _source_local_id(derivative):
            continue
        candidate_published = _parse_published_datetime(candidate.get("published"))
        older_or_equal = (
            derivative_published is not None
            and candidate_published is not None
            and candidate_published <= derivative_published
        )
        score = _source_match_score(derivative_text, _source_text_for_precedence(candidate))
        ranked.append(
            (
                score,
                1 if not candidate.get("citation_precedence_same_org_reference") else 0,
                1 if older_or_equal else 0,
                -_source_order(candidate, index),
                candidate_id,
            )
        )
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    return ranked[0][4]


def annotate_citation_precedence(citation_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate story-local citation sources with primary/derivative relationships."""

    annotated: list[dict[str, Any]] = []
    for index, source in enumerate(citation_sources):
        local_id = _source_local_id(source)
        source_org = _source_org(source)
        attributed_wire_orgs = _attributed_wire_orgs(source)
        annotated.append(
            {
                **source,
                "local_id": str(source.get("local_id") or local_id),
                "citation_precedence_order": index,
                "citation_precedence_org": source_org,
                "citation_precedence_attributed_orgs": attributed_wire_orgs,
                "citation_precedence_same_org_reference": _has_same_org_reference(source),
                "citation_precedence_derives_from": [],
                "citation_precedence_rank": NEUTRAL_CITATION_RANK,
                "citation_precedence_role": "neutral",
                "citation_precedence_reason": "",
                "citation_precedence_guidance": "",
            }
        )

    sources_by_org: dict[str, list[dict[str, Any]]] = {}
    source_by_local_id: dict[str, dict[str, Any]] = {}
    for source in annotated:
        local_id = _source_local_id(source)
        if local_id:
            source_by_local_id[local_id] = source
        org = str(source.get("citation_precedence_org") or "")
        if org:
            sources_by_org.setdefault(org, []).append(source)

    primary_source_ids: set[str] = set()
    for source in annotated:
        local_id = _source_local_id(source)
        if not local_id:
            continue
        derives_from: list[str] = []
        reasons: list[str] = []
        own_org = str(source.get("citation_precedence_org") or "")
        for attributed_org in source.get("citation_precedence_attributed_orgs") or []:
            if attributed_org == own_org:
                continue
            for candidate in sources_by_org.get(str(attributed_org), []):
                candidate_id = _source_local_id(candidate)
                if candidate_id and candidate_id != local_id and candidate_id not in derives_from:
                    derives_from.append(candidate_id)
                    reasons.append(f"wire_attribution:{attributed_org}")

        if source.get("citation_precedence_same_org_reference") and own_org:
            same_org_candidates = [
                candidate
                for candidate in sources_by_org.get(own_org, [])
                if _source_local_id(candidate) != local_id
            ]
            primary_id = _same_org_primary_id(source, same_org_candidates)
            if primary_id and primary_id not in derives_from:
                derives_from.append(primary_id)
                reasons.append("same_org_previous_report")

        if derives_from:
            source["citation_precedence_derives_from"] = derives_from
            source["citation_precedence_rank"] = DERIVATIVE_CITATION_RANK
            source["citation_precedence_role"] = "derivative"
            source["citation_precedence_reason"] = ",".join(reasons)
            primary_source_ids.update(derives_from)

    for source in annotated:
        local_id = _source_local_id(source)
        derives_from = list(source.get("citation_precedence_derives_from") or [])
        if local_id in primary_source_ids and not derives_from:
            source["citation_precedence_rank"] = PRIMARY_CITATION_RANK
            source["citation_precedence_role"] = "primary"
            source["citation_precedence_reason"] = "cited_by_derivative"
        if derives_from:
            source["citation_precedence_guidance"] = (
                f"{local_id} appears to cite {', '.join(derives_from)}; "
                f"for shared facts prefer {', '.join(derives_from)} and cite {local_id} only for unique reporting."
            )
        elif source.get("citation_precedence_role") == "primary":
            source["citation_precedence_guidance"] = (
                f"{local_id} is the preferred citation for facts also repeated by derivative sources."
            )

    return annotated


def _citation_precedence_dependency_records_from_annotated(
    citation_sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in citation_sources:
        local_id = _source_local_id(source)
        derives_from = [
            _normalize_source_id(str(source_id))
            for source_id in source.get("citation_precedence_derives_from") or []
            if _normalize_source_id(str(source_id))
        ]
        if not local_id or not derives_from:
            continue
        records.append(
            {
                "source_id": local_id,
                "derives_from": derives_from,
                "reason": source.get("citation_precedence_reason") or "",
            }
        )
    return records






def strip_citation_markers(text: str) -> str:
    """Remove temporary and final numeric citation markers while preserving prose."""

    clean_text = normalize_temporary_citation_markers(str(text or ""))
    clean_text = TEMPORARY_CITATION_RE.sub("", clean_text)
    clean_text = DISPLAY_CITATION_RE.sub("", clean_text)
    clean_text = re.sub(r"[ \t]+([,.;:!?])", r"\1", clean_text)
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    return clean_text.strip()


def _normalize_source_id(value: str) -> str:
    return re.sub(r"[\s\[\]]+", "", str(value or "")).upper()


def normalize_temporary_citation_markers(text: str) -> str:
    """Canonicalize common model variants like [[S1], [S2]] into [[S1,S2]]."""

    def replace_bracketed_list(match: re.Match[str]) -> str:
        source_ids = [
            _normalize_source_id(source_id)
            for source_id in re.findall(r"\[([A-Za-z][A-Za-z0-9_-]*)\]", match.group(1))
        ]
        source_ids = [source_id for source_id in source_ids if source_id]
        if not source_ids:
            return match.group(0)
        return "[[" + ",".join(source_ids) + "]]"

    return BRACKETED_TEMPORARY_CITATION_LIST_RE.sub(
        replace_bracketed_list,
        str(text or ""),
    )


def _marker_source_ids(text: str) -> list[str]:
    source_ids: list[str] = []
    clean_text = normalize_temporary_citation_markers(str(text or ""))
    for marker_text in TEMPORARY_CITATION_RE.findall(clean_text):
        for raw_id in re.split(r"[,;\s]+", marker_text):
            source_id = _normalize_source_id(raw_id)
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
    return source_ids


def _remove_temporary_markers(text: str) -> str:
    clean_text = normalize_temporary_citation_markers(str(text or ""))
    clean_text = TEMPORARY_CITATION_RE.sub("", clean_text)
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

    clean_text = normalize_temporary_citation_markers(str(text or ""))
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
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




def _citation_dependency_map(citation_sources: list[dict[str, Any]]) -> dict[str, list[str]]:
    dependency_map: dict[str, list[str]] = {}
    valid_source_ids = {
        _source_local_id(source)
        for source in citation_sources
        if _source_local_id(source)
    }
    for source in citation_sources:
        local_id = _source_local_id(source)
        if not local_id:
            continue
        derives_from: list[str] = []
        for raw_source_id in source.get("citation_precedence_derives_from") or []:
            source_id = _normalize_source_id(str(raw_source_id))
            if (
                source_id
                and source_id in valid_source_ids
                and source_id != local_id
                and source_id not in derives_from
            ):
                derives_from.append(source_id)
        if derives_from:
            dependency_map[local_id] = derives_from
    return dependency_map


def _sentence_overlaps_source(sentence_text: str, source: dict[str, Any]) -> bool:
    sentence_tokens = _tokenize_for_matching(sentence_text)
    source_tokens = _tokenize_for_matching(_source_text_for_precedence(source))
    if not sentence_tokens or not source_tokens:
        return False
    overlap = len(sentence_tokens & source_tokens)
    if overlap >= CITATION_PRECEDENCE_PRIMARY_OVERLAP_MIN_TOKENS:
        return True
    score = overlap / math.sqrt(len(sentence_tokens) * len(source_tokens))
    return score >= CITATION_PRECEDENCE_PRIMARY_OVERLAP_MIN_SCORE


def _should_replace_derivative_citation(
    sentence_text: str,
    primary_ids: list[str],
    source_by_local_id: dict[str, dict[str, Any]],
) -> bool:
    return any(
        _sentence_overlaps_source(sentence_text, source_by_local_id[source_id])
        for source_id in primary_ids
        if source_id in source_by_local_id
    )


def _has_unique_derivative_support(
    sentence_text: str,
    derivative_source: dict[str, Any],
    primary_ids: list[str],
    source_by_local_id: dict[str, dict[str, Any]],
) -> bool:
    sentence_tokens = _tokenize_for_matching(sentence_text)
    derivative_tokens = _tokenize_for_matching(_source_text_for_precedence(derivative_source))
    primary_tokens: set[str] = set()
    for primary_id in primary_ids:
        primary_source = source_by_local_id.get(primary_id)
        if primary_source:
            primary_tokens.update(_tokenize_for_matching(_source_text_for_precedence(primary_source)))
    unique_tokens = (sentence_tokens & derivative_tokens) - primary_tokens
    return len(unique_tokens) >= CITATION_PRECEDENCE_PRIMARY_OVERLAP_MIN_TOKENS


def apply_citation_precedence(
    cited_sentences: list[dict[str, Any]],
    citation_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prefer primary story-local sources over derivative citations where possible."""

    annotated_sources = annotate_citation_precedence(citation_sources)
    source_by_local_id = {
        _source_local_id(source): source
        for source in annotated_sources
        if _source_local_id(source)
    }
    dependency_map = _citation_dependency_map(annotated_sources)
    dependency_records = _citation_precedence_dependency_records_from_annotated(annotated_sources)
    adjusted_sentences: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    suppressions: list[dict[str, Any]] = []
    retained_derivatives: list[dict[str, Any]] = []

    for sentence_index, sentence in enumerate(cited_sentences):
        original_source_ids = _normalized_sentence_source_ids(sentence)
        original_source_id_set = set(original_source_ids)
        adjusted_source_ids: list[str] = []
        sentence_text = str(sentence.get("text") or "")
        for source_id in original_source_ids:
            primary_ids = dependency_map.get(source_id) or []
            if not primary_ids:
                if source_id not in adjusted_source_ids:
                    adjusted_source_ids.append(source_id)
                continue

            primary_ids_in_story = [
                primary_id
                for primary_id in primary_ids
                if primary_id in source_by_local_id
            ]
            if not primary_ids_in_story:
                if source_id not in adjusted_source_ids:
                    adjusted_source_ids.append(source_id)
                continue

            primary_already_cited = any(
                primary_id in original_source_id_set
                for primary_id in primary_ids_in_story
            )
            should_replace = primary_already_cited or _should_replace_derivative_citation(
                sentence_text,
                primary_ids_in_story,
                source_by_local_id,
            )
            if should_replace:
                has_unique_derivative_support = _has_unique_derivative_support(
                    sentence_text,
                    source_by_local_id.get(source_id) or {},
                    primary_ids_in_story,
                    source_by_local_id,
                )
                for primary_id in primary_ids_in_story:
                    if primary_id not in adjusted_source_ids:
                        adjusted_source_ids.append(primary_id)
                if has_unique_derivative_support:
                    if source_id not in adjusted_source_ids:
                        adjusted_source_ids.append(source_id)
                    retained_derivatives.append(
                        {
                            "sentence_index": sentence_index,
                            "source_id": source_id,
                            "preferred_source_ids": primary_ids_in_story,
                            "reason": "unique_derivative_support",
                            "text": sentence_text[:220],
                        }
                    )
                    continue
                event = {
                    "sentence_index": sentence_index,
                    "source_id": source_id,
                    "preferred_source_ids": primary_ids_in_story,
                    "text": sentence_text[:220],
                }
                if primary_already_cited:
                    suppressions.append(event)
                else:
                    replacements.append(event)
                continue

            if source_id not in adjusted_source_ids:
                adjusted_source_ids.append(source_id)
            retained_derivatives.append(
                {
                    "sentence_index": sentence_index,
                    "source_id": source_id,
                    "preferred_source_ids": primary_ids_in_story,
                    "text": sentence_text[:220],
                }
            )

        adjusted_sentences.append(
            {
                **sentence,
                "source_ids": adjusted_source_ids,
                "citation_precedence_original_source_ids": original_source_ids,
            }
        )

    return {
        "cited_sentences": adjusted_sentences,
        "citation_sources": annotated_sources,
        "diagnostics": {
            "citation_precedence_dependencies": dependency_records,
            "citation_precedence_replacements": replacements,
            "citation_precedence_suppressions": suppressions,
            "citation_precedence_retained_derivatives": retained_derivatives,
            "citation_precedence_replacement_count": len(replacements),
            "citation_precedence_suppression_count": len(suppressions),
            "citation_precedence_retained_derivative_count": len(retained_derivatives),
        },
    }


def validate_cited_story_text(
    marked_text: str,
    citation_sources: list[dict[str, Any]],
    *,
    apply_precedence: bool = True,
) -> dict[str, Any]:
    """Return clean story text plus validated sentence-to-source metadata."""

    original_marked_text = str(marked_text or "")
    repaired_marker_variant_count = len(
        BRACKETED_TEMPORARY_CITATION_LIST_RE.findall(original_marked_text)
    )
    marked_text = normalize_temporary_citation_markers(original_marked_text)
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

    precedence_diagnostics: dict[str, Any] = {}
    if apply_precedence:
        precedence_result = apply_citation_precedence(cited_sentences, citation_sources)
        cited_sentences = list(precedence_result.get("cited_sentences") or cited_sentences)
        precedence_diagnostics = dict(precedence_result.get("diagnostics") or {})

    clean_paragraph = " ".join(sentence["text"] for sentence in cited_sentences).strip()
    return {
        "paragraph": clean_paragraph,
        "cited_sentences": cited_sentences,
        "diagnostics": {
            "sentence_count": len(cited_sentences),
            "temporary_marker_count": len(TEMPORARY_CITATION_RE.findall(marked_text or "")),
            "repaired_marker_variant_count": repaired_marker_variant_count,
            "malformed_marker_count": (marked_text or "").count("[[")
            - len(TEMPORARY_CITATION_RE.findall(marked_text or "")),
            "unknown_source_ids": sorted(set(unknown_source_ids)),
            "uncited_sentence_count": sum(1 for s in cited_sentences if not s["source_ids"]),
            "source_count": len(citation_sources),
            **precedence_diagnostics,
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




def _normalized_sentence_source_ids(sentence: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    for source_id in sentence.get("source_ids") or []:
        normalized_source_id = _normalize_source_id(str(source_id))
        if normalized_source_id and normalized_source_id not in source_ids:
            source_ids.append(normalized_source_id)
    return source_ids


def _precedence_ordered_source_ids(
    source_ids: list[str],
    source_by_local_id: dict[str, dict[str, Any]],
    *,
    first_seen_order: list[str] | None = None,
) -> list[str]:
    first_seen_index = {
        source_id: index
        for index, source_id in enumerate(first_seen_order or [])
    }

    def rank(source_id: str) -> tuple[int, int, int, str]:
        source = source_by_local_id.get(source_id) or {}
        return (
            _source_rank(source),
            first_seen_index.get(source_id, len(first_seen_index) + _source_order(source)),
            _source_order(source),
            source_id,
        )

    ordered: list[str] = []
    for source_id in sorted(source_ids, key=rank):
        if source_id in source_by_local_id and source_id not in ordered:
            ordered.append(source_id)
    return ordered


def render_cited_story(
    cited_sentences: list[dict[str, Any]],
    citation_sources: list[dict[str, Any]],
    registry: CitationRegistry,
    *,
    story_level_citation_sentence_threshold: int | None = DEFAULT_STORY_LEVEL_CITATION_SENTENCE_THRESHOLD,
    apply_precedence: bool = True,
) -> dict[str, Any]:
    precedence_diagnostics: dict[str, Any] = {}
    if apply_precedence:
        precedence_result = apply_citation_precedence(cited_sentences, citation_sources)
        cited_sentences = list(precedence_result.get("cited_sentences") or cited_sentences)
        citation_sources = list(precedence_result.get("citation_sources") or citation_sources)
        precedence_diagnostics = dict(precedence_result.get("diagnostics") or {})
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
        story_level_source_ids = _precedence_ordered_source_ids(
            story_level_source_ids,
            source_by_local_id,
            first_seen_order=source_first_seen_order,
        )
    story_level_source_id_set = set(story_level_source_ids)
    story_citation_numbers: list[int] = []

    def add_story_citation_number(number: int) -> None:
        if number > 0 and number not in story_citation_numbers:
            story_citation_numbers.append(number)

    rendered_source_ids: list[str] = []
    for source_id in story_level_source_ids:
        if source_id not in rendered_source_ids:
            rendered_source_ids.append(source_id)
    for sentence in cited_sentences:
        for source_id in _normalized_sentence_source_ids(sentence):
            if source_id in source_by_local_id and source_id not in rendered_source_ids:
                rendered_source_ids.append(source_id)
    for source_id in _precedence_ordered_source_ids(
        rendered_source_ids,
        source_by_local_id,
        first_seen_order=source_first_seen_order,
    ):
        registry.register(source_by_local_id[source_id])

    story_level_numbers: list[int] = []
    for source_id in story_level_source_ids:
        source = source_by_local_id.get(source_id)
        if not source:
            continue
        number = registry.register(source)
        if number not in story_level_numbers:
            story_level_numbers.append(number)
        add_story_citation_number(number)

    rendered_sentences: list[str] = []
    for sentence in cited_sentences:
        sentence_text = strip_citation_markers(str(sentence.get("text") or ""))
        numbers: list[int] = []
        sentence_source_ids = _precedence_ordered_source_ids(
            _normalized_sentence_source_ids(sentence),
            source_by_local_id,
            first_seen_order=source_first_seen_order,
        )
        for source_id in sentence_source_ids:
            if source_id in story_level_source_id_set:
                continue
            source = source_by_local_id.get(source_id)
            if not source:
                continue
            number = registry.register(source)
            if number not in numbers:
                numbers.append(number)
                add_story_citation_number(number)
        if numbers:
            sentence_text = f"{sentence_text}{''.join(f'[{number}]' for number in numbers)}"
        if sentence_text:
            rendered_sentences.append(sentence_text)
    return {
        "paragraph": " ".join(rendered_sentences).strip(),
        "headline_citation_text": "".join(f"[{number}]" for number in story_level_numbers),
        "story_citation_numbers": story_citation_numbers,
        "story_level_source_numbers": story_level_numbers,
        "story_level_source_ids": story_level_source_ids,
        "story_level_source_sentence_counts": {
            source_id: source_sentence_counts[source_id]
            for source_id in story_level_source_ids
            if source_id in source_sentence_counts
        },
        "citation_precedence_diagnostics": precedence_diagnostics,
    }


def _citation_source_number(source: dict[str, Any]) -> int:
    try:
        number = int(source.get("number") or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _source_by_citation_number(citation_sources: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    sources_by_number: dict[int, dict[str, Any]] = {}
    for source in citation_sources:
        number = _citation_source_number(source)
        if number > 0:
            sources_by_number[number] = source
    return sources_by_number


def _citation_group_title(group: dict[str, Any], index: int) -> str:
    for key in ("story_headline", "story", "title", "label"):
        title = strip_citation_markers(str(group.get(key) or ""))
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title
    return f"Story {index + 1}"


def _citation_group_numbers(group: dict[str, Any]) -> list[int]:
    raw_numbers = (
        group.get("citation_numbers")
        or group.get("source_numbers")
        or group.get("numbers")
        or group.get("sources")
        or []
    )
    numbers: list[int] = []
    for raw_number in raw_numbers:
        if isinstance(raw_number, dict):
            raw_number = raw_number.get("number") or raw_number.get("citation_number")
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in numbers:
            numbers.append(number)
    return numbers


def _render_plain_text_source(source: dict[str, Any]) -> str:
    number = _citation_source_number(source)
    if number <= 0:
        return ""
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
    return line


def _render_plain_text_source_section(title: str, sources: list[dict[str, Any]]) -> str:
    source_lines = [
        rendered
        for rendered in (_render_plain_text_source(source) for source in sources)
        if rendered
    ]
    if not source_lines:
        return ""
    clean_title = title.strip() or "Sources"
    return f"{clean_title}\n{'-' * len(clean_title)}\n\n" + "\n\n".join(source_lines)


def _grouped_citation_sources(
    citation_sources: list[dict[str, Any]],
    citation_groups: list[dict[str, Any]] | None,
) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    if not citation_groups:
        return [], []
    sources_by_number = _source_by_citation_number(citation_sources)
    sections: list[tuple[str, list[dict[str, Any]]]] = []
    captured_numbers: set[int] = set()
    for index, group in enumerate(citation_groups):
        group_sources: list[dict[str, Any]] = []
        for number in _citation_group_numbers(group):
            source = sources_by_number.get(number)
            if not source:
                continue
            if number not in captured_numbers:
                captured_numbers.add(number)
            group_sources.append(source)
        if group_sources:
            sections.append((_citation_group_title(group, index), group_sources))
    additional_sources = [
        source
        for source in citation_sources
        if _citation_source_number(source) not in captured_numbers
    ]
    return sections, additional_sources


def render_plain_text_sources(
    citation_sources: list[dict[str, Any]],
    citation_groups: list[dict[str, Any]] | None = None,
) -> str:
    grouped_sections, additional_sources = _grouped_citation_sources(
        citation_sources,
        citation_groups,
    )
    if grouped_sections:
        sections = [
            section
            for section in (
                _render_plain_text_source_section(title, sources)
                for title, sources in grouped_sections
            )
            if section
        ]
        additional_section = _render_plain_text_source_section(
            "Additional Sources",
            additional_sources,
        )
        if additional_section:
            sections.append(additional_section)
        if sections:
            return "\n\n".join(sections)

    lines: list[str] = []
    for source in citation_sources:
        rendered_source = _render_plain_text_source(source)
        if rendered_source:
            lines.append(rendered_source)
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


def _render_html_source_item(
    source: dict[str, Any],
    *,
    include_id: bool = True,
    show_number: bool = False,
) -> str:
    number = _citation_source_number(source)
    if number <= 0:
        return ""
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
    id_html = f" id=\"source-{number}\"" if include_id else ""
    number_html = (
        f"<span style=\"font-weight:700; color:#111827;\">[{number}]</span> "
        if show_number
        else ""
    )
    return (
        f"<li{id_html} style=\"margin:0 0 6px; padding-left:2px; font-size:13px; line-height:1.35; color:#374151;\">"
        f"{number_html}{title_html}{detail_html}</li>"
    )


def _render_html_source_section(
    title: str,
    sources: list[dict[str, Any]],
    anchored_numbers: set[int],
) -> str:
    items: list[str] = []
    for source in sources:
        number = _citation_source_number(source)
        include_id = number not in anchored_numbers
        rendered_item = _render_html_source_item(source, include_id=include_id, show_number=True)
        if rendered_item:
            items.append(rendered_item)
            if include_id:
                anchored_numbers.add(number)
    if not items:
        return ""
    clean_title = html.escape(title.strip() or "Sources")
    return (
        "<div style=\"margin:0 0 18px;\">"
        f"<h3 style=\"margin:0 0 8px; font-size:15px; line-height:1.35; font-weight:700; color:#111827;\">{clean_title}</h3>"
        "<ul style=\"margin:0; padding-left:0; list-style:none;\">"
        f"{''.join(items)}"
        "</ul>"
        "</div>"
    )


def render_html_sources(
    citation_sources: list[dict[str, Any]],
    citation_groups: list[dict[str, Any]] | None = None,
) -> str:
    grouped_sections, additional_sources = _grouped_citation_sources(
        citation_sources,
        citation_groups,
    )
    if grouped_sections:
        anchored_numbers: set[int] = set()
        sections = [
            section
            for section in (
                _render_html_source_section(title, sources, anchored_numbers)
                for title, sources in grouped_sections
            )
            if section
        ]
        additional_section = _render_html_source_section(
            "Additional Sources",
            additional_sources,
            anchored_numbers,
        )
        if additional_section:
            sections.append(additional_section)
        if sections:
            return "".join(sections)

    items: list[str] = []
    for source in citation_sources:
        rendered_item = _render_html_source_item(source)
        if rendered_item:
            items.append(rendered_item)
    if not items:
        return "<p style=\"margin:0; font-size:13px; line-height:1.3; color:#6b7280;\">No citation sources available.</p>"
    return "<ol style=\"margin:0; padding-left:20px;\">" + "".join(items) + "</ol>"
