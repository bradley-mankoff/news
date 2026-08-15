---
type: Operations Concept
title: Durable Run History
description: The canonical DuckDB and CSV record of run diagnostics, collected articles, summaries, and story outcomes.
tags: [daily-news, history, duckdb, csv]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-history
    resource: ../../CONTEXT.md
    title: Daily News Context — durable run history
  - id: history-code
    resource: ../../news_pipeline/history_store.py
    title: DuckDB and CSV history store
  - id: finalizer-code
    resource: ../../news_pipeline/run_finalizer.py
    title: RunFinalizer history adapter
---

# Definition

**Durable Run History** is the canonical runtime store for reproducible run
outcomes. `output/history/news_history.duckdb` records diagnostics and
structured article/story outcomes; configured CSV exports provide a reviewable
flat projection. Article rows retain separate raw `candidate` and post-stage
`translated` records when translation runs. Translation status, reason,
source/target language, model, and bounded original/translated previews are
nullable provenance fields, so legacy rows remain readable after migration.

# Authority boundary

The OKF Run Bundle is a portable Markdown/YAML projection derived from
structured records and the rendered report. It does not replace or mutate the
DuckDB schema, CSV history, or history-store semantics.
