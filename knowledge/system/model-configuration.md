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
Scale Screening, Title Generation, and Image Art Direction (an independent LLM
call producing the text-free FLUX prompt, issue #122). Story Discovery has no
LLM stage (embedding/TF-IDF clustering) so it inherits the default model — the
only inheritance case. **Model Tuning** is explicit inference
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
`external`, and `llama.cpp` (issue #75). Identity rules are backend-scoped:
MLX/external entries require `reference == hf_repo` (owner/repo id; never a
file-qualified `.gguf` path), while `llama.cpp` entries use a file-qualified
`owner/repo/file.gguf` reference under a bare `hf_repo` page id. The merged
catalog is a per-process snapshot loaded when the CLI/UI starts (restart
after editing) and is the single source for CLI/UI listing, model selector
options, alias resolution, backend inference, and Hugging Face runtime-fit
verdicts (`managed_mlx_lm`, `managed_mlx_vlm`, `managed_llama_cpp`,
`external_only`). YAML additions are user-verified, not
Apple-Silicon/llama-server verified by this project, and never silently
become the default model.

The managed `llama.cpp` backend (`NEWS_MODEL_BACKEND=llama.cpp`) launches an
operator-installed official native `llama-server` binary (default
`llama-server`, overridable with `NEWS_LLAMA_CPP_SERVER`) through the same
managed process/readiness/log/teardown lifecycle as MLX; the stdlib-only
adapter translates HF file-qualified references to `--hf-repo`/`--hf-file`,
bare HF repos to `--hf-repo`, and local `.gguf` paths to `--model`. The
application never downloads or installs the binary; a missing binary fails
at run launch with actionable guidance. Text-generation GGUF is managed;
multimodal GGUF (mmproj) is not.

## Sources

- `news_pipeline/model_catalog.py` — built-ins, YAML loader/validation,
  merge, runtime-fit matching
- `config/model_catalog.yaml` — user-editable overlay
- `news_pipeline/config.py` — alias resolution, backend inference, selector
  options
