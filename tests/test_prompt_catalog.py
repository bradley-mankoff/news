"""Prompt Catalog integrity and drift-guard tests.

The drift-guard tests assert that the `balanced` profile strings appear
verbatim in the rendered prompts of each stage module, so the default behavior
stays byte-identical to the pre-catalog prompts.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from news_pipeline import pipeline, prompt_catalog, prompt_contracts
from news_pipeline.article_summarization import (
    ArticleSummarizationRuntime,
    build_article_summary_prompt_messages,
)
from news_pipeline.story_drafting import (
    StoryDraftingRuntime,
    build_story_synthesis_prompt_messages,
)
from news_pipeline.story_selection import (
    StorySelectionRuntime,
    _global_scale_screening_prompt_messages,
)


def _article_summarization_runtime() -> ArticleSummarizationRuntime:
    return ArticleSummarizationRuntime(
        source_feeds={"Fixture Wire": {"name": "Fixture Wire"}},
        recent_window_hours=24,
        article_summary_concurrency=1,
        article_summary_max_tokens=800,
        build_article_heading=lambda article: str(article.get("title") or "Untitled article"),
        format_article_metadata=lambda article: f"Source: {article.get('source')}",
        build_article_fallback_entry=lambda _article: "fallback",
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content=""),
        has_structured_entry=lambda _content, _target: True,
        normalize_report_entry=lambda article, text: text,
        article_completed=lambda article=None: None,
    )


def _story_drafting_runtime() -> StoryDraftingRuntime:
    return StoryDraftingRuntime(
        story_synthesis_concurrency=1,
        story_drafting_max_tokens=1000,
        model_reference="test",
        model_name="test",
        model_backend="test",
        min_articles_per_story=2,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        estimate_message_token_count=lambda _message: 1,
        extract_prompt_tokens_from_response=lambda _response: None,
        strip_prompt_echo_lines=lambda text: text,
        strip_model_artifacts=lambda text: text,
        is_low_coverage_synthesis_section=lambda text: not str(text or "").strip(),
        fallback_synthesis_paragraph_from_summaries=lambda summaries: " ".join(summaries),
        story_drafting_word_count=lambda text: len(str(text or "").split()),
    )


def _story_selection_runtime() -> StorySelectionRuntime:
    return StorySelectionRuntime(
        story_scale_screening_enabled=True,
        model_max_input_tokens=1000,
        model_label="test-model",
        model_reference="test/reference",
        model_name="Test Model",
        model_backend="mlx-lm",
        relaxed_story_drafting_guards=True,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content="[]"),
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda _article: "",
        story_drafting_word_count=lambda text: len(str(text or "").split()),
        is_low_confidence_report_entry=lambda _entry: False,
        report_reference_key=lambda entry: entry,
    )


def _story_block() -> dict:
    return {
        "topic_title": "Weather",
        "story_title": "Storm damage",
        "summaries": ["Officials reported storm damage."],
        "citation_sources": [
            {
                "local_id": "S1",
                "title": "Fixture article S1",
                "source": "Fixture Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/s1",
                "article_id": "s1",
                "summary": "Officials reported storm damage.",
            }
        ],
    }


class PromptCatalogTests(unittest.TestCase):
    def test_builtin_profiles_cover_all_tasks(self) -> None:
        self.assertEqual(len(prompt_catalog.PROMPT_PROFILES), 5)
        expected_tasks = set(prompt_catalog.PROMPT_TASKS)
        for profile in prompt_catalog.PROMPT_PROFILES.values():
            self.assertEqual(set(profile.prompts.keys()), expected_tasks)
            for task, instruction in profile.prompts.items():
                self.assertTrue(instruction.strip(), f"{profile.id}:{task} is empty")

    def test_balanced_default_present_and_first(self) -> None:
        self.assertIn(prompt_catalog.DEFAULT_PROMPT_PROFILE_ID, prompt_catalog.PROMPT_PROFILES)
        self.assertEqual(prompt_catalog.PROMPT_PROFILE_IDS[0], prompt_catalog.DEFAULT_PROMPT_PROFILE_ID)
        self.assertEqual(
            list(prompt_catalog.list_prompt_profiles())[0]["id"],
            prompt_catalog.DEFAULT_PROMPT_PROFILE_ID,
        )

    def test_balanced_article_summary_string_appears_in_rendered_prompt(self) -> None:
        balanced = prompt_catalog.PROMPT_PROFILES["balanced"].prompts["article_summary"]
        messages = build_article_summary_prompt_messages(
            {"title": "Flood plan expands", "source": "Fixture Wire"},
            "May 30, 2026",
            _article_summarization_runtime(),
        )
        prompt_text = "\n\n".join(str(message.content) for message in messages)
        self.assertIn(balanced, prompt_text)
        self.assertIn("7. ", prompt_text)

    def test_balanced_story_drafting_string_appears_in_rendered_prompt(self) -> None:
        balanced = prompt_catalog.PROMPT_PROFILES["balanced"].prompts["story_drafting"]
        messages = build_story_synthesis_prompt_messages(_story_block(), "May 30, 2026")
        prompt_text = "\n\n".join(str(message.content) for message in messages)
        self.assertIn(balanced, prompt_text)
        self.assertIn("Headline:", prompt_text)
        self.assertIn("[[S1]]", prompt_text)

    def test_balanced_scale_screening_string_appears_in_rendered_prompt(self) -> None:
        balanced = prompt_catalog.PROMPT_PROFILES["balanced"].prompts["story_scale_screening"]
        messages = _global_scale_screening_prompt_messages([_story_block()])
        prompt_text = "\n\n".join(str(message.content) for message in messages)
        self.assertIn(balanced, prompt_text)
        self.assertIn("Return only valid JSON", prompt_text)

    def test_get_unknown_profile_error_lists_available(self) -> None:
        with self.assertRaisesRegex(ValueError, "Available profiles: .*balanced"):
            prompt_catalog.get_prompt_profile("nope")

    def test_resolve_instructions_defaults_to_balanced(self) -> None:
        resolved = prompt_catalog.resolve_prompt_instructions()
        self.assertEqual(resolved, prompt_catalog.DEFAULT_PROMPT_INSTRUCTIONS)
        self.assertEqual(
            prompt_catalog.resolve_prompt_instructions("playful"),
            prompt_catalog.PROMPT_PROFILES["playful"].prompts,
        )

    def test_compare_profiles(self) -> None:
        self.assertEqual(
            prompt_catalog.compare_prompt_profiles("balanced"),
            {},
        )
        diffs = prompt_catalog.compare_prompt_profiles("playful")
        self.assertIn("story_drafting", diffs)
        self.assertIn("playful", diffs["story_drafting"])
        self.assertIn("balanced:story_drafting", diffs["story_drafting"])

    def test_profile_ids_match_registry_keys(self) -> None:
        # Drift-guard: PROMPT_PROFILE_IDS feeds the config knob options (UI
        # selector); a profile added to only one registry would silently vanish
        # from the UI while remaining valid via env/CLI.
        self.assertEqual(
            set(prompt_catalog.PROMPT_PROFILE_IDS),
            set(prompt_catalog.PROMPT_PROFILES),
        )

    def test_scale_screening_guidance_is_format_safe(self) -> None:
        # story_selection renders the scale-screening prompt via
        # textwrap.dedent(...).format(screening_guidance=...); a brace in any
        # profile's guidance would raise KeyError/ValueError at prompt-build
        # time, crashing the pipeline mid-run. Converting the data mistake into
        # a failing unit test at catalog-review time instead.
        for profile in prompt_catalog.PROMPT_PROFILES.values():
            guidance = profile.prompts["story_scale_screening"]
            self.assertNotIn("{", guidance, f"{profile.id} guidance would break .format()")
            self.assertNotIn("}", guidance, f"{profile.id} guidance would break .format()")

    def test_protocol_tasks_match_prompt_tasks(self) -> None:
        # Drift-guard: the contract registry must cover exactly the same five
        # stages the Prompt Catalog editorializes; a stage added to only one
        # side would silently skip contract validation.
        self.assertEqual(prompt_contracts.PROTOCOL_TASKS, prompt_catalog.PROMPT_TASKS)
        self.assertEqual(
            set(prompt_contracts.PROTOCOL_MARKERS),
            set(prompt_catalog.PROMPT_TASKS),
        )

    def test_all_profiles_render_all_prompt_contracts(self) -> None:
        # Every built-in profile must render every stage with its full machine
        # contract intact; a template edit that drops a protocol marker fails
        # here regardless of which profile is active.
        for profile in prompt_catalog.PROMPT_PROFILES.values():
            summary_messages = build_article_summary_prompt_messages(
                {"title": "Flood plan expands", "source": "Fixture Wire"},
                "May 30, 2026",
                replace(
                    _article_summarization_runtime(),
                    prompt_instructions=profile.prompts["article_summary"],
                ),
            )
            prompt_contracts.assert_prompt_contract(
                "article_summary",
                "\n\n".join(str(message.content) for message in summary_messages),
            )

            screening_messages = _global_scale_screening_prompt_messages(
                [_story_block()],
                prompt_instructions=profile.prompts["story_scale_screening"],
            )
            prompt_contracts.assert_prompt_contract(
                "story_scale_screening",
                "\n\n".join(str(message.content) for message in screening_messages),
            )

            drafting_messages = build_story_synthesis_prompt_messages(
                _story_block(),
                "May 30, 2026",
                prompt_instructions=profile.prompts["story_drafting"],
            )
            prompt_contracts.assert_prompt_contract(
                "story_drafting",
                "\n\n".join(str(message.content) for message in drafting_messages),
            )

            art_system_prompt = pipeline._build_image_art_system_prompt(
                profile.prompts["image_art_direction"],
                profile.prompts["title_generation"],
            )
            prompt_contracts.assert_prompt_contract("image_art_direction", art_system_prompt)
            prompt_contracts.assert_prompt_contract("title_generation", art_system_prompt)

    def test_validate_prompt_contract_reports_missing_markers(self) -> None:
        missing = prompt_contracts.validate_prompt_contract("story_drafting", "no markers here")
        self.assertIn("Headline:", missing)
        with self.assertRaisesRegex(ValueError, "missing markers:.*Headline"):
            prompt_contracts.assert_prompt_contract("story_drafting", "no markers here")
        with self.assertRaisesRegex(ValueError, "Unknown prompt task"):
            prompt_contracts.validate_prompt_contract("no_such_task", "anything")

    def test_validate_editorial_instructions_accepts_builtin_profiles(self) -> None:
        # Vocabulary overlap (image_prompt, overlay_headline,
        # obviously_small_scale) is not a violation; only strong contract
        # sentences are blocked.
        for profile in prompt_catalog.PROMPT_PROFILES.values():
            self.assertEqual(
                prompt_contracts.validate_editorial_instructions(profile.prompts),
                [],
                f"{profile.id} instructions must not violate the output contracts",
            )

    def test_validate_editorial_instructions_rejects_violations(self) -> None:
        clean = {
            "article_summary": "Summarize factually.",
            "story_scale_screening": "Be conservative.",
            "story_drafting": "Write a factual story.",
            "title_generation": "Keep it short.",
            "image_art_direction": "Depict the event.",
        }
        self.assertEqual(prompt_contracts.validate_editorial_instructions(clean), [])

        violating = dict(clean)
        violating["story_drafting"] = "Do not use [[S1]] markers"
        violating["story_scale_screening"] = "Return {json} here"
        del violating["title_generation"]
        violations = prompt_contracts.validate_editorial_instructions(violating)
        self.assertTrue(
            any("missing or empty instructions for title_generation" in item for item in violations)
        )
        self.assertTrue(any("brace" in item for item in violations))
        self.assertTrue(
            any("story_drafting" in item and "[[S1]]" in item for item in violations)
        )


if __name__ == "__main__":
    unittest.main()
