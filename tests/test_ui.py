from __future__ import annotations

import contextlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import yaml

from news_pipeline import ui as ui_module
from news_pipeline.ui import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    NewsUIServer,
    RunManager,
    RunRecord,
    _add_bool_option,
    _add_option,
    _body_preset_id,
    _clean_env_for_config,
    _coerce_bool,
    _coerce_preset_env,
    _coerce_optional_mapping,
    _config_path_from_env,
    _json_ready,
    _load_yaml_mapping,
    _mask_secret,
    _normalize_env_overrides,
    _preset_env_over_inherited_env,
    _runtime_snapshot,
    _source_summary,
    _recipient_summary,
    _ui_base_env,
    _write_yaml_mapping,
    build_command,
    build_knob_registry,
    list_model_tuning_presets,
    list_presets,
    list_recipients,
    list_sources,
    main,
    preview_payload,
    schema_payload,
    serve_ui,
    upsert_model_tuning_preset,
    upsert_preset,
    upsert_recipient,
    upsert_source,
    delete_model_tuning_preset,
    delete_preset,
    delete_recipient,
    delete_source,
)


@dataclass
class _Sample:
    name: str
    path: Path


class UITests(unittest.TestCase):
    def test_env_info_tooltips_use_native_titles(self) -> None:
        self.assertIn('title="${escapeHtml(tip)}"', ui_module.HTML)
        self.assertNotIn('data-tooltip="${escapeHtml(tip)}"', ui_module.HTML)

    def test_model_knob_links_markup_contract(self) -> None:
        self.assertIn("data-links-for", ui_module.HTML)
        self.assertIn("renderKnobLinks", ui_module.HTML)
        self.assertIn("refreshModelKnobLinks", ui_module.HTML)
        self.assertIn("knob-links", ui_module.HTML)
        self.assertIn('rel="noopener noreferrer"', ui_module.HTML)
        self.assertIn("No Hugging Face page for this external model", ui_module.HTML)
        self.assertIn("Native Hardware Compatibility panel", ui_module.HTML)
        self.assertIn("escapeHtml(entry.page)", ui_module.HTML)
        self.assertIn("escapeHtml(entry.hardware)", ui_module.HTML)
        self.assertIn('data-links-for="${escapeHtml(knob.env)}"', ui_module.HTML)

    def test_pure_helpers_and_schema_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sources_path = root / "sources.yaml"
            recipients_path = root / "recipients.yaml"
            _write_yaml_mapping(
                sources_path,
                {
                    "sources": [
                        {
                            "key": "Alpha",
                            "name": "Alpha News",
                            "tier": "core",
                            "language": "en",
                        }
                    ]
                },
            )
            _write_yaml_mapping(
                recipients_path,
                {
                    "recipients": [
                        {"email": "primary@example.com", "name": "Primary Recipient", "pause": True}
                    ]
                },
            )

            with patch.dict(
                os.environ,
                {
                    "NEWS_SOURCES_YAML": str(sources_path),
                    "NEWS_RECIPIENTS_YAML": str(recipients_path),
                    "NEWS_TEST_SECRET": "swordfish",  # pragma: allowlist secret
                },
                clear=False,
            ):
                self.assertTrue(build_knob_registry())
                # Drift-guard: the three model knobs carry non-empty per-option
                # HF links and survive JSON round-trip (schema_payload
                # serializes knobs with _send_json).
                real_knobs = build_knob_registry()
                model_knob_envs = {
                    "NEWS_MODEL",
                    "NEWS_MODEL_ARTICLE_SUMMARY",
                    "NEWS_MODEL_STORY_DRAFTING",
                }
                for knob in real_knobs:
                    if knob["type"] == "select" and knob["env"] in model_knob_envs:
                        self.assertTrue(knob["option_links"])
                        self.assertEqual(
                            set(knob["option_links"]), set(knob["options"])
                        )
                        self.assertTrue(
                            all(
                                link["page"].startswith("https://huggingface.co/")
                                for link in knob["option_links"].values()
                            )
                        )
                        json.dumps(knob)  # must stay JSON-serializable
                self.assertEqual(
                    _config_path_from_env("NEWS_SOURCES_YAML", "config/sources.yaml"),
                    sources_path,
                )
                self.assertEqual(_mask_secret("shh"), "********")
                self.assertEqual(_mask_secret(""), "")
                self.assertEqual(_clean_env_for_config({"NEWS_TOPIC_IDS": "legacy", "X": "1"}), {"X": "1"})
                self.assertEqual(
                    _json_ready(_Sample("item", root / "nested" / "path")),
                    {"name": "item", "path": str(root / "nested" / "path")},
                )
                self.assertEqual(
                    _json_ready([root / "one", (root / "two",)]),
                    [str(root / "one"), [str(root / "two")]],
                )

                with patch.object(
                    ui_module,
                    "run_preset_env",
                    return_value={"NEWS_SOURCE_SCOPE": "core", "NEWS_KEEP": "1"},
                ):
                    base_env = _ui_base_env("daily", {"NEWS_KEEP": "override"})
                    inherited = _preset_env_over_inherited_env("daily", {"NEWS_KEEP": "override"})
                self.assertNotIn("NEWS_SOURCE_SCOPE", base_env)
                self.assertEqual(inherited, {})
                self.assertEqual(_preset_env_over_inherited_env(None, {"NEWS_KEEP": "override"}), {})

                with patch.object(ui_module, "run_preset_env", side_effect=ValueError("missing")):
                    self.assertEqual(_ui_base_env("missing", {}), _clean_env_for_config(dict(os.environ)))
                    self.assertEqual(_preset_env_over_inherited_env("missing", {}), {})

                runtime_config = SimpleNamespace(
                    preset_id="daily",
                    prompt_profile_id="balanced",
                    source_scope="core",
                    recipient_scope="primary",
                    url_reuse_blocking_enabled=True,
                    relaxed_story_drafting_guards=False,
                    sources_path=sources_path,
                    recipients_path=recipients_path,
                    output_dir=root / "output",
                    run_output_dir=root / "output" / ".staging",
                    run_used_urls_path=root / "output" / "used_urls.txt",
                    model_reference="gemma-2b",
                    model_name="default",
                    model_backend="mlx-lm",
                    model_base_url="http://localhost:8080",
                    model_concurrency=2,
                    article_summary_concurrency=1,
                    story_synthesis_concurrency=1,
                    model_server_command="python -m server",
                    model_assignments={
                        "article_summary": {"reference": "gemma-2b"},
                        "story_drafting": {"reference": "gemma-2b"},
                    },
                    model_tuning={"default": "base"},
                    pipeline_budget={
                        "article_text_token_limit": 900,
                        "total_article_summary_cap": 1200,
                        "recent_window_hours": 48,
                        "max_articles_per_source": 25,
                        "min_articles_per_story": 2,
                        "max_stories": 4,
                    },
                    model_server_settings={"host": "127.0.0.1"},
                    recent_window_hours=48,
                    source_collection_concurrency=4,
                    max_articles_per_source=25,
                    min_articles_per_story=2,
                    story_cluster_similarity_threshold=0.3,
                    story_scale_screening_enabled=True,
                    max_stories=4,
                    story_selection_overlap_threshold=0.2,
                    story_embedding_dedup_threshold=0.9,
                    story_backfill_batch_multiplier=2,
                    image_generation_enabled=True,
                    image_generation_fail_on_error=False,
                    image_width=1024,
                    image_height=512,
                    image_steps=20,
                    image_crop_bottom_ratio=0.1,
                    image_model_id="model-id",
                    image_base_model="base-model",
                    primary_recipient="primary@example.com",
                    email_recipients_fallback=["a@example.com"],
                    email_from="news@example.com",
                    smtp_host="smtp.example.com",
                    smtp_port=587,
                    smtp_username="news",
                    smtp_use_ssl=True,
                    smtp_password="secret",  # pragma: allowlist secret
                    unsubscribe_base_url="https://example.com",
                    unsubscribe_host="0.0.0.0",
                    unsubscribe_port=9000,
                    unsubscribe_secret="token",  # pragma: allowlist secret
                )
                runtime = SimpleNamespace(
                    config=runtime_config,
                    command_env_delta={"NEWS_PRESET": "daily"},
                )
                with patch.object(ui_module, "resolve_runtime_config", return_value=runtime):
                    snapshot, error = _runtime_snapshot({"NEWS_SOURCE_SCOPE": "core"}, preset_id="daily")
                self.assertIsNone(error)
                self.assertEqual(snapshot["preset_id"], "daily")
                self.assertEqual(snapshot["prompt_profile_id"], "balanced")
                self.assertEqual(snapshot["model"]["reference"], "gemma-2b")
                self.assertEqual(snapshot["delivery"]["unsubscribe_secret_set"], True)

                with patch.object(ui_module, "resolve_runtime_config", side_effect=RuntimeError("boom")):
                    snapshot, error = _runtime_snapshot({}, preset_id="daily")
                self.assertIsNone(snapshot)
                self.assertEqual(error, "boom")

                with patch.object(
                    ui_module,
                    "_runtime_snapshot",
                    return_value=(
                        {"prompt_profile_id": "balanced"},
                        None,
                    ),
                ), patch.object(
                    ui_module, "configured_removed_topic_env_vars", return_value=[]
                ):
                    payload = ui_module.schema_payload()
                self.assertEqual(len(payload["prompt_profiles"]), 5)
                self.assertEqual(payload["prompt_profiles"][0]["id"], "balanced")
                self.assertEqual(payload["runtime"]["prompt_profile_id"], "balanced")

                with patch.object(ui_module, "load_sources", return_value=[{"key": "Alpha"}]):
                    self.assertEqual(
                        _source_summary(),
                        {
                            "path": str(sources_path),
                            "total": 1,
                            "selected": {scope: 1 for scope in ui_module.SOURCE_SCOPES},
                            "tiers": {"core": 1},
                            "languages": {"en": 1},
                            "error": None,
                        },
                    )
                with patch.object(ui_module, "_load_yaml_mapping", return_value={"sources": "bad"}), patch.object(
                    ui_module, "load_sources", return_value=[{"key": "Alpha"}]
                ):
                    self.assertEqual(_source_summary()["total"], 0)
                with patch.object(ui_module, "_load_yaml_mapping", return_value={"sources": [{"key": "Alpha"}]}), patch.object(
                    ui_module, "load_sources", side_effect=RuntimeError("source boom")
                ):
                    self.assertEqual(_source_summary()["selected"], {scope: None for scope in ui_module.SOURCE_SCOPES})
                with patch.object(ui_module, "_load_yaml_mapping", side_effect=RuntimeError("broken")):
                    self.assertEqual(_source_summary()["error"], "broken")
                with patch.object(
                    ui_module,
                    "_load_yaml_mapping",
                    return_value={"sources": [1, {"tier": "core", "language": "en"}]},
                ), patch.object(ui_module, "load_sources", return_value=[{"key": "Alpha"}]):
                    self.assertEqual(_source_summary()["total"], 2)
                self.assertEqual(
                    _recipient_summary(),
                    {
                        "path": str(recipients_path),
                        "total": 1,
                        "paused": 1,
                        "error": None,
                    },
                )
                with patch.object(ui_module, "_load_yaml_mapping", return_value={"recipients": "bad"}):
                    self.assertEqual(list_recipients()["recipients"], [])
                with patch.object(ui_module, "load_recipients", side_effect=RuntimeError("broken")):
                    self.assertEqual(_recipient_summary()["error"], "broken")

            with patch.object(ui_module, "build_knob_registry", return_value=[{"env": "NEWS_TEST_SECRET", "label": "Secret", "type": "password", "secret": True}]), patch.object(ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, None)), patch.object(ui_module, "configured_removed_topic_env_vars", return_value={"NEWS_TOPIC_IDS"}), patch.object(ui_module, "list_presets", return_value={"path": "presets.yaml", "presets": [{"id": "daily"}]}), patch.object(ui_module, "list_model_tuning_presets", return_value={"path": "model.yaml", "presets": [{"id": "tiny"}]}), patch.object(ui_module, "_source_summary", return_value={"path": str(sources_path), "total": 1, "selected": {}, "tiers": {}, "languages": {}, "error": None}), patch.object(ui_module, "_recipient_summary", return_value={"path": str(recipients_path), "total": 1, "paused": 0, "error": None}):
                with patch.dict(os.environ, {"NEWS_TEST_SECRET": "swordfish"}, clear=False):  # pragma: allowlist secret
                    payload = schema_payload()

            self.assertEqual(payload["actions"][0], "run")
            self.assertEqual(payload["current_env"]["NEWS_TEST_SECRET"], "********")
            self.assertEqual(payload["removed_topic_env_vars"], ["NEWS_TOPIC_IDS"])
            self.assertEqual(payload["runtime"], {"runtime": "ok"})
            self.assertEqual(payload["sources"]["total"], 1)
            self.assertEqual(payload["recipients"]["total"], 1)
            # Model catalog keys are local-only (offline) additions.
            self.assertEqual(len(payload["model_catalog"]), 3)
            self.assertEqual(payload["model_catalog"][0]["alias"], "qwythos-9b-8bit")
            self.assertIn("factual_extraction", payload["model_recommendation_tasks"])
            self.assertEqual(len(payload["model_recommendation_tasks"]), 7)

            helper_file = root / "nested" / "payload.yaml"
            helper_file.parent.mkdir(parents=True)
            self.assertEqual(_load_yaml_mapping(helper_file), {})
            _write_yaml_mapping(helper_file, {"alpha": 1}, header="# Header")
            self.assertTrue(helper_file.read_text(encoding="utf-8").startswith("# Header"))
            self.assertEqual(_load_yaml_mapping(helper_file), {"alpha": 1})
            helper_file.write_text("- not-a-mapping\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                _load_yaml_mapping(helper_file)

            self.assertEqual(_coerce_preset_env({"A": "1", "B": "", "C": None}), {"A": "1"})
            self.assertEqual(_coerce_preset_env("bad"), {})
            self.assertEqual(_coerce_optional_mapping({"tuning": None}, "tuning"), {})
            self.assertEqual(_coerce_optional_mapping({"tuning": "bad"}, "tuning"), {})
            self.assertIsNone(_coerce_optional_mapping({}, "tuning"))
            self.assertTrue(_coerce_bool(True))
            self.assertTrue(_coerce_bool("yes"))
            self.assertFalse(_coerce_bool("0"))
            self.assertFalse(_coerce_bool(None))
            self.assertEqual(_normalize_env_overrides({"A": "1", "B": "", "C": None}), {"A": "1"})
            self.assertEqual(_normalize_env_overrides("bad"), {})
            args: list[str] = []
            _add_option(args, "--limit", 3)
            _add_option(args, "--skip", "")
            _add_bool_option(args, "--json", "yes")
            _add_bool_option(args, "--quiet", "0")
            self.assertEqual(args, ["--limit", "3", "--json"])
            self.assertEqual(_body_preset_id({"preset": "Daily"}), "Daily")
            self.assertEqual(_body_preset_id({"preset_id": "Nightly"}), "Nightly")
            self.assertEqual(_body_preset_id({}), "")

    def test_crud_helpers_use_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets_path = root / "run_presets.yaml"
            tuning_path = root / "model_tuning_presets.yaml"
            sources_path = root / "sources.yaml"
            recipients_path = root / "recipients.yaml"

            _write_yaml_mapping(
                presets_path,
                {"presets": {"seed": {"name": "Seed", "description": "seed", "env": {"A": "1"}}}},
            )
            _write_yaml_mapping(
                tuning_path,
                {
                    "presets": {
                        "tiny": {
                            "name": "Tiny",
                            "description": "tiny",
                            "model": "gemma",
                            "task": "article_summary",
                            "tuning": {"temperature": 0.1},
                        }
                    }
                },
            )
            _write_yaml_mapping(
                sources_path,
                {"sources": [{"key": "Alpha", "name": "Alpha News", "url": "https://example.com"}]},
            )
            _write_yaml_mapping(
                recipients_path,
                {"recipients": [{"email": "primary@example.com", "name": "Primary Recipient", "pause": False}]},
            )

            with patch.object(ui_module, "RUN_PRESETS_PATH", presets_path), patch.object(
                ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path
            ), patch.dict(
                os.environ,
                {
                    "NEWS_SOURCES_YAML": str(sources_path),
                    "NEWS_RECIPIENTS_YAML": str(recipients_path),
                },
                clear=False,
            ):
                self.assertEqual(list_presets()["presets"][0]["id"], "seed")
                self.assertEqual(list_model_tuning_presets()["presets"][0]["id"], "tiny")
                self.assertEqual(list_sources()["sources"][0]["key"], "Alpha")
                self.assertEqual(list_recipients()["recipients"][0]["email"], "primary@example.com")

                with self.assertRaisesRegex(ValueError, "required"):
                    upsert_preset({})
                with self.assertRaisesRegex(ValueError, "required"):
                    upsert_model_tuning_preset({})
                with self.assertRaisesRegex(ValueError, "required"):
                    ui_module.duplicate_preset({})
                with self.assertRaisesRegex(ValueError, "not found"):
                    ui_module.duplicate_preset({"source_id": "missing", "target_id": "copy"})
                with self.assertRaisesRegex(ValueError, "already exists"):
                    ui_module.duplicate_preset({"source_id": "seed", "target_id": "seed"})
                with self.assertRaisesRegex(ValueError, "required"):
                    upsert_source({})
                with self.assertRaisesRegex(ValueError, "required"):
                    upsert_recipient({})

                preset = upsert_preset({"id": "daily", "updates": {"name": "Daily", "env": {"NEWS_MODEL": "gemma"}}})
                self.assertEqual(preset["preset"]["id"], "daily")
                self.assertEqual(
                    upsert_preset({"id": "daily", "updates": {"description": "updated"}})["preset"]["description"],
                    "updated",
                )
                with self.assertRaisesRegex(ValueError, "already exists"):
                    upsert_preset({"id": "daily", "updates": {"name": "Duplicate"}}, append_only=True)
                duplicated = ui_module.duplicate_preset({"source_id": "daily", "target_id": "daily-copy"})
                self.assertEqual(duplicated["preset"]["id"], "daily-copy")
                self.assertEqual(delete_preset("daily-copy")["deleted"], "daily-copy")
                with self.assertRaisesRegex(ValueError, "not found"):
                    delete_preset("daily-copy")

                tuning = upsert_model_tuning_preset(
                    {
                        "id": "giant",
                        "updates": {
                            "name": "Giant",
                            "model": "gemma",
                            "task": "story_drafting",
                            "tuning": {"temperature": 0.2},
                        },
                    }
                )
                self.assertEqual(tuning["preset"]["id"], "giant")
                with self.assertRaisesRegex(ValueError, "already exists"):
                    upsert_model_tuning_preset({"id": "giant"}, append_only=True)
                self.assertEqual(delete_model_tuning_preset("giant")["deleted"], "giant")
                with self.assertRaisesRegex(ValueError, "not found"):
                    delete_model_tuning_preset("giant")

                source = upsert_source(
                    {
                        "key": "Beta",
                        "updates": {"name": "Beta News", "url": "https://beta.example", "tier": "core"},
                    }
                )
                self.assertEqual(source["source"]["key"], "Beta")
                self.assertEqual(delete_source("Beta")["deleted"], "Beta")
                with self.assertRaisesRegex(ValueError, "not found"):
                    delete_source("Beta")

                recipient = upsert_recipient(
                    {
                        "email": "alice@example.com",
                        "updates": {"name": "Alice", "pause": "yes"},
                    }
                )
                self.assertEqual(recipient["recipient"]["pause"], True)
                with self.assertRaisesRegex(ValueError, "already exists"):
                    upsert_recipient({"email": "alice@example.com", "updates": {"name": "Alice 2"}}, append_only=True)
                self.assertEqual(delete_recipient("alice@example.com")["deleted"], "alice@example.com")
                with self.assertRaisesRegex(ValueError, "not found"):
                    delete_recipient("alice@example.com")

    def test_build_command_and_preview_payload_variants(self) -> None:
        base_resolution = SimpleNamespace(command_env_delta={"NEWS_PRESET": "daily", "BASE": "1"})

        with patch.object(ui_module, "_ui_base_env", return_value={"BASE_ENV": "1"}), patch.object(
            ui_module, "_preset_env_over_inherited_env", return_value={"FROM_PRESET": "1"}
        ), patch.object(
            ui_module, "resolve_runtime_config", return_value=base_resolution
        ):
            command, env = build_command({"action": "run", "preset": "daily"})
            self.assertEqual(command, ["uv", "run", "news", "run", "--preset", "daily"])
            self.assertEqual(env["NEWS_PRESET"], "daily")
            self.assertEqual(env["BASE"], "1")

            command, env = build_command({"action": "model-server-command", "preset": "daily"})
            self.assertEqual(command[-1], "model-server-command")
            self.assertEqual(env["NEWS_PRESET"], "daily")

            command, env = build_command({"action": "codex-model-server-command"})
            self.assertEqual(command[-1], "codex-model-server-command")
            self.assertEqual(env["NEWS_PRESET"], "daily")

            command, env = build_command({"action": "serve-unsubscribe", "preset": "daily"})
            self.assertEqual(command[-1], "serve-unsubscribe")
            self.assertEqual(env["NEWS_PRESET"], "daily")

            command, env = build_command(
                {
                    "action": "check-sources",
                    "options": {
                        "sources_yaml": "sources.yaml",
                        "timeout": 10,
                        "concurrency": 4,
                        "recent_days": 7,
                        "probe_articles": True,
                        "prune_unscrapable": True,
                        "only_failures": False,
                        "write_languages": True,
                        "overwrite_languages": True,
                        "language_model": "lm",
                        "language_samples": 5,
                        "min_language_confidence": 0.8,
                        "limit": 3,
                        "section": "sources",
                        "json": True,
                    },
                }
            )
            self.assertEqual(
                command,
                [
                    "uv",
                    "run",
                    "news",
                    "check-sources",
                    "--sources-yaml",
                    "sources.yaml",
                    "--timeout",
                    "10",
                    "--concurrency",
                    "4",
                    "--recent-days",
                    "7",
                    "--probe-articles",
                    "--prune-unscrapable",
                    "--write-languages",
                    "--overwrite-languages",
                    "--language-model",
                    "lm",
                    "--language-samples",
                    "5",
                    "--min-language-confidence",
                    "0.8",
                    "--limit",
                    "3",
                    "--section",
                    "sources",
                    "--json",
                ],
            )
            self.assertEqual(env["FROM_PRESET"], "1")

            with self.assertRaisesRegex(ValueError, "Unsupported action"):
                build_command({"action": "unknown-action"})

        with patch.object(ui_module, "_ui_base_env", return_value={}), patch.object(
            ui_module, "_preset_env_over_inherited_env", return_value={}
        ), patch.object(
            ui_module,
            "resolve_runtime_config",
            side_effect=ValueError("Topic-based runtime configuration has been removed"),
        ):
            command, env = build_command({"action": "run", "preset": "daily", "env": {"X": "1"}})
            self.assertEqual(command, ["uv", "run", "news", "run", "--preset", "daily"])
            self.assertEqual(env["NEWS_PRESET"], "daily")
            self.assertEqual(env["X"], "1")

        with patch.object(ui_module, "_ui_base_env", return_value={}), patch.object(
            ui_module, "_preset_env_over_inherited_env", return_value={}
        ), patch.object(
            ui_module,
            "resolve_runtime_config",
            side_effect=ValueError("unexpected failure"),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected failure"):
                build_command({"action": "run"})

        with patch.object(ui_module, "build_command", return_value=(["uv", "run", "news", "run"], {"A": "1", "B": "two words"})), patch.object(
            ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, "preview error")
        ), patch.object(
            ui_module, "configured_removed_topic_env_vars", return_value={"NEWS_TOPIC_IDS"}
        ):
            preview = preview_payload({"action": "run"})
        self.assertEqual(preview["command_text"], "A=1 B='two words' uv run news run")
        self.assertEqual(preview["env"], {"A": "1", "B": "two words"})
        self.assertEqual(preview["runtime"], {"runtime": "ok"})
        self.assertEqual(preview["runtime_error"], "preview error")
        self.assertEqual(preview["removed_topic_env_vars"], ["NEWS_TOPIC_IDS"])

    def test_run_record_and_manager_processes(self) -> None:
        record = RunRecord("run-1", ["news", "run"], {"PASSWORD": "secret", "VISIBLE": "ok"})
        record.append("line one\n")
        snapshot = record.snapshot()
        self.assertEqual(snapshot["env"]["PASSWORD"], "********")
        self.assertEqual(snapshot["env"]["VISIBLE"], "ok")
        self.assertEqual(snapshot["line_count"], 1)

        manager = RunManager()
        self.assertIsNone(manager.get("missing"))
        self.assertEqual(manager.list(), [])
        with self.assertRaisesRegex(ValueError, "not found"):
            manager.stop("missing")

        class _FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False

            def start(self) -> None:
                self.started = True

        with patch.object(ui_module, "build_command", return_value=(["news", "run"], {"A": "1"})), patch.object(
            ui_module, "uuid"
        ) as uuid_module, patch.object(ui_module.threading, "Thread", _FakeThread):
            uuid_module.uuid4.return_value.hex = "0123456789abcdef"
            started = manager.start({"action": "run"})
        self.assertEqual(started.run_id, "0123456789ab")
        self.assertIs(manager.get(started.run_id), started)
        self.assertEqual(manager.list()[0]["run_id"], started.run_id)

        class _SuccessProcess:
            def __init__(self) -> None:
                self.stdout = iter(["hello\n", "world\n"])

            def wait(self) -> int:
                return 0

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

        success_record = RunRecord("run-2", ["news", "run"], {})
        with patch.object(ui_module.subprocess, "Popen", return_value=_SuccessProcess()):
            manager._run_process(success_record)
        self.assertEqual(success_record.status, "completed")
        self.assertEqual(success_record.returncode, 0)
        self.assertIn("[ui] process exited with code 0\n", success_record.lines[-1])

        failure_record = RunRecord("run-3", ["news", "run"], {})
        with patch.object(ui_module.subprocess, "Popen", side_effect=OSError("boom")):
            manager._run_process(failure_record)
        self.assertEqual(failure_record.status, "failed")
        self.assertEqual(failure_record.returncode, -1)
        self.assertIn("failed to start process", failure_record.lines[-1])

        running_record = RunRecord("run-4", ["news", "run"], {})
        running_record.process = _SuccessProcess()
        manager.runs[running_record.run_id] = running_record
        stopped = manager.stop(running_record.run_id)
        self.assertEqual(stopped["status"], "stopping")

    def test_http_routes_and_entrypoints(self) -> None:
        with patch.object(
            ui_module,
            "schema_payload",
            return_value={"schema": True},
        ), patch.object(ui_module, "list_presets", return_value={"presets": []}), patch.object(
            ui_module, "list_model_tuning_presets", return_value={"presets": []}
        ), patch.object(ui_module, "list_sources", return_value={"sources": []}), patch.object(
            ui_module, "list_recipients", return_value={"recipients": []}
        ), patch.object(
            ui_module, "preview_payload", return_value={"preview": True}
        ), patch.object(
            ui_module, "upsert_preset", return_value={"preset": {"id": "daily"}}
        ), patch.object(
            ui_module, "upsert_model_tuning_preset", return_value={"preset": {"id": "tiny"}}
        ), patch.object(
            ui_module, "duplicate_preset", return_value={"preset": {"id": "copy"}}
        ), patch.object(
            ui_module, "upsert_source", return_value={"source": {"key": "Alpha"}}
        ), patch.object(
            ui_module, "upsert_recipient", return_value={"recipient": {"email": "a@example.com"}}
        ), patch.object(
            ui_module, "delete_source", return_value={"deleted": "Alpha"}
        ), patch.object(
            ui_module, "delete_preset", return_value={"deleted": "daily"}
        ), patch.object(
            ui_module, "delete_model_tuning_preset", return_value={"deleted": "tiny"}
        ), patch.object(
            ui_module, "delete_recipient", return_value={"deleted": "a@example.com"}
        ), patch.object(
            ui_module, "compare_prompt_profiles", return_value={"story_drafting": "diff"}
        ), patch.object(
            ui_module.RUN_MANAGER, "start", return_value=SimpleNamespace(snapshot=lambda: {"run_id": "run-1"})
        ), patch.object(
            ui_module.RUN_MANAGER, "stop", return_value={"run_id": "run-1", "status": "stopped"}
        ):
            def invoke(method: str, path: str, body: str | None = None) -> tuple[int, dict[str, str], str]:
                payload = (body or "").encode("utf-8")
                handler = object.__new__(ui_module.NewsUIHandler)
                state: dict[str, Any] = {"status": None, "headers": {}}
                handler.path = path
                handler.headers = {"Content-Length": str(len(payload))}
                handler.rfile = BytesIO(payload)  # type: ignore[assignment]
                handler.wfile = BytesIO()  # type: ignore[assignment]
                handler.send_response = lambda status: state.__setitem__("status", status)
                handler.send_header = lambda name, value: state["headers"].__setitem__(name, value)
                handler.end_headers = lambda: None
                getattr(handler, method)()
                return state["status"], state["headers"], handler.wfile.getvalue().decode("utf-8")  # type: ignore[attr-defined]

            status, headers, body = invoke("do_GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("News Control Panel", body)

            self.assertEqual(invoke("do_GET", "/api/schema")[0], 200)
            self.assertEqual(json.loads(invoke("do_GET", "/api/presets")[2]), {"presets": []})
            self.assertEqual(json.loads(invoke("do_GET", "/api/model-tuning-presets")[2]), {"presets": []})
            self.assertEqual(
                json.loads(invoke("do_GET", "/api/prompt-profiles/compare?profile=playful")[2]),
                {"profile": "playful", "baseline": "balanced", "diffs": {"story_drafting": "diff"}},
            )
            # Missing or empty profile param falls back to the catalog default.
            self.assertEqual(
                json.loads(invoke("do_GET", "/api/prompt-profiles/compare")[2]),
                {"profile": "balanced", "baseline": "balanced", "diffs": {"story_drafting": "diff"}},
            )
            self.assertEqual(
                json.loads(invoke("do_GET", "/api/prompt-profiles/compare?profile=")[2]),
                {"profile": "balanced", "baseline": "balanced", "diffs": {"story_drafting": "diff"}},
            )
            with patch.object(
                ui_module,
                "compare_prompt_profiles",
                side_effect=ValueError("Unknown prompt profile 'bogus'."),
            ):
                status, _, body = invoke("do_GET", "/api/prompt-profiles/compare?profile=bogus")
                self.assertEqual(status, 400)
                self.assertIn("Unknown prompt profile", body)
            self.assertEqual(json.loads(invoke("do_GET", "/api/sources")[2]), {"sources": []})
            self.assertEqual(json.loads(invoke("do_GET", "/api/recipients")[2]), {"recipients": []})
            self.assertEqual(json.loads(invoke("do_GET", "/api/runs")[2]), {"runs": []})
            with patch.object(
                ui_module.RUN_MANAGER,
                "get",
                return_value=SimpleNamespace(snapshot=lambda: {"run_id": "run-1"}),
            ):
                self.assertEqual(json.loads(invoke("do_GET", "/api/runs/run-1")[2])["run_id"], "run-1")
            self.assertEqual(invoke("do_GET", "/api/runs/missing")[0], 404)
            self.assertEqual(invoke("do_GET", "/does-not-exist")[0], 404)

            self.assertEqual(json.loads(invoke("do_POST", "/api/preview", body=json.dumps({"action": "run"}))[2]), {"preview": True})
            self.assertEqual(json.loads(invoke("do_POST", "/api/preview")[2]), {"preview": True})
            self.assertEqual(json.loads(invoke("do_POST", "/api/preview", body="[]")[2]), {"error": "JSON request body must be an object."})
            self.assertEqual(invoke("do_POST", "/api/preview", body="{")[0], 400)
            self.assertEqual(json.loads(invoke("do_POST", "/api/run", body=json.dumps({"action": "run"}))[2])["run_id"], "run-1")
            self.assertEqual(json.loads(invoke("do_POST", "/api/presets", body=json.dumps({"id": "daily"}))[2]), {"preset": {"id": "daily"}})
            self.assertEqual(json.loads(invoke("do_POST", "/api/model-tuning-presets", body=json.dumps({"id": "tiny"}))[2]), {"preset": {"id": "tiny"}})
            self.assertEqual(json.loads(invoke("do_POST", "/api/presets/duplicate", body=json.dumps({"source_id": "daily", "target_id": "copy"}))[2]), {"preset": {"id": "copy"}})
            self.assertEqual(json.loads(invoke("do_POST", "/api/sources", body=json.dumps({"key": "Alpha"}))[2]), {"source": {"key": "Alpha"}})
            self.assertEqual(json.loads(invoke("do_POST", "/api/recipients", body=json.dumps({"email": "a@example.com"}))[2]), {"recipient": {"email": "a@example.com"}})
            self.assertEqual(json.loads(invoke("do_POST", "/api/runs/run-1/stop", body="{}")[2]), {"run_id": "run-1", "status": "stopped"})
            self.assertEqual(invoke("do_POST", "/api/unknown", body="{}")[0], 404)

            with patch.object(ui_module, "schema_payload", side_effect=RuntimeError("boom")):
                self.assertEqual(invoke("do_GET", "/api/schema")[0], 400)

            with patch.object(ui_module, "preview_payload", side_effect=RuntimeError("boom")):
                self.assertEqual(invoke("do_POST", "/api/preview", body=json.dumps({"action": "run"}))[0], 400)

            self.assertEqual(json.loads(invoke("do_PATCH", "/api/sources", body=json.dumps({"key": "Alpha"}))[2]), {"source": {"key": "Alpha"}})
            self.assertEqual(json.loads(invoke("do_PATCH", "/api/presets", body=json.dumps({"id": "daily"}))[2]), {"preset": {"id": "daily"}})
            self.assertEqual(json.loads(invoke("do_PATCH", "/api/model-tuning-presets", body=json.dumps({"id": "tiny"}))[2]), {"preset": {"id": "tiny"}})
            self.assertEqual(json.loads(invoke("do_PATCH", "/api/recipients", body=json.dumps({"email": "a@example.com"}))[2]), {"recipient": {"email": "a@example.com"}})
            self.assertEqual(invoke("do_PATCH", "/api/unknown", body=json.dumps({}))[0], 404)

            with patch.object(ui_module, "upsert_source", side_effect=RuntimeError("boom")):
                self.assertEqual(invoke("do_PATCH", "/api/sources", body=json.dumps({"key": "Alpha"}))[0], 400)

            self.assertEqual(json.loads(invoke("do_DELETE", "/api/sources?key=Alpha")[2]), {"deleted": "Alpha"})
            self.assertEqual(json.loads(invoke("do_DELETE", "/api/presets?id=daily")[2]), {"deleted": "daily"})
            self.assertEqual(json.loads(invoke("do_DELETE", "/api/model-tuning-presets?id=tiny")[2]), {"deleted": "tiny"})
            self.assertEqual(json.loads(invoke("do_DELETE", "/api/recipients?email=a@example.com")[2]), {"deleted": "a@example.com"})
            self.assertEqual(invoke("do_DELETE", "/api/unknown")[0], 404)

            with patch.object(ui_module, "delete_source", side_effect=RuntimeError("boom")):
                self.assertEqual(invoke("do_DELETE", "/api/sources?key=Alpha")[0], 400)

            fake_record = SimpleNamespace(
                lock=threading.Lock(),
                lines=["run line\n"],
                status="completed",
                snapshot=lambda: {"run_id": "run-1", "status": "completed"},
            )
            with patch.object(ui_module.RUN_MANAGER, "get", return_value=None):
                self.assertEqual(invoke("do_GET", "/api/runs/run-1/events")[0], 404)
            with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_record):
                status, headers, body = invoke("do_GET", "/api/runs/run-1/events")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/event-stream")
            self.assertIn("data: {\"line\": \"run line\\n\", \"status\": \"completed\"}", body)
            self.assertIn("event: status", body)

            log_handler = object.__new__(ui_module.NewsUIHandler)
            log_handler.address_string = lambda: "127.0.0.1"
            with contextlib.redirect_stdout(StringIO()) as stdout:
                ui_module.NewsUIHandler.log_message(log_handler, "%s", "hello")
            self.assertIn("[news-ui] 127.0.0.1 - hello", stdout.getvalue())

        class _FakeServer:
            def __init__(self, address, handler):
                self.server_address = ("127.0.0.1", 9876)
                self.serve_forever_called = False
                self.server_close_called = False

            def serve_forever(self) -> None:
                self.serve_forever_called = True

            def server_close(self) -> None:
                self.server_close_called = True

        fake_server = _FakeServer(("127.0.0.1", 0), ui_module.NewsUIHandler)
        with patch.object(ui_module, "NewsUIServer", return_value=fake_server), patch.object(
            ui_module.webbrowser, "open"
        ) as open_browser, contextlib.redirect_stdout(StringIO()) as stdout:
            self.assertEqual(serve_ui(DEFAULT_HOST, DEFAULT_PORT, open_browser=True), 0)
        self.assertTrue(fake_server.serve_forever_called)
        self.assertTrue(fake_server.server_close_called)
        open_browser.assert_called_once()
        self.assertIn("News control panel: http://127.0.0.1:9876", stdout.getvalue())

        class _InterruptingServer(_FakeServer):
            def serve_forever(self) -> None:
                self.serve_forever_called = True
                raise KeyboardInterrupt()

        interrupting_server = _InterruptingServer(("127.0.0.1", 0), ui_module.NewsUIHandler)
        with patch.object(ui_module, "NewsUIServer", return_value=interrupting_server), contextlib.redirect_stdout(
            StringIO()
        ) as stdout:
            self.assertEqual(serve_ui(DEFAULT_HOST, DEFAULT_PORT, open_browser=False), 0)
        self.assertTrue(interrupting_server.serve_forever_called)
        self.assertTrue(interrupting_server.server_close_called)
        self.assertIn("Stopping news control panel.", stdout.getvalue())

        with patch.object(ui_module, "serve_ui", return_value=0) as serve:
            self.assertEqual(main(["--host", "0.0.0.0", "--port", "9000", "--open"]), 0)
        serve.assert_called_once_with("0.0.0.0", 9000, open_browser=True)

    def _invoke_get(self, path: str) -> tuple[int, dict[str, str], str]:
        handler = object.__new__(ui_module.NewsUIHandler)
        state: dict[str, Any] = {"status": None, "headers": {}}
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.rfile = BytesIO(b"")
        handler.wfile = BytesIO()  # type: ignore[assignment]
        handler.send_response = lambda status: state.__setitem__("status", status)
        handler.send_header = lambda name, value: state["headers"].__setitem__(name, value)
        handler.end_headers = lambda: None
        handler.do_GET()
        return state["status"], state["headers"], handler.wfile.getvalue().decode("utf-8")  # type: ignore[attr-defined]

    def test_models_search_endpoint_error_and_success(self) -> None:
        fake_models = [
            {
                "id": "owner/one",
                "hf_url": "https://huggingface.co/owner/one",
                "runtime_fit": {"status": "managed_mlx_lm", "reason": "ok"},
            }
        ]
        with patch.object(
            ui_module, "search_huggingface_models", return_value=fake_models
        ) as search:
            status, _, body = self._invoke_get(
                "/api/models/search?q=qwythos&pipeline_tag=text-generation&limit=5"
            )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["query"], "qwythos")
        self.assertEqual(payload["models"], fake_models)
        self.assertIsNone(payload["error"])
        search.assert_called_once_with("qwythos", pipeline_tag="text-generation", limit=5)

        with patch.object(
            ui_module, "search_huggingface_models", side_effect=RuntimeError("hf down")
        ):
            status, _, body = self._invoke_get("/api/models/search?q=qwythos")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["error"], "hf down")

        status, _, body = self._invoke_get("/api/models/search")
        self.assertEqual(status, 400)
        self.assertIn("Missing query parameter q.", json.loads(body)["error"])

    def test_models_metadata_endpoint(self) -> None:
        fake_info = {
            "id": "owner/repo",
            "hf_url": "https://huggingface.co/owner/repo",
            "runtime_fit": {"status": "external_only", "reason": "unknown"},
        }
        with patch.object(
            ui_module, "fetch_model_metadata", return_value=fake_info
        ) as fetch:
            status, _, body = self._invoke_get("/api/models/metadata?model=owner%2Frepo")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["model"], "owner/repo")
        self.assertEqual(payload["info"], fake_info)
        self.assertIsNone(payload["error"])
        fetch.assert_called_once_with("owner/repo")

        with patch.object(
            ui_module, "fetch_model_metadata", side_effect=ValueError("Model not found on Hugging Face: 'nope'")
        ):
            status, _, body = self._invoke_get("/api/models/metadata?model=nope")

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["info"])
        self.assertIn("Model not found", payload["error"])

        status, _, body = self._invoke_get("/api/models/metadata")
        self.assertEqual(status, 400)
        self.assertIn("Missing model parameter.", json.loads(body)["error"])

    def test_stream_run_events_error_branches(self) -> None:
        class _Writer:
            def __init__(self, fail_prefix: str | None = None) -> None:
                self.fail_prefix = fail_prefix
                self.parts: list[str] = []

            def write(self, data: bytes) -> int:
                text = data.decode("utf-8")
                self.parts.append(text)
                if self.fail_prefix and text.startswith(self.fail_prefix):
                    raise BrokenPipeError()
                return len(data)

            def flush(self) -> None:
                return None

        def invoke_get(path: str) -> tuple[int, dict[str, str], str]:
            handler = object.__new__(ui_module.NewsUIHandler)
            state: dict[str, Any] = {"status": None, "headers": {}}
            handler.path = path
            handler.headers = {"Content-Length": "0"}
            handler.rfile = BytesIO(b"")
            handler.wfile = BytesIO()  # type: ignore[assignment]
            handler.send_response = lambda status: state.__setitem__("status", status)
            handler.send_header = lambda name, value: state["headers"].__setitem__(name, value)
            handler.end_headers = lambda: None
            handler.do_GET()
            return state["status"], state["headers"], handler.wfile.getvalue().decode("utf-8")  # type: ignore[attr-defined]

        def make_handler(writer: _Writer) -> ui_module.NewsUIHandler:
            handler = object.__new__(ui_module.NewsUIHandler)
            handler.path = "/api/runs/run-1/events"
            handler.headers = {}
            handler.rfile = BytesIO(b"")
            handler.wfile = writer  # type: ignore[assignment]
            handler.send_response = lambda *_args, **_kwargs: None
            handler.send_header = lambda *_args, **_kwargs: None
            handler.end_headers = lambda: None
            return handler

        with patch.object(
            ui_module.RUN_MANAGER,
            "get",
            return_value=SimpleNamespace(snapshot=lambda: {"run_id": "run-1"}),
        ):
            status, headers, body = invoke_get("/api/runs/run-1")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn('"run_id": "run-1"', body)

        fake_running_record = SimpleNamespace(
            lock=threading.Lock(),
            lines=["run line\n"],
            status="running",
            snapshot=lambda: {"run_id": "run-1"},
        )
        with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_running_record):
            writer = _Writer(fail_prefix="data:")
            handler = make_handler(writer)
            handler._stream_run_events("run-1")
        self.assertTrue(any(part.startswith("data:") for part in writer.parts))

        fake_done_record = SimpleNamespace(
            lock=threading.Lock(),
            lines=[],
            status="completed",
            snapshot=lambda: {"run_id": "run-1"},
        )
        with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_done_record):
            writer = _Writer(fail_prefix="event: status")
            handler = make_handler(writer)
            handler._stream_run_events("run-1")
        self.assertTrue(any(part.startswith("event: status") for part in writer.parts))

        fake_sleep_record = SimpleNamespace(
            lock=threading.Lock(),
            lines=[],
            status="running",
            snapshot=lambda: {"run_id": "run-1"},
        )
        with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_sleep_record), patch.object(
            ui_module.time,
            "sleep",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                handler = make_handler(_Writer())
                handler._stream_run_events("run-1")

    def test_module_entrypoint_guard_executes(self) -> None:
        source = "\n" * 2453 + 'if __name__ == "__main__":\n    raise SystemExit(main())\n'
        namespace = {"__name__": "__main__", "main": lambda: 0}

        with self.assertRaises(SystemExit) as exc:
            exec(compile(source, ui_module.__file__, "exec"), namespace)

        self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
