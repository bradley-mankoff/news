# Daily News Pipeline

This repo now has one master script, `todays_news.py`, backed by the `news_pipeline/` package.
The old Iran-specific fork has been removed from the active code path.

## Run

Set up the project environment first:

```bash
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

The platform check should print `arm64`.

Start the local OpenAI-compatible model server first:

```bash
uv run todays_news.py --model-server-command
```

Run the printed command in a second terminal. The command is
profile-aware, so the large model gets conservative MLX memory settings and the
small model gets more aggressive KV cache/headroom settings.

MLX requires Apple Silicon. If `uv run mlx_lm.server ...` says there is no compatible `mlx` wheel, this machine cannot run the MLX server directly; point the pipeline at another OpenAI-compatible server instead.

If the command says `No module named 'mlx_lm'` on an Apple Silicon Mac, your `.venv` may have been created as x86_64/Rosetta. Rebuild it with arm64 Python:

```bash
uv run python -c 'import platform; print(platform.machine())'
mv .venv ".venv-x86_64-backup-$(date +%Y%m%d-%H%M%S)"
/opt/homebrew/bin/uv sync --python /opt/homebrew/bin/python3.12
uv run python -c 'import platform; print(platform.machine())'
```

The final command should print `arm64`; then start the model server again.

Then run the pipeline:

```bash
uv run todays_news.py
```

Production email/history mode:

```bash
NEWS_DEV=0 uv run todays_news.py
```

Local-production review mode runs the full production-width news sweep and
records production URL history, but sends the report only to Bradley:

```bash
uv run local_prod_news.py
# or
NEWS_RUN_MODE=local-prod uv run todays_news.py
```

Unsubscribe server:

```bash
uv run todays_news.py --serve-unsubscribe
```

## Configuration

Edit `config/sources.yaml` to change both topic-discovery providers and article feeds.

The top-of-funnel is now stage-aware:

- `top_funnel_providers` seed and/or validate topic candidates.
- `sources` enrich selected topics with actual articles for summarization.
- Provider metadata includes `region`, `frame`, `provider_type`, `intended_role`, `weight`, and stage flags: `can_seed_topics`, `can_validate_topics`, `can_enrich_coverage`.

Topic discovery is intentionally not quota-driven. Seed-capable providers produce a larger candidate set; validation-capable providers test those candidates; then a soft weighted sampler nudges the long-run mix toward a US-aware western frame with room for non-western/global-south framing when the day warrants it. AP and Reuters are configured as validation signals by default, so they can confirm prominence without automatically dominating every stage. When a provider both seeds and validates globally, it does not get credit for validating a topic it already seeded.

If model topic clustering fails, fallback topics are now deterministic cross-provider headline clusters, not single weak headline-token topics. A fallback topic must be supported by at least two top-of-funnel providers before selection, uses boundary-aware keyword matching, and requires a higher article relevance score than model-authored topics.

Example provider:

```yaml
top_funnel_providers:
  - key: al_jazeera_top
    name: Al Jazeera
    provider_type: international_rss
    intended_role: non-western and global-south seed and validation counterweight
    region: qatar/global
    frame: global south/non-western
    weight: 0.9
    can_seed_topics: true
    can_validate_topics: true
    can_enrich_coverage: false
    fetcher: rss
    url: https://www.aljazeera.com/xml/rss/all.xml
```

Edit `config/recipients.yaml` to change recipients, pause delivery, or add a `personal_prompt`.

Model selection uses friendly aliases so the pipeline can refer to the latest intended version without putting version numbers in everyday commands:

- `gemma-26b-moe` -> `mlx-community/gemma-4-26B-A4B-it-heretic-4bit`
- `qwen-9b-dense` -> `TheCluster/Qwen3.5-9B-Heretic-MLX-mxfp4`

Model runtime profiles are inferred from the alias unless `NEWS_MODEL_PROFILE`
is set explicitly:

- `big_conservative` for `gemma-26b-moe`: low concurrency, small KV cache, smaller article/input budget.
- `small_aggressive` for `qwen-9b-dense`: bounded local concurrency/cache with a smaller article/input budget.

Sampling is task-aware within each profile. Translation, topic clustering,
article summaries, final synthesis, and title generation can all use different
decode settings. Qwen 9B profiles use the Hugging Face model-card instruct
preset for translation, article summaries, and titles; topic clustering and
final synthesis use the card's reasoning preset. The Gemma profile uses its own
conservative task mix: deterministic translation, narrower factual summaries,
broader clustering/synthesis, and slightly freer title generation.

The default is `gemma-26b-moe`. For the safest large-model test:

```bash
NEWS_MODEL=gemma-26b-moe uv run todays_news.py --model-server-command
NEWS_MODEL=gemma-26b-moe uv run todays_news.py
```

For the small-model comparison:

```bash
NEWS_MODEL=qwen-9b-dense uv run todays_news.py --model-server-command
NEWS_MODEL=qwen-9b-dense uv run todays_news.py
```

To change the default without editing code:

```bash
NEWS_DEFAULT_MODEL=qwen-9b-dense uv run todays_news.py
```

`NEWS_MODEL_NAME` still works as a raw Hugging Face repo ID override when you want to bypass the alias list.

Useful environment overrides:

```bash
NEWS_RUN_MODE=dev
NEWS_DEV=1
NEWS_DEV_RECIPIENT=bradley@mankoff.com
NEWS_NUM_TOP_TOPICS=4
NEWS_TOP_OF_FUNNEL_PER_PROVIDER=10
NEWS_TOP_TOPIC_PROBES=4
NEWS_MAX_ARTICLES_PER_SOURCE=6
NEWS_RECENT_WINDOW_HOURS=24
NEWS_MODEL=gemma-26b-moe
NEWS_DEFAULT_MODEL=gemma-26b-moe
NEWS_MODEL_NAME=mlx-community/gemma-4-26B-A4B-it-heretic-4bit
NEWS_MODEL_PROFILE=big_conservative
NEWS_MODEL_BASE_URL=http://127.0.0.1:8080/v1
NEWS_MODEL_MAX_INPUT_TOKENS=7000
NEWS_ARTICLE_SUMMARY_CONCURRENCY=1
NEWS_ARTICLE_TEXT_TOKEN_LIMIT=6000
NEWS_TOTAL_ARTICLE_SUMMARY_CAP=28
NEWS_PER_TOPIC_ARTICLE_SUMMARY_CAP=7
NEWS_TOPIC_CLUSTERING_MAX_TOKENS=1800
NEWS_TRANSLATION_MAX_TOKENS=1800
NEWS_ARTICLE_SUMMARY_MAX_TOKENS=1600
NEWS_FINAL_SYNTHESIS_MAX_TOKENS=2200
NEWS_TITLE_GENERATION_MAX_TOKENS=50
NEWS_SERVER_DECODE_CONCURRENCY=1
NEWS_SERVER_PROMPT_CONCURRENCY=1
NEWS_SERVER_PREFILL_STEP_SIZE=512
NEWS_SERVER_PROMPT_CACHE_SIZE=2
NEWS_SERVER_PROMPT_CACHE_BYTES=512MB
NEWS_SERVER_MAX_TOKENS=2500
NEWS_MODEL_TEMPERATURE=0.7
NEWS_MODEL_TOP_P=0.8
NEWS_MODEL_TOP_K=20
NEWS_MODEL_MIN_P=0.0
NEWS_MODEL_PRESENCE_PENALTY=1.5
NEWS_MODEL_REPETITION_PENALTY=1.0
NEWS_MODEL_REASONING_TEMPERATURE=1.0
NEWS_MODEL_REASONING_TOP_P=1.0
NEWS_MODEL_REASONING_TOP_K=40
NEWS_MODEL_REASONING_MIN_P=0.0
NEWS_MODEL_REASONING_PRESENCE_PENALTY=2.0
NEWS_MODEL_REASONING_REPETITION_PENALTY=1.0
NEWS_MODEL_TRANSLATION_TEMPERATURE=0.0
NEWS_MODEL_TOPIC_CLUSTERING_TEMPERATURE=0.15
NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE=0.2
NEWS_MODEL_FINAL_SYNTHESIS_TEMPERATURE=0.3
NEWS_MODEL_TITLE_GENERATION_TEMPERATURE=0.45
NEWS_IMAGE_ENABLED=1
NEWS_IMAGE_FAIL_ON_ERROR=0
NEWS_IMAGE_WIDTH=1024
NEWS_IMAGE_HEIGHT=1024
NEWS_IMAGE_STEPS=4
NEWS_IMAGE_CROP_BOTTOM_RATIO=0.12
NEWS_IMAGE_MODEL_ID=Runpod/FLUX.2-klein-4B-mflux-4bit
NEWS_IMAGE_BASE_MODEL=flux2-klein-4b
NEWS_DEV_SOURCE_LIMIT=3
NEWS_DEV_NUM_TOPICS=2
NEWS_OUTPUT_DIR=output/daily_outputs
NEWS_SMTP_PASSWORD='...'
```

Image generation uses FLUX.2 klein through the `mflux` CLI after final synthesis
and after the text model has generated both the image prompt and the separate
footer headline. The generated image prompt explicitly asks for no embedded
typography; the readable headline is overlaid afterward by code. By default,
image generation is enabled but fail-open, so a local mflux/Pillow issue records
a warning and the report still saves/sends. Set `NEWS_IMAGE_FAIL_ON_ERROR=1` if
you want image failures to stop the run.

In `dev` mode, the run is intentionally narrow: by default it selects up to two
topics, scans the first three article sources, and summarizes at most two
articles per selected topic.

## Outputs

Each run writes to `output/daily_outputs/YYYY-MM-DD/`:

- `news_report_*.txt`: final email/report body.
- `news_report_*_image.png`: generated FLUX image with the code-rendered headline footer.
- `news_report_*_raw.png`: raw text-free FLUX image before the footer overlay.
- `news_report_*_image_prompt.txt`: generated image-model prompt.
- `news_report_*_image_stats.json`: image model, seed, timing, and output paths.
- `topics_*.json`: raw top-story headlines and LLM-selected topics.
- `run_details_*.json`: granular machine-readable funnel log.
- `run_details_*.md`: human-readable backend audit trail.
- `dev_used_urls.txt` or `used_urls.txt`: URL history for this run.

The run details include which top-of-funnel providers connected, the headline counts from each, merged top stories, selected topics, generated keyword and boost phrase lists, per-source feed item counts, topic matches, dedupe/history rejections, and report outputs.

Topic diagnostics also record why each topic was selected: seed providers, validation providers, frame tags, frame counts, validation matches, and the soft selection weight. The final report does not use discovery headlines as factual evidence; it is grounded only in article summaries gathered after topic selection.

Model diagnostics record the selected model profile, suggested server command,
input/article caps, article-budget inclusions and drops, retry/fallback counts,
and final synthesis token estimates. Report filenames include the model profile
so same-day big/small comparison runs are easier to distinguish.

The pipeline also refreshes the managed block in `.cursorignore` on each run so Cursor can see only the newest dated output folder and ignore stale generated runs.
