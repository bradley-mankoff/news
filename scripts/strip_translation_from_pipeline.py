"""Strip remaining translation code from pipeline.py.

Operates on the current line numbers identified by the grep above.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / "news_pipeline" / "pipeline.py"


def strip_lines(text: str, ranges: list[tuple[int, int]]) -> str:
    """Remove 1-indexed inclusive line ranges from text (in reverse order)."""
    lines = text.splitlines(keepends=True)
    for start, end in sorted(ranges, key=lambda r: -r[0]):
        del lines[start - 1 : end]
    return "".join(lines)


def main() -> int:
    text = PIPELINE.read_text(encoding="utf-8")
    raw_lines = text.splitlines()
    n = len(raw_lines)
    print(f"pipeline.py has {n} lines")

    # Locate the remaining translation function and block boundaries.
    def first_match(pattern: str) -> int:
        for i, line in enumerate(raw_lines, start=1):
            if re.search(pattern, line):
                return i
        return -1

    def last_match(pattern: str) -> int:
        for i, line in enumerate(reversed(raw_lines), start=1):
            if re.search(pattern, line):
                return n - i + 1
        return -1

    targets: list[tuple[int, int]] = []

    # _text_looks_non_english definition through _infer_script_translation_language
    # definition (ends right before the next non-blank function).
    a = first_match(r"^def _text_looks_non_english\b")
    b = first_match(r"^def _article_translation_decision\b")
    if a and b and a < b:
        # _article_translation_decision is already removed; fall through to other markers.
        pass
    a = first_match(r"^def _text_looks_non_english\b")
    c = first_match(r"^def _format_translation_prompt\b")
    if a and c and a < c:
        # _format_translation_prompt is the next function after the helpers.
        targets.append((a, c - 1))

    # _format_translation_prompt through end of _translate_text_with_translation_model
    a = first_match(r"^def _format_translation_prompt\b")
    d = first_match(r"^def translate_article_candidates\b")
    if a and d and a < d:
        targets.append((a, d - 1))

    # translate_article_candidates function
    a = first_match(r"^def translate_article_candidates\b")
    e = first_match(r"^def _build_article_heading\b")
    if a and e and a < e:
        targets.append((a, e - 1))

    # probe_translation_model_generation
    a = first_match(r"^def probe_translation_model_generation\b")
    b = first_match(r"^def _managed_translation_model_server_log_path\b")
    if a and b and a < b:
        targets.append((a, b - 1))

    # _managed_translation_model_server_log_path
    a = first_match(r"^def _managed_translation_model_server_log_path\b")
    b = first_match(r"^def _wait_for_managed_model_server\b")
    if a and b and a < b:
        targets.append((a, b - 1))

    # _wait_for_managed_translation_model_server
    a = first_match(r"^def _wait_for_managed_translation_model_server\b")
    b = first_match(r"^@contextmanager\b")
    if a and b and a < b:
        targets.append((a, b - 1))

    # managed_translation_model_server
    a = first_match(r"^def managed_translation_model_server\b")
    b = first_match(r"^def _run_pipeline\b")
    if a and b and a < b:
        targets.append((a, b - 1))

    # Translation status string in _run_pipeline (the if/else that mentions TRANSLATION_*)
    a = first_match(r'"Translation model: \{TRANSLATION_MODEL_REFERENCE\}')
    b = first_match(r'\)\s*$\n\s*progress_tracker.detail\(\s*\n\s*f"Default model:')
    if a and b and a < b:
        targets.append((a, b))

    # Translation event in _run_pipeline diagnostics (the if/else with TRANSLATION_ENABLED)
    a = first_match(r'"translation",\s*\n\s*candidate_count=len\(article_candidates\)')
    b = first_match(r'progress_tracker\.detail\("Translation pass skipped before global story clustering\."\)')
    if a and b and a < b:
        targets.append((a, b))

    # The call to _with_translation_metadata (the function and its 4 lines of kwargs)
    a = first_match(r"        article_record = _with_translation_metadata\(")
    c = first_match(r'            "url": selected_url,')
    if a and c and a < c:
        targets.append((a, c - 1))

    # Diagnostics output TRANSLATION_* entries
    a = first_match(r'        "TRANSLATION_MODEL_SERVER_COMMAND"')
    b = first_match(r'        "BRADLEY_RECIPIENT"')
    if a and b and a < b:
        targets.append((a, b - 1))

    # TRANSLATION_MAX_TOKENS line in diagnostics
    a = first_match(r'        "TRANSLATION_MAX_TOKENS"')
    b = first_match(r'        "ARTICLE_SUMMARY_MAX_TOKENS"')
    if a and b and a < b:
        targets.append((a, b - 1))

    # Step names list
    a = first_match(r'        "translation",\n        "clustering"')
    if a:
        targets.append((a, a))

    # Step labels
    a = first_match(r'        "translation": "translation",')
    if a:
        targets.append((a, a))

    # Environment output TRANSLATION_* entries
    a = first_match(r'            "translation_model": TRANSLATION_MODEL_REFERENCE,')
    b = first_match(r'            "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,')
    if a and b and a < b:
        targets.append((a, b - 1))

    # translation_max_tokens in environment output
    a = first_match(r'            "translation_max_tokens": TRANSLATION_MAX_TOKENS,')
    b = first_match(r'            "article_summary_max_tokens": ARTICLE_SUMMARY_MAX_TOKENS,')
    if a and b and a < b:
        targets.append((a, b - 1))

    # _translation_response_content call inside the probe
    a = first_match(r'        result\["content_preview"\] = _translation_response_content')
    b = first_match(r'        result\["ok"\] = True')
    if a and b and a < b:
        targets.append((a, b - 1))

    print("Removing ranges:")
    for start, end in sorted(targets, key=lambda r: r[0]):
        print(f"  lines {start}-{end}")

    if not targets:
        print("No matching ranges found; pipeline.py may already be clean.")
        return 0

    new_text = strip_lines(text, targets)
    PIPELINE.write_text(new_text, encoding="utf-8")
    print(f"Wrote {len(new_text.splitlines())} lines (was {n}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
