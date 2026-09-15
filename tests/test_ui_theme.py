"""DN-82: dark mode is the default theme. Covers AC1-AC4."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from news_pipeline import ui as ui_module


def _find_node() -> str | None:
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


def _theme_js(html: str) -> str:
    start = html.find('const THEME_STORAGE_KEY')
    self_end_marker = 'function initTheme() {'
    end_start = html.find(self_end_marker)
    assert start >= 0 and end_start >= 0, "theme JS block missing"
    end = html.find('}', html.find('return applyTheme(getStoredTheme());', end_start)) + 1
    return html[start:end]


class UIThemeTests(unittest.TestCase):
    def test_fresh_load_defaults_to_dark_with_no_flash(self) -> None:
        html = ui_module.HTML
        head = html.split('</head>')[0]
        bootstrap_at = head.find('news-theme')
        style_at = head.find('<style>')
        self.assertGreaterEqual(bootstrap_at, 0, "pre-paint theme bootstrap missing")
        self.assertGreater(style_at, bootstrap_at, "bootstrap must run before <style> to avoid flash")
        self.assertIn('localStorage.getItem("news-theme")', head)
        self.assertIn('dataset.theme', head)
        self.assertIn('"dark"', head)
        # CSS defaults are dark even before JS runs (JS-disabled still dark).
        self.assertIn(':root {\n      color-scheme: dark;', html)
        self.assertIn('--bg: #12151c;', html)
        self.assertIn(':root[data-theme="light"]', html)

    def test_visible_toggle_persists_choice(self) -> None:
        html = ui_module.HTML
        self.assertIn('id="themeToggle"', html)
        self.assertIn('class="header-actions"', html)
        # Segmented control mirrors the mode pill: two options, pressed state.
        self.assertIn('aria-label="Color theme"', html)
        self.assertIn('data-theme-value="light"', html)
        self.assertIn('data-theme-value="dark"', html)
        self.assertIn('☀️ Light', html)
        self.assertIn('🌙 Dark', html)
        # Persistence contract: storage key + read/write + wiring.
        self.assertIn('const THEME_STORAGE_KEY = "news-theme";', html)
        self.assertIn('function getStoredTheme()', html)
        self.assertIn('function applyTheme(theme)', html)
        self.assertIn('function toggleTheme()', html)
        self.assertIn('function initTheme()', html)
        self.assertIn('localStorage.setItem(THEME_STORAGE_KEY', html)
        self.assertIn('const themeGroup = $("themeToggle")', html)
        self.assertIn('querySelectorAll("[data-theme-value]")', html)
        self.assertIn('initTheme();', html)

    def test_dark_palette_stays_readable(self) -> None:
        html = ui_module.HTML
        # Dark tokens: light ink on dark surfaces (contrast, not invisible text).
        for token in (
            '--ink: #e9edf6;',
            '--muted: #a7b0c2;',
            '--surface: #1a202c;',
            '--card: #1e2532;',
            '--input-bg: #151b26;',
        ):
            self.assertIn(token, html)
        # Every major view surface follows the theme variables.
        for selector in (
            '.stat { border-left: 4px solid var(--blue); padding: 8px 10px; background: var(--card);',
            '.table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--card); }',
            '.knob { border: 1px solid var(--line); border-radius: 14px; padding: 10px; background: var(--card);',
            'tr:hover td { background: var(--hover); }',
            '.selected td { background: var(--selected); }',
            'background: var(--banner-bg);',
            'background: var(--header-bg);',
        ):
            self.assertIn(selector, html)
        # Badge tints have dark-mode overrides (no low-contrast pastel on dark).
        for override in (
            ':root[data-theme="dark"] .badge.good',
            ':root[data-theme="dark"] .badge.warn',
            ':root[data-theme="dark"] .badge.bad',
            ':root[data-theme="dark"] .badge.blue',
        ):
            self.assertIn(override, html)
        # Light-blue primary buttons keep contrast with dark text in dark mode.
        self.assertIn(':root[data-theme="dark"] button.primary { color: #0e1522; }', html)

    def test_theme_js_persists_and_toggles_in_node_harness(self) -> None:
        node = _find_node()
        if node is None:
            self.skipTest("Node.js is required for the theme harness")
        theme_js = _theme_js(ui_module.HTML)
        harness = (
            "const store = {};\n"
            "globalThis.localStorage = {\n"
            "  getItem: (k) => (k in store ? store[k] : null),\n"
            "  setItem: (k, v) => { store[k] = String(v); },\n"
            "};\n"
            "const attrs = {};\n"
            "const mkBtn = (value) => ({ dataset: { themeValue: value }, _attrs: {},\n"
            "  setAttribute(k, v) { this._attrs[k] = String(v); } });\n"
            "const groupEl = { _btns: [mkBtn('light'), mkBtn('dark')],\n"
            "  querySelectorAll(sel) { return sel.includes('data-theme-value') ? this._btns : []; } };\n"
            "globalThis.document = { documentElement: { dataset: {}, style: {} },\n"
            "  getElementById: (id) => (id === 'themeToggle' ? groupEl : null) };\n"
            + theme_js + "\n"
            "const assert = (cond, msg) => { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } };\n"
            "const pressed = (v) => groupEl._btns.find((b) => b.dataset.themeValue === v)._attrs['aria-pressed'];\n"
            "assert(getStoredTheme() === 'dark', 'fresh load must default to dark');\n"
            "assert(applyTheme(getStoredTheme()) === 'dark', 'applyTheme returns dark');\n"
            "assert(document.documentElement.dataset.theme === 'dark', 'dataset.theme is dark');\n"
            "assert(store['news-theme'] === 'dark', 'dark choice persists');\n"
            "assert(pressed('dark') === 'true' && pressed('light') === 'false', 'dark segment pressed, light not');\n"
            "assert(toggleTheme() === 'light', 'toggle flips to light');\n"
            "assert(store['news-theme'] === 'light', 'light choice persists');\n"
            "assert(document.documentElement.dataset.theme === 'light', 'dataset.theme is light');\n"
            "assert(pressed('light') === 'true' && pressed('dark') === 'false', 'light segment pressed, dark not');\n"
            "assert(getStoredTheme() === 'light', 'stored light survives reload');\n"
            "console.log('theme harness ok');\n"
        )
        result = subprocess.run(
            [node, "--input-type=module", "-"],
            input=harness,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("theme harness ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
