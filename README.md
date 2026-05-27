# Daily News Pipeline

Python 3.12 `uv` project for building and sending the daily news report. The
active code path is the `news_pipeline/` package plus YAML config in `config/`.

## Setup

Run commands from the repo root so `uv` uses this project's environment:

```bash
cd /Users/home/personal_code/news
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

On Apple Silicon, the platform check should print `arm64`.

## Run

The daily commands are intentionally plain:

```bash
uv run news dev
uv run news local-prod
uv run news prod
```

`dev` sends only to `NEWS_DEV_RECIPIENT`, uses the 40-source English `dev`
source tier, defaults to `gemma-e2b-tiny`, keeps image generation off, and
records a dev URL log without updating shared production history.

`local-prod` uses the full runnable English source set (`dev` + `core` tiers),
defaults to the normal large Gemma model with image generation on, but still
sends only to `NEWS_DEV_RECIPIENT`. It uses isolated URL history by default so a
review run does not starve a later production run.

`prod` uses the same runnable English source set as `local-prod`, sends to
configured active recipients, and updates shared URL history.

Compatibility commands still work:

```bash
uv run todays_news.py
uv run todays_news.py local-prod
NEWS_RUN_MODE=local-prod uv run todays_news.py
NEWS_DEV=0 uv run todays_news.py
```

Other useful commands:

```bash
uv run news model-server-command
uv run news check-sources --only-failures
uv run news source-languages --sources-yaml config/sources.yaml --json
uv run news serve-unsubscribe
```

## Configuration

- `config/client.yaml` selects active predefined topic IDs in report order.
- `config/topics.yaml` defines topic vocabulary and matching thresholds.
- `config/sources.yaml` is the single master source list. It includes working
  feeds only, tagged by `language`, `tier`, optional `topics`, and optional
  `nations`. Pipeline runs only select English `dev`/`core` sources;
  `peripheral` and non-English sources are retained for later review but are not
  runnable.
- `config/recipients.yaml` defines recipients and paused recipients.

Most runtime knobs are `NEWS_` environment variables. See `SETTINGS.md` for the
full reference. Common overrides:

```bash
NEWS_MODEL=qwen-9b-dense uv run news local-prod
NEWS_IMAGE_ENABLED=0 uv run news local-prod
#local prod but tiny:
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news local-prod
NEWS_LOCAL_PROD_USE_SHARED_HISTORY=1 uv run news local-prod
```

## Model Server

Normal report runs start the matching local MLX server, wait until it is ready,
run the pipeline, and stop the managed server when the run exits. Manual server
startup is still available when you want to keep the server warm:

```bash
uv run news model-server-command
```

Run the printed command from the repo root in a second terminal, then run the
pipeline command you want in the first terminal.

If MLX says there is no compatible wheel, this machine cannot run the local MLX
server directly. Point `NEWS_MODEL_BASE_URL` at another OpenAI-compatible server
instead.

If `mlx_lm` is missing on an Apple Silicon Mac, the `.venv` may have been built
under x86_64/Rosetta. Rebuild it with arm64 Python:

```bash
uv run python -c 'import platform; print(platform.machine())'
mv .venv ".venv-x86_64-backup-$(date +%Y%m%d-%H%M%S)"
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

## Outputs

Each run writes to `output/daily_outputs/YYYY-MM-DD/`:

- `news_report_*.txt`: final report body.
- `news_report_*_image.png`: generated image with code-rendered headline.
- `news_report_*_raw.png`: raw text-free generated image.
- `news_report_*_image_prompt.txt`: generated image prompt.
- `news_report_*_image_stats.json`: image model, seed, timing, and paths.
- `topics_*.json`: configured topic diagnostics.
- `run_details_*.json` and `run_details_*.md`: backend audit trail.
- `terminal_output_*.log`: captured terminal output.
- `dev_used_urls.txt`, `local_prod_used_urls.txt`, or `used_urls.txt`: URL log.

## Project Map

- `news_pipeline/cli.py`: run modes and utility commands.
- `news_pipeline/config.py`: YAML/env config, model profiles, assistant context
  ignore management.
- `news_pipeline/pipeline.py`: orchestration, article gathering, synthesis,
  image generation, rendering, email, and diagnostics.
- `news_pipeline/source_checks.py`: source connectivity diagnostics used by
  `uv run news check-sources`.
