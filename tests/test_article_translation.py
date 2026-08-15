"""Behavioral contract tests for the deep translation stage (issue #172)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from news_pipeline.article_translation import (
    TRANSLATION_REASON_EMPTY_BODY,
    TRANSLATION_REASON_EMPTY_MODEL_OUTPUT,
    TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE,
    TRANSLATION_REASON_MODEL_RETURNED_UNCHANGED,
    TRANSLATION_REASON_NOT_NEEDED,
    TRANSLATION_REASON_TRANSLATED,
    TRANSLATION_REASON_TRANSLATION_FAILED,
    TRANSLATION_REASON_UNSUPPORTED_LANGUAGE_CODE,
    TRANSLATION_STATUS_NOT_NEEDED,
    TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE,
    TRANSLATION_STATUS_TRANSLATED,
    TRANSLATION_STATUS_UNCHANGED,
    ArticleTranslationRuntime,
    build_translation_messages,
    normalize_language_code,
    run_article_translation_pass,
    translation_decision,
)

MODEL_REFERENCE = "translategemma-4b-it-4bit"
MODEL_NAME = "mlx-community/translategemma-4b-it-4bit"

SOURCE_FEEDS = {
    "el-pais": {"language": "es"},
    "le-monde": {"language": "fr"},
    "bbc": {"language": "en"},
    "zh-news": {"language": "zh_Hans"},
    "no-lang": {},
}


def _runtime(
    *,
    enabled: bool = True,
    target_language: str = "en",
    translated_text: str = "Translated text.",
    used_fallback: bool = False,
    validate_language_code: Any = None,
) -> tuple[ArticleTranslationRuntime, list[Any]]:
    calls: list[Any] = []

    def build_chat_model(**kwargs: object) -> object:
        calls.append(("build_chat_model", kwargs))
        return object()

    def invoke_with_retries(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append(("invoke_with_retries", _kwargs))
        return SimpleNamespace(
            content=translated_text,
            response_metadata={
                "news_pipeline_used_fallback": used_fallback,
            },
        )

    return (
        ArticleTranslationRuntime(
            source_feeds=SOURCE_FEEDS,
            enabled=enabled,
            target_language=target_language,
            max_tokens=800,
            build_chat_model=build_chat_model,
            invoke_with_retries=invoke_with_retries,
            strip_model_artifacts=lambda text: text,
            model_reference=MODEL_REFERENCE,
            model_name=MODEL_NAME,
            validate_language_code=validate_language_code,
            progress_start=lambda total: calls.append(("progress_start", total)),
            progress_advance=lambda detail: calls.append(("progress_advance", detail)),
            progress_finish=lambda detail: calls.append(("progress_finish", detail)),
            detail=lambda message: calls.append(("detail", message)),
        ),
        calls,
    )


def _article(source: str = "el-pais", *, title: str = "Titulo", text: str = "Hola mundo") -> dict[str, Any]:
    return {
        "article_id": f"{source}-article-1",
        "source": source,
        "title": title,
        "url": f"https://example.com/{source}/1",
        "text": text,
    }


class TranslationDecisionTests(unittest.TestCase):
    def test_disabled_policy_decides_no_translation(self) -> None:
        runtime, _calls = _runtime(enabled=False)
        decision = translation_decision(_article(), runtime)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["reason"], "translation_disabled")
        self.assertIsNone(decision["source_language"])

    def test_declared_source_language_is_authoritative(self) -> None:
        runtime, _calls = _runtime()
        decision = translation_decision(_article("el-pais"), runtime)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["source_language"], "es")

    def test_english_source_is_not_needed(self) -> None:
        runtime, _calls = _runtime()
        decision = translation_decision(_article("bbc"), runtime)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["reason"], TRANSLATION_REASON_NOT_NEEDED)

    def test_source_equals_configured_target_is_not_needed(self) -> None:
        runtime, _calls = _runtime(target_language="fr")
        decision = translation_decision(_article("le-monde"), runtime)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["reason"], TRANSLATION_REASON_NOT_NEEDED)
        # A non-target source still needs translation.
        self.assertTrue(translation_decision(_article("el-pais"), runtime)["needed"])

    def test_missing_declared_language_is_skipped_not_guessed(self) -> None:
        runtime, _calls = _runtime()
        decision = translation_decision(_article("no-lang"), runtime)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["reason"], TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE)
        self.assertIsNone(decision["source_language"])

    def test_underscore_language_code_normalizes_to_hyphen_only(self) -> None:
        runtime, _calls = _runtime()
        decision = translation_decision(_article("zh-news"), runtime)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["source_language"], "zh-hans")


class TranslationMessageTests(unittest.TestCase):
    def test_structured_message_uses_normalized_language_codes(self) -> None:
        messages = build_translation_messages("Bonjour", "fr", "en")
        self.assertEqual(len(messages), 1)
        content = messages[0].content
        self.assertIsInstance(content, list)
        item = content[0]
        self.assertEqual(item["type"], "text")
        self.assertEqual(item["source_lang_code"], "fr")
        self.assertEqual(item["target_lang_code"], "en")
        self.assertEqual(item["text"], "Bonjour")

    def test_request_body_is_bounded_to_5000_characters(self) -> None:
        messages = build_translation_messages("x" * 9000, "es", "en")
        item = messages[0].content[0]
        self.assertEqual(len(item["text"]), 5000)

    def test_normalize_language_code_folds_underscores_and_case(self) -> None:
        self.assertEqual(normalize_language_code("zh_Hans"), "zh-hans")
        self.assertEqual(normalize_language_code("  FR "), "fr")
        self.assertIsNone(normalize_language_code(""))
        self.assertIsNone(normalize_language_code(None))


class TranslationPassTests(unittest.TestCase):
    def test_disabled_pass_returns_articles_untouched_with_no_model_calls(self) -> None:
        runtime, calls = _runtime(enabled=False)
        articles = [_article(), _article("bbc")]
        out, payload = run_article_translation_pass(articles, runtime)
        self.assertEqual(out, articles)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["candidate_count"], 2)
        self.assertEqual(payload["translated_count"], 0)
        self.assertFalse(
            [call for call in calls if call[0] in ("build_chat_model", "invoke_with_retries")]
        )

    def test_english_only_pass_emits_no_op_event_without_model_calls(self) -> None:
        runtime, calls = _runtime(enabled=True)
        articles = [_article("bbc", text="Hello world")]
        out, payload = run_article_translation_pass(articles, runtime)
        self.assertEqual(out[0]["text"], "Hello world")
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_NOT_NEEDED)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_NOT_NEEDED)
        self.assertEqual(payload["translated_count"], 0)
        self.assertEqual(payload["not_needed_count"], 1)
        self.assertFalse(
            [call for call in calls if call[0] in ("build_chat_model", "invoke_with_retries")]
        )

    def test_successful_translation_replaces_only_text_and_keeps_identity(self) -> None:
        runtime, calls = _runtime()
        article = _article()
        out, payload = run_article_translation_pass([article], runtime)
        translated = out[0]
        self.assertEqual(translated["translation_status"], TRANSLATION_STATUS_TRANSLATED)
        self.assertEqual(translated["translation_reason"], TRANSLATION_REASON_TRANSLATED)
        self.assertEqual(translated["text"], "Translated text.")
        self.assertEqual(translated["url"], article["url"])
        self.assertEqual(translated["title"], article["title"])
        self.assertEqual(translated["source"], "el-pais")
        self.assertEqual(translated["translation_source_language"], "es")
        self.assertEqual(translated["translation_target_language"], "en")
        self.assertEqual(translated["translation_model"], MODEL_REFERENCE)
        self.assertEqual(translated["translation_original_text_preview"], "Hola mundo")
        self.assertEqual(translated["translation_text_preview"], "Translated text.")
        self.assertEqual(payload["translated_count"], 1)
        self.assertEqual(payload["source_language_counts"], {"es": 1})
        self.assertEqual(
            [call[0] for call in calls],
            ["detail", "progress_start", "progress_advance", "build_chat_model", "invoke_with_retries", "progress_finish"],
        )
        self.assertEqual(calls[3][1]["task"], "translation")
        self.assertEqual(calls[4][1]["task_name"], "translation for Titulo")

    def test_unchanged_model_output_is_recorded_not_translated(self) -> None:
        runtime, _calls = _runtime(translated_text="Hola mundo")
        out, payload = run_article_translation_pass([_article()], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_UNCHANGED)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_MODEL_RETURNED_UNCHANGED)
        self.assertEqual(out[0]["text"], "Hola mundo")
        self.assertEqual(payload["translated_count"], 0)
        self.assertEqual(payload["unchanged_count"], 1)

    def test_model_fallback_preserves_original_body_with_distinct_reason(self) -> None:
        runtime, _calls = _runtime(translated_text="Hola mundo", used_fallback=True)
        out, payload = run_article_translation_pass([_article()], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_UNCHANGED)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_TRANSLATION_FAILED)
        self.assertEqual(out[0]["text"], "Hola mundo")
        self.assertEqual(out[0]["translation_model"], MODEL_REFERENCE)
        self.assertEqual(payload["unchanged_count"], 1)

    def test_empty_model_output_preserves_original_body(self) -> None:
        runtime, _calls = _runtime(translated_text="")
        out, _payload = run_article_translation_pass([_article()], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_UNCHANGED)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_EMPTY_MODEL_OUTPUT)
        self.assertEqual(out[0]["text"], "Hola mundo")

    def test_empty_body_skips_model_call_and_stays_unchanged(self) -> None:
        runtime, calls = _runtime()
        out, payload = run_article_translation_pass([_article(text="   ")], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_UNCHANGED)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_EMPTY_BODY)
        self.assertEqual(out[0]["text"], "   ")
        self.assertFalse(
            [call for call in calls if call[0] in ("build_chat_model", "invoke_with_retries")]
        )
        self.assertEqual(payload["unchanged_count"], 1)

    def test_unsupported_language_code_skips_without_model_call(self) -> None:
        runtime, calls = _runtime(
            validate_language_code=lambda code: code in {"es", "fr", "en", "zh-hans"}
        )
        article = _article("el-pais")
        # A declared-but-unsupported code is only reachable through a feed
        # whose language is not in the injected model vocabulary.
        feeds = dict(SOURCE_FEEDS)
        feeds["el-pais"] = {"language": "xx"}
        runtime = ArticleTranslationRuntime(
            source_feeds=feeds,
            enabled=True,
            target_language="en",
            max_tokens=800,
            build_chat_model=lambda **_kwargs: object(),
            invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content="x", response_metadata={}),
            strip_model_artifacts=lambda text: text,
            model_reference=MODEL_REFERENCE,
            model_name=MODEL_NAME,
            validate_language_code=lambda code: code in {"es", "fr", "en"},
        )
        out, payload = run_article_translation_pass([article], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE)
        self.assertEqual(
            out[0]["translation_reason"], TRANSLATION_REASON_UNSUPPORTED_LANGUAGE_CODE
        )
        self.assertEqual(out[0]["text"], "Hola mundo")
        self.assertIsNone(out[0]["translation_model"])
        self.assertEqual(payload["skipped_unknown_language"], 1)
        self.assertEqual(payload["source_language_counts"], {"xx": 1})

    def test_missing_language_direct_call_records_skipped_status(self) -> None:
        runtime, calls = _runtime()
        out, payload = run_article_translation_pass([_article("no-lang")], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_SKIPPED_UNKNOWN_LANGUAGE)
        self.assertEqual(out[0]["translation_reason"], TRANSLATION_REASON_MISSING_SOURCE_LANGUAGE)
        self.assertIsNone(out[0]["translation_model"])
        self.assertEqual(payload["skipped_unknown_language"], 1)
        self.assertFalse(
            [call for call in calls if call[0] in ("build_chat_model", "invoke_with_retries")]
        )

    def test_output_ordering_and_multi_language_counts_are_deterministic(self) -> None:
        runtime, _calls = _runtime()
        articles = [
            _article("el-pais", title="A", text="Hola"),
            _article("bbc", title="B", text="Hello"),
            _article("le-monde", title="C", text="Bonjour"),
        ]
        out, payload = run_article_translation_pass(articles, runtime)
        self.assertEqual([a["title"] for a in out], ["A", "B", "C"])
        self.assertEqual(
            [a["translation_status"] for a in out],
            [TRANSLATION_STATUS_TRANSLATED, TRANSLATION_STATUS_NOT_NEEDED, TRANSLATION_STATUS_TRANSLATED],
        )
        self.assertEqual(payload["translated_count"], 2)
        self.assertEqual(payload["not_needed_count"], 1)
        self.assertEqual(payload["source_language_counts"], {"en": 1, "es": 1, "fr": 1})

    def test_model_artifacts_are_stripped_from_translated_output(self) -> None:
        def strip(text: str) -> str:
            return text.replace("<eos>", "").strip()

        runtime, _calls = _runtime(translated_text="Hola mundo traducido.<eos>")
        runtime = ArticleTranslationRuntime(
            source_feeds=runtime.source_feeds,
            enabled=runtime.enabled,
            target_language=runtime.target_language,
            max_tokens=runtime.max_tokens,
            build_chat_model=runtime.build_chat_model,
            invoke_with_retries=runtime.invoke_with_retries,
            strip_model_artifacts=strip,
            model_reference=runtime.model_reference,
            model_name=runtime.model_name,
        )
        out, _payload = run_article_translation_pass([_article()], runtime)
        self.assertEqual(out[0]["translation_status"], TRANSLATION_STATUS_TRANSLATED)
        self.assertEqual(out[0]["text"], "Hola mundo traducido.")


if __name__ == "__main__":
    unittest.main()
