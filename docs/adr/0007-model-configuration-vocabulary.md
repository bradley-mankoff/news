# ADR 0007: Model Configuration Vocabulary

Status: Proposed

Date: 2026-06-21

## Context

The current model configuration Interface is shallow. `NEWS_MODEL` chooses a
model, but it also causes Runtime Config Resolution to infer a model runtime
profile, server settings, sampling settings, token limits, and article caps.
That gives callers a small-looking Interface that hides too many unrelated
decisions behind one value.

The confusing symptom is the setup line that can report a profile such as
`big_conservative` instead of the model the user thought they selected. The
deeper problem is not just the name. A size-class profile is an inferred bundle
of opinions. It mixes model identity, Model Tuning, Pipeline Budget, and Model
Server Settings. Those concepts vary for different reasons and should not share
one implicit seam.

The UI also exposes enough raw settings to override pieces of the inferred
profile. That makes the profile neither a reliable default nor a clean user
preset. Users see too many controls, and several controls appear to overlap
because they belong to different concepts that are not named separately.

## Vocabulary

- Run Settings: every user-controllable value that shapes one Run Session.
- Run Preset: a saved Run Settings overlay in `config/run_presets.yaml`.
- Task Model Assignment: the model selected for a model-using task. Every
  actual LLM stage has its own assignment: Article Summarization, Story
  Drafting, Story Scale Screening, and Title Generation. Image Art Direction
  inherits the Title Generation assignment (one shared LLM call produces both
  outputs), and Story Discovery has no LLM stage (embedding/TF-IDF clustering)
  so it inherits the default model.
- Model Defaults: model or backend defaults used when no explicit Model Tuning
  is configured.
- Model Tuning: explicit inference settings for a selected model, such as
  sampling settings and task token limits.
- Model Tuning Preset: a saved Model Tuning overlay for one model or one
  model-task pair.
- Pipeline Budget: non-model limits such as article caps, story caps, text
  truncation, recency windows, and concurrency.
- Model Server Settings: adapter settings for the local OpenAI-compatible model
  server.
- Runtime Config Snapshot: the resolved immutable result consumed by Run
  Session.

## Decision

Runtime Config Resolution remains the Module that owns the external Interface
for Run Settings. It should stop inferring size-class model profiles from
`NEWS_MODEL`.

Model selection should be explicit through Task Model Assignment. A default run
may keep one model for all model-using tasks, but callers should be able to
select separate models for Article Summarization, Story Drafting, Story Scale
Screening, and Title Generation without forking the rest of the run.

Model Tuning should be explicit and user-owned. If a Hugging Face model page
documents recommended inference settings, the repo may carry those as named
defaults for that specific model. If the page has no guidance, Runtime Config
Resolution should let the backend/model defaults ride.

Run Presets and Model Tuning Presets are different concepts. A Run Preset
chooses a workflow. A Model Tuning Preset chooses inference settings for a
selected model or task. The UI may show both, but it should label them
separately and keep advanced Model Tuning in collapsible sections.

## Consequences

- `NEWS_MODEL` stops being a hidden bundle of model identity, tuning, budgets,
  and server settings.
- The `big_conservative` profile concept can be deleted rather than renamed.
- Model-specific settings gain Locality in Model Tuning.
- Run-wide limits gain Locality in Pipeline Budget.
- UI settings become easier to scan because controls are grouped by concept,
  not by historical environment-variable shape.
