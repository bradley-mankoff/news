# News Core Context

This is the startup brief for Codex in this repository. Treat the repo as a
core-pipeline workspace by default, and keep the context window lean.

## What This Repo Is

This is a Python 3.12 `uv` project for running a daily news pipeline.

Primary entry points:

- Dev run: `uv run news dev`
- Local production review: `uv run news local-prod`
- Loose local production review: `uv run news loose-local-prod`
- Production run: `uv run news prod`
- Installed CLI: `news` and `todays-news` map to `news_pipeline.cli:main`

Run commands from the repo root.

## Run-Mode Behavior

- `dev` uses `tier: core` English sources whose `allowed_topic_ids` intersect
  the active runtime topics from `config/sources.yaml`, sends only to
  `NEWS_DEV_RECIPIENT`, defaults to `gemma-e2b-tiny`, and keeps image generation
  off unless overridden.
- `local-prod` uses `tier: core` plus `tier: peripheral` English sources whose
  `allowed_topic_ids` intersect the active runtime topics, sends only to
  `NEWS_DEV_RECIPIENT`, defaults to the large Gemma model, keeps image
  generation on, and uses isolated URL history by default.
- `loose-local-prod` behaves like `local-prod` for source selection, delivery,
  model defaults, image generation, and URL history, but uses dev-loose
  topic/story matching thresholds while retaining the 4-article story floor.
- `prod` uses `tier: core` plus `tier: peripheral` English sources whose
  `allowed_topic_ids` intersect the active runtime topics, sends to configured
  active recipients, and updates shared URL history.

## Source Selection

Source selection is entirely driven by `config/sources.yaml`:

- `dev` uses `tier: core` English sources whose `allowed_topic_ids` intersect
  the active runtime topic IDs.
- `local-prod`, `loose-local-prod`, and `prod` use `tier: core` plus
  `tier: peripheral` English sources whose `allowed_topic_ids` intersect the
  active runtime topic IDs.
- `loose-local-prod` otherwise behaves like `local-prod`, but uses dev-loose
  topic/story matching thresholds while keeping the 4-article story floor.
- Translation is currently paused by default (`NEWS_TRANSLATION_ENABLED=0`).
  Non-English sources remain in `config/sources.yaml` for later review but are
  not selected by normal runs, and the pipeline no longer translates the full
  scraped candidate funnel before topic classification.
- Sources without `allowed_topic_ids` stay in the master source file for later
  review but are not selected by normal runs.
- Source-level `topics` metadata has been removed; use `allowed_topic_ids` for
  topical source scoping.

## Codex Model Safety

- Codex must not start the managed model server or run model-calling pipeline
  checks against the regular large/local models.
- The only model Codex may use for model-related verification is
  `gemma-e2b-tiny`, which resolves to
  `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`.
- For Codex-run model checks, set `NEWS_CODEX_TESTING=1` or use
  `uv run news codex-model-server-command`.
- `news_pipeline.config.ensure_codex_safe_model_reference()` blocks Codex-run
  model invocation unless the tiny model is selected.

## Core Files

Core files are the files needed to run the daily news pipeline in dev,
local-production review, or production mode:

- `todays_news.py`
- `news_pipeline/`
- `config/client.yaml`
- `config/topics.yaml`
- `config/sources.yaml`
- `config/recipients.yaml`
- `pyproject.toml`
- `uv.lock`
- `.python-version`
- `AGENTS.md`
- assistant context files such as `.codex/config.toml`, `.codexignore`,
  `.cursorignore`, and `.continueignore`

Peripheral files are intentionally excluded from default agent context: docs,
tests, old generated outputs, virtualenvs, temporary scripts, editor folders,
caches, and local notes. Codex sandbox reads are also denied for docs, tests,
virtualenvs, temporary scripts, editor folders, caches, and local notes.
`output/` remains sandbox-readable so the current dated run can be inspected,
but assistant ignore files should expose only the current run by default.
Do not inspect other peripheral files unless the user explicitly asks to relax
the core-only boundary.

## How To Get Oriented Cheaply

Default to a narrow reconnaissance pass:

1. Read this file.
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
