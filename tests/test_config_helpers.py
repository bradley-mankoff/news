from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import news_pipeline.config as config_module
from news_pipeline.config import ModelSamplingSettings, ModelTuningSettings, RuntimeConfigRequest


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
                task_sampling={"default": base_sampling},
            ),
            ModelTuningSettings(
                article_summary_max_tokens=20,
                task_sampling={"story_drafting": overlay_sampling},
            ),
        )
        self.assertEqual(merged_tuning.article_summary_max_tokens, 20)
        self.assertEqual(merged_tuning.task_sampling["story_drafting"].top_p, 0.8)

        with patch.object(config_module, "resolve_model_name", return_value="patched-model"), patch.object(
            config_module,
            "MODEL_SPECIFIC_TUNING_DEFAULTS",
            {"patched-model": ModelTuningSettings(model_max_input_tokens=123, task_sampling={"default": overlay_sampling})},
        ):
            tuned = config_module._base_model_tuning("anything")
        self.assertEqual(tuned.model_max_input_tokens, 123)
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
        self.assertEqual(config_module.resolve_model_name(config_module.QWWYTHOS_9B_4BIT_MODEL_ALIAS), config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE)
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

    def test_remaining_config_helpers_cover_unseen_branches(self) -> None:
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
        self.assertEqual(config_module.infer_model_backend(config_module.QWWYTHOS_9B_4BIT_MODEL_REFERENCE), "mlx-vlm")
        self.assertEqual(config_module.infer_model_backend("other-model"), "mlx-lm")

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
