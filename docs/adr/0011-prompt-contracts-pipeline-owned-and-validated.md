# ADR 0011: Prompt contracts are pipeline-owned and validated

Status: Accepted

Date: 2026-08-02

## Context

Issue #26 requires the machine-required output contracts to be separated from
the editable editorial surface: the parsers, retry loops, citation renderers,
and sanitizers depend on exact protocol text (`DATABASE_ENTRY:` blocks,
`Headline:`/`Main story:`/`Contradictions:` format, `[[S1]]` citation markers,
strict JSON for image art and scale screening, the format-error retry message).
Today those contracts are un-named inline literals buried inside the four stage
templates (`article_summarization.py`, `story_drafting.py`,
`story_selection.py`, `pipeline.py`), with no single source of truth and no
validation that a rendered prompt still contains every required marker. ADR
0010 already moved the *editorial* sentences into the Prompt Catalog; this ADR
closes the other half of the split.

## Decision

Machine contracts move out of the stage templates into a new stdlib-only
registry `news_pipeline/prompt_contracts.py` (single source of truth,
code-owned, outside the user-editable surface). It owns:

- Named contract constants — the exact protocol fragments moved verbatim from
  the stage modules (whitespace-exact; rendered prompts stay byte-identical).
- `PROTOCOL_MARKERS` — the required marker substrings per task.
- `EDITORIAL_BLOCKLIST` — strong contract sentences forbidden inside editable
  instruction strings (vocabulary words like `image_prompt` or
  `obviously_small_scale` are deliberately not blocked).
- `validate_prompt_contract(task, rendered_text)` / `assert_prompt_contract` —
  report/raise on missing markers in a rendered prompt.
- `validate_editorial_instructions(instructions)` — profile-safety checks
  (every task slot present and non-empty, no braces in the `.format()`-rendered
  screening slot, no blocklisted contract language).

Stage modules compose their prompts from the constants with byte-identical
rendered output; `config.py` fail-fasts on profile violations at runtime config
resolution; tests validate all 5 profiles × 5 stages.

## Consequences

- Single source of truth for every parsable format, retry message, citation
  marker, and JSON requirement — a template edit that drops a protocol marker
  is caught by the drift-guard tests and the shared validators.
- Profile editors learn immediately at config resolution when an instruction
  would break a contract, instead of discovering it mid-run.
- Editorial instructions remain editable via `NEWS_PROMPT_PROFILE`; the
  rendered prompts are unchanged (byte-identical).
- Image Art Direction and Title Generation are separate pipeline-owned JSON
  contracts: the image-art call requires only `image_prompt` (text-free FLUX
  prompt), and the title call requires only `overlay_headline`, which is
  rendered later by code. Each call validates its own contract independently,
  so one malformed response never suppresses the other output (issue #122).
- The `validate_prompt_contract` API is the natural hook for the later
  full-template validation work in Advanced Settings (HANDOFF.md).
