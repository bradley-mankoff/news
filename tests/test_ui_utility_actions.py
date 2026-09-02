from __future__ import annotations

import os
import shutil
import subprocess
import unittest

from news_pipeline import ui as ui_module


def _find_node() -> str | None:
    """Return a Node.js binary that can execute stdin module harnesses."""
    for candidate in (
        "/opt/homebrew/opt/node/bin/node",
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("node")
    if found and os.path.basename(os.path.realpath(found)) != "bun":
        return found
    return None


class UtilityActionUITests(unittest.TestCase):
    def _run_node(self, script: str) -> None:
        node = _find_node()
        if node is None:
            self.skipTest("Node.js is required for the embedded UI harness")
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_renderer_preserves_order_selection_fallback_and_escaping(self) -> None:
        html = ui_module.HTML

        def block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        utility_helpers = html[
            html.index("    const SOURCE_UTILITY_ACTIONS") : html.index(
                "    function decorateUtilityHints"
            )
        ]
        script = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const decodeEntities = text => String(text)
  .replaceAll("&lt;", "<")
  .replaceAll("&gt;", ">")
  .replaceAll("&quot;", '"')
  .replaceAll("&amp;", "&");
const byId = {};
let utilityButtons = [];
function makeClassList(initial = []) {
  const classes = new Set(initial);
  return {
    contains(name) { return classes.has(name); },
    toggle(name, force) {
      const next = force === undefined ? !classes.has(name) : Boolean(force);
      if (next) classes.add(name); else classes.delete(name);
      return next;
    }
  };
}
class FakeElement {
  constructor(id = "", initialClasses = []) {
    this.id = id;
    this.value = "";
    this.dataset = {};
    this.attributes = {};
    this.classList = makeClassList(initialClasses);
    this.disabled = false;
    this.onclick = null;
    this.onchange = null;
    this.oninput = null;
    this.onkeydown = null;
    this._innerHTML = "";
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    if (this.id === "runSetupMount") parseMarkup(this._innerHTML);
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
}
function parseMarkup(markup) {
  utilityButtons = [];
  for (const match of markup.matchAll(/\bid="([^"]+)"/g)) {
    if (!byId[match[1]]) byId[match[1]] = new FakeElement(match[1]);
  }
  const buttonPattern = /<button\b([^>]*?)data-utility-action="([^"]+)"([^>]*)>/g;
  for (const match of markup.matchAll(buttonPattern)) {
    const attrs = `${match[1]}${match[3]}`;
    const classes = (attrs.match(/\bclass="([^"]*)"/)?.[1] || "").split(/\s+/).filter(Boolean);
    const button = new FakeElement("", classes);
    button.dataset.utilityAction = decodeEntities(match[2]);
    for (const name of ["aria-pressed", "aria-label", "data-action", "value"]) {
      const attr = attrs.match(new RegExp(`\\b${name}="([^"]*)"`));
      if (attr) button.setAttribute(name, decodeEntities(attr[1]));
    }
    utilityButtons.push(button);
  }
}
byId.runSetupMount = new FakeElement("runSetupMount");
function $(id) { return byId[id] || null; }
const document = {
  querySelector() { return null; },
  querySelectorAll(selector) {
    return selector === "[data-utility-action]" ? utilityButtons : [];
  },
  getElementById(id) { return byId[id] || null; }
};
const isWizardEnabled = () => false;
const state = { schema: null, selectedUtilityAction: "run" };
const renderPresetSummary = () => {};
const renderModelTuningPanels = () => {};
const decorateEnvHints = () => {};
const renderPromptProfilePanel = () => {};
const renderModelCatalogPanel = () => {};
const refreshModelKnobLinks = () => {};
const updateRunControls = () => {};
const knobField = () => "";
function currentControlValue(_env) { return ""; }
"""
            + block("function escapeHtml(text) {", "function formatDefault")
            + block(
                'function formatDefault(value, fallback="none") {',
                "function currentControlValue",
            )
            + utility_helpers
            + block("function renderRunSetup() {", "const TASK_MAX_TOKENS_LABELS")
            + r"""
state.schema = {
  actions: ["run", "check-sources", "future<&"],
  current_env: {},
  runtime: {},
  knobs: [],
  prompt_profiles: []
};
state.selectedUtilityAction = "check-sources";
renderRunSetup();
let markup = $("runSetupMount").innerHTML;
let buttons = utilityButtons;
assert(
  buttons.map(button => button.dataset.utilityAction).join(",") ===
    "run,check-sources,future<&",
  "utility buttons must preserve schema order and values"
);
assert(
  buttons.filter(button => button.getAttribute("aria-pressed") === "true").length === 1 &&
    buttons[1].classList.contains("selected"),
  "the configured utility action must be the only selected button"
);
assert(
  !$("sourceOptions").classList.contains("hidden"),
  "source options must be visible for a source utility"
);
assert(
  markup.includes('data-utility-action="future&lt;&amp;"') &&
    !markup.includes('data-utility-action="future<&"'),
  "schema action values must be escaped in attributes"
);
assert(
  buttons[2].getAttribute("aria-label") === "Future<&",
  "unknown actions should receive a readable fallback label"
);
selectUtilityAction("run");
assert(state.selectedUtilityAction === "run", "selection was not stored");
assert(
  buttons.filter(button => button.getAttribute("aria-pressed") === "true").length === 1 &&
    buttons[0].classList.contains("selected"),
  "selection must update button state and aria-pressed exclusively"
);
assert(
  $("sourceOptions").classList.contains("hidden"),
  "source options must hide for non-source utilities"
);
state.selectedUtilityAction = "removed-action";
renderRunSetup();
assert(
  utilityButtons[0].getAttribute("aria-pressed") === "true",
  "selection must fall back to the first schema action"
);
"""
        )
        self._run_node(script)

    def test_event_wiring_dispatches_selection_and_tracks_active_run(self) -> None:
        html = ui_module.HTML

        def block(start: str, end: str) -> str:
            return html[html.index(start) : html.index(end, html.index(start))]

        utility_helpers = html[
            html.index("    const SOURCE_UTILITY_ACTIONS") : html.index(
                "    function decorateUtilityHints"
            )
        ]
        script = (
            r"""
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
function makeClassList() {
  const classes = new Set();
  return {
    contains(name) { return classes.has(name); },
    toggle(name, force) {
      const next = force === undefined ? !classes.has(name) : Boolean(force);
      if (next) classes.add(name); else classes.delete(name);
      return next;
    }
  };
}
class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.classList = makeClassList();
    this.disabled = false;
    this.onclick = null;
    this.title = "";
  }
  setAttribute(name, value) { this[name] = String(value); }
}
const elements = {
  stopBtn: new FakeElement("stopBtn"),
  utilityPreviewBtn: new FakeElement("utilityPreviewBtn"),
  utilityRunBtn: new FakeElement("utilityRunBtn"),
  sourceOptions: new FakeElement("sourceOptions")
};
const utilityButtons = ["run", "check-sources", "source-languages"].map(action => {
  const button = new FakeElement("");
  button.dataset.utilityAction = action;
  return button;
});
function $(id) { return elements[id] || null; }
const document = {
  querySelectorAll(selector) {
    return selector === "[data-utility-action]" ? utilityButtons : [];
  }
};
const state = {
  schema: { actions: ["run", "check-sources", "source-languages"] },
  selectedUtilityAction: "run",
  activeRun: null
};
const calls = [];
function previewWithStatus(action) { calls.push(["preview", action]); }
function runAction(action) { calls.push(["run", action]); return Promise.resolve(); }
function setStatus(_message, _kind) {}
function api() { return Promise.resolve({}); }
const TASK_CONFIG = {};
"""
            + utility_helpers
            + block("    function updateRunControls() {", "    async function previewQuietly")
            + block("    function wireEvents() {", "    function applySelectedPresetFromState")
            + r"""
wireEvents();
assert(
  utilityButtons.every(button => typeof button.onclick === "function"),
  "utility action buttons must receive click handlers"
);
utilityButtons[1].onclick();
assert(
  state.selectedUtilityAction === "check-sources",
  "clicking a utility action must update the selected action"
);
assert(
  $("sourceOptions").classList.contains("hidden") === false,
  "selecting a source utility must show source options"
);
$("utilityPreviewBtn").onclick();
$("utilityRunBtn").onclick();
assert(
  JSON.stringify(calls) === JSON.stringify([
    ["preview", "check-sources"], ["run", "check-sources"]
  ]),
  "utility controls must dispatch the selected action"
);
state.activeRun = "run-1";
updateRunControls();
assert($("utilityRunBtn").disabled, "utility run must be disabled during an active run");
assert(
  utilityButtons.every(button => button.disabled),
  "utility action selection must be disabled during an active run"
);
state.activeRun = null;
updateRunControls();
assert(!$("utilityRunBtn").disabled, "utility run must be restored after the run ends");
assert(
  utilityButtons.every(button => !button.disabled),
  "utility action selection must be restored after the run ends"
);
"""
        )
        self._run_node(script)


if __name__ == "__main__":
    unittest.main()
