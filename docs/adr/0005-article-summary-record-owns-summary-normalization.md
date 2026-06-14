# ADR 0005: Article Summary Record owns summary normalization

Status: Accepted

Date: 2026-06-14

## Context

The architecture review identified article summaries as a shallow Interface:
callers treated a Markdown convention as the record shape, so metadata parsing,
summary extraction, Article ID lookup, story assignment, debug rows, citation
sources, and report rendering each had to know parser quirks.

## Decision

Treat Article Summary Record as the Module that owns article summary
normalization. It owns parsing model Markdown, rendering compatibility Markdown,
Article ID lookup, story assignment copies, citation-source conversion, history
rows, and low-confidence summary checks.

Markdown remains an Adapter for the model prompt and legacy report rendering.
Run Session remains the run lifecycle owner, and Run Finalizer remains the owner
of writing recorded run outcomes.

## Consequences

- Parser behavior gains Locality in one Module.
- Story drafting, story selection, citation helpers, and run finalization can
  consume a smaller Interface.
- Tests can verify article summary behavior through Article Summary Record
  rather than repeating Markdown fixtures across Modules.
- Legacy Markdown helpers may remain as transitional Adapters, but new article
  summary behavior should prefer Article Summary Record.
