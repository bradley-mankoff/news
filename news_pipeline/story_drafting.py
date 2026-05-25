"""Draft story paragraphs from article summaries inside retained clusters."""

from __future__ import annotations

import re
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


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


def story_summary_blocks_from_clusters(
    story_records: list[dict],
    article_summary_lookup: dict[str, str],
    *,
    min_articles_per_story: int,
) -> list[dict[str, Any]]:
    story_blocks: list[dict[str, Any]] = []
    for story_index, story in enumerate(story_records):
        summaries: list[str] = []
        article_ids: list[str] = []
        for article_id in story.get("cluster_article_ids") or story.get("article_ids") or []:
            clean_article_id = str(article_id or "").strip()
            entry = article_summary_lookup.get(clean_article_id)
            if not entry:
                continue
            summary_text = report_summary_text(entry)
            if not summary_text:
                continue
            summaries.append(summary_text)
            article_ids.append(clean_article_id)
        if len(summaries) < min_articles_per_story:
            continue
        story_blocks.append(
            {
                "topic_title": "Unclassified News",
                "story_title": story.get("story_title") or story.get("title") or "News update",
                "story_key": story.get("story_key") or f"global-story-{story_index + 1:02d}",
                "summaries": summaries,
                "article_ids": article_ids,
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
    story_title = str(story_block.get("story_title") or "Story update")
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    source_summary_lines = "\n".join(
        f"{index}. {summary}"
        for index, summary in enumerate(summaries, start=1)
    )
    system_prompt = SystemMessage(content=textwrap.dedent(f"""
        Today: {now_label}.
        You are synthesizing prewritten article summaries into one newsletter story paragraph.
        Use only the supplied source summaries.
        Write exactly one cohesive prose paragraph, roughly 70-130 words.
        Lead with today's reported development. Include concrete reported claims, named actors,
        places, timing, figures, damage, statements, deadlines, and uncertainty when supported.
        Do not write a heading, bullets, labels, source-material notes, methodology, or preamble.
        Do not merge in background material unless a source summary reports it as part of today's update.
    """).strip())
    user_prompt = HumanMessage(content=textwrap.dedent(f"""
        Topic: {topic_title}
        Story: {story_title}

        Source summaries:
        {source_summary_lines}

        Return only the story paragraph.
    """).strip())
    return [system_prompt, user_prompt]


def clean_story_synthesis_paragraph(
    raw_text: str,
    summaries: list[str],
    runtime: StoryDraftingRuntime,
) -> str:
    clean_text = runtime.strip_prompt_echo_lines(runtime.strip_model_artifacts(raw_text or ""))
    clean_text = re.sub(r"(?m)^##+\s+.*$", "", clean_text)
    clean_text = re.sub(r"(?mi)^(topic|story|source summaries?|paragraph)\s*:\s*.*$", "", clean_text)
    clean_text = re.sub(r"(?m)^\s*[-*]\s+", "", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    if clean_text and not runtime.is_low_coverage_synthesis_section(clean_text):
        return clean_text
    return runtime.dev_synthesis_paragraph_from_summaries(summaries)


def run_story_synthesis_block(
    story_block: dict[str, Any],
    now_label: str,
    runtime: StoryDraftingRuntime,
) -> dict[str, Any]:
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    fallback_paragraph = runtime.dev_synthesis_paragraph_from_summaries(summaries)
    prompt_messages = build_story_synthesis_prompt_messages(story_block, now_label)
    estimated_input_tokens = sum(runtime.estimate_message_token_count(message) for message in prompt_messages)
    response = runtime.invoke_with_retries(
        runtime.build_chat_model(
            max_tokens=max(300, min(900, runtime.final_synthesis_max_tokens)),
            task="final_synthesis",
        ),
        prompt_messages,
        task_name=f"story synthesis for {story_block.get('story_title') or 'story'}",
        fallback_content=fallback_paragraph,
    )
    paragraph = clean_story_synthesis_paragraph(response.content, summaries, runtime)
    prompt_tokens = runtime.extract_prompt_tokens_from_response(response)
    return {
        **story_block,
        "paragraph": paragraph,
        "estimated_input_tokens": estimated_input_tokens,
        "actual_prompt_tokens": prompt_tokens,
        "word_count": runtime.final_synthesis_word_count(paragraph),
        "valid": bool(paragraph),
        "reason": "accepted" if paragraph else "empty after cleanup",
        "preview": paragraph[:500],
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_lookup = article_summary_lookup_by_id(article_summary_reports)
    story_blocks = story_summary_blocks_from_clusters(
        story_records,
        summary_lookup,
        min_articles_per_story=runtime.min_articles_per_story,
    )
    if not story_blocks:
        return [], {
            "synthesis_method": "per_story_parallel",
            "story_blocks_requested": 0,
            "story_drafts_generated": 0,
            "missing_or_singleton_story_count": len(story_records),
        }
    now = datetime.now().strftime("%B %d, %Y")
    story_drafts = run_story_synthesis_blocks(story_blocks, now, runtime)
    for draft in story_drafts:
        draft["story_text"] = str(draft.get("paragraph") or "").strip()
    return story_drafts, {
        "synthesis_method": "per_story_parallel",
        "story_blocks_requested": len(story_blocks),
        "story_drafts_generated": len(story_drafts),
        "story_synthesis_concurrency": runtime.article_summary_concurrency,
        "model": runtime.model_reference,
        "model_name": runtime.model_name,
        "model_backend": runtime.model_backend,
    }
