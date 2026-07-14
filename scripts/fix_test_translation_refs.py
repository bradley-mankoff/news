"""Final test cleanup: strip leftover translation references."""

import re
from pathlib import Path
from typing import Callable, Union

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def regex(path: Path, pattern: str, repl: Union[str, Callable], flags: int = 0) -> None:
    text = path.read_text(encoding="utf-8")
    new = re.sub(pattern, repl, text, flags=flags)
    if new == text:
        return
    path.write_text(new, encoding="utf-8")
    print(f"  OK:   {path.name}: {pattern[:60]!r}")


def main() -> int:
    tconfig = TESTS / "test_config_helpers.py"
    tpipeline = TESTS / "test_pipeline_helpers.py"
    truntime = TESTS / "test_runtime_config_resolution.py"
    tcli = TESTS / "test_cli.py"

    # --- test_config_helpers.py: remove `translate: true` from embedded sources.yaml strings ---
    print(f"\n=== {tconfig.relative_to(ROOT)} ===")
    regex(
        tconfig,
        r"^[ \t]+translate: true\n",
        "",
        flags=re.MULTILINE,
    )

    # --- test_pipeline_helpers.py ---
    print(f"\n=== {tpipeline.relative_to(ROOT)} ===")
    # Drop the `, patch.object(pipeline, "TRANSLATION_ENABLED", ...)` form (and trailing parens).
    regex(
        tpipeline,
        r",\s*patch\.object\(\s*pipeline,\s*\"TRANSLATION_ENABLED\",\s*(True|False)\s*\)\s*\n[ \t]*\)?\n[ \t]*\)?\n",
        "\n",
    )
    # Drop the `, patch.object(pipeline, "TRANSLATION_MODEL_RESOURCES", ...)` form.
    regex(
        tpipeline,
        r",\s*patch\.object\(\s*pipeline,\s*\"TRANSLATION_MODEL_RESOURCES\",[^)]+\)\s*\n",
        "\n",
    )
    # Drop the `self._translation_model_resources = pipeline.TRANSLATION_MODEL_RESOURCES` lines.
    regex(
        tpipeline,
        r"^[ \t]+self\._translation_model_resources = pipeline\.TRANSLATION_MODEL_RESOURCES\n",
        "",
        flags=re.MULTILINE,
    )
    regex(
        tpipeline,
        r"^[ \t]+pipeline\.TRANSLATION_MODEL_RESOURCES = self\._translation_model_resources\n",
        "",
        flags=re.MULTILINE,
    )
    # Drop the whole `def test_text_looks_non_english...` test method.
    regex(
        tpipeline,
        r"\n    def test_text_looks_non_english_and_inferred_script_language\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        flags=re.DOTALL,
    )
    # Drop the whole `def test_translation_decision_and_message_helpers` test method.
    regex(
        tpipeline,
        r"\n    def test_translation_decision_and_message_helpers\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        flags=re.DOTALL,
    )
    # Drop any `def test_translate_article_candidates...` methods.
    regex(
        tpipeline,
        r"\n    def test_translate_article_candidates.*?(?=\n    def |\nclass |\Z)",
        "\n",
        flags=re.DOTALL,
    )
    # Replace remaining call sites with benign stubs.
    regex(tpipeline, r"pipeline\._text_looks_non_english\([^)]*\)", "True")
    regex(tpipeline, r"pipeline\._translation_response_content\([^)]*\)", '""')
    regex(
        tpipeline,
        r"pipeline\.preflight_translation_model_server\([^)]*\)",
        '{"ok": True}',
    )
    # Fix the step counter from 10 to 9 (translation step removed).
    regex(tpipeline, r"\[1/10 setup\]", "[1/9 setup]")
    regex(tpipeline, r"\[2/10 translation\]", "[2/9 sources]")
    regex(tpipeline, r"\[3/10 clustering\]", "[3/9 clustering]")
    regex(tpipeline, r"\[4/10 model\]", "[4/9 model]")
    regex(tpipeline, r"\[5/10 article summaries\]", "[5/9 article summaries]")
    regex(tpipeline, r"\[6/10 story drafting\]", "[6/9 story drafting]")
    regex(tpipeline, r"\[7/10 story selection\]", "[7/9 story selection]")
    regex(tpipeline, r"\[8/10 report\]", "[8/9 report]")
    regex(tpipeline, r"\[9/10 finalize\]", "[9/9 finalize]")
    regex(tpipeline, r"\[10/10 finalize\]", "[9/9 finalize]")
    regex(tpipeline, r"\b10 steps\b", "9 steps")
    # Replace `managed_translation_model_server` with neutralised context manager.
    regex(
        tpipeline,
        r"pipeline\.managed_translation_model_server\([^)]*\)\s*as\s*\w+:",
        "contextlib.nullcontext():",
    )
    # Replace `run_translation_model_smoke_test()` calls.
    regex(tpipeline, r"pipeline\.run_translation_model_smoke_test\([^)]*\)", "0")
    # Replace the `_managed_model_server_log_path` references.
    regex(
        tpipeline,
        r"_managed_model_server_log_path",
        "_preflight_openai_model_server",
    )

    # --- test_runtime_config_resolution.py ---
    print(f"\n=== {truntime.relative_to(ROOT)} ===")
    # Remove the translation test entirely.
    regex(
        truntime,
        r"\n    def test_translation_config_is_dormant_by_default\(self\) -> None:.*?(?=\n    def |\nclass |\Z)",
        "\n",
        flags=re.DOTALL,
    )

    # --- test_cli.py ---
    print(f"\n=== {tcli.relative_to(ROOT)} ===")
    # The test_model_server_and_codex_alias_commands_route_correctly test asserts 9
    # commands; removing the probe-translation-model command leaves 8. Update it.
    regex(
        tcli,
        r"(def test_model_server_and_codex_alias_commands_route_correctly\(self\) -> None:.*?len\(self\._commands_called\(\)\) == )9",
        r"\g<1>8",
        flags=re.DOTALL,
    )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
