# Isolate run-log sink failures

Status: ready-for-agent
Lights: off
Tags: fusion-02
GitHub: #174

## Problem
A failing log sink can mask the primary pipeline result.

## Acceptance
- [ ] Sink write/flush failure does not replace the run outcome
- [ ] Which sink failed is recorded
- [ ] Tests cover a dead sink
