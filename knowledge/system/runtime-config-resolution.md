---
type: System Concept
title: Runtime Config Resolution
description: The resolution step that combines base environment values, saved presets, and explicit overrides into one Runtime Config snapshot.
tags: [daily-news, configuration, presets]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-runtime-config
    resource: ../../CONTEXT.md
    title: Daily News Context — Runtime Config Resolution
  - id: adr-runtime-config
    resource: ../../docs/adr/0004-runtime-config-resolution-owns-env-overlays.md
    title: ADR 0004 — Runtime Config Resolution
  - id: runtime-config-code
    resource: ../../news_pipeline/config.py
    title: RuntimeConfig and resolution code
  - id: run-presets
    resource: ../../config/run_presets.yaml
    title: Saved Run Presets
---

# Definition

**Runtime Config Resolution** produces the immutable `RuntimeConfig` snapshot
used by a Run Session. Base environment values are resolved first, the chosen
Run Preset overlays them, and explicit environment or UI overrides win over the
preset. Removed settings are rejected instead of silently changing semantics.

# Authority

`news_pipeline/config.py` owns resolution rules. `config/run_presets.yaml` is
editable input data; this concept does not copy or redefine its values. The
snapshot includes the opt-in translation enable/target settings and the
separate Translation model assignment; the assignment is resolved even when
the stage is disabled so diagnostics and previews remain reproducible.
