---
type: System Concept
title: Model Configuration Vocabulary
description: The vocabulary separating Run Settings, Run Presets, Task Model Assignments, Model Tuning, Model Defaults, Pipeline Budget, and Model Server Settings.
tags: [daily-news, models, configuration]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-model-vocabulary
    resource: ../../CONTEXT.md
    title: Daily News Context — Model Configuration Vocabulary
  - id: model-config-code
    resource: ../../news_pipeline/config.py
    title: Model and runtime configuration implementation
  - id: model-tuning-presets
    resource: ../../config/model_tuning_presets.yaml
    title: Model Tuning Presets
  - id: runtime-config-adr
    resource: ../../docs/adr/0004-runtime-config-resolution-owns-env-overlays.md
    title: ADR 0004 — Runtime Config Resolution
---

# Definition

**Run Settings** are all user-controllable values for one run. A **Run Preset**
is a saved overlay. **Task Model Assignment** selects a model for work such as
Article Summarization or Story Drafting. **Model Tuning** is explicit inference
configuration, **Model Defaults** fill unset values, **Pipeline Budget** covers
non-model limits, and **Model Server Settings** configure the local
OpenAI-compatible server.

These terms keep model selection, inference tuning, and pipeline limits
separate while Runtime Config Resolution produces one run snapshot.
