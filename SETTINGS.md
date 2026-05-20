# Settings Reference

All runtime knobs are environment variables with the prefix `NEWS_`. Set them
inline or export them before running:

```bash
NEWS_MODEL=gemma-26b-moe uv run news prod
```

Two YAML files in `config/` handle the things that are too structured for env
vars: the source list and the recipient list. Everything else is below.

---

## Mode

| Variable | Default | Description |
|---|---|---|
| `NEWS_RUN_MODE` | _(derived from `NEWS_DEV`)_ | Explicit mode: `dev`, `local-prod`, or `prod`. `dev` uses the narrow test run and relaxed final-output blocking; `local-prod` uses production source/topic/cap scope with isolated URL history and sends only to `NEWS_DEV_RECIPIENT`; `prod` sends to the configured active recipients and updates shared URL history. |
| `NEWS_DEV` | `1` | Backward-compatible mode switch when `NEWS_RUN_MODE` is unset. `1` = `dev`; `0` = `prod`. |
| `NEWS_DEV_RECIPIENT` | `bradley@mankoff.com` | Single recipient used by `dev` and `local-prod` modes. |
| `NEWS_DEV_RELAXED_FINAL_GUARDS` | `1` | In `dev` mode only, allows short/degraded final synthesis text through so render/image/email paths can be tested even when the narrowed source pool produces sparse coverage. |
| `NEWS_LOCAL_PROD_USE_SHARED_HISTORY` | `0` | `1` makes `local-prod` read/write the shared production URL history. The default keeps review runs from starving later runs or marking URLs as sent to the production audience. |

---

## Model selection

| Variable | Default | Description |
|---|---|---|
| `NEWS_MODEL` | _(see below)_ | Friendly alias **or** full HuggingFace repo ID. Takes highest priority. Aliases: `gemma-26b-moe`, `qwen-9b-dense`. |
| `NEWS_DEFAULT_MODEL` | `gemma-26b-moe` | Fallback alias when `NEWS_MODEL` is unset. Change this to permanently switch the default without setting it every run. |
| `NEWS_MODEL_NAME` | _(none)_ | Lower-priority raw repo ID override (legacy; prefer `NEWS_MODEL`). |
| `NEWS_MODEL_PROFILE` | _(inferred)_ | Force a runtime profile: `big_conservative` or `small_aggressive`. Inferred from the alias when unset. |
| `NEWS_MODEL_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI-compatible endpoint. Change if running the model server on a different port or machine. |

---

## Model server (mlx_lm)

These control the MLX server command that the pipeline builds and launches
automatically. They are part of the model runtime profile and can be overridden
per-run.

| Variable | big_conservative | small_aggressive | Description |
|---|---|---|---|
| `NEWS_SERVER_DECODE_CONCURRENCY` | 1 | 2 | MLX decode concurrency. |
| `NEWS_SERVER_PROMPT_CONCURRENCY` | 1 | 2 | MLX prompt concurrency. |
| `NEWS_SERVER_PREFILL_STEP_SIZE` | 512 | 2048 | MLX prefill step size (tokens). |
| `NEWS_SERVER_PROMPT_CACHE_SIZE` | 2 | 16 | Number of KV cache slots. |
| `NEWS_SERVER_PROMPT_CACHE_BYTES` | `512MB` | `3GB` | Memory for the KV cache. |
| `NEWS_SERVER_MAX_TOKENS` | 2500 | 2400 | Max tokens the server will generate per request. |

Print the fully resolved command without running the pipeline:

```bash
NEWS_MODEL=qwen-9b-dense uv run news model-server-command
```

---

## Model input / context budgets

| Variable | big_conservative | small_aggressive | Description |
|---|---|---|---|
| `NEWS_MODEL_MAX_INPUT_TOKENS` | 7000 | 12000 | Hard ceiling on the synthesis prompt. Older articles are trimmed if exceeded. |
| `NEWS_ARTICLE_TEXT_TOKEN_LIMIT` | 6000 | 8000 | Truncate each scraped article to this many tokens before summarization. |

---

## Per-task generation caps (tokens)

| Variable | big_conservative | small_aggressive | Description |
|---|---|---|---|
| `NEWS_TOPIC_CLUSTERING_MAX_TOKENS` | 1800 | 2200 | Max tokens for the LLM topic clustering response. |
| `NEWS_TRANSLATION_MAX_TOKENS` | 1800 | 2400 | Max tokens when translating non-English articles. |
| `NEWS_ARTICLE_SUMMARY_MAX_TOKENS` | 1600 | 1800 | Max tokens for each individual article summary. |
| `NEWS_FINAL_SYNTHESIS_MAX_TOKENS` | 2200 | 2400 | Max tokens for the final newsletter synthesis. |
| `NEWS_TITLE_GENERATION_MAX_TOKENS` | 50 | 60 | Max tokens for the report title. |

---

## Sampling parameters

Each task has its own sampling group. Unset task-level variables fall back to the
default group.

**Default / article summary / translation / title generation:**

| Variable | Description |
|---|---|
| `NEWS_MODEL_TEMPERATURE` | Sampling temperature for the default group and any tasks not overridden below. |
| `NEWS_MODEL_TOP_P` | Nucleus sampling p. |
| `NEWS_MODEL_TOP_K` | Top-k cutoff. |
| `NEWS_MODEL_MIN_P` | Min-p cutoff. |
| `NEWS_MODEL_PRESENCE_PENALTY` | Presence penalty. |
| `NEWS_MODEL_REPETITION_PENALTY` | Repetition penalty. |

**Reasoning / final synthesis:**

| Variable | Description |
|---|---|
| `NEWS_MODEL_REASONING_TEMPERATURE` | Temperature for topic clustering and final synthesis tasks. |
| `NEWS_MODEL_REASONING_TOP_P` | |
| `NEWS_MODEL_REASONING_TOP_K` | |
| `NEWS_MODEL_REASONING_MIN_P` | |
| `NEWS_MODEL_REASONING_PRESENCE_PENALTY` | |
| `NEWS_MODEL_REASONING_REPETITION_PENALTY` | |

**Per-task fine-tuning** (each task also accepts the full `_TEMPERATURE`, `_TOP_P`,
`_TOP_K`, `_MIN_P`, `_PRESENCE_PENALTY`, `_REPETITION_PENALTY` suffix):

| Prefix | Task |
|---|---|
| `NEWS_MODEL_TRANSLATION_` | Non-English article translation |
| `NEWS_MODEL_TOPIC_CLUSTERING_` | Topic candidate clustering |
| `NEWS_MODEL_ARTICLE_SUMMARY_` | Per-article summarization |
| `NEWS_MODEL_FINAL_SYNTHESIS_` | Newsletter synthesis pass |
| `NEWS_MODEL_TITLE_GENERATION_` | Report title and image art direction |

---

## Article pipeline caps

| Variable | Default | Description |
|---|---|---|
| `NEWS_RECENT_WINDOW_HOURS` | `24` | Only articles published within this window are considered. |
| `NEWS_MAX_ARTICLES_PER_SOURCE` | `6` | Maximum articles selected per source per topic during feed scanning. |
| `NEWS_TOTAL_ARTICLE_SUMMARY_CAP` | profile-dependent | Hard ceiling on total articles sent to the summarization pass. |
| `NEWS_PER_TOPIC_ARTICLE_SUMMARY_CAP` | profile-dependent | Per-topic ceiling on articles sent to summarization. |
| `NEWS_MAX_STORIES_PER_TOPIC` | `2` dev, `4` local-prod/prod | Maximum detected story clusters retained for each topic before article summaries. |
| `NEWS_MAX_ARTICLES_PER_STORY` | `4` big_conservative, `5` small_aggressive | Maximum articles fully summarized from a retained story cluster. |
| `NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD` | `0.22` | Full-text TF-IDF cosine threshold for linking articles into a story cluster. |
| `NEWS_PER_SOURCE_TOPIC_ARTICLE_CAP` | `1` | Maximum articles from a single source for a topic; after story clustering, extra summaries are capped per source/story. |
| `NEWS_ARTICLE_SUMMARY_CONCURRENCY` | profile-dependent | Number of article summaries run in parallel. Increase carefully; higher values stress the local model server. |

---

## Topic selection

| Variable | Default | Description |
|---|---|---|
| `NEWS_TOPIC_MODE` | `predefined` | `predefined` loads fixed topic vocabulary from YAML. `dynamic` runs the legacy top-of-funnel discovery and LLM clustering path. |
| `NEWS_CLIENT_YAML` | `config/client.yaml` | Client config listing active predefined topic IDs in report order. |
| `NEWS_TOPICS_YAML` | `config/topics.yaml` | Predefined topic vocabulary and matching thresholds. |
| `NEWS_NUM_TOP_TOPICS` | `4` | Dynamic mode only: final number of topics selected for the newsletter. |
| `NEWS_TOP_TOPIC_PROBES` | `4` | Dynamic mode only: approximate number of headlines the LLM inspects per topic cluster during discovery. |
| `NEWS_TOP_OF_FUNNEL_PER_PROVIDER` | `10` | Dynamic mode only: headlines fetched per top-of-funnel provider during discovery. |
| `NEWS_DEV_SOURCE_LIMIT` | `3` | In `dev`, only this many article sources are scanned after topic selection. |
| `NEWS_DEV_NUM_TOPICS` | `2` | In `dev`, cap predefined topics to the first N configured IDs; in dynamic mode, cap selected topics to N. |
| `NEWS_SUMMARY_SCOPE` | `today's selected news topics` | Descriptive phrase embedded in synthesis prompts that tells the model what kind of content this is. |

Predefined mode is the default. The local LLM does not choose the topics or
generate matching terms; it only summarizes and synthesizes articles after the
configured vocabulary has selected them.

Dynamic mode is preserved as a fork. Topic frame nudging (the soft geographic
balance between US/western/non-western topics) is hard-coded in `pipeline.py` as
`TOPIC_FRAME_TARGETS` and `TOPIC_FRAME_NUDGE_STRENGTH`.

In dynamic mode, if the LLM returns no usable topic clusters, fallback topic
discovery is deterministic: it clusters top-of-funnel headlines by normalized
keyword overlap, keeps only clusters with support from at least two providers,
and uses stricter article matching.

In `dev`, article summaries are also narrowed after gathering: the effective
budget is capped at two articles per selected topic and twice the selected topic
count.

---

## Email and SMTP

| Variable | Default | Description |
|---|---|---|
| `NEWS_EMAIL_FROM` | `bradley.mankoff@gmail.com` | Sender address. Also used as `SMTP_USERNAME` default. |
| `NEWS_SMTP_HOST` | `smtp.gmail.com` | SMTP server hostname. |
| `NEWS_SMTP_PORT` | `465` | SMTP port. |
| `NEWS_SMTP_USERNAME` | _(same as `NEWS_EMAIL_FROM`)_ | SMTP login username. |
| `NEWS_SMTP_USE_SSL` | `1` | `1` = SMTP_SSL (port 465). `0` = STARTTLS (port 587). |
| `NEWS_SMTP_PASSWORD` | _(from `env.json` → `pw` key)_ | SMTP password. Set via env var or store in `env.json`. |
| `NEWS_EMAIL_RECIPIENTS` | _(same as `NEWS_DEV_RECIPIENT`)_ | Comma-separated fallback recipient list when `config/recipients.yaml` has no entries. |

---

## Unsubscribe server

| Variable | Default | Description |
|---|---|---|
| `NEWS_UNSUBSCRIBE_BASE_URL` | `http://127.0.0.1:8765/unsubscribe` | Full URL embedded in email footers. Update to a publicly reachable address when running the unsubscribe server on a VPS. |
| `NEWS_UNSUBSCRIBE_HOST` | `127.0.0.1` | Interface the `--serve-unsubscribe` HTTP server binds to. |
| `NEWS_UNSUBSCRIBE_PORT` | `8765` | Port the `--serve-unsubscribe` HTTP server listens on. |
| `NEWS_UNSUBSCRIBE_SECRET` | _(falls back to SMTP password, then sender address)_ | HMAC signing key for unsubscribe tokens. Set explicitly for stability. |

---

## Image generation

| Variable | Default | Description |
|---|---|---|
| `NEWS_IMAGE_ENABLED` | `1` | `0` skips image generation entirely. |
| `NEWS_IMAGE_FAIL_ON_ERROR` | `0` | `1` makes image failures abort the run. Default is fail-open (warning logged, run continues). |
| `NEWS_IMAGE_MODEL_ID` | `Runpod/FLUX.2-klein-4B-mflux-4bit` | mflux model identifier passed to the FLUX.2 generator `--model` option. |
| `NEWS_IMAGE_BASE_MODEL` | `flux2-klein-4b` | mflux base model family passed to `--base-model`. |
| `NEWS_IMAGE_WIDTH` | `1024` | Generated image width in pixels. |
| `NEWS_IMAGE_HEIGHT` | `1024` | Generated image height in pixels. |
| `NEWS_IMAGE_STEPS` | `4` | Diffusion steps. Higher = slower + usually sharper. |
| `NEWS_IMAGE_CROP_BOTTOM_RATIO` | `0.12` | Fraction of the raw image cropped from the bottom before the headline footer is added (0.0–0.35). Compensates for FLUX's tendency to add blank space at the bottom. |

---

## File paths

| Variable | Default | Description |
|---|---|---|
| `NEWS_OUTPUT_DIR` | `output/daily_outputs` | Directory where dated run folders are written. Relative to the project root. |
| `NEWS_SOURCES_YAML` | `config/sources.yaml` | Path to the source list YAML. |
| `NEWS_RECIPIENTS_YAML` | `config/recipients.yaml` | Path to the recipient list YAML. |
| `NEWS_ENV_JSON` | `env.json` | Path to a JSON file with a `pw` key used as SMTP password fallback. |
| `NEWS_TOKEN_ENCODING` | `o200k_base` | tiktoken encoding used for token counting (`o200k_base` or `cl100k_base`). |

---

## YAML config files

### `config/client.yaml`

Client-level topic selection. Changes take effect on the next run.

| Field | Required | Description |
|---|---|---|
| `topic_ids` | yes | Ordered list of predefined topic IDs from `config/topics.yaml`. |

### `config/topics.yaml`

Predefined topic vocabulary. Each entry is selected by ID from
`config/client.yaml`.

| Field | Required | Description |
|---|---|---|
| `id` | yes | Stable topic identifier used by client config and diagnostics. |
| `title` | yes | Human-readable topic title used in reports. |
| `rationale` | no | Short editorial scope note for diagnostics. |
| `keywords` | conditionally | Relevance terms. Required if `boost_phrases` is empty. |
| `boost_phrases` | conditionally | Stronger multi-word relevance phrases. Required if `keywords` is empty. |
| `min_score` | no | Relevance score required for article selection. Defaults to `2`. |
| `max_articles_per_source` | no | Per-source article cap for this topic. Defaults to `NEWS_MAX_ARTICLES_PER_SOURCE`. |
| `frame_tags` | no | Diagnostic tags such as `us`, `western`, or `non_western`. |

### `config/sources.yaml`

Article feeds plus legacy dynamic discovery providers. Changes take effect on
the next run without touching Python code.

**`top_funnel_providers`** — used only when `NEWS_TOPIC_MODE=dynamic`. Each entry needs:

| Field | Required | Description |
|---|---|---|
| `key` | yes | Stable identifier. |
| `name` | yes | Human-readable label. |
| `url` | yes | RSS/Atom/JSON endpoint. |
| `fetcher` | no | `rss` (default) or `reddit_top_json`. |
| `region` | no | Geographic label for frame tracking. |
| `frame` | no | Editorial frame label (falls back to `region`). |
| `weight` | no | Float multiplier applied to validation scoring (default `1.0`). |
| `can_seed_topics` | no | `true` → headlines enter the topic candidate pool. |
| `can_validate_topics` | no | `true` → headlines validate candidates from seeders. |
| `can_enrich_coverage` | no | Not used for top-funnel providers (always `false` effectively). |

**`sources`** — the article pool searched after topics are selected. Each entry needs:

| Field | Required | Description |
|---|---|---|
| `key` | yes | Stable identifier. |
| `name` | yes | Human-readable label shown in the newsletter. |
| `url` | yes | RSS/Atom feed URL. Google News source-scoped URLs work. |
| `homepage` | no | Outlet homepage linked in the email source listing. |
| `region` | no | Geographic label. |
| `frame` | no | Editorial frame label (falls back to `region`). |
| `weight` | no | Float (default `1.0`). Not currently used in article ranking, reserved for future use. |
| `can_enrich_coverage` | no | Default `true`. Set `false` to include the source as metadata only. |

### `config/recipients.yaml`

| Field | Description |
|---|---|
| `email` | Recipient address. |
| `name` | Display name used in the greeting. |
| `personal_prompt` | Optional custom instruction inserted into the final synthesis prompt. Recipients sharing the exact same prompt text are batched into one synthesis pass. `null` uses the default newsletter format. |
| `pause` | `true` skips delivery without removing the recipient. Also settable via the unsubscribe endpoint. |

---

## CLI commands

| Command | Description |
|---|---|
| `uv run news dev` | Narrow dev run. Sends only to `NEWS_DEV_RECIPIENT`. |
| `uv run news local-prod` | Production-width review run. Sends only to `NEWS_DEV_RECIPIENT` and uses isolated URL history by default. |
| `uv run news prod` | Production delivery and shared URL history. |
| `uv run news model-server-command` | Print the resolved MLX server command for the selected model and exit. |
| `uv run news check-sources` | Check configured source connectivity. Supports `--section`, `--timeout`, `--only-failures`, and `--json`. |
| `uv run news serve-unsubscribe` | Start the local HTTP unsubscribe endpoint instead of running the pipeline. |
