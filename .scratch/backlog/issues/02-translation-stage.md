# Translation pipeline stage

Status: ready-for-agent
Lights: off
Blocked by: 01-translation-policy
Tags: fusion-04
GitHub: #172

## Problem
English-only policy is recorded; the verified translation stage is not implemented.

## Acceptance
- [ ] A translation stage exists with a model assignment
- [ ] Uses 01's deterministic enable rules
- [ ] Report semantics include translation when it ran
- [ ] `.venv/bin/python3 -m pytest tests/ -q` passes
