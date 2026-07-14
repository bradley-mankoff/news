"""Finish the pipeline.py translation removal.

Handles:
- The _with_translation_metadata call site (unwraps the call to just the dict)
- Step names list and labels
- _translation_response_content usage in the probe
- _wait_for_managed_translation_model_server
- managed_translation_model_server
- The translation status string in _run_pipeline
- The translation event block in _run_pipeline
- The _with_translation_metadata function definition (and the unwrap above)
"""

from __future__ import annotations

import re
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / "news_pipeline" / "pipeline.py"


def main() -> int:
    text = PIPELINE.read_text(encoding="utf-8")
    orig = text
    lines = text.splitlines(keepends=True)

    def first_idx(pattern: str) -> int:
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                return i
        return -1

    def last_idx(pattern: str) -> int:
        for i in range(len(lines) - 1, -1, -1):
            if re.search(pattern, lines[i]):
                return i
        return -1

    edits: list[str] = []

    # 1. Unwrap the _with_translation_metadata call: remove the call wrapper and
    # dedent the dict body by 4 spaces.
    call_start = first_idx(r"        article_record = _with_translation_metadata\(")
    if call_start >= 0:
        # Find the matching close paren. The body is indented 12 spaces, the
        # kwargs at 12 spaces, and the close paren at 8 spaces.
        close_paren = -1
        for i in range(call_start + 1, len(lines)):
            if re.match(r"^        \)\s*$", lines[i]):
                close_paren = i
                break
        if close_paren < 0:
            print("ERROR: could not find close paren for _with_translation_metadata call")
            return 1
        # Replace the call line with just the dict assignment opening.
        lines[call_start] = "        article_record = {\n"
        # Dedent the dict body (lines call_start+1 to first occurrence of
        # `            },` or `            source_name=`).
        # The body is the first contiguous block of lines indented 12+ spaces.
        body_end = -1
        for i in range(call_start + 1, close_paren):
            stripped = lines[i].lstrip()
            if stripped and not lines[i].startswith(" " * 12):
                # First non-indented line after the body — stop.
                body_end = i
                break
        if body_end < 0:
            body_end = close_paren
        for i in range(call_start + 1, body_end):
            lines[i] = lines[i][4:] if lines[i].startswith(" " * 4) else lines[i].lstrip()
        # Find the dict closing `},` and dedent it to 8 spaces.
        for i in range(call_start + 1, close_paren):
            if re.match(r"^            \},?\s*$", lines[i]):
                lines[i] = re.sub(r"^ {12}", "        ", lines[i])
                break
        # Find the kwargs lines and the close paren, remove them.
        # The kwargs start at the first `            source_name=` line.
        for i in range(call_start + 1, close_paren):
            if re.match(r"^            source_name=", lines[i]):
                # Remove from this line to the close paren (inclusive).
                del lines[i:close_paren + 1]
                break
        else:
            # Fallback: just remove the close paren.
            del lines[close_paren:close_paren + 1]
        edits.append("unwrapped _with_translation_metadata call")

    # 2. Step names list — remove "translation", entry.
    a = first_idx(r'        "translation",\n        "clustering"')
    if a >= 0:
        del lines[a:a + 2]
        edits.append(f"removed step name entry at line {a}")

    # Also handle the case where it's on one line (read may collapse).
    a = first_idx(r'        "translation",')
    b = first_idx(r'        "clustering",')
    if a >= 0 and b >= 0 and a < b and (b - a) <= 2:
        del lines[a:b]
        edits.append("removed step name (one-line form)")

    # 3. Step labels — remove "translation": "translation",
    a = first_idx(r'        "translation": "translation",')
    if a >= 0:
        del lines[a:a + 1]
        edits.append(f"removed step label at line {a}")

    # 4. _translation_response_content usage in the probe.
    a = first_idx(r'        result\["content_preview"\] = _translation_response_content')
    b = first_idx(r'        result\["ok"\] = True')
    if a >= 0 and b >= 0 and a < b:
        del lines[a:b]
        edits.append("removed _translation_response_content call in probe")

    # 5. _wait_for_managed_translation_model_server function.
    a = first_idx(r"^def _wait_for_managed_translation_model_server\b")
    b = first_idx(r"^def managed_translation_model_server\b")
    if a >= 0 and b >= 0 and a < b:
        del lines[a:b]
        edits.append("removed _wait_for_managed_translation_model_server")
    elif a >= 0:
        # Fall back: remove to the next decorator.
        c = first_idx(r"^@contextmanager\b")
        if c >= 0 and a < c:
            del lines[a:c]
            edits.append("removed _wait_for_managed_translation_model_server (to @contextmanager)")

    # 6. managed_translation_model_server function.
    a = first_idx(r"^def managed_translation_model_server\b")
    b = first_idx(r"^def _run_pipeline\b")
    if a >= 0 and b >= 0 and a < b:
        del lines[a:b]
        edits.append("removed managed_translation_model_server")

    # 7. Translation status string in _run_pipeline.
    a = first_idx(r'"Translation model: \{TRANSLATION_MODEL_REFERENCE\}')
    if a >= 0:
        # Find the end of the if/else expression.
        b = a
        depth = 0
        started = False
        for i in range(a, min(a + 20, len(lines))):
            line = lines[i]
            for ch in line:
                if ch == "(":
                    depth += 1
                    started = True
                elif ch == ")":
                    depth -= 1
            if started and depth <= 0:
                b = i
                break
        if b > a:
            del lines[a:b + 1]
            edits.append("removed translation status string in _run_pipeline")

    # 8. Translation event block in _run_pipeline.
    a = first_idx(r'        diagnostics\.event\(\s*\n\s*"translation",\s*\n\s*candidate_count=len\(article_candidates\)')
    if a >= 0:
        b = a
        for i in range(a, min(a + 20, len(lines))):
            if re.match(r"^    progress_tracker\.detail\(", lines[i]):
                b = i
                break
        if b > a:
            del lines[a:b]
            edits.append("removed translation event in _run_pipeline")

    # 9. Remove _with_translation_metadata function definition (if still there).
    a = first_idx(r"^def _with_translation_metadata\b")
    if a >= 0:
        # Find next function or non-blank non-indented line.
        b = a
        for i in range(a + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("def ") or stripped.startswith("class "):
                b = i
                break
            if stripped and not lines[i].startswith((" ", "\t")):
                b = i
                break
        else:
            b = len(lines)
        del lines[a:b]
        edits.append("removed _with_translation_metadata function definition")

    new_text = "".join(lines)
    if new_text == orig:
        print("No changes applied.")
    else:
        PIPELINE.write_text(new_text, encoding="utf-8")
        print("Applied edits:")
        for e in edits:
            print(f"  - {e}")
        print(f"New line count: {len(lines)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
