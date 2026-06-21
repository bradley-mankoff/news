# Execute Ponytail Audit Cuts Precise Plan

## Plan Metadata

- Created: 2026-06-21 16:03 CDT
- Workspace: `/Users/home/personal_code/news`
- Request: "Write a precise-plan to execute those 10 suggestions you gave"
- Plan file: `plans/2026-06-21-execute-ponytail-audit-cuts.md`
- Status: `Complete`

## Objective

Execute the ten over-engineering cuts identified in the repo-wide ponytail audit while preserving existing user-visible behavior of the daily news pipeline. The target outcome is a smaller codebase with fewer direct dependencies, less duplicated helper logic, fewer dead compatibility paths, and validation proving the pipeline utilities still behave as expected.

This plan is intentionally conservative. It orders low-risk deletions and helper consolidation before the broad `RunSession`/global compatibility cleanup. If a later step reveals that a compatibility surface is still required by tests or external CLI behavior, block and record the evidence instead of improvising a partial rewrite.

## Non-Goals

- Do not change newsletter selection rules, prompts, model behavior, email formatting, source list contents, recipient config, or generated output formats except where a step explicitly says so.
- Do not delete untracked local output folders, venv backups, caches, or user worktree changes.
- Do not refresh Graphify or run semantic extraction.
- Do not add new dependencies.

## Worker Instructions

Read this whole plan before editing. Work steps in order. After completing a step, update that step's checkbox and add a Progress Log entry with the files changed, validation run, and result.

The worktree was dirty when this plan was written: `.codex/config.toml`, `AGENTS.md`, `config/sources.yaml`, `news_pipeline/config.py`, `tests/test_runtime_config_resolution.py`, and `tests/test_source_catalog.py` had existing modifications, and `.cursor/` was untracked. Before editing a file, inspect it and preserve unrelated user changes. Do not revert anything you did not create.

If a referenced file is missing, the code does not match the plan, a command fails unexpectedly, or a requirement is ambiguous, first use the bounded self-escalation rules from `implement-precise-plan` if the mismatch is small and local. If self-escalation does not clearly authorize continuing, stop. Do not skip the step or invent a different implementation. Mark the step as blocked, add a Blocker Note, and leave the repository in the least surprising state possible.

If a consultation later clears a blocker, the next implementation worker should resume at the first incomplete step unless a `Resume Point` or latest Consultation Note says otherwise.

## Self-Escalation Notes

Self-Escalation Decision:
- Trigger: Step 9 compatibility-global audit still points to `test_session_finalizer_survives_compat_global_drift` and the remaining globals in `_compat_runtime_values`/`RunSession._sync_to_legacy_globals` are still consumed by live run helpers or compatibility tests.
- Classification: real blocker
- Evidence inspected: `docs/adr/0002-run-session-owns-daily-run-lifecycle-state.md`, `docs/adr/0003-run-finalization-finishes-recorded-run-outcomes.md`, `docs/adr/0004-runtime-config-resolution-owns-env-overlays.md`, `tests/test_failed_run_logging.py`, `tests/test_run_finalizer.py`, `rg "ACTIVE_RUN_|MODEL_CALL_STATS|RUN_ACTIVITY_SNAPSHOTS|MANAGED_MODEL_SERVER_|RUN_LOG_FILE|RUN_LOG_FILES|progress_tracker" news_pipeline tests`
- Allowed action: block
- Validation required: a separate deepening plan or explicit test expectation change that narrows which compatibility globals can be removed
- Scope guard: avoids turning the compatibility cleanup into a broad RunSession/RunFinalizer orchestration rewrite under this plan
- Return to worker mode: yes
- Consultation outcome: resolved by explicit deferral on 2026-06-21 17:13 CDT; no active blocker remains and the plan resumes at Step 10.

## Consultation Notes

2026-06-21 16:52 CDT | Codex | Mid-plan consultation confirmed the Step 9 blocker is real, not stale. Current branch is `main...origin/main`; the worktree contains the planned edits from Steps 2-8 plus pre-existing local ignore/config changes and untracked `plans/`, `.cursor/`, `news_pipeline/feed_utils.py`, and `tests/test_feed_utils.py`. Read-only checks used: `git status --short --branch`, `git log --oneline -3`, targeted reads of `news_pipeline/pipeline.py`, `tests/test_failed_run_logging.py`, `tests/test_run_finalizer.py`, ADR 0002, ADR 0003, and `graphify query` for RunSession/RunFinalizer relationships. Evidence still shows the remaining globals are live compatibility adapters for pipeline free functions and tests. Resume point: do not keep attempting Step 9 under this plan; either write a separate RunSession/RunFinalizer deepening plan, or get an explicit decision to relax legacy-global compatibility expectations.

2026-06-21 17:13 CDT | Codex | Mid-plan consultation rechecked Step 9 and classified it as resolved by explicit deferral. Read-only evidence: `rg "ACTIVE_RUN_|MODEL_CALL_STATS|RUN_ACTIVITY_SNAPSHOTS|MANAGED_MODEL_SERVER_|RUN_LOG_FILE|RUN_LOG_FILES|progress_tracker" news_pipeline tests docs` shows the globals are still used by live pipeline helpers and compatibility tests; `RunSession._sync_to_legacy_globals` and `_capture_from_legacy_globals` still bridge session-owned state to legacy free functions; `test_session_finalizer_survives_compat_global_drift` intentionally verifies that `RunFinalizer` state survives legacy-global drift; ADR 0002 says compatibility globals are transitional adapters; ADR 0003 says follow-on RunFinalizer deepening remains future work. No small safe deletion fits this ponytail-audit plan without broad RunSession/RunFinalizer orchestration work or changing explicit compatibility expectations. Step 9 is complete by deferral; follow-up belongs in a separate RunSession/RunFinalizer deepening plan.

## Resume Point

Resume at Step 10: Final Validation And Cleanup. Step 9 is `Complete - Deferred` and should not be retried by the next implementation worker under this plan.

## Design Guardrails

- Treat `run_article_summary_pass`, `apply_source_catalog_patch`, `RunSession`, and `RunFinalizer` as the important Module interfaces. Prefer deleting shallow internals behind those interfaces over adding new caller-facing knobs, classes, adapters, or compatibility methods.
- Shared helper extraction is allowed only where there are multiple live callers. `strip_model_artifacts` belongs in existing `text_cleaning.py`; feed URL/date helpers may get a small `feed_utils.py` because `pipeline.py`, `source_checks.py`, and `story_clustering.py` currently duplicate the same mechanics.
- Keep product-flow meaning in action/orchestration code. Shared mechanics modules should own only reusable parsing, decoding, normalization, and cleanup behavior, return simple values, and avoid config, diagnostics, DB writes, or progress mutation.
- `RunSession` and `RunFinalizer` are the accepted lifecycle Modules per ADRs 0002 and 0003. Step 9 must not introduce a new lifecycle seam; it should delete truly dead compatibility globals or block for a separate deepening plan.

## Context Files To Read First

| Path | Why it matters | What to look for |
| --- | --- | --- |
| `AGENTS.md` | Local repo instructions | Ponytail mode, RTK/uv command routing, Graphify rules |
| `README.md` | Public CLI/documentation contract | Any documented `todays_news.py`, top-funnel, or source utility behavior |
| `pyproject.toml` | Dependency and script declarations | `datetime`, `langchain`, `langchain-community`, `langgraph`, `todays-news` |
| `uv.lock` | Locked dependency graph | Entries that should disappear after dependency cleanup |
| `news_pipeline/article_summarization.py` | LangGraph-only article summary workflow | `_build_article_summary_app`, `ArticleSummaryState`, retry/fallback behavior |
| `news_pipeline/source_catalog.py` | Source catalog edit Module | `apply_source_catalog_patch` interface, YAML block helpers, formatting/newline preservation |
| `tests/test_source_catalog.py` | Source catalog behavior contract | Field order, unknown-field rejection, delete/upsert/language tests, non-source YAML preservation, CRLF handling |
| `news_pipeline/config.py` | Runtime config, top-funnel, dead dataclasses | `NewsSource`, `load_top_funnel_providers`, `top_of_funnel_per_provider` |
| `news_pipeline/pipeline.py` | Main orchestration and compatibility globals | `_compat_runtime_values`, `RunSession._activate`, Google News helpers, article-summary delegates |
| `news_pipeline/source_checks.py` | Duplicate Google News/date helpers | `_is_google_news_url`, `_decode_google_news_article_path`, `_parse_feed_datetime` |
| `news_pipeline/story_clustering.py` | Duplicate date and artifact helpers | `strip_model_artifacts`, `_parse_feed_datetime` |
| `news_pipeline/article_summary_records.py` | Existing artifact cleanup and summary record helpers | `strip_model_artifacts`, `fallback_entry`, summary normalization |
| `news_pipeline/text_cleaning.py` | Existing shared text cleanup module | Best home for shared model-artifact cleanup if no new module is needed |
| `news_pipeline/history_store.py` | Dead history dataclass and validation surface | `HistoryConfig`, history command result behavior |
| `news_pipeline/cli.py` | CLI aliases and wrapper compatibility docs | `todays_news.py` compatibility mentions and command aliases |
| `todays_news.py` | Compatibility wrapper | Whether deletion is still safe after docs/scripts checks |
| `tests/test_runtime_config_resolution.py` | Runtime config UI/CLI assertions | Knob and command changes |
| `tests/test_topicless_global_pipeline.py` | End-to-end component behavior | Pipeline stage behavior after refactors |
| `tests/test_article_collection.py` | Source collection contract | URL helper/date behavior and diagnostics |
| `tests/test_failed_run_logging.py` | RunSession compatibility risk | Tests around finalizer/global drift |

## Files To Edit Or Create

| Path | Action | Purpose |
| --- | --- | --- |
| `news_pipeline/article_summarization.py` | Edit | Replace LangGraph workflow with plain loop and remove LangGraph imports |
| `news_pipeline/source_catalog.py` | Edit | Simplify implementation behind `apply_source_catalog_patch` while preserving its caller interface and documented file behavior |
| `tests/test_source_catalog.py` | Edit | Keep behavior assertions at the source catalog interface; relax quote-only formatting assertions only if no behavior is lost |
| `news_pipeline/config.py` | Edit | Remove unused top-funnel config and dead `NewsSource`; remove dead runtime knob |
| `news_pipeline/ui.py` | Edit | Remove UI display of removed top-funnel knob if config field disappears |
| `news_pipeline/pipeline.py` | Edit | Remove duplicate helpers/delegates, import shared helpers, and cautiously reduce compatibility globals |
| `news_pipeline/source_checks.py` | Edit | Use shared Google News and date helpers |
| `news_pipeline/story_clustering.py` | Edit | Use shared date and artifact helpers |
| `news_pipeline/article_summary_records.py` | Edit | Use shared artifact helper |
| `news_pipeline/text_cleaning.py` | Edit | Home for shared `strip_model_artifacts`; avoid creating a new text-cleanup seam |
| `news_pipeline/feed_utils.py` | Create only if Step 3 still has multiple callers | Small shared mechanics module for Google News URL helpers and feed datetime parser |
| `news_pipeline/history_store.py` | Edit | Remove unused `HistoryConfig` |
| `news_pipeline/cli.py` | Edit | Remove `todays_news.py` compatibility docs if wrapper/alias are removed |
| `todays_news.py` | Delete | Remove one-file compatibility wrapper |
| `pyproject.toml` | Edit | Remove unused deps and `todays-news` alias |
| `uv.lock` | Edit via `UV_CACHE_DIR=.uv-cache uv lock` | Reflect dependency removals |
| Tests under `tests/` | Edit only as required | Preserve assertions while removing obsolete compatibility expectations |

## Assumptions To Verify

- [ ] `news_pipeline/article_summarization.py` is the only importer of `langgraph`. If not, update the dependency-removal step to include every importer.
- [ ] `langchain` and `langchain-community` are not imported directly; `langchain-openai` and `langchain-core` remain required.
- [ ] `datetime` in `pyproject.toml` is the third-party package name, not needed for stdlib `datetime`.
- [ ] `load_top_funnel_providers`, `TOP_FUNNEL_PROVIDERS`, and `top_of_funnel_per_provider` have no runtime use. If any real selection code still uses them, block.
- [ ] `NewsSource` and `HistoryConfig` are unused. If an external import is discovered in tests/docs, block before deleting.
- [ ] Deleting `todays_news.py` and the `todays-news` console script is acceptable because README now documents `uv run news ...`. If hidden automation still calls either, block and keep only the dependency/code cuts.
- [ ] Source catalog edits do not require preserving comments, CRLF, or minimal diffs before using whole-file `safe_dump`. If any of those remain part of the interface, simplify the existing implementation instead of changing file-level behavior.
- [ ] The compatibility-global cleanup is transitional but not yet fully removed. If tests like `test_session_finalizer_survives_compat_global_drift` still encode required behavior, stop and write a Blocker Note for that step rather than weakening tests casually.

## Step-By-Step Plan

### Step 1: Confirm Baseline And Ownership

- Status: `[x] Complete`
- Context to read:
  - `git status --short` - identify existing user changes
  - `pyproject.toml` - dependency and scripts state
  - `README.md` - documented public commands
  - `tests/` - available unit-test names
- Files to edit/create:
  - This plan file only - mark progress
- Instructions:
  1. Run `git status --short` and record dirty files in Progress Log.
  2. Run `rg "langgraph|langchain-community|from langchain|todays_news|todays-news|top_funnel|TOP_FUNNEL|NewsSource|HistoryConfig" .`.
  3. Confirm the ten target cuts are still represented by current code.
- Validation:
  - Inspection only.
  - Expected result: no surprise external callers beyond the files named in this plan.
- Stop if:
  - A target cut is already partly implemented in a way that conflicts with this plan.
  - A dirty file contains unrelated user edits in the same block you intend to change and the merge is not obvious.
- Completion note:
  - 2026-06-21 16:34 CDT | Codex | Inspection only | Confirmed dirty worktree entries, plan targets, and current symbol/test coverage. No product files changed.

### Step 2: Centralize Model Artifact Cleanup

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/text_cleaning.py` - existing shared text helpers
  - `news_pipeline/article_summary_records.py` - current `strip_model_artifacts`
  - `news_pipeline/story_clustering.py` - duplicate `strip_model_artifacts`
  - `news_pipeline/pipeline.py` - call sites for `strip_model_artifacts`
- Files to edit/create:
  - `news_pipeline/text_cleaning.py` - add shared `strip_model_artifacts`
  - `news_pipeline/article_summary_records.py` - import/use shared helper, delete duplicate
  - `news_pipeline/story_clustering.py` - import/use shared helper, delete duplicate
  - `news_pipeline/pipeline.py` - import/use shared helper if it has a local duplicate
- Instructions:
  1. Move the exact model-artifact stripping behavior into `news_pipeline/text_cleaning.py` as `strip_model_artifacts`.
  2. Replace duplicate definitions in `article_summary_records.py` and `story_clustering.py` with imports.
  3. In `pipeline.py`, either import the shared helper directly or keep a temporary alias only if removing the local name would cause a large churn. If an alias remains, mark it with a small comment and delete it in Step 9.
  4. Do not change cleanup regex behavior in this step.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_article_summary_records tests.test_story_drafting tests.test_topicless_global_pipeline`
  - Expected result: tests pass; no duplicate `def strip_model_artifacts` remains outside `text_cleaning.py`.
- Stop if:
  - The duplicate helpers have intentionally different behavior that tests depend on.
- Completion note:
  - 2026-06-21 16:35 CDT | Codex | `news_pipeline/text_cleaning.py`, `news_pipeline/article_summary_records.py`, `news_pipeline/story_clustering.py`, `news_pipeline/pipeline.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_article_summary_records tests.test_story_drafting tests.test_topicless_global_pipeline` | Passed. `strip_model_artifacts` now lives in `text_cleaning.py`; duplicate local definitions were removed.

### Step 3: Centralize Google News URL Helpers And Feed Date Parsing

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/pipeline.py` lines around `_is_google_news_url`, `_decode_google_news_article_path`, `_resolve_google_news_url_details`, `_parse_feed_datetime`
  - `news_pipeline/source_checks.py` duplicate Google News helpers and fuller `_parse_feed_datetime`
  - `news_pipeline/story_clustering.py` `_parse_feed_datetime`
- Files to edit/create:
  - `news_pipeline/feed_utils.py` - create shared helper module, or use an existing small module if one appears
  - `news_pipeline/pipeline.py` - import shared helpers and delete duplicate bodies
  - `news_pipeline/source_checks.py` - import shared helpers and delete duplicate bodies
  - `news_pipeline/story_clustering.py` - import shared date parser and delete duplicate body
  - `tests/test_article_collection.py` or a new focused test file - add a small parser/helper regression test if no existing test covers the helpers
- Instructions:
  1. Create `news_pipeline/feed_utils.py` only if the caller audit still shows at least two live callers. Keep it as a shared mechanics module with these helpers:
     - `is_google_news_url`
     - `google_news_query_target`
     - `decode_google_news_article_path`
     - `resolve_google_news_url`
     - `parse_feed_datetime`
  2. Use the fuller date parsing behavior from `source_checks.py`, including Portuguese token normalization and ISO `Z` handling.
  3. Preserve each caller's visible datetime contract. If the shared parser returns timezone-aware UTC datetimes, keep `story_clustering.py` sorting behavior by normalizing at `_article_sort_datetime` rather than changing article order semantics accidentally.
  4. Keep pipeline-specific redirect fallback details in `pipeline.py` as `resolve_google_news_url_details`, but have it call shared helpers for query/decode mechanics.
  5. Update imports and call sites. If private underscore names are needed for minimal churn, assign aliases at import time, for example `from .feed_utils import is_google_news_url as _is_google_news_url`.
  6. Add focused tests for Google News query target and ISO/RFC date parsing if existing tests do not already cover them.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_article_collection tests.test_source_catalog tests.test_topicless_global_pipeline`
  - `rg "def _is_google_news_url|def _google_news_query_target|def _decode_google_news_article_path|def _parse_feed_datetime" news_pipeline`
  - Expected result: duplicate definitions are gone or reduced to import aliases; behavior tests pass.
- Stop if:
  - Source checks and pipeline require materially different Google News behavior beyond pipeline's redirect fallback.
- Completion note:
  - 2026-06-21 16:40 CDT | Codex | `news_pipeline/feed_utils.py`, `news_pipeline/source_checks.py`, `news_pipeline/pipeline.py`, `news_pipeline/story_clustering.py`, `tests/test_feed_utils.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_feed_utils tests.test_article_collection tests.test_source_catalog tests.test_topicless_global_pipeline` | Passed. Shared Google News helpers and feed date parsing now live in `feed_utils.py`; the pipeline keeps a thin naive-date wrapper for its existing recent-window contract.

### Step 4: Remove Unused Top-Funnel Provider Path

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - `top_of_funnel_per_provider`, `load_top_funnel_providers`, runtime knob registry
  - `news_pipeline/pipeline.py` - `TOP_OF_FUNNEL_PER_PROVIDER`, `TOP_FUNNEL_PROVIDERS`, diagnostics settings
  - `news_pipeline/ui.py` - runtime snapshot display
  - `config/sources.yaml` - any `top_funnel_providers` block
  - `tests/test_source_catalog.py` - tests preserving `top_funnel_providers`
- Files to edit/create:
  - `news_pipeline/config.py` - remove field, env knob, loader, and config assignment
  - `news_pipeline/pipeline.py` - remove unused constants and diagnostics settings
  - `news_pipeline/ui.py` - remove runtime snapshot field
  - `config/sources.yaml` - remove `top_funnel_providers` block if present and unused
  - `tests/test_source_catalog.py` - remove assertions that unrelated YAML top-funnel blocks are preserved if the block is deleted from fixtures
  - `README.md` - remove docs if it mentions top-funnel behavior
- Instructions:
  1. Run `rg "top_funnel_providers|TOP_FUNNEL_PROVIDERS|top_of_funnel_per_provider|NEWS_TOP_OF_FUNNEL_PER_PROVIDER" news_pipeline tests README.md config`.
  2. Remove the unused runtime config field and UI display.
  3. Remove `load_top_funnel_providers` only if it has no callers.
  4. If `config/sources.yaml` still contains `top_funnel_providers`, delete that block only after confirming no loader uses it.
  5. Adjust tests to assert current source edit behavior without preserving a dead block.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_source_catalog`
  - `rg "top_funnel_providers|TOP_FUNNEL_PROVIDERS|NEWS_TOP_OF_FUNNEL_PER_PROVIDER" news_pipeline tests README.md config`
  - Expected result: tests pass and no dead top-funnel symbols remain, except possibly historical text in plan/progress notes.
- Stop if:
  - A real runtime caller uses top-funnel providers for article collection.
- Completion note:
  - 2026-06-21 16:42 CDT | Codex | `news_pipeline/config.py`, `news_pipeline/pipeline.py`, `news_pipeline/ui.py`, `tests/test_source_catalog.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_source_catalog` | Passed. The dead top-funnel runtime knob, loader, globals, and UI snapshot field were removed; the source-catalog fixture no longer carries a `top_funnel_providers` block.

### Step 5: Remove Dead Dataclasses

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - `NewsSource`
  - `news_pipeline/history_store.py` - `HistoryConfig`
  - `rg "NewsSource|HistoryConfig" news_pipeline tests README.md`
- Files to edit/create:
  - `news_pipeline/config.py` - delete `NewsSource`
  - `news_pipeline/history_store.py` - delete `HistoryConfig`
- Instructions:
  1. Confirm both classes have no constructor calls or imports outside their defining files.
  2. Delete `NewsSource`.
  3. Delete `HistoryConfig`.
  4. Remove any now-unused imports caused by these deletions.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_history_store tests.test_runtime_config_resolution`
  - `rg "NewsSource|HistoryConfig" news_pipeline tests README.md`
  - Expected result: tests pass and no references remain.
- Stop if:
  - External tests or code import either class.
- Completion note:
  - 2026-06-21 16:43 CDT | Codex | `news_pipeline/config.py`, `news_pipeline/history_store.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_history_store tests.test_runtime_config_resolution` | Passed. `NewsSource` and `HistoryConfig` were removed and no references remain outside the plan notes.

### Step 6: Replace LangGraph Article Summarization With Plain Python

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/article_summarization.py` - all of `_build_article_summary_app` and `run_article_summary_pass`
  - `tests/test_topicless_global_pipeline.py` - article summarization runtime fixture behavior
  - `tests/test_terminal_progress.py` - progress behavior
  - `tests/test_gemma4_article_budget.py` - article summary budget behavior
- Files to edit/create:
  - `news_pipeline/article_summarization.py` - remove LangGraph imports, `ArticleSummaryState`, `_build_article_summary_app`; implement simple loop
  - Tests only if they are too coupled to LangGraph internals, not for product behavior changes
- Instructions:
  1. Preserve the public function signature of `run_article_summary_pass(article_targets, runtime)`.
  2. Preserve concurrency behavior: when `article_summary_concurrency > 1`, process individual articles concurrently and return results in original order.
  3. For the single-article path, implement up to 3 attempts:
     - Build messages using `build_article_summary_prompt_messages`.
     - Invoke model with `task_name=f"analysis for {target}"` and fallback content from `runtime.build_article_fallback_entry`.
     - If `runtime.has_structured_entry(response.content, target)` is true, normalize response, notify completion, and return the record.
     - If invalid and attempts remain, retry with the same model call path plus the existing format-error message if needed.
     - After 3 invalid attempts, normalize the fallback entry, notify completion, and return fallback record.
  4. Remove `TypedDict`, `Annotated`, `RemoveMessage`, `END`, `StateGraph`, and `add_messages` imports if unused.
  5. Do not change prompt text unless needed to keep the same retry instruction.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_topicless_global_pipeline tests.test_terminal_progress tests.test_gemma4_article_budget tests.test_article_summary_records`
  - `rg "langgraph|StateGraph|add_messages|RemoveMessage|ArticleSummaryState" news_pipeline tests pyproject.toml`
  - Expected result: no code imports `langgraph`; tests pass.
- Stop if:
  - Existing tests assert LangGraph-specific streaming behavior rather than output behavior.
- Completion note:
  - 2026-06-21 16:45 CDT | Codex | `news_pipeline/article_summarization.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_topicless_global_pipeline tests.test_terminal_progress tests.test_gemma4_article_budget tests.test_article_summary_records` | Passed. The LangGraph workflow was replaced with a direct retry loop and the public function signature stayed the same.

### Step 7: Remove Dependency Cruft And Wrapper Alias

- Status: `[x] Complete`
- Context to read:
  - `pyproject.toml` - dependencies and project scripts
  - `uv.lock` - lock entries
  - `news_pipeline/cli.py` - compatibility usage text
  - `README.md` - public usage docs
  - `todays_news.py` - wrapper file
- Files to edit/create:
  - `pyproject.toml` - remove `datetime`, `langchain`, `langchain-community`, `langgraph`; remove `todays-news` script
  - `uv.lock` - regenerate with `UV_CACHE_DIR=.uv-cache uv lock`
  - `news_pipeline/cli.py` - remove `todays_news.py` wrapper compatibility block
  - `todays_news.py` - delete
  - `README.md` - update if it mentions wrapper or removed aliases
- Instructions:
  1. After Step 6, run `rg "langgraph|from langchain|import langchain|langchain-community|datetime\\\"" news_pipeline tests pyproject.toml`.
  2. Remove `datetime`, `langchain`, `langchain-community`, and `langgraph>=1.1.3` from `pyproject.toml`.
  3. Keep `langchain-openai`; do not remove it unless no `ChatOpenAI` import remains.
  4. Remove the `todays-news` script from `[project.scripts]`.
  5. Delete `todays_news.py` and remove wrapper-compatibility docs in `news_pipeline/cli.py`.
  6. Run `UV_CACHE_DIR=.uv-cache uv lock`. If it fails due network/sandbox but `pyproject.toml` is correct, record the blocker or rerun with approved escalation if implementing interactively.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_topicless_global_pipeline`
  - `rg "todays_news|todays-news|langgraph|langchain-community|\\\"datetime\\\"" .`
  - Expected result: no stale dependency/wrapper references remain except `uv.lock` historical package names should be gone after lock regeneration.
- Stop if:
  - Hidden automation or README still requires `todays_news.py`; block and keep wrapper while still removing deps.
- Completion note:
  - 2026-06-21 16:46 CDT | Codex | `pyproject.toml`, `uv.lock`, `news_pipeline/cli.py`, `news_pipeline/config.py` | `UV_CACHE_DIR=.uv-cache uv lock`; `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_topicless_global_pipeline` | Passed. The `todays-news` wrapper alias and dead dependency entries were removed, and the lockfile was regenerated accordingly.

### Step 8: Simplify Source Catalog Editing

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/source_catalog.py` - full file
  - `tests/test_source_catalog.py` - source catalog assertions
  - `news_pipeline/source_checks.py` - wrappers `write_source_languages`, `remove_source_blocks`
  - `news_pipeline/ui.py` - source upsert/delete callers
- Files to edit/create:
  - `news_pipeline/source_catalog.py` - remove unnecessary parser/rendering helpers only where behavior stays local to `apply_source_catalog_patch`
  - `tests/test_source_catalog.py` - adjust quote-only expectations to semantic YAML expectations if safe; keep non-source YAML and newline behavior tests unless Step 4 intentionally removed that data
- Instructions:
  1. Keep public dataclasses and functions unless a caller audit proves they are unused:
     - `UpsertSource`
     - `DeleteSources`
     - `SetSourceLanguages`
     - `MarkTranslationRequired`
     - `SourceCatalogPatchResult`
     - `load_source_records`
     - `load_source_rows`
     - `load_source_records_from_lines`
     - `apply_source_catalog_patch`
  2. First try the smallest simplification behind `apply_source_catalog_patch`: one in-memory list of source records, shared mutation helpers for upsert/delete/languages/translation flags, and one render/write path. Do not expose new helpers to callers.
  3. Preserve core semantics:
     - Unknown source fields raise `ValueError`.
     - `append_only=True` rejects existing sources.
     - Delete removes only matching source records.
     - `SetSourceLanguages` only writes missing language unless `overwrite=True`.
     - `MarkTranslationRequired` sets `requires_translation: true` and `translation_source_language` when provided.
     - Optional empty values are removed except required `key`, `name`, `url`.
     - Field ordering follows `SOURCE_FIELD_ORDER`.
     - Existing non-`sources` YAML remains preserved unless Step 4 deliberately removed that data.
     - CRLF input files keep CRLF output unless tests/docs are changed with an explicit reason.
  4. Use whole-file `yaml.safe_dump(..., sort_keys=False, allow_unicode=True)` only if the caller audit and tests prove comments/newlines/minimal diffs are not part of the interface. Otherwise keep a lean line-based renderer and delete only helpers made redundant by the new internal shape.
  5. Update tests to parse YAML and compare data structures instead of exact quote formatting, unless a formatting contract is explicitly documented. Do not remove preservation tests merely to make `safe_dump` easier.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_source_catalog`
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_topicless_global_pipeline`
  - Expected result: source catalog behavior passes and the implementation is shorter or clearer behind the same interface. Exact helper names may remain only if they still earn their keep for preserving file behavior.
- Stop if:
  - Preserving comments/CRLF/minimal line edits is mandatory and the remaining simplification would be a broad rewrite. This was the riskiest simplification in the audit.
- Completion note:
  - 2026-06-21 16:48 CDT | Codex | `news_pipeline/source_catalog.py`, `tests/test_source_catalog.py` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_source_catalog tests.test_topicless_global_pipeline` | Passed. The source catalog patch path now carries a single in-memory record list through the edit loop while keeping the line-based renderer and CRLF/non-source YAML behavior intact.

### Step 9: Reduce Pipeline Compatibility Globals Carefully

- Status: `[x] Complete - Deferred`
- Context to read:
  - `docs/adr/0002-run-session-owns-daily-run-lifecycle-state.md`
  - `docs/adr/0003-run-finalization-finishes-recorded-run-outcomes.md`
  - `docs/adr/0004-runtime-config-resolution-owns-env-overlays.md`
  - `news_pipeline/pipeline.py` - `_compat_runtime_values`, `RunSession._activate`, `_sync_to_legacy_globals`, `_capture_from_legacy_globals`
  - `tests/test_failed_run_logging.py` - especially compatibility/global drift test
  - `tests/test_run_finalizer.py`
- Files to edit/create:
  - `news_pipeline/pipeline.py` - reduce `_compat_runtime_values` and global mutation only where tests prove it is safe
  - Tests only to remove obsolete compatibility expectations after behavior is intentionally replaced
- Instructions:
  1. Run `rg "ACTIVE_RUN_|MODEL_CALL_STATS|RUN_ACTIVITY_SNAPSHOTS|MANAGED_MODEL_SERVER_|RUN_LOG_FILE|RUN_LOG_FILES|progress_tracker" news_pipeline tests`.
  2. Classify each global in `_compat_runtime_values` and `RunSession._sync_to_legacy_globals`:
     - Still used by free functions during a run.
     - Used only by tests.
     - Truly dead.
  3. Delete only truly dead names first.
  4. For names still used by free functions, prefer passing the existing `RuntimeConfig`, `RunDiagnostics`, `RunFinalizer`, or active `RunSession` only if the edit is local and tests remain clear.
  5. Do not add a new lifecycle Module, wrapper class, or adapter. If the next obvious move is a real RunSession/RunFinalizer deepening, stop and write a Blocker Note for a separate precise plan.
  6. If reducing globals requires touching many unrelated pipeline functions, stop and write a Blocker Note. Do not perform a broad orchestration rewrite under this plan.
  7. If a compatibility alias from Step 2 remains, delete it here only if all local call sites can import the shared helper directly.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_failed_run_logging tests.test_run_finalizer tests.test_terminal_progress`
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_topicless_global_pipeline`
  - Expected result: tests pass; `_compat_runtime_values` contains fewer entries; any remaining globals are justified by live call sites or a Blocker Note.
- Stop if:
  - `test_session_finalizer_survives_compat_global_drift` or a similar test still documents required compatibility behavior and no small local replacement is obvious.
- Completion note:
  - 2026-06-21 17:13 CDT | Codex | Consultation only; no product code changed | Rechecked the compatibility-global evidence and confirmed this is outside the safe ponytail-audit cut scope. The remaining globals are live transitional adapters for pipeline free functions and compatibility tests, especially `test_session_finalizer_survives_compat_global_drift`. Follow-up should be a separate RunSession/RunFinalizer deepening plan or an explicit decision to relax legacy-global compatibility expectations.

### Step 10: Final Validation And Cleanup

- Status: `[x] Complete`
- Context to read:
  - `git diff --stat`
  - `git diff -- pyproject.toml uv.lock news_pipeline tests README.md`
  - This plan's Progress Log
- Files to edit/create:
  - This plan file - final status and progress
  - Any touched docs/tests only for obvious stale references found in validation
- Instructions:
  1. Run targeted tests from each completed step if not already recorded.
  2. Run the full test suite if local resources allow: `UV_CACHE_DIR=.uv-cache uv run python -m unittest discover tests`.
  3. Run stale-reference scans:
     - `rg "langgraph|langchain-community|\\\"datetime\\\"|todays_news|todays-news|top_funnel_providers|TOP_FUNNEL_PROVIDERS|NewsSource|HistoryConfig" .`
     - `rg "def strip_model_artifacts|def _is_google_news_url|def _google_news_query_target|def _decode_google_news_article_path|def _parse_feed_datetime" news_pipeline`
  4. Inspect `git diff --stat` and ensure no unrelated files were changed.
  5. Mark plan status `Complete` only if all implemented steps passed validation and any deferred step has a Consultation Note. If an active blocker remains, mark `Blocked` and leave a clear Blocker Note.
- Validation:
  - Full suite or explicit reason it was not run.
  - Expected result: tests pass or blocker is recorded; stale references are gone except plan/progress notes.
- Stop if:
  - Any validation fails and the fix is not an obvious local correction.
- Completion note:
  - 2026-06-21 17:17 CDT | Codex | `plans/2026-06-21-execute-ponytail-audit-cuts.md` | `UV_CACHE_DIR=.uv-cache uv run python -m unittest discover tests` panicked in `uv` with `system-configuration` NULL-object setup; validated instead with `.venv/bin/python -m unittest discover tests` (passed). Product-only stale-reference scan returned no matches, and the helper-definition scan showed only the shared `strip_model_artifacts` helper plus the intentional `_parse_feed_datetime` wrapper.

## Validation Plan

Run these after the relevant steps, not before implementing the plan:

| Command | When to run | Expected result |
| --- | --- | --- |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_article_summary_records tests.test_story_drafting tests.test_topicless_global_pipeline` | After Step 2 | Shared model-artifact cleanup preserves behavior |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_article_collection tests.test_source_catalog tests.test_topicless_global_pipeline` | After Step 3 | Shared feed helpers preserve article collection/source utility behavior |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_source_catalog` | After Step 4 | Runtime config and source catalog tests pass after top-funnel deletion |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_history_store tests.test_runtime_config_resolution` | After Step 5 | Dead dataclass deletion has no behavior effect |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_topicless_global_pipeline tests.test_terminal_progress tests.test_gemma4_article_budget tests.test_article_summary_records` | After Step 6 | Plain article-summary loop matches old behavior |
| `UV_CACHE_DIR=.uv-cache uv lock` | After Step 7 | Lockfile reflects dependency removals |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_topicless_global_pipeline` | After Step 7 | Dependency/script cleanup did not break imports or CLI config |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_source_catalog` | After Step 8 | Source catalog rewrite preserves behavior |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_failed_run_logging tests.test_run_finalizer tests.test_terminal_progress tests.test_topicless_global_pipeline` | After Step 9 | Compatibility-global cleanup did not break run lifecycle behavior |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest discover tests` | Final | Full unit test suite passes |

## Final Review Checklist

- [x] All ten audit suggestions were implemented or explicitly deferred with a Consultation Note or Blocker Note.
- [x] All planned files were updated or explicitly deferred with a Consultation Note or Blocker Note.
- [x] Every completed step has a Progress Log entry.
- [x] Validation commands were run and results recorded.
- [x] User-facing behavior matches the Objective.
- [x] `pyproject.toml` and `uv.lock` agree.
- [x] No unrelated files were changed.
- [x] Existing user worktree changes were preserved.

## Progress Log

| Time | Agent | Step | Status | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-21 16:03 | Codex | Plan creation | Complete | Created this precise plan from the ponytail-audit findings. No product code changed. |
| 2026-06-21 16:12 | Codex | Plan improvement | Complete | Added codebase-design guardrails; narrowed shared mechanics, source catalog, feed datetime, and RunSession compatibility instructions. No product code changed. |
| 2026-06-21 16:49 | Codex | Step 9 | Blocked | Audited remaining compatibility globals against RunSession/RunFinalizer ADRs and the compatibility tests. No safe deletion was obvious without a broader orchestration rewrite. |
| 2026-06-21 16:52 | Codex | Mid-plan consultation | Blocked | Confirmed the Step 9 blocker is current. `test_session_finalizer_survives_compat_global_drift`, `RunSession._sync_to_legacy_globals`, and live pipeline free-function usages still require the compatibility bridge. No product code changed. |
| 2026-06-21 17:13 | Codex | Step 9 | Complete - Deferred | Reconfirmed Step 9 is outside this ponytail-audit scope because safe removal requires separate RunSession/RunFinalizer deepening or an explicit compatibility expectation change. Cleared active blocker state and set resume point to Step 10. No product code changed. |
| 2026-06-21 17:17 | Codex | Step 10 | Complete | Final validation passed with `.venv/bin/python -m unittest discover tests` after `UV_CACHE_DIR=.uv-cache uv run python -m unittest discover tests` panicked in `uv`. Product-only stale-reference scan was clean; helper-definition scan showed only the shared `strip_model_artifacts` helper and the intentional `_parse_feed_datetime` wrapper. |

## Blocker Notes

No active blockers at cleanup time. If a future worker stops, append a new blocker note below this template.
