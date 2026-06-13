# Graph Report - news  (2026-06-13)

## Corpus Check
- 42 files · ~75,089 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1155 nodes · 3363 edges · 42 communities (34 shown, 8 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 324 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `4919e68a`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 44|Community 44]]

## God Nodes (most connected - your core abstractions)
1. `RunDiagnostics` - 82 edges
2. `Any` - 69 edges
3. `ProgressTracker` - 53 edges
4. `RunFinalizer` - 52 edges
5. `run_pipeline()` - 50 edges
6. `Any` - 45 edges
7. `ArticleCollectionAdapters` - 43 edges
8. `ArticleCollectionRequest` - 42 edges
9. `Any` - 40 edges
10. `RuntimeConfig` - 34 edges

## Surprising Connections (you probably didn't know these)
- `RunDiagnostics` --uses--> `RunDiagnostics`  [INFERRED]
  tests/test_history_store.py → news_pipeline/diagnostics.py
- `Run Presets` --references--> `Local Production Run Preset`  [INFERRED]
  README.md → config/run_presets.yaml
- `Model Aliases` --semantically_similar_to--> `Model Aliases Table`  [INFERRED] [semantically similar]
  README.md → SETTINGS.md
- `REVIEW.md Standards File` --semantically_similar_to--> `REVIEW.md Standards File`  [INFERRED] [semantically similar]
  .github/workflows/claude-review.yml → claude_pr_tool_settings.txt
- `HistoryStoreTests` --uses--> `RunDiagnostics`  [INFERRED]
  tests/test_history_store.py → news_pipeline/diagnostics.py

## Import Cycles
- 1-file cycle: `news_pipeline/pipeline.py -> news_pipeline/pipeline.py`
- 1-file cycle: `news_pipeline/citations.py -> news_pipeline/citations.py`
- 1-file cycle: `news_pipeline/source_checks.py -> news_pipeline/source_checks.py`
- 1-file cycle: `news_pipeline/story_clustering.py -> news_pipeline/story_clustering.py`

## Communities (42 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.08
Nodes (17): _clean_progress_message(), generate_image_with_mflux(), generate_report_image_art(), get_active_recipient_config(), load_recipient_config(), maybe_email_report(), ProgressTracker, Load active recipient metadata from config/recipients.yaml. (+9 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (74): _article_sort_datetime(), _article_source_identity(), _article_time_rank(), _build_story_tfidf_vectors(), _clean_story_title(), cluster_global_stories_by_similarity(), cosine_similarity(), filter_budgeted_targets_by_story_floor() (+66 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (82): BaseHTTPRequestHandler, configured_removed_topic_env_vars(), _apply_delete(), _apply_languages(), _apply_line_edits(), apply_source_catalog_patch(), _apply_translation(), _apply_upsert() (+74 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (58): _articles_from_diagnostics(), _artifact_family(), backfill_outputs(), _backfill_run(), blocking_urls(), _bytes_label(), _cleanup_candidates(), cleanup_outputs() (+50 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (68): annotate_citation_precedence(), apply_citation_precedence(), _attributed_wire_orgs(), _canonical_source_key(), _citation_dependency_map(), _citation_group_numbers(), _citation_group_title(), citation_precedence_dependency_records() (+60 more)

### Community 5 - "Community 5"
Cohesion: 0.10
Nodes (53): ArgumentParser, Element, Namespace, _best_language_label(), build_parser(), _clean_sample_text(), _count_items(), _decode_google_news_article_path() (+45 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (51): ArticleSummarizationRuntime, ArticleSummaryState, _build_article_summary_app(), build_article_summary_prompt_messages(), _notify_article_completed(), BaseMessage, Article summarization pass for retained story-cluster articles., run_article_summary_pass() (+43 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (31): _annotated_citation_sources(), _article_body_evidence(), _article_lookup_by_id(), article_summary_lookup_by_id(), build_story_synthesis_prompt_messages(), _citation_diagnostics_with_presence(), clean_story_synthesis_contradictions(), clean_story_synthesis_headline() (+23 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (39): add_headline_overlay(), _append_unique_urls(), _article_scrape_deadline(), _bool_env(), _bounded_env_float(), build_article_fallback_entry(), _build_article_heading(), build_fallback_final_synthesis_preview() (+31 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (76): AIMessage, ArticleCollectionRequest, ChatOpenAI, ModelSamplingSettings, _article_candidates_from_source_context(), ArticleCollectionAdapters, ArticleCollectionRequest, ArticleCollectionResult (+68 more)

### Community 10 - "Community 10"
Cohesion: 0.05
Nodes (89): _apply_cli_preset(), _consume_preset_arg(), main(), _print_codex_model_server_command(), _print_model_server_command(), Command-line entry point for the daily news pipeline., _run_history(), _run_pipeline_command() (+81 more)

### Community 11 - "Community 11"
Cohesion: 0.19
Nodes (20): _duckdb(), build_chat_model(), _ensure_main_model_server_ready(), managed_model_server(), _managed_model_server_exit_message(), _managed_model_server_log_path(), managed_translation_model_server(), _managed_translation_model_server_log_path() (+12 more)

### Community 12 - "Community 12"
Cohesion: 0.08
Nodes (43): _article_confirms_wire_attribution(), capture_activity_snapshot(), _configured_source_display_name(), _confirm_wire_source_match(), _dedupe_story_drafts_for_global_selection(), _excluded_feed_item_reason(), _feed_item_matches_configured_source(), _feed_item_source_labels() (+35 more)

### Community 13 - "Community 13"
Cohesion: 0.40
Nodes (4): ADR 0001: Record architecture decisions in docs/adr, Consequences, Context, Decision

### Community 14 - "Community 14"
Cohesion: 0.40
Nodes (4): ADR 0002: Run Session owns daily run lifecycle state, Consequences, Context, Decision

### Community 15 - "Community 15"
Cohesion: 0.40
Nodes (4): ADR 0003: Run finalization finishes recorded run outcomes, Consequences, Context, Decision

### Community 16 - "Community 16"
Cohesion: 0.12
Nodes (19): config/sources.yaml, AFP Source, Al Jazeera Source, Associated Press Source, Ars Technica Source, BBC World News Source, CNA Source, Core Tier Sources (+11 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (17): _build_html_synthesis(), clean_synthesis_for_publication(), _contains_disallowed_final_markup(), _enforce_text_free_image_prompt(), _fallback_image_prompt(), _format_plain_text_synthesis(), generate_image_art_brief(), generate_report_title() (+9 more)

### Community 18 - "Community 18"
Cohesion: 0.13
Nodes (18): _duration_label(), _format_count_map(), _hours_label(), _join_or_none(), _last_event(), _model_call_bucket(), _model_token_totals(), Any (+10 more)

### Community 19 - "Community 19"
Cohesion: 0.11
Nodes (18): Advanced Tuning, CLI Commands, Default Run Knobs, Infrastructure, Model And Translation, NEWS_HISTORY_DB Variable, NEWS_MAX_ARTICLES_PER_SOURCE Variable, NEWS_MODEL_BASE_URL Variable (+10 more)

### Community 20 - "Community 20"
Cohesion: 0.17
Nodes (13): _base64url_decode(), _base64url_encode(), _build_html_article_listing(), _build_plain_text_article_listing(), build_report_body(), build_report_html(), build_unsubscribe_token(), build_unsubscribe_url() (+5 more)

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (12): Connection, _article_embed_text(), _content_hash(), dedup_story_drafts(), embed_articles(), embed_texts(), _load_model(), _open_cache() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.27
Nodes (10): _build_feed_fallback_text(), _decode_google_news_article_path(), _google_news_query_target(), _is_google_news_url(), Decode modern Google News RSS article URLs (CBMi... base64 path encoding)., Follow Google News redirect links without treating Google pages as articles., Follow Google News redirect to get the real article URL when possible., _resolve_and_scrape_feed_article() (+2 more)

### Community 23 - "Community 23"
Cohesion: 0.22
Nodes (13): _coerce_int(), estimate_message_token_count(), estimate_token_count(), _extract_token_usage_from_response(), _get_token_encoder(), invoke_with_retries(), _is_transient_model_error(), _model_call_bucket() (+5 more)

### Community 24 - "Community 24"
Cohesion: 0.28
Nodes (9): anthropics/claude-code-action, Claude PR Review Workflow Setup Script, REVIEW.md Standards File, ANTHROPIC_API_KEY Secret, actions/checkout@v4, anthropics/claude-code-action@v1, claude-haiku-4-5 Model, Claude PR Review GitHub Workflow (+1 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (16): CLI, Configuration, Daily News Pipeline, News History DuckDB, Image, Model, NEWS_IMAGE_ENABLED Variable, News Run Command (+8 more)

### Community 27 - "Community 27"
Cohesion: 0.29
Nodes (7): Local Production Run Preset, Loose Local Production Run Preset, Production Run Preset, gemma-26b-moe Alias, NEWS_BLOCK_REUSED_URLS Variable, NEWS_BLOCK_REUSED_URLS Variable, NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD Variable

### Community 28 - "Community 28"
Cohesion: 0.29
Nodes (7): NEWS_RECIPIENT_SCOPE Variable, config/recipients.yaml, Aidan Coon Recipient, Bradley Mankoff Recipient, NEWS_BRADLEY_RECIPIENT Variable, NEWS_RECIPIENT_SCOPE Variable, NEWS_RECIPIENTS_YAML Variable

### Community 30 - "Community 30"
Cohesion: 0.25
Nodes (8): _article_translation_decision(), _generate_translation_text(), _infer_script_translation_language(), _load_translation_model_resources(), _normalize_translation_language(), _text_looks_non_english(), _translate_text_with_translation_model(), _with_translation_metadata()

### Community 31 - "Community 31"
Cohesion: 0.25
Nodes (7): 1. RTK Commands & Env, 2. graphify, 3. Karpathy Execution Guidelines, A. Think Before Coding (No Assumptions), B. Simplicity First (No Speculation), C. Surgical Changes (No Renovation), D. Goal-Driven Execution (Verify)

### Community 33 - "Community 33"
Cohesion: 0.29
Nodes (7): Default Run Preset, Dev Run Preset, gemma-e2b-tiny Alias, NEWS_SOURCE_SCOPE Variable, NEWS_MIN_ARTICLES_PER_STORY Variable, NEWS_SOURCE_SCOPE Variable, NEWS_SOURCES_YAML Variable

### Community 34 - "Community 34"
Cohesion: 0.50
Nodes (5): Model Aliases, NEWS_MODEL Variable, qwen-9b-dense Alias, Model Aliases Table, NEWS_MODEL Variable

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (3): RTK Database Path, RTK Shell Command Rule, Terse Output Protocol

### Community 44 - "Community 44"
Cohesion: 0.40
Nodes (4): Article Collection Funnel, Context, Run Session, Source Catalog

## Ambiguous Edges - Review These
- `Bradley Mankoff Recipient` → `Production Run Preset`  [AMBIGUOUS]
  config/run_presets.yaml · relation: conceptually_related_to

## Knowledge Gaps
- **72 isolated node(s):** `allow`, `BaseMessage`, `Any`, `Connection`, `SourceCatalogEdit` (+67 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Bradley Mankoff Recipient` and `Production Run Preset`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `RunDiagnostics` connect `Community 9` to `Community 0`, `Community 3`, `Community 8`, `Community 11`, `Community 12`, `Community 18`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `load_runtime_config()` connect `Community 10` to `Community 8`, `Community 9`, `Community 2`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Why does `ProgressTracker` connect `Community 0` to `Community 8`, `Community 9`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Are the 56 inferred relationships involving `RunDiagnostics` (e.g. with `AIMessage` and `ArticleCollectionRequest`) actually correct?**
  _`RunDiagnostics` has 56 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `Any` (e.g. with `ArticleCollectionAdapters` and `ArticleCollectionRequest`) actually correct?**
  _`Any` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `ProgressTracker` (e.g. with `ArticleCollectionAdapters` and `ArticleCollectionRequest`) actually correct?**
  _`ProgressTracker` has 12 INFERRED edges - model-reasoned connections that need verification._