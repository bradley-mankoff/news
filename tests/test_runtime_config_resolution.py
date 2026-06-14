from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from news_pipeline.config import (
    ACTIVE_PRESET_ENV_VAR,
    CODEX_TEST_MODEL_ALIAS,
    GEMMA_12B_OPTIQ_MODEL_ALIAS,
    GEMMA_12B_OPTIQ_MODEL_NAME,
    PRESET_ENV_VAR,
    RuntimeConfigRequest,
    load_runtime_config,
    resolve_runtime_config,
)
from news_pipeline.ui import build_command, preview_payload


class RuntimeConfigResolutionTests(unittest.TestCase):
    def test_resolver_does_not_mutate_process_environment(self) -> None:
        previous = dict(os.environ)

        resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={},
                preset_id="dev",
                overrides={"NEWS_MODEL": GEMMA_12B_OPTIQ_MODEL_ALIAS},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(dict(os.environ), previous)

    def test_preset_base_env_and_overrides_have_documented_precedence(self) -> None:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env={"NEWS_MODEL": GEMMA_12B_OPTIQ_MODEL_ALIAS},
                preset_id="dev",
                overrides={"NEWS_SOURCE_SCOPE": "peripheral"},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )
        )

        self.assertEqual(resolution.config.preset_id, "dev")
        self.assertEqual(resolution.config.model_reference, GEMMA_12B_OPTIQ_MODEL_ALIAS)
        self.assertEqual(resolution.config.model_name, GEMMA_12B_OPTIQ_MODEL_NAME)
        self.assertEqual(resolution.config.model_backend, "mlx-lm")
        self.assertIn("python -m mlx_lm server", resolution.config.model_server_command)
        self.assertEqual(resolution.config.source_scope, "peripheral")
        self.assertEqual(resolution.config.recipient_scope, "bradley")
        self.assertEqual(resolution.command_env_delta["NEWS_PRESET"], "dev")
        self.assertEqual(resolution.command_env_delta["NEWS_SOURCE_SCOPE"], "peripheral")
        self.assertNotIn("NEWS_RECIPIENT_SCOPE", resolution.command_env_delta)

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
            overrides={"NEWS_MODEL": "gemma-26b-moe"},
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

    def test_ui_command_env_delta_matches_preview_overrides(self) -> None:
        body = {
            "action": "run",
            "preset": "dev",
            "env": {
                "NEWS_SOURCE_SCOPE": "peripheral",
                "NEWS_RECIPIENT_SCOPE": "bradley",
            },
        }

        command, env = build_command(body)
        preview = preview_payload(body)

        self.assertEqual(command, ["uv", "run", "news", "run", "--preset", "dev"])
        self.assertEqual(env, {"NEWS_PRESET": "dev", "NEWS_SOURCE_SCOPE": "peripheral"})
        self.assertEqual(preview["env"], env)
        self.assertEqual(preview["runtime"]["source_scope"], "peripheral")

    def test_absolute_paths_resolve_from_explicit_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "outputs"
            config = load_runtime_config(
                environ={"NEWS_OUTPUT_DIR": str(output_dir)},
                materialize_outputs=False,
                run_started_at=datetime(2026, 6, 14, 12, 0, 0),
            )

        self.assertEqual(config.output_dir, output_dir)


if __name__ == "__main__":
    unittest.main()
