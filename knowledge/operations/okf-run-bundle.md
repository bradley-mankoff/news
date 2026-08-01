---
type: Operations Concept
title: OKF Run Bundle
description: A portable Open Knowledge Format v0.2 projection of one Daily News run and its structured article, story, and report concepts.
tags: [daily-news, okf, projection, provenance]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: okf-spec
    resource: https://raw.githubusercontent.com/GoogleCloudPlatform/knowledge-catalog/main/okf/SPEC.md
    title: Open Knowledge Format v0.2 specification
  - id: okf-serializer
    resource: ../../news_pipeline/okf.py
    title: Daily News OKF serializer
  - id: finalizer-code
    resource: ../../news_pipeline/run_finalizer.py
    title: RunFinalizer output adapters
  - id: finalizer-adr
    resource: ../../docs/adr/0003-run-finalization-finishes-recorded-run-outcomes.md
    title: ADR 0003 — Run finalization
  - id: projection-boundary-adr
    resource: ../../docs/adr/0008-okf-projection-source-boundary.md
    title: ADR 0008 — OKF projection boundary
---

# Definition

An **OKF Run Bundle** is written at
`output/history/okf/<run_id>/`, derived from the parent directory of the
configured history database. It contains `report.md`, `articles/`, `stories/`,
progressive-disclosure indexes, and a conformant `log.md`.

Article concepts use original article URLs as provenance. Story concepts link
to article concepts and retain safe structured metrics. The report concept
preserves the rendered report body and links to story concepts. Completed
runs use `status: stable`; failed, aborted, or unknown runs use `status: draft`.
Empty runs still receive a report concept and indexes.

# Authority boundary

`CONTEXT.md`, accepted ADRs, `news_pipeline/`, `config/`, report output, and
DuckDB/CSV history remain canonical according to their responsibilities. The
OKF Run Bundle is a portable projection for inspection and exchange, never the
canonical runtime store. The checked-in `knowledge/` bundle documents the
system/domain concepts and contains no generated run output.
