# Daily News Context

This project is a desktop-first Daily News Application. It generates a Daily News Report from configured sources and owns source collection, article filtering and summarization, story clustering and drafting, report generation, optional delivery, diagnostics, and durable run history.

## Context Map
- `README.md` is the human runbook for setup, CLI/UI commands, run settings, model choices, and PR review flow.
- `docs/adr/` records architecture decisions that should not be re-litigated without new evidence.
- `config/sources.yaml` is the editable Source Catalog.
- `config/recipients.yaml` is the delivery-recipients list (Delivery Profile input).
- `config/run_presets.yaml` stores saved Run Settings overlays.
- `config/model_tuning_presets.yaml` stores explicit model/task tuning overlays.
- `output/history/` contains durable run history artifacts.
- `knowledge/` is the checked-in OKF v0.2 system/domain bundle; generated run projections live at `output/history/okf/<run_id>/`.

## Run Session
A Run Session is one execution of the daily news run. It owns the run's config snapshot, output paths, progress, diagnostics, run logs, and managed model server lifecycle.

## Daily News Application
The Daily News Application is the product surface. It supports local desktop review of the generated report and optional automation; it is desktop-first, with the report rendered for review before any delivery step.

## Product Modes

The Daily News Application supports three product modes:
- **Interactive generation:** a user starts a Run Session and reviews the Daily News Report locally.
- **Scheduled generation:** automation starts Run Sessions on a schedule; local report review remains available.
- **Optional delivery:** a completed report may be sent by email to the owner or explicitly added recipients.

A Run Session succeeds when report generation and durable artifacts complete. Email delivery is an independent, optional outcome; skipped or failed delivery is surfaced as delivery status and does not change the report outcome.

## Daily News Report
The Daily News Report is the generated artifact. A completed report is complete and reviewable even when no delivery is configured.

## Delivery Profile
A Delivery Profile is an optional policy controlling whether to send, the owner recipient, additional recipients, and transport configuration. It owns transport credentials; the owner is the first-class recipient for scheduled delivery. An email sender identity belongs to transport configuration, not the report domain; it may be the same address as the owner recipient.

## Automation
Automation is a scheduled Run Session with an optional Delivery Profile.
(This is a product concept — scheduled daily runs — and is distinct from the
repo's board automation in `automation/`, which drives the GitHub project board.)

## Article Collection Funnel
The Article Collection Funnel fetches configured sources, scrapes articles, rejects source mismatches, dedupes URLs, records Source Run diagnostics, persists candidate URL history, and yields fresh article candidates.

## Source Catalog
The Source Catalog is `config/sources.yaml`. It owns source records, YAML layout, source-language tags, language retagging, and source removal.

## Runtime Config Resolution
Runtime Config Resolution turns base environment values, saved run presets, and explicit overrides into one Runtime Config snapshot before a Run Session starts. It owns setting metadata, Run Preset overlay rules, command environment deltas, and removed-setting validation.

## Model Configuration Vocabulary
Run Settings are the whole set of user-controllable values for one Run Session. A Run Preset is a saved Run Settings overlay in `config/run_presets.yaml`. Task Model Assignment chooses which model handles a model-using task: Article Summarization, Story Drafting, Story Scale Screening, and Title Generation each have their own assignment; Image Art Direction shares the Title Generation model (one shared LLM call) and Story Discovery has no LLM stage (embedding/TF-IDF clustering), so it inherits the default model. Model Tuning is explicit inference settings for a selected model. Model Defaults are backend/model defaults used when no Model Tuning is set. Pipeline Budget covers non-model limits such as article caps, story caps, text truncation, and recency windows. Model Server Settings are adapter settings for the local OpenAI-compatible model server. Model Backend is the runtime that serves a model: managed local MLX (`mlx-lm`/`mlx-vlm`) or an external OpenAI-compatible endpoint (`external`); the default model's backend comes from `NEWS_MODEL_BACKEND` (validated closed set) or is inferred from the model reference, while per-task models always use inference. Model Catalog: the code-owned curated registry of models verified for the supported backends, with per-task recommendations (factual extraction, structured output, synthesis, citation fidelity, speed, context length, translation); `config/model_catalog.yaml` is an optional user overlay that can override presentation/recommendation metadata and add new entries (validated at load; a per-process snapshot, so restart the CLI/UI after editing). Hugging Face search surfaces live metadata and runtime-fit verdicts; the catalog never promises hardware fitting beyond linking to HF model pages. A Prompt Profile is a bundle of per-task editorial instructions from the built-in Prompt Catalog, selected by `NEWS_PROMPT_PROFILE`; profiles swap editorial sentences only, never the machine-required output contracts.

## Article Summary Record
An Article Summary Record is the normalized result of summarizing one retained article. It owns article title, source, published time, URL, Article ID, story assignment, summary prose, and Markdown compatibility rendering for downstream report and history adapters.

## Story Record

A Story Record is the normalized story-level record produced by global story clustering and consumed by story drafting, story selection, budget trimming, and debug reporting. It owns story key, story title, selected Article IDs, cluster Article IDs, ranking metrics, overlap comparison, and story debug projection.

## OKF Run Bundle

An **OKF Run Bundle** is the portable Open Knowledge Format v0.2 projection of one Daily News run at `output/history/okf/<run_id>/`, derived from structured Article Summary Record, Story Record, diagnostics, and the rendered report body. It contains `report.md`, `articles/`, `stories/`, progressive-disclosure indexes, and a conformant log. `knowledge/` is the checked-in system/domain projection. `CONTEXT.md`, ADRs, `config/`, `news_pipeline/`, report output, and DuckDB/CSV history remain canonical; OKF never becomes the runtime source of truth.
