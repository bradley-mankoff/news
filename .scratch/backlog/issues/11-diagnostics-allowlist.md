# RunDiagnostics.record_report key allowlist

Status: ready-for-agent
Lights: off
Tags: oneshot
GitHub: #132

## Problem
`record_report(**details)` accepts arbitrary kwargs. Typos persist into diagnostics JSON.

## Acceptance
- [ ] Unknown keys are rejected or dropped
- [ ] Tests cover a typo'd key
