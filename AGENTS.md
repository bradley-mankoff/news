# News Core Context

Codex should treat this repository as a core-pipeline workspace by default.

Before broad exploration, read `HEY_CODEX_LOOK_HERE.md` for the compact startup
brief. Use it to avoid spending a large context budget rediscovering stable repo
facts.

Core files are the files needed to run the daily news pipeline in dev, local-production review, or production mode:

- `todays_news.py`
- `news_pipeline/`
- `config/client.yaml`
- `config/topics.yaml`
- `config/sources.yaml`
- `config/recipients.yaml`
- `pyproject.toml`
- `uv.lock`
- `.python-version`
- `HEY_CODEX_LOOK_HERE.md`
- assistant context files such as `.codex/config.toml`, `.codexignore`, `.cursorignore`, and `.continueignore`

Peripheral files are intentionally excluded from default agent context: docs, tests, old generated outputs, virtualenvs, temporary scripts, editor folders, and local notes. Codex sandbox reads are also denied for docs, tests, virtualenvs, temporary scripts, editor folders, caches, and local notes. `output/` remains sandbox-readable so the current dated run can be inspected, but assistant ignore files should expose only the current run by default. Do not inspect other peripheral files unless the user explicitly asks to relax the core-only boundary.

Run commands from the repo root:

- Dev: `uv run news dev`
- Local production review: `uv run news local-prod`
- Production: `uv run news prod`

Source selection is now entirely driven by `config/sources.yaml`:

- `dev` uses `tier: dev` sources that are English or explicitly
  translation-enabled.
- `local-prod` and `prod` use `tier: dev` plus `tier: core` sources that are
  English or translation-enabled.
- Non-English sources are translation-enabled by their `language` field unless
  `requires_translation: false` is set. Translation runs as a bundled step using
  `google/translategemma-4b-it` before the main summarization model is loaded.
- `tier: peripheral` sources stay in the master list for later review but are
  not selected by any run mode.

Codex model safety:

- Codex must not start the managed model server or run model-calling pipeline
  checks against the regular large/local models.
- The only model Codex may use for model-related verification is
  `gemma-e2b-tiny`, which resolves to
  `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`.
- For Codex-run model checks, set `NEWS_CODEX_TESTING=1` or use
  `uv run news codex-model-server-command`. The pipeline also has a Codex
  runtime guard that rejects model invocation unless this tiny model is selected.
