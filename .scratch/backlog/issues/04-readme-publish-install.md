# README install docs at publish

Status: ready-for-agent
Lights: off
Tags: oneshot
GitHub: #60

## Problem
Release-time `pip install news-pipeline` / `uv tool install` docs are missing. Premature until publish; write the section as the publish-step checklist.

## Acceptance
- [ ] README has install commands gated on the published package name (ADR 0009)
- [ ] Notes PyPI re-verify at publish
