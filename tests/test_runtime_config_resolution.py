from __future__ import annotations

import os
import tempfile
import unittest
import contextlib
import io
from datetime import datetime
from pathlib import Path
import textwrap
from unittest.mock import patch

from news_pipeline import prompt_catalog
from news_pipeline import ui as ui_module
from news_pipeline import config as config_module
from news_pipeline.config import (
    ACTIVE_PRESET_ENV_VAR,
    CODEX_TEST_MODEL_ALIAS,
    GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
    GEMMA_4_12B_IT_4BIT_MODEL_REPO,
    MODEL_TASK_ARTICLE_SUMMARY,
    MODEL_TASK_IMAGE_ART_DIRECTION,
    MODEL_TASK_STORY_DRAFTING,
    MODEL_TASK_STORY_SCALE_SCREENING,
    MODEL_TASK_TITLE_GENERATION,
    QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
    ModelSamplingSettings,
    ModelServerSettings,
    PRESET_ENV_VAR,
    RuntimeConfigRequest,
    load_model_tuning_presets,
    load_runtime_config,
    resolve_runtime_config,
)
from news_pipeline.prompt_catalog import PromptProfile
from news_pipeline.ui import build_command, preview_payload, schema_payload


class RuntimeConfigResolutionTests(unittest.TestCase):
    def test_resolver_does_not_mutate_process_environment(self) -> None:
        previous = dict(os.environ)

        resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={},
                preset_id="dev",
                overrides={"NEWS_MODEL": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(dict(os.environ), previous)

    def test_preset_base_env_and_overrides_have_documented_precedence(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={"NEWS_MODEL": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS},
                preset_id="dev",
                overrides={"NEWS_SOURCE_SCOPE": "peripheral"},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(resolution.config.preset_id, "dev")
        self.assertEqual(resolution.config.model_reference, GEMMA_4_12B_IT_4BIT_MODEL_ALIAS)
        self.assertEqual(resolution.config.model_name, GEMMA_4_12B_IT_4BIT_MODEL_REPO)
        self.assertEqual(resolution.config.model_backend, "mlx-vlm")
        self.assertIn("python -m mlx_vlm.server", resolution.config.model_server_command)
        self.assertEqual(resolution.config.source_scope, "peripheral")
        self.assertEqual(resolution.config.recipient_scope, "primary")
        self.assertEqual(resolution.command_env_delta["NEWS_PRESET"], "dev")
        self.assertEqual(resolution.command_env_delta["NEWS_SOURCE_SCOPE"], "peripheral")
        self.assertNotIn("NEWS_RECIPIENT_SCOPE", resolution.command_env_delta)

    def test_default_model_resolves_to_launchable_gemma_4_12b(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={},
            materialize_outputs=False,
            run_started_at=datetime(2026, 6, 14, 12, 0, 0),
        )
        # Default alias, resolved reference, backend.
        self.assertEqual(config.model_reference, "gemma-4-12b-it-4bit")
        self.assertEqual(config.model_name, "mlx-community/gemma-4-12B-it-4bit")
        self.assertEqual(config.model_backend, "mlx-vlm")
        # Generated server command uses the repo id, never a file-qualified path.
        self.assertIn("--model mlx-community/gemma-4-12B-it-4bit", config.model_server_command)
        self.assertNotIn(".gguf", config.model_server_command)
        # One default model across all four LLM stages.
        for task in ("article_summary", "story_drafting", "story_scale_screening", "title_generation"):
            self.assertEqual(config.model_assignments[task].reference, "gemma-4-12b-it-4bit")
        # No translation assignment; only default + the four stages.
        self.assertEqual(
            set(config.model_assignments),
            {"default", "article_summary", "story_drafting",
             "story_scale_screening", "title_generation"},
        )

    def test_qwythos_aliases_fail_fast_with_actionable_error(self) -> None:
        # The error message must name the replacement so stale configs are
        # self-serviceable (issue acceptance criterion).
        with self.assertRaisesRegex(
            ValueError,
            r"Unsupported model reference: qwythos-9b-8bit.*gemma-4-12b-it-4bit",
        ):
            load_runtime_config(
                environ={},
                overrides={"NEWS_MODEL": "qwythos-9b-8bit"},
                materialize_outputs=False,
            )

    def test_raw_qwythos_gguf_references_fail_fast_too(self) -> None:
        # Raw owner/repo/file.gguf references and their URL forms (the values
        # the old SETTINGS.md published as "Resolved model") fail fast like
        # the aliases instead of half-starting an mlx-vlm server (issue #124).
        for stale in (
            QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
            f"https://huggingface.co/{QWWYTHOS_9B_4BIT_MODEL_REFERENCE}",
            f"https://hf.co/{QWWYTHOS_9B_4BIT_MODEL_REFERENCE}",
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
                load_runtime_config(
                    environ={},
                    overrides={"NEWS_MODEL": stale},
                    materialize_outputs=False,
                )

    def test_news_model_backend_external_override_for_default_model(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={
                    "NEWS_MODEL_BACKEND": "external",
                    "NEWS_MODEL_BASE_URL": "https://api.example.com/v1",
                },
                overrides={"NEWS_MODEL": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(resolution.config.model_backend, "external")
        self.assertEqual(resolution.config.model_server_command, "")
        self.assertEqual(resolution.config.model_assignments["default"].server_command, "")

    def test_news_model_backend_explicit_mlx_vlm_beats_inference(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={"NEWS_MODEL_BACKEND": "mlx-vlm"},
                overrides={"NEWS_MODEL": "gemma-e2b-tiny"},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(resolution.config.model_backend, "mlx-vlm")

    def test_news_model_backend_invalid_value_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "NEWS_MODEL_BACKEND must be one of: mlx-lm, mlx-vlm, external"):
            resolve_runtime_config(
                RuntimeConfigRequest(
                    base_env={"NEWS_MODEL_BACKEND": "bogus"},
                    materialize_outputs=False,
                )
            )

    def test_news_model_backend_is_case_and_whitespace_insensitive(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={
                    "NEWS_MODEL_BACKEND": " EXTERNAL ",
                    "NEWS_MODEL_BASE_URL": "https://api.example.com/v1",
                },
                materialize_outputs=False,
            )
        )

        self.assertEqual(resolution.config.model_backend, "external")

    def test_news_model_backend_external_requires_base_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "NEWS_MODEL_BACKEND=external requires NEWS_MODEL_BASE_URL"):
            resolve_runtime_config(
                RuntimeConfigRequest(
                    base_env={"NEWS_MODEL_BACKEND": "external"},
                    materialize_outputs=False,
                )
            )

    def test_preset_marker_overrides_do_not_desynchronize_resolution(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={},
                preset_id="dev",
                overrides={
                    PRESET_ENV_VAR: "prod",
                    ACTIVE_PRESET_ENV_VAR: "prod",
                    "NEWS_SOURCE_SCOPE": "peripheral",
                },
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(resolution.config.preset_id, "dev")
        self.assertEqual(resolution.effective_env[PRESET_ENV_VAR], "dev")
        self.assertEqual(resolution.effective_env[ACTIVE_PRESET_ENV_VAR], "dev")
        self.assertEqual(
            resolution.command_env_delta,
            {"NEWS_PRESET": "dev", "NEWS_SOURCE_SCOPE": "peripheral"},
        )

    def test_context_env_drives_runtime_config_derivations(self) -> None:
        config = load_runtime_config(
            environ={"NEWS_TOTAL_ARTICLE_SUMMARY_CAP": "55"},
            overrides={"NEWS_MODEL": "gemma-e2b-tiny"},
            materialize_outputs=False,
            run_started_at=datetime(2026, 6, 14, 12, 0, 0),
        )

        self.assertFalse(config.total_article_summary_cap_gemma_4_derived)

    def test_unknown_preset_error_lists_available_presets(self) -> None:
        with self.assertRaisesRegex(ValueError, "Available presets: .*dev"):
            resolve_runtime_config(
                RuntimeConfigRequest(
                    base_env={},
                    preset_id="missing",
                    materialize_outputs=False,
                )
            )

    def test_prompt_profile_env_resolves_into_runtime_config(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={"NEWS_PROMPT_PROFILE": "playful"},
            materialize_outputs=False,
        )

        self.assertEqual(config.prompt_profile_id, "playful")

    def test_prompt_profile_defaults_to_balanced_when_unset(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={},
            materialize_outputs=False,
        )

        self.assertEqual(config.prompt_profile_id, "balanced")

    def test_prompt_profile_empty_value_counts_as_unset(self) -> None:
        # NEWS_PROMPT_PROFILE= (a common "unset" idiom in .env files and
        # docker-compose) must not crash; it resolves to the default like the
        # CLI/UI paths.
        config = load_runtime_config(
            environ={},
            overrides={"NEWS_PROMPT_PROFILE": ""},
            materialize_outputs=False,
        )

        self.assertEqual(config.prompt_profile_id, "balanced")

    def test_unknown_prompt_profile_error_lists_available_profiles(self) -> None:
        with self.assertRaisesRegex(ValueError, "Available profiles: .*balanced"):
            load_runtime_config(
                environ={},
                overrides={"NEWS_PROMPT_PROFILE": "bogus"},
                materialize_outputs=False,
            )

    def test_prompt_override_envs_resolve_into_runtime_config(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={"NEWS_PROMPT_OVERRIDE_STORY_DRAFTING": "Write shorter."},
            materialize_outputs=False,
        )

        self.assertEqual(
            config.prompt_instruction_overrides,
            {"story_drafting": "Write shorter."},
        )

    def test_prompt_overrides_empty_and_whitespace_count_as_unset(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY": "",
                "NEWS_PROMPT_OVERRIDE_TITLE_GENERATION": "   ",
            },
            materialize_outputs=False,
        )

        self.assertEqual(config.prompt_instruction_overrides, {})
        unset = load_runtime_config(
            environ={},
            overrides={},
            materialize_outputs=False,
        )
        self.assertEqual(unset.prompt_instruction_overrides, {})
    def test_profile_violating_contracts_fails_config_resolution(self) -> None:
        # A profile whose editorial instructions contain pipeline-owned
        # contract language must fail fast at config resolution instead of
        # silently weakening the parsers/retries/citation renderers mid-run.
        # All other slots stay valid (balanced strings) so the failure is
        # specifically the blocklisted drafting sentence.
        bad_profile = PromptProfile(
            id="bad",
            name="Bad",
            description="Violates the drafting output contract.",
            prompts={
                **prompt_catalog.PROMPT_PROFILES["balanced"].prompts,
                "story_drafting": "Return exactly this format: and nothing else.",
            },
        )
        with patch(
            "news_pipeline.prompt_catalog.PROMPT_PROFILES",
            {**prompt_catalog.PROMPT_PROFILES, "bad": bad_profile},
        ):
            with self.assertRaisesRegex(ValueError, "violates pipeline-owned output contracts"):
                load_runtime_config(
                    environ={},
                    overrides={"NEWS_PROMPT_PROFILE": "bad"},
                    materialize_outputs=False,
                )

    def test_removed_topic_env_vars_reported_and_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "NEWS_TOPIC_IDS"):
            load_runtime_config(
                environ={},
                overrides={"NEWS_TOPIC_IDS": "legacy"},
                materialize_outputs=False,
            )

        preview = preview_payload(
            {"action": "run", "env": {"NEWS_TOPIC_IDS": "legacy"}}
        )
        self.assertEqual(preview["removed_topic_env_vars"], ["NEWS_TOPIC_IDS"])
        self.assertIn("NEWS_TOPIC_IDS", preview["runtime_error"])

    def test_story_runtime_knobs_resolve_into_runtime_config(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                "NEWS_STORY_SCALE_SCREENING_ENABLED": "0",
                "NEWS_MAX_STORIES": "7",
                "NEWS_STORY_SELECTION_OVERLAP_THRESHOLD": "0.4",
                "NEWS_STORY_DEDUP_THRESHOLD": "0.72",
                "NEWS_STORY_BACKFILL_BATCH_MULTIPLIER": "3",
                "NEWS_TOTAL_ARTICLE_SUMMARY_CAP": "55",
            },
            materialize_outputs=False,
        )

        self.assertFalse(config.story_scale_screening_enabled)
        self.assertEqual(config.max_stories, 7)
        self.assertEqual(config.story_selection_overlap_threshold, 0.4)
        self.assertEqual(config.story_embedding_dedup_threshold, 0.72)
        self.assertEqual(config.story_backfill_batch_multiplier, 3)
        self.assertFalse(config.total_article_summary_cap_gemma_4_derived)

    def test_task_model_assignments_can_diverge_from_default_model(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL": "http://127.0.0.1:8090/v1",
                "NEWS_MODEL_STORY_DRAFTING": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                "NEWS_MODEL_STORY_DRAFTING_BASE_URL": "http://127.0.0.1:8091/v1",
            },
            materialize_outputs=False,
        )

        self.assertEqual(config.model_assignments["default"].reference, CODEX_TEST_MODEL_ALIAS)
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].reference,
            GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].name,
            GEMMA_4_12B_IT_4BIT_MODEL_REPO,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].base_url,
            "http://127.0.0.1:8090/v1",
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_STORY_DRAFTING].base_url,
            "http://127.0.0.1:8091/v1",
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_STORY_DRAFTING].reference,
            GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
        )
        self.assertNotEqual(
            config.model_assignments["default"].reference,
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].reference,
        )
        self.assertNotEqual(
            config.model_assignments["default"].reference,
            config.model_assignments[MODEL_TASK_STORY_DRAFTING].reference,
        )

    def test_task_model_assignments_cover_all_four_llm_stages(self) -> None:
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                "NEWS_MODEL_STORY_SCALE_SCREENING": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL": "http://127.0.0.1:8090/v1",
                "NEWS_MODEL_TITLE_GENERATION": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                "NEWS_MODEL_TITLE_GENERATION_BASE_URL": "http://127.0.0.1:8091/v1",
            },
            materialize_outputs=False,
        )

        self.assertEqual(
            set(config.model_assignments),
            {
                "default",
                MODEL_TASK_ARTICLE_SUMMARY,
                MODEL_TASK_STORY_DRAFTING,
                MODEL_TASK_STORY_SCALE_SCREENING,
                MODEL_TASK_TITLE_GENERATION,
            },
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_STORY_SCALE_SCREENING].reference,
            GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_STORY_SCALE_SCREENING].name,
            GEMMA_4_12B_IT_4BIT_MODEL_REPO,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_TITLE_GENERATION].reference,
            GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
        )
        self.assertNotEqual(
            config.model_assignments["default"].reference,
            config.model_assignments[MODEL_TASK_STORY_SCALE_SCREENING].reference,
        )
        self.assertNotEqual(
            config.model_assignments["default"].reference,
            config.model_assignments[MODEL_TASK_TITLE_GENERATION].reference,
        )
        # Unset per-task env vars inherit the default reference and base URL.
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].reference,
            CODEX_TEST_MODEL_ALIAS,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].base_url,
            config.model_assignments["default"].base_url,
        )
        # image_art_direction inherits the title_generation token cap field.
        self.assertEqual(
            config_module._task_max_tokens_field(MODEL_TASK_IMAGE_ART_DIRECTION),
            "title_generation_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(MODEL_TASK_STORY_SCALE_SCREENING),
            "story_scale_screening_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(MODEL_TASK_TITLE_GENERATION),
            "title_generation_max_tokens",
        )

    def test_managed_default_rejects_different_task_model_on_shared_base_url(self) -> None:
        # Regression for #113: the default managed server serves one model;
        # a different task model on the same base URL must fail at config
        # resolution, before source collection or server startup.
        with self.assertRaisesRegex(
            ValueError,
            r"Managed model server cannot serve multiple different models "
            r"from the same base URL.*Set a per-task base URL",
        ):
            load_runtime_config(
                environ={},
                overrides={
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                },
                materialize_outputs=False,
            )

    def test_managed_default_rejects_task_model_on_trailing_slash_base_url(self) -> None:
        # Regression for #113: URL spelling variants (trailing slash) of the
        # shared base URL must still trip the early rejection instead of
        # falling through to the old mid-run failure.
        with self.assertRaisesRegex(ValueError, "Managed model server cannot serve"):
            load_runtime_config(
                environ={},
                overrides={
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL": "http://127.0.0.1:8080/v1/",
                },
                materialize_outputs=False,
            )

    def test_managed_default_rejects_task_model_on_localhost_base_url_alias(self) -> None:
        # Regression for #134: localhost and 127.0.0.1 are the same managed
        # endpoint; an alias spelling must still fail at config resolution
        # instead of falling through to the old mid-run failure.
        with self.assertRaisesRegex(ValueError, "Managed model server cannot serve"):
            load_runtime_config(
                environ={},
                overrides={
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL": "http://localhost:8080/v1",
                },
                materialize_outputs=False,
            )
        # Reverse direction: default base URL spelled as localhost while the
        # task override uses 127.0.0.1 must trip the same rejection.
        with self.assertRaisesRegex(ValueError, "Managed model server cannot serve"):
            load_runtime_config(
                environ={},
                overrides={
                    "NEWS_MODEL_BASE_URL": "http://localhost:8080/v1",
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                    "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL": "http://127.0.0.1:8080/v1",
                },
                materialize_outputs=False,
            )

    def test_external_backend_allows_different_task_models_on_shared_base_url(self) -> None:
        # External OpenAI-compatible endpoints can serve multiple models; the
        # managed-server restriction does not apply.
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_MODEL_BACKEND": "external",
                "NEWS_MODEL_BASE_URL": "https://api.example.com/v1",
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
            },
            materialize_outputs=False,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].base_url,
            config.model_base_url,
        )
        self.assertNotEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].name,
            config.model_name,
        )

    def test_same_task_model_on_shared_managed_base_url_resolves(self) -> None:
        # Explicitly naming the default model for a task stays legal on the
        # managed base URL (same model, same server).
        config = load_runtime_config(
            environ={},
            overrides={
                "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                "NEWS_MODEL_ARTICLE_SUMMARY": CODEX_TEST_MODEL_ALIAS,
            },
            materialize_outputs=False,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].name,
            config.model_name,
        )
        self.assertEqual(
            config.model_assignments[MODEL_TASK_ARTICLE_SUMMARY].base_url,
            config.model_base_url,
        )

    def test_default_recipient_and_sender_are_clean_example_addresses(self) -> None:
        config = load_runtime_config(
            environ={},
            materialize_outputs=False,
            run_started_at=datetime(2026, 6, 14, 12, 0, 0),
        )

        self.assertEqual(config.primary_recipient, "primary@example.com")
        self.assertEqual(config.email_from, "news@example.com")
        self.assertEqual(config.recipient_scope, "primary")
        # Exact-value asserts above already rule out personal data; these
        # negative asserts document that intent explicitly.
        self.assertNotIn("mankoff", config.primary_recipient)
        self.assertNotIn("bradley", config.primary_recipient)
        self.assertNotIn("gmail", config.email_from)

    def test_primary_recipient_env_override(self) -> None:
        config = load_runtime_config(
            environ={"NEWS_PRIMARY_RECIPIENT": "owner@example.com"},
            materialize_outputs=False,
            run_started_at=datetime(2026, 6, 14, 12, 0, 0),
        )
        self.assertEqual(config.primary_recipient, "owner@example.com")

    def test_legacy_bradley_recipient_env_warns_and_is_ignored(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ,
                        {"NEWS_BRADLEY_RECIPIENT": "old@example.com"},
                        clear=True):
            with contextlib.redirect_stderr(stderr):
                config = load_runtime_config(
                    environ=None,
                    materialize_outputs=False,
                    run_started_at=datetime(2026, 6, 14, 12, 0, 0),
                )
        # Legacy var is no longer read — delivery falls back to the default,
        # and the migration is surfaced on stderr instead of silently.
        self.assertEqual(config.primary_recipient, "primary@example.com")
        self.assertIn("NEWS_BRADLEY_RECIPIENT is obsolete", stderr.getvalue())

    def test_legacy_recipient_env_warning_not_emitted_when_unset(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stderr(stderr):
                load_runtime_config(
                    environ=None,
                    materialize_outputs=False,
                    run_started_at=datetime(2026, 6, 14, 12, 0, 0),
                )
        self.assertNotIn("NEWS_BRADLEY_RECIPIENT is obsolete", stderr.getvalue())

    def test_ui_command_env_delta_matches_preview_overrides(self) -> None:
        body = {
            "action": "run",
            "preset": "dev",
            "env": {
                "NEWS_SOURCE_SCOPE": "peripheral",
                "NEWS_RECIPIENT_SCOPE": "primary",
            },
        }

        command, env = build_command(body)
        preview = preview_payload(body)

        self.assertEqual(command, ["uv", "run", "news", "run", "--preset", "dev"])
        self.assertEqual(env, {"NEWS_PRESET": "dev", "NEWS_SOURCE_SCOPE": "peripheral"})
        self.assertEqual(preview["env"], env)
        self.assertEqual(preview["runtime"]["source_scope"], "peripheral")

    def test_ui_command_carries_preset_source_scope_over_existing_env(self) -> None:
        body = {"action": "run", "preset": "local-prod", "env": {}}

        with patch.dict(os.environ, {"NEWS_SOURCE_SCOPE": "core"}):
            command, env = build_command(body)
            preview = preview_payload(body)

        self.assertEqual(command, ["uv", "run", "news", "run", "--preset", "local-prod"])
        self.assertEqual(env["NEWS_PRESET"], "local-prod")
        self.assertEqual(env["NEWS_SOURCE_SCOPE"], "peripheral")
        self.assertEqual(preview["runtime"]["source_scope"], "peripheral")

    def test_ui_schema_groups_and_task_models_are_explicit(self) -> None:
        with patch.dict(os.environ, {"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS}, clear=True):
            schema = schema_payload()

        groups = {knob["group"] for knob in schema["knobs"]}
        self.assertTrue(
            {
                "Run Settings",
                "Model Selection",
                "Model Tuning",
                "Pipeline Budget",
                "Model Server Settings",
            }.issubset(groups)
        )
        self.assertNotIn("Image", groups)
        self.assertNotIn("Story", groups)
        self.assertNotIn("Infrastructure", groups)

        knobs = {knob["env"]: knob for knob in schema["knobs"]}
        for env in (
            "NEWS_SOURCE_COLLECTION_CONCURRENCY",
            "NEWS_ARTICLE_SUMMARY_CONCURRENCY",
            "NEWS_STORY_SYNTHESIS_CONCURRENCY",
            "NEWS_MODEL_CONCURRENCY",
        ):
            self.assertIn(env, knobs)
            self.assertEqual(knobs[env]["type"], "number")
            self.assertIsNotNone(knobs[env]["default"])
        self.assertIn("model_tuning_presets", schema)
        self.assertIn("presets", schema["model_tuning_presets"])

        model = schema["runtime"]["model"]
        self.assertNotIn("profile", model)
        self.assertEqual(model["reference"], CODEX_TEST_MODEL_ALIAS)
        self.assertEqual(model["article_summary"]["reference"], CODEX_TEST_MODEL_ALIAS)
        self.assertEqual(model["story_drafting"]["reference"], CODEX_TEST_MODEL_ALIAS)
        self.assertEqual(model["article_summary"]["base_url"], model["base_url"])
        self.assertEqual(model["story_drafting"]["base_url"], model["base_url"])

    def test_absolute_paths_resolve_from_explicit_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "outputs"
            config = load_runtime_config(
                environ={"NEWS_OUTPUT_DIR": str(output_dir)},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )

        self.assertEqual(config.output_dir, output_dir)

    def test_load_model_tuning_presets_missing_and_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            missing_path = tmpdir_path / "missing-model-tuning.yaml"
            self.assertEqual(load_model_tuning_presets(missing_path), {})

            preset_path = tmpdir_path / "model_tuning_presets.yaml"
            preset_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      concise-story-drafting:
                        model: mlx-community/example-model
                        task: story_drafting
                        tuning:
                          temperature: 0.2
                          top_p: 0.9
                          max_tokens: 1400
                    """
                ),
                encoding="utf-8",
            )

            presets = load_model_tuning_presets(preset_path)
            self.assertEqual(
                presets["concise-story-drafting"],
                {
                    "id": "concise-story-drafting",
                    "model": "mlx-community/example-model",
                    "task": "story_drafting",
                    "tuning": {
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "max_tokens": 1400,
                    },
                },
            )

    def test_model_tuning_preset_applies_with_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            preset_path = tmpdir_path / "model_tuning_presets.yaml"
            preset_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      concise-story-drafting:
                        model: mlx-community/example-model
                        task: story_drafting
                        tuning:
                          temperature: 0.2
                          top_p: 0.9
                          max_tokens: 1400
                      wrong-task:
                        model: mlx-community/example-model
                        task: article_summary
                        tuning:
                          temperature: 0.1
                      blank-default:
                        tuning:
                          max_tokens: ""
                    """
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_STORY_DRAFTING": "mlx-community/example-model",
                    "NEWS_MODEL_STORY_DRAFTING_BASE_URL": "http://127.0.0.1:8090/v1",
                    "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET": "concise-story-drafting",
                    "NEWS_MODEL_STORY_DRAFTING_TEMPERATURE": "0.4",
                    "NEWS_STORY_DRAFTING_MAX_TOKENS": "1500",
                },
                clear=True,
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", preset_path):
                config = load_runtime_config(materialize_outputs=False)
                story_tuning = config.model_assignments[MODEL_TASK_STORY_DRAFTING].tuning
                self.assertEqual(story_tuning.story_drafting_max_tokens, 1500)
                self.assertEqual(story_tuning.task_sampling[MODEL_TASK_STORY_DRAFTING].temperature, 0.4)
                self.assertEqual(story_tuning.task_sampling[MODEL_TASK_STORY_DRAFTING].top_p, 0.9)
                self.assertEqual(
                    config.model_assignments[MODEL_TASK_STORY_DRAFTING].reference,
                    "mlx-community/example-model",
                )

            with patch.dict(
                os.environ,
                {
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_STORY_DRAFTING": "mlx-community/example-model",
                    "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET": "wrong-task",
                },
                clear=True,
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", preset_path):
                with self.assertRaisesRegex(
                    ValueError,
                    r"wrong-task.*article_summary.*story_drafting",
                ):
                    load_runtime_config(materialize_outputs=False)

            with patch.dict(
                os.environ,
                {
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_TITLE_GENERATION": "mlx-community/example-model",
                    "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET": "wrong-task",
                },
                clear=True,
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", preset_path):
                with self.assertRaisesRegex(
                    ValueError,
                    r"wrong-task.*article_summary.*title_generation",
                ):
                    load_runtime_config(materialize_outputs=False)

            with patch.dict(
                os.environ,
                {
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_TUNING_PRESET": "blank-default",
                },
                clear=True,
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", preset_path):
                with self.assertRaisesRegex(
                    ValueError,
                    r"blank-default.*max_tokens.*must be a number",
                ):
                    load_runtime_config(materialize_outputs=False)

    def test_new_task_tuning_preset_applies_with_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            preset_path = tmpdir_path / "model_tuning_presets.yaml"
            preset_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      quick-scale-screen:
                        model: mlx-community/example-model
                        task: story_scale_screening
                        tuning:
                          temperature: 0.7
                          max_tokens: 2500
                    """
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS,
                    "NEWS_MODEL_STORY_SCALE_SCREENING": "mlx-community/example-model",
                    "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL": "http://127.0.0.1:8090/v1",
                    "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET": "quick-scale-screen",
                    "NEWS_MODEL_STORY_SCALE_SCREENING_TEMPERATURE": "0.4",
                    "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS": "2600",
                    "NEWS_TITLE_GENERATION_MAX_TOKENS": "800",
                },
                clear=True,
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", preset_path):
                config = load_runtime_config(materialize_outputs=False)
                scale_tuning = config.model_assignments[MODEL_TASK_STORY_SCALE_SCREENING].tuning
                self.assertEqual(scale_tuning.story_scale_screening_max_tokens, 2600)
                self.assertEqual(
                    scale_tuning.task_sampling[MODEL_TASK_STORY_SCALE_SCREENING].temperature,
                    0.4,
                )
                # Preset max_tokens (2500) is applied, then the direct env
                # override (2600) wins.
                self.assertEqual(
                    config.model_assignments[MODEL_TASK_STORY_SCALE_SCREENING].reference,
                    "mlx-community/example-model",
                )
                # The other new task's override path is also exercised: the
                # direct env override (800) wins over the 700 default.
                title_tuning = config.model_assignments[MODEL_TASK_TITLE_GENERATION].tuning
                self.assertEqual(title_tuning.title_generation_max_tokens, 800)
                self.assertEqual(
                    config.model_assignments[MODEL_TASK_TITLE_GENERATION].reference,
                    CODEX_TEST_MODEL_ALIAS,
                )
                # Documented defaults are pinned so drift is caught by CI.
                self.assertEqual(config_module.DEFAULT_STORY_SCALE_SCREENING_MAX_TOKENS, 3000)
                self.assertEqual(config_module.DEFAULT_TITLE_GENERATION_MAX_TOKENS, 700)

    def test_sampling_fields_remain_unset_without_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
            },
            clear=True,
        ):
            config = load_runtime_config(materialize_outputs=False)

        self.assertEqual(
            config.model_tuning.task_sampling["default"],
            ModelSamplingSettings(),
        )

    def test_model_selection_does_not_change_server_settings(self) -> None:
        shared_env = {
            "NEWS_MODEL_BASE_URL": "http://127.0.0.1:8111/v1",
            "NEWS_MODEL_SERVER_MAX_TOKENS": "777",
        }
        config_one = load_runtime_config(
            materialize_outputs=False,
            environ={**shared_env, "NEWS_MODEL": CODEX_TEST_MODEL_ALIAS},
        )
        config_two = load_runtime_config(
            materialize_outputs=False,
            environ={**shared_env, "NEWS_MODEL": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS},
        )

        self.assertEqual(config_one.model_server_settings, config_two.model_server_settings)
        self.assertEqual(
            config_one.model_server_settings,
            ModelServerSettings(
                base_url="http://127.0.0.1:8111/v1",
                prefill_step_size=512,
                prompt_cache_size=2,
                prompt_cache_bytes="512MB",
                max_tokens=777,
            ),
        )
        self.assertIn("--port 8111", config_one.model_server_command)
        self.assertIn("--max-tokens 777", config_one.model_server_command)
        self.assertNotIn("--prompt-cache-size", config_one.model_server_command)
        self.assertNotIn("--prompt-cache-bytes", config_one.model_server_command)
        self.assertNotIn("--prompt-cache-size", config_two.model_server_command)
        self.assertNotIn("--prompt-cache-bytes", config_two.model_server_command)

    def test_run_and_model_tuning_preset_round_trip_modified_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            run_path = tmpdir_path / "run_presets.yaml"
            run_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      demo:
                        name: Demo
                        description: Example run preset.
                        env:
                          NEWS_IMAGE_ENABLED: '1'
                    """
                ),
                encoding="utf-8",
            )
            tuning_path = tmpdir_path / "model_tuning_presets.yaml"
            tuning_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      concise-story-drafting:
                        model: mlx-community/example-model
                        task: story_drafting
                        tuning:
                          temperature: 0.2
                    """
                ),
                encoding="utf-8",
            )

            with patch.object(ui_module, "RUN_PRESETS_PATH", run_path), patch.object(
                ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path
            ), patch.object(config_module, "MODEL_TUNING_PRESETS_PATH", tuning_path):
                run_listing = ui_module.list_presets()
                self.assertEqual(run_listing["presets"][0]["id"], "demo")
                self.assertNotIn("modified_at", run_listing["presets"][0])

                saved_run = ui_module.upsert_preset(
                    {
                        "id": "demo",
                        "name": "Demo",
                        "description": "Example run preset.",
                        "env": {"NEWS_IMAGE_ENABLED": "1"},
                    }
                )
                self.assertIn("modified_at", saved_run["preset"])
                self.assertEqual(ui_module.list_presets()["presets"][0]["id"], "demo")
                self.assertIn("modified_at", ui_module.list_presets()["presets"][0])

                model_listing = ui_module.list_model_tuning_presets()
                self.assertEqual(model_listing["presets"][0]["id"], "concise-story-drafting")
                self.assertNotIn("modified_at", model_listing["presets"][0])

                saved_model = ui_module.upsert_model_tuning_preset(
                    {
                        "id": "concise-story-drafting",
                        "name": "Concise Story Drafting",
                        "model": "mlx-community/example-model",
                        "task": "story_drafting",
                        "tuning": {
                            "temperature": 0.2,
                        },
                    }
                )
                self.assertIn("modified_at", saved_model["preset"])
                self.assertEqual(
                    ui_module.list_model_tuning_presets()["presets"][0]["name"],
                    "Concise Story Drafting",
                )

                deleted_run = ui_module.delete_preset("demo")
                self.assertEqual(deleted_run["deleted"], "demo")
                self.assertEqual(ui_module.list_presets()["presets"], [])

                deleted_model = ui_module.delete_model_tuning_preset("concise-story-drafting")
                self.assertEqual(deleted_model["deleted"], "concise-story-drafting")
                self.assertEqual(ui_module.list_model_tuning_presets()["presets"], [])


if __name__ == "__main__":
    unittest.main()
