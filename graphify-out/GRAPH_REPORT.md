# Graph Report - news  (2026-06-12)

## Corpus Check
- 32 files · ~72,076 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 957 nodes · 2728 edges · 32 communities (27 shown, 5 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 100 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `693a15e7`
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
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]

## God Nodes (most connected - your core abstractions)
1. `Any` - 67 edges
2. `RunDiagnostics` - 51 edges
3. `run_pipeline()` - 50 edges
4. `ProgressTracker` - 48 edges
5. `Any` - 45 edges
6. `Any` - 43 edges
7. `load_runtime_config()` - 34 edges
8. `ModelSamplingSettings` - 28 edges
9. `Path` - 27 edges
10. `Any` - 24 edges

## Surprising Connections (you probably didn't know these)
- `RunDiagnostics` --uses--> `RunDiagnostics`  [INFERRED]
  tests/test_history_store.py → news_pipeline/diagnostics.py
- `REVIEW.md Standards File` --semantically_similar_to--> `REVIEW.md Standards File`  [INFERRED] [semantically similar]
  .github/workflows/claude-review.yml → claude_pr_tool_settings.txt
- `HistoryStoreTests` --uses--> `RunDiagnostics`  [INFERRED]
  tests/test_history_store.py → news_pipeline/diagnostics.py
- `FakePipe` --uses--> `ProgressTracker`  [INFERRED]
  tests/test_terminal_progress.py → news_pipeline/pipeline.py
- `FakeTTY` --uses--> `ProgressTracker`  [INFERRED]
  tests/test_terminal_progress.py → news_pipeline/pipeline.py

## Import Cycles
- 1-file cycle: `news_pipeline/pipeline.py -> news_pipeline/pipeline.py`
- 1-file cycle: `news_pipeline/citations.py -> news_pipeline/citations.py`
- 1-file cycle: `news_pipeline/source_checks.py -> news_pipeline/source_checks.py`
- 1-file cycle: `news_pipeline/story_clustering.py -> news_pipeline/story_clustering.py`

## Communities (32 total, 5 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (47): ChatOpenAI, ensure_codex_safe_model_reference(), _attach_pending_activity_snapshots(), build_chat_model(), _clean_progress_message(), _collect_source_contexts(), _ensure_main_model_server_ready(), _finalize_failed_run() (+39 more)

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
Cohesion: 0.08
Nodes (52): ArticleSummarizationRuntime, ArticleSummaryState, _build_article_summary_app(), build_article_summary_prompt_messages(), _notify_article_completed(), BaseMessage, Article summarization pass for retained story-cluster articles., run_article_summary_pass() (+44 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (35): Daily news pipeline package., _annotated_citation_sources(), _article_body_evidence(), _article_lookup_by_id(), article_summary_lookup_by_id(), build_story_synthesis_prompt_messages(), _citation_diagnostics_with_presence(), clean_story_synthesis_contradictions() (+27 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (38): add_headline_overlay(), _append_unique_urls(), _article_translation_decision(), _base64url_decode(), _base64url_encode(), _bool_env(), _bounded_env_float(), build_email_subject() (+30 more)

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (19): _duration_label(), _format_count_map(), _hours_label(), _join_or_none(), _last_event(), _model_call_bucket(), _model_token_totals(), Any (+11 more)

### Community 10 - "Community 10"
Cohesion: 0.05
Nodes (83): apply_run_preset_to_environment(), _bool_env(), build_model_server_command(), codex_model_guard_active(), _coerce_bool_value(), _coerce_float_value(), _coerce_int_value(), _coerce_pause_value() (+75 more)

### Community 11 - "Community 11"
Cohesion: 0.53
Nodes (6): ModelSamplingSettings, _model_extra_body(), _new_run_diagnostics(), _sampling_to_dict(), _sampling_to_extra_body(), _task_sampling_to_dict()

### Community 12 - "Community 12"
Cohesion: 0.10
Nodes (38): _article_confirms_wire_attribution(), _budget_article_targets_for_summary(), _configured_source_display_name(), _confirm_wire_source_match(), _dedupe_story_drafts_for_global_selection(), _excluded_feed_item_reason(), _feed_item_matches_configured_source(), _feed_item_source_labels() (+30 more)

### Community 13 - "Community 13"
Cohesion: 0.29
Nodes (7): _build_html_article_listing(), _build_plain_text_article_listing(), build_report_body(), build_report_html(), _collect_grouped_headlines(), _extract_first_name(), CitationIntegrationTests

### Community 14 - "Community 14"
Cohesion: 0.22
Nodes (9): build_fallback_final_synthesis_preview(), _fallback_synthesis_paragraph_from_summaries(), _first_sentences(), _persist_article_summaries_debug(), Create grouped prose when final synthesis repeatedly returns empty., _report_entry_debug_record(), _report_entry_debug_records(), _report_story_label() (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.25
Nodes (7): 1. RTK Commands & Env, 2. graphify, 3. Karpathy Execution Guidelines, A. Think Before Coding (No Assumptions), B. Simplicity First (No Speculation), C. Surgical Changes (No Renovation), D. Goal-Driven Execution (Verify)

### Community 16 - "Community 16"
Cohesion: 0.20
Nodes (16): _apply_cli_preset(), _consume_preset_arg(), main(), _print_codex_model_server_command(), _print_model_server_command(), Command-line entry point for the daily news pipeline., _run_history(), _run_pipeline_command() (+8 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (17): _build_html_synthesis(), clean_synthesis_for_publication(), _contains_disallowed_final_markup(), _enforce_text_free_image_prompt(), _fallback_image_prompt(), _format_plain_text_synthesis(), generate_image_art_brief(), generate_report_title() (+9 more)

### Community 18 - "Community 18"
Cohesion: 0.20
Nodes (15): AIMessage, _coerce_int(), estimate_message_token_count(), estimate_token_count(), extract_prompt_tokens_from_response(), _extract_token_usage_from_response(), _get_token_encoder(), invoke_with_retries() (+7 more)

### Community 19 - "Community 19"
Cohesion: 0.22
Nodes (8): Advanced Tuning, CLI Commands, Default Run Knobs, Infrastructure, Model And Translation, Removed Settings, Settings Reference, YAML Files

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (12): Connection, _article_embed_text(), _content_hash(), dedup_story_drafts(), embed_articles(), embed_texts(), _load_model(), _open_cache() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.19
Nodes (13): _article_scrape_deadline(), _build_feed_fallback_text(), _decode_google_news_article_path(), _download_article_html(), _google_news_query_target(), _is_google_news_url(), Decode modern Google News RSS article URLs (CBMi... base64 path encoding)., Follow Google News redirect links without treating Google pages as articles. (+5 more)

### Community 24 - "Community 24"
Cohesion: 0.28
Nodes (9): anthropics/claude-code-action, Claude PR Review Workflow Setup Script, REVIEW.md Standards File, ANTHROPIC_API_KEY Secret, actions/checkout@v4, anthropics/claude-code-action@v1, claude-haiku-4-5 Model, Claude PR Review GitHub Workflow (+1 more)

### Community 25 - "Community 25"
Cohesion: 0.50
Nodes (5): build_article_fallback_entry(), _build_article_heading(), _extract_sentences(), _format_article_metadata(), normalize_report_entry()

### Community 26 - "Community 26"
Cohesion: 0.22
Nodes (9): CLI, Configuration, Daily News Pipeline, Image, Model, Outputs, Run Presets, Runtime Variables (+1 more)

### Community 27 - "Community 27"
Cohesion: 0.50
Nodes (4): capture_activity_snapshot(), _parse_activity_command_output(), Collect macOS memory/activity signals without making the run depend on them., _run_activity_command()

### Community 35 - "Community 35"
Cohesion: 0.50
Nodes (4): _article_candidates_from_source_context(), gather_article_candidates_for_source(), _normalize_url_for_dedupe(), Loose URL canonicalization for cross-provider dedupe of the same article.     Dr

## Knowledge Gaps
- **32 isolated node(s):** `allow`, `BaseMessage`, `NewsSource`, `Any`, `Connection` (+27 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `RunDiagnostics` connect `Community 9` to `Community 0`, `Community 3`, `Community 6`, `Community 8`, `Community 10`, `Community 11`, `Community 12`, `Community 18`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Why does `ProgressTracker` connect `Community 0` to `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `load_runtime_config()` connect `Community 10` to `Community 16`, `Community 8`, `Community 2`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Any` (e.g. with `ModelSamplingSettings` and `RuntimeConfig`) actually correct?**
  _`Any` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `RunDiagnostics` (e.g. with `AIMessage` and `ChatOpenAI`) actually correct?**
  _`RunDiagnostics` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `ProgressTracker` (e.g. with `ModelSamplingSettings` and `RuntimeConfig`) actually correct?**
  _`ProgressTracker` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `allow`, `Daily news pipeline package.`, `BaseMessage` to the rest of the system?**
  _82 weakly-connected nodes found - possible documentation gaps or missing edges._