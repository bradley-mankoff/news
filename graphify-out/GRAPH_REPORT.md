# Graph Report - news-deepen-run-finalizer  (2026-06-13)

## Corpus Check
- 39 files · ~74,163 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1123 nodes · 3214 edges · 45 communities (39 shown, 6 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 285 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1d0555e8`
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
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]

## God Nodes (most connected - your core abstractions)
1. `RunDiagnostics` - 82 edges
2. `Any` - 70 edges
3. `ProgressTracker` - 53 edges
4. `RunFinalizer` - 52 edges
5. `run_pipeline()` - 50 edges
6. `Any` - 45 edges
7. `ArticleCollectionAdapters` - 43 edges
8. `Any` - 43 edges
9. `ArticleCollectionRequest` - 42 edges
10. `load_runtime_config()` - 34 edges

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

## Communities (45 total, 6 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.09
Nodes (14): _clean_progress_message(), _generate_translation_text(), _load_translation_model_resources(), maybe_email_report(), ProgressTracker, run_logging(), run_pipeline(), _story_drafting_progress() (+6 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (74): _article_sort_datetime(), _article_source_identity(), _article_time_rank(), _build_story_tfidf_vectors(), _clean_story_title(), cluster_global_stories_by_similarity(), cosine_similarity(), filter_budgeted_targets_by_story_floor() (+66 more)

### Community 2 - "Community 2"
Cohesion: 0.09
Nodes (58): BaseHTTPRequestHandler, configured_removed_topic_env_vars(), load_run_presets(), normalize_preset_id(), run_preset_env(), _add_bool_option(), _add_option(), _body_preset_id() (+50 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (59): _articles_from_diagnostics(), _artifact_family(), backfill_outputs(), _backfill_run(), blocking_urls(), _bytes_label(), _cleanup_candidates(), cleanup_outputs() (+51 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (68): annotate_citation_precedence(), apply_citation_precedence(), _attributed_wire_orgs(), _canonical_source_key(), _citation_dependency_map(), _citation_group_numbers(), _citation_group_title(), citation_precedence_dependency_records() (+60 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (61): ArgumentParser, Element, Namespace, _best_language_label(), build_parser(), _clean_sample_text(), _count_items(), _decode_google_news_article_path() (+53 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (58): ArticleSummarizationRuntime, ArticleSummaryState, _build_article_summary_app(), build_article_summary_prompt_messages(), _notify_article_completed(), BaseMessage, Article summarization pass for retained story-cluster articles., run_article_summary_pass() (+50 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (30): _annotated_citation_sources(), _article_body_evidence(), _article_lookup_by_id(), article_summary_lookup_by_id(), build_story_synthesis_prompt_messages(), _citation_diagnostics_with_presence(), clean_story_synthesis_contradictions(), clean_story_synthesis_headline() (+22 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (38): _append_unique_urls(), _article_translation_decision(), _bool_env(), _bounded_env_float(), build_article_fallback_entry(), _build_article_heading(), build_email_subject(), _build_html_article_listing() (+30 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (74): AIMessage, ArticleCollectionRequest, ChatOpenAI, ModelSamplingSettings, _article_candidates_from_source_context(), ArticleCollectionAdapters, ArticleCollectionRequest, ArticleCollectionResult (+66 more)

### Community 10 - "Community 10"
Cohesion: 0.13
Nodes (24): codex_model_guard_active(), _coerce_int_value(), _configured_model_backend(), _configured_preset_id(), _configured_recipient_scope(), _configured_translation_model_backend(), _configured_translation_model_reference(), _direct_source_field_line() (+16 more)

### Community 11 - "Community 11"
Cohesion: 0.21
Nodes (20): ensure_codex_safe_model_reference(), build_chat_model(), _ensure_main_model_server_ready(), managed_model_server(), _managed_model_server_exit_message(), _managed_model_server_log_path(), managed_translation_model_server(), _managed_translation_model_server_log_path() (+12 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (42): _article_confirms_wire_attribution(), _budget_article_targets_for_summary(), capture_activity_snapshot(), _configured_source_display_name(), _confirm_wire_source_match(), _dedupe_story_drafts_for_global_selection(), _excluded_feed_item_reason(), _feed_item_matches_configured_source() (+34 more)

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
Cohesion: 0.13
Nodes (22): add_headline_overlay(), _build_html_synthesis(), clean_synthesis_for_publication(), _contains_disallowed_final_markup(), _enforce_text_free_image_prompt(), _fallback_image_prompt(), _format_plain_text_synthesis(), generate_image_art_brief() (+14 more)

### Community 18 - "Community 18"
Cohesion: 0.22
Nodes (13): _coerce_int(), estimate_message_token_count(), estimate_token_count(), _extract_token_usage_from_response(), _get_token_encoder(), invoke_with_retries(), _is_transient_model_error(), _model_call_bucket() (+5 more)

### Community 19 - "Community 19"
Cohesion: 0.11
Nodes (18): Advanced Tuning, CLI Commands, Default Run Knobs, Infrastructure, Model And Translation, NEWS_HISTORY_DB Variable, NEWS_MAX_ARTICLES_PER_SOURCE Variable, NEWS_MODEL_BASE_URL Variable (+10 more)

### Community 20 - "Community 20"
Cohesion: 0.13
Nodes (18): _duration_label(), _format_count_map(), _hours_label(), _join_or_none(), _last_event(), _model_call_bucket(), _model_token_totals(), Any (+10 more)

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (12): Connection, _article_embed_text(), _content_hash(), dedup_story_drafts(), embed_articles(), embed_texts(), _load_model(), _open_cache() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.19
Nodes (13): _article_scrape_deadline(), _build_feed_fallback_text(), _decode_google_news_article_path(), _download_article_html(), _google_news_query_target(), _is_google_news_url(), Decode modern Google News RSS article URLs (CBMi... base64 path encoding)., Follow Google News redirect links without treating Google pages as articles. (+5 more)

### Community 23 - "Community 23"
Cohesion: 0.21
Nodes (20): _coerce_bool_value(), _coerce_float_value(), _coerce_pause_value(), _coerce_source_text_list(), _configured_source_scope(), load_recipients(), load_sources(), load_top_funnel_providers() (+12 more)

### Community 24 - "Community 24"
Cohesion: 0.28
Nodes (9): anthropics/claude-code-action, Claude PR Review Workflow Setup Script, REVIEW.md Standards File, ANTHROPIC_API_KEY Secret, actions/checkout@v4, anthropics/claude-code-action@v1, claude-haiku-4-5 Model, Claude PR Review GitHub Workflow (+1 more)

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (9): build_fallback_final_synthesis_preview(), _fallback_synthesis_paragraph_from_summaries(), _first_sentences(), _persist_article_summaries_debug(), Create grouped prose when final synthesis repeatedly returns empty., _report_entry_debug_record(), _report_entry_debug_records(), _report_story_label() (+1 more)

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
Cohesion: 0.19
Nodes (17): _apply_cli_preset(), _consume_preset_arg(), main(), _print_codex_model_server_command(), _print_model_server_command(), Command-line entry point for the daily news pipeline., _run_history(), _run_pipeline_command() (+9 more)

### Community 31 - "Community 31"
Cohesion: 0.25
Nodes (7): 1. RTK Commands & Env, 2. graphify, 3. Karpathy Execution Guidelines, A. Think Before Coding (No Assumptions), B. Simplicity First (No Speculation), C. Surgical Changes (No Renovation), D. Goal-Driven Execution (Verify)

### Community 32 - "Community 32"
Cohesion: 0.47
Nodes (6): _base64url_decode(), _base64url_encode(), build_unsubscribe_token(), build_unsubscribe_url(), parse_unsubscribe_token(), _unsubscribe_signing_secret()

### Community 33 - "Community 33"
Cohesion: 0.29
Nodes (7): Default Run Preset, Dev Run Preset, gemma-e2b-tiny Alias, NEWS_SOURCE_SCOPE Variable, NEWS_MIN_ARTICLES_PER_STORY Variable, NEWS_SOURCE_SCOPE Variable, NEWS_SOURCES_YAML Variable

### Community 34 - "Community 34"
Cohesion: 0.50
Nodes (5): Model Aliases, NEWS_MODEL Variable, qwen-9b-dense Alias, Model Aliases Table, NEWS_MODEL Variable

### Community 35 - "Community 35"
Cohesion: 0.24
Nodes (7): is_gemma_4_model_reference(), _load_password_from_env_json(), load_runtime_config(), _article(), _article_ids(), Gemma4ArticleBudgetTests, _story()

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (3): RTK Database Path, RTK Shell Command Rule, Terse Output Protocol

### Community 37 - "Community 37"
Cohesion: 0.24
Nodes (11): build_model_server_command(), configured_min_articles_per_story(), configured_model_profile(), configured_story_cluster_similarity_threshold(), _default_total_article_summary_cap(), _float_env(), _int_env(), ModelRuntimeProfile (+3 more)

### Community 43 - "Community 43"
Cohesion: 0.24
Nodes (10): _bool_env(), _configured_model_name(), _configured_model_profile_key(), _configured_model_reference(), _default_article_summary_concurrency(), _default_story_synthesis_concurrency(), infer_model_profile_key(), is_codex_test_model_reference() (+2 more)

### Community 44 - "Community 44"
Cohesion: 0.50
Nodes (3): Article Collection Funnel, Context, Run Session

## Ambiguous Edges - Review These
- `Bradley Mankoff Recipient` → `Production Run Preset`  [AMBIGUOUS]
  config/run_presets.yaml · relation: conceptually_related_to

## Knowledge Gaps
- **73 isolated node(s):** `BaseMessage`, `NewsSource`, `Any`, `Connection`, `ArgumentParser` (+68 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Bradley Mankoff Recipient` and `Production Run Preset`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `RunDiagnostics` connect `Community 9` to `Community 0`, `Community 3`, `Community 8`, `Community 11`, `Community 12`, `Community 20`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `load_sources()` connect `Community 23` to `Community 2`, `Community 4`, `Community 6`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Why does `load_runtime_config()` connect `Community 35` to `Community 2`, `Community 37`, `Community 8`, `Community 9`, `Community 10`, `Community 43`, `Community 23`, `Community 30`?**
  _High betweenness centrality (0.042) - this node is a cross-community bridge._
- **Are the 56 inferred relationships involving `RunDiagnostics` (e.g. with `AIMessage` and `ArticleCollectionRequest`) actually correct?**
  _`RunDiagnostics` has 56 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `Any` (e.g. with `ArticleCollectionAdapters` and `ArticleCollectionRequest`) actually correct?**
  _`Any` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `ProgressTracker` (e.g. with `ArticleCollectionAdapters` and `ArticleCollectionRequest`) actually correct?**
  _`ProgressTracker` has 12 INFERRED edges - model-reasoned connections that need verification._