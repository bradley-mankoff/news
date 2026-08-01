# Daily News Pipeline

Python 3.12 `uv` project for building and sending the daily news report. Run
commands from the repo root so `uv` uses this project environment.

```bash
cd /Users/home/personal_code/news
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

On Apple Silicon, the platform check should print `arm64`.

## PR Review Flow

The Claude PR reviewer is intentionally not push-triggered. Normal pushes to an
open PR should run regular checks, but should not start a fresh AI review loop.

Use this flow:

1. Push your feature branch as often as needed.
2. Open the PR as a draft while the branch is still being shaped.
3. Mark the PR ready for review when you want one Claude review pass.
4. Fix the review comments and push the fixes to the same branch.
5. Do not expect Claude to rerun on that fix push.
6. To request another review manually, open GitHub Actions, choose
   `Claude PR Review`, click `Run workflow`, and enter the PR number.

Manual CLI trigger:

```bash
gh workflow run "Claude PR Review" -f pr_number=123
```

Opening a non-draft PR also starts one review pass. Reopening a non-draft PR
starts one review pass. Draft PRs are skipped until they are marked ready.

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

## CLI

Run with a saved preset or explicit overrides:

```bash
uv run news run --preset NAME
NEWS_SOURCE_SCOPE=peripheral NEWS_RECIPIENT_SCOPE=bradley uv run news run
```

Useful utility commands:

```bash
uv run news model-server-command
uv run news check-sources --only-failures
uv run news prune-sources --recent-days 7
uv run news source-languages --sources-yaml config/sources.yaml --json
uv run news serve-unsubscribe
```

## Run Settings

Most Run Settings are controlled by `NEWS_` environment variables. The core
ones are Run Preset selection, source/recipient scope, URL reuse blocking, model
selection, and image generation.

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
NEWS_MODEL=qwythos-9b-8bit NEWS_IMAGE_ENABLED=1 uv run news run
```

Key Run Settings:

- `NEWS_SOURCE_SCOPE=core|peripheral`: `peripheral` includes both core and
  peripheral sources.
- `NEWS_RECIPIENT_SCOPE=bradley|all`: send to Bradley only or all active
  configured recipients.
- `NEWS_BLOCK_REUSED_URLS=0|1`: every run records URL history; only `1` makes
  previously recorded URLs block future reuse.
- `NEWS_IMAGE_ENABLED=0|1`: report image generation, default off unless a
  preset enables it.
- `NEWS_MODEL`: default model selection only. Task models are assigned
  separately with `NEWS_MODEL_ARTICLE_SUMMARY` and
  `NEWS_MODEL_STORY_DRAFTING`.

### Model Selection

```bash
NEWS_MODEL=gemma-e2b-tiny uv run news run
NEWS_MODEL=qwythos-9b-8bit uv run news run --preset NAME
NEWS_MODEL=qwythos-9b-4bit uv run news run --preset NAME
```

Task-specific model assignments inherit from `NEWS_MODEL` unless you set them
```bash
NEWS_MODEL_ARTICLE_SUMMARY=gemma-e2b-tiny uv run news run
NEWS_MODEL_STORY_DRAFTING=qwythos-9b-8bit uv run news run --preset NAME
```
Built-in aliases:

- `gemma-e2b-tiny`: `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit` (Codex-safe test model)
- `qwythos-9b-4bit`: `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q4_K.gguf`
- `qwythos-9b-8bit`: `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q8_0.gguf` (default)

Normal report runs start the matching local MLX server, wait until it is ready,
run the pipeline, and stop the managed server when the run exits. To keep a
server warm manually, print the matching command and run it in another terminal:

```bash
NEWS_MODEL=qwythos-9b-8bit uv run news model-server-command
```

If Article Summarization or Story Drafting uses a different model, give that
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

Precedence is:

1. Backend/model defaults when a tuning field is unset.
2. Verified model-specific code defaults, if any exist.
3. The selected Model Tuning Preset.
4. Explicit `NEWS_` tuning overrides.

Direct tuning overrides still win, such as `NEWS_MODEL_MAX_INPUT_TOKENS`,
`NEWS_ARTICLE_SUMMARY_MAX_TOKENS`, `NEWS_STORY_DRAFTING_MAX_TOKENS`, and
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
- `config/recipients.yaml`: active and paused recipients. `NEWS_RECIPIENT_SCOPE`
  chooses Bradley-only or all active recipients.
- `config/model_tuning_presets.yaml`: saved Model Tuning Presets keyed by id.

Normal collection accepts active English sources. Removed topic-scoped runtime
variables and source topic fields are rejected when present.

## Outputs

Current run review files are written under `output/daily_outputs/`:

- `latest_run.md`: latest human-readable report.
- `latest_run.log`: latest captured terminal log.
- `latest_run_details.json`: latest backend audit details.

Durable run history is written to `output/history/news_history.duckdb`, with CSV
exports in `output/history/` for quick review. A run with a non-empty newsletter
body also writes paste-ready Markdown to `output/beehiiv/YYYY-MM-DD.md` for
manual publication.

### Open Knowledge Format projections

The pipeline also writes two portable OKF v0.2 bundle forms:

- `knowledge/` is the checked-in system/domain knowledge bundle. It documents current concepts and links back to `CONTEXT.md`, accepted ADRs, `news_pipeline/`, `config/`, and runtime stores; it contains no generated run output.
- `output/history/okf/<run_id>/` is the generated **OKF Run Bundle** for one run, derived from structured Article Summary Record and Story Record data plus the rendered report body. It contains `report.md`, `articles/`, `stories/`, progressive-disclosure indexes, and `log.md`.

These are portable projections, not a second source of truth. Runtime behavior remains in `news_pipeline/`, vocabulary and accepted decisions remain in `CONTEXT.md` and `docs/adr/`, editable inputs remain in `config/`, the report remains the rendered output, and DuckDB/CSV remain canonical run history. A completed diagnostic run is `stable`; failed, aborted, or unknown runs are `draft`.

History maintenance:

```bash
uv run news history backfill --dry-run
uv run news history backfill --apply
uv run news history cleanup --dry-run
uv run news history cleanup --apply
uv run news history export
```

## Fast Test Run

For a quick local test that minimizes runtime and sends to a single
recipient, use the `dev` preset:

```bash
uv run news run --preset dev
```

The `dev` preset:

- Uses `gemma-e2b-tiny` (the smallest model — the only one we keep for
  local testing now that the 12b/26b gemma slots are filled by Qwythos).
- Sets `NEWS_SOURCE_SCOPE=core` (the narrowest source pool).
- Sets `NEWS_RECIPIENT_SCOPE=bradley` (sends to the single `bradley@…`
  recipient only).
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
