from __future__ import annotations

import os
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import news_pipeline.config as config_module
from news_pipeline import model_catalog
from news_pipeline.config import ModelSamplingSettings, ModelTuningSettings, RuntimeConfigRequest


_CUSTOM_CATALOG = """\
models:
  my-gemma-4-lm:
    reference: mlx-community/gemma-4-custom-lm
    name: Custom Gemma LM
    backend: mlx-lm
    hf_repo: mlx-community/gemma-4-custom-lm
    description: Declared mlx-lm despite the gemma-4 name heuristic.
  my-vlm-model:
    reference: mlx-community/plain-vlm
    name: Custom VLM
    backend: mlx-vlm
    hf_repo: mlx-community/plain-vlm
    description: Declared mlx-vlm despite the plain name.
  my-ext-model:
    reference: external-org/openai-compatible
    name: External Model
    backend: external
    hf_repo: external-org/openai-compatible
    description: Declared external backend.
  my-mlx-model:
    reference: mlx-community/example-model
    name: Example MLX Model
    backend: mlx-lm
    hf_repo: mlx-community/example-model
    context_length: 8192
    description: A user-verified MLX language model.
    task_notes:
      speed: Fast local model.
"""


class ConfigHelperTests(unittest.TestCase):
    def test_model_tuning_helpers_cover_edge_branches(self) -> None:
        base_sampling = ModelSamplingSettings(
            temperature=0.1,
            top_p=0.9,
            top_k=40,
            min_p=0.2,
            presence_penalty=0.3,
            repetition_penalty=0.4,
        )
        overlay_sampling = ModelSamplingSettings(top_p=0.8, repetition_penalty=1.1)

        self.assertEqual(config_module._optional_int_env("X", {"X": "7"}), 7)
        self.assertIsNone(config_module._optional_int_env("X", {"X": " "}))
        self.assertEqual(config_module._optional_float_env("X", {"X": "1.5"}), 1.5)
        self.assertIsNone(config_module._optional_float_env("X", {"X": ""}))

        merged_sampling = config_module._merge_model_sampling_settings(base_sampling, overlay_sampling)
        self.assertEqual(merged_sampling.temperature, 0.1)
        self.assertEqual(merged_sampling.top_p, 0.8)
        self.assertEqual(merged_sampling.repetition_penalty, 1.1)

        merged_tuning = config_module._merge_model_tuning_settings(
            ModelTuningSettings(
                model_max_input_tokens=1,
                article_summary_max_tokens=2,
                story_drafting_max_tokens=3,
                story_scale_screening_max_tokens=3000,
                title_generation_max_tokens=700,
                task_sampling={"default": base_sampling},
            ),
            ModelTuningSettings(
                article_summary_max_tokens=20,
                title_generation_max_tokens=900,  # overlay wins
                task_sampling={"story_drafting": overlay_sampling},
            ),
        )
        self.assertEqual(merged_tuning.article_summary_max_tokens, 20)
        self.assertEqual(merged_tuning.task_sampling["story_drafting"].top_p, 0.8)
        # Silent-drop prevention for the two new per-task fields: base value
        # survives when the overlay leaves it unset, overlay wins when set.
        self.assertEqual(merged_tuning.story_scale_screening_max_tokens, 3000)  # base survives
        self.assertEqual(merged_tuning.title_generation_max_tokens, 900)        # overlay wins

        with patch.object(config_module, "resolve_model_name", return_value="patched-model"), patch.object(
            config_module,
            "MODEL_SPECIFIC_TUNING_DEFAULTS",
            {"patched-model": ModelTuningSettings(
                model_max_input_tokens=123,
                story_scale_screening_max_tokens=3100,
                title_generation_max_tokens=710,
                task_sampling={"default": overlay_sampling},
            )},
        ):
            tuned = config_module._base_model_tuning("anything")
        self.assertEqual(tuned.model_max_input_tokens, 123)
        self.assertEqual(tuned.story_scale_screening_max_tokens, 3100)
        self.assertEqual(tuned.title_generation_max_tokens, 710)
        self.assertEqual(tuned.task_sampling["default"].top_p, 0.8)

        self.assertEqual(config_module._task_max_tokens_field("default"), "model_max_input_tokens")
        self.assertEqual(
            config_module._task_max_tokens_field(config_module.MODEL_TASK_ARTICLE_SUMMARY),
            "article_summary_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(config_module.MODEL_TASK_STORY_DRAFTING),
            "story_drafting_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(config_module.MODEL_TASK_STORY_SCALE_SCREENING),
            "story_scale_screening_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(config_module.MODEL_TASK_TITLE_GENERATION),
            "title_generation_max_tokens",
        )
        self.assertEqual(
            config_module._task_max_tokens_field(config_module.MODEL_TASK_IMAGE_ART_DIRECTION),
            "title_generation_max_tokens",
        )
        with self.assertRaises(ValueError):
            config_module._task_max_tokens_field("bogus")

        self.assertEqual(config_module._selected_model_tuning_preset_id("reasoning"), "")

        with self.assertRaisesRegex(ValueError, "does not accept a task scope"):
            config_module._validate_model_tuning_preset_scope(
                preset_id="sample",
                preset={"task": "story_drafting"},
                assignment_reference="model-a",
                assignment_name="model-a",
                assignment_task="default",
            )
        with self.assertRaisesRegex(ValueError, "expects task 'article_summary'"):
            config_module._validate_model_tuning_preset_scope(
                preset_id="sample",
                preset={"task": "article_summary"},
                assignment_reference="model-a",
                assignment_name="model-a",
                assignment_task="story_drafting",
            )

        empty_tuning = config_module._apply_model_tuning_preset(
            ModelTuningSettings(task_sampling={}),
            preset_id="sample",
            preset={"tuning": None},
            assignment_task="default",
        )
        self.assertEqual(empty_tuning.task_sampling["default"], ModelSamplingSettings())

        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            config_module._apply_model_tuning_preset(
                ModelTuningSettings(task_sampling={}),
                preset_id="sample",
                preset={"tuning": []},
                assignment_task="default",
            )
        with self.assertRaisesRegex(ValueError, "Unsupported tuning field"):
            config_module._apply_model_tuning_preset(
                ModelTuningSettings(task_sampling={}),
                preset_id="sample",
                preset={"tuning": {"unsupported": 1}},
                assignment_task="default",
            )

        story_tuning = config_module._apply_model_tuning_preset(
            ModelTuningSettings(task_sampling={}),
            preset_id="sample",
            preset={"tuning": {"max_tokens": 17, "temperature": 0.25}},
            assignment_task="story_drafting",
        )
        self.assertEqual(story_tuning.story_drafting_max_tokens, 17)
        self.assertEqual(story_tuning.task_sampling["story_drafting"].temperature, 0.25)

        scale_tuning = config_module._apply_model_tuning_preset(
            ModelTuningSettings(task_sampling={}),
            preset_id="sample",
            preset={"tuning": {"max_tokens": 2500}},
            assignment_task="story_scale_screening",
        )
        self.assertEqual(scale_tuning.story_scale_screening_max_tokens, 2500)

        title_tuning = config_module._apply_model_tuning_preset(
            ModelTuningSettings(task_sampling={}),
            preset_id="sample",
            preset={"tuning": {"max_tokens": 700}},
            assignment_task="title_generation",
        )
        self.assertEqual(title_tuning.title_generation_max_tokens, 700)

        with self.assertRaises(ValueError):
            config_module._sampling_settings_from_mapping([("temperature", 0.1)])

        self.assertIsNone(config_module._coerce_optional_int_value(" "))
        self.assertEqual(config_module._coerce_optional_int_value("5"), 5)
        self.assertIsNone(config_module._coerce_optional_float_value(" "))
        self.assertEqual(config_module._coerce_optional_float_value("1.25"), 1.25)

        with patch.dict(
            os.environ,
            {
                "NEWS_MODEL_TEMPERATURE": "0.3",
                "NEWS_MODEL_REASONING_TEMPERATURE": "0.5",
                "NEWS_MODEL_MAX_INPUT_TOKENS": "9000",
            },
            clear=True,
        ):
            overridden = config_module._apply_model_tuning_env_overrides(
                ModelTuningSettings(
                    model_max_input_tokens=10,
                    task_sampling={"default": ModelSamplingSettings(temperature=0.1)},
                )
            )
        self.assertEqual(overridden.model_max_input_tokens, 9000)
        self.assertEqual(overridden.task_sampling["default"].temperature, 0.3)
        self.assertEqual(overridden.task_sampling["reasoning"].temperature, 0.5)

        with self.assertRaisesRegex(ValueError, "Unknown model tuning preset"):
            config_module._configured_model_tuning("model", preset_id="missing", presets={})

    def test_model_tuning_max_tokens_reject_non_positive_values(self) -> None:
        # Preset path: the max_tokens shorthand resolves by assignment task and
        # then must be positive; the error names the preset id, original key,
        # and the offending value.
        for preset_key in ("max_tokens", "story_drafting_max_tokens"):
            for bad_value in (0, -1):
                with self.subTest(preset_key=preset_key, value=bad_value):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"sample.*{preset_key}.*greater than zero",
                    ) as ctx:
                        config_module._apply_model_tuning_preset(
                            ModelTuningSettings(task_sampling={}),
                            preset_id="sample",
                            preset={"tuning": {preset_key: bad_value}},
                            assignment_task="story_drafting",
                        )
                    self.assertIn(str(bad_value), str(ctx.exception))

        # Valid positive boundary: max_tokens=1 resolves through the task alias
        # and survives unchanged (not replaced by a default).
        boundary_tuning = config_module._apply_model_tuning_preset(
            ModelTuningSettings(task_sampling={}),
            preset_id="sample",
            preset={"tuning": {"max_tokens": 1}},
            assignment_task="story_drafting",
        )
        self.assertEqual(boundary_tuning.story_drafting_max_tokens, 1)

        # Env path: each model-tuning max-token variable rejects 0 and negative
        # values with the variable named, and 1 survives as the tuning value.
        env_fields = {
            "NEWS_MODEL_MAX_INPUT_TOKENS": "model_max_input_tokens",
            "NEWS_ARTICLE_SUMMARY_MAX_TOKENS": "article_summary_max_tokens",
            "NEWS_STORY_DRAFTING_MAX_TOKENS": "story_drafting_max_tokens",
            "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS": "story_scale_screening_max_tokens",
            "NEWS_TITLE_GENERATION_MAX_TOKENS": "title_generation_max_tokens",
        }
        for env_name, field_name in env_fields.items():
            for bad_value in (0, -1):
                with self.subTest(env=env_name, value=bad_value):
                    with patch.dict(os.environ, {env_name: str(bad_value)}, clear=True):
                        with self.assertRaisesRegex(
                            ValueError,
                            rf"{env_name}.*greater than zero",
                        ) as ctx:
                            config_module._apply_model_tuning_env_overrides(
                                ModelTuningSettings(task_sampling={})
                            )
                    self.assertIn(str(bad_value), str(ctx.exception))
            with patch.dict(os.environ, {env_name: "1"}, clear=True):
                overridden = config_module._apply_model_tuning_env_overrides(
                    ModelTuningSettings(task_sampling={})
                )
            self.assertEqual(getattr(overridden, field_name), 1)

        # The positive wrapper keeps blank/unset and syntax behavior from the
        # generic parser: blank means unset, a bad number still raises the
        # generic integer error.
        self.assertIsNone(config_module._positive_optional_int_env("X", {"X": " "}))
        self.assertEqual(config_module._positive_optional_int_env("X", {"X": "7"}), 7)
        with self.assertRaisesRegex(ValueError, "Invalid integer value"):
            config_module._positive_optional_int_env("X", {"X": "abc"})

        # Sampling fields are unaffected: top_k=0 stays legal on the generic
        # integer path and only max-token fields enforce positivity.
        with patch.dict(os.environ, {"NEWS_MODEL_ARTICLE_SUMMARY_TOP_K": "0"}, clear=True):
            sampling_ok = config_module._apply_model_tuning_env_overrides(
                ModelTuningSettings(task_sampling={})
            )
        self.assertEqual(
            sampling_ok.task_sampling[config_module.MODEL_TASK_ARTICLE_SUMMARY].top_k,
            0,
        )

    def test_model_tuning_preset_max_tokens_covers_all_fields_and_task_aliases(self) -> None:
        canonical_fields = (
            "model_max_input_tokens",
            "article_summary_max_tokens",
            "story_drafting_max_tokens",
            "story_scale_screening_max_tokens",
            "title_generation_max_tokens",
        )
        for field_name in canonical_fields:
            for bad_value in (0, -1):
                with self.subTest(field=field_name, value=bad_value):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"sample.*{field_name}.*greater than zero",
                    ) as ctx:
                        config_module._apply_model_tuning_preset(
                            ModelTuningSettings(task_sampling={}),
                            preset_id="sample",
                            preset={"tuning": {field_name: bad_value}},
                            assignment_task="default",
                        )
                    self.assertIn(str(bad_value), str(ctx.exception))

        task_fields = (
            ("default", "model_max_input_tokens"),
            (config_module.MODEL_TASK_ARTICLE_SUMMARY, "article_summary_max_tokens"),
            (config_module.MODEL_TASK_STORY_DRAFTING, "story_drafting_max_tokens"),
            (
                config_module.MODEL_TASK_STORY_SCALE_SCREENING,
                "story_scale_screening_max_tokens",
            ),
            (config_module.MODEL_TASK_TITLE_GENERATION, "title_generation_max_tokens"),
            (config_module.MODEL_TASK_IMAGE_ART_DIRECTION, "title_generation_max_tokens"),
        )
        for assignment_task, field_name in task_fields:
            with self.subTest(task=assignment_task):
                tuning = config_module._apply_model_tuning_preset(
                    ModelTuningSettings(task_sampling={}),
                    preset_id="sample",
                    preset={"tuning": {"max_tokens": 1}},
                    assignment_task=assignment_task,
                )
                self.assertEqual(getattr(tuning, field_name), 1)

    def test_validate_managed_model_assignments_covers_branches(self) -> None:
        # The default assignment defines the compared values (model_name,
        # model_base_url), so it always matches and is skipped.
        config_module._validate_managed_model_assignments(
            {
                "default": SimpleNamespace(base_url="http://127.0.0.1:8080/v1", reference="main", name="main"),
                "article_summary": SimpleNamespace(base_url="http://127.0.0.1:8080/v1", reference="main", name="main"),
            },
            model_reference="main",
            model_name="main",
            model_base_url="http://127.0.0.1:8080/v1",
            model_backend="mlx-lm",
        )  # no raise: default skip + same-name task
        # External backends serve multiple models: early return, no raise.
        config_module._validate_managed_model_assignments(
            {"article_summary": SimpleNamespace(base_url="http://127.0.0.1:8080/v1", reference="other", name="other")},
            model_reference="main",
            model_name="main",
            model_base_url="http://127.0.0.1:8080/v1",
            model_backend="external",
        )
        # Multiple violating tasks: first-error-wins by dict order.
        with self.assertRaisesRegex(ValueError, "Task 'article_summary'"):
            config_module._validate_managed_model_assignments(
                {
                    "article_summary": SimpleNamespace(base_url="http://127.0.0.1:8080/v1", reference="other", name="other"),
                    "story_drafting": SimpleNamespace(base_url="http://127.0.0.1:8080/v1", reference="third", name="third"),
                },
                model_reference="main",
                model_name="main",
                model_base_url="http://127.0.0.1:8080/v1",
                model_backend="mlx-lm",
            )
        # A trailing-slash spelling of the same endpoint still trips the guard.
        with self.assertRaisesRegex(ValueError, "multiple different models"):
            config_module._validate_managed_model_assignments(
                {"article_summary": SimpleNamespace(base_url="http://127.0.0.1:8080/v1/", reference="other", name="other")},
                model_reference="main",
                model_name="main",
                model_base_url="http://127.0.0.1:8080/v1",
                model_backend="mlx-lm",
            )
        # A loopback-alias spelling of the same endpoint still trips the guard.
        with self.assertRaisesRegex(ValueError, "multiple different models"):
            config_module._validate_managed_model_assignments(
                {"article_summary": SimpleNamespace(base_url="http://localhost:8080/v1", reference="other", name="other")},
                model_reference="main",
                model_name="main",
                model_base_url="http://127.0.0.1:8080/v1",
                model_backend="mlx-lm",
            )

    def test_same_model_endpoint_tolerates_spelling_variants(self) -> None:
        canonical = "http://127.0.0.1:8080/v1"
        self.assertTrue(config_module.same_model_endpoint(canonical, canonical + "/"))
        self.assertTrue(config_module.same_model_endpoint(canonical, "HTTP://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint(canonical, "http://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://127.0.0.1:80/v1", "http://127.0.0.1/v1"))
        self.assertFalse(config_module.same_model_endpoint(canonical, "http://127.0.0.1:8080/v2"))
        self.assertFalse(config_module.same_model_endpoint(canonical, "http://127.0.0.1:8081/v1"))
        self.assertFalse(config_module.same_model_endpoint(canonical, ""))
        self.assertTrue(config_module.same_model_endpoint("", ""))
        # Loopback host aliases (Issue #134): localhost, 127.0.0.1, and ::1
        # denote the same endpoint and must not bypass the conflict check.
        self.assertTrue(config_module.same_model_endpoint("http://localhost:8080/v1", "http://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://127.0.0.1:8080/v1", "http://localhost:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://[::1]:8080/v1", "http://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("HTTP://LOCALHOST:8080/v1", "http://127.0.0.1:8080/v1"))
        # A trailing-dot FQDN names the same host and stays loopback.
        self.assertTrue(config_module.same_model_endpoint("http://localhost.:8080/v1", "http://127.0.0.1:8080/v1"))
        # Exotic IPv6 loopback spellings urlparse returns verbatim:
        # the uncompressed form of ::1 and the IPv4-mapped forms of
        # 127.0.0.1 (dotted and hex) denote the same loopback interface.
        self.assertTrue(config_module.same_model_endpoint("http://[0:0:0:0:0:0:0:1]:8080/v1", "http://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://127.0.0.1:8080/v1", "http://[0:0:0:0:0:0:0:1]:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://[::ffff:127.0.0.1]:8080/v1", "http://127.0.0.1:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://127.0.0.1:8080/v1", "http://[::ffff:127.0.0.1]:8080/v1"))
        self.assertTrue(config_module.same_model_endpoint("http://[::ffff:7f00:1]:8080/v1", "http://127.0.0.1:8080/v1"))
        self.assertFalse(config_module.same_model_endpoint("http://localhost:8080/v1", "http://127.0.0.1:9090/v1"))
        self.assertFalse(config_module.same_model_endpoint("http://localhost:8080/v1", "http://localhost:8080/v2"))

    def test_runtime_model_and_env_helpers_cover_edge_branches(self) -> None:
        self.assertEqual(config_module.normalize_preset_id("  dev  "), "dev")
        self.assertTrue(config_module._bool_env("FLAG", False, {"FLAG": "yes"}))
        self.assertFalse(config_module._bool_env("FLAG", True, {"FLAG": "no"}))
        self.assertEqual(config_module._int_env("COUNT", 2, {"COUNT": "4"}), 4)
        self.assertEqual(config_module._float_env("RATE", 1.5, {"RATE": "2.5"}), 2.5)
        self.assertEqual(config_module._str_env("TEXT", "fallback", {"TEXT": "  hi  "}), "hi")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_run_presets = root / "bad_run_presets.yaml"
            bad_run_presets.write_text("presets: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define presets as a mapping"):
                config_module.load_run_presets(bad_run_presets)

            bad_run_env = root / "bad_run_env.yaml"
            bad_run_env.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      sample:
                        env: []
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "env must be a mapping"):
                config_module.load_run_presets(bad_run_env)

            good_run_presets = root / "good_run_presets.yaml"
            good_run_presets.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      sample:
                        name: Sample
                        description: Example preset
                        modified_at: 2026-06-01T10:00:00
                        env:
                          NEWS_MODEL: gemma-26b-moe
                          NEWS_EMPTY: ""
                    """
                ),
                encoding="utf-8",
            )
            presets = config_module.load_run_presets(good_run_presets)
            self.assertEqual(presets["sample"]["env"], {"NEWS_MODEL": "gemma-26b-moe"})
            self.assertEqual(presets["sample"]["modified_at"], "2026-06-01 10:00:00")

            bad_model_presets = root / "bad_model_presets.yaml"
            bad_model_presets.write_text("presets: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define presets as a mapping"):
                config_module.load_model_tuning_presets(bad_model_presets)

            invalid_tuning_presets = root / "invalid_tuning_presets.yaml"
            invalid_tuning_presets.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      sample:
                        tuning: []
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "tuning must be a mapping"):
                config_module.load_model_tuning_presets(invalid_tuning_presets)

            good_model_presets = root / "good_model_presets.yaml"
            good_model_presets.write_text(
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
            model_presets = config_module.load_model_tuning_presets(good_model_presets)
            self.assertEqual(model_presets["concise-story-drafting"]["task"], "story_drafting")

            with self.assertRaisesRegex(ValueError, "Unknown run preset"):
                config_module.run_preset_env("missing", good_run_presets)

        with patch.dict(
            os.environ,
            {
                config_module.ACTIVE_PRESET_ENV_VAR: "old",
                "NEWS_PRESET": "old",
                "SHARED": "keep",
                "FROM_OLD": "1",
            },
            clear=True,
        ), patch.object(
            config_module,
            "run_preset_env",
            side_effect=[{"FROM_OLD": "1"}, {"FROM_NEW": "2", "SHARED": "preset"}],
        ):
            normalized = config_module.apply_run_preset_to_environment("new")
            self.assertEqual(normalized, "new")
            self.assertEqual(os.environ[config_module.PRESET_ENV_VAR], "new")
            self.assertEqual(os.environ[config_module.ACTIVE_PRESET_ENV_VAR], "new")
            self.assertEqual(os.environ["FROM_NEW"], "2")

        with patch.dict(os.environ, {"NEWS_MIN_ARTICLES_PER_STORY": "1", "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD": "2.0"}, clear=True):
            self.assertEqual(config_module.configured_min_articles_per_story(), 2)
            self.assertEqual(config_module.configured_story_cluster_similarity_threshold(), 1.0)
        with patch.dict(os.environ, {"NEWS_BAD_FLOAT": "nope"}, clear=True):
            self.assertEqual(config_module._bounded_env_float("NEWS_BAD_FLOAT", 0.4), 0.4)

        self.assertEqual(
            config_module.resolve_model_name(""),
            config_module.resolve_model_name(config_module.DEFAULT_MODEL_ALIAS),
        )
        self.assertEqual(
            config_module.resolve_model_name(config_module.GEMMA_4_12B_IT_4BIT_MODEL_ALIAS),
            config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO,
        )
        with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
            config_module.resolve_model_name("qwythos-9b-8bit")
        # Raw GGUF references and their URL forms fail fast too (all forms the
        # old docs published as "Resolved model").
        for unsupported in (
            config_module.QWWYTHOS_9B_8BIT_MODEL_REFERENCE,
            config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
            f"https://huggingface.co/{config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE}",
            f"https://hf.co/{config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE}",
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
                config_module.resolve_model_name(unsupported)
        with patch.object(config_module, "UNSUPPORTED_MODEL_REFERENCES", {"blocked"}):
            with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
                config_module.resolve_model_name("blocked")
        self.assertTrue(config_module.is_codex_test_model_reference(config_module.CODEX_TEST_MODEL_ALIAS))
        self.assertTrue(config_module.is_gemma_4_model_reference("gemma-4-vision"))
        self.assertFalse(config_module.is_gemma_4_model_reference("gpt-4o-mini"))
        with patch.dict(os.environ, {"CODEX_SANDBOX": "1", "NEWS_CODEX_TESTING": "1"}, clear=True):
            self.assertTrue(config_module.codex_model_guard_active())
            self.assertEqual(config_module._configured_model_reference(), config_module.CODEX_TEST_MODEL_ALIAS)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config_module.codex_model_guard_active())
            self.assertEqual(config_module._configured_model_reference(), config_module.DEFAULT_MODEL_ALIAS)
        with patch.dict(os.environ, {"CODEX_SANDBOX": "1", "NEWS_CODEX_TESTING": "1"}, clear=True):
            config_module.ensure_codex_safe_model_reference(config_module.CODEX_TEST_MODEL_ALIAS)
            with self.assertRaises(RuntimeError):
                config_module.ensure_codex_safe_model_reference("bad-model")
        self.assertEqual(config_module.infer_model_backend("gemma-4-vision"), "mlx-vlm")
        self.assertEqual(config_module._default_article_summary_concurrency(config_module.CODEX_TEST_MODEL_ALIAS), 8)
        self.assertEqual(config_module._default_story_synthesis_concurrency(config_module.CODEX_TEST_MODEL_ALIAS), 2)
        self.assertEqual(config_module._default_story_synthesis_concurrency("gemma-4-vision"), 1)
        knob = config_module._runtime_knob("Group", "Label", "NEWS_TEST", secret=True, options=["one"])
        self.assertTrue(knob["secret"])
        self.assertEqual(knob["options"], ["one"])
        registry = config_module.runtime_knob_registry()
        self.assertTrue(any(knob["env"] == "NEWS_MODEL_CONCURRENCY" for knob in registry))
        knob_envs = {knob["env"] for knob in registry}
        for env in (
            "NEWS_MODEL_STORY_SCALE_SCREENING",
            "NEWS_MODEL_TITLE_GENERATION",
            "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
            "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
            "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS",
            "NEWS_TITLE_GENERATION_MAX_TOKENS",
            "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL",
            "NEWS_MODEL_TITLE_GENERATION_BASE_URL",
        ):
            self.assertIn(env, knob_envs)
        for knob in registry:
            if knob["env"] in {"NEWS_MODEL_STORY_SCALE_SCREENING", "NEWS_MODEL_TITLE_GENERATION"}:
                self.assertEqual(knob["group"], "Model Selection")
                self.assertEqual(knob["type"], "select")
            if knob["env"] in {"NEWS_STORY_SCALE_SCREENING_MAX_TOKENS", "NEWS_TITLE_GENERATION_MAX_TOKENS"}:
                self.assertEqual(knob["group"], "Model Tuning")
                self.assertEqual(knob["type"], "number")
                self.assertEqual(knob["min"], 1)
            if knob["env"] in {"NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL", "NEWS_MODEL_TITLE_GENERATION_BASE_URL"}:
                self.assertEqual(knob["group"], "Model Server Settings")
        backend_knobs = [knob for knob in registry if knob["env"] == "NEWS_MODEL_BACKEND"]
        self.assertEqual(len(backend_knobs), 1)
        self.assertEqual(backend_knobs[0]["group"], "Model Selection")
        self.assertEqual(backend_knobs[0]["options"], ["external", "mlx-lm", "mlx-vlm"])
        # Pin the Prompt Profile knob contract: select with catalog-backed
        # default and options (drift-guard for runtime_knob_registry).
        prompt_profile_knob = next(knob for knob in registry if knob["env"] == "NEWS_PROMPT_PROFILE")
        self.assertEqual(prompt_profile_knob["type"], "select")
        self.assertEqual(prompt_profile_knob["default"], "balanced")
        self.assertIn("playful", prompt_profile_knob["options"])
        self.assertEqual(set(prompt_profile_knob["options"]), set(config_module.PROMPT_PROFILE_IDS))
        # Pin the per-stage prompt override knobs contract: one text knob per
        # task, hidden from non-advanced surfaces (advanced=True) exactly like
        # the sampling knobs; the UI's SURFACED_ENVS suppresses them from the
        # Advanced tab in favor of the dedicated Editorial approach editors.
        override_knobs = [
            knob for knob in registry
            if knob["env"] in config_module.PROMPT_TASK_OVERRIDE_ENV_VARS.values()
        ]
        self.assertEqual(
            {knob["env"] for knob in override_knobs},
            set(config_module.PROMPT_TASK_OVERRIDE_ENV_VARS.values()),
        )
        for knob in override_knobs:
            self.assertEqual(knob["type"], "text")
            self.assertTrue(knob["advanced"])
        # Drift-guard: every model knob option maps to an HF page + hardware
        # link; the backend knob (not a model choice) carries none.
        model_knob_envs = ("NEWS_MODEL", "NEWS_MODEL_ARTICLE_SUMMARY", "NEWS_MODEL_STORY_DRAFTING")
        for env in model_knob_envs:
            model_knob = next(knob for knob in registry if knob["env"] == env)
            self.assertEqual(
                set(model_knob["option_links"]),
                set(model_knob["options"]),
                f"{env} option_links must cover every offered option",
            )
            for option, link in model_knob["option_links"].items():
                self.assertEqual(sorted(link), ["hardware", "page"])
                self.assertTrue(link["page"].startswith("https://huggingface.co/"), option)
                self.assertNotIn("https://huggingface.co/https://", link["page"], option)
                self.assertEqual(link["hardware"], link["page"])
        self.assertEqual(backend_knobs[0]["option_links"], {})
        # Drift-guard: aliases must never land in the unsupported set, or the
        # registry build and hf_model_page_url's ValueError fallback break.
        self.assertEqual(
            set(config_module.UNSUPPORTED_MODEL_REFERENCES) & set(config_module.MODEL_ALIASES),
            set(),
        )
        # hf_model_page_url: alias, URL keys, MLX repo, external id and empty input.
        gemma_page = f"https://huggingface.co/{config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO}"
        self.assertEqual(
            config_module.hf_model_page_url(config_module.GEMMA_4_12B_IT_4BIT_MODEL_ALIAS),
            gemma_page,
        )
        self.assertEqual(
            config_module.hf_model_page_url(f"https://hf.co/{config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO}"),
            gemma_page,
        )
        self.assertEqual(
            config_module.hf_model_page_url(f"https://huggingface.co/{config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO}"),
            gemma_page,
        )
        self.assertEqual(
            config_module.hf_model_page_url(f"  https://hf.co/{config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO}  "),
            gemma_page,
        )
        self.assertEqual(
            config_module.hf_model_page_url(config_module.CODEX_TEST_MODEL_ALIAS),
            f"https://huggingface.co/{config_module.CODEX_TEST_MODEL_NAME}",
        )
        # The unsupported Qwythos aliases and their GGUF references have no page.
        self.assertIsNone(config_module.hf_model_page_url(config_module.QWWYTHOS_9B_4BIT_MODEL_ALIAS))
        self.assertIsNone(config_module.hf_model_page_url(config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE))
        self.assertIsNone(config_module.hf_model_page_url("gpt-4o-mini"))
        self.assertIsNone(config_module.hf_model_page_url("openai/gpt-4o"))
        self.assertIsNone(config_module.hf_model_page_url("foo.gguf"))
        self.assertIsNone(config_module.hf_model_page_url("https://huggingface.co/unknown/owner-repo"))
        self.assertIsNone(config_module.hf_model_page_url("https://example.com/not-huggingface"))
        self.assertIsNone(config_module.hf_model_page_url(""))
        self.assertIsNone(config_module.hf_model_page_url("   "))
        # Unsupported references yield None rather than raising ValueError.
        with patch.object(config_module, "UNSUPPORTED_MODEL_REFERENCES", {"qwythos-9b-8bit"}):
            self.assertIsNone(config_module.hf_model_page_url("qwythos-9b-8bit"))

    def test_hf_model_page_url_edge_cases_never_emit_broken_links(self) -> None:
        """Malformed / defensive inputs to hf_model_page_url must all yield
        None (the 'never emit a broken link' contract), never a URL."""
        ref = config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE
        self.assertIsNone(config_module.hf_model_page_url(None))
        self.assertIsNone(config_module.hf_model_page_url(f"https://huggingface.co/{ref}/"))
        self.assertIsNone(
            config_module.hf_model_page_url(f"https://huggingface.co/https://hf.co/{ref}")
        )
        self.assertIsNone(config_module.hf_model_page_url("HTTPS://HUGGINGFACE.CO/foo/bar"))

    def test_docs_drift_guard_links_match_model_aliases(self) -> None:
        """Every built-in MODEL_ALIASES HF page URL must appear in README.md
        and SETTINGS.md (the docs hardcode the same URLs the UI renders).

        MODEL_ALIASES is the built-in-only baseline; YAML-added aliases never
        enter it (they are validated structurally in
        test_custom_catalog_aliases_reach_runtime_surfaces instead, since user
        URLs are not tracked repository docs)."""
        repo_root = Path(__file__).resolve().parents[1]
        docs_text = "\n".join(
            (repo_root / name).read_text(encoding="utf-8")
            for name in ("README.md", "SETTINGS.md")
        )
        for alias in config_module.MODEL_ALIASES:
            url = config_module.hf_model_page_url(alias)
            self.assertIsNotNone(url, alias)
            self.assertIn(url, docs_text, f"{alias} page URL missing from README/SETTINGS")

    def test_custom_catalog_aliases_reach_runtime_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "custom_catalog.yaml"
            path.write_text(_CUSTOM_CATALOG, encoding="utf-8")
            custom = model_catalog.load_model_catalog(path)
        with patch.object(model_catalog, "_CATALOG_SNAPSHOT", custom):
            # Alias resolution maps the YAML alias to its declared reference;
            # raw references keep the identity fallback.
            self.assertEqual(
                config_module.resolve_model_name("my-mlx-model"),
                "mlx-community/example-model",
            )
            self.assertEqual(
                config_module.resolve_model_name("mlx-community/example-model"),
                "mlx-community/example-model",
            )
            # Declared catalog backends win over the name heuristic: the
            # gemma-4 name would infer mlx-vlm, plain names would infer mlx-lm.
            self.assertEqual(config_module.infer_model_backend("my-gemma-4-lm"), "mlx-lm")
            self.assertEqual(config_module.infer_model_backend("my-vlm-model"), "mlx-vlm")
            self.assertEqual(
                config_module.infer_model_backend("mlx-community/gemma-4-custom-lm"),
                "mlx-lm",
            )
            self.assertEqual(config_module.infer_model_backend("my-ext-model"), "external")
            # Selector options and links include the custom aliases.
            registry = config_module.runtime_knob_registry()
            model_knob = next(knob for knob in registry if knob["env"] == "NEWS_MODEL")
            for alias in ("my-mlx-model", "my-gemma-4-lm", "my-vlm-model", "my-ext-model"):
                self.assertIn(alias, model_knob["options"])
                self.assertEqual(
                    model_knob["option_links"][alias]["page"],
                    f"https://huggingface.co/{custom[alias].hf_repo}",
                )
            for env in ("NEWS_MODEL_ARTICLE_SUMMARY", "NEWS_MODEL_STORY_DRAFTING"):
                task_knob = next(knob for knob in registry if knob["env"] == env)
                self.assertIn("my-mlx-model", task_knob["options"])
                self.assertIn("my-mlx-model", task_knob["option_links"])
            # HF page URLs work for the custom alias and its URL form.
            self.assertEqual(
                config_module.hf_model_page_url("my-mlx-model"),
                "https://huggingface.co/mlx-community/example-model",
            )
            self.assertEqual(
                config_module.hf_model_page_url("https://hf.co/mlx-community/example-model"),
                "https://huggingface.co/mlx-community/example-model",
            )
            # The external entry still gets a model page (owner/repo id).
            self.assertEqual(
                config_module.hf_model_page_url("my-ext-model"),
                "https://huggingface.co/external-org/openai-compatible",
            )
        # The default baseline is untouched once the custom snapshot is gone.
        self.assertEqual(
            config_module.resolve_model_name("gemma-4-12b-it-4bit"),
            config_module.GEMMA_4_12B_IT_4BIT_MODEL_REPO,
        )
        self.assertNotIn("my-mlx-model", config_module.MODEL_ALIASES)

    def test_custom_catalog_reserved_collision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reserved.yaml"
            path.write_text(
                "models:\n"
                "  qwythos-9b-4bit:\n"
                "    reference: mlx-community/some-model\n"
                "    name: Collision Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: mlx-community/some-model\n"
                "    description: Reserved alias collision.\n",
                encoding="utf-8",
            )
            custom = model_catalog.load_model_catalog(path)
        with patch.object(model_catalog, "_CATALOG_SNAPSHOT", custom):
            # The legacy unsupported guard still fires first for the alias
            # itself, and the registry build rejects the collision so the
            # alias can never become a selector option.
            with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
                config_module.resolve_model_name("qwythos-9b-4bit")
            with self.assertRaisesRegex(ValueError, "unsupported reference"):
                config_module.runtime_knob_registry()

    def test_mlx_vlm_floor_is_gemma_4_capable(self) -> None:
        """The managed mlx-vlm backend must be able to load the default
        gemma4_unified model type (issue #124): floor >= 0.6.4 in both
        pyproject.toml and uv.lock."""
        repo_root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        mlx_vlm_dep = next(
            (d for d in pyproject["project"]["dependencies"] if d.startswith("mlx-vlm")),
            None,
        )
        self.assertIsNotNone(
            mlx_vlm_dep, "mlx-vlm missing from pyproject.toml dependencies"
        )
        # Exact specifier token: >=0.6.4 must appear as its own clause (a
        # floor like >=0.6.40 would otherwise match a substring check).
        self.assertIn(
            "mlx-vlm>=0.6.4", mlx_vlm_dep.split(";")[0].split(","), mlx_vlm_dep
        )
        lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
        mlx_vlm = next((p for p in lock["package"] if p["name"] == "mlx-vlm"), None)
        self.assertIsNotNone(mlx_vlm, "mlx-vlm missing from uv.lock packages")
        major, minor, patch = (int(x) for x in mlx_vlm["version"].split(".")[:3])
        self.assertGreaterEqual((major, minor, patch), (0, 6, 4), mlx_vlm["version"])

    def test_default_model_knob_points_at_gemma_and_hides_qwythos(self) -> None:
        registry = config_module.runtime_knob_registry()
        knob = next(k for k in registry if k["env"] == "NEWS_MODEL")
        self.assertEqual(knob["default"], "gemma-4-12b-it-4bit")
        self.assertIn("gemma-4-12b-it-4bit", knob["options"])
        self.assertNotIn("qwythos-9b-8bit", knob["options"])
        # prod preset now launches gemma
        self.assertEqual(config_module.run_preset_env("prod")["NEWS_MODEL"], "gemma-4-12b-it-4bit")

    def test_yaml_scope_and_runtime_config_helpers_cover_edge_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_yaml = root / "bad.yaml"
            bad_yaml.write_text("- not-a-mapping\n", encoding="utf-8")
            self.assertEqual(config_module._load_yaml_mapping(root / "missing.yaml"), {})
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                config_module._load_yaml_mapping(bad_yaml)

            env_json = root / "env.json"
            env_json.write_text('{"pw": "  secret key  "}', encoding="utf-8")
            self.assertEqual(config_module._load_password_from_env_json(env_json), "secretkey")
            env_json.write_text("broken", encoding="utf-8")
            self.assertEqual(config_module._load_password_from_env_json(env_json), "")

            original = "prefix\n" + config_module.CURSORIGNORE_MANAGED_START + "\nmanaged\n" + config_module.CURSORIGNORE_MANAGED_END + "\nsuffix"
            self.assertEqual(
                config_module._strip_managed_block(
                    original,
                    config_module.CURSORIGNORE_MANAGED_START,
                    config_module.CURSORIGNORE_MANAGED_END,
                ),
                "prefix\nsuffix",
            )
            self.assertEqual(
                config_module._strip_managed_block(
                    "no markers",
                    config_module.CURSORIGNORE_MANAGED_START,
                    config_module.CURSORIGNORE_MANAGED_END,
                ),
                "no markers",
            )

            output_dir = root / "output" / "daily_outputs"
            output_dir.mkdir(parents=True)
            latest_run = output_dir / "latest_run.md"
            latest_run.write_text("latest", encoding="utf-8")
            (output_dir / "latest_run.log").write_text("log", encoding="utf-8")
            (output_dir / "latest_run_details.json").write_text("{}", encoding="utf-8")
            patterns = config_module._latest_run_output_patterns(root, output_dir)
            self.assertIn("!output/daily_outputs/", patterns)
            self.assertIn(f"!{latest_run.relative_to(root).as_posix()}", patterns)
            self.assertEqual(config_module._latest_run_output_patterns(root, root / "outside"), [])
            self.assertEqual(config_module._latest_run_output_patterns(root, root / "missing"), [])

            ignore_path = root / ".cursorignore"
            ignore_path.write_text(
                "keep-this\n"
                + config_module.ASSISTANT_CONTEXT_MANAGED_START
                + "\nold\n"
                + config_module.ASSISTANT_CONTEXT_MANAGED_END
                + "\n",
                encoding="utf-8",
            )
            config_module._sync_cursorignore_latest_output(root, output_dir, output_dir / ".staging")
            updated = ignore_path.read_text(encoding="utf-8")
            self.assertIn("keep-this", updated)
            self.assertIn(config_module.ASSISTANT_CONTEXT_MANAGED_START, updated)

            self.assertEqual(
                config_module._coerce_source_text_list(["Alpha", " ", "Alpha", "Beta"]),
                ["Alpha", "Beta"],
            )
            self.assertEqual(config_module._coerce_source_text_list("Alpha"), ["Alpha"])
            self.assertEqual(config_module._coerce_source_text_list(None), [])
            self.assertEqual(config_module._normalize_source_tier("", source_key="Alpha"), config_module.PERIPHERAL_SOURCE_TIER)
            self.assertEqual(config_module._normalize_source_tier("core", source_key="Alpha"), config_module.CORE_SOURCE_TIER)
            with self.assertRaisesRegex(ValueError, "Unsupported source tier"):
                config_module._normalize_source_tier("unsupported", source_key="Alpha")
            self.assertEqual(config_module._normalize_source_scope("full"), config_module.SOURCE_SCOPE_PERIPHERAL)
            with self.assertRaisesRegex(ValueError, "NEWS_SOURCE_SCOPE must be one of"):
                config_module._normalize_source_scope("unsupported")
            with patch.dict(os.environ, {"NEWS_SOURCE_SCOPE": "all"}, clear=True):
                self.assertEqual(config_module._configured_source_scope(), config_module.SOURCE_SCOPE_PERIPHERAL)
            self.assertEqual(config_module._normalize_recipient_scope("full"), config_module.RECIPIENT_SCOPE_ALL)
            with self.assertRaisesRegex(ValueError, "NEWS_RECIPIENT_SCOPE must be one of"):
                config_module._normalize_recipient_scope("unsupported")
            with patch.dict(os.environ, {"NEWS_RECIPIENT_SCOPE": "single"}, clear=True):
                self.assertEqual(config_module._configured_recipient_scope(), config_module.RECIPIENT_SCOPE_PRIMARY)
            self.assertEqual(config_module.RECIPIENT_SCOPE_PRIMARY, "primary")
            with self.assertRaisesRegex(ValueError, "NEWS_RECIPIENT_SCOPE must be one of"):
                config_module._normalize_recipient_scope("bradley")
            with self.assertRaisesRegex(ValueError, "NEWS_RECIPIENT_SCOPE must be one of"):
                config_module._normalize_recipient_scope("bradley-only")
            # Normalization edges: default, case, whitespace, underscore forms.
            self.assertEqual(config_module._normalize_recipient_scope(None), config_module.RECIPIENT_SCOPE_PRIMARY)
            self.assertEqual(config_module._normalize_recipient_scope(""), config_module.RECIPIENT_SCOPE_PRIMARY)
            self.assertEqual(config_module._normalize_recipient_scope("PRIMARY"), config_module.RECIPIENT_SCOPE_PRIMARY)
            self.assertEqual(config_module._normalize_recipient_scope(" primary "), config_module.RECIPIENT_SCOPE_PRIMARY)
            with self.assertRaisesRegex(ValueError, "NEWS_RECIPIENT_SCOPE must be one of"):
                config_module._normalize_recipient_scope("primary_scope")
            self.assertFalse(
                config_module._source_enabled_for_scope(
                    {"language": "es", "tier": "core"},
                    config_module.SOURCE_SCOPE_CORE,
                    source_key="Alpha",
                )
            )
            self.assertTrue(
                config_module._source_enabled_for_scope(
                    {"language": "en", "tier": "core"},
                    config_module.SOURCE_SCOPE_CORE,
                    source_key="Alpha",
                )
            )
            self.assertEqual(
                config_module._normalize_source_match_mode("wire-attribution", source_key="Alpha"),
                config_module.SOURCE_MATCH_MODE_WIRE_ATTRIBUTION,
            )
            with self.assertRaisesRegex(ValueError, "source_match_mode must be one of"):
                config_module._normalize_source_match_mode("unsupported", source_key="Alpha")
            with self.assertRaisesRegex(ValueError, "uses removed topic field"):
                config_module._reject_removed_source_topic_fields(
                    {"allowed_topic_ids": [1]},
                    source_key="Alpha",
                )

            sources_path = root / "sources.yaml"
            sources_path.write_text(
                textwrap.dedent(
                    """\
                    sources:
                      - key: EnglishCore
                        url: https://example.com/core.xml
                        language: en
                        tier: core
                        source_match_mode: feed-label
                        source_match_aliases: core alias
                      - key: EnglishPeripheral
                        url: https://example.com/peripheral.xml
                        language: en
                        tier: peripheral
                        source_match_mode: wire-attribution
                        source_match_aliases:
                          - peripheral alias
                      - key: BadLanguage
                        url: https://example.com/bad.xml
                        language: es
                        tier: peripheral
                      - key: MissingUrl
                        language: en
                        tier: core
                      - not-a-dict
                    """
                ),
                encoding="utf-8",
            )
            self.assertEqual(list(config_module.load_sources(sources_path, source_scope="core")), ["EnglishCore"])
            self.assertEqual(
                list(config_module.load_sources(sources_path, source_scope="peripheral")),
                ["EnglishCore", "EnglishPeripheral"],
            )
            self.assertEqual(
                list(config_module.load_sources(sources_path, source_scope="peripheral", include_inactive=True)),
                ["EnglishCore", "EnglishPeripheral", "BadLanguage"],
            )

            bad_sources_path = root / "bad_sources.yaml"
            bad_sources_path.write_text("sources: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define sources as a list"):
                config_module.load_sources(bad_sources_path)

            recipients_path = root / "recipients.yaml"
            recipients_path.write_text("recipients: {bad: value}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define recipients as a list"):
                config_module.load_recipients(recipients_path)
            recipients_path.write_text(
                textwrap.dedent(
                    """\
                    recipients:
                      - email: reader@example.com
                        name: Reader
                        pause: true
                      - email: invalid
                      - name: Missing email
                    """
                ),
                encoding="utf-8",
            )
            recipients = config_module.load_recipients(recipients_path)
            self.assertEqual(recipients, {"reader@example.com": {"name": "Reader", "pause": True}})
            self.assertEqual(
                config_module.update_recipient_pause_setting(
                    "reader@example.com",
                    pause=False,
                    path=recipients_path,
                ),
                1,
            )
            self.assertEqual(
                config_module.update_recipient_pause_setting(
                    "missing@example.com",
                    pause=True,
                    path=recipients_path,
                ),
                0,
            )

            self.assertEqual(config_module.configured_removed_topic_env_vars({"NEWS_TOPIC_IDS": "1"}), ["NEWS_TOPIC_IDS"])
            with patch.dict(os.environ, {"NEWS_TOPIC_IDS": "1"}, clear=True):
                with self.assertRaisesRegex(ValueError, "Unset removed environment variable"):
                    config_module.reject_removed_topic_env_vars()

            self.assertEqual(config_module._coerce_bool_value(True), True)
            self.assertEqual(config_module._coerce_bool_value(0), False)
            self.assertEqual(config_module._coerce_float_value("bad", 1.25), 1.25)
            self.assertEqual(config_module._coerce_pause_value("yes"), True)
            self.assertEqual(config_module._clean_env({"A": "1", "B": None}), {"A": "1"})

            delta = config_module._runtime_command_env_delta(
                base_env={"A": "1", "KEEP": "same"},
                effective_env={
                    "A": "2",
                    "KEEP": "same",
                    "B": "3",
                    config_module.PRESET_ENV_VAR: "new",
                    config_module.ACTIVE_PRESET_ENV_VAR: "new",
                },
                preset_env={"A": "2"},
                preset_id="new",
            )
            self.assertEqual(delta, {"B": "3", config_module.PRESET_ENV_VAR: "new"})

            with patch.object(
                config_module,
                "run_preset_env",
                return_value={"NEWS_MODEL": "preset-model"},
            ):
                preset_id, base_env, preset_env, effective_env = config_module._resolve_effective_env(
                    RuntimeConfigRequest(
                        base_env={"KEEP": "base"},
                        preset_id="preset",
                        overrides={"NEWS_MODEL": "override"},
                        materialize_outputs=False,
                    )
                )
            self.assertEqual(preset_id, "preset")
            self.assertEqual(base_env["KEEP"], "base")
            self.assertEqual(preset_env, {"NEWS_MODEL": "preset-model"})
            self.assertEqual(effective_env["NEWS_MODEL"], "override")
            self.assertEqual(effective_env[config_module.PRESET_ENV_VAR], "preset")

    def test_delivery_mode_and_placeholder_helpers_cover_edge_branches(self) -> None:
        # Closed-set normalization: canonical values, aliases, and
        # case/whitespace/underscore-insensitive forms.
        for raw, expected in (
            ("disabled", config_module.DELIVERY_MODE_DISABLED),
            ("owner", config_module.DELIVERY_MODE_OWNER),
            ("recipients", config_module.DELIVERY_MODE_RECIPIENTS),
            ("DISABLED", config_module.DELIVERY_MODE_DISABLED),
            (" Owner ", config_module.DELIVERY_MODE_OWNER),
            ("owner_only", config_module.DELIVERY_MODE_OWNER),
            ("none", config_module.DELIVERY_MODE_DISABLED),
            ("off", config_module.DELIVERY_MODE_DISABLED),
            ("owner-only", config_module.DELIVERY_MODE_OWNER),
        ):
            self.assertEqual(config_module._normalize_delivery_mode(raw), expected)
        with self.assertRaisesRegex(ValueError, "NEWS_DELIVERY_MODE must be one of"):
            config_module._normalize_delivery_mode("everyone")
        with self.assertRaisesRegex(ValueError, "NEWS_DELIVERY_MODE must be one of"):
            config_module._normalize_delivery_mode("")

        # Precedence: explicit mode > legacy recipient scope > owner default;
        # an empty explicit value behaves as unset.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_OWNER)
        with patch.dict(os.environ, {"NEWS_RECIPIENT_SCOPE": "primary"}, clear=True):
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_OWNER)
        with patch.dict(os.environ, {"NEWS_RECIPIENT_SCOPE": "all"}, clear=True):
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_RECIPIENTS)
        with patch.dict(os.environ, {"NEWS_DELIVERY_MODE": "disabled"}, clear=True):
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_DISABLED)
        with patch.dict(
            os.environ,
            {"NEWS_DELIVERY_MODE": "owner", "NEWS_RECIPIENT_SCOPE": "all"},
            clear=True,
        ):
            # Explicit new setting wins over legacy scope.
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_OWNER)
        with patch.dict(
            os.environ,
            {"NEWS_DELIVERY_MODE": "", "NEWS_RECIPIENT_SCOPE": "all"},
            clear=True,
        ):
            # Empty-but-present explicit value follows the empty-env
            # convention and behaves as unset, so legacy scope applies.
            self.assertEqual(config_module._configured_delivery_mode(), config_module.DELIVERY_MODE_RECIPIENTS)
        with patch.dict(os.environ, {"NEWS_DELIVERY_MODE": "bogus"}, clear=True):
            with self.assertRaisesRegex(ValueError, "NEWS_DELIVERY_MODE must be one of"):
                config_module._configured_delivery_mode()

        # Placeholder predicates are case-insensitive and whitespace-
        # normalized; the empty credential counts as a placeholder.
        self.assertTrue(config_module.is_placeholder_address("you@example.com"))
        self.assertTrue(config_module.is_placeholder_address("PRIMARY@EXAMPLE.COM"))
        self.assertTrue(config_module.is_placeholder_address(" news@example.com "))
        self.assertFalse(config_module.is_placeholder_address("owner@example.com"))
        self.assertFalse(config_module.is_placeholder_address(""))
        for token in ("password", "your-password", "change-me", "changeme", "placeholder", ""):
            self.assertTrue(config_module.is_placeholder_credential(token), token)
        self.assertTrue(config_module.is_placeholder_credential("Change-Me"))
        self.assertTrue(config_module.is_placeholder_credential(" PASSWORD "))
        self.assertFalse(config_module.is_placeholder_credential("s3cret"))
        self.assertTrue(config_module.is_placeholder_credential(None))

        # The public profile snapshot never exposes credentials: passwords
        # and unsubscribe secrets appear only as configured booleans, and
        # placeholder values count as not configured.
        placeholder_profile = config_module.DeliveryProfile(
            mode=config_module.DELIVERY_MODE_OWNER,
            owner_recipient="primary@example.com",
            sender="news@example.com",
            smtp_host="smtp.gmail.com",
            smtp_port=465,
            smtp_username="news@example.com",
            smtp_password="password",
            unsubscribe_secret="change-me",
        )
        snapshot = placeholder_profile.public_snapshot()
        self.assertEqual(snapshot["mode"], "owner")
        self.assertFalse(snapshot["smtp_password_set"])
        self.assertFalse(snapshot["unsubscribe_secret_set"])
        # The raw credential values never appear in the projection; only the
        # configured booleans exist.
        self.assertNotIn("smtp_password", snapshot)
        self.assertNotIn("unsubscribe_secret", snapshot)
        self.assertNotIn("change-me", str(snapshot))

        secret_profile = config_module.DeliveryProfile(
            mode=config_module.DELIVERY_MODE_RECIPIENTS,
            owner_recipient="owner@example.com",
            sender="owner@example.com",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="owner@example.com",
            smtp_use_ssl=False,
            smtp_password="s3cret",
            unsubscribe_secret="unsub-token",
        )
        secret_snapshot = secret_profile.public_snapshot()
        self.assertTrue(secret_snapshot["smtp_password_set"])
        self.assertTrue(secret_snapshot["unsubscribe_secret_set"])
        self.assertNotIn("s3cret", str(secret_snapshot))
        self.assertNotIn("unsub-token", str(secret_snapshot))

        # A sender equal to the owner is accepted at the profile level; no
        # identity inequality check exists (ADR 0012 identity rules).
        self.assertEqual(secret_profile.sender, secret_profile.owner_recipient)

        # Delivery mode knob contract in the shared registry: a Run Settings
        # select with the three canonical options and the owner default.
        registry = config_module.runtime_knob_registry()
        delivery_knobs = [knob for knob in registry if knob["env"] == config_module.DELIVERY_MODE_ENV_VAR]
        self.assertEqual(len(delivery_knobs), 1)
        self.assertEqual(delivery_knobs[0]["group"], "Run Settings")
        self.assertEqual(delivery_knobs[0]["type"], "select")
        self.assertEqual(delivery_knobs[0]["default"], config_module.DELIVERY_MODE_OWNER)
        self.assertEqual(set(delivery_knobs[0]["options"]), set(config_module.DELIVERY_MODES))


        self.assertEqual(config_module.MODEL_BACKEND_EXTERNAL, "external")
        self.assertEqual(config_module.SUPPORTED_MODEL_BACKENDS, ("mlx-lm", "mlx-vlm", "external"))
        self.assertEqual(
            config_module.build_model_server_command(
                "m",
                config_module.ModelServerSettings(base_url="http://x:9/v1"),
                backend="external",
            ),
            "",
        )
        self.assertEqual(config_module._coerce_source_text_list(123), [])
        self.assertEqual(
            config_module._default_story_synthesis_concurrency("some-other-model"),
            config_module.DEFAULT_STORY_SYNTHESIS_CONCURRENCY,
        )
        self.assertEqual(config_module.infer_model_backend("other-model"), "mlx-lm")
        # Legacy raw Qwythos references fail fast like the aliases (issue #124);
        # the retained "qwythos" routing branch only serves non-listed raw ids.
        with self.assertRaisesRegex(ValueError, "Unsupported model reference"):
            config_module.infer_model_backend(config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE)
        self.assertEqual(config_module.infer_model_backend("someone/qwythos-other-raw-id"), "mlx-vlm")

        with patch.dict(os.environ, {config_module.PRESET_ENV_VAR: "sample"}, clear=True):
            self.assertEqual(config_module._configured_preset_id(), "sample")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config_module.apply_run_preset_to_environment(None), "")
            config_module.ensure_codex_safe_model_reference("anything")

        with patch.dict(os.environ, {config_module.ACTIVE_PRESET_ENV_VAR: "old"}, clear=True), patch.object(
            config_module,
            "run_preset_env",
            side_effect=[ValueError("missing"), {"NEWS_MODEL": "new"}],
        ):
            self.assertEqual(config_module.apply_run_preset_to_environment("new"), "new")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output" / "daily_outputs"
            output_dir.mkdir(parents=True)
            latest_run = output_dir / "latest_run.md"
            latest_run.write_text("latest", encoding="utf-8")
            (output_dir / "latest_run.log").write_text("log", encoding="utf-8")
            (output_dir / "latest_run_details.json").write_text("{}", encoding="utf-8")
            outside_dir = root.parent / f"{root.name}-outside"
            outside_dir.mkdir(parents=True, exist_ok=True)
            output_parent = output_dir.parent.relative_to(root).as_posix().rstrip("/")
            output_path = output_dir.relative_to(root).as_posix().rstrip("/")
            call_count = {"value": 0}

            def relative_to(self, *args):
                call_count["value"] += 1
                if call_count["value"] == 3:
                    raise ValueError("boom")
                if call_count["value"] == 1:
                    return Path("output")
                if call_count["value"] == 2:
                    return Path("output/daily_outputs")
                return Path("unexpected")

            with patch.object(config_module.Path, "relative_to", side_effect=relative_to):
                self.assertEqual(
                    config_module._latest_run_output_patterns(root, output_dir),
                    [
                        "",
                        "# Keep generated output context narrowed to rolling run review artifacts.",
                        f"!{output_parent}/",
                        f"!{output_path}/",
                        f"{output_path}/*",
                    ],
                )
            self.assertEqual(config_module._latest_run_output_patterns(root, outside_dir), [])
            self.assertEqual(config_module._latest_run_output_patterns(root, root / "missing"), [])

            run_presets_path = root / "run_presets.yaml"
            run_presets_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      valid:
                        name: Friendly preset
                        description: Example preset
                        modified_at: 2026-06-01T10:00:00
                        env:
                          NEWS_MODEL: gemma-26b-moe
                      invalid: []
                      "": {}
                    """
                ),
                encoding="utf-8",
            )
            run_presets = config_module.load_run_presets(run_presets_path)
            self.assertEqual(config_module.run_preset_env("valid", run_presets_path), {"NEWS_MODEL": "gemma-26b-moe"})
            self.assertEqual(run_presets["valid"]["name"], "Friendly preset")
            self.assertEqual(run_presets["valid"]["description"], "Example preset")
            self.assertEqual(run_presets["valid"]["modified_at"], "2026-06-01 10:00:00")
            self.assertEqual(run_presets["valid"]["env"], {"NEWS_MODEL": "gemma-26b-moe"})
            self.assertEqual(list(run_presets), ["valid"])

            model_presets_path = root / "model_presets.yaml"
            model_presets_path.write_text(
                textwrap.dedent(
                    """\
                    presets:
                      valid-empty:
                        name: Named preset
                        description: Description text
                        modified_at: 2026-06-01T10:00:00
                        tuning: ""
                      valid:
                        tuning:
                          temperature: 0.2
                      invalid: []
                      "": {tuning: {temperature: 0.4}}
                    """
                ),
                encoding="utf-8",
            )
            model_presets = config_module.load_model_tuning_presets(model_presets_path)
            self.assertEqual(model_presets["valid-empty"]["tuning"], {})
            self.assertEqual(model_presets["valid"]["tuning"]["temperature"], 0.2)
            self.assertEqual(model_presets["valid-empty"]["name"], "Named preset")
            self.assertEqual(model_presets["valid-empty"]["description"], "Description text")
            self.assertEqual(model_presets["valid-empty"]["modified_at"], "2026-06-01 10:00:00")
            self.assertEqual(
                config_module._configured_model_tuning(
                    "model-a",
                    preset_id="sample",
                    presets={"sample": {"model": "model-a", "tuning": {"temperature": 0.4}}},
                ).task_sampling["default"].temperature,
                0.4,
            )
            with self.assertRaisesRegex(ValueError, "expects model"):
                config_module._validate_model_tuning_preset_scope(
                    preset_id="sample",
                    preset={"model": "other-model"},
                    assignment_reference="model-a",
                    assignment_name="model-a",
                    assignment_task="default",
                )
            self.assertIsNone(
                config_module._validate_model_tuning_preset_scope(
                    preset_id="sample",
                    preset={},
                    assignment_reference="model-a",
                    assignment_name="model-a",
                    assignment_task="default",
                )
            )

            sources_path = root / "sources.yaml"
            sources_path.write_text(
                textwrap.dedent(
                    """\
                    sources:
                      - key: EnglishCore
                        url: https://example.com/core.xml
                        language: en
                        tier: core
                        source_match_mode: feed-label
                        source_match_aliases: core alias
                      - key: EnglishPeripheral
                        url: https://example.com/peripheral.xml
                        language: en
                        tier: peripheral
                        source_match_mode: wire-attribution
                        source_match_aliases:
                          - peripheral alias
                      - key: BadLanguage
                        url: https://example.com/bad.xml
                        language: es
                        tier: peripheral
                      - key: WeirdAliases
                        url: https://example.com/weird.xml
                        language: es
                        tier: peripheral
                        source_match_aliases:
                          bad: value
                      - key: MissingUrl
                        language: en
                        tier: core
                      - not-a-dict
                    """
                ),
                encoding="utf-8",
            )
            self.assertEqual(list(config_module.load_sources(sources_path, source_scope="core")), ["EnglishCore"])
            self.assertEqual(list(config_module.load_sources(sources_path, source_scope="peripheral")), ["EnglishCore", "EnglishPeripheral"])
            self.assertEqual(
                list(config_module.load_sources(sources_path, source_scope="peripheral", include_inactive=True)),
                ["EnglishCore", "EnglishPeripheral", "BadLanguage", "WeirdAliases"],
            )
            self.assertEqual(
                config_module.load_sources(sources_path, source_scope="peripheral", include_inactive=True)["WeirdAliases"][
                    "source_match_aliases"
                ],
                [],
            )
            no_valid_sources_path = root / "no_valid_sources.yaml"
            no_valid_sources_path.write_text(
                textwrap.dedent(
                    """\
                    sources:
                      - not-a-dict
                      - key: MissingUrl
                        language: en
                        tier: core
                      - key: BadLanguage
                        url: https://example.com/bad.xml
                        language: es
                        tier: core
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "No valid source entries found"):
                config_module.load_sources(no_valid_sources_path, source_scope="core")

            recipients_path = root / "recipients.yaml"
            recipients_path.write_text("recipients: []\n", encoding="utf-8")
            self.assertEqual(config_module.load_recipients(recipients_path), {})
            recipients_path.write_text(
                textwrap.dedent(
                    """\
                    recipients:
                      - not-a-dict
                      - email: reader@example.com
                        name: Reader
                        pause: true
                      - email: invalid
                      - name: Missing email
                    """
                ),
                encoding="utf-8",
            )
            recipients = config_module.load_recipients(recipients_path)
            self.assertEqual(recipients, {"reader@example.com": {"name": "Reader", "pause": True}})
            self.assertEqual(
                config_module.update_recipient_pause_setting(
                    "reader@example.com",
                    pause=False,
                    path=recipients_path,
                ),
                1,
            )
            self.assertEqual(
                config_module.update_recipient_pause_setting(
                    "missing@example.com",
                    pause=True,
                    path=recipients_path,
                ),
                0,
            )
            bad_recipients_path = root / "bad_recipients.yaml"
            bad_recipients_path.write_text("recipients: {bad: value}\n", encoding="utf-8")
            self.assertEqual(
                config_module.update_recipient_pause_setting(
                    "reader@example.com",
                    pause=False,
                    path=bad_recipients_path,
                ),
                0,
            )

            self.assertEqual(
                config_module.build_model_server_command(
                    "model-a",
                    config_module.ModelServerSettings(
                        base_url="http://127.0.0.1:8081/v1",
                        prefill_step_size=64,
                        prompt_cache_size=2,
                        prompt_cache_bytes="1GB",
                        max_tokens=42,
                    ),
                    backend="mlx-lm",
                    model_concurrency=3,
                ),
                "uv run python -m mlx_lm server --model model-a --decode-concurrency 3 --prompt-concurrency 3 --host 127.0.0.1 --port 8081 --prefill-step-size 64 --prompt-cache-size 2 --prompt-cache-bytes 1GB --max-tokens 42 --log-level INFO",
            )
            with patch.object(config_module, "_sync_cursorignore_latest_output") as sync_cursorignore:
                fake_config = SimpleNamespace(
                    root_dir=root,
                    output_dir=output_dir,
                    run_output_dir=output_dir / ".staging",
                )
                config_module.sync_assistant_context_latest_output(fake_config)


if __name__ == "__main__":
    unittest.main()

