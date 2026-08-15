# Deterministic translation policy

Status: ready-for-agent
Lights: off
Tags: fusion-02
GitHub: #33

## Problem
Translation must be explicit: enabled AND source.language declared AND != target. No Unicode sniffing, no `_text_looks_non_english`, no source retagging. Unknown language surfaces config status.

## Acceptance
- [ ] Translation runs only when enabled + declared source lang != target
- [ ] Unknown language does not guess; status is recorded
- [ ] Provenance keeps original + translated text, langs, model, status
- [ ] `.venv/bin/python3 -m pytest tests/ -q` passes

## Out of scope
The full pipeline stage (#172 / 02-translation-stage).
