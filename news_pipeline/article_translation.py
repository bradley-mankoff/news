"""Deep translation pass for declared non-English article candidates.

The stage is opt-in (``enabled`` on the runtime), deterministic, and
non-destructive: it translates scraped article bodies only, keeps the source
catalog's declared ``language`` as the authority (no script sniffing or
language guessing), and always preserves the original body as the fallback
for downstream clustering/summarization. The TranslateGemma structured
language-code message contract is the only prompt shape this stage builds
(issue #172; pattern source: the removed implementation at 06ef7b0^).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

TranslationProgressCallback = Callable[..., None]

# Bounded request body sent to the translation model; the full in-memory body
# stays available for fallback and provenance previews.
TRANSLATION_REQUEST_TEXT_LIMIT = 5000
TRANSLATION_PREVIEW_LIMIT = 300
TRANSLATION_TASK = "translation"
_DEFAULT_TARGET_LANGUAGE = "en"

TRANSLATION_STATUS_TRANSLATED = "translated"
TRANSLATION_STATUS_UNCHANGED = "unchanged"
TRANSLATION_STATUS_NOT_NEEDED = "not_needed"
TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE = "skipped_unknown_language"

TRANSLATION_REASON_DISABLED = "translation_disabled"
TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE = "missing_source_language"
TRANSLATION_REASON_NOT_NEEDED = "not_needed"
TRANSLATION_REASON_UNSUPPORTED_LANGUAGE_CODE = "unsupported_language_code"
TRANSLATION_REASON_EMPTY_BODY = "empty_body"
TRANSLATION_REASON_TRANSLATION_FAILED = "translation_failed"
TRANSLATION_REASON_EMPTY_MODEL_OUTPUT = "empty_model_output"
TRANSLATION_REASON_MODEL_RETURNED_UNCHANGED = "model_returned_unchanged"
TRANSLATION_REASON_TRANSLATED = "translated"


@dataclass(frozen=True)
class ArticleTranslationRuntime:
    """Injected collaborators for the translation stage (issue #172).

    Mirrors ``ArticleSummarizationRuntime``: the stage never imports pipeline
    globals; model building, retries, artifact stripping, and progress
    reporting arrive through the runtime so the stage is independently
    testable.
    """

    source_feeds: dict[str, dict[str, Any]]
    enabled: bool
    target_language: str
    max_tokens: int
    build_chat_model: Callable[..., Any]
    invoke_with_retries: Callable[..., AIMessage]
    strip_model_artifacts: Callable[[str], str]
    model_reference: str
    model_name: str
    # Optional authority for the model's language-code vocabulary. When
    # provided and a code fails validation, the article is skipped with a
    # visible status and no model call; when None the model template is the
    # only authority and a rejection surfaces as a failed translation that
    # preserves the original body.
    validate_language_code: Callable[[str], bool] | None = None
    progress_start: Callable[[int], None] | None = None
    progress_advance: TranslationProgressCallback | None = None
    progress_finish: TranslationProgressCallback | None = None
    detail: Callable[[str], None] | None = None


def normalize_language_code(value: Any) -> str | None:
    """Normalize a declared language code for model input.

    Lowercases and folds underscore separators to hyphens (``zh_Hans`` ->
    ``zh-Hans``) without mapping unknown codes to another language. Empty or
    whitespace values normalize to None: a missing declared language is a
    skipped status, never an inference.
    """
    normalized = str(value or "").strip().lower().replace("_", "-")
    return normalized or None


def translation_decision(article: dict[str, Any], runtime: ArticleTranslationRuntime) -> dict[str, Any]:
    """Deterministic per-article translation decision from declared language.

    Rules (issue #33 semantics): disabled -> no translation; missing declared
    source language -> skip; normalized source equals target -> unchanged;
    normalized source differs from target -> translation candidate. No script
    inspection and no language guessing.
    """
    if not runtime.enabled:
        return {
            "needed": False,
            "reason": TRANSLATION_REASON_DISABLED,
            "source_language": None,
        }
    source_name = str(article.get("source") or "")
    source_config = runtime.source_feeds.get(source_name) or {}
    source_language = normalize_language_code(source_config.get("language"))
    if not source_language:
        return {
            "needed": False,
            "reason": TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE,
            "source_language": None,
        }
    target_language = normalize_language_code(runtime.target_language) or _DEFAULT_TARGET_LANGUAGE
    if source_language == target_language:
        return {
            "needed": False,
            "reason": TRANSLATION_REASON_NOT_NEEDED,
            "source_language": source_language,
        }
    return {
        "needed": True,
        "reason": "needs_translation",
        "source_language": source_language,
    }


def build_translation_messages(
    text: str,
    source_language: str,
    target_language: str,
) -> list[BaseMessage]:
    """Build the TranslateGemma structured language-code user message.

    The model expects one content list item carrying ``type``,
    ``source_lang_code``, ``target_lang_code``, and ``text``; the request
    body is bounded to ``TRANSLATION_REQUEST_TEXT_LIMIT`` characters while
    the caller keeps the full in-memory body for fallback/provenance.
    """
    return [
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "source_lang_code": source_language,
                    "target_lang_code": target_language,
                    "text": (text or "")[:TRANSLATION_REQUEST_TEXT_LIMIT],
                }
            ]
        )
    ]


def _with_translation_metadata(
    article: dict[str, Any],
    *,
    status: str,
    reason: str,
    source_language: str | None,
    target_language: str,
    model_reference: str | None,
    text: str,
) -> dict[str, Any]:
    original_preview = str(article.get("text") or "")[:TRANSLATION_PREVIEW_LIMIT]
    return {
        **article,
        "text": text,
        "translation_status": status,
        "translation_reason": reason,
        "translation_source_language": source_language,
        "translation_target_language": target_language,
        "translation_model": model_reference,
        "translation_original_text_preview": original_preview,
        "translation_text_preview": text[:TRANSLATION_PREVIEW_LIMIT],
    }


def _translation_status_result(
    article: dict[str, Any],
    *,
    status: str,
    reason: str,
    source_language: str | None,
    target_language: str,
    text: str,
    model_reference: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pair an annotated article with its JSON-ready status record.

    Every terminal translation outcome shares this shape: ``text`` carries
    the translated body when the model produced new content, otherwise the
    original body (the stage is non-destructive).
    """
    return (
        _with_translation_metadata(
            article,
            status=status,
            reason=reason,
            source_language=source_language,
            target_language=target_language,
            model_reference=model_reference,
            text=text,
        ),
        {
            "status": status,
            "reason": reason,
            "source_language": source_language,
        },
    )


def _translation_failure_result(
    article: dict[str, Any],
    runtime: ArticleTranslationRuntime,
    *,
    source_language: str,
    target_language: str,
    original_text: str,
    title: str,
    error: BaseException,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a visible, non-destructive result for an adapter failure."""
    label = title or str(article.get("url") or "article")
    if runtime.detail is not None:
        runtime.detail(
            f"Translation failed for {label[:80]} "
            f"({type(error).__name__}: {error}); keeping original body."
        )
    return _translation_status_result(
        article,
        status=TRANSLATION_STATUS_UNCHANGED,
        reason=TRANSLATION_REASON_TRANSLATION_FAILED,
        source_language=source_language,
        target_language=target_language,
        text=original_text,
        model_reference=runtime.model_reference,
    )


def _translation_chunks(text: str) -> list[str]:
    """Split a body into bounded chunks without dropping its suffix."""
    return [
        text[start : start + TRANSLATION_REQUEST_TEXT_LIMIT]
        for start in range(0, len(text), TRANSLATION_REQUEST_TEXT_LIMIT)
    ] or [""]


def _translate_single_article(
    article: dict[str, Any],
    runtime: ArticleTranslationRuntime,
    decision: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate one candidate, preserving the original body on any failure."""
    source_language = decision["source_language"]
    target_language = (
        normalize_language_code(runtime.target_language) or _DEFAULT_TARGET_LANGUAGE
    )
    original_text = str(article.get("text") or "")
    title = str(article.get("title") or "")

    if runtime.validate_language_code is not None:
        try:
            language_is_supported = runtime.validate_language_code(source_language)
        except (ValueError, TypeError, AttributeError, OSError, TimeoutError) as error:
            return _translation_failure_result(
                article,
                runtime,
                source_language=source_language,
                target_language=target_language,
                original_text=original_text,
                title=title,
                error=error,
            )
        if not language_is_supported:
            return _translation_status_result(
                article,
                status=TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE,
                reason=TRANSLATION_REASON_UNSUPPORTED_LANGUAGE_CODE,
                source_language=source_language,
                target_language=target_language,
                text=original_text,
                model_reference=None,
            )

    if not original_text.strip():
        return _translation_status_result(
            article,
            status=TRANSLATION_STATUS_UNCHANGED,
            reason=TRANSLATION_REASON_EMPTY_BODY,
            source_language=source_language,
            target_language=target_language,
            text=original_text,
            model_reference=None,
        )

    try:
        llm = runtime.build_chat_model(max_tokens=runtime.max_tokens, task=TRANSLATION_TASK)
        chunks = _translation_chunks(original_text)
        translated_parts: list[str] = []
        used_fallback = False
        empty_model_output = False
        for chunk_index, chunk in enumerate(chunks, start=1):
            task_name = f"translation for {title}"
            if len(chunks) > 1:
                task_name += f" (part {chunk_index}/{len(chunks)})"
            response = runtime.invoke_with_retries(
                llm,
                build_translation_messages(chunk, source_language, target_language),
                task_name=task_name,
                fallback_content=chunk,
            )
            used_fallback = bool(
                (getattr(response, "response_metadata", None) or {}).get(
                    "news_pipeline_used_fallback"
                )
            )
            if used_fallback:
                break
            translated_part = runtime.strip_model_artifacts(
                str(response.content or "")
            ).strip()
            if not translated_part:
                empty_model_output = True
                break
            translated_parts.append(translated_part)
        translated_text = "".join(translated_parts).strip()
    except (ValueError, TypeError, AttributeError, OSError, TimeoutError) as error:
        return _translation_failure_result(
            article,
            runtime,
            source_language=source_language,
            target_language=target_language,
            original_text=original_text,
            title=title,
            error=error,
        )

    if used_fallback or empty_model_output or not translated_text:
        # A distinct failure reason even though the body status is unchanged:
        # the original body flows to clustering/summarization untouched.
        reason = (
            TRANSLATION_REASON_TRANSLATION_FAILED
            if used_fallback
            else TRANSLATION_REASON_EMPTY_MODEL_OUTPUT
        )
        return _translation_status_result(
            article,
            status=TRANSLATION_STATUS_UNCHANGED,
            reason=reason,
            source_language=source_language,
            target_language=target_language,
            text=original_text,
            model_reference=runtime.model_reference,
        )
    if translated_text == original_text.strip():
        return _translation_status_result(
            article,
            status=TRANSLATION_STATUS_UNCHANGED,
            reason=TRANSLATION_REASON_MODEL_RETURNED_UNCHANGED,
            source_language=source_language,
            target_language=target_language,
            text=original_text,
            model_reference=runtime.model_reference,
        )
    return _translation_status_result(
        article,
        status=TRANSLATION_STATUS_TRANSLATED,
        reason=TRANSLATION_REASON_TRANSLATED,
        source_language=source_language,
        target_language=target_language,
        text=translated_text,
        model_reference=runtime.model_reference,
    )


def run_article_translation_pass(
    article_targets: list[dict[str, Any]],
    runtime: ArticleTranslationRuntime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the ordered translation pass over article candidates.

    Returns ``(ordered_articles, stage_result)``: the candidates in their
    exact original order (translated bodies replace ``text`` only when the
    model produced new content) and a JSON-ready stage-result payload with
    counts, target, model identity, and per-source-language counts. When
    translation is disabled or no candidate needs translation, the articles
    are returned unchanged and no model builder/invoker is called.
    """
    target_language = (
        normalize_language_code(runtime.target_language) or _DEFAULT_TARGET_LANGUAGE
    )
    payload: dict[str, Any] = {
        "enabled": runtime.enabled,
        "candidate_count": len(article_targets),
        "translated_count": 0,
        "unchanged_count": 0,
        "skipped_unknown_language": 0,
        "target_language": target_language,
        "model": runtime.model_reference,
        "model_name": runtime.model_name,
        "source_language_counts": {},
    }
    if not runtime.enabled:
        return article_targets, payload

    decisions = [translation_decision(article, runtime) for article in article_targets]
    translation_targets = [
        (article, decision)
        for article, decision in zip(article_targets, decisions)
        if decision["needed"]
    ]
    if runtime.detail is not None and translation_targets:
        runtime.detail(
            f"Translation model: {runtime.model_reference} -> {runtime.model_name}; "
            f"target language: {target_language}"
        )
    if runtime.progress_start is not None and translation_targets:
        runtime.progress_start(len(translation_targets))

    translated_articles: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_language_counts: Counter[str] = Counter()
    translation_index = 0
    for article, decision in zip(article_targets, decisions):
        if not decision["needed"]:
            status = (
                TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE
                if decision["reason"] == TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE
                else TRANSLATION_STATUS_NOT_NEEDED
            )
            translated_article, _ = _translation_status_result(
                article,
                status=status,
                reason=decision["reason"],
                source_language=decision["source_language"],
                target_language=target_language,
                text=str(article.get("text") or ""),
                model_reference=None,
            )
        else:
            translation_index += 1
            if runtime.progress_advance is not None:
                runtime.progress_advance(
                    detail=(
                        f"  [{translation_index}/{len(translation_targets)}] Translating "
                        f"{str(article.get('title') or article.get('url') or 'article')[:80]}"
                    )
                )
            translated_article, _ = _translate_single_article(article, runtime, decision)
        translated_articles.append(translated_article)
        status = translated_article["translation_status"]
        status_counts[status] += 1
        if translated_article.get("translation_source_language"):
            source_language_counts[translated_article["translation_source_language"]] += 1

    payload["translated_count"] = int(status_counts.get(TRANSLATION_STATUS_TRANSLATED, 0))
    payload["unchanged_count"] = int(status_counts.get(TRANSLATION_STATUS_UNCHANGED, 0))
    payload["skipped_unknown_language"] = int(
        status_counts.get(TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE, 0)
    )
    payload["not_needed_count"] = int(status_counts.get(TRANSLATION_STATUS_NOT_NEEDED, 0))
    payload["source_language_counts"] = dict(sorted(source_language_counts.items()))
    if runtime.progress_finish is not None:
        runtime.progress_finish(
            detail=f"{payload['translated_count']} translated article(s)."
        )
    return translated_articles, payload
