# Hey Codex, Look Here

This is the startup brief for an agentic coding partner in this repo. Read this
before broad exploration, then keep the context window lean.

## What This Repo Is

This is a Python 3.12 `uv` project for running a daily news pipeline.

Primary entry points:

- Dev run: `uv run news dev`
- Local production review: `uv run news local-prod`
- Loose local production review: `uv run news loose-local-prod`
- Production run: `uv run news prod`
- Installed CLI: `news` and `todays-news` map to `news_pipeline.cli:main`

Run-mode source and delivery behavior:

- `dev` uses English `tier: dev` sources from `config/sources.yaml`, sends only
  to `NEWS_DEV_RECIPIENT`, defaults to `gemma-e2b-tiny`, and keeps image
  generation off unless overridden.
- `local-prod` uses English `tier: dev` plus `tier: core` sources, sends only to
  `NEWS_DEV_RECIPIENT`, defaults to the large Gemma model, keeps image
  generation on, and uses isolated URL history by default.
- `loose-local-prod` behaves like `local-prod` for source selection, delivery,
  model defaults, image generation, and URL history, but uses dev-loose
  topic/story matching thresholds while retaining the 4-article story floor.
- `prod` uses the same `dev`/`core` source set as local production review runs,
  sends to configured active recipients, and updates shared URL history.
- Translation is currently paused by default (`NEWS_TRANSLATION_ENABLED=0`).
  Non-English sources remain in `config/sources.yaml` for later review but are
  not selected by normal runs, and the pipeline no longer translates the full
  scraped candidate funnel before topic classification.
- `tier: peripheral` sources are retained in the master source file but are not
  selected by any run mode.

Codex model safety:

- Codex must not run the managed model server or model-calling checks against
  the regular large/local models.
- Use `NEWS_CODEX_TESTING=1` or `uv run news codex-model-server-command` for
  Codex-run model checks. That forces `gemma-e2b-tiny`, the MLX 4-bit model at
  `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`.
- `news_pipeline.config.ensure_codex_safe_model_reference()` blocks Codex-run
  model invocation unless the tiny model is selected.

Core code and configuration:

- `todays_news.py`
- `news_pipeline/`
- `config/client.yaml`
- `config/topics.yaml`
- `config/sources.yaml`
- `config/recipients.yaml`
- `pyproject.toml`
- `uv.lock`
- `.python-version`

## How To Get Oriented Cheaply

Default to a narrow reconnaissance pass:

1. Read `AGENTS.md` and this file.
2. Use `rg` for exact symbols, config keys, filenames, and error text.
3. Open only the directly relevant files.
4. Prefer `news_pipeline/` plus the relevant `config/*.yaml` over sweeping docs,
   tests, old output, caches, notes, or editor folders.
5. Inspect `output/` only when the user asks about a current run or generated
   result, and prefer the current dated run.

Do not spend a large context budget getting generally familiar with the repo.
If the task is vague, do one small targeted pass and ask one concrete question.

## Working Norms

- Preserve existing pipeline shape and naming unless the user asks for a larger
  redesign.
- Treat docs, tests, old generated outputs, temporary scripts, virtualenvs,
  editor folders, caches, and local notes as peripheral unless explicitly
  requested.
- Avoid unrelated cleanup while fixing a pipeline issue.
- Do not overwrite generated outputs or config files casually.
- If a command needs verification, prefer the narrowest relevant run command
  first, then report exactly what was or was not run.

## Common Starting Points

- Pipeline behavior or orchestration: start in `news_pipeline/`.
- Topic/source/client changes: start with the relevant file in `config/`.
  `config/sources.yaml` is the only source-list YAML.
- Dev execution issues: start with `todays_news.py`, `news_pipeline/cli.py`, and
  the exact traceback or current output the user mentions.
- Local production review issues: start with `news_pipeline/cli.py` and run-mode
  handling in `news_pipeline/config.py`.
- Dependency or script entry point issues: start with `pyproject.toml`.

Keep it surgical. This file exists so each task starts with useful priors, not a
full repo rediscovery ritual.
