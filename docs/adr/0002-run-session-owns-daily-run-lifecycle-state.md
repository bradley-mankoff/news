# ADR 0002: Run Session owns daily run lifecycle state

Status: Accepted

Date: 2026-06-13

## Context

The architecture review identified the Daily News Run Interface as too shallow:
the Implementation leaked import-time globals, mutable run state, model server
lifecycle, diagnostics, progress, and output paths across the pipeline.

The Run Session deepening has already begun. `RunSession` now owns one execution
of the Daily News Run and coordinates compatibility with older globals while the
rest of the pipeline migrates behind narrower Interfaces.

## Decision

Treat Run Session as the Module that owns Daily News Run lifecycle state. It
owns the Runtime Config snapshot, progress, Run Diagnostics, model-call and
activity state, run logs, output paths, and managed model server lifecycle.

CLI and compatibility entrypoints may act as Adapters into Run Session. New work
should not widen the legacy global surface when it can instead pass state through
Run Session or explicit stage inputs.

## Consequences

- Run lifecycle bugs should concentrate in Run Session rather than spreading
  across callers.
- Tests for run-level behavior should prefer the Run Session Interface.
- Compatibility globals are transitional Adapters, not extension points.
- Follow-on deepening, including Run Finalizer work, should preserve Run Session
  as the lifecycle owner.
