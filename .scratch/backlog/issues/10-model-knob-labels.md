# Model knob labels from runtime reference

Status: ready-for-agent
Lights: off
Tags: oneshot
GitHub: #121

## Problem
Default model-knob labels are static and can drift from the resolved model.

## Acceptance
- [ ] Labels derive from the resolved runtime model reference
- [ ] Changing the reference updates labels
- [ ] Config/UI tests cover the derivation
