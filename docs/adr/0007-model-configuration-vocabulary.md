# ADR 0007: Model Configuration Vocabulary

Status: Accepted

Date: 2026-08-06

## Context

Model configuration originally exposed a shallow Interface: `NEWS_MODEL`
chose a model, but it also caused Runtime Config Resolution to infer a
size-class runtime profile (reported as names such as `big_conservative`) that
bundled server settings, sampling settings, token limits, and article caps
behind one value. That gave callers a small-looking Interface that hid too many
unrelated decisions, and the UI exposed raw settings that overlapped the
inferred bundle's pieces, making the profile neither a reliable default nor a
clean user preset.

The implementation has since replaced profile inference with explicit category
seams. Model identity/task routing, Model Tuning, Pipeline Budget, and Model
Server Settings are separate frozen dataclasses in `RuntimeConfig`, and the
runtime knob registry groups settings under those same names. The decision
record stayed `Proposed` while the code shipped, so maintainers could not treat
the vocabulary as a settled architecture constraint. This record accepts the
explicit vocabulary as the load-bearing boundary between model selection,
tuning, budgets, and model-server settings. The `big_conservative` size-class
profile is historical pressure, not current runtime behavior.

## Vocabulary

- Run Settings: every user-controllable value that shapes one Run Session.
- Run Preset: a saved Run Settings overlay in `config/run_presets.yaml`.
- Task Model Assignment: the model selected for a model-using task, including
  its resolved name, backend, base URL, server command, and tuning. Every
  actual LLM stage has its own assignment: Article Summarization, Story
  Drafting, Story Scale Screening, Title Generation, and Image Art Direction
  (a separate LLM call producing the text-free FLUX prompt). Story Discovery
  has no LLM stage (embedding/TF-IDF clustering) and inherits the default
  model — the only inheritance case.
- Model Defaults: model or backend defaults used when no explicit Model Tuning
  is configured.
- Model Tuning: explicit inference settings for a selected model, such as
  sampling settings and model/task token limits.
- Model Tuning Preset: a saved Model Tuning overlay for one model or one
  model-task pair, stored in `config/model_tuning_presets.yaml`.
- Pipeline Budget: non-model limits such as article caps, story caps, text
  truncation, recency windows, thresholds, and pipeline workload concurrency.
- Model Server Settings: adapter settings for the local OpenAI-compatible model
  server, including base URLs, model-server concurrency, prefill, cache, and
  server max tokens.
- Runtime Config Snapshot: the resolved immutable result consumed by Run
  Session.

## Decision

Runtime Config Resolution (ADR 0004) remains the Module that turns base
environment values, saved run presets, and explicit overrides into one Runtime
Config Snapshot. It does not infer size-class model profiles from `NEWS_MODEL`.

Setting ownership is exactly:

1. **Model Selection / Task Model Assignment** — `NEWS_MODEL` is default model
   selection only. The five LLM stages (Article Summarization, Story Drafting,
   Story Scale Screening, Title Generation, and Image Art Direction) have
   their own assignments via `NEWS_MODEL_<TASK>`. A default run may keep one
   model for all model-using tasks, but callers can select separate models
   without forking the rest of the run. Image Art Direction is an independent
   LLM call with its own assignment; Story Discovery is algorithmic and
   inherits the default model. Model selection never infers a backend or a
   workload/server concurrency profile: an unset `NEWS_MODEL_BACKEND`
   resolves to the fixed default backend (`mlx-vlm`, ADR 0017), and a known
   catalog model whose declared backend differs must set `NEWS_MODEL_BACKEND`
   explicitly or config resolution fails with an actionable message.
   Inherited task assignments (no per-task model override) carry the resolved
   default backend so diagnostics and server commands never disagree; a
   task-specific model keeps its catalog/inferred backend metadata.
2. **Model Tuning** — sampling settings and model/task token caps, such as
   `NEWS_MODEL_MAX_INPUT_TOKENS`, `NEWS_<TASK>_MAX_TOKENS`, and
   `NEWS_MODEL_<TASK>_TEMPERATURE`. Precedence is backend/model defaults, then
   verified model-specific code defaults, then the selected Model Tuning
   Preset, then explicit `NEWS_` tuning overrides. If a Hugging Face model page
   documents recommended inference settings, the repo may carry those as named
   defaults for that specific model; otherwise backend/model defaults ride.
3. **Pipeline Budget** — article text caps, article summary caps, recency
   windows, article/story limits, story thresholds, and pipeline workload
   concurrency (`NEWS_SOURCE_COLLECTION_CONCURRENCY`,
   `NEWS_ARTICLE_SUMMARY_CONCURRENCY`, `NEWS_STORY_SYNTHESIS_CONCURRENCY`).
   Stage concurrency defaults are fixed pipeline values (`4`) for every
   model choice; model identity never changes them.
4. **Model Server Settings** — base URLs (`NEWS_MODEL_BASE_URL`,
   `NEWS_MODEL_<TASK>_BASE_URL`), model-server concurrency
   (`NEWS_MODEL_CONCURRENCY`), prefill step size, prompt cache size/bytes, and
   server max tokens. The model-server concurrency default is model-neutral:
   it derives from the fixed server default plus explicit stage worker
   counts, never from model identity.

Run Presets and Model Tuning Presets are different concepts. A Run Preset
chooses a workflow; a Model Tuning Preset chooses inference settings for a
selected model or task. The UI labels them separately and keeps advanced Model
Tuning in collapsible sections.

Supported backend policy (`mlx-lm`, `mlx-vlm`, `external`) is delegated to ADR
0017 (Runtime Matrix) and is not restated here.

Prompt Profiles are adjacent but out of boundary: they swap editorial
instruction sentences only, and their ownership belongs to the Prompt Catalog
ADR (`0018-prompt-catalog-owns-editorial-instructions.md`), not to Model
Tuning.

## Consequences

- `NEWS_MODEL` is default model selection only; it is not a hidden bundle of
  model identity, tuning, budgets, and server settings, and no size-class
  profile (such as `big_conservative`) is inferred at runtime. It also never
  selects a backend or workload/server concurrency defaults: those come from
  the fixed product default (`mlx-vlm`, stage/server concurrency `4`) or
  explicit settings.
- Model-specific settings gain Locality in Model Tuning; run-wide limits gain
  Locality in Pipeline Budget; endpoint and server behavior gain Locality in
  Model Server Settings.
- UI settings are grouped by concept — Model Selection, Model Tuning, Pipeline
  Budget, Model Server Settings — matching the runtime knob registry, so
  controls no longer appear to overlap.
- `NEWS_MODEL_STORY_DISCOVERY_*` knobs are compatibility-only: story discovery
  has no LLM stage and inherits the default model — the only inheritance case.
  Image Art Direction is a first-class assignment with its own
  `NEWS_MODEL_IMAGE_ART_DIRECTION_*` variable family.
- Run Presets and Model Tuning Presets remain distinct and are never treated
  as interchangeable.
