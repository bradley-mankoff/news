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
  - id: model-catalog-overlay
    resource: ../../config/model_catalog.yaml
    title: User-editable Model Catalog overlay
  - id: model-catalog-code
    resource: ../../news_pipeline/model_catalog.py
    title: Model Catalog built-ins, YAML loader, and runtime-fit matching
  - id: runtime-config-adr
    resource: ../../docs/adr/0004-runtime-config-resolution-owns-env-overlays.md
    title: ADR 0004 — Runtime Config Resolution
---

# Definition

**Run Settings** are all user-controllable values for one run. A **Run Preset**
is a saved overlay. **Task Model Assignment** selects a model for a model-using task. Every actual
LLM stage has its own assignment: Article Summarization, Story Drafting, Story
Scale Screening, and Title Generation. Image Art Direction inherits the Title
Generation assignment (one shared LLM call produces both outputs), and Story
Discovery has no LLM stage (embedding/TF-IDF clustering) so it inherits the
default model. **Model Tuning** is explicit inference
configuration, **Model Defaults** fill unset values, **Pipeline Budget** covers
non-model limits, and **Model Server Settings** configure the local
OpenAI-compatible server.

These terms keep model selection, inference tuning, and pipeline limits
separate while Runtime Config Resolution produces one run snapshot.

**Model Catalog** is the code-owned baseline registry of curated models plus
an optional user-editable YAML overlay (`config/model_catalog.yaml`, issue
#90). Built-in entries stay the reviewed baseline; the overlay may change
descriptive/recommendation metadata for existing aliases (name, description,
context_length, task_notes) and add new complete entries (reference, name,
backend, hf_repo, description) with backends limited to `mlx-lm`, `mlx-vlm`,
and `external`. `reference` must equal `hf_repo` (owner/repo id; never a
file-qualified `.gguf` path). The merged catalog is a per-process snapshot
loaded when the CLI/UI starts (restart after editing) and is the single
source for CLI/UI listing, model selector options, alias resolution, backend
inference, and Hugging Face runtime-fit verdicts. YAML additions are
user-verified, not Apple-Silicon verified by this project, and never
silently become the default model.

## Sources

- `news_pipeline/model_catalog.py` — built-ins, YAML loader/validation,
  merge, runtime-fit matching
- `config/model_catalog.yaml` — user-editable overlay
- `news_pipeline/config.py` — alias resolution, backend inference, selector
  options
