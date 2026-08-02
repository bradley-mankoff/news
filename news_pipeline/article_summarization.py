"""Article summarization pass for retained story-cluster articles."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from .article_summary_records import ArticleSummaryRecord
from .prompt_catalog import DEFAULT_PROMPT_INSTRUCTIONS

@dataclass(frozen=True)
class ArticleSummarizationRuntime:
    source_feeds: dict[str, dict[str, Any]]
    recent_window_hours: int
    article_summary_concurrency: int
    article_summary_max_tokens: int
    build_article_heading: Callable[[dict], str]
    format_article_metadata: Callable[[dict], str]
    build_article_fallback_entry: Callable[[dict], str]
    build_chat_model: Callable[..., Any]
    invoke_with_retries: Callable[..., AIMessage]
    has_structured_entry: Callable[[str, str], bool]
    normalize_report_entry: Callable[[dict, str], ArticleSummaryRecord]
    article_completed: Callable[..., None]
    prompt_instructions: str | None = None


ARTICLE_SUMMARY_FORMAT_ERROR_MESSAGE = (
    "Format Error: respond with exactly one article block only. "
    "Use 'DATABASE_ENTRY:' followed by '### article title', then 'Metadata:' with Source/Published/URL bullets "
    "then 'Summary:'. "
    "Do not add commentary, correction text, code fences, or trailing notes."
)


def _notify_article_completed(runtime: ArticleSummarizationRuntime, article: dict) -> None:
    try:
        runtime.article_completed(article)
    except TypeError:
        runtime.article_completed()


def build_article_summary_prompt_messages(
    current_article: dict,
    now_label: str,
    runtime: ArticleSummarizationRuntime,
) -> list[BaseMessage]:
    source_name = current_article.get("source", "Unknown source")
    source_config = runtime.source_feeds.get(source_name)
    display_name = (
        source_config.get("name", source_name)
        if isinstance(source_config, dict)
        else source_name
    )
    display_name = str(current_article.get("source_display_name") or display_name)
    target = runtime.build_article_heading(current_article)
    selection_guidance = f"7. {runtime.prompt_instructions or DEFAULT_PROMPT_INSTRUCTIONS['article_summary']}"
    system_prompt = SystemMessage(content=textwrap.dedent(f"""
        Today: {now_label}.
        Current Task: Summarize one preselected article from the last {runtime.recent_window_hours} hours
        for story discovery, selection, and synthesis.
        1. Use only the provided article metadata, URL, description, and article text.
        2. Do not call tools in this step.
        3. Ignore outlet style and focus on concrete reported claims.
        4. Include key facts: what reportedly happened, where, timeline, named actors, casualties or damage if reported, and what remains unconfirmed.
        5. If the article text is thin, summarize only what is actually supported by the provided text and metadata.
        6. Do not recap the general history of a longstanding subject or conflict; include background only
           when the article reports a new fact about it or one short clause is needed for orientation.
        {selection_guidance}
        8. Start your response with 'DATABASE_ENTRY:' and then exactly the requested Markdown block.
        9. Do not include any text before 'DATABASE_ENTRY:' or after the summary.
    """).strip())
    story_line = f"Story: {current_article.get('story_title')}\n" if current_article.get("story_title") else ""
    article_payload = (
        "Selected article:\n\n"
        f"Title: {current_article.get('title') or 'N/A'}\n"
        f"Source: {display_name}\n"
        f"Published: {current_article.get('pub_date') or 'Unknown publish time'}\n"
        f"URL: {current_article.get('url') or 'N/A'}\n"
        f"{story_line}"
        f"Description: {current_article.get('description') or 'N/A'}\n"
        f"Article text:\n{current_article.get('text') or 'N/A'}\n\n"
        "Return exactly this block, replacing only the summary text:\n\n"
        "DATABASE_ENTRY:\n"
        f"### {target}\n"
        "Metadata:\n"
        f"- Source: {display_name}\n"
        f"- Published: {current_article.get('pub_date') or 'Unknown publish time'}\n"
        f"- URL: {current_article.get('url') or 'N/A'}\n\n"
        "Summary:\n"
        "<4-7 sentence article summary in plain prose, no brackets>"
    )
    return [system_prompt, HumanMessage(content=article_payload)]


def _summarize_single_article(runtime: ArticleSummarizationRuntime, current_article: dict) -> ArticleSummaryRecord:
    now = datetime.now().strftime("%B %d, %Y")
    target = runtime.build_article_heading(current_article)
    prompt_messages = build_article_summary_prompt_messages(current_article, now, runtime)
    format_error_message = HumanMessage(content=ARTICLE_SUMMARY_FORMAT_ERROR_MESSAGE)
    fallback_content = runtime.build_article_fallback_entry(current_article)

    for attempt in range(1, 4):
        llm = runtime.build_chat_model(
            max_tokens=runtime.article_summary_max_tokens,
            task="article_summary",
        )
        messages = prompt_messages if attempt == 1 else [*prompt_messages, format_error_message]
        response = runtime.invoke_with_retries(
            llm,
            messages,
            task_name=f"analysis for {target}",
            fallback_content=fallback_content,
        )
        if runtime.has_structured_entry(response.content, target):
            summary = runtime.normalize_report_entry(current_article, response.content)
            _notify_article_completed(runtime, current_article)
            return summary

    fallback_summary = runtime.normalize_report_entry(current_article, fallback_content)
    _notify_article_completed(runtime, current_article)
    return fallback_summary


def run_article_summary_pass(
    article_targets: list[dict],
    runtime: ArticleSummarizationRuntime,
) -> list[ArticleSummaryRecord]:
    if not article_targets:
        return []

    if runtime.article_summary_concurrency > 1 and len(article_targets) > 1:
        ordered_results: list[tuple[int, list[ArticleSummaryRecord]]] = []
        with ThreadPoolExecutor(max_workers=runtime.article_summary_concurrency) as executor:
            future_map = {
                executor.submit(run_article_summary_pass, [article], runtime): index
                for index, article in enumerate(article_targets)
            }
            for future in as_completed(future_map):
                ordered_results.append((future_map[future], future.result()))
        final_reports: list[ArticleSummaryRecord] = []
        for _, reports in sorted(ordered_results, key=lambda item: item[0]):
            final_reports.extend(reports)
        return final_reports

    final_reports: list[ArticleSummaryRecord] = []
    for article in article_targets:
        final_reports.append(_summarize_single_article(runtime, article))
    return final_reports
