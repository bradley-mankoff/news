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
uv run news loose-local-prod
uv run news prod
```

`dev` sends only to `NEWS_DEV_RECIPIENT`, uses `tier: core` English sources
allowed for the active topic IDs, defaults to `gemma-e2b-tiny`, keeps image
generation off, and records a dev URL log without updating shared production
history.

`local-prod` uses `tier: core` plus `tier: peripheral` English sources allowed
for the active topic IDs, defaults to the normal large Gemma model with image
generation on, but still sends only to `NEWS_DEV_RECIPIENT`. It uses isolated
URL history by default so a review run does not starve a later production run.

`loose-local-prod` uses the same source pool as `local-prod`, but applies the
dev-loose topic/story matching thresholds while keeping the production story
floor.

`prod` uses `tier: core` plus `tier: peripheral` English sources allowed for the
active topic IDs, sends to configured active recipients, and updates shared URL
history.

### Runtime Topic Selection

By default, all run modes use the ordered `topic_ids` in `config/client.yaml`,
which are the original four report topics: `global_crises_conflict`,
`us_economy`, `global_business_finance`, and `us_politics`. You can override
that list for a single run with `--topics` and a comma-separated list of topic
IDs:

```bash
uv run news dev --topics sports,science_space_tech
uv run news local-prod --topics global_crises_conflict,us_politics,sports
uv run news loose-local-prod entertainment,science_space_tech
uv run news prod --topics global_crises_conflict,us_economy,global_business_finance,us_politics
```

The positional form after a run mode is equivalent to `--topics`. The CLI also
honors `NEWS_TOPIC_IDS` for compatibility scripts:

```bash
NEWS_TOPIC_IDS=sports,entertainment uv run todays_news.py local-prod
```

Topic IDs are defined in `config/topics.yaml`. The current IDs are:

- `global_crises_conflict`
- `us_economy`
- `global_business_finance`
- `us_politics`
- `sports`
- `entertainment`
- `science_space_tech`

Runtime topic selection fully replaces the default `config/client.yaml` list for
that run; it does not append to it.

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
- `config/topics.yaml` defines topic vocabulary and matching thresholds. It now
  includes optional run topics for sports, entertainment/celebrity/music/
  Hollywood coverage, and science/space/technology coverage.
- `config/sources.yaml` is the single master source list. It includes working
  feeds tagged by `language`, `tier`, optional `nations`, and
  `allowed_topic_ids`. Pipeline runs select English sources whose `tier` is
  enabled for the run mode and whose `allowed_topic_ids` intersect the active
  runtime topics. Sources without `allowed_topic_ids` remain in the master list
  but are not selected by normal runs.
- `config/recipients.yaml` defines recipients and paused recipients.

### Source Pools and Topic-Scoped Sources

Sources are controlled by two independent YAML attributes:

- `tier` controls source pool size. Use `core` for quick/dev runs. Use
  `peripheral` for additional sources that should appear only in
  `local-prod`, `loose-local-prod`, and `prod`.
- `allowed_topic_ids` controls topical scope. It is the only source-level topic
  selection field.

```yaml
sources:
  - key: Example Sports Feed
    name: Example Sports Feed
    language: en
    tier: core
    allowed_topic_ids:
      - sports
    url: https://example.com/sports/rss
```

The old source-level `topics` metadata has been removed. A source is loaded only
when both source-selection gates pass:

- Its `tier` is enabled for the run mode: `dev` uses `core`; `local-prod`,
  `loose-local-prod`, and `prod` use `core` plus `peripheral`.
- At least one of its `allowed_topic_ids` is active for the run.
- After semantic topic classification, article-topic matches from that source
  are kept only for the listed topic IDs.

The previous general-purpose runnable source pool is now represented by the four
default IDs on each source: `global_crises_conflict`, `us_economy`,
`global_business_finance`, and `us_politics`. Sports-only sources are tagged
only with `sports`, entertainment-only sources only with `entertainment`, and so
on, so they are not scraped during the default four-topic run and cannot leak
into unrelated topics.

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

Use the report and image files to review what would be delivered, use the
`topics_*`, `run_details_*`, and `terminal_output_*` files to debug or audit how
the run behaved, and treat the URL logs as run-history state that prevents
repeat stories in later runs.

## Project Map

- `news_pipeline/cli.py`: run modes and utility commands.
- `news_pipeline/config.py`: YAML/env config, model profiles, assistant context
  ignore management.
- `news_pipeline/pipeline.py`: orchestration, article gathering, synthesis,
  image generation, rendering, email, and diagnostics.
- `news_pipeline/source_checks.py`: source connectivity diagnostics used by
  `uv run news check-sources`.
