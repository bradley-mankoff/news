# ADR 0001: Record architecture decisions in docs/adr

Status: Accepted

Date: 2026-06-13

## Context

Architecture reviews for this repo use `CONTEXT.md` for domain vocabulary and
ADRs for decisions that should not be re-litigated. The repo had `CONTEXT.md`
but no durable ADR location.

## Decision

Store architecture decisions in `docs/adr/` as numbered Markdown files. Keep
each ADR focused on one decision, and update or supersede ADRs when a later
decision changes the load-bearing constraint.

Future architecture reviews should read `CONTEXT.md` and relevant ADRs before
suggesting deepening work.

## Consequences

- Architecture reviews get a stable memory of accepted tradeoffs.
- ADRs explain why a decision matters; they do not replace implementation docs.
- Rejected or superseded decisions should stay visible so the same suggestion is
  not rediscovered without new evidence.
