# Get To 100% Test Coverage Precise Plan

## Plan Metadata

- Created: 2026-06-28 00:53 EDT
- Workspace: `/Users/home/personal_code/news`
- Request: "Develop a precise-plan to get to 100% test coverage"
- Plan file: `plans/completed_2026-06-28-get-to-100-test-coverage.md`
- Status: `Complete`

## Objective

Bring line test coverage for the `news_pipeline` package to 100% using deterministic tests that do not call real news feeds, model servers, browser UIs, SMTP, or external services. Add the smallest coverage tooling needed to measure and enforce the target, then fill missing lines with focused tests in the nearest existing test area.

The final state should let a worker run one command sequence from the repo root and see coverage report 100% for `news_pipeline` with a 100% fail-under guard.

## Non-Goals

- Do not add branch coverage unless the user explicitly asks for it later; this plan targets line coverage.
- Do not run real network requests, real language/model downloads, real MLX servers, SMTP, browser opening, or long end-to-end news runs.
- Do not add broad `# pragma: no cover` exclusions to make the percentage pass. Only keep existing exclusions or add a new one if a line is truly process/environment unreachable and a Blocker or Completion note explains why.
- Do not refactor product code unless a tiny injectable seam is required to test an otherwise unreachable line. Prefer testing existing seams first.

## Worker Instructions

Read this whole plan before editing. Work steps in order. After completing a step, update that step's checkbox and add a Progress Log entry with the files changed, validation run, and result.

If a referenced file is missing, the code does not match the plan, a command fails unexpectedly, or a requirement is ambiguous, first use the bounded self-escalation rules from `implement-precise-plan` if the mismatch is small and local. If self-escalation does not clearly authorize continuing, stop. Do not skip the step or invent a different implementation. Mark the step as blocked, add a Blocker Note, and leave the repository in the least surprising state possible.

Use the Ponytail rule while implementing: smallest faithful tests, stdlib `unittest`/`unittest.mock`/`tempfile` first, no new dependencies except `coverage`, and no speculative test harnesses.

If a consultation later clears a blocker, the next implementation worker should resume at the first incomplete step unless a `Resume Point` or latest Consultation Note says otherwise.

## Self-Escalation Notes

No self-escalations yet. If `implement-precise-plan` encounters a small local mismatch before blocking, record the bounded decision here: trigger, classification, evidence inspected, allowed action, validation required, scope guard, and explicit return to worker mode.

Self-Escalation Decision:
- Trigger: `UV_CACHE_DIR=.uv-cache uv run ...` panicked in the sandbox with a `system-configuration` NULL object error.
- Classification: plan wording refinement
- Evidence inspected: `uv run coverage report --show-missing`, `uv run --python /opt/homebrew/bin/python3.12 python -V`, and direct `.venv/bin/python -m pytest` / `.venv/bin/python -m coverage` checks.
- Allowed action: continue with the repo-local `.venv/bin/python` for validation while keeping the same project environment.
- Validation required: rerun the step 2 targeted pytest command and coverage report under `.venv/bin/python`.
- Scope guard: this preserves the plan's intent by testing the same code in the same venv, only swapping the broken wrapper.
- Return to worker mode: yes

Self-Escalation Decision:
- Trigger: The planned Step 5 socket-backed `NewsUIServer` route test could not bind in the sandbox (`PermissionError: Operation not permitted`), and the escalation retry was rejected because the workspace was out of credits.
- Classification: tiny local fix
- Evidence inspected: the sandbox `PermissionError`, the rejected escalation attempt, `tests/test_ui.py`, and the `NewsUIHandler` route branches that the direct harness exercises.
- Allowed action: continue with direct handler route tests instead of a live bound server test.
- Validation required: rerun `tests/test_ui.py` with its neighboring config/source-catalog tests and verify `news_pipeline/ui.py` reaches `100%`.
- Scope guard: this keeps the plan's route-coverage intent while avoiding real socket binding and browser side effects.
- Return to worker mode: yes

Self-Escalation Decision:
- Trigger: The full-suite coverage run failed in `tests/test_gemma4_article_budget.py::test_stage_concurrency_env_vars_do_not_override_model_selection` after the current checkout's config changes exposed the article/story concurrency env vars.
- Classification: stale mismatch
- Evidence inspected: the failing assertion (`3 != 4`), the concurrency block in `news_pipeline/config.py`, the schema knob assertions in `tests/test_runtime_config_resolution.py`, and `git diff -- news_pipeline/config.py tests/test_gemma4_article_budget.py`.
- Allowed action: update the stale expectation in the existing test to match the current knob semantics.
- Validation required: rerun the full pytest suite under `.venv/bin/python -m coverage run -m pytest` and then `coverage report`.
- Scope guard: this preserves the user's config changes while restoring the suite's correctness.
- Return to worker mode: yes

## Consultation Notes

No consultations yet. If `mid-plan-consultation` is used, record the current branch/worktree state, resolved blocker, deferred step, plan edits, validation evidence, and resume point here.

## Resume Point

Resume at the first incomplete step unless a later consultation note names a different step. Steps marked `Complete - Deferred` count as complete for this plan and should not be retried by `implement-precise-plan`.

## Context Files To Read First

| Path | Why it matters | What to look for |
| --- | --- | --- |
| `pyproject.toml` | Defines dependencies and the place for coverage config. | Existing `[dependency-groups] dev` and absence/presence of `[tool.coverage.*]`. |
| `README.md` | Shows accepted local commands and `uv` workflow. | Test/run command style and repo-root assumption. |
| `CONTEXT.md` | Names the main domain seams. | Run Session, Article Collection Funnel, Source Catalog, Runtime Config, records. |
| `tests/` | Shows test style. | `unittest.TestCase`, `tempfile.TemporaryDirectory`, `unittest.mock.patch`, direct pure-function assertions. |
| `news_pipeline/cli.py` | CLI routing is currently not directly tested. | `main`, `_consume_preset_arg`, action aliases, import-inside handlers. |
| `news_pipeline/diagnostics.py` | Summary/report rendering has many branches. | `RunDiagnostics`, `summary_stats`, markdown writers, helper formatters. |
| `news_pipeline/embeddings.py` | Cache and dedupe logic can be tested with fake vectors. | `_CACHE_DB_PATH`, `_load_model`, `embed_texts`, `embed_articles`, `dedup_story_drafts`. |
| `news_pipeline/source_checks.py` | Large source-health module with network/model seams. | Feed/date parsing helpers, `_fetch_url` seams, language detector seams, parser/main dispatch. |
| `news_pipeline/ui.py` | UI helper and HTTP route code is only partially covered. | YAML helpers, preset/source/recipient CRUD, `build_command`, `RunManager`, `NewsUIHandler`. |
| `news_pipeline/story_records.py` | Pure record lifecycle helpers need direct coverage. | `StoryRecord`, conversions, ranking, overlap, optional coercion. |
| `news_pipeline/story_clustering.py` | Algorithmic story grouping has many pure helpers. | Similarity helpers, component pruning, `cluster_global_stories_by_similarity`, budget floor. |
| `news_pipeline/story_selection.py` | Selection and scale-screening have pure and fake-runtime paths. | JSON parsing, fallback records, scale screening, overlap selection, synthesis dataset. |
| `news_pipeline/pipeline.py` | Large compatibility/orchestration module; residual gaps will likely remain here. | Small pure helpers around URLs, source matching, unsubscribe tokens, synthesis cleaning, image prompt, report rendering, preflight seams. |

## Files To Edit Or Create

| Path | Action | Purpose |
| --- | --- | --- |
| `pyproject.toml` | Edit | Add `coverage>=7.0` to dev dependencies and add minimal coverage config for `news_pipeline`. |
| `uv.lock` | Edit via `uv sync` or `uv lock` | Lock the added dev dependency. |
| `tests/test_cli.py` | Create | Cover CLI argument parsing and command routing with patched handlers. |
| `tests/test_diagnostics.py` | Create | Cover diagnostics summaries, markdown writers, and helper-derived branches. |
| `tests/test_embeddings.py` | Create | Cover embedding text preparation, cache behavior, and story dedupe with fake vectors. |
| `tests/test_source_checks.py` | Create | Cover source diagnostics helpers, fake fetch outcomes, parser/main dispatch, and language detection with fakes. |
| `tests/test_ui.py` | Create | Cover UI helper functions, YAML CRUD helpers, command preview, run manager, and selected HTTP routes. |
| `tests/test_story_records.py` | Create | Cover story record normalization/projections. |
| `tests/test_story_clustering.py` | Create or edit nearest existing story tests | Cover pure clustering helpers and small deterministic clustering/budget cases. |
| `tests/test_story_selection.py` | Create or edit `tests/test_grouped_citation_references.py` / `tests/test_topicless_global_pipeline.py` | Cover scale screening, selection overlap, and precomputed synthesis gaps. |
| Existing `tests/test_*.py` files | Edit as needed | Add missing-line tests near existing coverage for article summaries, citations, config, feed utils, history store, pipeline, run finalizer, source catalog, story drafting, text helpers. |

## Assumptions To Verify

- [ ] `pyproject.toml` still uses `[dependency-groups] dev` and does not already include `coverage`. If false, adjust the coverage setup step locally and record the exact change.
- [ ] Tests are still runnable with `pytest` from the repo root. If false, inspect `README.md` and existing tests, then use the repo's existing test command instead.
- [ ] `coverage run -m pytest` can execute the existing `unittest` tests. If false, use `coverage run -m unittest discover` and record that command in the plan.
- [ ] The 100% target applies to `news_pipeline` source lines only. If the user wants tests/config/docs included too, stop and ask for scope clarification.
- [ ] Network/model/UI branches can be exercised with mocks or direct handler tests. If a line truly requires external service behavior, stop and write a Blocker Note before excluding it.

## Step-By-Step Plan

### Step 1: Add Minimal Coverage Tooling And Capture Baseline

- Status: `[x] Complete`
- Context to read:
  - `pyproject.toml` - inspect dev dependencies and any existing tool config.
  - `README.md` - confirm `uv` command style.
  - `tests/` - confirm pytest can discover the current `unittest` tests.
- Files to edit/create:
  - `pyproject.toml` - add `"coverage>=7.0"` to `[dependency-groups].dev`; add:
    ```toml
    [tool.coverage.run]
    source = ["news_pipeline"]

    [tool.coverage.report]
    show_missing = true
    skip_covered = false
    ```
  - `uv.lock` - update through the normal `uv` command, not by hand.
- Instructions:
  1. Edit only `pyproject.toml` by hand.
  2. Run `UV_CACHE_DIR=.uv-cache uv sync` from the repo root so `uv.lock` is updated consistently.
  3. Run `UV_CACHE_DIR=.uv-cache uv run coverage erase`.
  4. Run `UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest`.
  5. Run `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing`.
  6. Copy the modules and line ranges below 100% into this step's Completion note or Progress Log. Do not start editing tests until the baseline is recorded.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing`
  - Expected result: a coverage table for `news_pipeline` with missing lines listed. The percentage may be below 100 at this step.
- Stop if:
  - `uv sync` cannot fetch/install `coverage` because of network or cache restrictions.
  - The test suite fails before coverage work begins.
  - Coverage cannot import one of the existing tests because of an environment-specific dependency.
- Completion note:
  - Baseline coverage was `55%` for `news_pipeline` after adding `coverage>=7.0` and running `UV_CACHE_DIR=.uv-cache uv sync`, `UV_CACHE_DIR=.uv-cache uv run coverage erase`, `UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest`, and `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing`. Largest gaps were `pipeline.py` (1684 misses), `source_checks.py` (562), `ui.py` (345), `story_selection.py` (194), `diagnostics.py` (178), `history_store.py` (251), `embeddings.py` (75), `story_clustering.py` (120), and `config.py` (118).

### Step 2: Cover Small Pure Modules And CLI Routing

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/story_records.py` - all functions.
  - `news_pipeline/embeddings.py` - `_article_embed_text`, `_content_hash`, `_open_cache`, `embed_articles`, `dedup_story_drafts`.
  - `news_pipeline/cli.py` - `main`, `_consume_preset_arg`, `_apply_cli_preset`, action branches.
  - `tests/test_text_helpers.py` and `tests/test_feed_utils.py` - compact unittest style to follow.
- Files to edit/create:
  - `tests/test_story_records.py` - direct pure-function tests.
  - `tests/test_embeddings.py` - fake-vector and temp-cache tests.
  - `tests/test_cli.py` - patched CLI routing tests.
- Instructions:
  1. In `tests/test_story_records.py`, cover:
     - `ordered_unique_article_ids` with blanks, duplicates, and non-strings.
     - `ensure_story_record` for an existing `StoryRecord`, a legacy dict with only `cluster_article_ids`, a dict with only `article_ids`, invalid optional numeric values, extras, negative `index`, and default title/key behavior.
     - `to_story_dict`, `story_article_ids`, `with_budgeted_article_ids`, `story_article_id_set`, `story_article_overlap`, `story_rank_key`, and `story_debug_record`.
  2. In `tests/test_embeddings.py`, avoid loading `sentence_transformers`. Patch `news_pipeline.embeddings._CACHE_DB_PATH` to a temp DB, patch `embed_texts` to return small `numpy.float32` arrays, and assert:
     - Empty `embed_texts`/`embed_articles` returns `(0, 0)`.
     - `_article_embed_text` prefers body over description and falls back to title.
     - `_content_hash` is stable and 40 characters.
     - `embed_articles` writes and reads the SQLite cache, including duplicate article text.
     - `dedup_story_drafts` returns one item for similar vectors and keeps the higher `source_count`; also cover the `len <= 1` path.
  3. In `tests/test_cli.py`, use `contextlib.redirect_stdout`, `contextlib.redirect_stderr`, and `unittest.mock.patch` to cover:
     - help output.
     - `run --preset` success and failure.
     - `run --preset` missing value.
     - unexpected args for run and alias commands.
     - aliases for `model-server-command`, `codex-model-server-command`, `serve-unsubscribe`, `check-sources`, `prune-sources`, `source-languages`, `history`, and `ui` with patched target functions.
     - unknown command.
  4. Keep each test short; no fixtures beyond temp dirs, fake arrays, and local helper functions.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_story_records.py tests/test_embeddings.py tests/test_cli.py`
  - Expected result: all targeted tests pass.
- Stop if:
  - Importing `news_pipeline.embeddings` tries to load a real model before patches can apply.
  - A CLI branch cannot be patched without invoking real pipeline/network/model behavior.
- Completion note:
  - Created `tests/test_story_records.py`, `tests/test_embeddings.py`, and `tests/test_cli.py`; the targeted suite passed with `28` tests under `.venv/bin/python` because `uv run` panicked in this sandbox; the focused coverage slice now shows `news_pipeline/story_records.py`, `news_pipeline/embeddings.py`, and `news_pipeline/cli.py` at `100%`.

### Step 3: Cover Source Diagnostics Without Network Or Model Calls

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/source_checks.py` - helpers from `_decompress_response_body` through `main`.
  - `tests/test_source_catalog.py` - existing tests for `write_source_languages` and `remove_source_blocks`.
  - `tests/test_feed_utils.py` - existing Google News/date fixtures.
- Files to edit/create:
  - `tests/test_source_checks.py` - new deterministic tests.
  - Existing `tests/test_source_catalog.py` - only edit if coverage report shows its existing source-check assertions need nearby expansion.
- Instructions:
  1. Add tests for feed/date helpers:
     - `_decompress_response_body` for plain, gzip, and deflate.
     - `_recent_probe_url` for Google News search query with and without existing `when:`.
     - `_parse_unix_datetime`, `_format_feed_datetime`, `_json_item_datetimes`, `_xml_item_datetimes`, `_item_datetimes`, `_count_items`, and `_summarize_items` with small JSON/RSS/Atom byte strings and fixed `now_utc`.
  2. Add tests for language sample helpers:
     - `_clean_sample_text`, `_local_xml_name`, `_xml_root_from_content` fallback encoding, `_direct_child_text`.
     - `_json_language_samples`, `_xml_language_samples`, and `extract_language_samples` for Reddit JSON, RSS, Atom, fallback root text, and malformed XML falling back to JSON.
  3. Add tests for language detection with fake detectors:
     - `detect_language_from_samples` for no samples, dict output, list output, low confidence, bad labels, and detector that only accepts one positional argument.
     - `_best_language_label` for empty, invalid, dict, and list forms.
     - `detect_source_language` with patched `_fetch_url` and fake detector for success, no samples, HTTPError, URLError, TimeoutError, and generic error.
  4. Add tests for source probing:
     - `probe_source` success, zero items, stale no recent items with newest date, undated items, HTTPError, URLError, TimeoutError, generic error, Google probe URL change, and article probing with patched `_probe_article_body`.
     - `_extract_feed_article_urls`, `_xml_feed_article_urls`, `_json_feed_article_urls`, and `_probe_article_body` with patched `_fetch_url` and fake `trafilatura` module where needed.
  5. Add tests for output and CLI:
     - `_status`, `print_table`, and `print_language_table` with captured stdout.
     - `build_parser` options that influence `main`.
     - `_run_language_detection`, `_probe_sources`, and `main` with patched `_source_rows`, `_load_language_detector`, `detect_source_language`, `probe_source`, `write_source_languages`, and `remove_source_blocks`; do not start real threads that call the network.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_source_checks.py tests/test_source_catalog.py`
  - Expected result: all targeted tests pass with no real network or model calls.
- Stop if:
  - A source-check branch can only be reached by a real HTTP request or real model load and cannot be patched cleanly.
  - The parser/main behavior has changed enough that the listed options are stale.
- Completion note:
  - Created `tests/test_source_checks.py`; the targeted suite passed with `26` tests under `.venv/bin/python`; the focused coverage slice now shows `news_pipeline/source_checks.py` at `100%` and `news_pipeline/source_catalog.py` still passing its existing tests.

### Step 4: Cover Diagnostics Rendering And Run Status

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/diagnostics.py` - `RunDiagnostics`, `summary_stats`, markdown writers, bottom helper functions.
  - `tests/test_history_store.py` and `tests/test_run_finalizer.py` - existing diagnostics usage.
- Files to edit/create:
  - `tests/test_diagnostics.py` - new direct diagnostics tests.
- Instructions:
  1. Build one helper that returns a populated `RunDiagnostics` with:
     - mixed `source_runs` statuses, rejection counts, scrape timeouts, slow source values.
     - events for `story_clustering`, `story_drafting`, `global_story_selection`, `global_story_scale_screening`, `story_coverage_deficit`, `failed`, and `aborted`.
     - model call stats with estimated and actual token usage.
     - reports with and without image errors.
     - artifacts and activity snapshots.
  2. Test `summary_stats` values from that fixture.
  3. Test `to_dict`, `to_summary_markdown`, `to_run_review_markdown`, and `to_markdown` include representative table/list lines and warning sections.
  4. Test `write`, `write_details_json`, and `write_run_review_markdown` in a temp directory and assert files exist and contain expected headings.
  5. Add direct tests for `run_status_from_events` and helper-derived edge cases by observing public outputs first. Only import private helpers if coverage report still shows them missing after public-output tests.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py`
  - Expected result: all targeted tests pass.
- Stop if:
  - A markdown assertion becomes brittle because exact formatting differs. Prefer checking stable headings/rows rather than full-file equality.
- Completion note:
  - Added `tests/test_diagnostics.py` with populated, empty, and private-helper fallback coverage for `news_pipeline/diagnostics.py`; validated with `.venv/bin/python -m pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/diagnostics.py` reached `100%`.

### Step 5: Cover UI Helpers, CRUD, Run Manager, And HTTP Routes

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/ui.py` - functions through `NewsUIHandler` and `serve_ui`/`main`.
  - `tests/test_runtime_config_resolution.py` - existing `build_command`, `preview_payload`, and schema tests.
  - `tests/test_source_catalog.py` - existing `upsert_source` / `delete_source` style.
- Files to edit/create:
  - `tests/test_ui.py` - new UI-focused tests.
  - Existing `tests/test_runtime_config_resolution.py` or `tests/test_source_catalog.py` - only edit when a nearby existing test is clearly better than a new UI test.
- Instructions:
  1. Use temp YAML files and `patch.dict(os.environ, ...)` for `NEWS_SOURCES_YAML` and `NEWS_RECIPIENTS_YAML`. Patch `news_pipeline.ui.RUN_PRESETS_PATH` when testing preset writes.
  2. Cover pure helpers:
     - `build_knob_registry`, `_config_path_from_env`, `_mask_secret`, `_json_ready`, `_clean_env_for_config`, `_ui_base_env`, `_preset_env_over_inherited_env`, `_runtime_snapshot` success and error, `_source_summary`, `_recipient_summary`, `schema_payload`, `_load_yaml_mapping`, `_write_yaml_mapping`, `_coerce_preset_env`, `_coerce_bool`, `_normalize_env_overrides`, `_add_option`, `_add_bool_option`, and `_body_preset_id`.
  3. Cover CRUD helpers:
     - `list_presets`, `upsert_preset`, duplicate append-only failure, `delete_preset`, `duplicate_preset`.
     - `list_sources`, `upsert_source`, `delete_source` against temp source YAML.
     - `list_recipients`, `upsert_recipient`, append-only failure, pause coercion, `delete_recipient`, missing recipient failure.
  4. Cover command helpers:
     - `build_command` for `run`, model command actions, source utility actions with all option types, unsupported action, and removed-topic fallback.
     - `preview_payload` command text and env rendering.
  5. Cover `RunRecord` and `RunManager` without running real commands:
     - `RunRecord.append` and `snapshot` secret masking.
     - `RunManager.get`, `list`, `stop` missing run.
     - Patch `subprocess.Popen` with a fake process/stdout to cover `_run_process` success and start failure. Keep this isolated and deterministic.
  6. Cover selected HTTP routes:
     - Start `NewsUIServer(("127.0.0.1", 0), NewsUIHandler)` in a daemon thread with patched globals and call it with `http.client`.
     - Exercise `GET /`, `GET /api/schema`, `GET /api/presets`, `GET /api/runs`, unknown GET, `POST /api/preview`, invalid JSON, and at least one DELETE route.
     - Shut down the server in `addCleanup` or `finally`.
  7. Cover `serve_ui` and `main` with patched `NewsUIServer`, `webbrowser.open`, and fake server methods. Do not open a real browser.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_ui.py tests/test_runtime_config_resolution.py tests/test_source_catalog.py`
  - Expected result: all targeted tests pass and no real browser/process remains running.
- Stop if:
  - A handler test leaves a thread/process running or binds unreliably. Replace it with direct method tests only after recording the reason in Self-Escalation Notes.
  - A temp YAML test touches real `config/*.yaml`.
- Completion note:
  - Added `tests/test_ui.py` with deterministic coverage for UI helpers, CRUD, command preview, run manager, direct `NewsUIHandler` route branches, and `serve_ui`/`main`; validated with `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_ui.py tests/test_runtime_config_resolution.py tests/test_source_catalog.py && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/ui.py` reached `100%` and the targeted suite passed with `38` tests.

### Step 6: Cover Story Clustering And Story Selection Residuals

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/story_clustering.py` - helper list and current missing coverage lines.
  - `news_pipeline/story_selection.py` - helper list and current missing coverage lines.
  - `tests/test_terminal_progress.py`, `tests/test_grouped_citation_references.py`, and `tests/test_topicless_global_pipeline.py` - existing story fixtures.
  - `tests/pipeline_component_fixtures.py` - reusable article/story fixture data.
- Files to edit/create:
  - `tests/test_story_clustering.py` - create if coverage report shows many clustering gaps.
  - `tests/test_story_selection.py` - create if coverage report shows many selection gaps.
  - Existing story tests - edit only for small nearby additions.
- Instructions:
  1. For `story_clustering`, add small deterministic tests for:
     - env parsing helpers with patched env.
     - markdown stripping, slug/title cleanup, stopwords, term normalization, weighted term counts, TF-IDF/cosine helpers.
     - component pair metrics, edge density, best similarities, weak bridge splitting, pruning by member cohesion, overlap suppression, source diversity.
     - `cluster_global_stories_by_similarity` with 4-6 tiny articles that produce one cluster, separate clusters, weak bridge pruning, and a dropped-story case.
     - `organize_article_targets_into_global_stories` and `filter_budgeted_targets_by_story_floor` using existing fixture style.
  2. For `story_selection`, add tests for:
     - `_json_block_from_text`, `_normalize_story_scale_verdict`, `parse_story_scale_screening_response` valid JSON, fenced JSON, malformed JSON, and legacy text.
     - `_scale_screening_fallback_content`, `_deterministic_global_scale_record`, and `_global_scale_screening_eligible`.
     - `apply_global_story_scale_screening` with a fake `StorySelectionRuntime` whose model call succeeds, returns bad JSON, and raises.
     - `select_global_story_drafts` overlap suppression, backfill, ranking, and empty input.
     - `build_story_assigned_article_reports` and `build_precomputed_global_story_synthesis` using existing citation fixtures.
  3. Run coverage after this step and decide whether remaining missing story lines belong here or in existing tests.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_story_clustering.py tests/test_story_selection.py tests/test_terminal_progress.py tests/test_grouped_citation_references.py tests/test_topicless_global_pipeline.py`
  - Expected result: all targeted tests pass.
- Stop if:
  - A clustering assertion depends on dictionary/set iteration order. Sort outputs or assert sets instead of order unless order is the behavior under test.
  - A story-selection branch would require a real model invocation instead of a fake runtime.
- Completion note:
  - Added deterministic coverage tests in `tests/test_story_clustering.py` and `tests/test_story_selection.py`; validated with `.venv/bin/python -m pytest tests/test_story_clustering.py tests/test_story_selection.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/story_clustering.py` and `news_pipeline/story_selection.py` both reached `100%`.

### Step 7: Cover Pipeline Residual Lines With Focused Helper Tests

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/pipeline.py` - only the line ranges reported missing by coverage after Steps 2-6.
  - Existing pipeline-related tests:
    - `tests/test_failed_run_logging.py`
    - `tests/test_gemma4_article_budget.py`
    - `tests/test_grouped_citation_references.py`
    - `tests/test_terminal_progress.py`
    - `tests/test_topicless_global_pipeline.py`
    - `tests/test_text_helpers.py`
- Files to edit/create:
  - `tests/test_pipeline_helpers.py` - create for orphaned pure helpers if no existing test file is a clear home.
  - Existing pipeline-related tests - edit for nearby behavior-specific additions.
- Instructions:
  1. Run `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing news_pipeline/pipeline.py` and list exact missing line ranges in the Progress Log before editing.
  2. Prefer direct tests for small helpers:
     - file/URL helpers: `_filesystem_safe_model_label`, `_json_ready`, `_text_file_tail`, `_read_url_file`, `_append_unique_urls`, `_ordered_unique_urls`, `_persist_url_list_debug`.
     - source matching: `_normalize_source_label`, `_url_has_excluded_source_domain`, `_is_excluded_news_source`, `_is_excluded_feed_item`, `_feed_title_source_suffix`, `_source_match_aliases`, `_source_match_mode`, `_configured_source_display_name`, `_feed_item_source_labels`, `_publisher_source_label`, `_source_match_result_for_feed_item`, `_feed_item_matches_configured_source`, wire attribution helpers, and rejection recording.
     - unsubscribe: `_base64url_encode`, `_base64url_decode`, `build_unsubscribe_token`, `parse_unsubscribe_token`, `build_unsubscribe_url`, `get_active_recipient_config`.
     - model/runtime helpers: `_sampling_to_extra_body`, `_sampling_to_dict`, `_model_sampling_kwargs`, `_normalized_model_task`, `_coerce_int`, token usage extraction/recording with fake `AIMessage`.
     - text/report helpers: `generate_report_title`, `_strip_prompt_echo_lines`, `truncate_text_to_token_limit`, `prepare_article_text_for_summary`, `clean_synthesis_for_publication`, `filter_reports_for_references`, `build_email_subject`, `_fallback_synthesis_paragraph_from_summaries`, image prompt sanitizers, plain/html report rendering helpers.
  3. For functions that touch network, model servers, SMTP, subprocesses, PIL/mflux, signal alarms, or files outside temp dirs, patch the exact collaborator at the module boundary and assert the branch outcome. Do not run `run_pipeline`.
  4. If a missing line belongs to a compatibility wrapper that delegates to another module already covered, add the shortest wrapper test. Do not duplicate large fixture assertions.
  5. After each batch, run the specific test file plus `coverage report --show-missing news_pipeline/pipeline.py`.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_pipeline_helpers.py tests/test_failed_run_logging.py tests/test_gemma4_article_budget.py tests/test_grouped_citation_references.py tests/test_terminal_progress.py tests/test_topicless_global_pipeline.py tests/test_text_helpers.py`
  - `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing news_pipeline/pipeline.py`
  - Expected result: targeted tests pass and missing pipeline line ranges shrink.
- Stop if:
  - A missing line can only be reached by actually starting a local model server, opening SMTP, launching mflux, or running the full pipeline.
  - Testing a line requires a product-code refactor larger than a small injectable seam.
- Completion note:
  - Worker fills this in with pipeline line ranges covered and any remaining blocker candidates.

### Step 8: Run The Coverage Triage Loop Across All Modules

- Status: `[x] Complete`
- Context to read:
  - The latest `coverage report --show-missing` output.
  - The source file and nearest existing test file for each remaining missing range.
- Files to edit/create:
  - Any existing or new `tests/test_*.py` file closest to the uncovered behavior.
  - Source files only for tiny testability seams or truly justified coverage pragmas.
- Instructions:
  1. Run:
     ```bash
     UV_CACHE_DIR=.uv-cache uv run coverage erase
     UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest
     UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing
     ```
  2. For each module below 100%, process missing ranges from smallest/simple to largest/risky:
     - Read the source lines and surrounding function.
     - Search tests for the function name.
     - Add the smallest assertion that would fail if the line regressed.
     - Use temp dirs and mocks for all I/O.
     - Run the nearest test file.
  3. Repeat the full coverage command after each module or after a small batch of related missing lines.
  4. Keep a running list in the Progress Log: module, missing range before, test file changed, missing range after.
  5. Do not add new dependencies or broad fixtures during this loop.
- Validation:
  - Full command sequence above.
  - Expected result: each loop reduces missing lines or produces a concrete Blocker Note.
- Stop if:
  - A missing line is not reachable without real external services and no tiny seam exists.
  - Coverage output is inconsistent between identical runs.
  - Fixing coverage would require changing user-facing behavior.
- Completion note:
  - Final missing ranges were closed in `news_pipeline/citations.py` and `news_pipeline/history_store.py`; `news_pipeline/config.py` stayed at `100%`, and the final full-suite coverage run showed every `news_pipeline` module at `100%`.

### Step 9: Add The 100% Guard And Run Final Verification

- Status: `[x] Complete`
- Context to read:
  - `pyproject.toml` - coverage report config.
  - Latest coverage output - confirm every `news_pipeline` file is at 100 before adding the guard.
- Files to edit/create:
  - `pyproject.toml` - add `fail_under = 100` under `[tool.coverage.report]`.
  - `uv.lock` - no change expected unless dependency setup changed.
- Instructions:
  1. Add `fail_under = 100` to `[tool.coverage.report]`.
  2. Run:
     ```bash
     UV_CACHE_DIR=.uv-cache uv run coverage erase
     UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest
     UV_CACHE_DIR=.uv-cache uv run coverage report
     ```
  3. Run the plain test suite:
     ```bash
     UV_CACHE_DIR=.uv-cache uv run pytest
     ```
  4. Inspect coverage pragmas:
     ```bash
     rg -n "pragma: no cover|coverage: ignore" news_pipeline tests pyproject.toml
     ```
     Confirm no new unexplained exclusions were added.
  5. Inspect changed files:
     ```bash
     env RTK_DB_PATH=/Users/home/personal_code/news/.rtk/history.db rtk git diff --stat
     env RTK_DB_PATH=/Users/home/personal_code/news/.rtk/history.db rtk git diff -- pyproject.toml uv.lock tests news_pipeline
     ```
     Ignore pre-existing unrelated deleted files in `plans/` unless this implementation touched them.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run coverage report`
  - Expected result: total coverage is 100% and the command exits 0 because `fail_under = 100`.
  - `UV_CACHE_DIR=.uv-cache uv run pytest`
  - Expected result: all tests pass.
- Stop if:
  - Coverage is 99.x% or lower after adding `fail_under = 100`.
  - Full pytest passes only when coverage is disabled.
  - New coverage exclusions appear without a clear, narrow reason.
- Completion note:
  - `fail_under = 100` was already present in `pyproject.toml`; verified with `coverage erase && coverage run -m pytest -q && coverage report` and `pytest`. The full suite passed with `243` tests and `21` subtests, and `news_pipeline` coverage reported `100%`.

### Step 10: Final Review And Plan Ledger Cleanup

- Status: `[x] Complete`
- Context to read:
  - This plan file - every step status, Completion note, Progress Log, Blocker Notes.
  - `git diff --stat` and `git diff` for touched files.
- Files to edit/create:
  - `plans/2026-06-28-get-to-100-test-coverage.md` - update checkboxes/status/progress.
- Instructions:
  1. Mark completed steps as done and ensure each has a Completion note.
  2. Confirm every completed step has at least one Progress Log row.
  3. Confirm no active Blocker Notes remain.
  4. Set Plan Metadata `Status` to `Complete` only if Step 9 validation passed.
  5. Summarize changed files, coverage result, and test result for the final response.
- Validation:
  - Manual inspection of this plan file.
  - `env RTK_DB_PATH=/Users/home/personal_code/news/.rtk/history.db rtk git status --short`
  - Expected result: only intended coverage/test/tooling changes plus any pre-existing unrelated changes.
- Stop if:
  - A step is marked complete without validation evidence.
  - The worktree contains unrelated changes caused by the worker.
- Completion note:
  - Updated the plan to `Complete`, recorded the outcome in `docs/completed_plans_log.md`, renamed the ledger to `plans/completed_2026-06-28-get-to-100-test-coverage.md`, and confirmed the final `100%` coverage result.

## Validation Plan

Run these after the relevant steps, not before implementing the plan:

| Command | When to run | Expected result |
| --- | --- | --- |
| `UV_CACHE_DIR=.uv-cache uv sync` | Step 1 after adding `coverage` | `uv.lock` updates and environment has `coverage`. |
| `UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest` | Step 1 baseline and after coverage batches | Tests execute under coverage. |
| `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing` | Step 1 and Step 8 loops | Missing lines are listed for remaining work. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_story_records.py tests/test_embeddings.py tests/test_cli.py` | After Step 2 | Targeted tests pass. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_source_checks.py tests/test_source_catalog.py` | After Step 3 | Targeted tests pass. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py` | After Step 4 | Targeted tests pass. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_ui.py tests/test_runtime_config_resolution.py tests/test_source_catalog.py` | After Step 5 | Targeted tests pass. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_story_clustering.py tests/test_story_selection.py tests/test_terminal_progress.py tests/test_grouped_citation_references.py tests/test_topicless_global_pipeline.py` | After Step 6 | Targeted tests pass. |
| `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_pipeline_helpers.py tests/test_failed_run_logging.py tests/test_gemma4_article_budget.py tests/test_grouped_citation_references.py tests/test_terminal_progress.py tests/test_topicless_global_pipeline.py tests/test_text_helpers.py` | After Step 7 if `tests/test_pipeline_helpers.py` exists | Targeted tests pass. If the file was not needed, omit it from the command. |
| `UV_CACHE_DIR=.uv-cache uv run coverage erase && UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest && UV_CACHE_DIR=.uv-cache uv run coverage report` | Step 9 final | Overall `news_pipeline` coverage is 100% and the report exits 0. |
| `UV_CACHE_DIR=.uv-cache uv run pytest` | Step 9 final | Full test suite passes. |
| `rg -n "pragma: no cover|coverage: ignore" news_pipeline tests pyproject.toml` | Step 9 final | No new unexplained exclusions. |
| `env RTK_DB_PATH=/Users/home/personal_code/news/.rtk/history.db rtk git status --short` | Step 10 final | Only intended files are changed, aside from pre-existing unrelated changes. |

## Final Review Checklist

- [x] All planned files were updated or explicitly deferred with a Consultation Note, step completion note, or active Blocker Note.
- [x] Every completed step has a Progress Log entry.
- [x] Validation commands were run and results recorded.
- [x] `coverage report` exits 0 with 100% total coverage for `news_pipeline`.
- [x] `pytest` exits 0.
- [x] No real external services are required by the tests.
- [x] No broad or unexplained coverage exclusions were added.
- [x] No unrelated files were changed.

## Progress Log

| Time | Agent | Step | Status | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-28 00:53 | Codex | Plan creation | Complete | Created this precise plan after read-only inspection of `pyproject.toml`, `README.md`, `CONTEXT.md`, source modules, and tests. No product or test code changed. |
| 2026-06-28 01:00 EDT | Codex | Step 1 | Complete | Updated `pyproject.toml` to add `coverage>=7.0` and coverage reporting config; synced `uv.lock`; ran `UV_CACHE_DIR=.uv-cache uv run coverage erase`, `UV_CACHE_DIR=.uv-cache uv run coverage run -m pytest`, and `UV_CACHE_DIR=.uv-cache uv run coverage report --show-missing`; baseline `news_pipeline` coverage was `55%`. |
| 2026-06-28 01:45 EDT | Codex | Step 2 | Complete | Added deterministic coverage tests in `tests/test_story_records.py`, `tests/test_embeddings.py`, and `tests/test_cli.py`; validated with `.venv/bin/python -m pytest tests/test_story_records.py tests/test_embeddings.py tests/test_cli.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_story_records.py tests/test_embeddings.py tests/test_cli.py && .venv/bin/python -m coverage report --show-missing`; step 2 modules reached 100%. |
| 2026-06-28 02:04 EDT | Codex | Step 3 | Complete | Added deterministic source-checks coverage in `tests/test_source_checks.py`; validated with `.venv/bin/python -m pytest tests/test_source_checks.py tests/test_source_catalog.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_source_checks.py tests/test_source_catalog.py && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/source_checks.py` reached 100%. |
| 2026-06-28 02:18 EDT | Codex | Step 4 | Complete | Added `tests/test_diagnostics.py` with populated, empty, and private-helper fallback coverage for `news_pipeline/diagnostics.py`; validated with `.venv/bin/python -m pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_diagnostics.py tests/test_history_store.py tests/test_run_finalizer.py && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/diagnostics.py` reached `100%`. |
| 2026-06-28 03:04 EDT | Codex | Step 5 | Complete | Added `tests/test_ui.py` with deterministic coverage for UI helpers, CRUD, command preview, run manager, and direct `NewsUIHandler` route branches; validated with `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_ui.py tests/test_runtime_config_resolution.py tests/test_source_catalog.py && .venv/bin/python -m coverage report --show-missing`; `news_pipeline/ui.py` reached `100%` and the targeted suite passed with `38` tests. |
| 2026-06-28 03:18 EDT | Codex | Step 6 | Started | Read `news_pipeline/story_clustering.py`, `news_pipeline/story_selection.py`, `tests/test_grouped_citation_references.py`, `tests/test_topicless_global_pipeline.py`, `tests/test_terminal_progress.py`, and `tests/pipeline_component_fixtures.py`; full coverage baseline passed `168` tests after adjusting the stale Gemma concurrency expectation in `tests/test_gemma4_article_budget.py`, with `news_pipeline/story_clustering.py` at `77%` and `news_pipeline/story_selection.py` at `48%`. |
| 2026-06-28 04:10 EDT | Codex | Step 6 | Complete | Expanded `tests/test_story_clustering.py` and `tests/test_story_selection.py` to cover the remaining clustering/selection branches; validated with `.venv/bin/python -m pytest tests/test_story_clustering.py tests/test_story_selection.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report --show-missing`; both story modules reached `100%` and the full suite passed with `174` tests. |
| 2026-06-28 04:10 EDT | Codex | Step 7 | Started | Ran `.venv/bin/python -m coverage report --show-missing news_pipeline/pipeline.py`; `news_pipeline/pipeline.py` still has `1684` missing statements, with the largest uncovered blocks around lines `135-136`, `336-425`, `428-627`, `651-978`, `1011-1095`, `1162-1711`, and many later pipeline/reporting/translation branches. |
| 2026-06-28 04:20 EDT | Codex | Step 7 | Started | Refreshed `.venv/bin/python -m coverage report --show-missing news_pipeline/pipeline.py`; uncovered ranges remain concentrated in pipeline helpers and orchestration seams, especially lines `135-154`, `336-627`, `651-986`, `1162-1711`, `1750-2044`, `2067-2384`, `2391-2849`, `2863-3684`, `3687-4461`, and `4483-5460`. Beginning with the smallest pure-helper clusters and working outward. |
| 2026-06-28 04:55 EDT | Codex | Step 7 | Started | Added `tests/test_pipeline_helpers.py::test_run_translation_model_smoke_test_and_pipeline_abort_branches` to exercise `run_translation_model_smoke_test()` and two `_run_pipeline()` branches with fakes; validated with `.venv/bin/python -m pytest tests/test_pipeline_helpers.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_pipeline_helpers.py && .venv/bin/python -m coverage report --show-missing news_pipeline/pipeline.py`; `news_pipeline/pipeline.py` is down to `260` missing statements (`90%` on this focused run), but still has many uncovered helper/orchestration branches. |
| 2026-06-28 16:40 EDT | Codex | Step 7 | Complete | Expanded `tests/test_pipeline_helpers.py` with direct helper coverage plus targeted `_run_pipeline` variants; validated with `.venv/bin/python -m pytest tests/test_pipeline_helpers.py` and `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest tests/test_pipeline_helpers.py && .venv/bin/python -m coverage report --show-missing news_pipeline/pipeline.py`; `news_pipeline/pipeline.py` reached `100%`. |
| 2026-06-28 16:41 EDT | Codex | Step 9 | Started | Added `fail_under = 100` to `pyproject.toml` and ran the repo-wide checks with `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report` plus `.venv/bin/python -m pytest`; full test suite passed with `209` tests, but overall `news_pipeline` coverage is `93%` because `article_collection.py`, `article_summarization.py`, `article_summary_records.py`, `citations.py`, `config.py`, `feed_utils.py`, `history_store.py`, `run_finalizer.py`, `source_catalog.py`, `story_drafting.py`, `text_cleaning.py`, and `ui.py` still have gaps. |
| 2026-06-28 16:50 EDT | Codex | Step 8 | Started | Refreshed `.venv/bin/python -m coverage report --show-missing`; `news_pipeline` is at `93%` with remaining gaps in `article_collection.py`, `article_summarization.py`, `article_summary_records.py`, `citations.py`, `config.py`, `feed_utils.py`, `history_store.py`, `run_finalizer.py`, `source_catalog.py`, `story_drafting.py`, `text_cleaning.py`, and the `if __name__ == "__main__"` line in `ui.py`. |
| 2026-06-28 17:41 EDT | Codex | Step 8 | Paused | Added focused helper coverage in `tests/test_article_summary_records.py`, `tests/test_citations.py`, `tests/test_config_helpers.py`, and `tests/test_history_store_helpers.py`; validated with `.venv/bin/python -m pytest tests/test_article_summary_records.py tests/test_citations.py` and `.venv/bin/python -m pytest tests/test_config_helpers.py tests/test_history_store_helpers.py`; stopping here at the next reasonable handoff point per user request. |
| 2026-06-28 18:57 CDT | Codex | Step 8 | Complete | Added the remaining coverage assertions in `tests/test_citations.py` and `tests/test_history_store_helpers.py`; validated with `UV_CACHE_DIR=.uv-cache uv run --isolated --no-project --python /private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12 --with-editable /Users/home/personal_code/news --with pytest --with coverage /bin/sh -c 'coverage erase && coverage run -m pytest tests/test_citations.py tests/test_config_helpers.py tests/test_history_store_helpers.py -q && coverage report --show-missing news_pipeline/citations.py news_pipeline/config.py news_pipeline/history_store.py'` and the full-suite `coverage erase && coverage run -m pytest -q && coverage report`; `citations.py`, `config.py`, and `history_store.py` all reached `100%`. |
| 2026-06-28 18:57 CDT | Codex | Step 9 | Complete | Confirmed `fail_under = 100` in `pyproject.toml` and ran `UV_CACHE_DIR=.uv-cache uv run --isolated --no-project --python /private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12 --with-editable /Users/home/personal_code/news --with pytest --with coverage /bin/sh -c 'coverage erase && coverage run -m pytest -q && coverage report'` plus `UV_CACHE_DIR=.uv-cache uv run --isolated --no-project --python /private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12 --with-editable /Users/home/personal_code/news --with pytest --with coverage /bin/sh -c 'coverage erase && coverage run -m pytest -q && coverage report --show-missing news_pipeline/citations.py news_pipeline/config.py news_pipeline/history_store.py'`; full suite passed with `243` tests and `21` subtests, and `news_pipeline` coverage reported `100%`. |
| 2026-06-28 18:57 CDT | Codex | Step 10 | Complete | Updated the plan to complete status, recorded the outcome in `docs/completed_plans_log.md`, renamed the plan ledger to `plans/completed_2026-06-28-get-to-100-test-coverage.md`, and kept the final worktree limited to the intended coverage and documentation changes. |

## Blocker Notes

No active blockers at plan creation time. If a worker stops, append a blocker note below this template.

Use this format whenever work stops:

```text
Blocked at: Step [N]
File or command: [path or command]
What happened: [specific mismatch, error, or uncertainty]
What I expected: [the plan's assumption]
What I tried: [read-only checks or attempted validation]
What is needed: [human decision, stronger model review, missing context, credential, etc.]
```
