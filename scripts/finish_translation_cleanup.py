"""Final cleanup: remove remaining translation code from pipeline.py, tests, and SETTINGS.md."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_file(path: Path, edits: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    orig = text
    for old, new in edits:
        if old not in text:
            print(f"  SKIP (not found): {old[:60]!r}")
            continue
        text = text.replace(old, new, 1)
        print(f"  OK: {old[:60]!r}")
    if text != orig:
        path.write_text(text, encoding="utf-8")
        print(f"  Wrote {path.relative_to(ROOT)}")
    else:
        print(f"  No changes: {path.relative_to(ROOT)}")


def main() -> int:
    pipeline = ROOT / "news_pipeline" / "pipeline.py"
    settings = ROOT / "SETTINGS.md"
    tests_dir = ROOT / "tests"

    # ---------- pipeline.py ----------
    print(f"\n=== {pipeline.relative_to(ROOT)} ===")
    ptext = pipeline.read_text(encoding="utf-8")
    orig = ptext

    # Remove the _translation_response_content call in the probe.
    old = '        result["content_preview"] = _translation_response_content(response_payload)[:80]'
    new = '        result["content_preview"] = str(response_payload)[:80]'
    if old in ptext:
        ptext = ptext.replace(old, new, 1)
        print(f"  OK: replaced _translation_response_content call in probe")

    # Remove _wait_for_managed_translation_model_server function.
    a = ptext.find("\ndef _wait_for_managed_translation_model_server(")
    if a >= 0:
        b = ptext.find("\n@contextmanager\ndef managed_translation_model_server(", a)
        if b < 0:
            b = ptext.find("\ndef managed_translation_model_server(", a)
        if b >= 0:
            ptext = ptext[:a] + ptext[b:]
            print(f"  OK: removed _wait_for_managed_translation_model_server function")

    # Remove the translation event block (diagnostics.event).
    a = ptext.find('    diagnostics.event(\n        "translation",\n        candidate_count=len(article_candidates),')
    if a >= 0:
        b = ptext.find('    progress_tracker.detail("Translation pass skipped', a)
        if b >= 0:
            ptext = ptext[:a] + ptext[b:]
            print(f"  OK: removed translation event block in _run_pipeline")

    # Remove the orphaned progress_tracker.detail line.
    old = '    progress_tracker.detail("Translation pass skipped before global story clustering.")\n'
    if old in ptext:
        ptext = ptext.replace(old, "", 1)
        print("  OK: removed orphan translation-pass-skipped detail line")

    if ptext != orig:
        pipeline.write_text(ptext, encoding="utf-8")
        print(f"  Wrote pipeline.py ({len(ptext.splitlines())} lines)")

    # ---------- SETTINGS.md ----------
    print(f"\n=== {settings.relative_to(ROOT)} ===")
    stext = settings.read_text(encoding="utf-8")
    orig = stext
    stext = re.sub(
        r"\| `NEWS_TRANSLATION_MODEL`.*?\n"
        r"\| `NEWS_TRANSLATION_MODEL_BASE_URL`.*?\n"
        r"\| `NEWS_TRANSLATION_TARGET_LANGUAGE`.*?\n",
        "",
        stext,
        count=1,
    )
    if stext != orig:
        settings.write_text(stext, encoding="utf-8")
        print("  OK: removed NEWS_TRANSLATION_* env vars")

    # ---------- tests/test_cli.py ----------
    print(f"\n=== tests/test_cli.py ===")
    patch_file(
        tests_dir / "test_cli.py",
        [
            (
                '    def test_source_and_translation_alias_commands_route_to_helpers(self) -> None:\n'
                '        with patch("news_pipeline.source_checks.main", return_value=11) as source_checks_main:\n'
                '            code, stdout, stderr = self._invoke(["source-check", "--only-failures"])\n'
                '        self.assertEqual(code, 11)\n',
                "",
            ),
            (
                '        with patch("news_pipeline.pipeline.run_translation_model_smoke_test", return_value=14) as smoke_test:\n'
                '            code, stdout, stderr = self._invoke(["probe-translation-model"])\n'
                '        self.assertEqual(code, 14)\n',
                "",
            ),
            (
                '    def test_translation_model_alias_rejects_unexpected_args(self) -> None:\n'
                '        with patch("news_pipeline.pipeline.run_translation_model_smoke_test") as smoke_test:\n',
                "",
            ),
        ],
    )

    # ---------- tests/test_config_helpers.py ----------
    print(f"\n=== tests/test_config_helpers.py ===")
    patch_file(
        tests_dir / "test_config_helpers.py",
        [
            (
                '        with patch.dict(os.environ, {"NEWS_TRANSLATION_MODEL": "gemma-4-vision", "NEWS_TRANSLATION_ENABLED": "1"}, clear=True):\n'
                '            self.assertEqual(config_module._configured_translation_model_reference(), "gemma-4-vision")\n'
                '            self.assertTrue(config_module._configured_translation_enabled())\n'
                '            self.assertEqual(config_module._configured_translation_model_backend("gemma-4-vision"), "mlx-vlm")\n',
                "",
            ),
            (
                '            self.assertTrue(config_module._source_requires_translation({"requires_translation": True}))\n'
                '            self.assertFalse(config_module._source_requires_translation({"translate": False}))\n',
                "",
            ),
            (
                '                        translate: true\n',
                "",
            ),
            (
                '                self.assertEqual(\n'
                '                    config_module.write_source_translation_flags(root / "sources.yaml", {"alpha": "en"}),\n'
                '                    3,\n'
                '                )\n',
                "",
            ),
            (
                '            {\n'
                '                "NEWS_TRANSLATION_ENABLED": "1",\n'
                '                "NEWS_TRANSLATION_MODEL": "gemma-4-vision",\n'
                '            },\n'
                '            clear=True,\n'
                '        ), patch.object(config_module, "build_model_server_command", return_value="translation-cmd") as build_command, patch.object(\n'
                '            config_module,\n'
                '            "load_model_tuning_presets",\n'
                '            return_value={},\n'
                '        ), patch.dict(os.environ, {"NEWS_MODEL_BACKEND": "mlx-lm"}, clear=True):\n'
                '            runtime = config_module.load_runtime_config(materialize_outputs=False, environ={})\n'
                '        self.assertTrue(runtime.translation_enabled)\n'
                '        self.assertEqual(runtime.translation_model_server_command, "translation-cmd")\n'
                '        self.assertEqual(build_command.call_args_list[-1].args[0], "gemma-4-vision")\n'
                '        self.assertEqual(build_command.call_args_list[-1].kwargs["backend"], "mlx-vlm")\n',
                "",
            ),
        ],
    )

    # ---------- tests/test_pipeline_helpers.py ----------
    print(f"\n=== tests/test_pipeline_helpers.py ===")
    test_path = tests_dir / "test_pipeline_helpers.py"
    ttext = test_path.read_text(encoding="utf-8")
    orig = ttext
    # Replace the TRANSLATION_MODEL_RESOURCES setup/teardown with plain pattern.
    ttext = ttext.replace(
        "        self._translation_model_resources = pipeline.TRANSLATION_MODEL_RESOURCES\n",
        "",
    )
    ttext = ttext.replace(
        "        pipeline.TRANSLATION_MODEL_RESOURCES = self._translation_model_resources\n",
        "",
    )
    # Drop the whole _text_looks_non_english / _infer_script_translation_language test method.
    ttext = re.sub(
        r"\n    def test_text_looks_non_english_and_inferred_script_language\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    # Drop the translation decision/message helpers test method.
    ttext = re.sub(
        r"\n    def test_translation_decision_and_message_helpers\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    # Drop the translate_article_candidates tests.
    ttext = re.sub(
        r"\n    def test_translate_article_candidates.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    # Drop any remaining test_run_translation_model_smoke_test_*.
    ttext = re.sub(
        r"\n    def test_run_translation_model_smoke_test.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    ttext = re.sub(
        r"\n    def test_run_translation_model_smoke_test.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    # Drop the model_server_and_translation_readiness test.
    ttext = re.sub(
        r"\n    def test_model_server_and_translation_readiness_branches\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        ttext,
        count=1,
        flags=re.DOTALL,
    )
    if ttext != orig:
        test_path.write_text(ttext, encoding="utf-8")
        print("  OK: cleaned test_pipeline_helpers.py")

    # ---------- tests/test_runtime_config_resolution.py ----------
    print(f"\n=== tests/test_runtime_config_resolution.py ===")
    patch_file(
        tests_dir / "test_runtime_config_resolution.py",
        [
            (
                '    def test_translation_config_is_dormant_by_default(self) -> None:\n'
                '        config = load_runtime_config(\n'
                '            environ={},\n'
                '            overrides={"NEWS_MODEL": CODEX_TEST_MODEL_ALIAS},\n'
                '            materialize_outputs=False,\n'
                '            run_started_at=datetime(2026, 6, 14, 12, 0, 0),\n'
                '        )\n'
                '\n'
                '        self.assertFalse(config.translation_enabled)\n'
                '        self.assertEqual(config.translation_model_reference, "google/translategemma-4b-it")\n'
                '        self.assertEqual(config.translation_model_server_command, "")\n',
                "",
            ),
        ],
    )

    # ---------- tests/test_source_catalog.py ----------
    print(f"\n=== tests/test_source_catalog.py ===")
    patch_file(
        tests_dir / "test_source_catalog.py",
        [
            (
                "from news_pipeline.config import CONFIG_DIR, load_sources, write_source_translation_flags\n",
                "from news_pipeline.config import CONFIG_DIR, load_sources\n",
            ),
            (
                "    MarkTranslationRequired,\n",
                "",
            ),
            (
                "    _apply_translation,\n",
                "",
            ),
            (
                "    \"    language: es\",\n"
                "    \"    requires_translation: false\",\n",
                "    \"    language: es\",\n",
            ),
            (
                "    def test_core_sources_do_not_require_translation(self) -> None:\n"
                "        sources_path = CONFIG_DIR / \"sources.yaml\"\n"
                "        payload = yaml.safe_load(sources_path.read_text(encoding=\"utf-8\")) or {}\n"
                "        core_translation_sources = [\n"
                "            str(source.get(\"key\") or source.get(\"name\") or \"\")\n"
                "            for source in payload.get(\"sources\", [])\n"
                "            if isinstance(source, dict)\n"
                "            and (\n"
                "                str(source.get(\"language\") or \"\").strip().lower() != \"en\"\n"
                "                or bool(source.get(\"requires_translation\", source.get(\"translate\")))\n"
                "            )\n"
                "        ]\n"
                "\n"
                "        self.assertEqual(core_translation_sources, [])\n",
                "",
            ),
            (
                "    def test_source_loading_excludes_translation_sources_for_all_scopes(self) -> None:\n",
                "    def test_source_loading_excludes_non_english_sources_for_all_scopes(self) -> None:\n",
            ),
            (
                "                                \"key\": \"TranslationRequired\",\n"
                "                                \"url\": \"https://example.com/translate.xml\",\n"
                "                                \"language\": \"en\",\n"
                "                                \"tier\": \"peripheral\",\n"
                "                                \"requires_translation\": True,\n"
                "                            },\n",
                "",
            ),
            (
                "        self.assertEqual(_coerce_source_value(\"requires_translation\", \"1\"), True)\n",
                "",
            ),
            (
                "        self.assertEqual(_apply_translation(lines[:], records[:], MarkTranslationRequired({})), (lines[:], records[:], 0))\n",
                "",
            ),
            (
                "        translated_lines, translated_records, translated_count = _apply_translation(\n"
                "            translated_lines,\n"
                "            translated_records,\n"
                "            MarkTranslationRequired({\"Beta\": \"fr\"}),\n"
                "        )\n"
                "        self.assertGreater(translated_count, 0)\n"
                "        self.assertIn(\"    requires_translation: true\\n\", \"\".join(translated_lines))\n"
                "        self.assertIn(\"    translation_source_language: fr\\n\", \"\".join(translated_lines))\n",
                "",
            ),
            (
                "    def test_translation_flags_write_requires_translation_and_source_language(self) -> None:\n",
                "",
            ),
        ],
    )

    # ---------- tests/test_ui.py ----------
    print(f"\n=== tests/test_ui.py ===")
    patch_file(
        tests_dir / "test_ui.py",
        [
            (
                "                    translation_enabled=False,\n"
                "                    translation_model_reference=\"trans\",\n"
                "                    translation_model_name=\"Trans\",\n"
                "                    translation_model_backend=\"mlx-lm\",\n"
                "                    translation_model_base_url=\"http://localhost:8081\",\n"
                "                    translation_target_language=\"es\",\n"
                "                    translation_model_server_command=\"python -m trans\",\n",
                "",
            ),
        ],
    )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
