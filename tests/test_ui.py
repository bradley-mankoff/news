from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
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

import news_pipeline.config as config_module
from news_pipeline import ui as ui_module
from news_pipeline import model_catalog
from news_pipeline import prompt_catalog
from news_pipeline import prompt_templates
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


_FAKE_DOM_ELEMENT_JS = r'''
const decodeEntities = (text) => String(text)
  .replaceAll("&lt;", "<")
  .replaceAll("&gt;", ">")
  .replaceAll("&quot;", '"')
  .replaceAll("&amp;", "&");
class FakeElement {
  constructor(id = "", value = "") {
    this.id = id;
    this.value = value;
    this.dataset = {};
    this._innerHTML = "";
    this.disabled = false;
    this._listeners = {};
    this.onclick = null;
  }
  set innerHTML(value) { this._innerHTML = String(value); }
  get innerHTML() { return this._innerHTML; }
  get textContent() {
    return decodeEntities(this._innerHTML.replace(/<[^>]*>/g, ""));
  }
  optionText(value) {
    const marker = `<option value="${value}">`;
    const start = this._innerHTML.indexOf(marker);
    if (start < 0) return null;
    const contentStart = start + marker.length;
    const end = this._innerHTML.indexOf("</option>", contentStart);
    return end < 0 ? null : decodeEntities(this._innerHTML.slice(contentStart, end));
  }
  querySelector(selector) {
    const prefix = 'button[data-use-hf-model="';
    if (!selector.startsWith(prefix) || !selector.endsWith('"]')) return null;
    const modelId = selector.slice(prefix.length, -2);
    const marker = `data-use-hf-model="${modelId}"`;
    const markerStart = this._innerHTML.indexOf(marker);
    if (markerStart < 0) return null;
    const buttonStart = this._innerHTML.lastIndexOf("<button", markerStart);
    const buttonEnd = this._innerHTML.indexOf(">", markerStart);
    const button = this._innerHTML.slice(buttonStart, buttonEnd + 1);
    const backendMatch = button.match(/data-use-hf-backend="([^"]*)"/);
    return {
      disabled: button.includes(" disabled"),
      dataset: { useHfBackend: backendMatch ? backendMatch[1] : "" }
    };
  }
  querySelectorAll(_selector) { return []; }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  fire(type) {
    for (const fn of this._listeners[type] || []) fn.call(this, { target: this });
  }
}
'''

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
        self.assertEqual(advanced.count('modelTuningPanel("story_scale_screening")'), 1)
        self.assertEqual(advanced.count('modelTuningPanel("title_generation")'), 1)
        self.assertEqual(advanced.count('modelTuningPanel("image_art_direction")'), 1)
        tuning_editor = html.split('<h2>Model Tuning Preset Editor</h2>', 1)[1].split(
            '<h2>', 1
        )[0]
        self.assertIn(
            '<option value="image_art_direction">image_art_direction</option>',
            tuning_editor,
        )
        self.assertIn("image_art_direction_max_tokens", tuning_editor)
        # Dedicated envs are suppressed from the raw override list via the
        # registry-derived set (issue #115); the literal manifest is gone.
        derived = html.split("const SURFACED_ENVS = new Set();", 1)[1].split("const TASK_CONFIG", 1)[0]
        self.assertNotIn('"NEWS_', derived)
        self.assertIn("function syncSurfacedEnvs()", derived)
        self.assertIn("knob.ui_location", derived)

    def test_surfaced_envs_derived_from_registry_ui_location(self) -> None:
        """SURFACED_ENVS is a derived Set populated from the registry's
        ui_location metadata (issue #115): no literal environment-name
        manifest survives in the browser source, and missing/unknown client
        metadata must fail safe by leaving the knob in the raw list."""
        html = ui_module.HTML
        derived = html.split("const SURFACED_ENVS = new Set();", 1)[1].split("const TASK_CONFIG", 1)[0]
        # No literal env names inside the derived-set block.
        self.assertNotIn('"NEWS_', derived)
        self.assertNotIn('"', derived.split("function syncSurfacedEnvs")[0])
        # The sync function reads schema knob ui_location metadata and only
        # the two dedicated locations suppress the raw override.
        self.assertIn("function syncSurfacedEnvs()", derived)
        self.assertIn("((state.schema && state.schema.knobs) || [])", derived)
        self.assertIn("const location = knob.ui_location;", derived)
        self.assertIn('location === "run_setup" || location === "advanced_panels"', derived)
        self.assertIn("SURFACED_ENVS.add(knob.env)", derived)
        # The raw renderer keeps filtering through the same derived Set.
        raw_filter = html.split("function renderAdvancedKnobs")[1].split("function collectModelTuningPresetBody")[0]
        self.assertIn("if (SURFACED_ENVS.has(knob.env)) return;", raw_filter)

    def test_surfaced_envs_parity_with_dedicated_renderers(self) -> None:
        """Bidirectional registry/UI parity (issue #115): every surfaced
        registry record maps to an actual dedicated UI reference, and every
        dedicated UI reference maps back to one registered surfaced
        environment. A stale metadata entry cannot hide an unrendered knob,
        and a stale markup reference cannot point at a raw-only setting."""
        html = ui_module.HTML
        run_setup = html.split("function renderRunSetup")[1].split("function renderAdvancedPanels")[0]
        advanced = html.split("function renderAdvancedPanels")[1].split("function renderAdvancedKnobs")[0]
        task_config = html.split("const TASK_CONFIG")[1].split("const TASK_MAX_TOKENS_LABELS")[0]
        # Direct Run Setup refs: data-env controls plus knobField() calls.
        direct_run_setup = (
            set(re.findall(r'data-env="(NEWS_[A-Z_]+)"', run_setup))
            | set(re.findall(r'knobField\("(NEWS_[A-Z_]+)"', run_setup))
        )
        # Direct advanced panel refs: Budgets/Peripheral knobField() calls.
        direct_panels = set(re.findall(r'knobField\("(NEWS_[A-Z_]+)"', advanced))
        # TASK_CONFIG-driven refs: preset/base-URL/max-token envs and the
        # sampling prefix composed with the SAMPLING_FIELDS suffixes.
        task_direct = set(
            re.findall(r'(?:presetEnv|baseUrlEnv|taskMaxTokensEnv): "(NEWS_[A-Z_]+)"', task_config)
        )
        prefixes = re.findall(r'taskSamplingPrefix: "(NEWS_MODEL_[A-Z_]+)"', task_config)
        suffixes = re.findall(
            r'\["([A-Z_]+)", "',
            html.split("const SAMPLING_FIELDS")[1].split("function samplingFields")[0],
        )
        task_sampling = {f"{p}_{s}" for p in prefixes for s in suffixes}
        self.assertEqual(len(task_sampling), 30)
        # Sentence-level override map and the schema-driven template catalog.
        override_map = {
            env.strip('"')
            for env in re.findall(
                r'"NEWS_PROMPT_OVERRIDE_[A-Z_]+"',
                html.split("const PROMPT_OVERRIDE_ENVS")[1].split("function selectedPromptProfile")[0],
            )
        }
        self.assertEqual(len(override_map), 5)
        self.assertIn("function promptTemplateEnvMap()", html)
        self.assertIn("map[t.task] = t.env_var;", html)
        template_catalog = set(config_module.PROMPT_TEMPLATE_ENV_VARS.values())
        dedicated_refs = (
            direct_run_setup
            | direct_panels
            | task_direct
            | task_sampling
            | override_map
            | template_catalog
        )
        registry = {knob["env"]: knob["ui_location"] for knob in build_knob_registry()}
        surfaced = {env for env, loc in registry.items() if loc != "advanced_raw"}
        # Every dedicated UI reference maps back to a registered surfaced env.
        self.assertEqual(
            sorted(dedicated_refs - surfaced),
            [],
            "dedicated markup references an env that is not registry-surfaced",
        )
        # Every surfaced registry env has an actual dedicated reference.
        self.assertEqual(
            sorted(surfaced - dedicated_refs),
            [],
            "registry-surfaced env has no dedicated UI reference",
        )
        self.assertEqual(len(surfaced), 74)
        # The five per-task model selectors stay raw Advanced overrides even
        # though TASK_CONFIG references them (modelEnv is not a dedicated
        # control; no data-env input is generated for it).
        for env in (
            "NEWS_MODEL_ARTICLE_SUMMARY",
            "NEWS_MODEL_STORY_DRAFTING",
            "NEWS_MODEL_STORY_SCALE_SCREENING",
            "NEWS_MODEL_TITLE_GENERATION",
            "NEWS_MODEL_IMAGE_ART_DIRECTION",
        ):
            self.assertEqual(registry[env], "advanced_raw", f"{env} must stay raw")
            self.assertIn(f'modelEnv: "{env}"', task_config)

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
        # The derived suppression set synchronizes after the async schema load
        # and before any raw rendering (issue #115).
        self.assertLess(boot.index("state.schema = await api(\"/api/schema\");"), boot.index("syncSurfacedEnvs();"))
        self.assertLess(boot.index("syncSurfacedEnvs();"), boot.index("renderAdvancedKnobs();"))

    def test_model_tuning_panel_metadata_guard_executes_in_node_harness(self) -> None:
        """Execute the production modelTuningPanel() renderer in a Node harness.

        The panel must tolerate missing task metadata (issue #118) without
        aborting the Advanced Settings render, while preserving safe rendering
        with an initially empty schema and producing the full panel once
        populated metadata arrives. TASK_CONFIG, state, the knob/sampling
        helpers, and modelTuningPanel() are extracted from ui_module.HTML
        itself, not reimplemented here.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + r"""
// inputForKnob reads current values through document.querySelector; the
// harness has no DOM controls, so every lookup falls back to
// state.schema.current_env, mirroring a schema-driven initial render.
const document = { querySelector() { return null; } };
const advancedPanels = { innerHTML: "" };
const $ = (id) => id === "advancedPanels" ? advancedPanels : null;
const decorateEnvHints = () => {};
const renderPromptProfilePanel = () => {};
const renderPromptTemplateEditors = () => {};
"""
            + js_function_block("    const TASK_CONFIG = {", "    const state = {")
            + js_function_block("    const state = {", "    const icons = {")
            + js_function_block(
                "    const TASK_MAX_TOKENS_LABELS = {", "    const SAMPLING_FIELDS = ["
            )
            + js_function_block(
                "    const SAMPLING_FIELDS = [", "    function samplingFields(prefix) {"
            )
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block(
                'function formatDefault(value, fallback="none") {',
                "function currentControlValue",
            )
            + js_function_block(
                "function currentControlValue(env) {", "function setControlValue"
            )
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block(
                'function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {',
                "function knobField",
            )
            + js_function_block(
                "function knobField(env, label, options={}) {", "function knobHint"
            )
            + js_function_block(
                "function samplingFields(prefix) {", "function modelTuningPanel"
            )
            + js_function_block(
                "function modelTuningPanel(task) {", "function renderAdvancedPanels"
            )
            + js_function_block(
                "function renderAdvancedPanels() {", "function renderAdvancedKnobs"
            )
            + r"""
// ---- 1. Missing task metadata ---------------------------------------------
// An unknown task must render no markup instead of throwing, so one
// unrecognized panel cannot abort the whole Advanced Settings render.
assert(state.schema === null, "harness state must start with an empty schema");
assert(modelTuningPanel("missing_task") === "", "unknown task must render the empty string");
assert(modelTuningPanel("definitely_not_a_task") === "", "unknown task must render the empty string");

// ---- 2. Empty initial schema metadata -------------------------------------
// A known task with no schema yet must still render the panel shell with the
// existing Resolved fallback and without any schema-backed knob controls.
const emptyPanel = modelTuningPanel("article_summary");
assert(typeof emptyPanel === "string" && emptyPanel.length > 0, "known task must render panel markup with an empty schema");
assert(emptyPanel.includes("<h2>Article Summarization</h2>"), "panel label missing from empty-schema markup");
assert(emptyPanel.includes("Resolved: -"), "empty-schema panel must show the Resolved fallback");
assert(emptyPanel.includes('<select id="article_tuning_preset"'), "preset select id missing from empty-schema markup");
assert(!emptyPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "schema-backed knob rendered without schema metadata");

// ---- 3. Populated metadata arrival ----------------------------------------
// Once runtime metadata and schema knobs arrive, the same panel must render
// the resolved model and the shared cap, max tokens, base URL, and sampling
// controls that were absent in the empty-schema state.
state.schema = {
  runtime: { model: { article_summary: { name: "gpt-4o", reference: "gpt-4o" } } },
  current_env: {
    NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL: "http://localhost:11434/v1",
    NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE: "0.7"
  },
  knobs: [
    { env: "NEWS_MODEL_MAX_INPUT_TOKENS", type: "number", default: 100000 },
    { env: "NEWS_ARTICLE_SUMMARY_MAX_TOKENS", type: "number", default: 4000 },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL", type: "text", default: "http://localhost:11434/v1" },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE", type: "select", default: 0.7, options: ["0.2", "0.7", "1.0"] },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TOP_P", type: "select", default: 0.9, options: ["0.8", "0.9", "1.0"] }
  ]
};
const populatedPanel = modelTuningPanel("article_summary");
assert(populatedPanel.includes("Resolved: gpt-4o"), "resolved model name missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "shared cap knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_ARTICLE_SUMMARY_MAX_TOKENS"'), "task max tokens knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL"'), "base URL knob missing after metadata arrival");
assert(populatedPanel.includes('value="http://localhost:11434/v1"'), "base URL current value missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE"'), "sampling field missing after metadata arrival");
assert(populatedPanel.includes('<option value="0.7" selected>0.7</option>'), "sampling field current value not selected after metadata arrival");

// ---- 4. Known-task parity --------------------------------------------------
// Every configured task must still produce panel markup with an empty schema
// so the guard cannot silently suppress valid panels.
for (const task of Object.keys(TASK_CONFIG)) {
  const panel = modelTuningPanel(task);
  assert(typeof panel === "string" && panel.length > 0, `${task} must render non-empty panel markup`);
  assert(panel.includes(`<h2>${TASK_CONFIG[task].label}</h2>`), `${task} panel label missing`);
}

// ---- 5. Advanced Settings integration with missing metadata ---------------
// Simulate configuration drift at the parent-render boundary. The missing
// panel must interpolate as an empty string while later Advanced Settings
// sections remain present.
delete TASK_CONFIG.article_summary;
let renderError = null;
try {
  renderAdvancedPanels();
} catch (error) {
  renderError = error;
}
assert(!renderError, `Advanced Settings render threw: ${renderError}`);
assert(advancedPanels.innerHTML.includes("<h2>Story Writing</h2>"), "remaining model panel missing after a task is removed");
assert(advancedPanels.innerHTML.includes("<h2>Run budgets and quotas</h2>"), "Budgets panel missing after a task is removed");
assert(!advancedPanels.innerHTML.includes("undefined"), "undefined markup leaked into Advanced Settings");
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_dedicated_renderers_emit_surfaced_controls(self) -> None:
        """Execute every dedicated renderer and assert its controls are emitted.

        This complements the registry parity guard by inspecting rendered HTML:
        registry metadata may identify a control, but it cannot make a missing
        model-tuning field or prompt-template editor appear in the DOM.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const elements = { promptTemplateEditors: { innerHTML: "" } };
const $ = id => elements[id] || null;
const document = {
  querySelector: () => null,
  querySelectorAll: () => []
};
"""
            + js_function_block("    const TASK_CONFIG = {", "    const state = {")
            + js_function_block("    const state = {", "    const icons = {")
            + js_function_block(
                "    const TASK_MAX_TOKENS_LABELS = {", "    const SAMPLING_FIELDS = ["
            )
            + js_function_block(
                "    const SAMPLING_FIELDS = [", "    function samplingFields(prefix) {"
            )
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block(
                'function formatDefault(value, fallback="none") {',
                "function currentControlValue",
            )
            + js_function_block(
                "function currentControlValue(env) {", "function setControlValue"
            )
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block(
                'function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {',
                "function knobField",
            )
            + js_function_block(
                "function knobField(env, label, options={}) {", "function knobHint"
            )
            + js_function_block("function samplingFields(prefix) {", "function modelTuningPanel")
            + js_function_block("function modelTuningPanel(task) {", "function renderAdvancedPanels")
            + js_function_block(
                "    let promptTemplateDirty = {};", "    function promptTemplateEnvMap() {"
            )
            + js_function_block(
                "    function promptTemplateEnvMap() {", "    function promptTemplateRecord(task) {"
            )
            + js_function_block(
                "    function renderPromptTemplateEditors() {", "    function currentPromptTemplateEnv() {"
            )
            + r"""
const knobs = [];
const knob = (env, type = "text") => ({ env, type, default: type === "number" ? 10 : "" });
for (const task of Object.keys(TASK_CONFIG)) {
  const meta = TASK_CONFIG[task];
  knobs.push(knob(meta.baseUrlEnv));
  knobs.push(knob(meta.taskMaxTokensEnv, "number"));
  for (const [suffix] of SAMPLING_FIELDS) knobs.push(knob(`${meta.taskSamplingPrefix}_${suffix}`, "number"));
}
knobs.push(knob("NEWS_MODEL_MAX_INPUT_TOKENS", "number"));
const templateTasks = [
  ["article_summary", "NEWS_PROMPT_TEMPLATE_ARTICLE_SUMMARY", "Article Summarization"],
  ["story_scale_screening", "NEWS_PROMPT_TEMPLATE_STORY_SCALE_SCREENING", "Story Scale Screening"],
  ["story_drafting", "NEWS_PROMPT_TEMPLATE_STORY_DRAFTING", "Story Drafting"],
  ["title_generation", "NEWS_PROMPT_TEMPLATE_TITLE_GENERATION", "Title Generation"],
  ["image_art_direction", "NEWS_PROMPT_TEMPLATE_IMAGE_ART_DIRECTION", "Image Art Direction"]
].map(([task, env_var, label]) => ({
  task, env_var, label, system: `system ${task}`, user: `user ${task}`,
  required_placeholders: [], optional_placeholders: [], placeholder_descriptions: {}
}));
state.schema = { knobs, current_env: {}, runtime: { model: {} }, prompt_templates: templateTasks };

for (const task of Object.keys(TASK_CONFIG)) {
  const meta = TASK_CONFIG[task];
  const panel = modelTuningPanel(task);
  assert(panel.includes(`data-env="${meta.presetEnv}"`), `${task} preset control missing`);
  assert(panel.includes(`data-env="${meta.taskMaxTokensEnv}"`), `${task} max-token control missing`);
  assert(panel.includes(`data-env="${meta.baseUrlEnv}"`), `${task} base URL control missing`);
  for (const [suffix] of SAMPLING_FIELDS) {
    const env = `${meta.taskSamplingPrefix}_${suffix}`;
    assert(panel.includes(`data-env="${env}"`), `${task} sampling control missing: ${env}`);
  }
}
assert(
  modelTuningPanel("article_summary").includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'),
  "shared model input cap control missing"
);

renderPromptTemplateEditors();
for (const { task } of templateTasks) {
  const markup = elements.promptTemplateEditors.innerHTML;
  assert(markup.includes(`data-prompt-template-card="${task}"`), `${task} template card missing`);
  assert(markup.includes(`data-template-system="${task}"`), `${task} system editor missing`);
  assert(markup.includes(`data-template-user="${task}"`), `${task} user editor missing`);
}
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_model_tuning_panel_metadata_guard_executes_in_node_harness(self) -> None:
        """Execute the production modelTuningPanel() renderer in a Node harness.

        The panel must tolerate missing task metadata (issue #118) without
        aborting the Advanced Settings render, while preserving safe rendering
        with an initially empty schema and producing the full panel once
        populated metadata arrives. TASK_CONFIG, state, the knob/sampling
        helpers, and modelTuningPanel() are extracted from ui_module.HTML
        itself, not reimplemented here.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + r"""
// inputForKnob reads current values through document.querySelector; the
// harness has no DOM controls, so every lookup falls back to
// state.schema.current_env, mirroring a schema-driven initial render.
const document = { querySelector() { return null; } };
const advancedPanels = { innerHTML: "" };
const $ = (id) => id === "advancedPanels" ? advancedPanels : null;
const decorateEnvHints = () => {};
const renderPromptProfilePanel = () => {};
const renderPromptTemplateEditors = () => {};
"""
            + js_function_block("    const TASK_CONFIG = {", "    const state = {")
            + js_function_block("    const state = {", "    const icons = {")
            + js_function_block(
                "    const TASK_MAX_TOKENS_LABELS = {", "    const SAMPLING_FIELDS = ["
            )
            + js_function_block(
                "    const SAMPLING_FIELDS = [", "    function samplingFields(prefix) {"
            )
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block(
                'function formatDefault(value, fallback="none") {',
                "function currentControlValue",
            )
            + js_function_block(
                "function currentControlValue(env) {", "function setControlValue"
            )
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block(
                'function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {',
                "function knobField",
            )
            + js_function_block(
                "function knobField(env, label, options={}) {", "function knobHint"
            )
            + js_function_block(
                "function samplingFields(prefix) {", "function modelTuningPanel"
            )
            + js_function_block(
                "function modelTuningPanel(task) {", "function renderAdvancedPanels"
            )
            + js_function_block(
                "function renderAdvancedPanels() {", "function renderAdvancedKnobs"
            )
            + r"""
// ---- 1. Missing task metadata ---------------------------------------------
// An unknown task must render no markup instead of throwing, so one
// unrecognized panel cannot abort the whole Advanced Settings render.
assert(state.schema === null, "harness state must start with an empty schema");
assert(modelTuningPanel("missing_task") === "", "unknown task must render the empty string");
assert(modelTuningPanel("definitely_not_a_task") === "", "unknown task must render the empty string");

// ---- 2. Empty initial schema metadata -------------------------------------
// A known task with no schema yet must still render the panel shell with the
// existing Resolved fallback and without any schema-backed knob controls.
const emptyPanel = modelTuningPanel("article_summary");
assert(typeof emptyPanel === "string" && emptyPanel.length > 0, "known task must render panel markup with an empty schema");
assert(emptyPanel.includes("<h2>Article Summarization</h2>"), "panel label missing from empty-schema markup");
assert(emptyPanel.includes("Resolved: -"), "empty-schema panel must show the Resolved fallback");
assert(emptyPanel.includes('<select id="article_tuning_preset"'), "preset select id missing from empty-schema markup");
assert(!emptyPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "schema-backed knob rendered without schema metadata");

// ---- 3. Populated metadata arrival ----------------------------------------
// Once runtime metadata and schema knobs arrive, the same panel must render
// the resolved model and the shared cap, max tokens, base URL, and sampling
// controls that were absent in the empty-schema state.
state.schema = {
  runtime: { model: { article_summary: { name: "gpt-4o", reference: "gpt-4o" } } },
  current_env: {
    NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL: "http://localhost:11434/v1",
    NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE: "0.7"
  },
  knobs: [
    { env: "NEWS_MODEL_MAX_INPUT_TOKENS", type: "number", default: 100000 },
    { env: "NEWS_ARTICLE_SUMMARY_MAX_TOKENS", type: "number", default: 4000 },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL", type: "text", default: "http://localhost:11434/v1" },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE", type: "select", default: 0.7, options: ["0.2", "0.7", "1.0"] },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TOP_P", type: "select", default: 0.9, options: ["0.8", "0.9", "1.0"] }
  ]
};
const populatedPanel = modelTuningPanel("article_summary");
assert(populatedPanel.includes("Resolved: gpt-4o"), "resolved model name missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "shared cap knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_ARTICLE_SUMMARY_MAX_TOKENS"'), "task max tokens knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL"'), "base URL knob missing after metadata arrival");
assert(populatedPanel.includes('value="http://localhost:11434/v1"'), "base URL current value missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE"'), "sampling field missing after metadata arrival");
assert(populatedPanel.includes('<option value="0.7" selected>0.7</option>'), "sampling field current value not selected after metadata arrival");

// ---- 4. Known-task parity --------------------------------------------------
// Every configured task must still produce panel markup with an empty schema
// so the guard cannot silently suppress valid panels.
for (const task of Object.keys(TASK_CONFIG)) {
  const panel = modelTuningPanel(task);
  assert(typeof panel === "string" && panel.length > 0, `${task} must render non-empty panel markup`);
  assert(panel.includes(`<h2>${TASK_CONFIG[task].label}</h2>`), `${task} panel label missing`);
}

// ---- 5. Advanced Settings integration with missing metadata ---------------
// Simulate configuration drift at the parent-render boundary. The missing
// panel must interpolate as an empty string while later Advanced Settings
// sections remain present.
delete TASK_CONFIG.article_summary;
let renderError = null;
try {
  renderAdvancedPanels();
} catch (error) {
  renderError = error;
}
assert(!renderError, `Advanced Settings render threw: ${renderError}`);
assert(advancedPanels.innerHTML.includes("<h2>Story Writing</h2>"), "remaining model panel missing after a task is removed");
assert(advancedPanels.innerHTML.includes("<h2>Run budgets and quotas</h2>"), "Budgets panel missing after a task is removed");
assert(!advancedPanels.innerHTML.includes("undefined"), "undefined markup leaked into Advanced Settings");
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_model_tuning_panel_metadata_guard_executes_in_node_harness(self) -> None:
        """Execute the production modelTuningPanel() renderer in a Node harness.

        The panel must tolerate missing task metadata (issue #118) without
        aborting the Advanced Settings render, while preserving safe rendering
        with an initially empty schema and producing the full panel once
        populated metadata arrives. TASK_CONFIG, state, the knob/sampling
        helpers, and modelTuningPanel() are extracted from ui_module.HTML
        itself, not reimplemented here.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + r"""
// inputForKnob reads current values through document.querySelector; the
// harness has no DOM controls, so every lookup falls back to
// state.schema.current_env, mirroring a schema-driven initial render.
const document = { querySelector() { return null; } };
const advancedPanels = { innerHTML: "" };
const $ = (id) => id === "advancedPanels" ? advancedPanels : null;
const decorateEnvHints = () => {};
const renderPromptProfilePanel = () => {};
const renderPromptTemplateEditors = () => {};
"""
            + js_function_block("    const TASK_CONFIG = {", "    const state = {")
            + js_function_block("    const state = {", "    const icons = {")
            + js_function_block(
                "    const TASK_MAX_TOKENS_LABELS = {", "    const SAMPLING_FIELDS = ["
            )
            + js_function_block(
                "    const SAMPLING_FIELDS = [", "    function samplingFields(prefix) {"
            )
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block(
                'function formatDefault(value, fallback="none") {',
                "function currentControlValue",
            )
            + js_function_block(
                "function currentControlValue(env) {", "function setControlValue"
            )
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block(
                'function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {',
                "function knobField",
            )
            + js_function_block(
                "function knobField(env, label, options={}) {", "function knobHint"
            )
            + js_function_block(
                "function samplingFields(prefix) {", "function modelTuningPanel"
            )
            + js_function_block(
                "function modelTuningPanel(task) {", "function renderAdvancedPanels"
            )
            + js_function_block(
                "function renderAdvancedPanels() {", "function renderAdvancedKnobs"
            )
            + r"""
// ---- 1. Missing task metadata ---------------------------------------------
// An unknown task must render no markup instead of throwing, so one
// unrecognized panel cannot abort the whole Advanced Settings render.
assert(state.schema === null, "harness state must start with an empty schema");
assert(modelTuningPanel("missing_task") === "", "unknown task must render the empty string");
assert(modelTuningPanel("definitely_not_a_task") === "", "unknown task must render the empty string");

// ---- 2. Empty initial schema metadata -------------------------------------
// A known task with no schema yet must still render the panel shell with the
// existing Resolved fallback and without any schema-backed knob controls.
const emptyPanel = modelTuningPanel("article_summary");
assert(typeof emptyPanel === "string" && emptyPanel.length > 0, "known task must render panel markup with an empty schema");
assert(emptyPanel.includes("<h2>Article Summarization</h2>"), "panel label missing from empty-schema markup");
assert(emptyPanel.includes("Resolved: -"), "empty-schema panel must show the Resolved fallback");
assert(emptyPanel.includes('<select id="article_tuning_preset"'), "preset select id missing from empty-schema markup");
assert(!emptyPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "schema-backed knob rendered without schema metadata");

// ---- 3. Populated metadata arrival ----------------------------------------
// Once runtime metadata and schema knobs arrive, the same panel must render
// the resolved model and the shared cap, max tokens, base URL, and sampling
// controls that were absent in the empty-schema state.
state.schema = {
  runtime: { model: { article_summary: { name: "gpt-4o", reference: "gpt-4o" } } },
  current_env: {
    NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL: "http://localhost:11434/v1",
    NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE: "0.7"
  },
  knobs: [
    { env: "NEWS_MODEL_MAX_INPUT_TOKENS", type: "number", default: 100000 },
    { env: "NEWS_ARTICLE_SUMMARY_MAX_TOKENS", type: "number", default: 4000 },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL", type: "text", default: "http://localhost:11434/v1" },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE", type: "select", default: 0.7, options: ["0.2", "0.7", "1.0"] },
    { env: "NEWS_MODEL_ARTICLE_SUMMARY_TOP_P", type: "select", default: 0.9, options: ["0.8", "0.9", "1.0"] }
  ]
};
const populatedPanel = modelTuningPanel("article_summary");
assert(populatedPanel.includes("Resolved: gpt-4o"), "resolved model name missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_MAX_INPUT_TOKENS"'), "shared cap knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_ARTICLE_SUMMARY_MAX_TOKENS"'), "task max tokens knob missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL"'), "base URL knob missing after metadata arrival");
assert(populatedPanel.includes('value="http://localhost:11434/v1"'), "base URL current value missing after metadata arrival");
assert(populatedPanel.includes('data-env="NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE"'), "sampling field missing after metadata arrival");
assert(populatedPanel.includes('<option value="0.7" selected>0.7</option>'), "sampling field current value not selected after metadata arrival");

// ---- 4. Known-task parity --------------------------------------------------
// Every configured task must still produce panel markup with an empty schema
// so the guard cannot silently suppress valid panels.
for (const task of Object.keys(TASK_CONFIG)) {
  const panel = modelTuningPanel(task);
  assert(typeof panel === "string" && panel.length > 0, `${task} must render non-empty panel markup`);
  assert(panel.includes(`<h2>${TASK_CONFIG[task].label}</h2>`), `${task} panel label missing`);
}

// ---- 5. Advanced Settings integration with missing metadata ---------------
// Simulate configuration drift at the parent-render boundary. The missing
// panel must interpolate as an empty string while later Advanced Settings
// sections remain present.
delete TASK_CONFIG.article_summary;
let renderError = null;
try {
  renderAdvancedPanels();
} catch (error) {
  renderError = error;
}
assert(!renderError, `Advanced Settings render threw: ${renderError}`);
assert(advancedPanels.innerHTML.includes("<h2>Story Writing</h2>"), "remaining model panel missing after a task is removed");
assert(advancedPanels.innerHTML.includes("<h2>Run budgets and quotas</h2>"), "Budgets panel missing after a task is removed");
assert(!advancedPanels.innerHTML.includes("undefined"), "undefined markup leaked into Advanced Settings");
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

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
            "NEWS_MODEL_IMAGE_ART_DIRECTION",
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
        # All five override env vars are registry-marked advanced_panels (the
        # dedicated editors are the single surface; no raw duplicate).
        registry = {knob["env"]: knob["ui_location"] for knob in build_knob_registry()}
        for env_var in (
            "NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY",
            "NEWS_PROMPT_OVERRIDE_STORY_SCALE_SCREENING",
            "NEWS_PROMPT_OVERRIDE_STORY_DRAFTING",
            "NEWS_PROMPT_OVERRIDE_TITLE_GENERATION",
            "NEWS_PROMPT_OVERRIDE_IMAGE_ART_DIRECTION",
        ):
            self.assertEqual(registry[env_var], "advanced_panels", env_var)
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

    def test_prompt_template_editors_markup_and_surfaced_envs(self) -> None:
        html = ui_module.HTML
        # The Advanced panel hosts the full-template editors rendered from
        # schema.prompt_templates (not a second hard-coded task list).
        advanced = html.split("function renderAdvancedPanels")[1].split("function renderAdvancedKnobs")[0]
        self.assertEqual(advanced.count('id="promptTemplateEditors"'), 1)
        self.assertIn("renderPromptTemplateEditors();", advanced)
        self.assertIn("promptTemplateEnvMap", html)
        self.assertIn('data-template-system="${escapeHtml(t.task)}"', html)
        self.assertIn('data-template-user="${escapeHtml(t.task)}"', html)
        self.assertIn('class="prompt-template-validate"', html)
        self.assertIn('class="prompt-template-restore"', html)
        self.assertIn("restoreAllPromptTemplatesBtn", html)
        # The sentence-level profile editors remain in their own panel.
        self.assertEqual(advanced.count('id="promptProfileReadouts"'), 1)
        self.assertEqual(advanced.count('id="comparePromptProfileBtn"'), 1)
        # All five template envs are registry-marked advanced_panels (the
        # dedicated editors are the single surface; no raw duplicates).
        registry = {knob["env"]: knob["ui_location"] for knob in build_knob_registry()}
        for env_var in (
            "NEWS_PROMPT_TEMPLATE_ARTICLE_SUMMARY",
            "NEWS_PROMPT_TEMPLATE_STORY_SCALE_SCREENING",
            "NEWS_PROMPT_TEMPLATE_STORY_DRAFTING",
            "NEWS_PROMPT_TEMPLATE_TITLE_GENERATION",
            "NEWS_PROMPT_TEMPLATE_IMAGE_ART_DIRECTION",
        ):
            self.assertEqual(registry[env_var], "advanced_panels", env_var)
        # The editor DOM never carries data-env on the role textareas (two
        # inputs with the same env name would duplicate collectEnv() values).
        self.assertNotIn("data-env=\"${escapeHtml(envVar)}\"", html)
        # Changed-only JSON serialization and raw round-trip helpers exist.
        self.assertIn("function currentPromptTemplateEnv()", html)
        self.assertIn("JSON.stringify({ system: sysEl.value, user: userEl.value })", html)
        self.assertIn("function setPromptTemplateEnv(env)", html)
        self.assertIn("function restorePromptTemplateTask(task)", html)
        # Preset save validates the current template values first.
        self.assertIn("await validatePromptTemplateEnv(body.env);", html)
        self.assertIn("/api/prompt-templates/validate", html)
        # The editor's data-template-* elements are looked up with CSS
        # selectors via document.querySelector; the ID-only $ helper must
        # never receive a selector string (issue #227). The executable Node
        # harness below is the behavioral guard; this is only a source drift
        # guard for the same contract.
        prompt_block = html.split("// Advanced full-template editors (ADR 0015).", 1)[1].split("function modelTaskLabels()", 1)[0]
        self.assertNotIn("$(`[data-template-", prompt_block)
        self.assertIn(
            'const sysEl = document.querySelector(`[data-template-system="${t.task}"]`);',
            prompt_block,
        )
        self.assertIn(
            'const userEl = document.querySelector(`[data-template-user="${t.task}"]`);',
            prompt_block,
        )
        self.assertIn(
            'const statusEl = document.querySelector(`[data-template-status="${task}"]`);',
            prompt_block,
        )

    def test_prompt_template_validation_endpoint_and_preset_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            presets_path = Path(tmpdir) / "run_presets.yaml"
            _write_yaml_mapping(presets_path, {"presets": {}})
            with patch.object(ui_module, "RUN_PRESETS_PATH", presets_path):
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

                good_template = {
                    "system": "Custom art: $image_contract $editorial_instructions",
                    "user": "$synthesis_body",
                }
                status, _, body = invoke(
                    "do_POST",
                    "/api/prompt-templates/validate",
                    body=json.dumps({"templates": {"image_art_direction": good_template}}),
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"valid": True, "errors": {}})

                status, _, body = invoke(
                    "do_POST",
                    "/api/prompt-templates/validate",
                    body=json.dumps(
                        {"templates": {"story_drafting": {"system": "x", "user": "y"}}}
                    ),
                )
                self.assertEqual(status, 400)
                error = json.loads(body)["error"]
                self.assertIn("story_drafting", error)
                self.assertIn("missing required placeholder", error)
                # The error must not echo the untrusted template body.
                self.assertNotIn('"x"', error)

                status, _, body = invoke(
                    "do_POST",
                    "/api/prompt-templates/validate",
                    body=json.dumps({"task": "story_discovery", "template": good_template}),
                )
                self.assertEqual(status, 400)
                self.assertIn("story_discovery", json.loads(body)["error"])

                # Preset CRUD rejects an invalid full-template env value before
                # writing; a valid one round-trips.
                env_var = prompt_templates.PROMPT_TEMPLATE_ENV_VARS["image_art_direction"]
                status, _, body = invoke(
                    "do_POST",
                    "/api/presets",
                    body=json.dumps({"id": "bad-tpl", "env": {env_var: '{"system":"x"}'}}),
                )
                self.assertEqual(status, 400)
                self.assertIn(env_var, json.loads(body)["error"])
                self.assertEqual(list_presets()["presets"], [])

                status, _, body = invoke(
                    "do_POST",
                    "/api/presets",
                    body=json.dumps({"id": "good-tpl", "env": {env_var: json.dumps(good_template)}}),
                )
                self.assertEqual(status, 201)
                saved_env = json.loads(body)["preset"]["env"]
                self.assertEqual(json.loads(saved_env[env_var]), good_template)

    def test_prompt_template_schema_metadata(self) -> None:
        schema = schema_payload()
        templates = schema["prompt_templates"]
        self.assertEqual(
            [record["task"] for record in templates],
            list(prompt_catalog.PROMPT_TASKS),
        )
        self.assertNotIn(
            "story_discovery", [record["task"] for record in templates]
        )
        for record in templates:
            self.assertIn(record["env_var"], schema["current_env"])
            self.assertIn(record["env_var"], {knob["env"] for knob in schema["knobs"]})
            self.assertTrue(record["system"].strip())
            self.assertTrue(record["user"].strip())
            self.assertTrue(record["required_placeholders"])
            self.assertIn("editorial_instructions", record["placeholder_descriptions"])

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

    def test_render_knob_links_execute_in_node_dom_harness(self) -> None:
        """Execute the embedded renderKnobLinks renderer in a DOM-shaped Node harness.

        The production functions are extracted from ui_module.HTML itself and run
        in Node so known-link output, the external-model muted note, HTML
        escaping, default fallback, stale cleanup, and defensive branches are
        observed rather than inferred from source markers.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + _FAKE_DOM_ELEMENT_JS
            + r"""
const state = { schema: null };
const controls = {
  NEWS_TEST_MODEL: new FakeElement("modelControl", "")
};
const containers = {
  NEWS_TEST_MODEL: new FakeElement("modelLinks")
};
const warnings = [];
const missingControlEnvs = new Set();
// Production uses two selector shapes: [data-env="..."] resolves the control
// read by currentControlValue/inputForKnob, while [data-links-for="..."]
// resolves the .knob-links container written by renderKnobLinks. Unknown
// selectors fail loudly so drift cannot silently exercise the wrong branch;
// a missing container is the one intentional null return.
const document = {
  querySelector(selector) {
    if (selector.startsWith('[data-env="') && selector.endsWith('"]')) {
      const env = selector.slice('[data-env="'.length, -2);
      if (!(env in controls)) {
        if (!missingControlEnvs.has(env)) throw new Error(`unexpected control selector: ${selector}`);
        return null;
      }
      return controls[env];
    }
    if (selector.startsWith('[data-links-for="') && selector.endsWith('"]')) {
      const env = selector.slice('[data-links-for="'.length, -2);
      return containers[env] || null;
    }
    throw new Error(`unexpected selector: ${selector}`);
  }
};
const console = { warn: (...args) => warnings.push(args.join(" ")) };
"""
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block('function formatDefault(value, fallback="none") {', "function currentControlValue")
            + js_function_block("function currentControlValue(env) {", "function setControlValue")
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block('function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {', "function knobField")
            + js_function_block("function renderKnobLinks(env) {", "function refreshModelKnobLinks")
            + r"""
const control = controls.NEWS_TEST_MODEL;
const container = containers.NEWS_TEST_MODEL;
state.schema = {
  current_env: {},
  knobs: [
    {
      env: "NEWS_TEST_MODEL",
      type: "select",
      default: "known-model",
      options: ["known-model", "other-model"],
      option_links: {
        "known-model": {
          page: "https://example.test/model/known",
          hardware: "https://example.test/model/known/hardware"
        }
      }
    }
  ]
};
const knob = state.schema.knobs[0];

// The fixture mirrors the production container contract emitted by
// inputForKnob: a data-env select plus a knob-links container when the
// knob carries option_links.
const inputMarkup = inputForKnob(knob);
assert(inputMarkup.includes('<select data-env="NEWS_TEST_MODEL">'), "inputForKnob did not emit the data-env select");
assert(inputMarkup.includes('<option value="">default: known-model</option>'), "inputForKnob did not format the default empty option");
assert(inputMarkup.includes('<option value="known-model">known-model</option>'), "inputForKnob did not emit the known option");
assert(inputMarkup.includes('<div class="knob-links" data-links-for="NEWS_TEST_MODEL"></div>'), "inputForKnob did not emit the knob-links container");

// Known current value: exactly two anchors with the security attributes,
// replacing any stale container content.
control.value = "known-model";
container.innerHTML = '<a href="https://stale.test/old">stale</a>';
renderKnobLinks("NEWS_TEST_MODEL");
const anchors = (container.innerHTML.match(/<a /g) || []).length;
assert(anchors === 2, `expected exactly two anchors, got ${anchors}`);
assert(container.innerHTML.includes(
  '<a href="https://example.test/model/known" target="_blank" rel="noopener noreferrer">Hugging Face page</a>'
), "page URL is not bound to the page anchor");
assert(container.innerHTML.includes(
  '<a href="https://example.test/model/known/hardware" target="_blank" rel="noopener noreferrer" title="Native Hardware Compatibility panel (GGUF/MLX) on the model page">Hardware compatibility</a>'
), "hardware URL is not bound to the hardware anchor");
assert((container.innerHTML.match(/target="_blank"/g) || []).length === 2, "both anchors must open in a new tab");
assert((container.innerHTML.match(/rel="noopener noreferrer"/g) || []).length === 2, "both anchors must carry noopener noreferrer");
assert(!container.innerHTML.includes("stale"), "stale container content was not replaced");

// Empty control value falls back to the knob default for display only; the
// select itself stays empty so collectEnv() submission semantics are unchanged.
control.value = "";
container.innerHTML = "stale links";
renderKnobLinks("NEWS_TEST_MODEL");
assert(container.innerHTML.includes('href="https://example.test/model/known"'), "default model links did not render for an empty control");
assert(control.value === "", "renderer must not pre-select the default on the control");

// Unknown/external value: muted note replaces any stale anchors.
control.value = "external-model";
container.innerHTML = '<a href="https://stale.test/old">stale</a>';
renderKnobLinks("NEWS_TEST_MODEL");
assert(container.innerHTML === '<span class="muted">No Hugging Face page for this external model</span>', "external model did not render the muted note");

// HTML-sensitive URL characters pass through the real production escapeHtml:
// assert literal entities, never a test-side escaping implementation.
knob.option_links["known-model"] = {
  page: 'https://example.test/m?a=1&b=<2>"3"',
  hardware: 'https://example.test/h?x=4&y=<5>"6"'
};
control.value = "known-model";
renderKnobLinks("NEWS_TEST_MODEL");
assert(container.innerHTML.includes('href="https://example.test/m?a=1&amp;b=&lt;2&gt;&quot;3&quot;"'), "page URL was not escaped with literal entities");
assert(container.innerHTML.includes('href="https://example.test/h?x=4&amp;y=&lt;5&gt;&quot;6&quot;"'), "hardware URL was not escaped with literal entities");
assert(!container.innerHTML.includes("a=1&b=<2>"), "raw ampersand/angle markup leaked into page link");
assert(!container.innerHTML.includes('b=<2>"'), "raw quote markup leaked into page link");
assert(!container.innerHTML.includes("<2>"), "raw angle brackets leaked into rendered links");

// Empty control with no default clears stale links instead of keeping them.
knob.default = null;
control.value = "";
container.innerHTML = '<a href="https://stale.test/old">stale</a>';
renderKnobLinks("NEWS_TEST_MODEL");
assert(container.innerHTML === "", "stale links were not cleared for an empty value with no default");

// Missing schema knob: the container shows the defensive unavailable note.
containers.NEWS_TEST_MISSING = new FakeElement("missingLinks");
renderKnobLinks("NEWS_TEST_MISSING");
assert(containers.NEWS_TEST_MISSING.innerHTML === '<span class="muted">Links unavailable</span>', "missing knob did not render the unavailable note");

// Missing container: warn and return without throwing.
const warningCount = warnings.length;
renderKnobLinks("NEWS_TEST_NO_CONTAINER");
assert(warnings.length === warningCount + 1, "missing container did not emit the expected warning");
assert(warnings[warningCount].includes('no [data-links-for="NEWS_TEST_NO_CONTAINER"] container'), "missing container warning text drifted");

// A partial DOM can omit the control while schema.current_env still carries
// the selected value; renderKnobLinks must use that defensive fallback.
state.schema.current_env.NEWS_TEST_FALLBACK = "known-model";
state.schema.knobs.push({
  env: "NEWS_TEST_FALLBACK",
  type: "select",
  default: "other-model",
  options: ["known-model"],
  option_links: {
    "known-model": {
      page: "https://example.test/model/fallback",
      hardware: "https://example.test/model/fallback/hardware"
    }
  }
});
missingControlEnvs.add("NEWS_TEST_FALLBACK");
containers.NEWS_TEST_FALLBACK = new FakeElement("fallbackLinks");
renderKnobLinks("NEWS_TEST_FALLBACK");
assert(
  containers.NEWS_TEST_FALLBACK.innerHTML.includes("https://example.test/model/fallback"),
  "renderKnobLinks did not use schema.current_env when the control was absent"
);
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_sync_surfaced_envs_executes_in_node_dom_harness(self) -> None:
        """Execute the embedded syncSurfacedEnvs() derivation in a Node harness.

        The production Set and sync function are extracted from ui_module.HTML
        itself, so the observed behavior is the real code: only run_setup /
        advanced_panels knobs are suppressed, raw and missing-metadata knobs
        stay in the raw override list (fail-safe), and re-syncing clears stale
        entries.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const state = { schema: null };
"""
            + js_function_block("const SURFACED_ENVS = new Set();", "const TASK_CONFIG")
            + r"""

// Fixture: one dedicated panel knob, one raw knob, one knob with no
// ui_location metadata (must fail safe by staying visible in raw overrides).
state.schema = {
  knobs: [
    { env: "NEWS_TEST_PANEL", ui_location: "advanced_panels" },
    { env: "NEWS_TEST_SETUP", ui_location: "run_setup" },
    { env: "NEWS_TEST_RAW", ui_location: "advanced_raw" },
    { env: "NEWS_TEST_NO_LOCATION" }
  ]
};
syncSurfacedEnvs();
assert(SURFACED_ENVS.has("NEWS_TEST_PANEL"), "advanced_panels knob was not suppressed");
assert(SURFACED_ENVS.has("NEWS_TEST_SETUP"), "run_setup knob was not suppressed");
assert(!SURFACED_ENVS.has("NEWS_TEST_RAW"), "advanced_raw knob must stay in the raw list");
assert(!SURFACED_ENVS.has("NEWS_TEST_NO_LOCATION"), "missing metadata must fail safe (not suppressed)");

// Unknown location metadata also fails safe (never suppresses).
state.schema.knobs[0].ui_location = "stale_panel";
state.schema.knobs[3].ui_location = "legacy_location";
syncSurfacedEnvs();
assert(!SURFACED_ENVS.has("NEWS_TEST_PANEL"), "unknown location must not suppress");
assert(SURFACED_ENVS.has("NEWS_TEST_SETUP"), "run_setup knob lost after re-sync");

// Re-sync clears stale entries when a knob is reclassified to raw.
state.schema.knobs[1].ui_location = "advanced_raw";
syncSurfacedEnvs();
assert(!SURFACED_ENVS.has("NEWS_TEST_SETUP"), "re-sync did not clear a reclassified knob");
assert(SURFACED_ENVS.size === 0, "expected an empty derived set after reclassification");

// Missing schema/knobs array yields an empty set without throwing.
state.schema = null;
syncSurfacedEnvs();
assert(SURFACED_ENVS.size === 0, "missing schema must not throw and must suppress nothing");
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_recommendation_renderer_reads_schema_picks(self) -> None:
        html = ui_module.HTML
        # Keep the bounded source-contract guard focused on the complete
        # recommendation renderer rather than scattered implementation strings.
        block = html.split("function renderRecommendations", 1)[1].split(
            "async function searchHuggingFaceModels", 1
        )[0]
        for snippet in (
            "const picks = (state.schema && state.schema.model_recommendations && state.schema.model_recommendations[task]) || [];",
            "container.innerHTML = picks.map(pick => `",
            "escapeHtml(pick.name)",
            "escapeHtml(pick.alias)",
            "escapeHtml(pick.reason)",
            'data-use-model="${escapeHtml(pick.alias)}"',
            "btn.onclick = () => useModelReference(btn.dataset.useModel, catalogBackendForReference(btn.dataset.useModel));",
        ):
            self.assertIn(snippet, block)
        # The renderer no longer re-filters full catalog entries by task notes.
        self.assertNotIn("entry.task_notes[task]", block)
        self.assertNotIn("modelCatalogEntries().filter", block)
        # Empty or partial pick lists keep the documented honest-gap message.
        self.assertIn("No verified curated model for this task yet", block)
    def test_model_backend_hint_markup_contract(self) -> None:
        """The Default model panel exposes an accessible backend-compatibility
        hint driven by catalog/HF backend metadata (issue #94)."""
        html = ui_module.HTML
        # Markup and accessibility: the hint sits under the Default model
        # control and is announced politely; the hidden class starts it empty.
        self.assertIn('id="modelBackendHint"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("renderModelBackendHint", html)

        # Scope assertions to the lookup and renderer so unrelated occurrences
        # cannot keep this source-level contract green after a wiring change.
        lookup = html.split("function catalogBackendForReference", 1)[1].split(
            "function requiredBackendForSelectedModel", 1
        )[0]
        hint = html.split("function renderModelBackendHint", 1)[1].split(
            "function renderModelCatalogPanel", 1
        )[0]
        self.assertIn('const clean = (reference || "").trim();', lookup)
        self.assertIn('if (!clean) return "";', lookup)
        self.assertIn("entry.alias === clean || entry.reference === clean", lookup)
        self.assertIn("if (!required || !backendRequirementMismatch(required)) {", hint)
        self.assertIn("function backendRequirementMismatch(requiredBackend)", html)
        self.assertIn(
            'normalizedBackendValue(currentControlValue("NEWS_MODEL_BACKEND"))',
            html,
        )
        self.assertIn("This model needs NEWS_MODEL_BACKEND=${required}", hint)
        self.assertIn('hint.textContent = "";', hint)
        self.assertIn('hint.classList.add("hidden")', hint)
        self.assertIn('hint.classList.remove("hidden")', hint)
        self.assertIn('currentControlValue("NEWS_MODEL_BACKEND")', html)
        self.assertNotIn("hint.innerHTML", hint)
        # The effective backend helper resolves an empty control to the
        # registry default (issue #169): blank means the fixed default, never
        # selected-model inference, and the DOM value is not rewritten.
        helper = html.split("function effectiveModelBackend()", 1)[1].split(
            "function inputForKnob", 1
        )[0]
        self.assertIn('currentControlValue("NEWS_MODEL_BACKEND")', helper)
        self.assertIn('knobByEnv("NEWS_MODEL_BACKEND")', helper)
        self.assertIn("knob.default", helper)
        self.assertNotIn('setControlValue("NEWS_MODEL_BACKEND"', helper)

    def test_model_backend_hint_wiring_covers_each_path(self) -> None:
        html = ui_module.HTML
        catalog = html.split("function renderModelCatalogPanel", 1)[1].split(
            "function renderRecommendations", 1
        )[0]
        recommendations = html.split("function renderRecommendations", 1)[1].split(
            "async function searchHuggingFaceModels", 1
        )[0]
        search = html.split("async function searchHuggingFaceModels", 1)[1].split(
            "async function comparePromptProfiles", 1
        )[0]
        hf_buttons = html.split("function refreshHuggingFaceUseButtons", 1)[1].split(
            "async function searchHuggingFaceModels", 1
        )[0]
        presets = html.split("function applyRunPreset", 1)[1].split(
            "function resetAllOverrides", 1
        )[0]
        reset = html.split("function resetAllOverrides", 1)[1].split(
            "function setKnobEnv", 1
        )[0]
        delegated = html.split('document.addEventListener("change"', 1)[1].split(
            "await loadSources", 1
        )[0]
        boot = html.split("async function init()", 1)[1].split("init().catch", 1)[0]

        expected_catalog_call = (
            "useModelReference(btn.dataset.useModel, "
            "catalogBackendForReference(btn.dataset.useModel));"
        )
        self.assertIn(expected_catalog_call, catalog)
        self.assertIn(expected_catalog_call, recommendations)
        self.assertIn(
            'useModelReference(btn.dataset.useHfModel, btn.dataset.useHfBackend || "");',
            search,
        )
        self.assertIn('btn.disabled = disabled;', hf_buttons)
        self.assertIn('btn.textContent = disabled', hf_buttons)
        self.assertIn("refreshModelKnobLinks();", presets)
        self.assertIn('void previewQuietly("run");', presets)
        self.assertIn("refreshModelKnobLinks();", reset)
        self.assertIn('void previewWithStatus("run");', reset)
        self.assertIn('state.selectedModelRequiredBackend = catalogBackendForReference(el.value);', delegated)
        self.assertIn("requiredBackendForSelectedModel()", delegated)
        self.assertIn("refreshHuggingFaceUseButtons();", delegated)
        self.assertIn("refreshModelKnobLinks();", boot)

    def test_model_catalog_panel_markup_contract(self) -> None:
        """The Model catalog panel exposes the expected heading, controls,
        pipeline options, and limit attributes (issue #96)."""
        html = ui_module.HTML
        block = html.split('<p class="eyebrow">Model catalog</p>', 1)[1].split(
            "<summary>Utilities</summary>", 1
        )[0]
        for snippet in (
            "Model catalog and Hugging Face search",
            "Built-in models are verified for the managed backends",
            'id="recommendationTask"',
            'id="recommendationReadout"',
            'id="catalogCards"',
            "<summary>Search Hugging Face</summary>",
            'id="modelSearchQuery"',
            'id="modelSearchPipeline"',
            'id="modelSearchLimit"',
            'id="modelSearchBtn"',
            'id="modelSearchResults"',
            '<option value="text-generation">text-generation</option>',
            '<option value="text2text-generation">text2text-generation</option>',
            '<option value="image-text-to-text">image-text-to-text</option>',
            'min="1"',
            'max="50"',
            'value="10"',
        ):
            self.assertIn(snippet, block)

    def test_model_catalog_panel_renderer_wiring_contract(self) -> None:
        """The model-catalog renderer blocks keep their schema-backed data
        sources, empty/error fallbacks, escaped fields, and backend-aware
        wiring (issue #96)."""
        html = ui_module.HTML
        panel = html.split("function renderModelCatalogPanel() {", 1)[1].split(
            "function renderRecommendations", 1
        )[0]
        recommendations = html.split("function renderRecommendations", 1)[1].split(
            "async function searchHuggingFaceModels", 1
        )[0]
        search = html.split("async function searchHuggingFaceModels", 1)[1].split(
            "async function comparePromptProfiles", 1
        )[0]

        # Panel: schema-backed task options and catalog cards with escaped
        # fields, plus the catalog-aware use-model binding.
        for snippet in (
            "(state.schema && state.schema.model_recommendation_tasks) || []",
            "modelCatalogEntries().map(entry => `",
            "escapeHtml(entry.name)",
            "escapeHtml(entry.alias)",
            "escapeHtml(entry.description)",
            "escapeHtml(entry.backend)",
            "escapeHtml(entry.hf_url)",
            "renderRecommendations(select.value);",
            "btn.onclick = () => useModelReference(btn.dataset.useModel, catalogBackendForReference(btn.dataset.useModel));",
        ):
            self.assertIn(snippet, panel)

        # Recommendations: empty-task and empty-picks fallbacks stay honest,
        # and the Use buttons resolve the backend from the catalog.
        self.assertIn("Pick a task to see curated recommendations.", recommendations)
        self.assertIn("No verified curated model for this task yet", recommendations)
        self.assertIn(
            "btn.onclick = () => useModelReference(btn.dataset.useModel, catalogBackendForReference(btn.dataset.useModel));",
            recommendations,
        )

        # Search: empty query, API error, empty results, runtime-fit backend
        # lookup, external-only disabled state, binding, and catch rendering.
        for snippet in (
            "Enter a query to search Hugging Face.",
            "`/api/models/search?q=${encodeURIComponent(query)}&pipeline_tag=${encodeURIComponent(pipeline)}&limit=${limit}`",
            "if (data.error) {",
            "No models found.",
            "Object.prototype.hasOwnProperty.call(RUNTIME_FIT_BACKENDS, fit.status)",
            "RUNTIME_FIT_BACKENDS[fit.status]",
            'data-use-hf-backend="${escapeHtml(hfBackend)}"',
            "const useDisabled = backendRequirementMismatch(hfBackend);",
            "Set NEWS_MODEL_BACKEND=${hfBackend} to use",
            'useModelReference(btn.dataset.useHfModel, btn.dataset.useHfBackend || "");',
            "catch (err) {",
            "escapeHtml(err.message)",
        ):
            self.assertIn(snippet, search)

    def test_runtime_fit_backend_map_matches_catalog_vocabulary(self) -> None:
        html = ui_module.HTML
        block = html.split("const RUNTIME_FIT_BACKENDS = {", 1)[1].split("};", 1)[0]
        expected = {
            model_catalog.RUNTIME_FIT_MANAGED_MLX_LM: "mlx-lm",
            model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM: "mlx-vlm",
            model_catalog.RUNTIME_FIT_MANAGED_LLAMA_CPP: "llama.cpp",
            model_catalog.RUNTIME_FIT_EXTERNAL_ONLY: "external",
        }
        for status, backend in expected.items():
            self.assertIn(f'{status}: "{backend}"', block)
        self.assertIn(
            "Object.prototype.hasOwnProperty.call(RUNTIME_FIT_BACKENDS, fit.status)",
            html,
        )
        self.assertIn('data-use-hf-backend="${escapeHtml(hfBackend)}"', html)
        self.assertIn("function backendRequirementMismatch(requiredBackend)", html)
        self.assertIn(
            'normalizedBackendValue(currentControlValue("NEWS_MODEL_BACKEND"))',
            html,
        )
        # Use must never mutate the backend control.
        self.assertNotIn('setControlValue("NEWS_MODEL_BACKEND"', html)

    @unittest.skipUnless(shutil.which("node"), "node runtime required for model/backend behavior tests")
    def test_model_backend_hint_and_selection_behavior(self) -> None:
        html = ui_module.HTML
        current_start = html.index("    function currentControlValue")
        current_end = html.index("    function setControlValue", current_start)
        model_start = html.index("    function modelCatalogEntries")
        model_end = html.index("    function renderModelCatalogPanel", model_start)
        refresh_start = html.index("    function refreshHuggingFaceUseButtons")
        refresh_end = html.index("    async function searchHuggingFaceModels", refresh_start)
        production_source = "\n".join(
            [
                html[current_start:current_end],
                html[model_start:model_end],
                html[refresh_start:refresh_end],
            ]
        )
        script = """
import assert from "node:assert/strict";

function classList() {
  const values = new Set();
  return {
    add(value) { values.add(value); },
    remove(value) { values.delete(value); },
    contains(value) { return values.has(value); },
  };
}
function control(env, value = "", options = []) {
  return {
    dataset: { env },
    type: "select",
    tagName: "SELECT",
    value,
    options: options.map(option => ({ value: option })),
    insertAdjacentHTML(_position, html) {
      const match = html.match(/<option value="([^"]*)">/);
      if (match) this.options.unshift({ value: match[1] });
    },
    dispatchEvent(event) {
      assert.equal(event.type, "change");
      handleChange(this);
    },
  };
}
const modelSelect = control("NEWS_MODEL", "", ["vlm"]);
const backendSelect = control("NEWS_MODEL_BACKEND", "mlx-lm", ["mlx-lm", "mlx-vlm", "external"]);
const hint = { textContent: "", classList: classList() };
const externalOnlyButton = {
  dataset: { useHfModel: "org/external", useHfBackend: "external" },
  disabled: true,
  textContent: "",
};
const elements = new Map([
  ["modelBackendHint", hint],
  ["model", modelSelect],
  ["backend", backendSelect],
]);
const state = {
  schema: {
    model_catalog: [{ alias: "vlm", reference: "org/vlm", backend: "mlx-vlm" }],
    current_env: {},
  },
  selectedModelReference: "",
  selectedModelRequiredBackend: "",
};
function $(id) {
  if (id === "NEWS_MODEL") return modelSelect;
  if (id === "NEWS_MODEL_BACKEND") return backendSelect;
  return elements.get(id) || null;
}
function documentQuery(selector) {
  if (selector === '[data-env="NEWS_MODEL"]') return modelSelect;
  if (selector === '[data-env="NEWS_MODEL_BACKEND"]') return backendSelect;
  return null;
}
globalThis.document = {
  querySelector(selector) {
    return selector === "#modelBackendHint" ? hint : documentQuery(selector);
  },
  getElementById(id) {
    return elements.get(id) || null;
  },
  querySelectorAll(selector) {
    return selector === "[data-use-hf-model]" ? [externalOnlyButton] : [];
  },
};
globalThis.Event = class Event {
  constructor(type) { this.type = type; }
};
function escapeHtml(value) { return String(value); }
function handleChange(element) {
  if (element === modelSelect) {
    state.selectedModelReference = String(element.value || "").trim();
    state.selectedModelRequiredBackend = catalogBackendForReference(element.value);
    renderModelBackendHint(requiredBackendForSelectedModel());
  } else if (element === backendSelect) {
    refreshHuggingFaceUseButtons();
    renderModelBackendHint(requiredBackendForSelectedModel());
  }
}

""" + production_source + """

useModelReference("vlm", "mlx-vlm");
assert.equal(modelSelect.value, "vlm");
assert.equal(backendSelect.value, "mlx-lm");
assert.equal(hint.textContent, "This model needs NEWS_MODEL_BACKEND=mlx-vlm");
assert.equal(hint.classList.contains("hidden"), false);

backendSelect.value = "mlx-vlm";
renderModelBackendHint(requiredBackendForSelectedModel());
assert.equal(hint.classList.contains("hidden"), true);

backendSelect.value = "";
renderModelBackendHint(requiredBackendForSelectedModel());
assert.equal(hint.classList.contains("hidden"), false);
assert.equal(hint.textContent, "This model needs NEWS_MODEL_BACKEND=mlx-vlm");

backendSelect.value = " EXTERNAL ";
refreshHuggingFaceUseButtons();
assert.equal(externalOnlyButton.disabled, false);
assert.equal(externalOnlyButton.textContent, "Use");

backendSelect.value = "mlx-lm";
refreshHuggingFaceUseButtons();
assert.equal(externalOnlyButton.disabled, true);
assert.equal(externalOnlyButton.textContent, "Set NEWS_MODEL_BACKEND=external to use");
useModelReference("org/external", "external");
assert.equal(modelSelect.value, "org/external");
assert.equal(backendSelect.value, "mlx-lm");
assert.equal(hint.textContent, "This model needs NEWS_MODEL_BACKEND=external");
assert.equal(hint.classList.contains("hidden"), false);
"""
        result = subprocess.run(
            ["node", "--input-type=module"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
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
                        "image_art_direction": {"reference": "gemma-2b"},
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
                self.assertEqual(snapshot["model"]["image_art_direction"]["reference"], "gemma-2b")
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
            self.assertEqual(len(payload["model_catalog"]), 4)
            self.assertEqual(payload["model_catalog"][0]["alias"], "gemma-4-12b-it-4bit")
            self.assertIn("factual_extraction", payload["model_recommendation_tasks"])
            self.assertEqual(len(payload["model_recommendation_tasks"]), 7)
            # Server-owned recommendations: every task maps exactly to the
            # authoritative helper output (default fallback, no duplicates,
            # and the intentional empty translation list).
            self.assertEqual(
                payload["model_recommendations"],
                {
                    task: model_catalog.recommend_models(task)
                    for task in model_catalog.MODEL_RECOMMENDATION_TASKS
                },
            )
            self.assertEqual(payload["model_recommendations"]["translation"], [])
            self.assertEqual(
                [pick["alias"] for pick in payload["model_recommendations"]["speed"]],
                ["gemma-e2b-tiny", model_catalog.DEFAULT_CATALOG_MODEL_ALIAS],
            )

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
            "NEWS_MODEL_IMAGE_ART_DIRECTION",
            "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
            "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
            "NEWS_MODEL_IMAGE_ART_DIRECTION_TUNING_PRESET",
            "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL",
            "NEWS_MODEL_TITLE_GENERATION_BASE_URL",
            "NEWS_MODEL_IMAGE_ART_DIRECTION_BASE_URL",
            "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS",
            "NEWS_TITLE_GENERATION_MAX_TOKENS",
            "NEWS_IMAGE_ART_DIRECTION_MAX_TOKENS",
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

    def test_model_tuning_round_trip_persists_reloads_and_deletes(self) -> None:
        # Full record round trip through the real writer/loader: create a
        # complete valid preset, prove reload-from-disk through the public
        # listing, update metadata and tuning, then delete it.
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "model_tuning_presets.yaml"
            _write_yaml_mapping(tuning_path, {"presets": {}})
            with patch.object(ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path):
                self.assertEqual(list_model_tuning_presets()["presets"], [])

                created = upsert_model_tuning_preset(
                    {
                        "id": "concise",
                        "name": "Concise",
                        "description": "draft preset",
                        "model": "gemma-3-12b-it",
                        "task": "story_drafting",
                        "tuning": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1400},
                    }
                )
                self.assertEqual(created["preset"]["id"], "concise")

                # Reload from disk proves the write survived the YAML seam.
                reloaded = list_model_tuning_presets()["presets"]
                self.assertEqual([item["id"] for item in reloaded], ["concise"])
                self.assertEqual(reloaded[0]["task"], "story_drafting")
                self.assertEqual(
                    reloaded[0]["tuning"],
                    {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1400},
                )

                updated = upsert_model_tuning_preset(
                    {
                        "id": "concise",
                        "name": "Concise Drafts",
                        "tuning": {"temperature": 0.5, "min_p": 0.1, "model_max_input_tokens": 8192},
                    }
                )
                self.assertEqual(updated["preset"]["name"], "Concise Drafts")
                self.assertEqual(updated["preset"]["task"], "story_drafting")
                self.assertEqual(
                    updated["preset"]["tuning"],
                    {"temperature": 0.5, "min_p": 0.1, "model_max_input_tokens": 8192},
                )

                # Explicit empty optional fields clear an existing scope;
                # omission above still preserves it.
                cleared = upsert_model_tuning_preset(
                    {
                        "id": "concise",
                        "description": "",
                        "model": "",
                        "task": "",
                        "tuning": {"temperature": 0.5},
                    }
                )
                self.assertEqual(cleared["preset"]["description"], "")
                self.assertEqual(cleared["preset"]["model"], "")
                self.assertEqual(cleared["preset"]["task"], "")
                reloaded_cleared = list_model_tuning_presets()["presets"][0]
                self.assertEqual(reloaded_cleared.get("description", ""), "")
                self.assertEqual(reloaded_cleared.get("model", ""), "")
                self.assertEqual(reloaded_cleared.get("task", ""), "")

                self.assertEqual(delete_model_tuning_preset("concise")["deleted"], "concise")
                self.assertEqual(list_model_tuning_presets()["presets"], [])
                self.assertEqual(_load_yaml_mapping(tuning_path)["presets"], {})

    def test_model_tuning_string_values_round_trip_through_runtime(self) -> None:
        # Advanced Settings controls submit numeric HTML values as strings.
        # Keep those strings in YAML while proving the runtime resolver accepts
        # every integer-valued field using its int() coercion contract.
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "model_tuning_presets.yaml"
            _write_yaml_mapping(tuning_path, {"presets": {}})
            string_tuning = {
                "temperature": "0.2",
                "top_p": "0.9",
                "top_k": "0",
                "min_p": "0",
                "presence_penalty": "-2",
                "repetition_penalty": "3",
                "max_tokens": "1400",
                "model_max_input_tokens": "1",
                "article_summary_max_tokens": "1",
                "story_drafting_max_tokens": "1",
                "story_scale_screening_max_tokens": "1",
                "title_generation_max_tokens": "1",
            }
            with patch.object(ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path):
                created = upsert_model_tuning_preset(
                    {"id": "strings", "task": "story_drafting", "tuning": string_tuning}
                )
                self.assertEqual(created["preset"]["tuning"], string_tuning)
                self.assertEqual(
                    list_model_tuning_presets()["presets"][0]["tuning"], string_tuning
                )

                resolved = config_module._apply_model_tuning_preset(
                    config_module.ModelTuningSettings(task_sampling={}),
                    preset_id="strings",
                    preset=created["preset"],
                    assignment_task="story_drafting",
                )
                sampling = resolved.task_sampling["story_drafting"]
                self.assertEqual(sampling.temperature, 0.2)
                self.assertEqual(sampling.top_k, 0)
                self.assertEqual(resolved.model_max_input_tokens, 1)
                self.assertEqual(resolved.article_summary_max_tokens, 1)
                self.assertEqual(resolved.story_drafting_max_tokens, 1)
                self.assertEqual(resolved.story_scale_screening_max_tokens, 1)
                self.assertEqual(resolved.title_generation_max_tokens, 1)

    def test_model_tuning_validation_fails_before_write(self) -> None:
        # Every invalid upsert must raise with the preset id and offending
        # field/rule, and must leave the prior YAML bytes untouched.
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "model_tuning_presets.yaml"
            _write_yaml_mapping(
                tuning_path,
                {
                    "presets": {
                        "draft": {
                            "name": "Draft",
                            "tuning": {"temperature": 0.2, "max_tokens": 800},
                        }
                    }
                },
            )
            with patch.object(ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path):
                before = tuning_path.read_bytes()
                cases = [
                    ({"id": "new", "tuning": [1, 2]}, "'new' tuning must be a mapping"),
                    ({"id": "new", "tuning": None}, "'new' tuning must be a mapping"),
                    ({"id": "new", "tuning": "hot"}, "'new' tuning must be a mapping"),
                    ({"id": "new", "tuning": {"foo": 1}}, "'new' has unsupported tuning field 'foo'"),
                    ({"id": "new", "tuning": {"temperature": "warm"}}, "field 'temperature' must be a number"),
                    ({"id": "new", "tuning": {"top_p": True}}, "field 'top_p' must be a number"),
                    ({"id": "new", "tuning": {"temperature": 2.5}}, "field 'temperature' must be between 0 and 2"),
                    ({"id": "new", "tuning": {"top_p": 1.4}}, "field 'top_p' must be between 0 and 1"),
                    ({"id": "new", "tuning": {"min_p": -0.1}}, "field 'min_p' must be between 0 and 1"),
                    ({"id": "new", "tuning": {"top_k": 2.5}}, "field 'top_k' must be a whole number"),
                    ({"id": "new", "tuning": {"top_k": -1}}, "field 'top_k' must be at least 0"),
                    ({"id": "new", "tuning": {"presence_penalty": -2.5}}, "field 'presence_penalty' must be between -2 and 2"),
                    ({"id": "new", "tuning": {"repetition_penalty": 3.5}}, "field 'repetition_penalty' must be between 0 and 3"),
                    ({"id": "new", "tuning": {"max_tokens": 0}}, "field 'max_tokens' must be a whole number greater than zero"),
                    ({"id": "new", "tuning": {"max_tokens": 1.5}}, "field 'max_tokens' must be a whole number greater than zero"),
                    ({"id": "new", "tuning": {"max_tokens": -5}}, "field 'max_tokens' must be a whole number greater than zero"),
                    (
                        {"id": "new", "tuning": {"max_tokens": "nan"}},
                        "field 'max_tokens' must be a whole number greater than zero",
                    ),
                    (
                        {"id": "new", "tuning": {"article_summary_max_tokens": "inf"}},
                        "field 'article_summary_max_tokens' must be a whole number greater than zero",
                    ),
                    (
                        {"id": "new", "tuning": {"top_k": "1.0"}},
                        "field 'top_k' must be a whole number",
                    ),
                    (
                        {"id": "new", "tuning": {"max_tokens": "1400.0"}},
                        "field 'max_tokens' must be a whole number greater than zero",
                    ),
                    (
                        {"id": "new", "tuning": {"max_tokens": "1e3"}},
                        "field 'max_tokens' must be a whole number greater than zero",
                    ),
                    ({"id": "new", "task": "story_discovery"}, "task 'story_discovery' is not selectable"),
                    ({"id": "new", "task": "default"}, "task 'default' is not selectable"),
                    # PATCH on the existing record: a bad final record fails too.
                    ({"id": "draft", "tuning": {"temperature": 9}}, "field 'temperature' must be between 0 and 2"),
                ]
                for body, message in cases:
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        upsert_model_tuning_preset(body)
                    self.assertEqual(
                        tuning_path.read_bytes(),
                        before,
                        f"YAML mutated by rejected upsert {body}",
                    )

                # Image Art Direction is a first-class selectable preset scope
                # (independent task assignment with its own tuning preset, #122).
                scoped = upsert_model_tuning_preset(
                    {"id": "new", "task": "image_art_direction", "tuning": {"max_tokens": 512}}
                )
                self.assertEqual(scoped["preset"]["task"], "image_art_direction")
                self.assertEqual(scoped["preset"]["tuning"], {"max_tokens": 512})

                # A metadata-only PATCH preserves and revalidates the existing
                # mapping, and a valid write still succeeds afterward.
                renamed = upsert_model_tuning_preset({"id": "draft", "name": "Renamed"})
                self.assertEqual(renamed["preset"]["name"], "Renamed")
                self.assertEqual(
                    renamed["preset"]["tuning"],
                    {"temperature": 0.2, "max_tokens": 800},
                )

    def test_model_tuning_http_validation_returns_400(self) -> None:
        # The real upsert helper behind the HTTP handler: invalid POST/PATCH
        # bodies produce HTTP 400 JSON errors without mutating the file.
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "model_tuning_presets.yaml"
            _write_yaml_mapping(tuning_path, {"presets": {}})
            with patch.object(ui_module, "MODEL_TUNING_PRESETS_PATH", tuning_path):
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

                status, _, body = invoke(
                    "do_POST",
                    "/api/model-tuning-presets",
                    body=json.dumps({"id": "draft", "tuning": [1, 2]}),
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(body)["error"],
                    "Model tuning preset 'draft' tuning must be a mapping.",
                )
                self.assertEqual(list_model_tuning_presets()["presets"], [])

                status, _, body = invoke(
                    "do_PATCH",
                    "/api/model-tuning-presets",
                    body=json.dumps({"id": "draft", "tuning": {"top_p": 7}}),
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(body)["error"],
                    "Model tuning preset 'draft' field 'top_p' must be between 0 and 1, got 7.",
                )
                self.assertEqual(list_model_tuning_presets()["presets"], [])

                status, _, body = invoke(
                    "do_POST",
                    "/api/model-tuning-presets",
                    body=json.dumps({"id": "draft", "tuning": {"temperature": 0.2}}),
                )
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(body)["preset"]["id"], "draft")
                self.assertEqual(list_model_tuning_presets()["presets"][0]["id"], "draft")

    def test_model_tuning_dedicated_tab_static_contracts(self) -> None:
        html = ui_module.HTML
        # Navigation entry and section exist exactly once.
        self.assertIn('["modelTuning", "Model Tuning", "sliders"]', html)
        self.assertIn('<section id="modelTuning" class="view">', html)
        self.assertEqual(html.count('id="modelTuningPresetTable"'), 1)
        self.assertEqual(html.count('id="modelTuningPresetError"'), 1)
        # Dedicated, collision-free editor field IDs.
        for element_id in (
            "modelTuningPresetId",
            "modelTuningPresetName",
            "modelTuningPresetDescription",
            "modelTuningPresetModel",
            "modelTuningPresetTask",
            "modelTuningPresetTuning",
            "newModelTuningPresetBtn",
            "reloadModelTuningPresetsBtn",
            "saveModelTuningPresetBtn",
            "deleteModelTuningPresetBtn",
        ):
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)
        # The table renders every record (no filteredModelTuningPresets) and
        # escapes every interpolated value.
        editor = html.split("function renderModelTuningEditor()")[1].split("function editModelTuningPreset(")[0]
        self.assertIn("state.modelTuningPresets || []", editor)
        self.assertNotIn("filteredModelTuningPresets", editor)
        self.assertIn('escapeHtml(preset.name || preset.id || "")', editor)
        self.assertIn('escapeHtml(preset.id || "")', editor)
        self.assertIn('escapeHtml(preset.model || "")', editor)
        self.assertIn('escapeHtml(preset.task || "global")', editor)
        self.assertIn('escapeHtml(Object.keys(preset.tuning || {}).join(", "))', editor)
        # CRUD handlers are wired in wireEvents and use the shared API path
        # with POST for new IDs and PATCH for existing IDs.
        wired = html.split("function wireEvents()")[1].split("function applySelectedPresetFromState")[0]
        for handler in (
            "newModelTuningPresetBtn",
            "reloadModelTuningPresetsBtn",
            "saveModelTuningPresetBtn",
            "deleteModelTuningPresetBtn",
        ):
            self.assertIn(f'$("{handler}").onclick', wired)
        self.assertIn('api("/api/model-tuning-presets"', html)
        self.assertIn("method: exists ? \"PATCH\" : \"POST\"", html)
        self.assertIn("function reloadModelTuningPresets", html)
        self.assertIn("function saveModelTuningEditor", html)
        self.assertIn("function deleteModelTuningEditor", html)
        # Errors stay inline in the dedicated editor without discarding edits.
        self.assertIn('$("modelTuningPresetError").textContent = err.message', html)
        # The dedicated editor renders only after the schema state assignment.
        boot = html.split("async function init()")[1].split("init().catch")[0]
        self.assertIn("state.modelTuningPresets = (state.schema.model_tuning_presets && state.schema.model_tuning_presets.presets) || [];", boot)
        self.assertIn("renderModelTuningEditor();", boot)
        self.assertLess(
            boot.index("state.modelTuningPresets ="),
            boot.index("renderModelTuningEditor();"),
        )
        # Existing Advanced Settings task-panel surface is untouched.
        self.assertIn("function renderModelTuningControls(task", html)
        self.assertIn("function filteredModelTuningPresets(task", html)
        self.assertIn("function loadModelTuningPresets", html)

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
        with patch.dict(
            os.environ,
            {"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS, "NEWS_MODEL_BACKEND": "mlx-lm"},
            clear=True,
        ):
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
        with patch.dict(
            os.environ,
            {"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS, "NEWS_MODEL_BACKEND": "mlx-lm"},
            clear=True,
        ):
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
        record.append(
            "Retrying article summaries: attempt 2/3 failed (TimeoutError: model timed out); "
            "sleeping 5s before the next attempt.\n"
        )
        record.append(meter_b + "\n")
        record.append(meter_final + "\n")
        record.append("[7/9 story drafting] [####----------------] 12/47 stories\n")

        self.assertEqual(
            [event["kind"] for event in record.events],
            ["progress", "message", "message", "progress"],
        )
        # The clustering snapshot was replaced in place, never appended twice.
        self.assertEqual(record.events[0]["line"], meter_final)
        self.assertEqual(record.events[0]["stage"], "clustering")
        self.assertEqual(record.events[0]["replace"], True)
        self.assertEqual(record.events[0]["complete"], True)
        self.assertEqual(record.events[1]["line"], "WARNING: low coverage")
        # Warnings and retries stay ordinary message events in arrival order
        # between the coalesced clustering snapshot and the drafting meter.
        self.assertEqual(
            record.events[2]["line"],
            "Retrying article summaries: attempt 2/3 failed (TimeoutError: model timed out); "
            "sleeping 5s before the next attempt.",
        )
        self.assertEqual(record.events[3]["line"], "[7/9 story drafting] [####----------------] 12/47 stories")
        self.assertEqual(record.snapshot()["line_count"], 4)
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

        expected_exceptions = (
            OSError("hf down"),
            ValueError("invalid catalog response"),
            ImportError("huggingface-hub missing"),
        )
        for exception in expected_exceptions:
            with self.subTest(exception=type(exception).__name__), patch.object(
                ui_module, "search_huggingface_models", side_effect=exception
            ):
                status, _, body = self._invoke_get("/api/models/search?q=qwythos")

            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body),
                {"query": "qwythos", "models": [], "error": str(exception)},
            )

        with patch.object(
            ui_module,
            "search_huggingface_models",
            side_effect=RuntimeError("programming bug"),
        ):
            status, _, body = self._invoke_get("/api/models/search?q=qwythos")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "programming bug"})

        status, _, body = self._invoke_get("/api/models/search")
        self.assertEqual(status, 400)
        self.assertIn("Missing query parameter q.", json.loads(body)["error"])

    def test_models_search_endpoint_invalid_limit_returns_400(self) -> None:
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
            status, _, body = self._invoke_get("/api/models/search?q=qwythos&limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(body),
            {"query": "qwythos", "models": [], "error": "--limit must be an integer, got 'abc'."},
        )
        search.assert_not_called()

        # Values outside 1-50 (0, 999) stay successful too — they are clamped
        # by search_huggingface_models; the handler does not range-validate.
        for raw in ("1", "50", "0", "999"):
            with self.subTest(limit=raw), patch.object(
                ui_module, "search_huggingface_models", return_value=fake_models
            ) as search:
                status, _, body = self._invoke_get(f"/api/models/search?q=qwythos&limit={raw}")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["models"], fake_models)
            self.assertIsNone(payload["error"])
            search.assert_called_once_with("qwythos", pipeline_tag=None, limit=int(raw))

        with patch.object(
            ui_module, "search_huggingface_models", return_value=fake_models
        ) as search:
            status, _, body = self._invoke_get("/api/models/search?q=qwythos")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["models"], fake_models)
        search.assert_called_once_with("qwythos", pipeline_tag=None, limit=20)

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

        expected_exceptions = (
            OSError("network down"),
            ValueError("Model not found on Hugging Face: 'nope'"),
            ImportError("huggingface-hub missing"),
        )
        for exception in expected_exceptions:
            with self.subTest(exception=type(exception).__name__), patch.object(
                ui_module, "fetch_model_metadata", side_effect=exception
            ):
                status, _, body = self._invoke_get(
                    "/api/models/metadata?model=owner%2Frepo"
                )

            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body),
                {"model": "owner/repo", "info": None, "error": str(exception)},
            )

        with patch.object(
            ui_module,
            "fetch_model_metadata",
            side_effect=RuntimeError("programming bug"),
        ):
            status, _, body = self._invoke_get("/api/models/metadata?model=owner%2Frepo")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "programming bug"})

        status, _, body = self._invoke_get("/api/models/metadata")
        self.assertEqual(status, 400)
        self.assertIn("Missing model parameter.", json.loads(body)["error"])

    def test_schema_label_maps_and_drift_contract(self) -> None:
        """The schema serves the canonical model-catalog label maps and the
        embedded JavaScript reads them from state.schema at render time
        (issue #82): a label added on the Python side renders without a
        second UI edit, and no duplicate hardcoded map literals remain."""
        with patch.object(
            ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, None)
        ), patch.object(
            ui_module, "configured_removed_topic_env_vars", return_value=set()
        ), patch.object(
            ui_module, "list_presets", return_value={"path": "presets.yaml", "presets": []}
        ), patch.object(
            ui_module,
            "list_model_tuning_presets",
            return_value={"path": "model.yaml", "presets": []},
        ), patch.object(
            ui_module, "_source_summary", return_value={"total": 0}
        ), patch.object(ui_module, "_recipient_summary", return_value={"total": 0}):
            payload = ui_module.schema_payload()

        # The canonical dictionaries are served verbatim as copied projections.
        self.assertEqual(payload["model_task_labels"], model_catalog.MODEL_TASK_LABELS)
        self.assertEqual(payload["runtime_fit_labels"], model_catalog.RUNTIME_FIT_LABELS)
        self.assertEqual(payload["model_task_labels"]["translation"], "Translation")
        self.assertEqual(payload["runtime_fit_labels"]["external_only"], "External only")

        # A label added on the Python side appears in the schema response
        # without any UI code change and survives JSON encoding.
        with patch.dict(ui_module.MODEL_TASK_LABELS, {"future_task": "Future task"}), patch.dict(
            ui_module.RUNTIME_FIT_LABELS, {"future_fit": "Future fit"}
        ), patch.object(
            ui_module,
            "MODEL_RECOMMENDATION_TASKS",
            (*ui_module.MODEL_RECOMMENDATION_TASKS, "future_task"),
        ):
            payload = ui_module.schema_payload()
        self.assertEqual(payload["model_task_labels"]["future_task"], "Future task")
        self.assertEqual(payload["runtime_fit_labels"]["future_fit"], "Future fit")
        self.assertIn("future_task", payload["model_recommendation_tasks"])
        json.dumps(payload)  # must stay JSON-serializable for _send_json

        # Drift guard: the embedded JS holds no duplicate map literals and
        # resolves both vocabularies from state.schema, keeping the existing
        # raw-key/unknown fallbacks and the status-keyed behavior gate.
        html = ui_module.HTML
        self.assertNotIn("const MODEL_TASK_LABELS = {", html)
        self.assertNotIn("const RUNTIME_FIT_LABELS = {", html)
        self.assertIn("state.schema.model_task_labels", html)
        self.assertIn("state.schema.runtime_fit_labels", html)
        self.assertIn("labels[task] || task", html)
        self.assertIn('fit.status || "unknown"', html)
        self.assertIn(
            "Object.prototype.hasOwnProperty.call(RUNTIME_FIT_BACKENDS, fit.status)",
            html,
        )

    def test_schema_endpoint_serves_label_maps(self) -> None:
        """The public schema route serializes both canonical label maps."""
        with patch.object(
            ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, None)
        ), patch.object(
            ui_module, "configured_removed_topic_env_vars", return_value=set()
        ), patch.object(
            ui_module, "list_presets", return_value={"path": "presets.yaml", "presets": []}
        ), patch.object(
            ui_module,
            "list_model_tuning_presets",
            return_value={"path": "model.yaml", "presets": []},
        ), patch.object(
            ui_module, "_source_summary", return_value={"total": 0}
        ), patch.object(ui_module, "_recipient_summary", return_value={"total": 0}):
            status, headers, body = self._invoke_get("/api/schema")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(payload["model_task_labels"], model_catalog.MODEL_TASK_LABELS)
        self.assertEqual(payload["runtime_fit_labels"], model_catalog.RUNTIME_FIT_LABELS)

    def test_schema_payload_preserves_ui_location(self) -> None:
        """The schema response carries ui_location on every knob (issue #115):
        the browser derives SURFACED_ENVS from schema registry records instead
        of a hardcoded list, so the metadata must survive the JSON-ready
        payload exactly as the registry defines it."""
        with patch.object(
            ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, None)
        ), patch.object(
            ui_module, "configured_removed_topic_env_vars", return_value=set()
        ), patch.object(
            ui_module, "list_presets", return_value={"path": "presets.yaml", "presets": []}
        ), patch.object(
            ui_module,
            "list_model_tuning_presets",
            return_value={"path": "model.yaml", "presets": []},
        ), patch.object(
            ui_module, "_source_summary", return_value={"total": 0}
        ), patch.object(ui_module, "_recipient_summary", return_value={"total": 0}):
            payload = ui_module.schema_payload()
        payload_locations = {knob["env"]: knob["ui_location"] for knob in payload["knobs"]}
        registry_locations = {
            knob["env"]: knob["ui_location"] for knob in build_knob_registry()
        }
        self.assertEqual(payload_locations, registry_locations)
        self.assertEqual(
            set(payload_locations.values()),
            {"run_setup", "advanced_panels", "advanced_raw"},
        )
        json.dumps(payload)  # must stay JSON-serializable for _send_json

    def test_schema_label_renderers_execute_in_node_dom_harness(self) -> None:
        """Execute the embedded catalog renderers against schema and search fixtures.

        The project has no browser test dependency, so this uses Node's runtime
        with a small DOM-shaped fixture. The JavaScript functions are extracted
        from HTML itself, rather than reimplemented in the test, so label
        rendering, escaping, fallbacks, and the raw status gate are exercised.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + _FAKE_DOM_ELEMENT_JS
            + r"""
const elements = {
  recommendationTask: new FakeElement("recommendationTask"),
  catalogCards: new FakeElement("catalogCards"),
  recommendationReadout: new FakeElement("recommendationReadout"),
  modelSearchResults: new FakeElement("modelSearchResults"),
  modelSearchQuery: new FakeElement("modelSearchQuery", "catalog"),
  modelSearchPipeline: new FakeElement("modelSearchPipeline", "text-generation"),
  modelSearchLimit: new FakeElement("modelSearchLimit", "10")
};
function $(id) { return elements[id] || null; }
function value(id) { return $(id) ? $(id).value : ""; }
// Mirrors the real currentControlValue when no matching control exists:
// fall back to state.schema.current_env (see the embedded implementation
// next to escapeHtml, which is not part of the extracted renderer blocks).
function currentControlValue(env) {
  return (state.schema && state.schema.current_env && state.schema.current_env[env]) || "";
}
// Minimal document stub: currentControlValue/renderModelBackendHint fall
// back to state.schema.current_env when no matching control exists, and
// useModelReference returns early without a NEWS_MODEL select element.
const document = {
  querySelector: () => null,
  getElementById: () => null,
  querySelectorAll: () => []
};
const state = { schema: null };
const searchResults = [
  { id: "owner/future", hf_url: "https://example.test/future", runtime_fit: { status: "future_fit", reason: "future reason" } },
  { id: "owner/unknown", hf_url: "https://example.test/unknown", runtime_fit: { status: "unknown_fit", reason: "unknown reason" } },
  { id: "owner/missing-status", hf_url: "https://example.test/missing-status", runtime_fit: { reason: "no status" } },
  { id: "owner/empty-status", hf_url: "https://example.test/empty-status", runtime_fit: { status: "", reason: "empty status" } },
  { id: "owner/gguf", hf_url: "https://example.test/gguf", runtime_fit: { status: "managed_llama_cpp", reason: "managed llama reason" } },
  { id: "owner/external", hf_url: "https://example.test/external", runtime_fit: { status: "external_only", reason: "external reason" } }
];
async function api(path) {
  assert(path.startsWith("/api/models/search?"), `unexpected API path: ${path}`);
  return { models: searchResults, error: null };
}
"""
        + js_function_block("function escapeHtml(text) {", "function formatDefault")
        + js_function_block("function modelTaskLabels() {", "function useModelReference")
        + js_function_block("function normalizedBackendValue(value) {", "function setControlValue")
        + js_function_block('function useModelReference(reference, requiredBackend = "") {', "function renderModelCatalogPanel")
        + js_function_block("function renderModelCatalogPanel() {", "function renderRecommendations")
        + js_function_block("function renderRecommendations(task) {", "async function searchHuggingFaceModels")
        + js_function_block("async function searchHuggingFaceModels() {", "async function comparePromptProfiles")
        + r"""
state.schema = {
  model_recommendation_tasks: ["future_task", "unknown_task"],
  model_task_labels: { future_task: "Future <task> & choice" },
  runtime_fit_labels: { future_fit: "Future <fit> & choice" },
  model_catalog: [],
  current_env: { NEWS_MODEL_BACKEND: "mlx-lm" }
};
renderModelCatalogPanel();
assert(elements.recommendationTask.optionText("future_task") === "Future <task> & choice", "known task label was not rendered");
assert(elements.recommendationTask.optionText("unknown_task") === "unknown_task", "unknown task did not fall back to its key");
assert(elements.recommendationTask.innerHTML.includes("Future &lt;task&gt; &amp; choice"), "task label was not escaped");

await searchHuggingFaceModels();
assert(elements.modelSearchResults.textContent.includes("Future <fit> & choice"), "known fit label was not rendered");
assert(elements.modelSearchResults.textContent.includes("unknown_fit"), "unknown fit did not fall back to its status");
assert(elements.modelSearchResults.innerHTML.includes("Future &lt;fit&gt; &amp; choice"), "fit label was not escaped");
assert(elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external"]').disabled, "external-only model was enabled");
const ggufButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/gguf"]');
// Drift guard (ship-review #146): with NEWS_MODEL_BACKEND=mlx-lm the managed
// llama.cpp hit is disabled with actionable guidance instead of silently
// switching backends.
assert(ggufButton && ggufButton.disabled, "managed_llama_cpp was enabled without NEWS_MODEL_BACKEND=llama.cpp");
assert(elements.modelSearchResults.innerHTML.includes(">Set NEWS_MODEL_BACKEND=llama.cpp to use<"), "mismatch label did not name the required backend");
// With a matching backend override the managed llama.cpp hit becomes usable
// and still maps to NEWS_MODEL_BACKEND=llama.cpp.
state.schema.current_env.NEWS_MODEL_BACKEND = "llama.cpp";
await searchHuggingFaceModels();
const ggufUsable = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/gguf"]');
assert(ggufUsable && !ggufUsable.disabled, "managed_llama_cpp model was not enabled with matching backend");
assert(elements.modelSearchResults.innerHTML.includes(">Use</button>"), "enabled llama.cpp hit did not show the Use label");
assert(ggufUsable.dataset.useHfBackend === "llama.cpp", "managed_llama_cpp did not map to NEWS_MODEL_BACKEND=llama.cpp");

// Partial/empty schema state remains usable and keeps raw fallbacks.
state.schema = { model_recommendation_tasks: ["raw_task"] };
renderModelCatalogPanel();
assert(elements.recommendationTask.optionText("raw_task") === "raw_task", "empty label map did not fall back");
state.schema = {};
await searchHuggingFaceModels();
assert(elements.modelSearchResults.textContent.includes("future_fit"), "empty fit map did not fall back");
assert(elements.modelSearchResults.textContent.includes("Fit: unknown — no status"), "missing fit status did not fall back to unknown");
assert(elements.modelSearchResults.textContent.includes("Fit: unknown — empty status"), "empty fit status did not fall back to unknown");
assert(elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external"]').disabled, "empty schema enabled external-only model");
""" )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=js,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_model_catalog_renderers_execute_in_node_dom_harness(self) -> None:
        """Execute catalog cards, recommendations, and search fallbacks.

        The source-contract checks pin the intended snippets, while this
        harness exercises the extracted production functions with DOM-shaped
        elements so template escaping, button bindings, backend-aware disable
        state, and asynchronous fallbacks cannot regress silently.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + _FAKE_DOM_ELEMENT_JS
            + r'''
class RichElement extends FakeElement {
  set innerHTML(value) {
    super.innerHTML = value;
    this._buttons = [];
    const pattern = /<button\b([^>]*)>([\s\S]*?)<\/button>/g;
    for (const match of String(value).matchAll(pattern)) {
      const attrs = match[1];
      const button = {
        dataset: {},
        disabled: /\sdisabled(?:\s|$)/.test(attrs),
        textContent: decodeEntities(match[2].replace(/<[^>]*>/g, "")),
        onclick: null
      };
      for (const attr of attrs.matchAll(/\b(data-[\w-]+)="([^"]*)"/g)) {
        const key = attr[1].slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
        button.dataset[key] = decodeEntities(attr[2]);
      }
      this._buttons.push(button);
    }
  }
  get innerHTML() { return super.innerHTML; }
  querySelector(selector) {
    const match = selector.match(/^button\[data-([a-z-]+)="([^"]*)"\]$/);
    if (!match) return null;
    const key = match[1].replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return (this._buttons || []).find(button => button.dataset[key] === decodeEntities(match[2])) || null;
  }
  querySelectorAll(selector) {
    const match = selector.match(/^\[data-([a-z-]+)\]$/);
    if (!match) return [];
    const key = match[1].replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return (this._buttons || []).filter(button => Object.prototype.hasOwnProperty.call(button.dataset, key));
  }
}
const elements = {
  recommendationTask: new RichElement("recommendationTask", "speed"),
  catalogCards: new RichElement("catalogCards"),
  recommendationReadout: new RichElement("recommendationReadout"),
  modelSearchResults: new RichElement("modelSearchResults"),
  modelSearchQuery: new FakeElement("modelSearchQuery", "catalog"),
  modelSearchPipeline: new FakeElement("modelSearchPipeline", "text-generation"),
  modelSearchLimit: new FakeElement("modelSearchLimit", "10")
};
const modelSelect = {
  value: "",
  options: [{ value: "" }],
  insertAdjacentHTML(_position, markup) {
    const match = markup.match(/<option value="([^"]*)">/);
    if (match) this.options.unshift({ value: decodeEntities(match[1]) });
  },
  dispatchEvent(_event) {}
};
const modelBackendHint = {
  textContent: "",
  _hidden: true,
  classList: {
    add(name) { if (name === "hidden") modelBackendHint._hidden = true; },
    remove(name) { if (name === "hidden") modelBackendHint._hidden = false; }
  }
};
function $(id) { return elements[id] || null; }
function value(id) { return $(id) ? $(id).value : ""; }
function currentControlValue(env) {
  if (env === "NEWS_MODEL") return modelSelect.value;
  return (state.schema && state.schema.current_env && state.schema.current_env[env]) || "";
}
const document = {
  querySelector: selector => selector === '[data-env="NEWS_MODEL"]' ? modelSelect : null,
  getElementById: id => id === "modelBackendHint" ? modelBackendHint : null,
  querySelectorAll: () => []
};
class Event { constructor(type) { this.type = type; } }
const state = { schema: null };
let apiResponse = { models: [], error: null };
let apiFailure = null;
let apiCalls = 0;
async function api(path) {
  apiCalls += 1;
  assert(path.includes("q=catalog"), `unexpected API path: ${path}`);
  if (apiFailure) throw new Error(apiFailure);
  return apiResponse;
}
'''
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block("function normalizedBackendValue(value) {", "function setControlValue")
            + js_function_block("function modelTaskLabels() {", "function useModelReference")
            + js_function_block("function knobByEnv(env) {", "function inputForKnob")
            + js_function_block('function useModelReference(reference, requiredBackend = "") {', "function renderModelCatalogPanel")
            + js_function_block("function renderModelCatalogPanel() {", "function renderRecommendations")
            + js_function_block("function renderRecommendations(task) {", "async function searchHuggingFaceModels")
            + js_function_block("async function searchHuggingFaceModels() {", "async function comparePromptProfiles")
            + r'''
state.schema = {
  model_recommendation_tasks: ["speed"],
  model_task_labels: { speed: "Speed" },
  model_catalog: [{
    alias: "safe-alias",
    name: "<name>",
    description: "<description>",
    backend: "mlx-lm",
    context_length: 8192,
    hf_url: "https://example.test/?a=1&b=<2>"
  }],
  model_recommendations: {
    speed: [{ alias: "recommended", name: "<recommended>", reason: "<reason>" }]
  },
  runtime_fit_labels: { managed_mlx_lm: "Managed <MLX>" },
  current_env: { NEWS_MODEL_BACKEND: "mlx-lm" }
};
renderModelCatalogPanel();
assert(elements.catalogCards.innerHTML.includes("&lt;name&gt;"), "catalog name was not escaped");
assert(elements.catalogCards.innerHTML.includes("&lt;description&gt;"), "catalog description was not escaped");
assert(elements.catalogCards.innerHTML.includes("?a=1&amp;b=&lt;2&gt;"), "catalog URL was not escaped");
const catalogButton = elements.catalogCards.querySelector('button[data-use-model="safe-alias"]');
assert(catalogButton && typeof catalogButton.onclick === "function", "catalog Use handler missing");
catalogButton.onclick();
assert(modelSelect.value === "safe-alias", "catalog button did not select its model");
const recommendationButton = elements.recommendationReadout.querySelector('button[data-use-model="recommended"]');
assert(recommendationButton && typeof recommendationButton.onclick === "function", "recommendation handler missing");
recommendationButton.onclick();
assert(modelSelect.value === "recommended", "recommendation button did not select its model");
renderRecommendations("");
assert(elements.recommendationReadout.textContent.includes("Pick a task"), "empty task fallback missing");
renderRecommendations("unknown");
assert(elements.recommendationReadout.textContent.includes("No verified curated model"), "empty recommendation fallback missing");

apiResponse = {
  models: [
    {
      id: "owner/<model>",
      hf_url: "https://example.test/?a=1&b=<2>",
      pipeline_tag: "text-generation",
      library_name: "mlx",
      downloads: 1,
      likes: 2,
      context_length: 4096,
      runtime_fit: { status: "managed_mlx_lm", reason: "<fit>" }
    },
    {
      id: "owner/external",
      hf_url: "https://example.test/external",
      pipeline_tag: "text-generation",
      library_name: "unknown",
      runtime_fit: { status: "external_only", reason: "external" }
    }
  ],
  error: null
};
await searchHuggingFaceModels();
assert(elements.modelSearchResults.innerHTML.includes("owner/&lt;model&gt;"), "search model ID was not escaped");
assert(elements.modelSearchResults.innerHTML.includes("&amp;b=&lt;2&gt;"), "search URL was not escaped");
assert(elements.modelSearchResults.innerHTML.includes("Managed &lt;MLX&gt;"), "search fit label was not escaped");
const managedButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/<model>"]');
const externalButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external"]');
assert(managedButton && !managedButton.disabled, "managed model was disabled");
assert(externalButton && externalButton.disabled, "external-only model was enabled");
managedButton.onclick();
assert(modelSelect.value === "owner/<model>", "search Use handler did not select its model");

const callsBeforeBlank = apiCalls;
elements.modelSearchQuery.value = "  \t ";
await searchHuggingFaceModels();
assert(apiCalls === callsBeforeBlank, "blank search query called the API");
assert(elements.modelSearchResults.textContent.includes("Enter a query"), "blank query fallback missing");
elements.modelSearchQuery.value = "catalog";
apiResponse = { models: [], error: "<API failure>" };
await searchHuggingFaceModels();
assert(elements.modelSearchResults.innerHTML.includes("&lt;API failure&gt;"), "API error was not escaped");
apiResponse = { models: [], error: null };
await searchHuggingFaceModels();
assert(elements.modelSearchResults.textContent.includes("No models found."), "empty results fallback missing");
apiFailure = "<network failure>";
await searchHuggingFaceModels();
assert(elements.modelSearchResults.innerHTML.includes("&lt;network failure&gt;"), "request failure fallback missing");

state.schema.current_env.NEWS_MODEL_BACKEND = "external";
apiFailure = null;
apiResponse = { models: [{
  id: "owner/external",
  hf_url: "https://example.test/external",
  runtime_fit: { status: "external_only", reason: "external" }
}], error: null };
await searchHuggingFaceModels();
const enabledExternalButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external"]');
assert(enabledExternalButton && !enabledExternalButton.disabled, "external backend did not enable external-only model");
enabledExternalButton.onclick();
assert(modelSelect.value === "owner/external", "external search handler did not select its model");

// Blank backend with a registry default gates by the fixed default (issue
// #169): external-only results stay disabled and the hint compares against
// the effective default until the backend control is explicit.
delete state.schema.current_env.NEWS_MODEL_BACKEND;
state.schema.knobs = [{ env: "NEWS_MODEL_BACKEND", default: "mlx-vlm", options: [], type: "select" }];
renderModelBackendHint("mlx-lm");
assert(modelBackendHint._hidden === false, "mismatch hint hidden with blank backend + fixed default");
assert(modelBackendHint.textContent === "This model needs NEWS_MODEL_BACKEND=mlx-lm", "hint text missing for effective default mismatch");
apiResponse = { models: [{ id: "owner/external2", hf_url: "https://example.test/external2", runtime_fit: { status: "external_only", reason: "external" } }], error: null };
await searchHuggingFaceModels();
const defaultGatedButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external2"]');
assert(defaultGatedButton && defaultGatedButton.disabled, "blank backend with fixed default enabled external-only model");
state.schema.current_env.NEWS_MODEL_BACKEND = "external";
renderModelBackendHint("external");
assert(modelBackendHint._hidden === true, "mismatch hint shown for matching effective backend");
await searchHuggingFaceModels();
const defaultEnabledButton = elements.modelSearchResults.querySelector('button[data-use-hf-model="owner/external2"]');
assert(defaultEnabledButton && !defaultEnabledButton.disabled, "explicit external did not enable external-only model");
'''
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        timeout_seconds = 30
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-"],
                input=js,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"Node harness timed out after {timeout_seconds}s: "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_prompt_template_editor_actions_execute_in_node_dom_harness(self) -> None:
        """Execute the Advanced full-template editors in a DOM-shaped Node harness.

        The source-text assertions elsewhere prove the markup and function
        strings exist, which is exactly what let the ID-only ``$`` helper
        receive CSS selector strings and silently short-circuit Validate,
        Restore, changed-env collection, and preset application (issue #227).
        This harness renders the real schema-driven editor markup through the
        real production functions and drives them: each Validate click must
        POST to the validate endpoint and update its status, Restore must
        reset the textareas and serialization state, an edited pair must reach
        the /api/presets body through the real savePresetEditor(), and the
        saved env must round-trip through the real setPromptTemplateEnv().
        Any console.error call fails the harness, so a null selector lookup
        cannot be hidden behind a swallowed error.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        tasks_json = json.dumps(prompt_templates.list_prompt_templates())
        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
// A selector failure (or any unexpected path) must fail the run instead of
// being swallowed into a console message.
console.error = (...args) => { throw new Error("console.error: " + args.join(" ")); };
"""
            + _FAKE_DOM_ELEMENT_JS
            + r"""

// ---- Fake DOM ------------------------------------------------------------
// getElementById only recognizes real IDs (promptTemplateEditors, preset
// fields, restoreAllPromptTemplatesBtn); a CSS selector string is NOT an ID,
// so the old $(`[data-template-...]`) calls resolve to null here exactly as
// they do in a browser.
// Parse the schema-driven markup renderPromptTemplateEditors() generates, so
// the harness exercises the real renderer (escaping, defaults, malformed
// flags) instead of hand-built controls.
const textareas = new Map();    // "system:<task>" / "user:<task>" -> element
const statusEls = new Map();    // "<task>" -> status element
const validateBtns = new Map(); // "<task>" -> Validate button
const restoreBtns = new Map();  // "<task>" -> Restore default button
function parseEditorMarkup(markup) {
  textareas.clear();
  statusEls.clear();
  validateBtns.clear();
  restoreBtns.clear();
  let match;
  const textareaRe = /<textarea data-template-(system|user)="([^"]*)"[^>]*>([\s\S]*?)<\/textarea>/g;
  while ((match = textareaRe.exec(markup)) !== null) {
    const role = match[1];
    const task = match[2];
    const el = new FakeElement();
    el.value = decodeEntities(match[3]);
    el.dataset[role === "system" ? "templateSystem" : "templateUser"] = task;
    textareas.set(role + ":" + task, el);
  }
  const statusRe = /<p class="prompt-template-status" data-template-status="([^"]*)">([\s\S]*?)<\/p>/g;
  while ((match = statusRe.exec(markup)) !== null) {
    const el = new FakeElement();
    el.innerHTML = match[2];
    statusEls.set(match[1], el);
  }
  const buttonRe = /<button type="button" class="prompt-template-(validate|restore)" data-task="([^"]*)">/g;
  while ((match = buttonRe.exec(markup)) !== null) {
    const el = new FakeElement();
    el.dataset.task = match[2];
    if (match[1] === "validate") validateBtns.set(match[2], el);
    else restoreBtns.set(match[2], el);
  }
}
const byId = {};
function registerElement(id, el) { byId[id] = el; return el; }
const editorContainer = registerElement("promptTemplateEditors", new FakeElement("promptTemplateEditors"));
// Re-parse the generated editor markup whenever the container is (re)rendered.
const baseInnerHTMLSetter = Object.getOwnPropertyDescriptor(FakeElement.prototype, "innerHTML").set;
Object.defineProperty(editorContainer, "innerHTML", {
  set(value) { baseInnerHTMLSetter.call(this, value); parseEditorMarkup(String(value)); },
  get() { return this._innerHTML; }
});
registerElement("restoreAllPromptTemplatesBtn", new FakeElement("restoreAllPromptTemplatesBtn"));
for (const id of ["preset_id", "preset_name", "preset_description", "preset_env"]) {
  registerElement(id, new FakeElement(id));
}
const document = {
  getElementById(id) {
    // Deliberately ID-only: a CSS selector string must not resolve here.
    return byId[id] || null;
  },
  querySelector(selector) {
    const m = /^\[data-template-(system|user|status)="([^"]*)"\]$/.exec(selector);
    if (!m) throw new Error(`unexpected querySelector: ${selector}`);
    if (m[1] === "system") return textareas.get("system:" + m[2]) || null;
    if (m[1] === "user") return textareas.get("user:" + m[2]) || null;
    return statusEls.get(m[2]) || null;
  },
  querySelectorAll(selector) {
    if (selector === "#promptTemplateEditors textarea") return [...textareas.values()];
    if (selector === "#promptTemplateEditors .prompt-template-validate") return [...validateBtns.values()];
    if (selector === "#promptTemplateEditors .prompt-template-restore") return [...restoreBtns.values()];
    throw new Error(`unexpected querySelectorAll: ${selector}`);
  }
};
function $(id) { return document.getElementById(id); }
function value(id) { const el = $(id); return el ? el.value : ""; }
// The editor's textareas are addressed by CSS selector; keep the lookups
// short instead of repeating the template literal at every call site.
const templateSys = task => document.querySelector(`[data-template-system="${task}"]`);
const templateUser = task => document.querySelector(`[data-template-user="${task}"]`);
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

// ---- API and preset-save stubs (only the network edge is stubbed) --------
const requests = [];
let savedPresetPayload = null;
let presetsReloaded = 0;
let rejectValidation = false;
async function api(path, options = {}) {
  requests.push({ path, method: options.method || "GET", body: options.body || "" });
  if (path === "/api/prompt-templates/validate") {
    const body = JSON.parse(options.body);
    if (rejectValidation && body.task === TASKS[0].task) {
      throw new Error("invalid template <test>");
    }
    return { valid: true, errors: {} };
  }
  if (path === "/api/presets") {
    savedPresetPayload = JSON.parse(options.body);
    return { ok: true };
  }
  throw new Error(`unexpected API path: ${path}`);
}
async function loadPresets() { presetsReloaded++; }
function renderPresetSummary() {}
function renderRunPresetDrawer() {}
const state = { schema: null, presets: [] };

// The five canonical task records, serialized from the production catalog so
// the harness stays schema-driven (same shape /api/schema serves).
const TASKS = """
            + tasks_json
            + r""";
"""
            + js_function_block("function escapeHtml(text) {", "function formatDefault")
            + js_function_block("// Advanced full-template editors (ADR 0015).", "function modelTaskLabels()")
            + js_function_block("function collectRunPresetEditor() {", "function prepRunPresetEditorFromCurrent")
            + js_function_block("function textToEnv(text) {", "function renderStats")
            + js_function_block("async function savePresetEditor() {", "async function renamePresetDisplayName")
            + js_function_block("function applyRunPreset(preset) {", "function resetAllOverrides")
            + r"""

// The preset application path also refreshes unrelated panels. Stub those
// renderers so this harness can exercise the real orchestration function.
function setKnobEnv(_env) {}
function renderModelTuningPanels() {}
function renderPromptProfilePanel() {}
function refreshModelKnobLinks() {}
async function previewQuietly(_scope) {}

// ---- Render ---------------------------------------------------------------
state.schema = { current_env: {}, prompt_templates: TASKS };
renderPromptTemplateEditors();
assert(textareas.size === TASKS.length * 2, "editor textareas were not rendered");
assert(validateBtns.size === TASKS.length, "Validate buttons were not rendered");
assert(restoreBtns.size === TASKS.length, "Restore buttons were not rendered");

// ---- Validate: each task POSTs its rendered pair and reports success ------
for (const t of TASKS) {
  validateBtns.get(t.task).onclick();
  await flush();
  const posts = requests.filter(
    r => r.path === "/api/prompt-templates/validate" && JSON.parse(r.body).task === t.task
  );
  assert(posts.length === 1, `${t.task}: expected exactly one validate request`);
  const body = JSON.parse(posts[0].body);
  assert(body.template.system === t.system, `${t.task}: validate did not send the rendered system text`);
  assert(body.template.user === t.user, `${t.task}: validate did not send the rendered user text`);
  const statusEl = statusEls.get(t.task);
  assert(statusEl.innerHTML.includes("Template is valid."), `${t.task}: status did not report valid`);
  assert(!statusEl.innerHTML.includes("bad"), `${t.task}: status reported an error`);
}

// ---- Validate rejection: the task-specific error status is escaped --------
rejectValidation = true;
const failureTask = TASKS[0];
statusEls.get(failureTask.task).innerHTML = '<span class="ok">Template is valid.</span>';
validateBtns.get(failureTask.task).onclick();
await flush();
assert(
  statusEls.get(failureTask.task).innerHTML.includes('class="bad"'),
  "validation failure was not shown"
);
assert(
  statusEls.get(failureTask.task).innerHTML.includes("invalid template &lt;test&gt;"),
  "validation failure was not escaped or surfaced"
);

// ---- Restore: edited pair returns to defaults and drops its override ------
const restoreTask = TASKS[0];
const sysEl = templateSys(restoreTask.task);
const userEl = templateUser(restoreTask.task);
sysEl.value = "custom system text";
userEl.value = "custom user text";
sysEl.fire("input");
assert(statusEls.get(restoreTask.task).innerHTML === "", "input did not clear the status");
restoreBtns.get(restoreTask.task).onclick();
assert(sysEl.value === restoreTask.system, "restore did not reset the system textarea");
assert(userEl.value === restoreTask.user, "restore did not reset the user textarea");
assert(statusEls.get(restoreTask.task).innerHTML === "", "restore did not clear the status");
const envAfterRestore = currentPromptTemplateEnv();
assert(envAfterRestore[restoreTask.env_var] === undefined, "restored default pair was serialized as an override");

// ---- Restore all defaults ------------------------------------------------
for (const t of TASKS) {
  const sys = templateSys(t.task);
  const usr = templateUser(t.task);
  sys.value = `edited system ${t.task}`;
  usr.value = `edited user ${t.task}`;
  sys.fire("input");
}
$("restoreAllPromptTemplatesBtn").onclick();
const envAll = currentPromptTemplateEnv();
assert(Object.keys(envAll).length === 0, "restore-all left overrides behind");
for (const t of TASKS) {
  assert(
    templateSys(t.task).value === t.system,
    `${t.task}: restore-all did not reset the system textarea`
  );
}

// ---- User-only edit: it marks the task dirty and serializes both values ----
const userOnlyTask = TASKS[3];
setPromptTemplateEnv({});
const userOnly = templateUser(userOnlyTask.task);
statusEls.get(userOnlyTask.task).innerHTML = '<span class="ok">Template is valid.</span>';
userOnly.value = "user-only edit";
userOnly.fire("input");
assert(statusEls.get(userOnlyTask.task).innerHTML === "", "User input did not clear the status");
const userOnlyEnv = currentPromptTemplateEnv();
assert(
  JSON.parse(userOnlyEnv[userOnlyTask.env_var]).system === userOnlyTask.system,
  "User-only edit did not retain the system default"
);
assert(
  JSON.parse(userOnlyEnv[userOnlyTask.env_var]).user === "user-only edit",
  "User-only edit was not serialized"
);

// ---- Edit + save preset: the real savePresetEditor() must carry the pair --
const editTask = TASKS[1];
const editedSystem = `Custom $editorial_instructions system for ${editTask.task}`;
const editedUser = `Custom user text for ${editTask.task}`;
const sysEdit = templateSys(editTask.task);
const usrEdit = templateUser(editTask.task);
sysEdit.value = editedSystem;
usrEdit.value = editedUser;
sysEdit.fire("input");
document.getElementById("preset_id").value = "editor-test-preset";
document.getElementById("preset_name").value = "Editor Test";
document.getElementById("preset_description").value = "";
document.getElementById("preset_env").value = "";
const envBefore = currentPromptTemplateEnv();
assert(
  envBefore[editTask.env_var] === JSON.stringify({ system: editedSystem, user: editedUser }),
  "edited pair was not serialized into the env"
);
await savePresetEditor();
assert(savedPresetPayload !== null, "preset save did not POST /api/presets");
assert(savedPresetPayload.id === "editor-test-preset", "preset id was not sent");
assert(presetsReloaded === 1, "preset save did not reload presets");
assert(
  savedPresetPayload.env[editTask.env_var] === envBefore[editTask.env_var],
  "edited template is missing from the preset env"
);

// ---- Apply round-trip: the real preset path restores the exact pair -------
for (const t of TASKS) {
  const sys = templateSys(t.task);
  sys.value = "stale text";
  sys.fire("input");
}
state.presets = [{ id: savedPresetPayload.id, env: savedPresetPayload.env }];
applyRunPreset(state.presets[0]);
assert(state.selectedRunPresetId === savedPresetPayload.id, "apply did not select the preset");
assert(templateSys(editTask.task).value === editedSystem, "apply did not restore the edited system text");
assert(templateUser(editTask.task).value === editedUser, "apply did not restore the edited user text");
const envAfter = currentPromptTemplateEnv();
assert(
  envAfter[editTask.env_var] === envBefore[editTask.env_var],
  "applied env did not round-trip the exact JSON value"
);

// ---- Malformed overrides stay visible and round-trip raw ------------------
const malformedTask = TASKS[2];
const malformedValues = [
  '{"system": "broken"',
  JSON.stringify({ system: "broken" })
];
for (const malformedRaw of malformedValues) {
  state.schema = {
    current_env: { [malformedTask.env_var]: malformedRaw },
    prompt_templates: TASKS
  };
  renderPromptTemplateEditors();
  assert(
    statusEls.get(malformedTask.task).innerHTML.includes("malformed"),
    "malformed override was not flagged"
  );
  assert(
    templateSys(malformedTask.task).value === malformedTask.system,
    "malformed override replaced the built-in defaults"
  );
  setPromptTemplateEnv({ [malformedTask.env_var]: malformedRaw });
  assert(
    templateSys(malformedTask.task).value === malformedTask.system,
    "applying malformed override changed the system default"
  );
  assert(
    templateUser(malformedTask.task).value === malformedTask.user,
    "applying malformed override changed the user default"
  );
  const envMalformed = currentPromptTemplateEnv();
  assert(
    envMalformed[malformedTask.env_var] === malformedRaw,
    "malformed raw override was not preserved for round-trip"
  );
}
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=js,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_clear_reset_and_profile_preview_errors_use_status_path(self) -> None:
        """Drive the production clear/reset/profile handlers in a Node DOM
        harness and prove rejected previews surface through the existing
        setStatus(..., "bad") header path (issue #116) while successful
        previews still update #previewPane.

        The production blocks for preview(), requestBody(), setStatus(),
        resetAllOverrides(), and wireEvents() are extracted from the embedded
        script rather than reimplemented, so a regression that drops the
        status catch, returns a rejected promise, or stops updating the pane
        on success fails here. Source assertions pin the named handlers to the
        previewWithStatus() helper and keep the unrelated previewQuietly()
        auto-refresh paths untouched.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            begin = html.index(start)
            return html[begin : html.index(end, begin)]

        status_pattern = 'previewWithStatus("run")'
        # Named clear/reset/profile handlers route previews through the status
        # helper...
        reset_block = js_function_block(
            "function resetAllOverrides() {", "function setKnobEnv(env) {"
        )
        self.assertIn(f"void {status_pattern};", reset_block)
        profile_block = js_function_block(
            '$("promptProfileSelect").onchange = () => {',
            '$("comparePromptProfileBtn").onclick',
        )
        self.assertEqual(profile_block.count(status_pattern), 2)
        # ...while unrelated preset auto-refresh keeps previewQuietly().
        preset_block = js_function_block(
            "function applyRunPreset(preset) {", "function resetAllOverrides() {"
        )
        self.assertIn('void previewQuietly("run")', preset_block)
        self.assertIn('async function previewQuietly(action="run") {', html)
        # ...and the helper itself surfaces failures through setStatus().
        preview_block = js_function_block(
            'async function preview(action="run") {', "function updateRunControls() {"
        )
        self.assertIn(
            'return preview(action).catch(err => setStatus(err.message, "bad"));',
            preview_block,
        )

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + _FAKE_DOM_ELEMENT_JS
            + r"""
// ---- Fake DOM ------------------------------------------------------------
// wireEvents() assigns handlers to every control, so the ID-only $ helper
// must resolve each of them (a null lookup would throw inside wireEvents).
const byId = {};
// Production code writes textContent on #status and #previewPane, but the
// shared FakeElement exposes a read-only textContent getter; subclass it so
// setStatus() and preview() behave exactly as in the browser.
class WritableTextElement extends FakeElement {
  set textContent(value) { this._innerHTML = String(value); }
  get textContent() { return decodeEntities(this._innerHTML.replace(/<[^>]*>/g, "")); }
}
function registerElement(id) { return byId[id] || (byId[id] = new FakeElement(id)); }
function registerWritable(id) { return byId[id] || (byId[id] = new WritableTextElement(id)); }
for (const id of [
  "previewBtn", "runBtn", "utilityPreviewBtn", "utilityRunBtn", "stopBtn",
  "openRunPresetDrawerBtn", "savePresetBtn", "closeRunPresetDialogBtn",
  "newPresetBtn", "reloadPresetsBtn", "applyPresetBtn", "renamePresetBtn",
  "savePresetEditorBtn", "deletePresetBtn", "knobSearch", "clearKnobsBtn",
  "resetDefaultsBtn", "promptProfileSelect", "restorePromptProfileBtn",
  "comparePromptProfileBtn", "sourceSearch", "reloadSourcesBtn",
  "newSourceBtn", "saveSourceBtn", "deleteSourceBtn", "reloadRecipientsBtn",
  "newRecipientBtn", "saveRecipientBtn", "deleteRecipientBtn",
  "newModelTuningPresetBtn", "reloadModelTuningPresetsBtn",
  "saveModelTuningPresetBtn", "deleteModelTuningPresetBtn", "actionSelect",
  "sourceOptions"
]) registerElement(id);
registerWritable("status");
registerWritable("previewPane");
const profileEnvInput = new FakeElement("profileEnv");
function $(id) { return byId[id] || null; }
function value(id) { const el = $(id); return el ? el.value : ""; }
const document = {
  getElementById(id) { return byId[id] || null; },
  querySelector(selector) {
    // The restore-profile handler only clears the NEWS_PROMPT_PROFILE control.
    if (selector === '[data-env="NEWS_PROMPT_PROFILE"]') return profileEnvInput;
    return null;
  },
  querySelectorAll(_selector) {
    // No env knobs or override editors in this harness.
    return [];
  }
};
const state = { schema: null, selectedRunPresetId: "" };
const promptTemplateRaw = {};
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

// ---- Non-preview reset/profile dependencies are stubbed -------------------
function renderPresetSummary() {}
function renderModelTuningPanels() {}
function renderPromptProfilePanel() {}
function refreshModelKnobLinks() {}
function restorePromptTemplateTask() {}
function collectEnv() { return {}; }
function collectOptions() { return {}; }
// wireEvents() reads these identifiers eagerly while assigning handlers;
// their bodies are never invoked by this harness, so no-ops are sufficient.
function closeRunPresetDialog() {}
function renderAdvancedKnobs() {}
function loadPresets() {}
function loadSources() {}
function renderSources() {}
function loadRecipients() {}
// wireEvents() enumerates TASK_CONFIG at wiring time; this harness never
// invokes the model/tuning handlers, so the empty catalog is sufficient.
const TASK_CONFIG = {};

// ---- Production blocks under test -----------------------------------------
"""
            + js_function_block('function setStatus(text, cls="muted") {', "function showTab(id) {")
            + js_function_block('function requestBody(action="run") {', "function envToText(env) {")
            + js_function_block('async function preview(action="run") {', "function updateRunControls() {")
            + js_function_block("function resetAllOverrides() {", "function setKnobEnv(env) {")
            + js_function_block("function wireEvents() {", "function applySelectedPresetFromState() {")
            + r"""
// ---- API stub: only the network edge is stubbed ----------------------------
let previewCount = 0;
let rejectPreview = false;
async function api(path, options = {}) {
  assert(path === "/api/preview", `unexpected API path: ${path}`);
  previewCount++;
  if (rejectPreview) throw new Error("preview failed");
  return { command_text: "fresh preview", runtime_error: null };
}

// ---- Failure path: rejected previews surface through the status bar --------
wireEvents();
assert($("clearKnobsBtn").onclick === resetAllOverrides, "Clear overrides no longer binds the shared reset handler");
assert($("resetDefaultsBtn").onclick === resetAllOverrides, "Reset defaults no longer binds the shared reset handler");

function assertFailedPreview(label) {
  assert($("status").textContent === "preview failed", `${label} did not surface the preview error`);
  assert($("status").className === "bad", `${label} did not mark the status bad`);
}

rejectPreview = true;
$("clearKnobsBtn").onclick();
await flush();
assertFailedPreview("Clear overrides");
$("resetDefaultsBtn").onclick();
await flush();
assertFailedPreview("Reset defaults");
$("promptProfileSelect").onchange();
await flush();
assertFailedPreview("Profile change");
profileEnvInput.value = "custom-profile";
$("restorePromptProfileBtn").onclick();
await flush();
assert(profileEnvInput.value === "", "Profile restore did not clear the profile control");
assertFailedPreview("Profile restore");
assert(previewCount === 4, `expected 4 failed preview requests, got ${previewCount}`);

// ---- Success path: the pane still updates and no error is written ---------
rejectPreview = false;
$("previewPane").textContent = "stale preview";
$("clearKnobsBtn").onclick();
await flush();
$("resetDefaultsBtn").onclick();
await flush();
$("promptProfileSelect").onchange();
await flush();
$("restorePromptProfileBtn").onclick();
await flush();
assert($("previewPane").textContent === "fresh preview", "successful preview did not update the pane");
assert(previewCount === 8, `expected 8 preview requests total, got ${previewCount}`);
assert($("status").textContent === "preview failed", "successful preview overwrote the last error status");
assert($("status").className === "bad", "successful preview cleared the bad status");
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=js,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
    def test_wire_events_tuning_button_lookups_execute_in_node_dom_harness(self) -> None:
        """Execute the production wireEvents() loop in a DOM-shaped Node harness.

        wireEvents() must attach Save/Rename/Delete tuning handlers for every
        task whose controls are present and tolerate partial control trees
        (issue #117): a missing tuning button must not abort wiring of the
        same task's other buttons or any later task. TASK_CONFIG, the $()
        helper, and wireEvents() are extracted from HTML itself, not
        reimplemented here, so the harness exercises the exact production
        loop and its closures.
        """
        html = ui_module.HTML

        def js_function_block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        js = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
"""
            + _FAKE_DOM_ELEMENT_JS
            + r"""
// ---- Fake DOM ------------------------------------------------------------
// Register every static ID looked up directly by wireEvents() so the harness
// can vary only the per-task tuning buttons without unrelated null hits.
const STATIC_IDS = [
  "previewBtn", "runBtn", "utilityPreviewBtn", "utilityRunBtn", "stopBtn",
  "openRunPresetDrawerBtn", "savePresetBtn", "closeRunPresetDialogBtn",
  "newPresetBtn", "reloadPresetsBtn", "applyPresetBtn", "renamePresetBtn",
  "savePresetEditorBtn", "deletePresetBtn", "knobSearch", "clearKnobsBtn",
  "resetDefaultsBtn", "promptProfileSelect", "restorePromptProfileBtn",
  "comparePromptProfileBtn", "sourceSearch", "reloadSourcesBtn",
  "newSourceBtn", "saveSourceBtn", "deleteSourceBtn", "reloadRecipientsBtn",
  "newRecipientBtn", "saveRecipientBtn", "deleteRecipientBtn",
  "newModelTuningPresetBtn", "reloadModelTuningPresetsBtn",
  "saveModelTuningPresetBtn", "deleteModelTuningPresetBtn",
  "actionSelect", "sourceOptions"
];
const selectIds = meta => [meta.modelSelectId, meta.presetSelectId];
const buttonIds = meta => [meta.saveButtonId, meta.renameButtonId, meta.deleteButtonId];
let elements = {};
function makeElements(omitTuningButtonId) {
  const map = {};
  for (const id of STATIC_IDS) map[id] = new FakeElement(id);
  for (const meta of Object.values(TASK_CONFIG)) {
    map[meta.modelSelectId] = new FakeElement(meta.modelSelectId);
    map[meta.presetSelectId] = new FakeElement(meta.presetSelectId);
    for (const id of buttonIds(meta)) {
      if (id !== omitTuningButtonId) map[id] = new FakeElement(id);
    }
  }
  elements = map;
}
const document = { getElementById(id) { return elements[id] || null; } };
// Stubs used only when a handler is clicked; handler installation itself is
// the behavior under test. Bare function references in wireEvents() are
// evaluated at wiring time, so each must exist in harness scope.
const tuningCalls = [];
async function saveModelTuningPreset(runtimeKey) { tuningCalls.push(["save", runtimeKey]); }
async function renameModelTuningPreset(runtimeKey) { tuningCalls.push(["rename", runtimeKey]); }
async function deleteModelTuningPreset(runtimeKey) { tuningCalls.push(["delete", runtimeKey]); }
function setStatus(_message, _kind) {}
function closeRunPresetDialog() {}
function loadPresets() {}
function renderAdvancedKnobs() {}
function resetAllOverrides() {}
function renderSources() {}
function loadSources() {}
function loadRecipients() {}
"""
            + js_function_block(
                "    function $(id) { return document.getElementById(id); }",
                "    function value(id)",
            )
            + js_function_block("    const TASK_CONFIG = {", "    const state = {")
            + js_function_block(
                "    function wireEvents() {",
                "    function applySelectedPresetFromState",
            )
            + r"""
// ---- Complete control tree ------------------------------------------------
makeElements(null);
wireEvents();
for (const meta of Object.values(TASK_CONFIG)) {
  for (const id of selectIds(meta)) {
    assert(typeof elements[id].onchange === "function", `${id} did not receive its onchange handler`);
  }
  for (const id of buttonIds(meta)) {
    assert(typeof elements[id].onclick === "function", `${id} did not receive its onclick handler`);
  }
}

// Every tuning closure must preserve both its operation and task-specific runtimeKey.
for (const meta of Object.values(TASK_CONFIG)) {
  for (const [id, operation] of [
    [meta.saveButtonId, "save"],
    [meta.renameButtonId, "rename"],
    [meta.deleteButtonId, "delete"],
  ]) {
    await elements[id].onclick();
    const actual = tuningCalls.pop();
    const expected = [operation, meta.runtimeKey];
    assert(
      JSON.stringify(actual) === JSON.stringify(expected),
      `${id} invoked ${JSON.stringify(actual)} instead of ${operation}/${meta.runtimeKey}`
    );
  }
}
assert(tuningCalls.length === 0, "tuning callbacks left unexpected calls");
// ---- Partial control trees -------------------------------------------------
for (const absentId of ["article_tuning_save", "article_tuning_rename", "article_tuning_delete"]) {
  makeElements(absentId);
  wireEvents(); // must complete without throwing
  assert(elements[absentId] === undefined, `${absentId} should stay absent in the partial fixture`);
  const article = TASK_CONFIG.article_summary;
  for (const id of selectIds(article)) {
    assert(typeof elements[id].onchange === "function", `${id} lost its handler when ${absentId} was absent`);
  }
  for (const id of buttonIds(article)) {
    if (id === absentId) continue;
    assert(typeof elements[id].onclick === "function", `${id} lost its handler when ${absentId} was absent`);
  }
  // Later tasks must still be wired: an abort at the missing button is
  // observable because it sits in the first task entry of the loop.
  for (const meta of Object.values(TASK_CONFIG).slice(1)) {
    for (const id of [...selectIds(meta), ...buttonIds(meta)]) {
      const el = elements[id];
      assert(
        typeof el.onclick === "function" || typeof el.onchange === "function",
        `${id} was not wired when ${absentId} was absent`
      );
    }
  }
}
"""
        )
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the embedded UI renderer harness")
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=js,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_schema_payload_includes_custom_catalog_entries(self) -> None:
        """The existing schema payload is the integration surface for merged
        catalog data: custom aliases appear in catalog cards and selector
        options with no new endpoint (issue #90)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "custom_catalog.yaml"
            catalog_path.write_text(
                "models:\n"
                "  smoke-model:\n"
                "    reference: mlx-community/smoke-model\n"
                "    name: Smoke Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: mlx-community/smoke-model\n"
                "    description: Offline smoke entry\n"
                "    task_notes:\n"
                "      speed: Overlay-specific speed recommendation.\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None), patch.object(
                ui_module, "_runtime_snapshot", return_value=({"runtime": "ok"}, None)
            ), patch.object(
                ui_module, "configured_removed_topic_env_vars", return_value=set()
            ), patch.object(
                ui_module, "list_presets", return_value={"path": "presets.yaml", "presets": []}
            ), patch.object(
                ui_module,
                "list_model_tuning_presets",
                return_value={"path": "model.yaml", "presets": []},
            ), patch.object(
                ui_module, "_source_summary", return_value={"total": 0}
            ), patch.object(ui_module, "_recipient_summary", return_value={"total": 0}):
                payload = ui_module.schema_payload()
                # Recommendations come from the same merged snapshot and
                # exactly match the authoritative helper (issue #80).
                self.assertEqual(
                    payload["model_recommendations"],
                    {
                        task: model_catalog.recommend_models(task)
                        for task in model_catalog.MODEL_RECOMMENDATION_TASKS
                    },
                )
                self.assertEqual(payload["model_recommendations"]["translation"], [])
                speed_picks = payload["model_recommendations"]["speed"]
                self.assertEqual(
                    [pick["alias"] for pick in speed_picks],
                    ["gemma-e2b-tiny", "smoke-model", model_catalog.DEFAULT_CATALOG_MODEL_ALIAS],
                )
                self.assertEqual(
                    speed_picks[1]["reason"],
                    "Overlay-specific speed recommendation.",
                )

        self.assertEqual(
            [entry["alias"] for entry in payload["model_catalog"]],
            [
                "gemma-4-12b-it-4bit",
                "gemma-e2b-tiny",
                "qwythos-9b-4bit",
                "qwythos-9b-8bit",
                "smoke-model",
            ],
        )
        self.assertEqual(
            {entry["alias"]: entry["backend"] for entry in payload["model_catalog"]},
            {
                "gemma-4-12b-it-4bit": "mlx-vlm",
                "gemma-e2b-tiny": "mlx-lm",
                "qwythos-9b-4bit": "llama.cpp",
                "qwythos-9b-8bit": "llama.cpp",
                "smoke-model": "mlx-lm",
            },
        )
        model_knob = next(knob for knob in payload["knobs"] if knob["env"] == "NEWS_MODEL")
        self.assertIn("smoke-model", model_knob["options"])
        self.assertEqual(
            model_knob["option_links"]["smoke-model"]["page"],
            "https://huggingface.co/mlx-community/smoke-model",
        )
        task_knob = next(knob for knob in payload["knobs"] if knob["env"] == "NEWS_MODEL_STORY_DRAFTING")
        self.assertIn("smoke-model", task_knob["options"])

    def test_schema_malformed_catalog_uses_error_envelope(self) -> None:
        """A malformed catalog surfaces through the existing /api/schema
        error JSON (400) instead of a traceback or a silent empty catalog."""
        with patch.object(
            ui_module,
            "list_model_catalog",
            side_effect=ValueError("/tmp/bad_catalog.yaml must define models as a mapping."),
        ):
            status, _, body = self._invoke_get("/api/schema")
        self.assertEqual(status, 400)
        self.assertIn("must define models as a mapping", json.loads(body)["error"])

    def test_schema_recommendation_failure_uses_error_envelope(self) -> None:
        with patch.object(ui_module, "list_model_catalog", return_value=[]), patch.object(
            ui_module,
            "recommend_models",
            side_effect=ValueError("recommendation catalog failure"),
        ):
            status, _, body = self._invoke_get("/api/schema")

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "recommendation catalog failure")
    def test_schema_real_malformed_catalog_uses_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "bad_catalog.yaml"
            catalog_path.write_text("models: [\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                status, _, body = self._invoke_get("/api/schema")

        self.assertEqual(status, 400)
        self.assertIn(str(catalog_path), json.loads(body)["error"])
        self.assertIn("Could not load model catalog", json.loads(body)["error"])

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
        self.assertNotIn('textContent += `\\n[ui] ', html)
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

    @unittest.skipUnless(shutil.which("node"), "node runtime required for model tuning editor behavior tests")
    def test_model_tuning_editor_behavior(self) -> None:
        html = ui_module.HTML
        start = html.index("    function renderModelTuningEditor()")
        end = html.index("    function confirmAction", start)
        editor_source = html[start:end]
        script = f"""
import assert from "node:assert/strict";

const fieldIds = [
  "modelTuningPresetTable",
  "modelTuningPresetError",
  "modelTuningPresetId",
  "modelTuningPresetName",
  "modelTuningPresetDescription",
  "modelTuningPresetModel",
  "modelTuningPresetTask",
  "modelTuningPresetTuning",
];
const elements = new Map(fieldIds.map(id => [id, {{ value: "", textContent: "", innerHTML: "", dataset: {{}} }}]));
const table = elements.get("modelTuningPresetTable");
let renderedRows = [];
const serverPresets = [
  {{
    id: "concise",
    name: "Concise",
    description: "Short drafting output",
    model: "model-a",
    task: "story_drafting",
    tuning: {{ temperature: 0.2 }},
  }},
];
const state = {{
  modelTuningPresets: structuredClone(serverPresets),
  selectedModelTuningPresetId: "",
}};
const requests = [];
const statuses = [];
let failSave = false;
let failReload = false;
const clone = value => JSON.parse(JSON.stringify(value));
function $(id) {{
  const element = elements.get(id);
  if (!element) throw new Error(`Unexpected element: ${{id}}`);
  return element;
}}
function value(id) {{ return $(id).value; }}
function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, char => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }})[char]);
}}
function renderModelTuningPanels() {{}}
function setStatus(text, cls) {{ statuses.push({{ text, cls }}); }}
globalThis.document = {{
  querySelectorAll(selector) {{
    assert.equal(selector, "#modelTuningPresetTable tr[data-id]");
    renderedRows = state.modelTuningPresets.map(preset => ({{ dataset: {{ id: preset.id }}, onclick: null }}));
    return renderedRows;
  }},
}};
async function api(path, options = {{}}) {{
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({{ path, method, body }});
  if (method === "GET") {{
    if (failReload) throw new Error("reload unavailable");
    return {{ presets: clone(serverPresets) }};
  }}
  if (method === "POST" || method === "PATCH") {{
    if (failSave) throw new Error("save unavailable");
    const index = serverPresets.findIndex(preset => preset.id === body.id);
    if (method === "POST") assert.equal(index, -1);
    if (index === -1) serverPresets.push(clone(body));
    else serverPresets[index] = clone(body);
    return {{ preset: clone(body) }};
  }}
  if (method === "DELETE") {{
    const id = new URL(`http://localhost${{path}}`).searchParams.get("id");
    const index = serverPresets.findIndex(preset => preset.id === id);
    assert.notEqual(index, -1);
    serverPresets.splice(index, 1);
    return {{ deleted: id }};
  }}
  throw new Error(`Unexpected method: ${{method}}`);
}}
async function loadModelTuningPresets() {{
  const data = await api("/api/model-tuning-presets");
  state.modelTuningPresets = data.presets || [];
  renderModelTuningPanels();
  renderModelTuningEditor();
}}
function confirmAction() {{ return Promise.resolve(true); }}

{editor_source}

renderModelTuningEditor();
assert.equal(renderedRows.length, 1);
renderedRows[0].onclick();
assert.equal($("modelTuningPresetId").value, "concise");
assert.equal($("modelTuningPresetName").value, "Concise");
assert.equal(state.selectedModelTuningPresetId, "concise");
assert.ok(table.innerHTML.includes('class="selected"'));

$("modelTuningPresetId").value = "new-preset";
$("modelTuningPresetName").value = "New preset";
$("modelTuningPresetDescription").value = "Created in the editor";
$("modelTuningPresetModel").value = "model-b";
$("modelTuningPresetTask").value = "title_generation";
$("modelTuningPresetTuning").value = '{{"temperature":0.4,"max_tokens":1200}}';
await saveModelTuningEditor();
const createRequest = requests.find(request => request.method === "POST");
assert.deepEqual(createRequest.body, {{
  id: "new-preset",
  name: "New preset",
  description: "Created in the editor",
  model: "model-b",
  task: "title_generation",
  tuning: {{ temperature: 0.4, max_tokens: 1200 }},
}});
assert.equal(state.selectedModelTuningPresetId, "new-preset");
assert.equal($("modelTuningPresetError").textContent, "");

$("modelTuningPresetName").value = "Edited preset";
$("modelTuningPresetTuning").value = '{{"temperature":0.6}}';
await saveModelTuningEditor();
const patchRequests = requests.filter(request => request.method === "PATCH");
assert.equal(patchRequests.length, 1);
assert.equal(patchRequests[0].body.name, "Edited preset");
assert.deepEqual(patchRequests[0].body.tuning, {{ temperature: 0.6 }});

const requestCountBeforeInvalidJson = requests.length;
$("modelTuningPresetTuning").value = "{{";
await saveModelTuningEditor();
assert.equal(requests.length, requestCountBeforeInvalidJson);
assert.match($("modelTuningPresetError").textContent, /^Tuning JSON is not valid:/);
assert.equal($("modelTuningPresetTuning").value, "{{");

$("modelTuningPresetTuning").value = "[1, 2]";
await saveModelTuningEditor();
assert.equal(requests.length, requestCountBeforeInvalidJson);
assert.equal($("modelTuningPresetError").textContent, "Tuning must be a JSON object (mapping).");
assert.equal($("modelTuningPresetTuning").value, "[1, 2]");

failSave = true;
$("modelTuningPresetName").value = "Keep this edit";
$("modelTuningPresetTuning").value = '{{"temperature":0.7}}';
await saveModelTuningEditor();
assert.equal($("modelTuningPresetError").textContent, "save unavailable");
assert.equal($("modelTuningPresetName").value, "Keep this edit");
assert.equal($("modelTuningPresetTuning").value, '{{"temperature":0.7}}');
failSave = false;

serverPresets.length = 0;
await reloadModelTuningPresets();
assert.equal(state.selectedModelTuningPresetId, "");
assert.equal($("modelTuningPresetId").value, "");
assert.equal($("modelTuningPresetTuning").value, "{{}}");
assert.equal(table.innerHTML.includes('data-id="new-preset"'), false);

serverPresets.push({{ id: "remove-me", name: "Remove me", tuning: {{}} }});
await loadModelTuningPresets();
editModelTuningPreset("remove-me");
await deleteModelTuningEditor();
assert.ok(requests.some(request => request.method === "DELETE" && request.path.includes("remove-me")));
assert.equal(state.modelTuningPresets.length, 0);
assert.equal($("modelTuningPresetId").value, "");
assert.equal($("modelTuningPresetTuning").value, "{{}}");
assert.ok(statuses.some(status => status.text === "Model tuning preset remove-me deleted."));
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

    # -- Daily Automation schedule API and tab ------------------------------

    def test_schedule_api_routes_success_and_controlled_failures(self) -> None:
        safe_payload = {
            "supported": True,
            "enabled": True,
            "time": "06:45",
            "preset_id": "default",
            "delivery_mode": "owner",
            "launchd_status": "loaded",
            "next_run_label": "06:45 (local time, once daily)",
            "last_run": {"status": "completed", "run_id": "run-1"},
            "state_path": "/tmp/daily_schedule.json",
            "plist_path": "/tmp/job.plist",
            "error": None,
        }

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

        with patch.object(ui_module, "schedule_payload", return_value=safe_payload), patch.object(
            ui_module, "update_schedule", return_value=safe_payload
        ) as update, patch.object(
            ui_module, "disable_schedule_payload", return_value={**safe_payload, "enabled": False}
        ) as disable:
            status, _, body = invoke("do_GET", "/api/schedule")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), safe_payload)
            # The GET payload never leaks stored env maps, plist XML, launchctl
            # output, or credentials.
            serialized = json.dumps(safe_payload)
            for forbidden in ("base_env", "overrides", "launchctl", "stderr", "smtp", "secret", "api_key"):
                self.assertNotIn(forbidden, serialized)

            status, _, body = invoke(
                "do_PUT",
                "/api/schedule",
                body=json.dumps({"time": "06:45", "preset_id": "default", "delivery_mode": "owner"}),
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), safe_payload)
            update.assert_called_once_with(
                {"time": "06:45", "preset_id": "default", "delivery_mode": "owner"}
            )

            with patch.object(
                ui_module,
                "update_schedule",
                side_effect=ValueError("Schedule time is required (HH:MM, 24-hour local time)."),
            ):
                status, _, body = invoke("do_PUT", "/api/schedule", body=json.dumps({"time": ""}))
            self.assertEqual(status, 400)
            self.assertIn("Schedule time is required", json.loads(body)["error"])

            status, _, body = invoke("do_DELETE", "/api/schedule")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["enabled"])
            disable.assert_called_once_with()

            with patch.object(
                ui_module, "disable_schedule_payload", side_effect=RuntimeError("launchctl boom")
            ):
                status, _, body = invoke("do_DELETE", "/api/schedule")
            self.assertEqual(status, 400)
            self.assertIn("launchctl boom", json.loads(body)["error"])

    def test_schedule_payload_is_bounded_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                "NEWS_SCHEDULE_STATE": str(Path(td) / "daily_schedule.json"),
                "NEWS_SCHEDULE_PLIST": str(Path(td) / "job.plist"),
                "NEWS_SCHEDULE_LOCK": str(Path(td) / "lock"),
                "NEWS_SCHEDULE_LOG_DIR": str(Path(td) / "logs"),
                "NEWS_SMTP_PASSWORD": "hunter2",
            },
            clear=False,
        ):
            payload = ui_module.schedule_payload()
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["error"], None)
        for forbidden in ("base_env", "overrides", "env", "launchctl", "stderr", "hunter2"):
            self.assertNotIn(forbidden, json.dumps(payload))

    def test_schedule_tab_html_contracts(self) -> None:
        html = ui_module.HTML
        # Tab, section, and form controls exist.
        self.assertIn('["schedule", "Schedule", "clock"]', html)
        self.assertIn('<section id="schedule" class="view">', html)
        self.assertIn('id="scheduleMount"', html)
        self.assertIn('id="schedule_time" type="time"', html)
        self.assertIn('id="schedule_preset"', html)
        self.assertIn('id="schedule_delivery_mode"', html)
        self.assertIn('id="enableScheduleBtn"', html)
        self.assertIn('id="disableScheduleBtn"', html)
        self.assertIn('id="openReviewFromScheduleBtn"', html)
        # API wiring: GET on load, PUT to save, DELETE to disable.
        self.assertIn('await api("/api/schedule")', html)
        self.assertIn('await api("/api/schedule", { method: "PUT", body: JSON.stringify(body) })', html)
        self.assertIn('await api("/api/schedule", { method: "DELETE" })', html)
        # Boot refresh loads the durable schedule state after UI restart.
        boot = html.split("async function init()")[1].split("init().catch")[0]
        self.assertIn("await loadSchedule();", boot)
        # User-controlled status/errors are escaped through existing helpers.
        self.assertIn('escapeHtml(last.status || "never")', html)
        self.assertIn("escapeHtml(last.error_message)", html)
        self.assertIn("escapeHtml(error)", html)
        self.assertIn("escapeHtml(s.next_run_label", html)
        # Unsupported platforms disable the controls instead of pretending.
        self.assertIn('${!supported ? `<p class="bad"', html)
        self.assertIn('${canConfigure ? "" : "disabled"}', html)
        self.assertIn('${canDisable ? "" : "disabled"}', html)
        # Owner-first default and explicit opt-in vocabulary.
        self.assertIn("Owner only (default)", html)
        self.assertIn("Configured recipients", html)
        self.assertIn("no weekly recurrence or cron expressions", html)
        self.assertIn("never stored in schedule state or the launchd plist", html)
        # The tab must not imply the UI process performs the daily run.
        self.assertIn("macOS launchd starts", html)
        # Report Review remains the report surface; the tab links to it.
        self.assertIn('$("openReviewFromScheduleBtn").onclick = () => { showTab("review"); refreshReviewData(); };', html)


if __name__ == "__main__":
    unittest.main()
