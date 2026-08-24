# Advanced knobs from registry

Status: ready-for-agent
Lights: off
Tags: fusion-02
GitHub: #114

## Problem
Advanced Settings knobs are hand-listed. New registry knobs need a second UI list.

## Acceptance
- [ ] Advanced panel renders knobs from the shared registry
- [ ] A new registry knob appears in the right family without a second list
- [ ] `.venv/bin/python3 -m pytest tests/test_ui.py tests/test_config_helpers.py -q` passes
