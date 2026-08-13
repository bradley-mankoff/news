# Daily News Pipeline

Python 3.12 `uv` project for building and sending the daily news report. Run
commands from the repo root so `uv` uses this project environment.

```bash
git clone https://github.com/bradley-mankoff/news.git
cd news
uv sync
uv run pre-commit install   # secret-scanning hook (Gitleaks); runbook: docs/security/secret-prevention.md
uv run python -c 'import platform; print(platform.machine())'
```

On Apple Silicon, the platform check should print `arm64`. `uv` picks the
Python version from `.python-version` (3.12) automatically.

> **Package status**: `news-pipeline` (ADR 0009) is the public distribution
> name, but the package is not published to PyPI yet — install instructions
> will be added here at release time.

## Project Automation (product facts only)

This repo has automated issue/PR glue under `automation/`. Product workers do
not manage the board. Board policy lives outside this product tree.

### What product workers need

- Integration branch: `develop`. Production branch: `main`.
- Issue branches: `archon/task-issue-<N>` in isolated worktrees.
- Implementation PRs target `develop` and stay **draft**.
- On develop PRs/commits use `Issue: #N` — never `Fixes` / `Closes` / `Resolves`.
- Human-only product decisions: comment `NEEDS INPUT:` with 2–3 options, add
  label `needs-input`, stop coding.
- Completion records on issues must include:
  `## What shipped`, `## Decisions`, `## Acceptance criteria`,
  `## How to test`, `## Deferred work`.
- Create a shaped Backlog issue:
  `python3 automation/create_issue.py "<title>" --body "<shaped markdown>"`
- After automation/workflow install changes:
  `automation/deploy.sh`
- Reliable tests on this machine:
  `.venv/bin/python3 -m pytest tests/ -q`

### Local UI after develop merges

News keeps a dedicated clean UI runtime worktree and can restart only the UI
process it owns. Register the cmux-owned runner once:

```bash
python3 automation/news_ui_runtime.py register
```

Manual sync:

```bash
python3 automation/news_ui_runtime.py sync
```

This never rewrites a dirty developer checkout and never kills an unknown
listener on the UI port.

### Security gate

History scrub is human-gated: `automation/scrub_history.sh --dry-run`, review,
then `--execute`. Runbook: `docs/security/history-scrub.md`.
Secret prevention is automatic through the Gitleaks pre-commit hook in
`.pre-commit-config.yaml`, pinned to `v8.30.1`; install it with
`uv run pre-commit install`. It scans staged changes only and uses redacted
output. Runbook: `docs/security/secret-prevention.md`.


## UI

Start the guided local control panel from the repo root:

```bash
uv run news ui --open
```

By default it listens at `http://127.0.0.1:8766`. If you do not want the
browser opened automatically, omit `--open`:

```bash
uv run news ui
```

Use another port or host when needed:

```bash
uv run news ui --port 8770
uv run news ui --host 0.0.0.0 --port 8766
```

The UI runs until you stop it with `Ctrl-C` in the terminal. It can preview the
exact command and resolved Runtime Config Snapshot, launch and stop pipeline
runs, set `NEWS_` overrides for UI-launched commands, save/load Run Presets and
Model Tuning Presets, run source utilities, and edit `config/sources.yaml` or
`config/recipients.yaml`. Source and recipient edits write those YAML files
directly.

The **Report Review** tab is the read-only review surface for generated
reports. It shows the current report from `latest_run.md`/`latest_run_details.json`
(run id/time, run status, report status, preset/duration, and delivery status
as separate badges), lists recent completed and failed sessions from durable
DuckDB history, and can open a historical run's stable OKF `report.md`.
Report text is rendered as escaped plain text. When a run finishes in the UI,
the live stream closes and the review/history panels refresh automatically; a
completed report navigates to Report Review, while a failed run without a
report leaves you on Run Setup with the failure visible.

The **Run log** shows a concise, normalized projection of the child process:
progress meters update one line per active stage (with a small spinner glyph
while the stage is live), stage headers, warnings, retries, errors, and final
summaries append in order, and carriage returns/terminal control sequences
never reach the pane. The stream always ends with an explicit terminal line
using a restrained text glyph: `✓ [ui] completed`, `✗ [ui] failed`, or
`■ [ui] stopped` (a user-requested stop is never reported as a failure). The
glyphs are decoration only; plain counts and status words remain readable even
when glyphs are unavailable.

Report-generation status and optional email delivery status are independent:
a run with no sender/recipient/SMTP configuration finishes with delivery
`skipped: not_configured`, an explicitly disabled or all-paused delivery
finishes with `skipped: user_disabled`, and a delivery failure is recorded as
delivery `failed` without failing the run or hiding the completed report.
Delivery records include the phase and accepted/rejected recipient lists.
Runs recorded before delivery tracking show `not recorded`. The UI only reads
known rolling and OKF artifacts; it never replaces or deletes DuckDB/CSV
history or OKF bundles, and it exposes no arbitrary filesystem routes.

The main Run Setup view is prompt-first: routing, editorial prompt profile, and
default model selection. Per-task model selectors, model tuning, pipeline
budgets, clustering thresholds, server settings, full prompt templates, and raw
environment overrides live under Advanced Settings.

## Daily Automation (Schedule tab)

The **Schedule** tab enables exactly one daily personal Run Session. Pick a
local time (`HH:MM`), a saved Run Preset (or default settings), and a delivery
mode; **Enable schedule** persists the choice and installs a per-user macOS
LaunchAgent. The UI does not need to stay open: `launchd` starts
`news schedule run` once per day in your local time zone. Disabling boots out
the agent and removes the plist; repeated enable/update/disable is idempotent.

```bash
uv run news schedule status [--json]
uv run news schedule enable --time 07:30 --preset NAME --delivery-mode owner
uv run news schedule disable
uv run news schedule run   # foreground entry point used by launchd
```

Scheduled runs default to **owner-only** delivery; `disabled` and explicit
configured-recipient delivery remain available as opt-ins. Delivery outcome
stays independent of the run/report outcome. A scheduled run writes the same
canonical artifacts as a manual run: `output/daily_outputs/latest_run.*`,
DuckDB/CSV history, and the OKF bundle under
`output/history/okf/<run_id>/`. The Schedule tab shows enabled/disabled,
launchd loaded/not-loaded/unavailable, next daily time, running state, and the
last run id/time/run/report/delivery status; Report Review remains the report
surface and links from the Schedule tab.

Schedule state lives at `~/.config/news/daily_schedule.json` and the plist at
`~/Library/LaunchAgents/com.bradley-mankoff.news-daily-run.plist` (both `0600`;
the state directory is `0700`). Credentials and API keys are never written to
the schedule state, plist, API responses, or logs — the ignored local
`env.json` password fallback remains the only persisted SMTP credential input.
Non-macOS or missing `launchctl` reports an unavailable state instead of
pretending the schedule is active. Exactly one schedule at one local time is
supported: no weekly recurrence, per-day schedules, or cron expressions, and
launchd runs only for the logged-in user session.

Product Daily Automation is distinct from the GitHub-board automation under
`automation/`; the board automation is unchanged. See
[`docs/adr/0013-local-daily-automation-uses-launchagent.md`](docs/adr/0013-local-daily-automation-uses-launchagent.md)
for the accepted operational decision.

## CLI

Run with a saved preset or explicit overrides:

```bash
uv run news run --preset NAME
NEWS_SOURCE_SCOPE=peripheral NEWS_DELIVERY_MODE=owner uv run news run
```

Useful utility commands:

```bash
uv run news model-server-command
uv run news check-sources --only-failures
uv run news prune-sources --recent-days 7
uv run news source-languages --sources-yaml config/sources.yaml --json
uv run news serve-unsubscribe
uv run news schedule status
```

Daily Automation commands are documented in the
[Daily Automation (Schedule tab)](#daily-automation-schedule-tab) section.

## Run Settings

Most Run Settings are controlled by `NEWS_` environment variables. The core
ones are Run Preset selection, delivery mode and source scope, URL reuse
blocking, model selection, and image generation.

The accepted vocabulary separating Run Presets, Task Model Assignment, Model
Tuning, Pipeline Budget, and Model Server Settings is defined in
[`docs/adr/0007-model-configuration-vocabulary.md`](docs/adr/0007-model-configuration-vocabulary.md).

When running from a shell, put `NEWS_` assignments on the same command line or
export them first:

```bash
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news run
export NEWS_MODEL=gemma-e2b-tiny
uv run news run
```

### Run Presets

Run Presets live in `config/run_presets.yaml` as env-style Run Settings maps.
Preset IDs are opaque data; the code applies the selected Run Preset and then
applies any explicit shell/UI overrides on top.

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-4-12b-it-4bit NEWS_IMAGE_ENABLED=1 uv run news run
```

Key Run Settings:

- `NEWS_SOURCE_SCOPE=core|peripheral`: `peripheral` includes both core and
  peripheral sources.
- `NEWS_DELIVERY_MODE=disabled|owner|recipients`: optional email delivery
  policy. `owner` (the default) sends only to `NEWS_PRIMARY_RECIPIENT`;
  `recipients` is an explicit opt-in that sends to active entries in
  `config/recipients.yaml` (with the owner included only when listed). An
  explicitly configured `NEWS_EMAIL_RECIPIENTS` fallback is used only when
  the catalog is empty; a non-empty catalog with every entry paused records
  `skipped: user_disabled`. `disabled` sends nothing and records
  `skipped: user_disabled`. Legacy `NEWS_RECIPIENT_SCOPE=primary|all` still
  maps to `owner|recipients` when the new mode is unset.
- `NEWS_BLOCK_REUSED_URLS=0|1`: every run records URL history; only `1` makes
  previously recorded URLs block future reuse.
- `NEWS_IMAGE_ENABLED=0|1`: report image generation, default off unless a
  preset enables it.
- `NEWS_MODEL`: default model selection only. Task models are assigned
  separately with `NEWS_MODEL_ARTICLE_SUMMARY`, `NEWS_MODEL_STORY_DRAFTING`,
  `NEWS_MODEL_STORY_SCALE_SCREENING`, and `NEWS_MODEL_TITLE_GENERATION`.
  Stages with no LLM call of their own inherit a task model: image art
  direction runs on the Title Generation model (one shared LLM call), and
  story discovery has no LLM stage (embedding/TF-IDF clustering) so it
  inherits the default model.
- `NEWS_MODEL_BACKEND`: optional backend override for the default model
  (`mlx-lm`, `mlx-vlm`, or `external`; inferred from the model reference
  otherwise — see [Runtime Matrix](#runtime-matrix)).

### Prompt Profiles

Prompt Profiles are built-in editorial tone bundles for the five LLM prompt
stages (article summary, story scale screening, story drafting, title
generation, image art direction). They swap editorial instruction sentences
only; the pipeline's machine-required output contracts are unchanged. Prompt
Profile ownership is governed by the Prompt Catalog ADR
([`docs/adr/0010-prompt-catalog-owns-editorial-instructions.md`](docs/adr/0010-prompt-catalog-owns-editorial-instructions.md)),
not by Model Tuning.

```bash
uv run news run --prompt-profile playful
NEWS_PROMPT_PROFILE=facts-only uv run news run
```

Built-in profiles: `balanced` (default), `consensus-and-contradiction`,
`playful`, `facts-only`, `explain-like-im-five`. The UI's "Editorial approach"
panel selects a profile, edits per-stage prompts (defaults visible), and
restores defaults per stage or globally. Per-stage edits are stored in
`NEWS_PROMPT_OVERRIDE_<TASK>` env vars and layer on top of the selected
profile (override wins). The full per-task prompt templates and diffs against
`balanced` are under Advanced Settings. Profiles can also be pinned inside a
Run Preset's `env` map.

### Model Selection

```bash
NEWS_MODEL=gemma-e2b-tiny uv run news run
NEWS_MODEL=gemma-4-12b-it-4bit uv run news run --preset NAME
```

Task-specific model assignments inherit from `NEWS_MODEL` unless you set them
```bash
NEWS_MODEL_ARTICLE_SUMMARY=gemma-e2b-tiny uv run news run
NEWS_MODEL_STORY_DRAFTING=gemma-4-12b-it-4bit uv run news run --preset NAME
NEWS_MODEL_STORY_SCALE_SCREENING=gemma-e2b-tiny uv run news run
NEWS_MODEL_TITLE_GENERATION=gemma-4-12b-it-4bit uv run news run --preset NAME
```
Every actual LLM stage has its own assignment: Article Summarization, Story
Drafting, Story Scale Screening, and Title Generation. Two stages inherit by
design: `image_art_direction` shares the Title Generation LLM call (one prompt
produces both the art direction and the overlay headline, so it runs on the
Title Generation model), and `story_discovery` has no LLM stage — it is
algorithmic embedding/TF-IDF clustering and inherits the default model. There
is no `NEWS_MODEL_IMAGE_ART_DIRECTION` env var.
Built-in aliases:

- `gemma-e2b-tiny`: [`deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`](https://huggingface.co/deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit) (Codex-safe test model)
- `gemma-4-12b-it-4bit`: [`mlx-community/gemma-4-12B-it-4bit`](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit) (default; the standard Gemma 4 12B instruction model, 256K-token context)

The legacy `qwythos-9b-*` aliases are **unsupported**: mlx-vlm cannot launch
file-qualified GGUF references, so stale configs fail fast with an actionable
error instead of a half-started server.

Each model page shows Hugging Face's native Hardware Compatibility panel
(GGUF/MLX quantizations) — the UI model picker links directly to it.

### Model Catalog

The Model Catalog is the code-owned baseline registry: built-in models are
verified for the supported backends, while user-overlay entries remain
advisory. It provides recommendations per task — factual extraction,
structured output, synthesis, citation fidelity, speed, context length, and
translation — rather than parameter count or popularity:

```bash
uv run news models catalog
uv run news models search --query gemma --task text-generation --limit 5
```

Curated models (2):

- `gemma-4-12b-it-4bit` — mlx-vlm, 256K-token context, default model
  ([Hugging Face](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit))
- `gemma-e2b-tiny` — mlx-lm, Codex-safe test model
  ([Hugging Face](https://huggingface.co/deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit))

Hugging Face search results carry runtime-fit verdicts (`managed_mlx_lm`,
`managed_mlx_vlm`, or `external_only`) so unlaunchable repos are never picked
for a managed backend (ADR 0010 runtime matrix); hardware fitting itself lives
on the Hugging Face model page. The UI's "Model catalog" panel shows curated
cards, task recommendations, and search with the same verdicts.

#### User-editable YAML overrides

`config/model_catalog.yaml` is an optional, user-editable overlay on top of
the code-owned built-in entries. The catalog is a per-process snapshot:
built-ins first, then YAML metadata overrides and new entries. Editing the
file requires restarting `news` or the UI (no hot reload).

- Existing built-in aliases may override only `name`, `description`,
  `context_length`, and `task_notes` (task notes merge by task key); runtime
  identity (`reference`/`backend`/`hf_repo`) must be absent or match the
  built-in entry exactly.
- New aliases must provide `reference`, `name`, `backend`, `hf_repo`, and
  `description`. `backend` is limited to `mlx-lm`, `mlx-vlm`, and `external`;
  `reference` must equal `hf_repo` (an owner/repo id — file-qualified `.gguf`
  references are rejected, ADR 0010). `context_length` is optional and
  `task_notes` defaults to `{}`.
- Aliases must match the safe pattern (lowercase letters, digits, `.`, `_`,
  `-`, starting with a letter or digit). Unknown top-level keys, entry
  fields, and recommendation tasks are errors: malformed or unsafe YAML
  fails closed with a path-specific message instead of silently changing the
  catalog.
- `NEWS_MODEL_CATALOG_YAML` selects an alternate path; relative paths resolve
  from the repository root. The default path is `config/model_catalog.yaml`.
- YAML additions are user-verified, not Apple-Silicon verified by this
  project: they are selectable via `NEWS_MODEL` (or a preset) but never
  silently become the default, and runtime-fit verdicts stay advisory.

Example overlay:

```yaml
models:
  my-mlx-model:
    reference: mlx-community/example-model
    name: Example MLX Model
    backend: mlx-lm
    hf_repo: mlx-community/example-model
    context_length: 8192
    description: A user-verified MLX language model.
```

See [`docs/adr/0014-model-catalog-yaml-overrides.md`](docs/adr/0014-model-catalog-yaml-overrides.md)
for the accepted architecture record.

### Runtime Matrix

Initially supported runtimes (recorded in
[`docs/adr/0010-runtime-matrix.md`](docs/adr/0010-runtime-matrix.md)):

- `mlx-lm` — managed local MLX language-model server on Apple Silicon.
- `mlx-vlm` — managed local MLX vision-language-model server on Apple Silicon.
- `external` — any OpenAI-compatible endpoint.

Managed cross-platform GGUF via `llama.cpp` is **not** initially supported;
GGUF files are not launchable by any managed backend (file-qualified GGUF
references raise `HFValidationError` in `mlx-vlm`), so curated defaults are
MLX repo ids and GGUF repos are `external_only` for the model picker.

The default model's backend is inferred from the model reference unless
`NEWS_MODEL_BACKEND` is set to `mlx-lm`, `mlx-vlm`, or `external` (any other
value fails fast). To run the default model against an external
OpenAI-compatible endpoint — no managed server is started; the pipeline waits
for and probes the endpoint:

```bash
NEWS_MODEL_BACKEND=external NEWS_MODEL_BASE_URL=https://api.example.com/v1 NEWS_MODEL=<server-model-id> uv run news run
```

Authenticated endpoints are supported by setting `NEWS_MODEL_API_KEY`; it is
sent as a `Bearer` token on `/models` and `/chat/completions` requests (unset
sends no credentials). An endpoint that rejects the request with HTTP 401/403
fails fast instead of waiting out the readiness deadline.

`news model-server-command` reports that no managed server command exists for
the external backend. Per-task models can also use external endpoints by
giving that task a distinct base URL (`NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`,
`NEWS_MODEL_STORY_DRAFTING_BASE_URL`, `NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL`,
`NEWS_MODEL_TITLE_GENERATION_BASE_URL`).

Normal report runs start the matching local MLX server, wait until it is ready,
run the pipeline, and stop the managed server when the run exits. To keep a
server warm manually, print the matching command and run it in another terminal:

```bash
NEWS_MODEL=gemma-4-12b-it-4bit uv run news model-server-command
```

If Article Summarization, Story Drafting, Story Scale Screening, or Title
Generation uses a different model, give that
task a matching base URL or run it on an externally managed server. The current
runtime supports one managed local server per shared model/base URL; it does not
automatically coordinate multiple local servers for one run.

### Model Tuning

Model Tuning Presets live in `config/model_tuning_presets.yaml`. They are saved
overlays for one model or one model-task pair and are separate from Run
Presets.

Use these env vars to select a preset:

- `NEWS_MODEL_TUNING_PRESET`
- `NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET`
- `NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET`
- `NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET`
- `NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET`

Precedence is:

1. Backend/model defaults when a tuning field is unset.
2. Verified model-specific code defaults, if any exist.
3. The selected Model Tuning Preset.
4. Explicit `NEWS_` tuning overrides.

Direct tuning overrides still win, such as `NEWS_MODEL_MAX_INPUT_TOKENS`,
`NEWS_ARTICLE_SUMMARY_MAX_TOKENS`, `NEWS_STORY_DRAFTING_MAX_TOKENS`,
`NEWS_STORY_SCALE_SCREENING_MAX_TOKENS`, `NEWS_TITLE_GENERATION_MAX_TOKENS`, and
sampling env vars like `NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE`.

### Pipeline Budget

Pipeline Budget settings are separate from model selection and tuning. They
cover article text caps, article summary caps, recency windows, article/story
limits, and story thresholds.

### Model Server Settings

Model Server Settings control the local MLX/OpenAI-compatible server
configuration:

- `NEWS_MODEL_BASE_URL`
- `NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`
- `NEWS_MODEL_STORY_DRAFTING_BASE_URL`
- `NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL`
- `NEWS_MODEL_TITLE_GENERATION_BASE_URL`
- `NEWS_MODEL_SERVER_PREFILL_STEP_SIZE`
- `NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE`
- `NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES`
- `NEWS_MODEL_SERVER_MAX_TOKENS`

The base URL also determines the printed server port. If you point a task model
at a different base URL, the task needs its own matching server endpoint.

### Image

`NEWS_IMAGE_ENABLED` controls report image generation:

```bash
NEWS_IMAGE_ENABLED=0 uv run news run --preset NAME
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news run
NEWS_IMAGE_ENABLED=1 uv run news run --preset NAME
```

Image generation defaults off unless enabled by a preset or explicit override.
The image model, dimensions, crop, step count, and fail-open behavior are
hard-coded defaults rather than normal Run Settings.

## Configuration

- `config/sources.yaml`: single source list. Normal runs select active English
  sources using `NEWS_SOURCE_SCOPE`.
- `config/recipients.yaml`: public template for the additional-recipient
  catalog. `NEWS_DELIVERY_MODE=recipients` selects active entries; the owner
  is configured with `NEWS_PRIMARY_RECIPIENT` and is included only when
  listed. An explicitly configured legacy `NEWS_EMAIL_RECIPIENTS` list is
  used only when the catalog is empty; an all-paused catalog remains
  `skipped: user_disabled`. Real addresses belong in a local file referenced
  by `NEWS_RECIPIENTS_YAML`, never in tracked config.
- `config/model_tuning_presets.yaml`: saved Model Tuning Presets keyed by id.

Normal collection accepts active English sources. Removed topic-scoped runtime
variables and source topic fields are rejected when present.

## Outputs

Current run review files are written under `output/daily_outputs/`:

- `latest_run.md`: latest human-readable report.
- `latest_run.log`: latest concise normalized run log. Progress meters are
  compacted to stage start/final counts, carriage returns and ANSI control
  sequences are removed, and warnings, retries, errors, final summaries, and
  full failure tracebacks remain readable. The per-run timestamped counterpart
  is written to `.staging/<run-date>/run_log_<run-id>.log` with the same
  concise policy and is passed to DuckDB history.
- `latest_run_details.json`: latest backend audit details (includes the
  normalized delivery outcome — status, reason, phase, and
  accepted/rejected recipients — when a delivery attempt was possible). This
  and the managed `model_server.log` are the detailed diagnostic sources; the
  concise run log is not a raw transcript.

Durable run history is written to `output/history/news_history.duckdb`, with CSV
exports in `output/history/` for quick review. The DuckDB `run_logs` table
stores the normalized concise log content (CR/ANSI artifacts and repeated meter
snapshots removed at ingest, including for backfilled runs) and its `byte_count`
is the stored UTF-8 length. Each `runs` row carries the run status, report
metadata, and an independent `delivery_status`/`delivery` record (`sent`,
`skipped: not_configured`, `skipped: user_disabled`, or `failed`; older rows
read as `not recorded`). A run with a non-empty newsletter body also writes
paste-ready Markdown to `output/beehiiv/YYYY-MM-DD.md` for manual publication.

### Open Knowledge Format projections

The pipeline also writes two portable OKF v0.2 bundle forms:

- `knowledge/` is the checked-in system/domain knowledge bundle. It documents current concepts and links back to `CONTEXT.md`, accepted ADRs, `news_pipeline/`, `config/`, and runtime stores; it contains no generated run output.
- `output/history/okf/<run_id>/` is the generated **OKF Run Bundle** for one run, derived from structured Article Summary Record and Story Record data plus the rendered report body. It contains `report.md`, `articles/`, `stories/`, progressive-disclosure indexes, and `log.md`.

These are portable projections, not a second source of truth. Runtime behavior remains in `news_pipeline/`, vocabulary and accepted decisions remain in `CONTEXT.md` and `docs/adr/`, editable inputs remain in `config/`, the report remains the rendered output, and DuckDB/CSV remain canonical run history. The generated `log.md` is a short directory-update projection (per ADR 0008) and is never used as a run-log sink. A completed diagnostic run is `stable`; failed, aborted, or unknown runs are `draft`.

History maintenance:

```bash
uv run news history backfill --dry-run
uv run news history backfill --apply
uv run news history cleanup --dry-run
uv run news history cleanup --apply
uv run news history export
```

## License

Licensed under the [Apache License 2.0](LICENSE).

## Fast Test Run

For a quick local test that minimizes runtime and sends to a single
recipient, use the `dev` preset:

```bash
uv run news run --preset dev
```

The `dev` preset:

- Uses `gemma-e2b-tiny` (the smallest model — the only one we keep for
  local testing now that the standard Gemma 4 12B model is the default).
- Sets `NEWS_SOURCE_SCOPE=core` (the narrowest source pool).
- Sets `NEWS_RECIPIENT_SCOPE=primary` (legacy scope; maps to
  `NEWS_DELIVERY_MODE=owner`, sending only to the primary/owner recipient).
- Disables image generation and URL reuse blocking.
- Sets `NEWS_MIN_ARTICLES_PER_STORY=2` and relaxes story drafting guards.

For even faster runs, override the model explicitly and tighten the
recency window:

```bash
NEWS_MODEL=gemma-e2b-tiny NEWS_RECENT_WINDOW_HOURS=6 uv run news run
```

To preview the resolved config before launching a run, use the UI or
the model-server command:

```bash
uv run news model-server-command
```
