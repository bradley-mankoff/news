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

Start the local control panel from the repo root:

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
exact command and resolved runtime config, launch and stop pipeline runs, set
`NEWS_` overrides for UI-launched commands, save/load a browser-local env preset,
run source utilities, and edit `config/sources.yaml` or `config/recipients.yaml`.
Source and recipient edits write those YAML files directly.

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

## Runtime Variables

Most runtime behavior is controlled by `NEWS_` environment variables. The core
ones are preset selection, source/recipient scope, URL reuse blocking, model,
and image generation.

### Run Presets

Saved presets live in `config/run_presets.yaml` as env-style knob maps. Preset
IDs are opaque data; the code applies the selected preset and then applies any
explicit shell/UI overrides on top.

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-26b-moe NEWS_IMAGE_ENABLED=1 uv run news run
```

Key run-shaping knobs:

- `NEWS_SOURCE_SCOPE=core|peripheral`: `peripheral` includes both core and
  peripheral sources.
- `NEWS_RECIPIENT_SCOPE=bradley|all`: send to Bradley only or all active
  configured recipients.
- `NEWS_BLOCK_REUSED_URLS=0|1`: every run records URL history; only `1` makes
  previously recorded URLs block future reuse.
- `NEWS_IMAGE_ENABLED=0|1`: report image generation, default off unless a
  preset enables it.
- `NEWS_MODEL`: friendly alias or full model repo/name. Backend and runtime
  profile are inferred from the selected model.

### Model

`NEWS_MODEL` selects a friendly alias or a full model repo/name:

```bash
NEWS_MODEL=gemma-e2b-tiny uv run news run
NEWS_MODEL=gemma-26b-moe uv run news run --preset NAME
NEWS_MODEL=gemma-12b-optiq uv run news run --preset NAME
```

Built-in aliases:

- `gemma-e2b-tiny`: `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`
- `gemma-26b-moe`: `mlx-community/gemma-4-26B-A4B-it-heretic-4bit`
- `gemma-12b-optiq`: `mlx-community/gemma-4-12B-it-OptiQ-4bit`

Normal report runs start the matching local MLX server, wait until it is ready,
run the pipeline, and stop the managed server when the run exits. To keep a
server warm manually, print the matching command and run it in another terminal:

```bash
NEWS_MODEL=gemma-12b-optiq uv run news model-server-command
```

### Image

`NEWS_IMAGE_ENABLED` controls report image generation:

```bash
NEWS_IMAGE_ENABLED=0 uv run news run --preset NAME
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news run
NEWS_IMAGE_ENABLED=1 uv run news run --preset NAME
```

Image generation defaults off unless enabled by a preset or explicit override.
The image model, dimensions, crop, step count, and fail-open behavior are
hard-coded runtime defaults rather than normal run knobs.

## Configuration

- `config/sources.yaml`: single source list. Normal runs select active English
  sources using `NEWS_SOURCE_SCOPE`.
- `config/recipients.yaml`: active and paused recipients. `NEWS_RECIPIENT_SCOPE`
  chooses Bradley-only or all active recipients.

Translation is paused by default. The old topic-scoped runtime variables and
source topic fields have been removed from this branch and are rejected if set.

## Outputs

Current run review files are written under `output/daily_outputs/`:

- `latest_run.md`: latest human-readable report.
- `latest_run.log`: latest captured terminal log.
- `latest_run_details.json`: latest backend audit details.

Durable run history is written to `output/history/news_history.duckdb`, with CSV
exports in `output/history/` for quick review. Final report and image artifacts
also live under `output/daily_outputs/`.

History maintenance:

```bash
uv run news history backfill --dry-run
uv run news history backfill --apply
uv run news history cleanup --dry-run
uv run news history cleanup --apply
uv run news history export
```
