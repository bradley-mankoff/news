# Settings Reference

Run Settings are `NEWS_` environment variables. Use the UI to draft most runs,
then copy the generated command, or run directly from the terminal:

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-4-12b-it-4bit NEWS_SOURCE_SCOPE=peripheral uv run news run
```

Run Presets live in `config/run_presets.yaml` as env-style Run Settings maps.
Preset IDs are data, not code paths; explicit shell/UI overrides win over preset
values. In a shell, place `NEWS_` assignments on the same command line as
`uv run news run` or export them first. Prompt Profiles can be pinned by a
preset too, by adding `NEWS_PROMPT_PROFILE: playful` to the preset's `env` map.

See [`docs/adr/0007-model-configuration-vocabulary.md`](docs/adr/0007-model-configuration-vocabulary.md)
for the accepted decision that separates Run Settings, Run Presets, Task Model
Assignment, Model Tuning, Pipeline Budget, and Model Server Settings. Prompt
Profiles are editorial tone bundles, not model configuration; their ownership
is defined in
[`docs/adr/0010-prompt-catalog-owns-editorial-instructions.md`](docs/adr/0010-prompt-catalog-owns-editorial-instructions.md).

## Default Run Settings

| Variable | Default | Description |
|---|---|---|
| `NEWS_PRESET` | _(none)_ | Selects a saved preset when `--preset NAME` is not used. |
| `NEWS_PROMPT_PROFILE` | `balanced` | Editorial tone for the five LLM prompt stages. One of `balanced`, `consensus-and-contradiction`, `explain-like-im-five`, `facts-only`, `playful`. |
| `NEWS_PROMPT_OVERRIDE_<TASK>` | _(unset)_ | Per-stage editorial override layered on top of `NEWS_PROMPT_PROFILE` (override wins). Tasks: `ARTICLE_SUMMARY`, `STORY_SCALE_SCREENING`, `STORY_DRAFTING`, `TITLE_GENERATION`, `IMAGE_ART_DIRECTION`. Unset/empty = use profile text. Editable from the UI's Editorial approach panel. |
| `NEWS_MODEL` | `gemma-4-12b-it-4bit` | Default friendly alias or full model repo/name. Task-specific model assignments inherit this value unless overridden. Stages with no LLM call of their own (story discovery) inherit this value. |
| `NEWS_SOURCE_SCOPE` | `core` | `core` selects active English core sources. `peripheral` selects core plus peripheral sources. |
| `NEWS_DELIVERY_MODE` | `owner` | Optional email delivery policy: `disabled` (no delivery, `skipped: user_disabled`), `owner` (sends only to `NEWS_PRIMARY_RECIPIENT`), or `recipients` (explicit opt-in: active `config/recipients.yaml` entries, with the owner included only when listed). An explicitly configured `NEWS_EMAIL_RECIPIENTS` fallback is used only when the catalog is empty; an all-paused catalog records `skipped: user_disabled`. Legacy `NEWS_RECIPIENT_SCOPE` maps to this mode when the new variable is unset. |
| `NEWS_RECIPIENT_SCOPE` | `primary` | Legacy migration value: `primary` maps to `NEWS_DELIVERY_MODE=owner`, `all` maps to `recipients`. Prefer `NEWS_DELIVERY_MODE`. |
| `NEWS_PRIMARY_RECIPIENT` | `primary@example.com` | Owner recipient used by `owner` delivery mode. |
| `NEWS_BLOCK_REUSED_URLS` | `0` | Every run records URL history. `1` makes recorded URLs block future reuse. |
| `NEWS_IMAGE_ENABLED` | `0` | `1` enables report image generation. Image model, size, crop, steps, and fail-open behavior are fixed defaults. |
| `NEWS_RECENT_WINDOW_HOURS` | `24` | Only articles published within this window are considered. |
| `NEWS_MAX_ARTICLES_PER_SOURCE` | `4` | Maximum articles retained per source within a single story. |
| `NEWS_TOP_OF_FUNNEL_PER_PROVIDER` | `10` | Initial source-level candidate funnel size. |
| `NEWS_MIN_ARTICLES_PER_STORY` | `4` | Minimum articles required for a retained story cluster. |
| `NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD` | `0.27` | Full-text similarity threshold for clustering articles into stories. |
| `NEWS_TOTAL_ARTICLE_SUMMARY_CAP` | `40` | Pipeline Budget cap on articles sent to article summarization. |
| `NEWS_ARTICLE_SUMMARY_MAX_TOKENS` | `1000` | Model Tuning token limit for each article summary. |
| `NEWS_STORY_DRAFTING_MAX_TOKENS` | `1800` | Model Tuning token limit for story/newsletter synthesis. |
| `NEWS_STORY_SCALE_SCREENING_MAX_TOKENS` | `3000` | Model Tuning token limit for each global story scale screening call. |
| `NEWS_TITLE_GENERATION_MAX_TOKENS` | `700` | Model Tuning token limit for the title generation / image art direction call. |

## Renamed settings (migration note)

The `bradley` terminology was replaced with `primary` (issue #23):

- `NEWS_RECIPIENT_SCOPE=bradley` and `bradley-only` are rejected with a
  `ValueError` — use `primary` (or the `single` alias).
- `NEWS_BRADLEY_RECIPIENT` is no longer read. Rename it to
  `NEWS_PRIMARY_RECIPIENT`; a leftover `NEWS_BRADLEY_RECIPIENT` is ignored
  (a warning is printed to stderr), and delivery falls back to the
  `primary@example.com` default.

## Advanced Run Settings

| Variable | Description |
|---|---|
| `NEWS_MODEL_MAX_INPUT_TOKENS` | Model Tuning synthesis prompt ceiling; older article context is trimmed if exceeded. |
| `NEWS_ARTICLE_TEXT_TOKEN_LIMIT` | Pipeline Budget truncation for scraped article text before summarization. |
| `NEWS_RELAX_STORY_DRAFTING_GUARDS` | Allows short/degraded fallback story drafting output when explicitly enabled. |
| `NEWS_MODEL_TEMPERATURE`, `NEWS_MODEL_TOP_P`, `NEWS_MODEL_TOP_K`, `NEWS_MODEL_MIN_P` | Default sampling settings. |
| `NEWS_MODEL_PRESENCE_PENALTY`, `NEWS_MODEL_REPETITION_PENALTY` | Default repetition controls. |
| `NEWS_MODEL_REASONING_TEMPERATURE`, `NEWS_MODEL_REASONING_TOP_P`, `NEWS_MODEL_REASONING_TOP_K`, `NEWS_MODEL_REASONING_MIN_P` | Sampling settings for reasoning-heavy tasks. |
| `NEWS_MODEL_REASONING_PRESENCE_PENALTY`, `NEWS_MODEL_REASONING_REPETITION_PENALTY` | Reasoning-task repetition controls. |
| `NEWS_MODEL_STORY_DISCOVERY_*`, `NEWS_MODEL_STORY_SCALE_SCREENING_*`, `NEWS_MODEL_ARTICLE_SUMMARY_*`, `NEWS_MODEL_STORY_DRAFTING_*`, `NEWS_MODEL_TITLE_GENERATION_*` | Per-task sampling overrides using the same suffixes as the default sampling group. `NEWS_MODEL_STORY_DISCOVERY_*` is retained for compatibility: story discovery has no LLM stage (embedding/TF-IDF clustering), and image art direction shares the title generation stage, so there is no `NEWS_MODEL_IMAGE_ART_DIRECTION_*` group. |

## Models

Built-in model aliases:

| Alias | Resolved model | Hugging Face page |
|---|---|---|
| `gemma-e2b-tiny` | `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit` (kept as the Codex-safe test model) | [deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit](https://huggingface.co/deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit) |
| `gemma-4-12b-it-4bit` | `mlx-community/gemma-4-12B-it-4bit` (default) | [mlx-community/gemma-4-12B-it-4bit](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit) |

The legacy `qwythos-9b-*` aliases are **unsupported**: mlx-vlm cannot launch
file-qualified GGUF references, so stale configs fail fast with an actionable
error instead of a half-started server.

Each model page shows Hugging Face's native Hardware Compatibility panel
(GGUF/MLX quantizations) — the UI model picker links directly to it.

| Variable | Default | Description |
|---|---|---|
| `NEWS_MODEL_STORY_SCALE_SCREENING` | _(inherits `NEWS_MODEL`)_ | Model assignment for the global story scale screening LLM stage. |
| `NEWS_MODEL_TITLE_GENERATION` | _(inherits `NEWS_MODEL`)_ | Model assignment for the title generation / image art direction LLM stage. |
| `NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL` | `http://127.0.0.1:8080/v1` | Model server endpoint for story scale screening calls. |
| `NEWS_MODEL_TITLE_GENERATION_BASE_URL` | `http://127.0.0.1:8080/v1` | Model server endpoint for title generation calls. |
| `NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET` | _(none)_ | Model Tuning Preset for the story scale screening stage. |
| `NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET` | _(none)_ | Model Tuning Preset for the title generation stage. |
| `NEWS_MODEL_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible local model endpoint. |
| `NEWS_CODEX_TESTING` | `0` | `1` forces Codex-safe model references for model-related verification. |

Print the fully resolved local server command without running the pipeline:

```bash
NEWS_MODEL=gemma-4-12b-it-4bit uv run news model-server-command
```

## Infrastructure

These settings are intentionally not part of the normal Run Settings surface.

| Variable | Default | Description |
|---|---|---|
| `NEWS_SOURCES_YAML` | `config/sources.yaml` | Source list path. |
| `NEWS_RECIPIENTS_YAML` | `config/recipients.yaml` | Path to a local recipients file; the checked-in config/recipients.yaml is a public template. |
| `NEWS_OUTPUT_DIR` | `output/daily_outputs` | Dated run-output directory. |
| `NEWS_HISTORY_DB` | `output/history/news_history.duckdb` | DuckDB run and URL history path. |
| `NEWS_HISTORY_EXPORT_CSV` | `1` | Export readable history CSVs after writes. |
| `NEWS_ENV_JSON` | `env.json` | JSON file with SMTP password fallback. |
| `NEWS_EMAIL_FROM` | `news@example.com` | Delivery Profile sender address and SMTP username default. The checked-in example address is a placeholder and is never deliverable (`skipped: not_configured`). |
| `NEWS_EMAIL_RECIPIENTS` | `NEWS_PRIMARY_RECIPIENT` | Explicit legacy fallback recipient list used only when `NEWS_DELIVERY_MODE=recipients` has an empty YAML catalog; it is not used to override an all-paused catalog. |
| `NEWS_SMTP_HOST`, `NEWS_SMTP_PORT`, `NEWS_SMTP_USERNAME`, `NEWS_SMTP_USE_SSL`, `NEWS_SMTP_PASSWORD` | mail defaults | SMTP delivery configuration. Placeholder credential tokens (`password`, `change-me`, …) and empty values count as not configured. |
| `NEWS_UNSUBSCRIBE_BASE_URL`, `NEWS_UNSUBSCRIBE_HOST`, `NEWS_UNSUBSCRIBE_PORT`, `NEWS_UNSUBSCRIBE_SECRET` | local defaults | Unsubscribe endpoint configuration. |
| `NEWS_TOKEN_ENCODING` | `o200k_base` | Token-counting encoding. |

## YAML Files

`config/run_presets.yaml` stores saved Run Presets. The UI can list, create,
update, duplicate, delete, select, and preview commands for these presets.

`config/sources.yaml` is the single source list. Normal source selection uses:

- `active: false` to exclude a source.
- `language: en` for normal source selection.
- `tier: core` and `tier: peripheral` with `NEWS_SOURCE_SCOPE`.

`config/recipients.yaml` stores the Delivery Profile's additional-recipient
catalog. The checked-in file is a public template with an illustrative
placeholder entry; real addresses belong in a local copy referenced by
`NEWS_RECIPIENTS_YAML`. `pause: true` keeps a recipient configured but skips
delivery (`skipped: user_disabled`). `NEWS_DELIVERY_MODE=recipients` selects
active entries; the owner is configured with `NEWS_PRIMARY_RECIPIENT` and is
included only when also listed here. An explicitly configured
`NEWS_EMAIL_RECIPIENTS` fallback applies only to an empty catalog, never to a
catalog whose entries are all paused.

## Removed Settings

The old topic-scoped controls are rejected when present:

`NEWS_TOPIC_IDS`, `NEWS_TOPIC_MODE`, `NEWS_CLIENT_YAML`, `NEWS_TOPICS_YAML`,
`NEWS_MODEL_TOPIC_CLUSTERING`, `NEWS_MODEL_TOPIC_COUNTRY_GATE`,
`NEWS_MODEL_STORY_TOPIC_VALIDATION`, `NEWS_NUM_TOP_TOPICS`,
`NEWS_TOP_TOPIC_PROBES`, `NEWS_TOPIC_RELEVANCE_MIN_SCORE`,
`NEWS_STORY_TOPIC_FIT_MIN_SCORE`, `NEWS_STORY_TOPIC_VALIDATION_ENABLED`,
`NEWS_US_TOPIC_COUNTRY_GATE_ENABLED`, `NEWS_MAX_STORIES_PER_TOPIC`,
`NEWS_TOPIC_EMBEDDING_THRESHOLD`, `NEWS_PER_SOURCE_TOPIC_ARTICLE_CAP`, and
`NEWS_SUMMARY_SCOPE`.

Model backend, cache, concurrency, and image-generation details are derived from
explicit Run Settings or hard-coded defaults rather than hidden model-size
bundles.

## Daily Automation

The desktop application supports exactly one daily personal Run Session
(Daily Automation, ADR 0012 Slice C; operational decision in
[`docs/adr/0013-local-daily-automation-uses-launchagent.md`](docs/adr/0013-local-daily-automation-uses-launchagent.md)).
Schedule settings are configured in the **Schedule** tab or via the CLI:

```bash
uv run news schedule status [--json]
uv run news schedule enable --time 07:30 [--preset NAME] [--delivery-mode owner|disabled|recipients]
uv run news schedule disable
uv run news schedule run
```

| Setting | Default | Description |
|---|---|---|
| Time | `07:00` | One daily `HH:MM` in local machine time; `00:00`–`23:59` valid, malformed values rejected. No weekly recurrence or cron expressions. |
| Run Preset | _(default settings)_ | Saved Run Preset ID from `config/run_presets.yaml`. Empty means the normal default settings; a non-empty ID must exist or enable/run fails closed. |
| Delivery mode | `owner` | `owner` (default; owner only), `disabled`, or `recipients` (explicit opt-in via `config/recipients.yaml`). The schedule's mode is forced for scheduled runs and takes precedence over preset/ambient `NEWS_DELIVERY_MODE`. |

Fixed local artifacts:

- Schedule state: `~/.config/news/daily_schedule.json` (atomic writes, `0600`;
  directory `0700`). Contains the validated time, preset ID, delivery mode, a
  safe non-secret environment snapshot, fixed paths, and a bounded last-run
  projection. A malformed record is reported as an error and never executed.
- LaunchAgent: `~/Library/LaunchAgents/com.bradley-mankoff.news-daily-run.plist`
  (`0600`), generated with `plistlib`, argv-based `ProgramArguments`,
  `StartCalendarInterval`, `RunAtLoad: false`, and no `KeepAlive`; loaded in
  the per-user `gui/<uid>` domain. The plist carries only safe PATH/HOME/stream
  settings and the absolute Python interpreter.
- Run logs: `~/.config/news/scheduled/run.stdout.log` and
  `run.stderr.log`.

Secret boundary: `NEWS_SMTP_PASSWORD`, `NEWS_UNSUBSCRIBE_SECRET`,
`NEWS_MODEL_API_KEY`, and any future secret-named setting are never copied
into schedule state, the plist, API responses, or logs. The existing ignored
`env.json` password fallback (`NEWS_ENV_JSON`) remains the only persisted SMTP
credential input. `NEWS_PRESET`/`NEWS_ACTIVE_PRESET` markers are never
persisted; a schedule binds a preset by ID. The state record may contain a
real owner email address, but never credentials or report text.

Environment snapshot: enabling captures only registered non-secret Run
Settings plus the known non-secret infrastructure values
(`NEWS_SOURCES_YAML`, `NEWS_OUTPUT_DIR`, `NEWS_HISTORY_DB`, transport
host/user, tokenizer encoding, and similar); launchd does not inherit your
interactive shell's ad-hoc `NEWS_` environment, so the snapshot is what a
scheduled run starts from. Precedence at run time: saved preset, then safe
base environment, then explicit overrides, then the forced schedule delivery
mode. Test path overrides (`NEWS_SCHEDULE_STATE`, `NEWS_SCHEDULE_PLIST`,
`NEWS_SCHEDULE_LOCK`, `NEWS_SCHEDULE_LOG_DIR`) exist for automation and tests.

Platform and lifecycle limits: macOS-only (launchd required); a non-macOS or
missing-`launchctl` environment reports `supported=false` and
`launchd_status=unavailable` instead of pretending the schedule is active.
The agent runs for the logged-in user session only; logged-out/asleep sessions
may miss the calendar window, and missed-run backfill is not provided.
Disabling boots out the agent and removes the plist but never touches existing
reports, DuckDB/CSV history, or OKF bundles. Product Daily Automation is
separate from the GitHub-board automation under `automation/`.

## CLI Commands

| Command | Description |
|---|---|
| `uv run news run --preset NAME` | Run with a saved preset. |
| `uv run news run --prompt-profile NAME` | Run with a saved preset plus a Prompt Profile override. |
| `uv run news run` | Run with defaults and explicit environment overrides. |
| `uv run news ui` | Start the guided local control panel. |
| `uv run news model-server-command` | Print the resolved local model server command and exit (external backend: prints a "no managed server command" notice and exits 2). |
| `uv run news check-sources` | Check configured source connectivity. |
| `uv run news source-languages` | Detect or verify source language tags. |
| `uv run news serve-unsubscribe` | Start the local unsubscribe endpoint. |
| `uv run news schedule status [--json]` | Show the daily schedule state: enabled/disabled, time, preset, delivery mode, launchd status, and last-run projection. |
| `uv run news schedule enable --time HH:MM [--preset NAME] [--delivery-mode MODE]` | Validate and install the daily schedule (idempotent; replaces any existing job). |
| `uv run news schedule disable` | Boot out the agent, remove the plist, and mark the schedule disabled. |
| `uv run news schedule run` | Foreground scheduled-run entry point used by launchd; fails closed when disabled/corrupt. |
| `uv run news history backfill|cleanup|export` | Maintain DuckDB-backed run history and CSV exports. |
