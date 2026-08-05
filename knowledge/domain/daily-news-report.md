---
type: Domain Concept
title: Daily News Report
description: The rendered newsletter body assembled from selected story and article records for configured recipients.
tags: [daily-news, report, delivery]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-report-boundary
    resource: ../../CONTEXT.md
    title: Daily News Context — report and delivery ownership
  - id: report-rendering-code
    resource: ../../news_pipeline/pipeline.py
    title: Report rendering and delivery orchestration
  - id: finalizer-adr
    resource: ../../docs/adr/0003-run-finalization-finishes-recorded-run-outcomes.md
    title: ADR 0003 — Run finalization
  - id: recipients-yaml
    resource: ../../config/recipients.yaml
    title: Configured report recipients
---

# Definition

The **Daily News Report** is the user-facing Markdown newsletter body produced
from selected story/article records and delivered to configured recipients. It
is preserved verbatim by the latest-run review, Beehiiv paste adapter, and the
`report.md` concept in an OKF Run Bundle. A completed report is complete and
reviewable without any delivery configuration; delivery outcome is tracked
separately from the Run Session outcome.

# Authority

Report rendering and delivery code, recipient configuration, and the durable
run history define operational behavior. OKF adds links and provenance around
the rendered body; it does not replace report rendering or recipient semantics.
Delivery behavior and vocabulary follow ADR 0012 and the Delivery Profile
concept.
