# Settings Reference

Runtime settings are `NEWS_` environment variables. Use the UI to draft most
runs, then copy the generated command, or run directly from the terminal:

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-26b-moe NEWS_SOURCE_SCOPE=peripheral uv run news run
```

Saved presets live in `config/run_presets.yaml` as env-style knob maps. Preset
IDs are data, not code paths; explicit shell/UI overrides win over preset
values.

## Default Run Knobs

| Variable | Default | Description |
|---|---|---|
| `NEWS_PRESET` | _(none)_ | Selects a saved preset when `--preset NAME` is not used. |
| `NEWS_MODEL` | `gemma-26b-moe` | Friendly alias or full model repo/name. Backend and runtime profile are inferred from this value. |
| `NEWS_SOURCE_SCOPE` | `core` | `core` selects active English core sources. `peripheral` selects core plus peripheral sources. |
| `NEWS_RECIPIENT_SCOPE` | `bradley` | `bradley` sends to `NEWS_BRADLEY_RECIPIENT`. `all` sends to active configured recipients. |
| `NEWS_BRADLEY_RECIPIENT` | `bradley@mankoff.com` | Single-recipient address used by Bradley-scoped runs. |
| `NEWS_BLOCK_REUSED_URLS` | `0` | Every run records URL history. `1` makes recorded URLs block future reuse. |
| `NEWS_IMAGE_ENABLED` | `0` | `1` enables report image generation. Image model, size, crop, steps, and fail-open behavior are fixed defaults. |
| `NEWS_RECENT_WINDOW_HOURS` | `24` | Only articles published within this window are considered. |
| `NEWS_MAX_ARTICLES_PER_SOURCE` | `6` | Maximum feed items selected per source before article fetch/summarization. |
| `NEWS_TOP_OF_FUNNEL_PER_PROVIDER` | `10` | Initial source-level candidate funnel size. |
| `NEWS_MIN_ARTICLES_PER_STORY` | `4` | Minimum articles required for a retained story cluster. |
| `NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD` | `0.27` | Full-text similarity threshold for clustering articles into stories. |
| `NEWS_TOTAL_ARTICLE_SUMMARY_CAP` | profile-dependent | Hard cap on articles sent to article summarization. |
| `NEWS_ARTICLE_SUMMARY_MAX_TOKENS` | profile-dependent | Max tokens for each article summary. |
| `NEWS_FINAL_SYNTHESIS_MAX_TOKENS` | profile-dependent | Max tokens for story/newsletter synthesis. |
| `NEWS_TITLE_GENERATION_MAX_TOKENS` | profile-dependent | Max tokens for report title generation. |

## Advanced Tuning

| Variable | Description |
|---|---|
| `NEWS_MODEL_MAX_INPUT_TOKENS` | Synthesis prompt ceiling; older article context is trimmed if exceeded. |
| `NEWS_ARTICLE_TEXT_TOKEN_LIMIT` | Truncates scraped article text before summarization. |
| `NEWS_RELAX_FINAL_SYNTHESIS_GUARDS` | Allows short/degraded fallback synthesis output when explicitly enabled. |
| `NEWS_MODEL_TEMPERATURE`, `NEWS_MODEL_TOP_P`, `NEWS_MODEL_TOP_K`, `NEWS_MODEL_MIN_P` | Default sampling settings. |
| `NEWS_MODEL_PRESENCE_PENALTY`, `NEWS_MODEL_REPETITION_PENALTY` | Default repetition controls. |
| `NEWS_MODEL_REASONING_TEMPERATURE`, `NEWS_MODEL_REASONING_TOP_P`, `NEWS_MODEL_REASONING_TOP_K`, `NEWS_MODEL_REASONING_MIN_P` | Sampling settings for reasoning-heavy tasks. |
| `NEWS_MODEL_REASONING_PRESENCE_PENALTY`, `NEWS_MODEL_REASONING_REPETITION_PENALTY` | Reasoning-task repetition controls. |
| `NEWS_MODEL_STORY_DISCOVERY_*`, `NEWS_MODEL_STORY_SCALE_SCREENING_*`, `NEWS_MODEL_ARTICLE_SUMMARY_*`, `NEWS_MODEL_FINAL_SYNTHESIS_*`, `NEWS_MODEL_TITLE_GENERATION_*` | Per-task sampling overrides using the same suffixes as the default sampling group. |

## Model And Translation

Built-in model aliases:

| Alias | Resolved model |
|---|---|
| `gemma-e2b-tiny` | `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit` |
| `gemma-26b-moe` | `mlx-community/gemma-4-26B-A4B-it-heretic-4bit` |
| `gemma-12b-optiq` | `mlx-community/gemma-4-12B-it-OptiQ-4bit` |

| Variable | Default | Description |
|---|---|---|
| `NEWS_MODEL_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible local model endpoint. |
| `NEWS_TRANSLATION_MODEL` | fixed default | Translation model selection for non-English translation workflows. |
| `NEWS_TRANSLATION_MODEL_BASE_URL` | `NEWS_MODEL_BASE_URL` | Translation endpoint. |
| `NEWS_TRANSLATION_TARGET_LANGUAGE` | `en` | Translation target language. Normal runs leave translation paused. |
| `NEWS_CODEX_TESTING` | `0` | `1` forces Codex-safe model references for model-related verification. |

Print the fully resolved local server command without running the pipeline:

```bash
NEWS_MODEL=gemma-12b-optiq uv run news model-server-command
```

## Infrastructure

These settings are intentionally not part of the normal runtime knob surface.

| Variable | Default | Description |
|---|---|---|
| `NEWS_SOURCES_YAML` | `config/sources.yaml` | Source list path. |
| `NEWS_RECIPIENTS_YAML` | `config/recipients.yaml` | Recipient list path. |
| `NEWS_OUTPUT_DIR` | `output/daily_outputs` | Dated run-output directory. |
| `NEWS_HISTORY_DB` | `output/history/news_history.duckdb` | DuckDB run and URL history path. |
| `NEWS_HISTORY_EXPORT_CSV` | `1` | Export readable history CSVs after writes. |
| `NEWS_WRITE_LEGACY_DIAGNOSTICS` | `0` | Also write older per-run diagnostic artifacts. |
| `NEWS_ENV_JSON` | `env.json` | JSON file with SMTP password fallback. |
| `NEWS_EMAIL_FROM` | `bradley.mankoff@gmail.com` | Sender address and SMTP username default. |
| `NEWS_EMAIL_RECIPIENTS` | `NEWS_BRADLEY_RECIPIENT` | Fallback recipient list if recipient YAML has no active entries. |
| `NEWS_SMTP_HOST`, `NEWS_SMTP_PORT`, `NEWS_SMTP_USERNAME`, `NEWS_SMTP_USE_SSL`, `NEWS_SMTP_PASSWORD` | mail defaults | SMTP delivery configuration. |
| `NEWS_UNSUBSCRIBE_BASE_URL`, `NEWS_UNSUBSCRIBE_HOST`, `NEWS_UNSUBSCRIBE_PORT`, `NEWS_UNSUBSCRIBE_SECRET` | local defaults | Unsubscribe endpoint configuration. |
| `NEWS_TOKEN_ENCODING` | `o200k_base` | Token-counting encoding. |

## YAML Files

`config/run_presets.yaml` stores saved runtime presets. The UI can list, create,
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

Model backend/profile/cache/concurrency and image-generation details are derived
from the selected model or hard-coded runtime defaults rather than normal
run-drafting knobs.

## CLI Commands

| Command | Description |
|---|---|
| `uv run news run --preset NAME` | Run with a saved preset. |
| `uv run news run` | Run with defaults and explicit environment overrides. |
| `uv run news ui` | Start the local runtime drafting UI. |
| `uv run news model-server-command` | Print the resolved local model server command and exit. |
| `uv run news check-sources` | Check configured source connectivity. |
| `uv run news source-languages` | Detect or verify source language tags. |
| `uv run news serve-unsubscribe` | Start the local unsubscribe endpoint. |
| `uv run news history backfill|cleanup|export` | Maintain DuckDB-backed run history and CSV exports. |
