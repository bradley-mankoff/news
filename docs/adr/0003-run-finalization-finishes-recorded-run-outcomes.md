# ADR 0003: Run finalization finishes recorded run outcomes

Status: Accepted

Date: 2026-06-13

## Context

The architecture review identified Run Finalizer as the next strong deepening
candidate after Run Session. Today, early returns and failures still need to know
which article candidates, summaries, selected stories, reports, artifacts, and
diagnostic details must cross the finalization Seam.

That makes the finalization Interface shallow: callers assemble finalization
payloads at the moment they abort or complete, even though those outcomes were
created earlier in the Run Session.

## Decision

Deepen Run Finalizer around recorded run outcomes. Pipeline stages should record
diagnostics, summaries, reports, artifacts, and failure or abort events as those
outcomes happen. Run Finalizer should then finish the Run Session once, using the
accumulated state to write durable records.

Run Finalizer owns failed-run behavior, rolling run details, run review output,
and Run History status import. Local filesystem paths and DuckDB history are
local-substitutable Adapters for tests.

## Consequences

- Early aborts and failures stop rebuilding finalization payloads manually.
- Failed-run behavior gains Locality in one Module.
- Finalizer tests can cover completed, aborted, and failed runs through the same
  behavior surface.
- Until this deepening is implemented, legacy calls may remain, but new work
  should not widen `_write_run_diagnostics` or add more caller-specific
  finalization arguments.
