# ADR 0008: OKF is a portable projection, not a runtime authority

Status: Accepted

Date: 2026-07-27

## Context

Daily News has several authoritative surfaces with distinct responsibilities:
`CONTEXT.md` defines project vocabulary, ADRs record accepted architecture,
`config/` owns editable source and recipient data, `news_pipeline/` owns runtime
behavior, report files own rendered output, and DuckDB/CSV history owns durable
run records. Open Knowledge Format (OKF) v0.2 is useful for portable,
progressive-disclosure Markdown concepts with explicit provenance, but must not
create a second source of truth or alter those surfaces.

## Decision

Maintain two OKF projections:

1. `knowledge/` is a checked-in v0.2 bundle describing current system and domain
   concepts with sources back to the authoritative files.
2. `output/history/okf/<run_id>/` is a generated **OKF Run Bundle** written by
   the finalization adapter from structured Article Summary Record,
   Story Record, diagnostics, and the already-rendered report body.

The OKF serializer must use safe YAML frontmatter, valid concept links,
stable source IDs, deterministic filenames, and replace-on-success run
bundles. It must not parse report/history text as its structured input, mutate
report rendering, change the DuckDB schema, change Beehiiv output, or change
configuration/model/recipient semantics. A completed diagnostic status yields
`stable`; failed, aborted, and unknown statuses yield `draft`. Finalizer
failure is isolated from the other output adapters.

## Consequences

- Consumers can clone or inspect `knowledge/` and an individual OKF Run Bundle
  without learning the runtime store.
- Provenance points back to the current canonical code, configuration, ADRs,
  report, and history surfaces instead of duplicating their authority.
- Rerunning one run ID replaces its generated concepts and cannot leave stale
  article or story files in the final directory.
- New OKF fields or concepts are projections and require no change to the
  report format, history schema, or Beehiiv workflow.
