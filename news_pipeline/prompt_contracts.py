"""Prompt Contracts: code-owned registry of machine-required output protocols.

Prompt Profiles (``prompt_catalog.py``) customize the *editorial instruction
sentences* injected into the five LLM prompt stages. The machine contracts —
the output protocols the parsers, retry loops, citation renderers, and
sanitizers depend on (``DATABASE_ENTRY:`` blocks, retry correction messages,
``Headline:``/``Main story:``/``Contradictions:`` format, ``[[S1]]`` citation
markers, strict JSON for image art and scale screening) — live here as named
constants, the single source of truth. Stage modules compose their prompts
from these constants; ``validate_prompt_contract()`` proves a rendered prompt
still contains every required marker, and ``validate_editorial_instructions()``
fail-fasts at config resolution when a profile's editable sentences would
weaken or collide with a contract.

This module is deliberately stdlib-only (``collections.abc``, ``typing``) so
that ``config.py`` can import it without creating an import cycle (it must
never import stage modules). Built-ins live in Python (not YAML) because they
are code-reviewed contracts; editorial sentence overrides may be supplied by
``config/prompt_overrides.yaml`` and are validated here before pipeline
execution.
"""

from __future__ import annotations

from collections.abc import Mapping

PROTOCOL_TASKS = (
    "article_summary",
    "story_scale_screening",
    "story_drafting",
    "title_generation",
    "image_art_direction",
)

# --- Machine contracts (verbatim from the stage templates; whitespace-exact) ---

ARTICLE_SUMMARY_FORMAT_ERROR_MESSAGE = (
    "Format Error: respond with exactly one article block only. "
    "Use 'DATABASE_ENTRY:' followed by '### article title', then 'Metadata:' with Source/Published/URL bullets "
    "then 'Summary:'. "
    "Do not add commentary, correction text, code fences, or trailing notes."
)

ARTICLE_SUMMARY_OUTPUT_CONTRACT = (
    "8. Start your response with 'DATABASE_ENTRY:' and then exactly the requested Markdown block.\n"
    "9. Do not include any text before 'DATABASE_ENTRY:' or after the summary."
)

ARTICLE_SUMMARY_BLOCK_INTRO = "Return exactly this block, replacing only the summary text:"

STORY_DRAFTING_CITATION_CONTRACT = (
    "End every factual sentence with one or more source markers using the listed source IDs,\n"
    "like [[S1]] or one combined marker for multiple sources like [[S1,S3]].\n"
    "Use only listed source IDs and do not invent sources."
)

STORY_DRAFTING_OUTPUT_CONTRACT = (
    "Return exactly this format:\n"
    "Headline: <custom story headline>\n"
    "Main story: <story paragraph with sentence-end source markers>\n"
    "Contradictions: NONE\n"
    "\n"
    "Or, only if there is a real direct or material contradiction:\n"
    "Headline: <custom story headline>\n"
    "Main story: <story paragraph with sentence-end source markers>\n"
    "Contradictions: <short contradiction evidence paragraph with sentence-end source markers>"
)

# Single braces on purpose: injected as a .format() VALUE (inserted verbatim,
# never re-parsed as a template), so they must NOT be doubled.
STORY_SCALE_SCREENING_JSON_CONTRACT = (
    "Return only valid JSON as an array of objects:\n"
    "[{\n"
    '  "story_key":"...",\n'
    '  "scale":"obviously_large_scale|not_obvious|obviously_small_scale",\n'
    '  "scale_reason":"short scale reason"\n'
    "}]"
)

IMAGE_ART_JSON_CONTRACT = "Return ONLY valid JSON with the key image_prompt."

TITLE_GENERATION_JSON_CONTRACT = (
    "Return ONLY valid JSON with the key overlay_headline."
)

IMAGE_ART_OVERLAY_PROTOCOL = (
    "The overlay_headline is readable text that will be rendered later by code, "
    "not by the image model."
)

# --- Required marker substrings per task (drift guard for rendered prompts) ---

PROTOCOL_MARKERS: dict[str, tuple[str, ...]] = {
    "article_summary": (
        "DATABASE_ENTRY:",
        "Start your response with 'DATABASE_ENTRY:'",
        "Return exactly this block, replacing only the summary text:",
        "### ",
        "Metadata:",
        "Summary:",
    ),
    "story_drafting": (
        "Headline:",
        "Main story:",
        "Contradictions:",
        "[[S1]]",
        "[[S1,S3]]",
        "Use only listed source IDs and do not invent sources.",
        "Return exactly this format:",
    ),
    "story_scale_screening": (
        "Return only valid JSON",
        "obviously_large_scale",
        "not_obvious",
        "obviously_small_scale",
        "story_key",
        "scale_reason",
    ),
    "title_generation": (
        "Return ONLY valid JSON with the key overlay_headline",
        "overlay_headline",
        "rendered later by code",
    ),
    "image_art_direction": (
        "Return ONLY valid JSON with the key image_prompt",
    ),
}

# Contract sentences forbidden inside editable editorial instructions. Only
# strong, unambiguous phrases are blocked; vocabulary words the built-in
# profiles legitimately mention (``image_prompt``, ``overlay_headline``,
# ``obviously_small_scale``) are deliberately NOT blocked.
EDITORIAL_BLOCKLIST: dict[str, tuple[str, ...]] = {
    "article_summary": ("DATABASE_ENTRY:",),
    "story_drafting": (
        "Headline:",
        "Main story:",
        "Contradictions:",
        "[[S1]]",
        "Return exactly this format:",
    ),
    "story_scale_screening": ("Return only valid JSON",),
    "title_generation": ("Return ONLY valid JSON with the key overlay_headline",),
    "image_art_direction": ("Return ONLY valid JSON with the key image_prompt",),
}


def validate_prompt_contract(task: str, rendered_text: str) -> list[str]:
    """Return the required markers missing from a rendered prompt for ``task``.

    Raises ``ValueError`` for unknown tasks (a new stage must register its
    markers before it can be validated).
    """
    if task not in PROTOCOL_MARKERS:
        raise ValueError(
            f"Unknown prompt task {task!r}; expected one of {sorted(PROTOCOL_MARKERS)}"
        )
    return [marker for marker in PROTOCOL_MARKERS[task] if marker not in rendered_text]


def assert_prompt_contract(task: str, rendered_text: str) -> None:
    """Raise ``ValueError`` listing the markers missing from ``rendered_text``."""
    missing = validate_prompt_contract(task, rendered_text)
    if missing:
        raise ValueError(f"Prompt contract violation for {task!r}; missing markers: {missing!r}")


def validate_editorial_instructions(
    instructions: Mapping[str, str],
    *,
    allow_braces_for: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Return profile-safety violations for an editorial instruction map.

    Checks that every task slot is present and non-empty, that the
    ``story_scale_screening`` slot is free of braces (the screening template
    renders via ``.format()``), and that no slot contains blocklisted contract
    language. ``allow_braces_for`` may name tasks whose renderer already
    escapes literal braces before ``.format()`` (only ``story_scale_screening``
    today); it is not a general relaxation. Returns violations instead of
    raising so callers control how the profile error surfaces (config
    resolution fail-fasts; tests assert).
    """
    allowed_brace_tasks = set(allow_braces_for or ())
    violations: list[str] = []
    for task in PROTOCOL_TASKS:
        instruction = instructions.get(task)
        if instruction is None or not str(instruction).strip():
            violations.append(f"missing or empty instructions for {task}")
            continue
        if not isinstance(instruction, str):
            violations.append(f"instructions for {task} must be a string")
            continue
        if (
            task == "story_scale_screening"
            and task not in allowed_brace_tasks
            and ("{" in instruction or "}" in instruction)
        ):
            violations.append(
                "story_scale_screening instructions contain a brace that would break .format() rendering"
            )
        for marker in EDITORIAL_BLOCKLIST.get(task, ()):
            if marker in instruction:
                violations.append(
                    f"instructions for {task} contain pipeline-owned contract language: {marker!r}"
                )
    return violations
