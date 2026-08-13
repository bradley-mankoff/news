from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import subprocess
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
from news_pipeline.config import (
    CODEX_TEST_MODEL_ALIAS,
    DELIVERY_MODE_OWNER,
    DeliveryProfile,
    GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
)
from news_pipeline.diagnostics import RunDiagnostics
from news_pipeline.history_store import write_run_history
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
    def test_env_info_tooltips_are_focusable_and_announced(self) -> None:
        html = ui_module.HTML
        # Trigger is keyboard-reachable with an accessible name and ARIA wiring.
        self.assertIn('tabindex="0" role="button" aria-label="Help for ${escapeHtml(name)}" aria-describedby="${tipId}"', html)
        # A real tooltip surface exists (not native-title-only).
        self.assertIn('class="env-tooltip hidden" role="tooltip"', html)
        self.assertIn('${escapeHtml(tip)}', html)
        self.assertNotIn('title="${escapeHtml(tip)}"', html)
        # Keyboard focus must be visible: :focus-visible outline on the trigger.
        self.assertIn(".env-info:focus-visible {", html)
        self.assertIn("outline: 2px solid var(--blue);", html)
        # Idempotency guard: each label is decorated at most once, so re-runs
        # cannot inject duplicate tooltips or duplicate event listeners.
        self.assertIn('titleTarget.querySelector(".env-info")) return;', html)
        # The trigger's aria-describedby and the tooltip's id share ONE counter
        # expression — duplicate ids would break aria-describedby resolution.
        self.assertEqual(html.count('aria-describedby="${tipId}"'), 1)
        self.assertEqual(html.count('id="${tipId}"'), 1)

    def test_env_info_tooltip_show_hide_and_escape_handlers(self) -> None:
        html = ui_module.HTML
        self.assertIn('icon.addEventListener("mouseenter", show)', html)
        self.assertIn('icon.addEventListener("focus", show)', html)
        self.assertIn('icon.addEventListener("blur", hide)', html)
        self.assertIn('icon.addEventListener("keydown", ev => {', html)
        self.assertIn('ev.key === "Escape"', html)
        self.assertIn('ev.preventDefault(); hide();', html)
        # Button semantics: Enter/Space activate the trigger (preventDefault
        # keeps Space from scrolling the page).
        self.assertIn('ev.key === "Enter" || ev.key === " "', html)
        self.assertIn('ev.preventDefault(); show();', html)
        # Scroll (capture-phase, catches nested scroll containers) hides an
        # open tooltip once its trigger leaves the viewport, so it never
        # floats detached from its icon.
        self.assertIn('document.addEventListener("scroll", hideTooltipsOnScroll, true)', html)
        # Resize repositions only open tooltips; hidden ones stay inert.
        self.assertIn('if (!tooltip.classList.contains("hidden")) positionEnvTooltip(icon, tooltip);', html)
        # Delegated listeners are registered exactly once — per-icon global
        # registration would leak on knob-search re-renders.
        self.assertEqual(html.count('document.addEventListener("scroll"'), 1)
        self.assertEqual(html.count('window.addEventListener("resize"'), 1)

    def test_env_info_tooltip_positioning_avoids_viewport_clipping(self) -> None:
        html = ui_module.HTML
        # Fixed positioning escapes the Advanced Settings scroll container.
        self.assertIn("position: fixed;", html)
        # GUTTER keeps a viewport-safe margin on every edge.
        self.assertIn("const GUTTER = 8;", html)
        # Flip above the icon when the tooltip would overflow the viewport bottom.
        self.assertIn("top + t.height > window.innerHeight - GUTTER", html)
        self.assertIn("r.top - t.height - GUTTER", html)
        # Centered horizontally, clamped to the viewport edges.
        self.assertIn("window.innerWidth - t.width - GUTTER", html)
        # Tall hints stay readable on short viewports.
        self.assertIn("max-height: calc(100vh - 16px);", html)
        self.assertIn("overflow-y: auto;", html)

    def test_env_info_tooltips_cover_run_setup_and_advanced_settings(self) -> None:
        html = ui_module.HTML
        # Exact explanatory text for one Run Setup setting and one Advanced-only setting.
        self.assertIn('NEWS_DELIVERY_MODE: "Chooses the delivery policy: no delivery, owner only (default), or explicit configured recipients. Legacy NEWS_RECIPIENT_SCOPE still maps to this mode when set."', html)
        self.assertIn('NEWS_MAX_STORIES: "Maximum number of final stories selected for the report."', html)
        # All three surfaces run the decorator.
        self.assertIn('decorateEnvHints($("runSetupMount"))', html)
        self.assertIn('decorateEnvHints($("advancedPanels"))', html)
        self.assertIn('decorateEnvHints($("knobContainer"))', html)

    def test_advanced_settings_gate_holds_all_knobs(self) -> None:
        html = ui_module.HTML
        # Advanced tab hosts the moved panels; Run Setup no longer does.
        self.assertIn('id="advancedPanels"', html)
        self.assertIn("function renderAdvancedPanels", html)
        self.assertIn("function modelTuningPanel", html)
        run_setup = html.split("function renderRunSetup")[1].split("const SAMPLING_FIELDS")[0]
        self.assertNotIn("Run budgets and quotas", run_setup)
        self.assertNotIn("Optional run settings", run_setup)
        self.assertNotIn("article_tuning_preset", run_setup)
        self.assertNotIn("promptProfileReadouts", run_setup)
        self.assertNotIn("promptProfileCompare", run_setup)
        self.assertNotIn("<summary>Model tuning</summary>", html)
        # Moved panels exist exactly once, inside renderAdvancedPanels.
        advanced = html.split("function renderAdvancedPanels")[1].split("function renderAdvancedKnobs")[0]
        self.assertEqual(advanced.count("Run budgets and quotas"), 1)
        self.assertEqual(advanced.count("Optional run settings"), 1)
        self.assertEqual(advanced.count('id="promptProfileReadouts"'), 1)
        self.assertEqual(advanced.count('id="comparePromptProfileBtn"'), 1)
        self.assertEqual(advanced.count('modelTuningPanel("article_summary")'), 1)
        self.assertEqual(advanced.count('modelTuningPanel("story_drafting")'), 1)
        # Dedicated envs are suppressed from the raw override list (no duplicates).
        surface = html.split("const SURFACED_ENVS")[1].split("const TASK_CONFIG")[0]
        for env in (
            "NEWS_ARTICLE_TEXT_TOKEN_LIMIT",
            "NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE",
            "NEWS_MODEL_STORY_DRAFTING_REPETITION_PENALTY",
        ):
            self.assertIn(f'"{env}"', surface)

    def test_surfaced_envs_are_registered_and_composed(self) -> None:
        import re

        html = ui_module.HTML
        surface = html.split("const SURFACED_ENVS")[1].split("const TASK_CONFIG")[0]
        surfaced = re.findall(r'"NEWS_[A-Z_]+"', surface)
        # No duplicates in the suppression manifest.
        self.assertEqual(len(surfaced), len(set(surfaced)))
        # No typos: every surfaced env must be a real registered knob.
        registry = {knob["env"] for knob in build_knob_registry()}
        self.assertEqual([e for e in surfaced if e.strip('"') not in registry], [])
        # The 12 sampling envs are exactly prefix x suffix compositions, all surfaced.
        suffixes = re.findall(
            r'\["([A-Z_]+)", "',
            html.split("const SAMPLING_FIELDS")[1].split("function samplingFields")[0],
        )
        prefixes = re.findall(r'taskSamplingPrefix: "(NEWS_MODEL_[A-Z_]+)"', html)
        composed = {f"{p}_{s}" for p in prefixes for s in suffixes}
        self.assertEqual(len(composed), 24)
        for env in composed:
            self.assertIn(
                f'"{env}"', surface, f"composed sampling env {env} not suppressed"
            )
        # NEWS_ARTICLE_TEXT_TOKEN_LIMIT (the 13th dedicated env) must also be surfaced.
        self.assertIn('"NEWS_ARTICLE_TEXT_TOKEN_LIMIT"', surface)
        # NEWS_MODEL has a dedicated "Default model" knob in Run Setup, so it
        # must be suppressed from the Advanced raw list (no duplicate inputs).
        self.assertIn('"NEWS_MODEL"', surface)
        # The four per-task model envs moved OUT of Run Setup into Advanced,
        # so they must NOT be suppressed: each appears exactly once, in the
        # Advanced raw override list.
        for env in (
            "NEWS_MODEL_ARTICLE_SUMMARY",
            "NEWS_MODEL_STORY_DRAFTING",
            "NEWS_MODEL_STORY_SCALE_SCREENING",
            "NEWS_MODEL_TITLE_GENERATION",
        ):
            self.assertNotIn(f'"{env}"', surface, f"{env} must stay in the Advanced raw list")

    def test_advanced_panels_rendered_at_boot(self) -> None:
        html = ui_module.HTML
        boot = html.split("async function init()")[1].split("init().catch")[0]
        self.assertIn("renderAdvancedPanels();", boot)
        self.assertLess(
            boot.index("renderRunSetup();"), boot.index("renderAdvancedPanels();")
        )
        self.assertLess(
            boot.index("renderAdvancedPanels();"), boot.index("renderAdvancedKnobs();")
        )

    def test_run_setup_single_default_model_card(self) -> None:
        html = ui_module.HTML
        run_setup = html.split("function renderRunSetup")[1].split("const SAMPLING_FIELDS")[0]
        # Exactly one "Default model" knob; the four per-task model cards are gone.
        self.assertEqual(run_setup.count('knobField("NEWS_MODEL", "Default model"'), 1)
        for env in (
            "NEWS_MODEL_ARTICLE_SUMMARY",
            "NEWS_MODEL_STORY_DRAFTING",
            "NEWS_MODEL_STORY_SCALE_SCREENING",
            "NEWS_MODEL_TITLE_GENERATION",
        ):
            self.assertNotIn(f'knobField("{env}"', run_setup)
        # The readout binds to the top-level runtime.model {name, reference}.
        self.assertIn("defaultRuntime.name || defaultRuntime.reference", run_setup)
        # A failed runtime snapshot renders a visible banner, not a silent "-".
        self.assertIn("const runtimeError = schema.runtime_error || \"\";", run_setup)
        self.assertIn("Configuration error: ${escapeHtml(runtimeError)}", run_setup)
        advanced = html.split("function renderAdvancedPanels")[1].split(
            "function renderAdvancedKnobs"
        )[0]
        self.assertIn("renderPromptProfilePanel();", advanced)

    def test_prompt_override_editors_and_restore_buttons_in_html(self) -> None:
        # The Editorial approach panel must expose editable per-stage editors
        # bound to the override env vars, with per-stage restore buttons; the
        # old read-only readout is gone. Assertions run on the HTML module
        # constant (JS source), so the new JS lives in one obvious block.
        self.assertIn("NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY", ui_module.HTML)
        self.assertIn("NEWS_PROMPT_OVERRIDE_STORY_SCALE_SCREENING", ui_module.HTML)
        self.assertIn("NEWS_PROMPT_OVERRIDE_STORY_DRAFTING", ui_module.HTML)
        self.assertIn("NEWS_PROMPT_OVERRIDE_TITLE_GENERATION", ui_module.HTML)
        self.assertIn("NEWS_PROMPT_OVERRIDE_IMAGE_ART_DIRECTION", ui_module.HTML)
        # All five override env vars are suppressed from the Advanced tab like
        # NEWS_PROMPT_PROFILE itself (dedicated editors are the single surface).
        surfaced_block = ui_module.HTML.split("const SURFACED_ENVS = new Set([", 1)[1].split("]);", 1)[0]
        for env_var in (
            "NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY",
            "NEWS_PROMPT_OVERRIDE_STORY_SCALE_SCREENING",
            "NEWS_PROMPT_OVERRIDE_STORY_DRAFTING",
            "NEWS_PROMPT_OVERRIDE_TITLE_GENERATION",
            "NEWS_PROMPT_OVERRIDE_IMAGE_ART_DIRECTION",
        ):
            self.assertIn(env_var, surfaced_block)
        # Editable textareas carry data-env and are not readonly.
        self.assertIn(
            'textarea data-env="${escapeHtml(PROMPT_OVERRIDE_ENVS[task])}" rows="4"',
            ui_module.HTML,
        )
        self.assertIn('class="prompt-stage-restore"', ui_module.HTML)
        self.assertNotIn('textarea readonly rows="3"', ui_module.HTML)

    def test_prompt_override_editors_drop_stale_defaults_on_profile_switch(self) -> None:
        # Regression for the HIGH finding: switching the prompt profile must
        # NOT freeze the previous profile's text as per-stage overrides.
        # livePromptOverrides() must diff editor values against BOTH the newly
        # selected profile and the last-rendered profile (tracked via
        # lastRenderedPromptProfileId), so stale defaults are dropped and only
        # genuine edits survive. Assertions run on the HTML module constant
        # (JS source), matching the drift-guard style of this file.
        self.assertIn("let lastRenderedPromptProfileId = null;", ui_module.HTML)
        self.assertIn(
            "lastRenderedPromptProfileId = profile ? profile.id : null;",
            ui_module.HTML,
        )
        self.assertIn(
            "if (value === oldText || value === newText) return;",
            ui_module.HTML,
        )
        # The last-rendered profile must be recorded AFTER the diff, since the
        # editors still hold the previous render's text at that point.
        self.assertIn(
            "// The editors still hold the previous render's text at this point, so",
            ui_module.HTML,
        )
        # Empty editors still mean "no override" (matches collectEnv's
        # suppression of empty/unset override env vars).
        self.assertIn("if (!value) return;", ui_module.HTML)
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
                        "story_scale_screening": {"reference": "gemma-2b"},
                        "title_generation": {"reference": "gemma-2b"},
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
                    delivery_profile=DeliveryProfile(
                        mode=DELIVERY_MODE_OWNER,
                        owner_recipient="primary@example.com",
                        sender="news@example.com",
                        smtp_host="smtp.example.com",
                        smtp_port=587,
                        smtp_username="news",
                        smtp_use_ssl=True,
                        smtp_password="secret",  # pragma: allowlist secret
                        unsubscribe_base_url="https://example.com",
                        unsubscribe_host="0.0.0.0",
                        unsubscribe_port=9000,
                        unsubscribe_secret="token",  # pragma: allowlist secret
                    ),
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
                self.assertEqual(snapshot["model"]["story_scale_screening"]["reference"], "gemma-2b")
                self.assertEqual(snapshot["model"]["title_generation"]["reference"], "gemma-2b")
                self.assertEqual(snapshot["delivery"]["mode"], "owner")
                self.assertEqual(snapshot["delivery"]["unsubscribe_secret_set"], True)
                # Raw credential values never appear in the redacted snapshot.
                self.assertNotIn("\"token\"", json.dumps(snapshot["delivery"]))
                self.assertNotIn("smtp_password", snapshot["delivery"])
                self.assertNotIn("unsubscribe_secret", snapshot["delivery"])

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
            self.assertEqual(len(payload["model_catalog"]), 2)
            self.assertEqual(payload["model_catalog"][0]["alias"], "gemma-4-12b-it-4bit")
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

    def test_ui_js_task_config_envs_exist_in_knob_registry(self) -> None:
        # The embedded JS (TASK_CONFIG / KNOB_HINTS) names env vars by string;
        # a typo would silently break the advanced-settings panel for the two
        # new tasks. Pin each new env var to both the JS source and the Python
        # knob registry so the two surfaces cannot drift apart.
        js_source = ui_module.HTML
        knob_envs = {knob["env"] for knob in ui_module.runtime_knob_registry()}
        for env in (
            "NEWS_MODEL_STORY_SCALE_SCREENING",
            "NEWS_MODEL_TITLE_GENERATION",
            "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
            "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
            "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL",
            "NEWS_MODEL_TITLE_GENERATION_BASE_URL",
            "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS",
            "NEWS_TITLE_GENERATION_MAX_TOKENS",
        ):
            self.assertIn(env, js_source)
            self.assertIn(env, knob_envs)

    def test_ui_js_has_active_run_guard_text(self) -> None:
        # The embedded JS rejects a second run client-side and reflects the
        # active-run state in the controls; pin the copy so it cannot silently
        # drift away from the server-side guard.
        self.assertIn("A run is already active", ui_module.HTML)
        self.assertIn("updateRunControls", ui_module.HTML)

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

    def test_preview_rejects_different_task_model_on_managed_base_url(self) -> None:
        # Regression for #113: the preview must raise on the incompatible
        # model/base-URL combination instead of showing a clean command.
        # build_command re-raises the config ValueError, which the /api/preview
        # route serializes via _send_error_json for the browser.
        with patch.dict(os.environ, {"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                r"Managed model server cannot serve multiple different models "
                r"from the same base URL",
            ):
                preview_payload(
                    {
                        "action": "run",
                        "env": {
                            "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                        },
                    }
                )

    def test_preview_rejects_different_task_model_on_localhost_base_url_alias(self) -> None:
        # Regression for #134: an alias spelling of the managed base URL must
        # still raise in the preview instead of showing a clean command.
        with patch.dict(os.environ, {"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                r"Managed model server cannot serve multiple different models "
                r"from the same base URL",
            ):
                preview_payload(
                    {
                        "action": "run",
                        "env": {
                            "NEWS_MODEL_ARTICLE_SUMMARY": GEMMA_4_12B_IT_4BIT_MODEL_ALIAS,
                            "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL": "http://localhost:8080/v1",
                        },
                    }
                )

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
        self.assertIn("[ui] process exited with code 0", success_record.events[-1]["line"])

        failure_record = RunRecord("run-3", ["news", "run"], {})
        with patch.object(ui_module.subprocess, "Popen", side_effect=OSError("boom")):
            manager._run_process(failure_record)
        self.assertEqual(failure_record.status, "failed")
        self.assertEqual(failure_record.returncode, -1)
        self.assertIn("failed to start process", failure_record.events[-1]["line"])

        running_record = RunRecord("run-4", ["news", "run"], {})
        running_record.process = _SuccessProcess()
        manager.runs[running_record.run_id] = running_record
        stopped = manager.stop(running_record.run_id)
        self.assertEqual(stopped["status"], "stopping")

    def test_run_record_normalizes_events_and_suppresses_duplicates(self) -> None:
        record = RunRecord("run-1", ["news", "run"], {})
        meter_a = "[3/9 clustering] [###-----------------] 1000/200000 steps"
        meter_b = "[3/9 clustering] [####----------------] 10000/200000 steps"
        meter_final = "[3/9 clustering] [####################] 200000/200000 steps"
        record.append("\r" + meter_a + "\033[K\n")
        record.append(meter_a + "\n")  # exact duplicate snapshot
        record.append("WARNING: low coverage\n")
        record.append(meter_b + "\n")
        record.append(meter_final + "\n")
        record.append("[7/9 story drafting] [####----------------] 12/47 stories\n")

        self.assertEqual(
            [event["kind"] for event in record.events],
            ["progress", "message", "progress"],
        )
        # The clustering snapshot was replaced in place, never appended twice.
        self.assertEqual(record.events[0]["line"], meter_final)
        self.assertEqual(record.events[0]["stage"], "clustering")
        self.assertEqual(record.events[0]["replace"], True)
        self.assertEqual(record.events[0]["complete"], True)
        self.assertEqual(record.events[1]["line"], "WARNING: low coverage")
        self.assertEqual(record.events[2]["line"], "[7/9 story drafting] [####----------------] 12/47 stories")
        self.assertEqual(record.snapshot()["line_count"], 3)
        self.assertEqual(record.events[0]["line"], meter_final)

    def test_sse_delivers_progress_replacement_after_cursor_advances(self) -> None:
        record = RunRecord("run-sse", ["news", "run"], {})
        meter_a = "[3/9 clustering] [###-----------------] 1000/200000 steps"
        meter_b = "[3/9 clustering] [####----------------] 10000/200000 steps"
        record.append(meter_a + "\n")
        with record.lock:
            record.status = "running"

        class _Writer:
            def __init__(self) -> None:
                self.parts: list[str] = []
                self.replaced = False

            def write(self, data: bytes) -> int:
                text = data.decode("utf-8")
                self.parts.append(text)
                if meter_a in text and not self.replaced:
                    self.replaced = True
                    record.append(meter_b + "\n")
                    with record.lock:
                        record.status = "completed"
                return len(data)

            def flush(self) -> None:
                return None

        handler = object.__new__(ui_module.NewsUIHandler)
        handler.path = "/api/runs/run-sse/events"
        handler.headers = {}
        handler.rfile = BytesIO(b"")
        writer = _Writer()
        handler.wfile = writer  # type: ignore[assignment]
        handler.send_response = lambda *_args, **_kwargs: None
        handler.send_header = lambda *_args, **_kwargs: None
        handler.end_headers = lambda: None
        with patch.object(ui_module.RUN_MANAGER, "get", return_value=record), patch.object(
            ui_module.time, "sleep", return_value=None
        ):
            handler._stream_run_events(record.run_id)

        self.assertTrue(any(meter_a in part for part in writer.parts))
        self.assertTrue(any(meter_b in part for part in writer.parts))
        self.assertTrue(any('"replace": true' in part for part in writer.parts))

    def test_stop_requested_process_resolves_to_stopped(self) -> None:
        class _StoppableProcess:
            def __init__(self) -> None:
                self.stdout = iter([])
                self.terminated = False

            def wait(self) -> int:
                return -15

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

        manager = RunManager()
        process = _StoppableProcess()
        record = RunRecord("run-5", ["news", "run"], {})
        record.process = process
        manager.runs[record.run_id] = record
        stopped = manager.stop(record.run_id)
        self.assertEqual(stopped["status"], "stopping")
        self.assertTrue(process.terminated)
        with patch.object(ui_module.subprocess, "Popen", return_value=process):
            manager._run_process(record)
        self.assertEqual(record.status, "stopped")
        self.assertEqual(record.returncode, -15)
        self.assertIn("[ui] terminate requested", record.events[0]["line"])
        self.assertIn("[ui] process exited with code -15", record.events[-1]["line"])

    def test_stop_after_worker_has_marked_exit_does_not_relabel_finished_run(self) -> None:
        class _FinishedProcess:
            def poll(self) -> int:
                return 0

        manager = RunManager()
        record = RunRecord("run-6", ["news", "run"], {})
        record.process = _FinishedProcess()
        with record.lock:
            record.status = "completed"
        manager.runs[record.run_id] = record
        snapshot = manager.stop(record.run_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertFalse(record.stop_requested)

    def test_stop_before_process_start_is_honored(self) -> None:
        class _StoppableProcess:
            def __init__(self) -> None:
                self.stdout = iter([])
                self.terminated = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self) -> int:
                return -15

        manager = RunManager()
        record = RunRecord("run-before-start", ["news", "run"], {})
        manager.runs[record.run_id] = record
        self.assertEqual(manager.stop(record.run_id)["status"], "stopping")
        process = _StoppableProcess()
        with patch.object(ui_module.subprocess, "Popen", return_value=process):
            manager._run_process(record)
        self.assertTrue(process.terminated)
        self.assertEqual(record.status, "stopped")
        self.assertEqual(record.returncode, -15)

    def test_stop_racing_process_exit_resolves_to_stopped(self) -> None:
        wait_started = threading.Event()
        release_wait = threading.Event()

        class _RacingProcess:
            def __init__(self) -> None:
                self.stdout = iter([])
                self.terminated = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self) -> int:
                wait_started.set()
                release_wait.wait(timeout=2)
                return -15

        manager = RunManager()
        record = RunRecord("run-race", ["news", "run"], {})
        manager.runs[record.run_id] = record
        process = _RacingProcess()
        with patch.object(ui_module.subprocess, "Popen", return_value=process):
            worker = threading.Thread(target=manager._run_process, args=(record,))
            worker.start()
            self.assertTrue(wait_started.wait(timeout=2))
            self.assertEqual(manager.stop(record.run_id)["status"], "stopping")
            release_wait.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(process.terminated)
        self.assertEqual(record.status, "stopped")
        self.assertEqual(record.returncode, -15)

    def test_unexpected_nonzero_exit_stays_failed(self) -> None:
        class _FailingProcess:
            def __init__(self) -> None:
                self.stdout = iter([])

            def wait(self) -> int:
                return 7

            def poll(self) -> int | None:
                return None

        manager = RunManager()
        record = RunRecord("run-7", ["news", "run"], {})
        with patch.object(ui_module.subprocess, "Popen", return_value=_FailingProcess()):
            manager._run_process(record)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.returncode, 7)

    def test_stream_failure_is_not_reported_as_spawn_failure_and_child_is_reaped(self) -> None:
        class _BrokenOutput:
            def __iter__(self):
                raise RuntimeError("reader boom")
                yield "unreachable"

        class _LeakingProcess:
            def __init__(self) -> None:
                self.stdout = _BrokenOutput()
                self.terminated = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self) -> int:
                return -15

        manager = RunManager()
        record = RunRecord("run-stream-error", ["news", "run"], {})
        process = _LeakingProcess()
        with patch.object(ui_module.subprocess, "Popen", return_value=process):
            manager._run_process(record)
        self.assertTrue(process.terminated)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.returncode, -15)
        lines = [event["line"] for event in record.events]
        self.assertTrue(any("process output failed: reader boom" in line for line in lines))
        self.assertFalse(any("failed to start process" in line for line in lines))

    def test_run_manager_rejects_overlapping_start(self) -> None:
        class _FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False

            def start(self) -> None:
                self.started = True

        manager = RunManager()
        with patch.object(ui_module, "build_command", return_value=(["news", "run"], {})), patch.object(
            ui_module, "uuid"
        ) as uuid_module, patch.object(ui_module.threading, "Thread", _FakeThread):
            uuid_module.uuid4.return_value.hex = "a" * 16
            first = manager.start({"action": "run"})
            self.assertIs(manager.active(), first)
            with self.assertRaisesRegex(ui_module.RunAlreadyActiveError, "already active"):
                manager.start({"action": "run"})
            self.assertEqual(len(manager.runs), 1)

            # A completed run must not block the next start.
            first.status = "completed"
            uuid_module.uuid4.return_value.hex = "b" * 16
            second = manager.start({"action": "run"})
            self.assertEqual(second.run_id, "bbbbbbbbbbbb")
            self.assertIs(manager.active(), second)

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
                events=[{"line": "run line\n", "kind": "message"}],
                status="completed",
                snapshot=lambda: {"run_id": "run-1", "status": "completed"},
            )
            with patch.object(ui_module.RUN_MANAGER, "get", return_value=None):
                self.assertEqual(invoke("do_GET", "/api/runs/run-1/events")[0], 404)
            with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_record):
                status, headers, body = invoke("do_GET", "/api/runs/run-1/events")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/event-stream")
            self.assertIn('data: {"line": "run line\\n", "kind": "message", "status": "completed"}', body)
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

    def test_http_run_rejects_second_start_with_conflict(self) -> None:
        class _FakeThread:
            def __init__(self, target, args, daemon):
                self.started = False

            def start(self) -> None:
                self.started = True

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

        with patch.object(ui_module, "build_command", return_value=(["uv", "run", "news", "run"], {})), patch.object(
            ui_module, "uuid"
        ) as uuid_module, patch.object(ui_module.threading, "Thread", _FakeThread):
            uuid_module.uuid4.return_value.hex = "c" * 16
            status, _, body = invoke("do_POST", "/api/run", body=json.dumps({"action": "run"}))
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(body)["run_id"], "cccccccccccc")
            status, _, body = invoke("do_POST", "/api/run", body=json.dumps({"action": "run"}))
            self.assertEqual(status, 409)
            self.assertIn("already active", json.loads(body)["error"])

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
            events=[{"line": "run line\n", "kind": "message"}],
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
            events=[],
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
            events=[],
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

        fake_stopped_record = SimpleNamespace(
            lock=threading.Lock(),
            events=[{"line": "line one", "kind": "message"}],
            status="stopped",
            snapshot=lambda: {"run_id": "run-1", "status": "stopped"},
        )
        with patch.object(ui_module.RUN_MANAGER, "get", return_value=fake_stopped_record):
            writer = _Writer()
            handler = make_handler(writer)
            handler._stream_run_events("run-1")
        self.assertTrue(any(part.startswith("event: status") for part in writer.parts))
        self.assertTrue(any("\"status\": \"stopped\"" in part for part in writer.parts))
        self.assertTrue(any('data: {"line": "line one", "kind": "message", "status": "stopped"}' in part for part in writer.parts))

    def test_review_and_history_routes(self) -> None:
        with patch.object(
            ui_module,
            "recent_history_payload",
            return_value={"runs": [{"run_id": "r1"}], "error": None},
        ), patch.object(
            ui_module,
            "latest_review_payload",
            return_value={"report_status": "available", "report_text": "body"},
        ), patch.object(
            ui_module,
            "run_detail_payload",
            return_value={"run_id": "r1", "run_status": "completed"},
        ), patch.object(
            ui_module,
            "read_historical_report",
            return_value="# Report\n\nbody",
        ):
            status, _, body = self._invoke_get("/api/history")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["runs"], [{"run_id": "r1"}])

            status, _, body = self._invoke_get("/api/history?limit=5")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["runs"], [{"run_id": "r1"}])

            status, _, body = self._invoke_get("/api/history/r1")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["run_status"], "completed")

            status, _, body = self._invoke_get("/api/review/latest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["report_status"], "available")

            status, headers, body = self._invoke_get("/api/history/r1/report")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
            self.assertIn("# Report", body)

        with patch.object(ui_module, "run_detail_payload", return_value=None), patch.object(
            ui_module, "read_historical_report", return_value=None
        ):
            self.assertEqual(self._invoke_get("/api/history/missing")[0], 404)
            status, _, body = self._invoke_get("/api/history/missing/report")
            self.assertEqual(status, 404)
            self.assertIn("Report not available", json.loads(body)["error"])

        # A broken history store degrades to an error field, not a 500.
        with patch.object(
            ui_module,
            "recent_history_payload",
            return_value={"runs": [], "error": "duckdb down"},
        ):
            status, _, body = self._invoke_get("/api/history")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["error"], "duckdb down")

    def test_latest_review_payload_reads_rolling_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            (output_dir / "latest_run.md").write_text(
                "Report with <script>alert(1)</script>",
                encoding="utf-8",
            )
            (output_dir / "latest_run_details.json").write_text(
                json.dumps(
                    {
                        "run_started_at": "2026-06-01T10:00:00",
                        "settings": {"preset_id": "daily"},
                        "report_generated": True,
                        "delivery": {
                            "status": "failed",
                            "recipients": ["reader@example.com", "editor@example.com"],
                            "reason": "delivery refused for: editor@example.com",
                            "error_type": "SMTPRecipientsRefused",
                            "error_message": "refused recipient",
                            "phase": "send",
                            "accepted_recipients": ["reader@example.com"],
                            "rejected_recipients": ["editor@example.com"],
                        },
                        "events": [
                            {"at": "2026-06-01T10:00:30", "label": "completed"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()

        self.assertEqual(payload["run_id"], "2026-06-01_10-00-00")
        self.assertEqual(payload["run_status"], "completed")
        self.assertEqual(payload["report_status"], "available")
        self.assertEqual(payload["delivery_status"], "failed")
        self.assertEqual(payload["delivery"]["phase"], "send")
        self.assertEqual(
            payload["delivery"]["accepted_recipients"], ["reader@example.com"]
        )
        self.assertEqual(
            payload["delivery"]["rejected_recipients"], ["editor@example.com"]
        )
        self.assertEqual(payload["preset_id"], "daily")
        self.assertEqual(payload["duration_label"], "30s")
        self.assertIn("<script>", payload["report_text"])
        self.assertIsNone(payload["error"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            (output_dir / "latest_run.md").write_text(
                "Review survives <script>alert(1)</script>", encoding="utf-8"
            )
            (output_dir / "latest_run_details.json").write_text(
                "{broken", encoding="utf-8"
            )
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()
            self.assertIn("latest_run_details", payload["metadata_read_errors"])
            self.assertIn(
                "invalid JSON", payload["metadata_read_errors"]["latest_run_details"]
            )
            self.assertNotIn(
                "{broken", payload["metadata_read_errors"]["latest_run_details"]
            )
            # The readable rolling review is preserved despite the broken
            # details document; status stays conservative, never inferred
            # from report prose.
            self.assertEqual(
                payload["report_text"], "Review survives <script>alert(1)</script>"
            )
            self.assertEqual(payload["report_status"], "not_generated")
            self.assertEqual(payload["run_status"], "unknown")
            self.assertIsNone(payload["error"])

        for raw_details in (b"\xff", b"[" * 10000 + b"]" * 10000):
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                output_dir = root / "daily_outputs"
                output_dir.mkdir()
                (output_dir / "latest_run.md").write_text(
                    "Readable report", encoding="utf-8"
                )
                details_path = output_dir / "latest_run_details.json"
                details_path.write_bytes(raw_details)
                paths = {
                    "output_dir": output_dir,
                    "history_db": root / "history.duckdb",
                    "latest_run_markdown": output_dir / "latest_run.md",
                    "latest_run_details": details_path,
                }
                with patch.object(ui_module, "_review_paths", return_value=paths):
                    payload = ui_module.latest_review_payload()
                self.assertIn("latest_run_details", payload["metadata_read_errors"])
                self.assertIsNone(payload["error"])
                self.assertEqual(payload["report_text"], "Readable report")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            (output_dir / "latest_run.md").write_text(
                "Valid report body", encoding="utf-8"
            )
            (output_dir / "latest_run_details.json").write_text(
                json.dumps(
                    {
                        "run_started_at": "2026-06-01T10:00:00",
                        "settings": ["not", "a", "dict"],
                        "events": {"not": "a list"},
                        "delivery": "delivered",
                        "report_generated": "yes",
                        "reports": "none",
                    }
                ),
                encoding="utf-8",
            )
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()
            self.assertEqual(
                set(payload["metadata_read_errors"]),
                {"settings", "events", "delivery", "report_generated", "reports"},
            )
            self.assertEqual(payload["metadata_read_errors"]["settings"], "expected a JSON object")
            self.assertEqual(payload["metadata_read_errors"]["events"], "expected a JSON list")
            self.assertEqual(payload["run_status"], "unknown")
            self.assertEqual(payload["delivery_status"], "not recorded")
            self.assertEqual(payload["delivery"], {})
            self.assertEqual(payload["report_status"], "not_generated")
            self.assertEqual(payload["preset_id"], "custom")
            self.assertEqual(payload["report_text"], "Valid report body")
            self.assertIsNone(payload["error"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            (output_dir / "latest_run.md").write_text(
                "Valid report body", encoding="utf-8"
            )
            (output_dir / "latest_run_details.json").write_text(
                json.dumps(
                    {
                        "run_started_at": "2026-06-01T10:00:00",
                        "settings": "corrupted",
                        "events": [
                            {"at": "2026-06-01T10:00:30", "label": "completed"}
                        ],
                        "report_generated": True,
                        "delivery": {"status": "failed", "phase": "send"},
                    }
                ),
                encoding="utf-8",
            )
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()
            # One corrupt sibling field does not hide the valid events,
            # delivery, report, or review text.
            self.assertEqual(set(payload["metadata_read_errors"]), {"settings"})
            self.assertEqual(payload["run_status"], "completed")
            self.assertEqual(payload["report_status"], "available")
            self.assertEqual(payload["delivery_status"], "failed")
            self.assertEqual(payload["delivery"]["phase"], "send")
            self.assertEqual(payload["report_text"], "Valid report body")
            self.assertEqual(payload["run_id"], "2026-06-01_10-00-00")
            self.assertIsNone(payload["error"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            (output_dir / "latest_run.md").write_text(
                "Readable report", encoding="utf-8"
            )
            (output_dir / "latest_run_details.json").write_text(
                json.dumps(
                    {
                        "events": [
                            {"at": "2026-06-01T10:00:30", "label": "completed"}
                        ],
                        "report_generated": True,
                        "delivery": {
                            "status": "failed",
                            "accepted_recipients": "reader@example.com",
                            "rejected_recipients": {"recipient": "editor@example.com"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()
            self.assertEqual(
                set(payload["metadata_read_errors"]),
                {"delivery.accepted_recipients", "delivery.rejected_recipients"},
            )
            self.assertEqual(payload["delivery"]["accepted_recipients"], [])
            self.assertEqual(payload["delivery"]["rejected_recipients"], [])
            self.assertEqual(payload["delivery_status"], "failed")
            self.assertEqual(payload["report_status"], "available")
            self.assertIsNone(payload["error"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "daily_outputs"
            output_dir.mkdir()
            paths = {
                "output_dir": output_dir,
                "history_db": root / "history.duckdb",
                "latest_run_markdown": output_dir / "latest_run.md",
                "latest_run_details": output_dir / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                payload = ui_module.latest_review_payload()
            self.assertEqual(payload["report_status"], "not_generated")
            self.assertEqual(payload["run_status"], "unknown")
            self.assertEqual(payload["delivery_status"], "not recorded")
            self.assertEqual(payload["metadata_read_errors"], {})
            self.assertIsNone(payload["error"])

    def test_read_historical_report_validates_okf_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "history" / "news_history.duckdb"
            diagnostics = RunDiagnostics(
                run_started_at="2026-06-01T10:00:00",
                settings={"preset_id": "daily"},
                events=[{"at": "2026-06-01T10:00:30", "label": "completed"}],
            )
            diagnostics.record_report(path="output/daily_outputs/latest_run.md")
            write_run_history(
                db_path,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                export_csv=False,
            )
            bundle = db_path.parent / "okf" / "2026-06-01_10-00-00"
            bundle.mkdir(parents=True)
            (bundle / "report.md").write_text("Report with <script>", encoding="utf-8")
            paths = {
                "output_dir": root / "daily_outputs",
                "history_db": db_path,
                "latest_run_markdown": root / "daily_outputs" / "latest_run.md",
                "latest_run_details": root / "daily_outputs" / "latest_run_details.json",
            }
            with patch.object(ui_module, "_review_paths", return_value=paths):
                self.assertEqual(
                    ui_module.read_historical_report("2026-06-01_10-00-00"),
                    "Report with <script>",
                )
                self.assertIsNone(ui_module.read_historical_report("missing-run"))
                self.assertIsNone(
                    ui_module.read_historical_report("2026-06-01_10-00-00/../..")
                )

    def test_run_log_renderer_contracts(self) -> None:
        html = ui_module.HTML
        # The log pane is an accessible live region rendered via textContent.
        self.assertIn('<pre id="logPane" role="log" aria-live="polite"', html)
        # Stage-keyed reducer replaces the active progress row in place.
        self.assertIn("logState.progressByStage", html)
        self.assertIn("function appendLogEvent(payload)", html)
        self.assertIn("function resetLog()", html)
        self.assertIn("function stopSpinner()", html)
        self.assertIn("function finalizeRun(payload, events)", html)
        # Restrained spinner sequence and explicit terminal glyphs/statuses.
        self.assertIn("SPINNER_GLYPHS", html)
        self.assertIn('completed: "✓"', html)
        self.assertIn('failed: "✗"', html)
        self.assertIn('stopped: "■"', html)
        self.assertIn('["completed", "failed", "stopped"]', html)
        # The spinner decorates, never replaces, the numeric progress text.
        self.assertIn("SPINNER_GLYPHS[logState.spinnerIndex % SPINNER_GLYPHS.length]} ${text}", html)
        # All three terminal statuses stop the spinner, close the stream,
        # clear active controls, and refresh durable review data.
        self.assertIn("events.close();", html)
        self.assertIn("refreshReviewData();", html)
        self.assertIn("stopSpinner();", html)
        self.assertIn("TERMINAL_STATUSES.includes(payload.status)", html)
        self.assertIn("events.onerror", html)
        self.assertIn("pollRunStatus", html)
        self.assertIn("Live run stream disconnected", html)
        self.assertIn("Live run stream sent invalid data", html)
        self.assertIn("Stop failed:", html)
        # No append-only raw rendering path remains.
        self.assertNotIn("textContent += payload.line", html)
        self.assertNotIn("textContent += \`\\n[ui] ", html)
        # Run start clears and reinitializes the reducer state.
        run_action = html.split("async function runAction")[1].split("events.addEventListener")[0]
        self.assertIn("resetLog();", run_action)
        self.assertIn("startSpinner();", run_action)
        self.assertIn("new EventSource", run_action)

    @unittest.skipUnless(shutil.which("node"), "node runtime required for browser renderer behavior tests")
    def test_run_log_renderer_behavior(self) -> None:
        html = ui_module.HTML
        start = html.index("    const SPINNER_GLYPHS")
        end = html.index("    function badgeClass", start)
        renderer_source = html[start:end]
        script = f"""
import assert from "node:assert/strict";
const logPane = {{ textContent: "", scrollTop: 0, scrollHeight: 0 }};
const elements = new Map([["logPane", logPane]]);
function $(id) {{
  return elements.get(id) || {{ disabled: false, title: "" }};
}}
const state = {{ activeRun: null }};
const statuses = [];
let controlUpdates = 0;
let reviewRefreshes = 0;
function setStatus(text, cls) {{ statuses.push({{ text, cls }}); }}
function updateRunControls() {{ controlUpdates += 1; }}
function refreshReviewData() {{ reviewRefreshes += 1; }}
function requestBody() {{ return {{}}; }}
const apiCalls = [];
async function api(path) {{
  apiCalls.push(path);
  if (path === "/api/run") return {{ run_id: "run-1" }};
  return {{ run_id: "run-1", status: "completed", returncode: 0 }};
}}
globalThis.window = {{ setTimeout: () => 0 }};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {{}};
class FakeEventSource {{
  static instances = [];
  constructor(url) {{
    this.url = url;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }}
  close() {{ this.closed = true; }}
  addEventListener() {{}}
}}
globalThis.EventSource = FakeEventSource;

{renderer_source}

resetLog();
const firstMeter = "[3/9 clustering] [###-----------------] 1000/200000 steps";
const finalMeter = "[3/9 clustering] [####################] 200000/200000 steps";
appendLogEvent({{ kind: "progress", stage: "clustering", line: firstMeter, complete: false }});
appendLogEvent({{ kind: "message", line: "WARNING: low coverage" }});
appendLogEvent({{ kind: "progress", stage: "clustering", line: finalMeter, complete: true }});
assert.equal(logState.rows.length, 2);
assert.deepEqual(logState.rows.map(row => row.text), [finalMeter, "WARNING: low coverage"]);
assert.equal(logState.rows[0].live, false);
assert.equal(logState.rows[0].complete, true);
assert.equal(logPane.textContent, `✓ ${{finalMeter}}\\nWARNING: low coverage`);
appendLogEvent({{ kind: "progress", stage: "clustering", line: finalMeter, complete: true }});
assert.equal(logState.rows.length, 2);
assert.equal(logPane.textContent, `✓ ${{finalMeter}}\\nWARNING: low coverage`);

for (const [status, glyph] of [["completed", "✓"], ["failed", "✗"], ["stopped", "■"]]) {{
  resetLog();
  state.activeRun = `run-${{status}}`;
  startSpinner();
  appendLogEvent({{ kind: "progress", stage: "clustering", line: firstMeter, complete: false }});
  const events = new FakeEventSource("manual");
  assert.equal(finalizeRun({{ run_id: `run-${{status}}`, status }}, events), true);
  assert.equal(events.closed, true);
  assert.equal(state.activeRun, null);
  assert.equal(logState.spinnerTimer, null);
  assert.ok(logPane.textContent.includes(`${{glyph}} [ui] ${{status}}`));
}}
const pendingEvents = new FakeEventSource("pending");
state.activeRun = "run-pending";
assert.equal(finalizeRun({{ run_id: "run-pending", status: "running" }}, pendingEvents), false);
assert.equal(pendingEvents.closed, false);
assert.equal(state.activeRun, "run-pending");

state.activeRun = null;
await runAction();
assert.equal(state.activeRun, "run-1");
const liveEvents = FakeEventSource.instances[FakeEventSource.instances.length - 1];
liveEvents.onerror();
await new Promise(resolve => setImmediate(resolve));
assert.equal(liveEvents.closed, true);
assert.equal(state.activeRun, null);
assert.ok(statuses.some(item => item.text.startsWith("Live run stream disconnected")));
assert.ok(apiCalls.includes("/api/runs/run-1"));
assert.ok(reviewRefreshes >= 3);
assert.ok(controlUpdates >= 3);
"""
        result = subprocess.run(
            ["node", "--input-type=module"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_report_review_tab_contracts(self) -> None:
        html = ui_module.HTML
        # The tab exists with a dedicated icon and section mounts.
        self.assertIn('["review", "Report Review", "book"]', html)
        self.assertIn('<section id="review" class="view">', html)
        self.assertIn('id="reviewMount"', html)
        self.assertIn('id="historyMount"', html)
        # API endpoint strings used by the review surface.
        self.assertIn('"/api/review/latest"', html)
        self.assertIn('"/api/history"', html)
        self.assertIn('`/api/history/${encodeURIComponent(runId)}`', html)
        self.assertIn('`/api/history/${encodeURIComponent(runId)}/report`', html)
        # Separate run/report/delivery badges render independently.
        self.assertIn('run: ${escapeHtml(runStatus || "unknown")}', html)
        self.assertIn('report: ${escapeHtml(reportStatus || "unavailable")}', html)
        self.assertIn('delivery: ${escapeHtml(deliveryLabel(deliveryStatus))}', html)
        # Report text is rendered through textContent, never innerHTML, and
        # present review text wins over conservative status fallbacks so
        # metadata corruption cannot hide a readable report.
        self.assertIn("if (review.report_text && review.report_text.trim()) {", html)
        self.assertIn('pane.textContent = review.report_text;', html)
        self.assertIn('pane.textContent = "(empty report)";', html)
        self.assertIn("pane.textContent = text;", html)
        self.assertIn("pane.textContent = `Report unavailable: ${err.message}`;", html)
        # Metadata read diagnostics render as an escaped warning list in both
        # the latest review and selected-run surfaces.
        self.assertIn("function metadataWarnings(errors) {", html)
        self.assertIn("Object.entries(errors || {})", html)
        self.assertIn("${escapeHtml(field)}:</strong> ${escapeHtml(message)}", html)
        self.assertIn("metadataWarnings(review.metadata_read_errors)", html)
        self.assertIn("metadataWarnings(run.metadata_read_errors)", html)
        # Rich delivery metadata (phase, accepted/rejected recipients) is
        # rendered in the review and history detail surfaces, escaped through
        # the existing escapeHtml/textContent paths.
        self.assertIn('delivery.phase ? `phase: ${delivery.phase}` : ""', html)
        self.assertIn("Array.isArray(delivery.accepted_recipients)", html)
        self.assertIn("Array.isArray(delivery.rejected_recipients)", html)
        self.assertIn(
            'acceptedRecipients.length ? `accepted: ${acceptedRecipients.join(", ")}` : ""',
            html,
        )
        self.assertIn(
            'rejectedRecipients.length ? `rejected: ${rejectedRecipients.join(", ")}` : ""',
            html,
        )
        # Terminal status closes the stream, then refreshes durable review data.
        self.assertIn("events.close();", html)
        self.assertIn("refreshReviewData();", html)
        # Boot loads the durable review/history surfaces.
        boot = html.split("async function init()")[1].split("init().catch")[0]
        self.assertIn("await loadReview();", boot)
        self.assertIn("await loadHistory();", boot)
        # Stable empty/error states exist for missing history and reports.
        self.assertIn("No runs recorded yet.", html)
        self.assertIn("No completed report is available yet.", html)
        self.assertIn("No report was generated for this run.", html)

    def test_module_entrypoint_guard_executes(self) -> None:
        source = "\n" * 2453 + 'if __name__ == "__main__":\n    raise SystemExit(main())\n'
        namespace = {"__name__": "__main__", "main": lambda: 0}

        with self.assertRaises(SystemExit) as exc:
            exec(compile(source, ui_module.__file__, "exec"), namespace)

        self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
