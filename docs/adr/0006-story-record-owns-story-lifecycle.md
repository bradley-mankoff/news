# ADR 0006: Story Record owns story lifecycle

Status: Accepted

Date: 2026-06-14

## Context

The architecture review identified the story lifecycle as a shallow Interface:
story keys, selected Article IDs, cluster Article IDs, ranking metrics, overlap
rules, budget trimming, and debug rows were repeated as dict conventions across
story clustering, story drafting, story selection, and pipeline orchestration.

## Decision

Treat Story Record as the Module that owns story-level lifecycle behavior. It
owns story construction, ID normalization, budgeted Article ID projection,
ranking projection, overlap comparison, and debug serialization.

Legacy dicts remain compatibility Adapters at current seams while callers move
behind the Story Record Interface. Article Summary Record remains the Module
that owns article-level story assignment and report/history rendering.

## Consequences

- Story lifecycle invariants gain Locality in one Module.
- Story drafting, story selection, and pipeline orchestration can consume a
  smaller Interface instead of repeating dict key rules.
- Tests for story-level behavior should prefer Story Record over private helper
  functions in callers.
- Legacy dict Adapters may remain until pipeline stage Interfaces are narrowed.
