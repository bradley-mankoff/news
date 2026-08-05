---
type: Domain Concept
title: Delivery Profile
description: "Optional delivery policy for the Daily News Report: whether to send, owner recipient, additional recipients, and transport configuration."
tags: [daily-news, delivery, report]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-08-04T00:00:00Z}
sources:
  - id: context-delivery-profile
    resource: ../../CONTEXT.md
    title: Daily News Context — Delivery Profile
  - id: adr-desktop-first-optional-delivery
    resource: ../../docs/adr/0012-desktop-first-application-optional-delivery.md
    title: ADR 0012 — Desktop-first application with optional delivery
---

# Definition

A **Delivery Profile** is an optional policy controlling whether to send, the
owner recipient, additional recipients, and transport configuration for the
Daily News Report. Delivery Profile outcome per delivery attempt is one of
`skipped: not_configured` (missing sender, recipient, or transport
configuration), `skipped: user_disabled` (explicit no-delivery, e.g. a
`pause: true` recipient or a disabled profile), `sent` (send success), or
`failed` (send rejection or error). The delivery outcome NEVER changes the Run
Session outcome: missing or rejected email is surfaced as delivery status, not
as a failed run.

# Authority

ADR 0012 (Desktop-first application with optional delivery) and the CONTEXT.md
Delivery Profile section define the delivery vocabulary, outcome states,
precedence, and identity rules. The Delivery Profile is canonical vocabulary;
the implementation that records delivery status is named in ADR 0012's Slice B.
