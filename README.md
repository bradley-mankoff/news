# Daily News Pipeline

Python 3.12 `uv` project for building and sending the daily news report. Run
commands from the repo root so `uv` uses this project environment.

```bash
cd /Users/bradley_mankoff/personal_code/news
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

On Apple Silicon, the platform check should print `arm64`.

## Project Automation

The repo runs a fully automated agentic loop driven by the GitHub project board
(Projects v2, project #1 “Build public UI”, owner `bradley-mankoff`).

### Board flow

- Lanes: `Backlog` → `Todo` → `In Progress` → `Ready for Review` → `In Review` → `Done`.
- Creating an issue lands it in `Backlog`; nothing starts from `Backlog`.
- Moving an issue into `Todo` triggers an Archon workflow (label-aware: `bug`
  → `archon-fix-github-issue`, `feature`/`enhancement` → `archon-idea-to-pr`,
  default → `archon-fix-github-issue`), and the poller moves the issue to
  `In Progress`.
- When the dispatched run completes, the poller moves the issue to
  `Ready for Review` — the human tests the draft PR locally from there.
- Moving an issue into `In Review` triggers `archon-smart-pr-review` on the
  linked PR.
- Agents move issues with `python3 automation/move_item.py <issue> <lane>`.

### Two review stages (by design)

1. **Readiness review** — inside the implementation workflows, before the human
   sees anything: `archon-fix-github-issue` runs a smart review (code review +
   conditional error-handling/test/comment/docs) then self-fixes and simplifies;
   `archon-idea-to-pr` runs a 5-agent review block and fixes findings. The bar:
   “the human should not have to check whether it works, is complete, or
   matches the issue intent.” Implementation PRs are left **draft** so you can
   test the branch locally first.
2. **Quality review** — the `In Review` lane trigger (`archon-smart-pr-review`):
   after you judge the feature working and move the ticket, the review targets
   code quality, conventions, and subtle/peripheral breakage, and auto-fixes
   CRITICAL/HIGH findings. It runs on the final diff — including anything you
   changed during testing.

The workflows are the stock Archon 0.6.0 pi-usable set, curated in the archon
home (`workflows/`); claude-only workflows are archived, not discovered. Full
inventory: `docs/archon-workflows.md`.

### Components

- `automation/board_poller.py` — polls the board every 45s, dispatches Archon
  runs on lane transitions (moves the item to `In Progress` on dispatch, to
  `Ready for Review` when the run completes). First poll after (re)start is a
  snapshot: state is recorded, nothing is dispatched (prevents backlog bursts
  after downtime).
- `automation/config.json` — repo, project, lanes, and workflow mapping.
- `automation/move_item.py` — move an issue to a lane from the CLI.
- The poller runs as a launchd agent (`com.bradley-mankoff.news-board-poller`,
  plist in `~/Library/LaunchAgents/`). Logs: `automation/board_poller.log`;
  state: `automation/state.json` (gitignored).
- Archon executes all workflows on DeepSeek (`deepseek/deepseek-v4-flash`,
  max effort → xhigh thinking) via the Pi provider; tiers are configured in
  the archon-pi home `config.yaml`.

### Manual review

Review a PR by hand:

```bash
archon workflow run archon-smart-pr-review "Review PR #123"
```

### Monitoring

- Archon runs: `archon workflow runs` (run from the repo root).
- Poller: `launchctl list | grep news-board-poller`, or
  `tail -f automation/board_poller.log`.

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
