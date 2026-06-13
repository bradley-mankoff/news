"""Article summarization pass for retained story-cluster articles."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

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
    normalize_report_entry: Callable[[dict, str], str]
    article_completed: Callable[..., None]


class ArticleSummaryState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    final_reports: list[str]
    articles_remaining: list[dict]
    empty_response_count: int


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
    selection_guidance = (
        "7. Prioritize facts that help later clustering and story synthesis; include major "
        "concrete developments without inventing relevance."
    )
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


def _build_article_summary_app(runtime: ArticleSummarizationRuntime):
    def call_model(state: ArticleSummaryState):
        now = datetime.now().strftime("%B %d, %Y")
        current_article = state["articles_remaining"][0] if state["articles_remaining"] else None
        if current_article is None:
            return {
                "messages": [AIMessage(content="ARTICLE_SUMMARIES_COMPLETE")],
                "empty_response_count": 0,
            }
        target = runtime.build_article_heading(current_article) if current_article else "Article Summary"
        llm = runtime.build_chat_model(
            max_tokens=runtime.article_summary_max_tokens,
            task="article_summary",
        )
        prompt_messages = build_article_summary_prompt_messages(current_article, now, runtime)
        response = runtime.invoke_with_retries(
            llm,
            prompt_messages,
            task_name=f"analysis for {target}",
            fallback_content=runtime.build_article_fallback_entry(current_article),
        )

        is_valid = runtime.has_structured_entry(response.content, target)
        error_count = 0 if is_valid else state.get("empty_response_count", 0) + 1
        return {
            "messages": [response],
            "empty_response_count": error_count,
        }

    def recover_from_empty_response(state: ArticleSummaryState):
        error_count = state.get("empty_response_count", 0)

        if error_count >= 3:
            article = state["articles_remaining"][0]
            fallback_summary = runtime.build_article_fallback_entry(article).split("DATABASE_ENTRY:\n", 1)[1]
            wipe_messages = [RemoveMessage(id=m.id) for m in state["messages"]]
            _notify_article_completed(runtime, article)
            return {
                "messages": wipe_messages + [HumanMessage(content="Proceed to next target.")],
                "final_reports": state["final_reports"] + [fallback_summary],
                "articles_remaining": state["articles_remaining"][1:],
                "empty_response_count": 0,
            }

        return {
            "messages": [HumanMessage(content=(
                "Format Error: respond with exactly one article block only. "
                "Use 'DATABASE_ENTRY:' followed by '### article title', then 'Metadata:' with Source/Published/URL bullets "
                "then 'Summary:'. "
                "Do not add commentary, correction text, code fences, or trailing notes."
            ))]
        }

    def database_save_and_clear(state: ArticleSummaryState):
        last_message = state["messages"][-1]
        current_article = state["articles_remaining"][0]
        heading_name = runtime.build_article_heading(current_article)

        if runtime.has_structured_entry(last_message.content, heading_name):
            summary = runtime.normalize_report_entry(current_article, last_message.content)
            _notify_article_completed(runtime, current_article)

            wipe_messages = [RemoveMessage(id=m.id) for m in state["messages"]]
            remaining_articles = state["articles_remaining"][1:]
            next_prompt = [HumanMessage(content="Proceed to next target.")] if remaining_articles else []

            return {
                "messages": wipe_messages + next_prompt,
                "final_reports": state["final_reports"] + [summary],
                "articles_remaining": remaining_articles,
                "empty_response_count": 0,
            }
        return {}

    def should_continue(state: ArticleSummaryState):
        if not state["articles_remaining"]:
            return END
        last_message = state["messages"][-1]
        if runtime.has_structured_entry(
            last_message.content,
            runtime.build_article_heading(state["articles_remaining"][0]),
        ):
            return "database"
        return "recover"

    def after_database(state: ArticleSummaryState):
        return "agent" if state["articles_remaining"] else END

    workflow = StateGraph(ArticleSummaryState)
    workflow.add_node("agent", call_model)
    workflow.add_node("database", database_save_and_clear)
    workflow.add_node("recover", recover_from_empty_response)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue)
    workflow.add_conditional_edges("database", after_database)
    workflow.add_edge("recover", "agent")
    return workflow.compile()


def run_article_summary_pass(
    article_targets: list[dict],
    runtime: ArticleSummarizationRuntime,
) -> list[str]:
    if not article_targets:
        return []

    if runtime.article_summary_concurrency > 1 and len(article_targets) > 1:
        ordered_results: list[tuple[int, list[str]]] = []
        with ThreadPoolExecutor(max_workers=runtime.article_summary_concurrency) as executor:
            future_map = {
                executor.submit(run_article_summary_pass, [article], runtime): index
                for index, article in enumerate(article_targets)
            }
            for future in as_completed(future_map):
                ordered_results.append((future_map[future], future.result()))
        final_reports: list[str] = []
        for _, reports in sorted(ordered_results, key=lambda item: item[0]):
            final_reports.extend(reports)
        return final_reports

    app = _build_article_summary_app(runtime)
    inputs: ArticleSummaryState = {
        "messages": [HumanMessage(content="Initialize research protocol.")],
        "final_reports": [],
        "articles_remaining": article_targets,
        "empty_response_count": 0,
    }

    recursion_limit = max(80, (len(article_targets) * 12) + 20)
    final_reports: list[str] = []
    for output in app.stream(inputs, stream_mode="values", config={"recursion_limit": recursion_limit}):
        if "final_reports" in output:
            final_reports = output["final_reports"]

    return final_reports
