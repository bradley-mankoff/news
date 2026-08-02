# ADR 0010: Prompt Catalog owns editorial instructions

Status: Accepted

Date: 2026-08-02

## Context

The five LLM prompt stages hardcoded editorial sentences inside stage modules,
making tone changes a code change and blocking per-stage prompt customization.

## Decision

Introduce a code-owned Prompt Catalog (`news_pipeline/prompt_catalog.py`,
stdlib-only) of built-in Prompt Profiles. Profiles provide per-task editorial
instruction sentences for article summary, story scale screening, story
drafting, title generation, and image art direction. Machine-required output
contracts (`DATABASE_ENTRY:` blocks, citation markers, strict JSON, retry
messages, scale vocabulary) live in `news_pipeline/prompt_contracts.py` (ADR
0011); stage modules compose prompts from those constants, and config
resolution validates that profile instructions never weaken them. `balanced` is
the default profile and is byte-identical to the pre-catalog prompts.

## Consequences

- Easier: tone variation, comparison, UI selection, future per-stage editing.
- Harder/off-limits: YAML/editable prompt surface until the user-editing issue;
  profiles may not weaken machine-required output contracts.
