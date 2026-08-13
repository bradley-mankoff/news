"""Prompt Catalog integrity and drift-guard tests.

The drift-guard tests assert that the `balanced` profile strings appear
verbatim in the rendered prompts of each stage module, and golden snapshot
tests assert the exact rendered bytes (ADR 0012), so the default behavior
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

    def test_balanced_article_summary_prompt_is_byte_identical(self) -> None:
        # Golden byte-identity snapshot: the rendered prompts must stay
        # byte-identical to the pre-catalog prompts (ADR 0012). Substring
        # checks alone let whitespace drift through (dedent-margin collapse),
        # so lock the exact bytes the LLM receives.
        messages = build_article_summary_prompt_messages(
            {"title": "Flood plan expands", "source": "Fixture Wire"},
            "May 30, 2026",
            _article_summarization_runtime(),
        )
        self.assertEqual(
            str(messages[0].content),
            """Today: May 30, 2026.
Current Task: Summarize one preselected article from the last 24 hours
for story discovery, selection, and synthesis.
1. Use only the provided article metadata, URL, description, and article text.
2. Do not call tools in this step.
3. Ignore outlet style and focus on concrete reported claims.
4. Include key facts: what reportedly happened, where, timeline, named actors, casualties or damage if reported, and what remains unconfirmed.
5. If the article text is thin, summarize only what is actually supported by the provided text and metadata.
6. Do not recap the general history of a longstanding subject or conflict; include background only
   when the article reports a new fact about it or one short clause is needed for orientation.
7. Prioritize facts that help later clustering and story synthesis; include major concrete developments without inventing relevance.
8. Start your response with 'DATABASE_ENTRY:' and then exactly the requested Markdown block.
9. Do not include any text before 'DATABASE_ENTRY:' or after the summary.""",
        )
        self.assertEqual(
            str(messages[1].content),
            """Selected article:

Title: Flood plan expands
Source: Fixture Wire
Published: Unknown publish time
URL: N/A
Description: N/A
Article text:
N/A

Return exactly this block, replacing only the summary text:

DATABASE_ENTRY:
### Flood plan expands
Metadata:
- Source: Fixture Wire
- Published: Unknown publish time
- URL: N/A

Summary:
<4-7 sentence article summary in plain prose, no brackets>""",
        )

    def test_balanced_story_drafting_prompts_are_byte_identical(self) -> None:
        # Golden byte-identity snapshot (ADR 0012). Note the historical
        # layout: the source block's first line renders at the 8-space
        # placeholder position while its remaining lines sit at column 0, and
        # the output-contract block renders 8-space indented.
        messages = build_story_synthesis_prompt_messages(_story_block(), "May 30, 2026")
        self.assertEqual(
            str(messages[0].content),
            """Today: May 30, 2026.
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
Prioritize the headline, lede, and details around the central event supported by the supplied source summaries and evidence.
Lead with today's reported development. Include concrete reported claims, named actors,
places, timing, figures, damage, statements, deadlines, and uncertainty when supported.
Then assess whether the sources directly or materially contradict each other.
A reportable contradiction is a factual disagreement about the same claim, count,
timeline, attribution, status, quote, or outcome where the cited accounts cannot
both be true in the same context. Do not require identical wording.
Omission, different focus, routine updates over time, or one source addressing a
subject another source does not address is not a contradiction.
If there is no direct or material factual contradiction, write exactly 'NONE' for Contradictions.
If there is a contradiction, write 1-3 concise prose sentences under Contradictions.
Each contradiction sentence must cite the disagreeing sources and must use the cleaned article evidence,
not only the source summaries.
Do not write bullets, source-material notes, methodology, bibliography, or preamble.
Do not merge in background material unless a source summary reports it as part of today's update.""",
        )
        self.assertEqual(
            str(messages[1].content),
            """Story: Storm damage

        Source summaries and cleaned article evidence to paraphrase, not quote:
        S1:
Title: Fixture article S1
Article ID: s1
Source: Fixture Wire
Published: Sat, 16 May 2026 15:30:00 GMT
URL: https://example.com/s1
Summary: Officials reported storm damage.
Cleaned article evidence to paraphrase, not quote: N/A
Citation precedence: Cite this source only for facts it directly supports.

        Return exactly this format:
        Headline: <custom story headline>
        Main story: <story paragraph with sentence-end source markers>
        Contradictions: NONE

        Or, only if there is a real direct or material contradiction:
        Headline: <custom story headline>
        Main story: <story paragraph with sentence-end source markers>
        Contradictions: <short contradiction evidence paragraph with sentence-end source markers>""",
        )

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

    def test_override_env_vars_cover_all_tasks(self) -> None:
        # Drift-guard: PROMPT_TASK_OVERRIDE_ENV_VARS feeds the config knobs and
        # is mirrored by the JS PROMPT_OVERRIDE_ENVS map in ui.py; a task added
        # to only one side would silently lose its editor or its env wiring.
        self.assertEqual(
            set(prompt_catalog.PROMPT_TASK_OVERRIDE_ENV_VARS),
            set(prompt_catalog.PROMPT_TASKS),
        )
        for task, env_var in prompt_catalog.PROMPT_TASK_OVERRIDE_ENV_VARS.items():
            self.assertTrue(
                env_var.startswith(prompt_catalog.PROMPT_OVERRIDE_ENV_PREFIX),
                f"{task} env var {env_var} lacks the override prefix",
            )
            self.assertEqual(
                env_var,
                f"{prompt_catalog.PROMPT_OVERRIDE_ENV_PREFIX}{task.upper()}",
            )

    def test_resolve_instructions_applies_overrides(self) -> None:
        playful = prompt_catalog.PROMPT_PROFILES["playful"].prompts
        resolved = prompt_catalog.resolve_prompt_instructions(
            "playful", {"article_summary": "Custom text"}
        )
        self.assertEqual(resolved["article_summary"], "Custom text")
        self.assertEqual(resolved["story_drafting"], playful["story_drafting"])
        # Empty and whitespace-only overrides are ignored.
        for empty in ("", "   "):
            self.assertEqual(
                prompt_catalog.resolve_prompt_instructions(
                    "playful", {"article_summary": empty}
                ),
                playful,
            )
        # Unknown task keys are ignored (never injected into PROMPT_INSTRUCTIONS).
        self.assertEqual(
            prompt_catalog.resolve_prompt_instructions(
                "playful", {"not_a_task": "Bogus", "article_summary": "Custom"}
            ),
            {**playful, "article_summary": "Custom"},
        )
        # overrides=None returns the profile unchanged.
        self.assertEqual(prompt_catalog.resolve_prompt_instructions("playful"), playful)

    def test_scale_screening_override_with_braces_renders_safely(self) -> None:
        # User-entered override text may contain literal braces; the screening
        # prompt is rendered via str.format(), so braces must be escaped before
        # injection and unescaped after (regression for the story_selection.py
        # hardening). Rendered text must stay byte-identical to the user input.
        for guidance in (
            "Screen {these} braces",
            "balanced {and} {{nested}} braces }}",
            "no braces at all",
        ):
            messages = _global_scale_screening_prompt_messages(
                [_story_block()],
                prompt_instructions=guidance,
            )
            prompt_text = "\n\n".join(str(message.content) for message in messages)
            self.assertIn(guidance, prompt_text)

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
            )
            title_system_prompt = pipeline._build_title_generation_system_prompt(
                profile.prompts["title_generation"],
            )
            prompt_contracts.assert_prompt_contract("image_art_direction", art_system_prompt)
            prompt_contracts.assert_prompt_contract("title_generation", title_system_prompt)
            # Marker-only drift guard is vacuous for these two tasks: every
            # marker lives in the unconditionally-injected JSON contract, so a
            # template edit that drops the {image_art_direction}/{title_guidance}
            # interpolation would pass the checks above. Require the profile's
            # own editorial sentences to be present in the rendered prompt AND
            # isolated to their own call (issue #122 split).
            self.assertIn(profile.prompts["image_art_direction"], art_system_prompt)
            self.assertIn(profile.prompts["title_generation"], title_system_prompt)
            self.assertNotIn(profile.prompts["title_generation"], art_system_prompt)
            self.assertNotIn(profile.prompts["image_art_direction"], title_system_prompt)

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
        self.assertEqual(
            prompt_contracts.validate_editorial_instructions(
                {**clean, "story_scale_screening": "Use {literal} guidance."},
                allow_braces_for={"story_scale_screening"},
            ),
            [],
        )

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

    def test_validate_editorial_instructions_rejects_non_string_values(self) -> None:
        # Untrusted YAML values are reported as violations instead of leaking
        # a raw TypeError from substring checks.
        clean = {
            "article_summary": "Summarize factually.",
            "story_scale_screening": "Be conservative.",
            "story_drafting": "Write a factual story.",
            "title_generation": "Keep it short.",
            "image_art_direction": "Depict the event.",
        }
        non_string = dict(clean)
        non_string["story_drafting"] = 42
        violations = prompt_contracts.validate_editorial_instructions(non_string)
        self.assertTrue(
            any("instructions for story_drafting must be a string" in item for item in violations)
        )
        self.assertFalse(any("TypeError" in item for item in violations))

    def test_validate_editorial_instructions_allow_braces_for_screening_only(self) -> None:
        clean = {
            "article_summary": "Summarize factually.",
            "story_scale_screening": "Screen {these} braces safely",
            "story_drafting": "Write a factual story.",
            "title_generation": "Keep it short.",
            "image_art_direction": "Depict the event.",
        }
        # The default validator still rejects the brace text.
        self.assertTrue(
            any(
                "brace" in item
                for item in prompt_contracts.validate_editorial_instructions(clean)
            )
        )
        # The explicit story-scale exception (its renderer escapes braces)
        # accepts the same text.
        self.assertEqual(
            prompt_contracts.validate_editorial_instructions(
                clean,
                allow_braces_for={"story_scale_screening"},
            ),
            [],
        )
        # The exception is not a general relaxation: allowing braces for
        # another task does not let story_scale_screening keep its braces.
        other_allow = dict(clean)
        other_allow["story_drafting"] = "Use {curly} braces here"
        violations = prompt_contracts.validate_editorial_instructions(
            other_allow,
            allow_braces_for={"story_drafting"},
        )
        # story_scale_screening braces are still rejected even though another
        # task was allowed.
        self.assertTrue(
            any("story_scale_screening" in item and "brace" in item for item in violations)
        )
        # A brace in a non-screening task is not itself a brace violation
        # (only the .format()-rendered screening slot checks braces), and the
        # drafting brace text stays accepted when explicitly allowed.
        allowed = prompt_contracts.validate_editorial_instructions(
            other_allow,
            allow_braces_for={"story_scale_screening", "story_drafting"},
        )
        self.assertFalse(any("brace" in item for item in allowed))

    def test_resolve_instructions_preserves_profile_when_one_task_overridden(self) -> None:
        # The primitive config relies on: a single YAML override replaces one
        # task and all other instructions stay from the selected profile.
        playful = prompt_catalog.PROMPT_PROFILES["playful"].prompts
        resolved = prompt_catalog.resolve_prompt_instructions(
            "playful", {"article_summary": "YAML text"}
        )
        self.assertEqual(resolved["article_summary"], "YAML text")
        for task in (
            "story_scale_screening",
            "story_drafting",
            "title_generation",
            "image_art_direction",
        ):
            self.assertEqual(resolved[task], playful[task])


if __name__ == "__main__":
    unittest.main()
