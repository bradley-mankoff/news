# Bind prompt profile once

Status: ready-for-agent
Lights: off
Tags: oneshot
GitHub: #107

## Problem
`_build_runtime_config` calls `get_prompt_profile()` twice. Fail-fast ValueError lacks a remediation hint.

## Acceptance
- [ ] Profile is bound once
- [ ] ValueError includes a remediation hint
- [ ] Existing tests still pass
