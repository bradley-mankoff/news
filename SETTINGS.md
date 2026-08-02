# Settings Reference

Run Settings are `NEWS_` environment variables. Use the UI to draft most runs,
then copy the generated command, or run directly from the terminal:

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-26b-moe NEWS_SOURCE_SCOPE=peripheral uv run news run
```

Run Presets live in `config/run_presets.yaml` as env-style Run Settings maps.
Preset IDs are data, not code paths; explicit shell/UI overrides win over preset
values. In a shell, place `NEWS_` assignments on the same command line as
`uv run news run` or export them first.

See `docs/adr/0007-model-configuration-vocabulary.md` for the vocabulary used
to separate Run Settings, Run Presets, Task Model Assignment, Model Tuning,
Pipeline Budget, and Model Server Settings.

## Default Run Settings

| Variable | Default | Description |
|---|---|---|
| `NEWS_PRESET` | _(none)_ | Selects a saved preset when `--preset NAME` is not used. |
| `NEWS_MODEL` | `gemma-26b-moe` | Default friendly alias or full model repo/name. Task-specific model assignments inherit this value unless overridden. |
| `NEWS_SOURCE_SCOPE` | `core` | `core` selects active English core sources. `peripheral` selects core plus peripheral sources. |
| `NEWS_RECIPIENT_SCOPE` | `bradley` | `bradley` sends to `NEWS_BRADLEY_RECIPIENT`. `all` sends to active configured recipients. |
| `NEWS_BRADLEY_RECIPIENT` | `bradley@mankoff.com` | Single-recipient address used by Bradley-scoped runs. |
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
| `NEWS_MODEL_STORY_DISCOVERY_*`, `NEWS_MODEL_STORY_SCALE_SCREENING_*`, `NEWS_MODEL_ARTICLE_SUMMARY_*`, `NEWS_MODEL_STORY_DRAFTING_*`, `NEWS_MODEL_TITLE_GENERATION_*` | Per-task sampling overrides using the same suffixes as the default sampling group. |

## Models

Built-in model aliases:

| Alias | Resolved model |
|---|---|
| `gemma-e2b-tiny` | `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit` (kept as the Codex-safe test model) |
| `qwythos-9b-4bit` | `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q4_K.gguf` |
| `qwythos-9b-8bit` | `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q8_0.gguf` (default) |

| Variable | Default | Description |
|---|---|---|
| `NEWS_MODEL_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible local model endpoint. |
| `NEWS_CODEX_TESTING` | `0` | `1` forces Codex-safe model references for model-related verification. |

Print the fully resolved local server command without running the pipeline:

```bash
NEWS_MODEL=qwythos-9b-8bit uv run news model-server-command
```

## Infrastructure

These settings are intentionally not part of the normal Run Settings surface.

| Variable | Default | Description |
|---|---|---|
| `NEWS_SOURCES_YAML` | `config/sources.yaml` | Source list path. |
| `NEWS_RECIPIENTS_YAML` | `config/recipients.yaml` | Recipient list path. |
| `NEWS_OUTPUT_DIR` | `output/daily_outputs` | Dated run-output directory. |
| `NEWS_HISTORY_DB` | `output/history/news_history.duckdb` | DuckDB run and URL history path. |
| `NEWS_HISTORY_EXPORT_CSV` | `1` | Export readable history CSVs after writes. |
| `NEWS_ENV_JSON` | `env.json` | JSON file with SMTP password fallback. |
| `NEWS_EMAIL_FROM` | `bradley.mankoff@gmail.com` | Sender address and SMTP username default. |
| `NEWS_EMAIL_RECIPIENTS` | `NEWS_BRADLEY_RECIPIENT` | Fallback recipient list if recipient YAML has no active entries. |
| `NEWS_SMTP_HOST`, `NEWS_SMTP_PORT`, `NEWS_SMTP_USERNAME`, `NEWS_SMTP_USE_SSL`, `NEWS_SMTP_PASSWORD` | mail defaults | SMTP delivery configuration. |
| `NEWS_UNSUBSCRIBE_BASE_URL`, `NEWS_UNSUBSCRIBE_HOST`, `NEWS_UNSUBSCRIBE_PORT`, `NEWS_UNSUBSCRIBE_SECRET` | local defaults | Unsubscribe endpoint configuration. |
| `NEWS_TOKEN_ENCODING` | `o200k_base` | Token-counting encoding. |

## YAML Files

`config/run_presets.yaml` stores saved Run Presets. The UI can list, create,
update, duplicate, delete, select, and preview commands for these presets.

`config/sources.yaml` is the single source list. Normal source selection uses:

- `active: false` to exclude a source.
- `language: en` for normal source selection.
- `tier: core` and `tier: peripheral` with `NEWS_SOURCE_SCOPE`.

`config/recipients.yaml` stores delivery recipients. `pause: true` skips a
recipient without removing the entry.

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

## CLI Commands

| Command | Description |
|---|---|
| `uv run news run --preset NAME` | Run with a saved preset. |
| `uv run news run` | Run with defaults and explicit environment overrides. |
| `uv run news ui` | Start the guided local control panel. |
| `uv run news model-server-command` | Print the resolved local model server command and exit (external backend: prints a "no managed server command" notice and exits 2). |
| `uv run news check-sources` | Check configured source connectivity. |
| `uv run news source-languages` | Detect or verify source language tags. |
| `uv run news serve-unsubscribe` | Start the local unsubscribe endpoint. |
| `uv run news history backfill|cleanup|export` | Maintain DuckDB-backed run history and CSV exports. |
