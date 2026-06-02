"""Draft story paragraphs from article summaries inside retained clusters."""

from __future__ import annotations

import re
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from . import citations as citations_stage
from .text_cleaning import clean_article_text
from .topic_context import build_topic_context


ARTICLE_BODY_EVIDENCE_MAX_CHARS = 2000
MIN_STORY_DRAFT_WORD_COUNT = 50


@dataclass(frozen=True)
class StoryDraftingRuntime:
    article_summary_concurrency: int
    final_synthesis_max_tokens: int
    model_reference: str
    model_name: str
    model_backend: str
    min_articles_per_story: int
    build_chat_model: Callable[..., Any]
    invoke_with_retries: Callable[..., Any]
    estimate_message_token_count: Callable[[BaseMessage], int]
    extract_prompt_tokens_from_response: Callable[[Any], int | None]
    strip_prompt_echo_lines: Callable[[str], str]
    strip_model_artifacts: Callable[[str], str]
    is_low_coverage_synthesis_section: Callable[[str], bool]
    dev_synthesis_paragraph_from_summaries: Callable[[list[str]], str]
    final_synthesis_word_count: Callable[[str], int]


def report_summary_text(entry: str) -> str:
    summary_match = re.search(r"Summary:\s*(.*)", entry or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", summary_match.group(1).strip()) if summary_match else ""


def report_article_id(entry: str) -> str:
    article_id_match = re.search(r"^- Article ID:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return article_id_match.group(1).strip() if article_id_match else ""


def article_summary_lookup_by_id(final_reports: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for entry in final_reports:
        article_id = report_article_id(entry)
        if article_id:
            lookup[article_id] = entry
    return lookup


def _article_lookup_by_id(article_targets: list[dict] | None) -> dict[str, dict]:
    return {
        str(article.get("article_id") or ""): article
        for article in article_targets or []
        if article.get("article_id")
    }


def _article_body_evidence(article: dict | None) -> str:
    if not article:
        return ""
    clean_body = clean_article_text(
        article.get("text"),
        source=article.get("source"),
        url=article.get("url"),
        title=article.get("title"),
    )
    if len(clean_body) <= ARTICLE_BODY_EVIDENCE_MAX_CHARS:
        return clean_body
    return clean_body[:ARTICLE_BODY_EVIDENCE_MAX_CHARS].rsplit(" ", 1)[0].strip()


def _annotated_citation_sources(citation_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return citations_stage.annotate_citation_precedence(list(citation_sources or []))


def _story_topic_context(story: dict, fallback_title: str) -> str:
    topic_context = str(story.get("topic_context") or "").strip()
    if topic_context:
        return topic_context
    topic_definition = story.get("topic_definition")
    if isinstance(topic_definition, dict):
        return build_topic_context(topic_definition, fallback_title=fallback_title)
    return build_topic_context(None, fallback_title=fallback_title)


def story_summary_blocks_from_clusters(
    story_records: list[dict],
    article_summary_lookup: dict[str, str],
    article_lookup: dict[str, dict] | None = None,
    *,
    min_articles_per_story: int,
) -> list[dict[str, Any]]:
    story_blocks: list[dict[str, Any]] = []
    for story_index, story in enumerate(story_records):
        summaries: list[str] = []
        article_ids: list[str] = []
        citation_sources: list[dict[str, Any]] = []
        topic_title = str(story.get("topic_title") or "Unclassified News")
        fallback_topic_context = build_topic_context(None, fallback_title=topic_title)
        topic_context = _story_topic_context(story, topic_title)
        for article_id in story.get("cluster_article_ids") or story.get("article_ids") or []:
            clean_article_id = str(article_id or "").strip()
            entry = article_summary_lookup.get(clean_article_id)
            if not entry:
                continue
            source_record = citations_stage.parse_article_report_entry(entry)
            summary_text = str(source_record.get("summary") or report_summary_text(entry)).strip()
            if not summary_text:
                continue
            source_article = (article_lookup or {}).get(clean_article_id)
            source_topic_context = str((source_article or {}).get("topic_context") or "").strip()
            if source_topic_context and topic_context == fallback_topic_context:
                topic_context = source_topic_context
            summaries.append(summary_text)
            article_ids.append(clean_article_id)
            citation_sources.append(
                {
                    **source_record,
                    "local_id": f"S{len(citation_sources) + 1}",
                    "article_id": clean_article_id or source_record.get("article_id", ""),
                    "summary": summary_text,
                    "body_evidence": _article_body_evidence(
                        source_article
                    ),
                }
            )
        if len(summaries) < min_articles_per_story:
            continue
        citation_sources = _annotated_citation_sources(citation_sources)
        story_blocks.append(
            {
                "topic_key": story.get("topic_key") or "",
                "topic_title": topic_title,
                "topic_context": topic_context,
                "story_title": story.get("story_title") or story.get("title") or "News update",
                "story_key": story.get("story_key") or f"global-story-{story_index + 1:02d}",
                "summaries": summaries,
                "article_ids": article_ids,
                "citation_sources": citation_sources,
                "article_count": len(article_ids),
                "cluster_article_count": story.get("cluster_article_count") or story.get("article_count"),
                "source_count": story.get("source_count"),
                "average_similarity": story.get("average_similarity"),
                "connectedness_score": story.get("connectedness_score"),
                "story_strength_score": story.get("story_strength_score"),
                "edge_density": story.get("edge_density"),
                "story_rank": story_index,
            }
        )
    return story_blocks


def build_story_synthesis_prompt_messages(story_block: dict[str, Any], now_label: str) -> list[BaseMessage]:
    topic_title = str(story_block.get("topic_title") or "Unknown topic")
    topic_context = str(story_block.get("topic_context") or "").strip()
    if not topic_context:
        topic_context = build_topic_context(None, fallback_title=topic_title)
    story_title = str(story_block.get("story_title") or "Story update")
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    citation_sources = [
        source
        for source in _annotated_citation_sources(list(story_block.get("citation_sources") or []))
        if str(source.get("summary") or "").strip()
    ]
    if citation_sources:
        source_summary_lines = "\n\n".join(
            textwrap.dedent(f"""
                {source.get("local_id")}:
                Title: {source.get("title") or "Untitled article"}
                Article ID: {source.get("article_id") or "N/A"}
                Source: {source.get("source") or "Unknown source"}
                Published: {source.get("published") or "Unknown publish time"}
                URL: {source.get("url") or "N/A"}
                Summary: {source.get("summary")}
                Cleaned article evidence to paraphrase, not quote: {source.get("body_evidence") or "N/A"}
                Citation precedence: {source.get("citation_precedence_guidance") or "Cite this source only for facts it directly supports."}
            """).strip()
            for source in citation_sources
        )
    else:
        source_summary_lines = "\n".join(
            f"S{index}: {summary}"
            for index, summary in enumerate(summaries, start=1)
        )
    system_prompt = SystemMessage(content=textwrap.dedent(f"""
        Today: {now_label}.
        You are synthesizing prewritten article summaries and cleaned article evidence into one newsletter story.
        Use only the supplied source summaries and cleaned article evidence.
        Write one custom story headline, then one cohesive main story paragraph, roughly 70-130 words.
        The headline should be factual, specific, 4-10 words, and not copied wholesale from a source headline.
        End every factual sentence with one or more source markers using the listed source IDs,
        like [[S1]] or one combined marker for multiple sources like [[S1,S3]].
        Use only listed source IDs and do not invent sources.
        In the main story, try to support important claims with concrete evidence details from the
        cleaned article evidence when it is available. Paraphrase those details in your own words;
        do not quote article text, copy distinctive article wording, or use quotation marks around
        article-body phrasing. Cite the source IDs for the article or articles that supply each
        paraphrased evidence detail.
        If a source says it appears to cite another listed source, prefer the listed primary source
        for shared facts and cite the derivative source only for unique reporting or analysis.
        Use the provided Topic context to prioritize the headline, lede, and details most relevant
        to this topic, but include major concrete developments even if they complicate the topic
        framing; do not invent topic relevance.
        Lead with today's reported development. Include concrete reported claims, named actors,
        places, timing, figures, damage, statements, deadlines, and uncertainty when supported.
        Then assess whether the sources directly or materially contradict each other.
        A reportable contradiction is a factual disagreement about the same claim, count,
        timeline, attribution, status, quote, or outcome where the cited accounts cannot
        both be true in the same context. Do not require identical wording.
        Omission, different focus, routine updates over time, or one source addressing a
        topic another source does not address is not a contradiction.
        If there is no direct or material factual contradiction, write exactly 'NONE' for Contradictions.
        If there is a contradiction, write 1-3 concise prose sentences under Contradictions.
        Each contradiction sentence must cite the disagreeing sources and must use the cleaned article evidence,
        not only the source summaries.
        Do not write bullets, source-material notes, methodology, bibliography, or preamble.
        Do not merge in background material unless a source summary reports it as part of today's update.
    """).strip())
    user_prompt = HumanMessage(content=textwrap.dedent(f"""
        Topic: {topic_title}
        Topic context:
        {topic_context}

        Story: {story_title}

        Source summaries and cleaned article evidence to paraphrase, not quote:
        {source_summary_lines}

        Return exactly this format:
        Headline: <custom story headline>
        Main story: <story paragraph with sentence-end source markers>
        Contradictions: NONE

        Or, only if there is a real direct or material contradiction:
        Headline: <custom story headline>
        Main story: <story paragraph with sentence-end source markers>
        Contradictions: <short contradiction evidence paragraph with sentence-end source markers>
    """).strip())
    return [system_prompt, user_prompt]


def clean_story_synthesis_headline(
    raw_text: str,
    fallback: str,
    runtime: StoryDraftingRuntime,
) -> str:
    clean_text = runtime.strip_prompt_echo_lines(runtime.strip_model_artifacts(raw_text or ""))
    clean_text = re.sub(r"(?m)^##+\s*", "", clean_text)
    clean_text = re.sub(r"(?mi)^\s*(?:story\s+headline|headline)\s*:\s*", "", clean_text)
    clean_text = re.sub(r"\[\[[^\]]+\]\]", "", clean_text)
    clean_text = re.sub(r"\[[0-9,\s]+\]", "", clean_text)
    clean_text = re.sub(r"[\r\n]+", " ", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip(" \"'")
    clean_text = re.sub(r"\.+$", "", clean_text).strip()
    if not clean_text:
        clean_text = str(fallback or "Story update").strip()
    words = clean_text.split()
    if len(words) > 12:
        clean_text = " ".join(words[:12])
    return clean_text[:110].strip() or "Story update"


def clean_story_synthesis_paragraph(
    raw_text: str,
    summaries: list[str],
    runtime: StoryDraftingRuntime,
) -> str:
    clean_text = runtime.strip_prompt_echo_lines(runtime.strip_model_artifacts(raw_text or ""))
    clean_text = re.sub(r"(?m)^##+\s+.*$", "", clean_text)
    clean_text = re.sub(r"(?mi)^\s*(?:story\s+headline|headline)\s*:\s*.*$", "", clean_text)
    clean_text = re.sub(r"(?mi)^\s*paragraph\s*:\s*", "", clean_text)
    clean_text = re.sub(r"(?mi)^(topic|story|source summaries?|sources?|references?)\s*:\s*.*$", "", clean_text)
    clean_text = re.sub(r"(?m)^\s*[-*]\s+", "", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    if clean_text and not runtime.is_low_coverage_synthesis_section(clean_text):
        return clean_text
    return runtime.dev_synthesis_paragraph_from_summaries(summaries)


def clean_story_synthesis_contradictions(
    raw_text: str,
    runtime: StoryDraftingRuntime,
) -> str:
    clean_text = runtime.strip_prompt_echo_lines(runtime.strip_model_artifacts(raw_text or ""))
    clean_text = re.sub(r"(?m)^##+\s+.*$", "", clean_text)
    clean_text = re.sub(r"(?mi)^\s*contradictions?\s*:\s*", "", clean_text)
    clean_text = re.sub(r"(?mi)^(topic|story|source summaries?|sources?|references?)\s*:\s*.*$", "", clean_text)
    clean_text = re.sub(r"(?m)^\s*[-*]\s+", "", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    normalized = re.sub(r"[^a-z]+", " ", clean_text.lower()).strip()
    no_contradiction_phrases = {
        "",
        "none",
        "n a",
        "no contradiction",
        "no contradictions",
        "no direct contradiction",
        "no direct contradictions",
        "not applicable",
    }
    if normalized in no_contradiction_phrases:
        return ""
    if normalized.startswith("none "):
        return ""
    if clean_text and not runtime.is_low_coverage_synthesis_section(clean_text):
        return clean_text
    return ""


def parse_story_synthesis_output(
    raw_text: str,
    summaries: list[str],
    fallback_headline: str,
    runtime: StoryDraftingRuntime,
) -> tuple[str, str, str]:
    clean_text = runtime.strip_prompt_echo_lines(runtime.strip_model_artifacts(raw_text or ""))
    headline_match = re.search(
        r"(?mi)^\s*(?:story\s+headline|headline)\s*:\s*(.+)$",
        clean_text,
    )
    markdown_heading_match = re.search(r"(?m)^##+\s+(.+)$", clean_text)
    headline = (
        headline_match.group(1).strip()
        if headline_match
        else markdown_heading_match.group(1).strip()
        if markdown_heading_match
        else ""
    )

    main_story_match = re.search(
        r"(?mis)^\s*(?:main\s+story|paragraph)\s*:\s*(.*?)(?=^\s*contradictions?\s*:|\Z)",
        clean_text,
    )
    paragraph_source = main_story_match.group(1).strip() if main_story_match else clean_text

    contradictions_match = re.search(
        r"(?mis)^\s*contradictions?\s*:\s*(.+)$",
        clean_text,
    )
    contradictions_source = contradictions_match.group(1).strip() if contradictions_match else ""
    return (
        clean_story_synthesis_headline(headline, fallback_headline, runtime),
        clean_story_synthesis_paragraph(paragraph_source, summaries, runtime),
        clean_story_synthesis_contradictions(contradictions_source, runtime),
    )


def _citation_diagnostics_with_presence(citation_result: dict[str, Any]) -> dict[str, Any]:
    cited_sentences = list(citation_result.get("cited_sentences") or [])
    validated_citation_sentence_count = sum(
        1 for sentence in cited_sentences if sentence.get("source_ids")
    )
    diagnostics = dict(citation_result.get("diagnostics") or {})
    diagnostics["validated_citation_sentence_count"] = validated_citation_sentence_count
    diagnostics["has_validated_citation"] = validated_citation_sentence_count > 0
    return diagnostics


def _distinct_cited_source_ids(cited_sentences: list[dict[str, Any]]) -> list[str]:
    source_ids: list[str] = []
    for sentence in cited_sentences:
        for source_id in sentence.get("source_ids") or []:
            clean_source_id = re.sub(r"\s+", "", str(source_id or "")).upper()
            if clean_source_id and clean_source_id not in source_ids:
                source_ids.append(clean_source_id)
    return source_ids


def contradiction_presence_diagnostics(draft: dict[str, Any]) -> dict[str, Any]:
    marked_contradictions = str(draft.get("marked_contradictions") or "").strip()
    contradictions_paragraph = str(draft.get("contradictions_paragraph") or "").strip()
    cited_sentences = list(draft.get("contradiction_cited_sentences") or [])
    source_ids = _distinct_cited_source_ids(cited_sentences)
    citation_diagnostics = dict(draft.get("contradiction_citation_diagnostics") or {})
    return {
        "story_key": draft.get("story_key"),
        "story_title": draft.get("story_title"),
        "topic_key": draft.get("topic_key"),
        "topic_title": draft.get("topic_title"),
        "article_count": draft.get("article_count"),
        "source_count": draft.get("source_count"),
        "raw_contradiction_detected": bool(marked_contradictions),
        "validated_contradiction_detected": bool(contradictions_paragraph),
        "render_eligible": bool(contradictions_paragraph and cited_sentences and len(source_ids) >= 2),
        "contradiction_sentence_count": len(cited_sentences),
        "contradiction_source_count": len(source_ids),
        "contradiction_source_ids": source_ids,
        "citation_diagnostics": citation_diagnostics,
        "raw_preview": marked_contradictions[:280],
        "validated_preview": contradictions_paragraph[:280],
    }


def summarize_contradiction_analytics(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = [contradiction_presence_diagnostics(draft) for draft in drafts]
    raw_count = sum(1 for item in diagnostics if item["raw_contradiction_detected"])
    validated_count = sum(1 for item in diagnostics if item["validated_contradiction_detected"])
    render_eligible_count = sum(1 for item in diagnostics if item["render_eligible"])
    invalid_citation_count = sum(
        1
        for item in diagnostics
        if item["raw_contradiction_detected"]
        and not item["validated_contradiction_detected"]
    )
    single_source_count = sum(
        1
        for item in diagnostics
        if item["validated_contradiction_detected"]
        and not item["render_eligible"]
    )
    return {
        "stories_checked": len(drafts),
        "raw_contradiction_count": raw_count,
        "validated_contradiction_count": validated_count,
        "render_eligible_contradiction_count": render_eligible_count,
        "raw_contradictions_rejected_by_citation_validation": invalid_citation_count,
        "validated_contradictions_not_render_eligible": single_source_count,
        "diagnostics": diagnostics,
        "raw_contradiction_examples": [
            item for item in diagnostics if item["raw_contradiction_detected"]
        ][:10],
        "validated_contradiction_examples": [
            item for item in diagnostics if item["validated_contradiction_detected"]
        ][:10],
    }


def run_story_synthesis_block(
    story_block: dict[str, Any],
    now_label: str,
    runtime: StoryDraftingRuntime,
) -> dict[str, Any]:
    story_block = {
        **story_block,
        "citation_sources": _annotated_citation_sources(
            list(story_block.get("citation_sources") or [])
        ),
    }
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    fallback_headline = clean_story_synthesis_headline(
        str(story_block.get("story_title") or ""),
        "Story update",
        runtime,
    )
    fallback_paragraph = runtime.dev_synthesis_paragraph_from_summaries(summaries)
    prompt_messages = build_story_synthesis_prompt_messages(story_block, now_label)
    estimated_input_tokens = sum(runtime.estimate_message_token_count(message) for message in prompt_messages)
    response = runtime.invoke_with_retries(
        runtime.build_chat_model(
            max_tokens=max(450, min(1100, runtime.final_synthesis_max_tokens)),
            task="final_synthesis",
        ),
        prompt_messages,
        task_name=f"story synthesis for {story_block.get('story_title') or 'story'}",
        fallback_content=(
            f"Headline: {fallback_headline}\n"
            f"Main story: {fallback_paragraph}\n"
            "Contradictions: NONE"
        ),
    )
    story_headline, marked_paragraph, marked_contradictions = parse_story_synthesis_output(
        response.content,
        summaries,
        fallback_headline,
        runtime,
    )
    citation_result = citations_stage.validate_cited_story_text(
        marked_paragraph,
        list(story_block.get("citation_sources") or []),
    )
    contradiction_citation_result = citations_stage.validate_cited_story_text(
        marked_contradictions,
        list(story_block.get("citation_sources") or []),
        apply_precedence=False,
    ) if marked_contradictions else {"paragraph": "", "cited_sentences": [], "diagnostics": {}}
    citation_diagnostics = _citation_diagnostics_with_presence(citation_result)
    paragraph = str(citation_result.get("paragraph") or "").strip()
    contradictions_paragraph = str(contradiction_citation_result.get("paragraph") or "").strip()
    story_preview = "\n\n".join(
        part
        for part in (
            paragraph,
            f"Contradictions: {contradictions_paragraph}" if contradictions_paragraph else "",
        )
        if part
    )
    prompt_tokens = runtime.extract_prompt_tokens_from_response(response)
    word_count = runtime.final_synthesis_word_count(story_preview)
    valid = bool(paragraph) and word_count >= MIN_STORY_DRAFT_WORD_COUNT
    reason = (
        "accepted"
        if valid
        else "below_story_word_count_floor"
        if paragraph
        else "empty after cleanup"
    )
    return {
        **story_block,
        "story_headline": story_headline,
        "paragraph": paragraph,
        "main_story_paragraph": paragraph,
        "contradictions_paragraph": contradictions_paragraph,
        "marked_paragraph": marked_paragraph,
        "marked_main_story": marked_paragraph,
        "marked_contradictions": marked_contradictions,
        "cited_sentences": citation_result.get("cited_sentences", []),
        "contradiction_cited_sentences": contradiction_citation_result.get("cited_sentences", []),
        "citation_diagnostics": citation_diagnostics,
        "contradiction_citation_diagnostics": contradiction_citation_result.get("diagnostics", {}),
        "estimated_input_tokens": estimated_input_tokens,
        "actual_prompt_tokens": prompt_tokens,
        "word_count": word_count,
        "min_word_count": MIN_STORY_DRAFT_WORD_COUNT,
        "valid": valid,
        "reason": reason,
        "preview": story_preview[:500],
    }


def run_story_synthesis_blocks(
    story_blocks: list[dict[str, Any]],
    now_label: str,
    runtime: StoryDraftingRuntime,
) -> list[dict[str, Any]]:
    if runtime.article_summary_concurrency > 1 and len(story_blocks) > 1:
        ordered_results: list[tuple[int, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=runtime.article_summary_concurrency) as executor:
            future_map = {
                executor.submit(run_story_synthesis_block, story_block, now_label, runtime): index
                for index, story_block in enumerate(story_blocks)
            }
            for future in as_completed(future_map):
                ordered_results.append((future_map[future], future.result()))
        return [
            result
            for _, result in sorted(ordered_results, key=lambda item: item[0])
        ]
    return [run_story_synthesis_block(story_block, now_label, runtime) for story_block in story_blocks]


def draft_story_clusters_from_article_summaries(
    story_records: list[dict],
    article_summary_reports: list[str],
    runtime: StoryDraftingRuntime,
    article_targets: list[dict] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_lookup = article_summary_lookup_by_id(article_summary_reports)
    story_blocks = story_summary_blocks_from_clusters(
        story_records,
        summary_lookup,
        _article_lookup_by_id(article_targets),
        min_articles_per_story=runtime.min_articles_per_story,
    )
    if not story_blocks:
        return [], {
            "synthesis_method": "per_story_parallel",
            "story_blocks_requested": 0,
            "story_drafts_generated": 0,
            "missing_or_singleton_story_count": len(story_records),
            "contradiction_analytics": summarize_contradiction_analytics([]),
        }
    now = datetime.now().strftime("%B %d, %Y")
    all_story_drafts = run_story_synthesis_blocks(story_blocks, now, runtime)
    story_drafts = [draft for draft in all_story_drafts if draft.get("valid")]
    for draft in story_drafts:
        draft["story_text"] = str(draft.get("paragraph") or "").strip()
    rejected_drafts = [draft for draft in all_story_drafts if not draft.get("valid")]
    rejection_details = [
        {
            "story_key": draft.get("story_key"),
            "story_title": draft.get("story_title"),
            "topic_key": draft.get("topic_key"),
            "topic_title": draft.get("topic_title"),
            "article_count": draft.get("article_count"),
            "word_count": draft.get("word_count"),
            "min_word_count": draft.get("min_word_count"),
            "reason": draft.get("reason") or "unknown",
            "preview": draft.get("preview"),
        }
        for draft in rejected_drafts
    ]
    rejection_counts: dict[str, int] = {}
    for detail in rejection_details:
        reason = str(detail.get("reason") or "unknown")
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    return story_drafts, {
        "synthesis_method": "per_story_parallel",
        "story_blocks_requested": len(story_blocks),
        "story_drafts_generated": len(story_drafts),
        "story_drafts_rejected": len(all_story_drafts) - len(story_drafts),
        "story_draft_rejection_counts": rejection_counts,
        "story_draft_rejections": rejection_details,
        "contradiction_analytics": summarize_contradiction_analytics(all_story_drafts),
        "story_synthesis_concurrency": runtime.article_summary_concurrency,
        "model": runtime.model_reference,
        "model_name": runtime.model_name,
        "model_backend": runtime.model_backend,
    }
