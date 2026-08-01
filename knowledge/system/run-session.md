---
type: System Concept
title: Run Session
description: One execution of the Daily News run with its configuration, progress, diagnostics, paths, and managed model lifecycle.
tags: [daily-news, runtime, lifecycle]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-run-session
    resource: ../../CONTEXT.md
    title: Daily News Context — Run Session
  - id: adr-run-session
    resource: ../../docs/adr/0002-run-session-owns-daily-run-lifecycle-state.md
    title: ADR 0002 — Run Session lifecycle
  - id: run-session-code
    resource: ../../news_pipeline/pipeline.py
    title: Pipeline RunSession implementation
---

# Definition

A **Run Session** owns the lifecycle of one daily news execution: the resolved
settings snapshot, output paths, progress reporting, diagnostics, run log, and
managed model-server lifecycle. It delegates domain work to collection,
summary, clustering, story selection, report, and finalization modules.

# Authority

The runtime implementation in `news_pipeline/pipeline.py` and the accepted
Run Session ADR define behavior. This concept is a portable explanation, not a
replacement for runtime state.
