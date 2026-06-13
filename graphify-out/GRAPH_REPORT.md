# Graph Report - .  (2026-06-09)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 972 nodes · 2665 edges · 43 communities (36 shown, 7 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 80 edges (avg confidence: 0.58)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `18a16e65`
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

## God Nodes (most connected - your core abstractions)
1. `Any` - 63 edges
2. `run_pipeline()` - 47 edges
3. `RunDiagnostics` - 46 edges
4. `ProgressTracker` - 46 edges
5. `Any` - 43 edges
6. `Any` - 41 edges
7. `load_runtime_config()` - 34 edges
8. `Path` - 27 edges
9. `Any` - 24 edges
10. `ModelSamplingSettings` - 23 edges

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

## Communities (43 total, 7 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (48): ChatOpenAI, ensure_codex_safe_model_reference(), _attach_pending_activity_snapshots(), build_chat_model(), _clean_progress_message(), _collect_source_contexts(), _ensure_main_model_server_ready(), _finalize_failed_run() (+40 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (74): _article_sort_datetime(), _article_source_identity(), _article_time_rank(), _build_story_tfidf_vectors(), _clean_story_title(), cluster_global_stories_by_similarity(), cosine_similarity(), filter_budgeted_targets_by_story_floor() (+66 more)

### Community 2 - "Community 2"
Cohesion: 0.09
Nodes (58): BaseHTTPRequestHandler, configured_removed_topic_env_vars(), load_run_presets(), normalize_preset_id(), run_preset_env(), _add_bool_option(), _add_option(), _body_preset_id() (+50 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (57): _articles_from_diagnostics(), _artifact_family(), backfill_outputs(), _backfill_run(), blocking_urls(), _bytes_label(), _cleanup_candidates(), cleanup_outputs() (+49 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (63): annotate_citation_precedence(), apply_citation_precedence(), _attributed_wire_orgs(), _canonical_source_key(), _citation_dependency_map(), _citation_group_numbers(), _citation_group_title(), citation_precedence_dependency_records() (+55 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (61): ArgumentParser, Element, Namespace, _best_language_label(), build_parser(), _clean_sample_text(), _count_items(), _decode_google_news_article_path() (+53 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (51): ArticleSummaryState, _build_article_summary_app(), build_article_summary_prompt_messages(), _notify_article_completed(), BaseMessage, Article summarization pass for retained story-cluster articles., run_article_summary_pass(), _article_summarization_runtime() (+43 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (37): Daily news pipeline package., _annotated_citation_sources(), _article_body_evidence(), _article_lookup_by_id(), article_summary_lookup_by_id(), build_story_synthesis_prompt_messages(), _citation_diagnostics_with_presence(), clean_story_synthesis_contradictions() (+29 more)

### Community 8 - "Community 8"
Cohesion: 0.07
Nodes (37): add_headline_overlay(), _append_unique_urls(), _article_scrape_deadline(), _article_translation_decision(), _base64url_decode(), _base64url_encode(), build_email_subject(), build_unsubscribe_url() (+29 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (21): _duration_label(), _format_count_map(), _hours_label(), _join_or_none(), _last_event(), _model_call_bucket(), _model_token_totals(), Any (+13 more)

### Community 10 - "Community 10"
Cohesion: 0.16
Nodes (27): _bool_env(), build_model_server_command(), codex_model_guard_active(), configured_min_articles_per_story(), _configured_model_name(), configured_model_profile(), _configured_model_profile_key(), _configured_model_reference() (+19 more)

### Community 11 - "Community 11"
Cohesion: 0.14
Nodes (23): ModelSamplingSettings, _budget_article_targets_for_summary(), capture_activity_snapshot(), _dedupe_story_drafts_for_global_selection(), _format_translation_prompt(), _model_extra_body(), _ordered_unique_article_ids(), _parse_activity_command_output() (+15 more)

### Community 12 - "Community 12"
Cohesion: 0.13
Nodes (22): _article_confirms_wire_attribution(), _configured_source_display_name(), _confirm_wire_source_match(), _excluded_feed_item_reason(), _feed_item_matches_configured_source(), _feed_item_source_labels(), _feed_title_source_suffix(), _fetch_source_context_for_collection() (+14 more)

### Community 13 - "Community 13"
Cohesion: 0.19
Nodes (17): _apply_cli_preset(), _consume_preset_arg(), main(), _print_codex_model_server_command(), _print_model_server_command(), Command-line entry point for the daily news pipeline., _run_history(), _run_pipeline_command() (+9 more)

### Community 14 - "Community 14"
Cohesion: 0.23
Nodes (19): _coerce_bool_value(), _coerce_float_value(), _coerce_int_value(), _coerce_pause_value(), _coerce_source_text_list(), _load_password_from_env_json(), load_recipients(), load_sources() (+11 more)

### Community 15 - "Community 15"
Cohesion: 0.18
Nodes (12): _configured_model_backend(), _configured_translation_model_backend(), _default_article_summary_concurrency(), _default_story_synthesis_concurrency(), infer_model_backend(), infer_model_profile_key(), is_codex_test_model_reference(), is_gemma_4_model_reference() (+4 more)

### Community 16 - "Community 16"
Cohesion: 0.12
Nodes (19): config/sources.yaml, AFP Source, Al Jazeera Source, Associated Press Source, Ars Technica Source, BBC World News Source, CNA Source, Core Tier Sources (+11 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (17): _build_html_synthesis(), clean_synthesis_for_publication(), _contains_disallowed_final_markup(), _enforce_text_free_image_prompt(), _fallback_image_prompt(), _format_plain_text_synthesis(), generate_image_art_brief(), generate_report_title() (+9 more)

### Community 18 - "Community 18"
Cohesion: 0.20
Nodes (15): AIMessage, _coerce_int(), estimate_message_token_count(), estimate_token_count(), extract_prompt_tokens_from_response(), _extract_token_usage_from_response(), _get_token_encoder(), invoke_with_retries() (+7 more)

### Community 19 - "Community 19"
Cohesion: 0.15
Nodes (14): NEWS_SOURCE_SCOPE Variable, NEWS_HISTORY_DB Variable, NEWS_MAX_ARTICLES_PER_SOURCE Variable, NEWS_MODEL_BASE_URL Variable, NEWS_OUTPUT_DIR Variable, NEWS_PRESET Variable, NEWS_RECENT_WINDOW_HOURS Variable, SMTP Configuration Variables (+6 more)

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (12): Connection, _article_embed_text(), _content_hash(), dedup_story_drafts(), embed_articles(), embed_texts(), _load_model(), _open_cache() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.27
Nodes (10): _build_feed_fallback_text(), _decode_google_news_article_path(), _google_news_query_target(), _is_google_news_url(), Decode modern Google News RSS article URLs (CBMi... base64 path encoding)., Follow Google News redirect links without treating Google pages as articles., Follow Google News redirect to get the real article URL when possible., _resolve_and_scrape_feed_article() (+2 more)

### Community 23 - "Community 23"
Cohesion: 0.29
Nodes (7): _build_html_article_listing(), _build_plain_text_article_listing(), build_report_body(), build_report_html(), _collect_grouped_headlines(), _extract_first_name(), CitationIntegrationTests

### Community 24 - "Community 24"
Cohesion: 0.28
Nodes (9): anthropics/claude-code-action, Claude PR Review Workflow Setup Script, REVIEW.md Standards File, ANTHROPIC_API_KEY Secret, actions/checkout@v4, anthropics/claude-code-action@v1, claude-haiku-4-5 Model, Claude PR Review GitHub Workflow (+1 more)

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (9): build_fallback_final_synthesis_preview(), _fallback_synthesis_paragraph_from_summaries(), _first_sentences(), _persist_article_summaries_debug(), Create grouped prose when final synthesis repeatedly returns empty., _report_entry_debug_record(), _report_entry_debug_records(), _report_story_label() (+1 more)

### Community 26 - "Community 26"
Cohesion: 0.25
Nodes (8): Daily News Pipeline, News History DuckDB, NEWS_IMAGE_ENABLED Variable, News Run Command, News UI Command, Output Directory Structure, UV Python Project, NEWS_IMAGE_ENABLED Variable

### Community 27 - "Community 27"
Cohesion: 0.29
Nodes (7): Local Production Run Preset, Loose Local Production Run Preset, Production Run Preset, gemma-26b-moe Alias, NEWS_BLOCK_REUSED_URLS Variable, NEWS_BLOCK_REUSED_URLS Variable, NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD Variable

### Community 28 - "Community 28"
Cohesion: 0.29
Nodes (7): NEWS_RECIPIENT_SCOPE Variable, config/recipients.yaml, Aidan Coon Recipient, Bradley Mankoff Recipient, NEWS_BRADLEY_RECIPIENT Variable, NEWS_RECIPIENT_SCOPE Variable, NEWS_RECIPIENTS_YAML Variable

### Community 29 - "Community 29"
Cohesion: 0.57
Nodes (3): _article(), _article_ids(), _story()

### Community 30 - "Community 30"
Cohesion: 0.33
Nodes (6): _latest_run_output_patterns(), Keep assistant context tools focused on core files plus the newest run., RuntimeConfig, _strip_managed_block(), sync_assistant_context_latest_output(), _sync_cursorignore_latest_output()

### Community 31 - "Community 31"
Cohesion: 0.50
Nodes (5): _direct_source_field_line(), _preferred_translation_insert_line(), _source_block_key(), _source_block_ranges(), write_source_translation_flags()

### Community 32 - "Community 32"
Cohesion: 0.50
Nodes (5): build_article_fallback_entry(), _build_article_heading(), _extract_sentences(), _format_article_metadata(), normalize_report_entry()

### Community 33 - "Community 33"
Cohesion: 0.50
Nodes (5): Default Run Preset, Dev Run Preset, gemma-e2b-tiny Alias, Run Presets, NEWS_MIN_ARTICLES_PER_STORY Variable

### Community 34 - "Community 34"
Cohesion: 0.50
Nodes (5): Model Aliases, NEWS_MODEL Variable, qwen-9b-dense Alias, Model Aliases Table, NEWS_MODEL Variable

### Community 35 - "Community 35"
Cohesion: 0.50
Nodes (4): _article_candidates_from_source_context(), gather_article_candidates_for_source(), _normalize_url_for_dedupe(), Loose URL canonicalization for cross-provider dedupe of the same article.     Dr

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (3): RTK Database Path, RTK Shell Command Rule, Terse Output Protocol

## Ambiguous Edges - Review These
- `Bradley Mankoff Recipient` → `Production Run Preset`  [AMBIGUOUS]
  config/run_presets.yaml · relation: conceptually_related_to

## Knowledge Gaps
- **43 isolated node(s):** `allow`, `BaseMessage`, `NewsSource`, `Any`, `Connection` (+38 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Bradley Mankoff Recipient` and `Production Run Preset`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `RunDiagnostics` connect `Community 9` to `Community 0`, `Community 3`, `Community 6`, `Community 8`, `Community 11`, `Community 12`, `Community 18`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._
- **Why does `ProgressTracker` connect `Community 0` to `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Why does `load_runtime_config()` connect `Community 15` to `Community 2`, `Community 8`, `Community 10`, `Community 13`, `Community 14`, `Community 29`, `Community 30`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `Any` (e.g. with `ModelSamplingSettings` and `RunDiagnostics`) actually correct?**
  _`Any` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `RunDiagnostics` (e.g. with `AIMessage` and `ChatOpenAI`) actually correct?**
  _`RunDiagnostics` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `ProgressTracker` (e.g. with `ModelSamplingSettings` and `RunDiagnostics`) actually correct?**
  _`ProgressTracker` has 6 INFERRED edges - model-reasoned connections that need verification._