"""Nuke every test method that references any translation symbol, then clean up."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TP = ROOT / "tests" / "test_pipeline_helpers.py"

# Translation-related identifiers that, if present inside a test method body,
# mean the method exists solely to test translation behaviour.
TRANSLATION_TOKENS = [
    "TRANSLATION_",
    "_text_looks_non_english",
    "_translation_response_content",
    "_translation_messages",
    "_translation_payload",
    "_translation_model_resources",
    "_normalize_translation_language",
    "_infer_script_translation_language",
    "_article_translation_decision",
    "_with_translation_metadata",
    "_format_translation_prompt",
    "_load_translation_model_resources",
    "_unload_translation_model_resources",
    "_generate_translation_text",
    "_translate_text_with_translation_model",
    "_wait_for_managed_translation_model_server",
    "_managed_translation_model_server_log_path",
    "managed_translation_model_server",
    "run_translation_model_smoke_test",
    "translate_article_candidates",
    "preflight_translation_model_server",
    "probe_translation_model_generation",
]


def main() -> int:
    text = TP.read_text(encoding="utf-8")
    orig = text

    # Find every `def test_...` method and remove it if its body references any
    # translation token. A method is `def name(...) -> ret:\n  body\n` where body
    # extends until the next top-level `def ` or `class ` or end-of-file.
    method_re = re.compile(
        r"\n    def (test_\w+)\(self(?:, [^)]*)?\) -> [^:]+:",
        re.MULTILINE,
    )
    starts = [m.start() + 1 for m in method_re.finditer(text)]  # +1 to drop the leading \n
    ends = starts[1:] + [len(text)]

    # Walk through and remove methods whose body contains any translation token.
    # We work from the end backwards so offsets stay valid.
    to_remove: list[tuple[int, int, str]] = []
    for start, end in zip(starts, ends):
        body = text[start:end]
        if any(tok in body for tok in TRANSLATION_TOKENS):
            to_remove.append((start, end, body[:80].splitlines()[0] if body[:80] else ""))

    for start, end, header in sorted(to_remove, key=lambda r: -r[0]):
        # Also remove the blank line that follows the method (if any) so we
        # don't leave a double-blank.
        del_run_start = start
        del_run_end = end
        # Walk back to include the leading newline.
        if del_run_start > 0 and text[del_run_start - 1] == "\n":
            del_run_start -= 1
        # Walk forward to consume the trailing blank line.
        while del_run_end < len(text) and text[del_run_end] == "\n":
            del_run_end += 1
        text = text[:del_run_start] + text[del_run_end:]
        print(f"  OK: removed {header}")

    # Now clean up orphan code:
    # 1. `with patch.object(...),\n<blank>\n  body` where the patch.object is incomplete.
    text = re.sub(
        r"with patch\.object\([^)]*\),?\s*\n[ \t]*\n[ \t]+[A-Za-z_]",
        "if True:\n  ",
        text,
    )
    # 2. Standalone orphan `+ (` and `+ )` lines left by earlier rewrites.
    text = re.sub(r"^[ \t]+\+\s*\(\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]+\+\s*\)\s*$", "", text, flags=re.MULTILINE)
    # 3. `, patch.object(pipeline, "MANAGED_MODEL_SERVER_*", True/False)` lines
    # that are now disconnected from any `with`.
    text = re.sub(
        r"^[ \t]+, patch\.object\(\s*pipeline,\s*\"MANAGED_MODEL_SERVER_(?:ACTIVE|EXTERNAL|EXIT_RECORDED|READY|PROCESS)[^\n]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    # 4. Bare `            +` fragments.
    text = re.sub(r"^[ \t]+\+\s*$", "", text, flags=re.MULTILINE)
    # 5. Orphan `            +` after a `with patch.object(...)` block.
    text = re.sub(r"(\n            \+)\n", "\n", text)

    if text != orig:
        TP.write_text(text, encoding="utf-8")
        print(f"  Wrote {TP.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
