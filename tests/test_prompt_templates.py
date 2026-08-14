"""Prompt template catalog/parser/validator tests (ADR 0015).

Covers the stdlib-only full-template primitive: the five task records and
derived env map, JSON override parsing, placeholder allowlists, required
placeholders, code-owned contract rendering, deterministic contract errors,
literal-dollar handling, and custom/default resolution. Stage-level byte
identity is guarded by tests/test_prompt_catalog.py; config/preset/UI wiring
is guarded by tests/test_runtime_config_resolution.py and tests/test_ui.py.
"""

from __future__ import annotations

import json
import unittest

from news_pipeline import prompt_contracts, prompt_templates
from news_pipeline.prompt_catalog import PROMPT_TASKS
from news_pipeline.prompt_templates import (
    DEFAULT_PROMPT_TEMPLATES,
    PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS,
    PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS,
    PROMPT_TEMPLATE_ENV_PREFIX,
    PROMPT_TEMPLATE_ENV_VARS,
    PromptTemplate,
    assert_valid_prompt_template,
    list_prompt_templates,
    parse_prompt_template_override,
    render_prompt_template,
    resolve_prompt_templates,
    validate_prompt_template,
)


def _valid_body(task: str) -> dict[str, str]:
    default = DEFAULT_PROMPT_TEMPLATES[task]
    return {"system": default.system, "user": default.user}


class PromptTemplateCatalogTests(unittest.TestCase):
    def test_env_vars_cover_exactly_the_canonical_tasks(self) -> None:
        self.assertEqual(set(PROMPT_TEMPLATE_ENV_VARS), set(PROMPT_TASKS))
        for task, env_var in PROMPT_TEMPLATE_ENV_VARS.items():
            self.assertTrue(env_var.startswith(PROMPT_TEMPLATE_ENV_PREFIX))
            self.assertEqual(
                env_var,
                f"{PROMPT_TEMPLATE_ENV_PREFIX}{task.upper()}",
            )

    def test_default_records_exist_for_all_five_tasks(self) -> None:
        self.assertEqual(set(DEFAULT_PROMPT_TEMPLATES), set(PROMPT_TASKS))
        for task, template in DEFAULT_PROMPT_TEMPLATES.items():
            self.assertIsInstance(template, PromptTemplate)
            self.assertEqual(template.task, task)
            self.assertTrue(template.label)
            self.assertTrue(template.system.strip())
            self.assertTrue(template.user.strip())
            self.assertTrue(template.required_placeholders)
            self.assertIn("editorial_instructions", template.optional_placeholders)

    def test_placeholder_maps_cover_exactly_the_canonical_tasks(self) -> None:
        for registry in (
            PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS,
            PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS,
        ):
            self.assertEqual(set(registry), set(PROMPT_TASKS))
            for task, placeholders in registry.items():
                for name in placeholders:
                    self.assertRegex(name, r"^[a-z][a-z0-9_]*$")

    def test_required_placeholders_match_the_documented_contract(self) -> None:
        expected_dynamic = {
            "article_summary": ("now_label", "recent_window_hours", "article_payload"),
            "story_scale_screening": ("story_blocks",),
            "story_drafting": ("now_label", "story_title", "source_summary_lines"),
            "title_generation": ("report_title", "synthesis_body"),
            "image_art_direction": ("synthesis_body",),
        }
        expected_contract = {
            "article_summary": ("output_contract",),
            "story_scale_screening": ("scale_contract",),
            "story_drafting": ("citation_contract", "output_contract"),
            "title_generation": ("title_contract", "overlay_protocol"),
            "image_art_direction": ("image_contract",),
        }
        self.assertEqual(PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS, expected_dynamic)
        self.assertEqual(PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS, expected_contract)

    def test_story_discovery_is_not_a_template_task(self) -> None:
        self.assertNotIn("story_discovery", PROMPT_TEMPLATE_ENV_VARS)
        self.assertNotIn("story_discovery", DEFAULT_PROMPT_TEMPLATES)
        for task in PROMPT_TASKS:
            self.assertNotIn("story_discovery", task)

    def test_list_prompt_templates_matches_defaults_and_schema_shape(self) -> None:
        records = list_prompt_templates()
        self.assertEqual([record["task"] for record in records], list(PROMPT_TASKS))
        for record in records:
            task = record["task"]
            self.assertEqual(record["env_var"], PROMPT_TEMPLATE_ENV_VARS[task])
            self.assertEqual(record["system"], DEFAULT_PROMPT_TEMPLATES[task].system)
            self.assertEqual(record["user"], DEFAULT_PROMPT_TEMPLATES[task].user)
            self.assertEqual(
                set(record["required_placeholders"]),
                set(DEFAULT_PROMPT_TEMPLATES[task].required_placeholders),
            )
            self.assertIn(
                "editorial_instructions", record["placeholder_descriptions"]
            )


class ParsePromptTemplateOverrideTests(unittest.TestCase):
    def test_accepts_minimal_valid_json(self) -> None:
        raw = json.dumps({"system": "Sys $now_label", "user": "Usr $article_payload"})
        body = parse_prompt_template_override("article_summary", raw, source="env test")
        self.assertEqual(body["system"], "Sys $now_label")
        self.assertEqual(body["user"], "Usr $article_payload")

    def test_rejects_malformed_json(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_prompt_template_override("article_summary", "{not json", source="env test")
        message = str(ctx.exception)
        self.assertIn("env test", message)
        self.assertIn("article_summary", message)
        self.assertIn("not valid JSON", message)

    def test_rejects_unknown_task(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_prompt_template_override("story_discovery", "{}", source="env test")
        self.assertIn("story_discovery", str(ctx.exception))

    def test_rejects_non_object_json(self) -> None:
        for raw in ("[]", '"text"', "42"):
            with self.assertRaises(ValueError):
                parse_prompt_template_override("story_drafting", raw, source="env test")

    def test_rejects_unknown_keys(self) -> None:
        raw = json.dumps({"system": "s", "user": "u", "systemPrompt": "x"})
        with self.assertRaises(ValueError) as ctx:
            parse_prompt_template_override("story_drafting", raw, source="env test")
        self.assertIn("systemPrompt", str(ctx.exception))

    def test_rejects_missing_or_non_string_roles(self) -> None:
        for raw in (
            json.dumps({"system": "s"}),
            json.dumps({"user": "u"}),
            json.dumps({"system": 3, "user": "u"}),
            json.dumps({"system": "s", "user": []}),
            json.dumps({"system": "  ", "user": "u"}),
        ):
            with self.assertRaises(ValueError):
                parse_prompt_template_override("title_generation", raw, source="env test")


class ValidatePromptTemplateTests(unittest.TestCase):
    def test_default_bodies_validate_for_every_task(self) -> None:
        for task in PROMPT_TASKS:
            self.assertEqual(validate_prompt_template(task, _valid_body(task)), [])
            assert_valid_prompt_template(task, _valid_body(task))

    def test_empty_roles_are_rejected(self) -> None:
        for task in PROMPT_TASKS:
            violations = validate_prompt_template(
                task, {"system": " ", "user": "u"}
            )
            self.assertIn("'system' must be a non-empty string", violations)
            violations = validate_prompt_template(
                task, {"system": "s", "user": ""}
            )
            self.assertIn("'user' must be a non-empty string", violations)

    def test_unknown_placeholder_is_rejected(self) -> None:
        body = _valid_body("article_summary")
        body = {**body, "system": body["system"] + "\nUse $bogus_value."}
        violations = validate_prompt_template("article_summary", body)
        self.assertTrue(any("unknown placeholder" in v and "bogus_value" in v for v in violations))

    def test_malformed_dollar_syntax_is_rejected(self) -> None:
        body = {**_valid_body("story_drafting"), "system": "Today: $now_label. $1broken"}
        violations = validate_prompt_template("story_drafting", body)
        self.assertTrue(any("malformed placeholder" in v for v in violations))

    def test_missing_required_dynamic_placeholder_is_rejected(self) -> None:
        body = {**_valid_body("story_drafting"), "user": "No story title here."}
        violations = validate_prompt_template("story_drafting", body)
        self.assertTrue(any("missing required placeholder" in v and "story_title" in v for v in violations))

    def test_missing_required_contract_placeholder_is_rejected(self) -> None:
        body = {**_valid_body("image_art_direction"), "system": "Just art."}
        violations = validate_prompt_template("image_art_direction", body)
        self.assertTrue(any("missing required placeholder" in v and "image_contract" in v for v in violations))

    def test_contract_markers_cannot_be_dropped(self) -> None:
        # A template that keeps every placeholder but renders without the
        # pipeline-owned markers must fail, even when the source is valid.
        body = {
            "system": "$image_contract",
            "user": "No markers in this custom user text: $synthesis_body",
        }
        # The system half still carries the markers, so this variant passes;
        # now strip the contract placeholder value by dropping the placeholder
        # entirely from a different role pair.
        violations = validate_prompt_template("image_art_direction", body)
        self.assertEqual(violations, [])
        body = {
            "system": "Describe the image. $editorial_instructions",
            "user": "$synthesis_body",
        }
        violations = validate_prompt_template("image_art_direction", body)
        self.assertTrue(
            any("missing required placeholder" in v and "image_contract" in v for v in violations)
        )

    def test_literal_dollar_uses_double_dollar(self) -> None:
        body = {**_valid_body("article_summary"), "system": _valid_body("article_summary")["system"] + "\nCost: $$5 value."}
        self.assertEqual(validate_prompt_template("article_summary", body), [])
        template = DEFAULT_PROMPT_TEMPLATES["article_summary"]
        # Render path: values never re-parse; a $$ in custom text stays a $.
        custom = PromptTemplate(
            task="article_summary",
            label="custom",
            system=(
                "$now_label $recent_window_hours $article_payload\n"
                "Price: $$10\n$output_contract"
            ),
            user="$article_payload",
            required_placeholders=("article_payload", "output_contract", "now_label", "recent_window_hours"),
            optional_placeholders=("editorial_instructions",),
        )
        system_text, _user_text = render_prompt_template(
            "article_summary",
            custom,
            {
                "now_label": "2026-08-14",
                "recent_window_hours": "24",
                "article_payload": (
                    "Return exactly this block, replacing only the summary text:\n\n"
                    "DATABASE_ENTRY:\n### T\nMetadata:\n- Source: X\n- Published: 2026\n"
                    "- URL: u\n\nSummary:\nBody."
                ),
                "output_contract": prompt_contracts.ARTICLE_SUMMARY_OUTPUT_CONTRACT,
            },
        )
        self.assertIn("Price: $10", system_text)

    def test_editorial_instructions_are_optional(self) -> None:
        body = {**_valid_body("story_drafting")}
        # Replace the editorial placeholder with plain text: still valid as
        # long as every required placeholder remains.
        body["system"] = body["system"].replace(
            "$editorial_instructions\n", "Custom structure.\n"
        )
        violations = validate_prompt_template("story_drafting", body)
        self.assertEqual(violations, [])

    def test_unknown_task_validation_is_deterministic(self) -> None:
        violations = validate_prompt_template("story_discovery", {"system": "s", "user": "u"})
        self.assertEqual(len(violations), 1)
        self.assertIn("story_discovery", violations[0])


class RenderPromptTemplateTests(unittest.TestCase):
    def _values(self, task: str) -> dict[str, str]:
        if task == "article_summary":
            return {
                "now_label": "August 14, 2026",
                "recent_window_hours": "24",
                "article_payload": (
                    "Selected article:\n\nTitle: T\nSource: S\nPublished: P\nURL: U\n"
                    "Description: D\nArticle text:\nBody\n\n"
                    "Return exactly this block, replacing only the summary text:\n\n"
                    "DATABASE_ENTRY:\n### T\nMetadata:\n- Source: S\n- Published: P\n"
                    "- URL: U\n\nSummary:\nBody."
                ),
                "output_contract": prompt_contracts.ARTICLE_SUMMARY_OUTPUT_CONTRACT,
                "editorial_instructions": "Balanced guidance.",
            }
        if task == "story_scale_screening":
            return {
                "story_blocks": "Story key: k\nStory title: T\nStory draft: D\nArticle summaries:\n- S",
                "scale_contract": prompt_contracts.STORY_SCALE_SCREENING_JSON_CONTRACT,
                "editorial_instructions": "Be conservative.",
            }
        if task == "story_drafting":
            return {
                "now_label": "August 14, 2026",
                "story_title": "Sample story",
                "source_summary_lines": "S1:\nTitle: A\nArticle ID: a1\nSource: W\nPublished: P\nURL: U\nSummary: Sum.\nCleaned article evidence to paraphrase, not quote: Ev.\nCitation precedence: Direct.",
                "citation_contract": prompt_contracts.STORY_DRAFTING_CITATION_CONTRACT,
                "output_contract": prompt_contracts.STORY_DRAFTING_OUTPUT_CONTRACT,
                "editorial_instructions": "Balanced guidance.",
            }
        if task == "title_generation":
            return {
                "report_title": "Sample report",
                "synthesis_body": "Final output body.",
                "title_contract": prompt_contracts.TITLE_GENERATION_JSON_CONTRACT,
                "overlay_protocol": prompt_contracts.IMAGE_ART_OVERLAY_PROTOCOL,
                "editorial_instructions": "Punchy title.",
            }
        return {
            "synthesis_body": "Final output body.",
            "image_contract": prompt_contracts.IMAGE_ART_JSON_CONTRACT,
            "editorial_instructions": "Documentary photo.",
        }

    def test_default_templates_render_and_pass_contracts(self) -> None:
        for task in PROMPT_TASKS:
            system_text, user_text = render_prompt_template(
                task, DEFAULT_PROMPT_TEMPLATES[task], self._values(task)
            )
            self.assertTrue(system_text.strip())
            self.assertTrue(user_text.strip())
            self.assertEqual(
                prompt_contracts.validate_prompt_contract(
                    task, f"{system_text}\n\n{user_text}"
                ),
                [],
            )

    def test_custom_template_renders_with_dynamic_values(self) -> None:
        custom = PromptTemplate(
            task="article_summary",
            label="Custom",
            system=(
                "You summarize news. Today: $now_label. Window: $recent_window_hours hours.\n"
                "$output_contract"
            ),
            user="INPUT: $article_payload",
            required_placeholders=(
                "now_label",
                "recent_window_hours",
                "article_payload",
                "output_contract",
            ),
            optional_placeholders=("editorial_instructions",),
        )
        values = self._values("article_summary")
        system_text, user_text = render_prompt_template("article_summary", custom, values)
        self.assertIn("Today: August 14, 2026.", system_text)
        self.assertIn("Window: 24 hours.", system_text)
        self.assertIn("INPUT: Selected article:", user_text)
        self.assertIn("DATABASE_ENTRY:", system_text)

    def test_render_rejects_missing_values(self) -> None:
        values = self._values("story_drafting")
        del values["story_title"]
        with self.assertRaises(ValueError) as ctx:
            render_prompt_template(
                "story_drafting", DEFAULT_PROMPT_TEMPLATES["story_drafting"], values
            )
        self.assertIn("story_title", str(ctx.exception))

    def test_render_rejects_unknown_placeholder(self) -> None:
        bad = PromptTemplate(
            task="title_generation",
            label="bad",
            system="$title_contract $editorial_instructions $overlay_protocol $nope",
            user="$report_title\n$synthesis_body",
            required_placeholders=("report_title", "synthesis_body", "title_contract", "overlay_protocol"),
            optional_placeholders=("editorial_instructions",),
        )
        with self.assertRaises(ValueError) as ctx:
            render_prompt_template("title_generation", bad, self._values("title_generation"))
        self.assertIn("nope", str(ctx.exception))

    def test_render_contract_values_are_never_reparsed(self) -> None:
        # JSON braces in contract values must survive substitution untouched.
        values = self._values("story_scale_screening")
        system_text, _user_text = render_prompt_template(
            "story_scale_screening",
            DEFAULT_PROMPT_TEMPLATES["story_scale_screening"],
            values,
        )
        self.assertIn('"story_key":"..."', system_text)


class ResolvePromptTemplatesTests(unittest.TestCase):
    def test_no_overrides_returns_builtin_templates(self) -> None:
        resolved = resolve_prompt_templates()
        self.assertEqual(set(resolved), set(PROMPT_TASKS))
        for task in PROMPT_TASKS:
            self.assertIs(resolved[task], DEFAULT_PROMPT_TEMPLATES[task])

    def test_valid_override_wins_for_its_task_only(self) -> None:
        override = {
            "system": "Custom system: $synthesis_body\n$image_contract",
            "user": "Custom user: $synthesis_body",
        }
        resolved = resolve_prompt_templates({"image_art_direction": override})
        self.assertEqual(resolved["image_art_direction"].system, override["system"])
        self.assertEqual(resolved["image_art_direction"].user, override["user"])
        for task in PROMPT_TASKS:
            if task != "image_art_direction":
                self.assertIs(resolved[task], DEFAULT_PROMPT_TEMPLATES[task])

    def test_invalid_override_fails_closed(self) -> None:
        override = {"system": "Missing everything.", "user": "No placeholders."}
        with self.assertRaises(ValueError) as ctx:
            resolve_prompt_templates({"story_drafting": override})
        message = str(ctx.exception)
        self.assertIn("story_drafting", message)
        self.assertIn("missing required placeholder", message)


if __name__ == "__main__":
    unittest.main()
