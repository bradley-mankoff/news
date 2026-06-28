# Redesign News Control Panel Run Setup Precise Plan

## Plan Metadata

- Created: 2026-06-28 00:40 EDT
- Workspace: `/Users/home/personal_code/news`
- Request: Redesign the internal UI so the first screen is an intuitive top-to-bottom run setup with fundamental settings, nested model settings, budgets, preset management, reset defaults, and a sticky Run button.
- Plan file: `plans/2026-06-28-redesign-news-control-panel-run-setup.md`
- Status: `Not started`

## Objective

Replace the current dense tab-first control panel with a run-first internal setup screen. The first viewport should guide the user through recipient scope, source scope, model/settings choices, budgets, peripheral toggles, preview, and Run. Presets should be easy to load from a drawer, save, edit, rename by display name, and delete with confirmation.

Keep the implementation small. This project serves the UI from `news_pipeline/ui.py` with stdlib HTTP plus inline HTML/CSS/JS; keep that stack. Use existing Runtime Config Resolution for defaults, preview, and command generation. Add only the smallest backend metadata needed for last-modified dates, model-setting presets, and any runtime knob that is truly missing.

## Non-Goals

- Do not create an end-customer/client UI.
- Do not add a frontend framework, bundler, CSS framework, icon package, or browser test dependency.
- Do not refresh Graphify or edit `graphify-out/`.
- Do not change newsletter content, story selection semantics, prompts, email formatting, source data, or recipient data except through existing UI behavior.
- Do not implement true independent multi-model execution in this plan unless Step 1 verifies the backend already supports it. Current evidence says article summarization and story writing both use one `MODEL_NAME` and one `MODEL_BASE_URL`.
- Do not rename preset IDs in this pass. "Rename preset" means edit the displayed `name`; the ID remains the CLI/API identifier.

## Worker Instructions

Read this whole plan before editing. Work steps in order. After completing a step, update that step's checkbox and add a Progress Log entry with files changed, validation run, and result.

The worktree had untracked `.agent-daily-logs/` and `.rtk/` when this plan was created. Preserve unrelated user changes. Do not revert files you did not intentionally edit.

If a referenced file is missing, code differs materially from this plan, a command fails unexpectedly, or a requirement is ambiguous, first use bounded self-escalation from `implement-precise-plan` for small local mismatches. If that does not clearly authorize continuing, stop. Mark the step blocked, add a Blocker Note, and leave the repository in the least surprising state possible.

If a consultation later clears a blocker, resume at the first incomplete step unless a `Resume Point` or latest Consultation Note says otherwise.

## Self-Escalation Notes

No self-escalations yet. If `implement-precise-plan` encounters a small local mismatch before blocking, record the bounded decision here: trigger, classification, evidence inspected, allowed action, validation required, scope guard, and explicit return to worker mode.

## Consultation Notes

No consultations yet. If `mid-plan-consultation` is used, record the current branch/worktree state, resolved blocker, deferred step, plan edits, validation evidence, and resume point here.

## Resume Point

Resume at the first incomplete step unless a later consultation note names a different step. Steps marked `Complete - Deferred` count as complete for this plan and should not be retried by `implement-precise-plan`.

## Context Files To Read First

| Path | Why it matters | What to look for |
| --- | --- | --- |
| `AGENTS.md` | Local repo instructions | Ponytail mode, RTK/uv routing, Graphify rules |
| `README.md` | Documented UI and runtime behavior | `uv run news ui`, run presets, runtime variables |
| `SETTINGS.md` | Runtime knob reference | Defaults, advanced knobs, removed settings, current note that model concurrency is derived |
| `CONTEXT.md` | Domain vocabulary | Runtime Config Resolution and Run Session ownership |
| `docs/adr/0004-runtime-config-resolution-owns-env-overlays.md` | Runtime config boundary | New knobs should resolve before Run Session starts |
| `pyproject.toml` | Project tooling | Python version, `news` console script, no frontend deps |
| `config/run_presets.yaml` | Existing saved presets | Current env-map schema and legacy records with no `updated_at` |
| `news_pipeline/config.py` | Runtime config and knob registry | `RuntimeConfig`, `runtime_knob_registry`, model profiles, concurrency defaults, `load_run_presets` |
| `news_pipeline/ui.py` | Entire current UI | `schema_payload`, preset CRUD helpers, `preview_payload`, `build_command`, inline `HTML` |
| `news_pipeline/pipeline.py` | Model execution reality | `build_chat_model`, one `MODEL_NAME`, one `MODEL_BASE_URL`, managed model server lifecycle |
| `tests/test_runtime_config_resolution.py` | Current runtime/UI tests | Preset precedence, preview/build command assertions |
| `tests/test_gemma4_article_budget.py` | Model-derived concurrency contract | Tests asserting article/story concurrency env vars do not override model profile |
| `tests/test_source_catalog.py` | Source UI helper tests | Existing UI helper test style |

## Files To Edit Or Create

| Path | Action | Purpose |
| --- | --- | --- |
| `news_pipeline/ui.py` | Edit | Redesign the inline UI, expose preset/model-setting metadata, wire drawer/modal/reset/run behavior |
| `news_pipeline/config.py` | Edit only if needed | Add missing runtime knob metadata and preserve preset metadata in `load_run_presets`; add source collection concurrency env if implementing that knob |
| `tests/test_runtime_config_resolution.py` | Edit | Cover runtime/schema/preset behavior added for this UI |
| `tests/test_gemma4_article_budget.py` | Edit only if Step 1 authorizes concurrency behavior changes | Preserve or deliberately update model-derived concurrency expectations |
| `README.md` | Edit | Briefly update UI description if behavior changes materially |
| `SETTINGS.md` | Edit | Document new/renamed runtime knobs and preset metadata if added |
| `config/run_presets.yaml` | Avoid manual edit unless required by tests/docs | Existing presets should migrate lazily through UI writes |

## Assumptions To Verify

- [ ] `news_pipeline/ui.py` still owns the only browser UI and serves one inline `HTML` string. If a separate frontend exists, stop and write a Blocker Note.
- [ ] `build_chat_model(max_tokens, task=...)` still uses one configured `MODEL_NAME` and one `MODEL_BASE_URL`. If true independent summarization and story-writing models are required now, stop and write a Blocker Note instead of creating fake selectors.
- [ ] `NEWS_SOURCE_SCOPE=peripheral` is still the runtime value for "all sources" because `SOURCE_SCOPE_TIERS` maps it to core plus peripheral. If naming changed, use the current config values.
- [ ] `NEWS_BLOCK_REUSED_URLS` remains the existing knob for avoiding reuse of previously recorded article URLs. If a separate "write over-time article log" knob exists, use that instead.
- [ ] Existing run presets can accept optional metadata fields such as `updated_at` without breaking CLI runs. If `load_run_presets` rejects unknown fields, update it minimally.
- [ ] No new package is needed. If a worker thinks a dependency is needed for drawer/modal/accordion behavior, stop and write a Blocker Note.

## Step-By-Step Plan

### Step 1: Confirm Scope And Model Reality

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/ui.py` - inspect `schema_payload`, `build_command`, `preview_payload`, `HTML`
  - `news_pipeline/pipeline.py` - inspect `build_chat_model`, managed model server functions, `MODEL_NAME` globals
  - `tests/test_gemma4_article_budget.py` - inspect concurrency tests
- Files to edit/create:
  - This plan file only - update status/progress
- Instructions:
  1. Run `git status --short` and record unrelated dirty files in the Progress Log.
  2. Run `rg "def build_chat_model|MODEL_NAME|MODEL_BASE_URL|MANAGED_MODEL_SERVER|NEWS_ARTICLE_SUMMARY_CONCURRENCY|NEWS_STORY_SYNTHESIS_CONCURRENCY" news_pipeline tests`.
  3. Confirm whether the backend can actually run different article-summary and story-writing models in one run.
  4. If it cannot, proceed with one shared generation model control and two task-specific settings accordions: `Article summarization settings` and `Story writing settings`. Do not show two independent editable model selects that can diverge.
  5. If the user explicitly requires two independently runnable models now, stop here and write a Blocker Note recommending a separate backend model-server plan.
- Validation:
  - Inspection only.
  - Expected result: the plan's one-model UI assumption is either confirmed or blocked before edits.
- Stop if:
  - The code already supports task-specific model servers and this plan's one-model assumption is stale.
  - The user requirement is clarified as "different summarization/story-writing models must run in the same pipeline execution."
- Completion note:
  - Worker fills this in after completing the step.

### Step 2: Add Only Missing Runtime Metadata

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/config.py` - `RuntimeConfig`, `_build_runtime_config`, `runtime_knob_registry`
  - `SETTINGS.md` - documented defaults
  - `tests/test_runtime_config_resolution.py` - runtime config test style
- Files to edit/create:
  - `news_pipeline/config.py` - add missing runtime knob only if needed
  - `tests/test_runtime_config_resolution.py` - add focused test if a knob is added
  - `SETTINGS.md` - document the knob if added
- Instructions:
  1. Add `NEWS_SOURCE_COLLECTION_CONCURRENCY` only if it is still missing. It should default to `DEFAULT_SOURCE_COLLECTION_CONCURRENCY`, clamp to at least `1`, and populate `RuntimeConfig.source_collection_concurrency`.
  2. Add a `runtime_knob_registry` entry for source collection concurrency under a budget/run group.
  3. Do not add env overrides for `article_summary_concurrency` or `story_synthesis_concurrency` in this plan. Current tests assert those are derived from the model profile. The UI can display those as model-derived values.
  4. Add or update a test proving `NEWS_SOURCE_COLLECTION_CONCURRENCY=3` resolves into `RuntimeConfig.source_collection_concurrency == 3`.
  5. Update `SETTINGS.md` to list the new knob if added.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`
  - Expected result: tests pass; existing model-derived concurrency tests still pass.
- Stop if:
  - Adding source collection concurrency requires touching article collection behavior beyond reading `RuntimeConfig.source_collection_concurrency`.
  - Existing tests or docs intentionally forbid source collection concurrency overrides.
- Completion note:
  - Worker fills this in after completing the step.

### Step 3: Preserve Preset Metadata And Add Model-Setting Preset Storage

- Status: `[ ] Not started`
- Context to read:
  - `config/run_presets.yaml` - existing YAML shape
  - `news_pipeline/config.py` - `load_run_presets`
  - `news_pipeline/ui.py` - `_preset_records`, `_write_presets`, `upsert_preset`, `delete_preset`, `duplicate_preset`
- Files to edit/create:
  - `news_pipeline/config.py` - preserve optional preset metadata returned by `load_run_presets`
  - `news_pipeline/ui.py` - update YAML read/write helpers and API payloads
  - `tests/test_runtime_config_resolution.py` - add focused preset metadata tests
- Instructions:
  1. Keep `config/run_presets.yaml` as the storage file. Do not create a second config file unless preserving the existing file becomes materially harder.
  2. Extend run preset records to support optional `updated_at` and `ui` metadata. `env` remains the runtime contract used by CLI and preview.
  3. Add a top-level `model_setting_presets` mapping in the YAML payload, but do not require existing files to have it. Use exact model reference strings as keys, then task keys `article_summary` and `story_writing`, then preset IDs.
  4. A model-setting preset record should have `name`, `description`, `updated_at`, and `env`. Its `env` should be limited to model/profile/task setting variables such as:
     - `NEWS_MODEL_MAX_INPUT_TOKENS`
     - `NEWS_ARTICLE_TEXT_TOKEN_LIMIT`
     - `NEWS_TOTAL_ARTICLE_SUMMARY_CAP`
     - `NEWS_ARTICLE_SUMMARY_MAX_TOKENS`
     - `NEWS_FINAL_SYNTHESIS_MAX_TOKENS`
     - `NEWS_TITLE_GENERATION_MAX_TOKENS`
     - `NEWS_MODEL_*` sampling variables
     - `NEWS_MODEL_ARTICLE_SUMMARY_*`
     - `NEWS_MODEL_FINAL_SYNTHESIS_*`
     - `NEWS_MODEL_TITLE_GENERATION_*`
  5. Update `_write_presets` or replace it with a helper that preserves both `presets` and `model_setting_presets`. Do not drop unknown top-level data unless it is malformed.
  6. On run preset create/update/duplicate, set `updated_at` to the current local time in an ISO-like string. For legacy presets without `updated_at`, display the YAML file mtime as a fallback in API output.
  7. Add small UI API helpers for model-setting presets. Prefer reusing `/api/presets` only if it stays readable; otherwise add narrow endpoints such as `/api/model-setting-presets`.
  8. Add tests with a temporary YAML file or monkeypatching `news_pipeline.ui.RUN_PRESETS_PATH` to prove writing one run preset does not erase `model_setting_presets`, and writing one model-setting preset does not erase `presets`.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - Expected result: new tests pass and existing preset tests still pass.
- Stop if:
  - Preserving metadata requires a broad config module rewrite.
  - YAML comments or ordering become a larger concern than the behavior needed for this internal UI.
- Completion note:
  - Worker fills this in after completing the step.

### Step 4: Build A Run-Setup Schema For The Browser

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/ui.py` - `schema_payload`, `_runtime_snapshot`, `list_presets`
  - `news_pipeline/config.py` - `runtime_knob_registry`, model aliases/profiles
- Files to edit/create:
  - `news_pipeline/ui.py` - schema helpers
  - `tests/test_runtime_config_resolution.py` - schema assertions
- Instructions:
  1. Extend `schema_payload()` so the browser has everything needed for the first screen without hardcoding runtime knowledge in JS:
     - `source_scopes` with labels `Core` and `All`, where `All` maps to runtime value `peripheral`.
     - `recipient_scopes` with labels `Bradley only` and `All`.
     - model choices from the existing model alias registry.
     - global default env values for the visible run setup controls.
     - current effective runtime snapshot.
     - run presets with `id`, display `name`, `description`, `updated_at`, `env`, and optional `ui`.
     - model-setting presets grouped by selected model/task.
  2. Add a small helper that builds model-derived defaults for the selected model from `RuntimeConfig.model_profile`; reuse `_runtime_snapshot` or `resolve_runtime_config` rather than duplicating profile constants in JS.
  3. Ensure default values appear as actual input values in the browser, not only faint placeholders. The browser should track whether a value equals the default so unchanged defaults are not unnecessarily saved as explicit env overrides.
  4. Keep `preview_payload(body)` as the command/runtime authority. Do not invent a second command renderer in JS.
  5. Add a Python assertion test that `schema_payload()` includes preset `updated_at`, model choices, and the source/recipient labels.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - `UV_CACHE_DIR=.uv-cache uv run python -c 'from news_pipeline.ui import schema_payload; s=schema_payload(); assert s["presets"]["presets"]; assert s["source_scopes"]; assert s["recipient_scopes"]'`
  - Expected result: tests pass and schema command exits 0.
- Stop if:
  - Schema generation needs network access or starts a model server.
  - Defaults can only be computed by mutating `os.environ`.
- Completion note:
  - Worker fills this in after completing the step.

### Step 5: Replace The First Screen With Sequential Run Setup

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/ui.py` - entire `HTML` string, CSS, current tab views
- Files to edit/create:
  - `news_pipeline/ui.py` - HTML/CSS markup only for this step if possible
- Instructions:
  1. Keep the UI as one HTML document inside `news_pipeline/ui.py`.
  2. Replace the tab-first layout with a run setup page ordered top-to-bottom and left-to-right:
     - Sticky top banner: title, active preset name/modified status, `Presets` drawer button, `Reset defaults`, `Save`, `Save as`, and primary `Run`.
     - Step 1 `Basics`: recipient scope segmented control and source scope segmented control.
     - Step 2 `Models`: one shared generation model select, followed by collapsed `<details>` blocks for `Article summarization settings` and `Story writing settings`.
     - Step 3 `Budgets`: source collection concurrency, recent window, max articles per source, total article summary cap, max stories, min articles per story, token limits, story thresholds.
     - Step 4 `Peripheral`: image generation, URL reuse blocking/avoid reused articles, relaxed final synthesis guards, and other existing non-secret run toggles that are safe for the normal user.
     - Step 5 `Preview and run log`: command preview and log output.
  3. Use native controls: `<button>`, `<select>`, `<input>`, `<details>`, and `<dialog>`. Use CSS for segmented controls. Do not add icons unless an icon library already exists.
  4. Keep existing source and recipient editors reachable below the setup flow, for example under a collapsed `Manage sources and recipients` section. Do not make them the starting point.
  5. Keep source utility actions such as `check-sources`, `prune-sources`, and `source-languages` behind a collapsed `Utilities` section. The sticky `Run` button should always run the pipeline action with current settings.
  6. Avoid nested cards. Use full-width sections or simple bordered panels; keep border radius at `8px` or less.
  7. Make the layout responsive at mobile widths; no text should overflow buttons or segmented controls.
- Validation:
  - Manual inspection of `news_pipeline/ui.py` after edit.
  - `UV_CACHE_DIR=.uv-cache uv run python -m py_compile news_pipeline/ui.py`
  - Expected result: `py_compile` succeeds and the first HTML view is run setup, not tabs.
- Stop if:
  - The UI was moved out of `news_pipeline/ui.py`.
  - The markup change becomes easier as a full frontend rewrite; that is out of scope.
- Completion note:
  - Worker fills this in after completing the step.

### Step 6: Wire Defaults, Preset Drawer, Save, Rename, Delete, And Reset

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/ui.py` - current JS functions `collectEnv`, `requestBody`, `renderPresets`, `savePresetEditor`, `deleteSelectedPreset`
- Files to edit/create:
  - `news_pipeline/ui.py` - JavaScript and small API handler updates
  - `tests/test_runtime_config_resolution.py` - backend helper tests as needed
- Instructions:
  1. Replace tab-state JS with a single run setup state object. Keep functions small and local.
  2. Render the preset drawer from API data. Each row must show display `name` and `updated_at`; include description if it fits without crowding.
  3. Selecting a preset should populate all visible controls from the preset's env/ui metadata, then preview the command.
  4. `Reset defaults` should set controls to global defaults, clear active preset selection, clear explicit overrides, and preview.
  5. `Save` should PATCH the selected preset. If no preset is selected, route to `Save as`.
  6. `Save as` should open a native `<dialog>` asking for display name and optional ID. Generate a safe default ID from the display name if the user leaves ID blank.
  7. `Rename` should edit the display `name` on the selected preset. Do not change the preset ID.
  8. `Delete` should use a native `<dialog>` confirmation and then call the existing DELETE endpoint.
  9. Keep `duplicate` only if it remains useful and costs very little; otherwise `Save as` covers it.
  10. Do not save unchanged global defaults as explicit env values. Save only values that differ from defaults plus any selected model-setting preset references in `ui` metadata.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - Manual browser check after Step 8: load preset, reset defaults, save as, rename display name, delete with confirmation.
  - Expected result: preset API calls update YAML without erasing unrelated preset data.
- Stop if:
  - Native `<dialog>` is unsupported in the target browser the user uses.
  - Save/delete needs authentication or another persistence layer not present in this app.
- Completion note:
  - Worker fills this in after completing the step.

### Step 7: Wire Model-Setting Presets Without Pretending Multi-Model Support

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/config.py` - `MODEL_TASK_SAMPLING_ENV_PREFIXES`, model profile fields
  - `news_pipeline/ui.py` - model/settings rendering from prior steps
  - `tests/test_gemma4_article_budget.py` - model-derived defaults and concurrency expectations
- Files to edit/create:
  - `news_pipeline/ui.py` - model-setting preset UI and API calls
  - `tests/test_runtime_config_resolution.py` - persistence tests if not covered in Step 3
- Instructions:
  1. Show one shared generation model select unless Step 1 proved true task-specific model execution exists.
  2. Under the model select, render two collapsed settings areas:
     - `Article summarization settings`: article text token limit, total article summary cap, article summary max tokens, article summary sampling variables, and model-derived article summary concurrency shown as read-only text.
     - `Story writing settings`: model input cap, final synthesis max tokens, title max tokens, final synthesis/title sampling variables, and model-derived story synthesis concurrency shown as read-only text.
  3. Each settings area should include a model-setting preset picker scoped to the selected model and task, plus `Save settings`, `Save settings as`, `Rename`, `Delete`, and `Reset to model defaults`.
  4. Saving a model-setting preset should store only the env values for that model/task settings area.
  5. Selecting a model-setting preset should populate that settings area and merge its env values into the run preview body.
  6. A run preset should store references to selected model-setting presets in its `ui` metadata and store the resulting env values in `env` so CLI runs keep working even if the UI metadata is ignored.
  7. When the model changes, clear or re-resolve model-setting preset selections for that model. Do not apply a setting preset saved for another model.
  8. If the user selects a setting value equal to the selected model's default, omit it from saved env unless it is needed to preserve a named preset's intent.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`
  - Manual browser check after Step 8: changing model updates displayed defaults; article/story setting preset lists are model-scoped.
  - Expected result: no task model mismatch is possible unless backend support was explicitly verified.
- Stop if:
  - The implementation starts changing `build_chat_model` or managed server lifecycle. That belongs in a separate backend plan.
  - The model-setting preset schema cannot be kept backward-compatible with existing run presets.
- Completion note:
  - Worker fills this in after completing the step.

### Step 8: Preserve Run, Preview, Logs, Sources, Recipients, And Utilities

- Status: `[ ] Not started`
- Context to read:
  - `news_pipeline/ui.py` - `requestBody`, `preview`, `runAction`, run SSE handling, source/recipient editors, utility options
- Files to edit/create:
  - `news_pipeline/ui.py` - JS event wiring and preserved sections
- Instructions:
  1. The sticky `Run` button must call `/api/run` with `action: "run"` regardless of which collapsed utility section is open.
  2. Preview should refresh after any fundamental setting, budget, toggle, model-setting preset, or run preset changes.
  3. Keep the existing run log SSE behavior and Stop button.
  4. Keep source and recipient CRUD behavior working. Moving markup is fine; changing source/recipient YAML semantics is not.
  5. Keep utility actions working from a non-primary section. Their options can reuse existing inputs, but they should not distract from the main run setup.
  6. Surface runtime errors in the sticky status area and command preview.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_source_catalog tests.test_runtime_config_resolution`
  - `UV_CACHE_DIR=.uv-cache uv run python -c 'from news_pipeline.ui import preview_payload; p=preview_payload({"action":"run","env":{"NEWS_SOURCE_SCOPE":"core","NEWS_RECIPIENT_SCOPE":"bradley"},"options":{}}); assert p["command"][:3] == ["uv","run","news"]'`
  - Expected result: tests pass and preview builds a run command.
- Stop if:
  - Source or recipient editors require broad rewrite unrelated to the first-screen UX.
  - Running preview starts a real pipeline or model server.
- Completion note:
  - Worker fills this in after completing the step.

### Step 9: Update Docs And Run Final Validation

- Status: `[ ] Not started`
- Context to read:
  - `README.md` - UI section
  - `SETTINGS.md` - runtime knobs and preset docs
  - `news_pipeline/ui.py` - final UI behavior
- Files to edit/create:
  - `README.md` - concise UI behavior update
  - `SETTINGS.md` - concise preset/model-setting/source-concurrency update if applicable
  - This plan file - final progress and checklist
- Instructions:
  1. Update docs only where behavior changed. Keep it short.
  2. Run the validation commands below.
  3. Start the UI locally only long enough to manually inspect it, then stop it before final response:
     - `UV_CACHE_DIR=.uv-cache uv run news ui --port 8766`
     - Open `http://127.0.0.1:8766`
     - Confirm first screen is the run setup flow, preset drawer opens, defaults reset, preview updates, and Run button is visible in the sticky banner.
  4. Do not leave the UI server running after validation.
  5. Update the Final Review Checklist and Progress Log.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_source_catalog`
  - `UV_CACHE_DIR=.uv-cache uv run python -m py_compile news_pipeline/ui.py news_pipeline/config.py`
  - Manual browser inspection of `http://127.0.0.1:8766`
  - Expected result: unit tests pass, compile succeeds, and manual inspection confirms the new first-screen flow.
- Stop if:
  - Unit tests fail for behavior not touched by this plan and the failure cannot be explained as pre-existing.
  - Manual inspection shows overlapping text, missing Run button, broken preset drawer, or blank page.
- Completion note:
  - Worker fills this in after completing the step.

## Validation Plan

Run these after the relevant steps, not before implementing the plan:

| Command | When to run | Expected result |
| --- | --- | --- |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget` | After Steps 2, 4, and 7 | Runtime config and model-derived concurrency behavior pass |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_source_catalog tests.test_runtime_config_resolution` | After Step 8 | Source/recipient UI helpers and preview behavior pass |
| `UV_CACHE_DIR=.uv-cache uv run python -m py_compile news_pipeline/ui.py news_pipeline/config.py` | After Steps 5 and 9 | Edited Python files compile |
| `UV_CACHE_DIR=.uv-cache uv run python -c 'from news_pipeline.ui import schema_payload; s=schema_payload(); assert s["presets"]["presets"]; assert s["source_scopes"]; assert s["recipient_scopes"]'` | After Step 4 | Browser schema includes required setup metadata |
| `UV_CACHE_DIR=.uv-cache uv run news ui --port 8766` | Final manual inspection only | UI starts locally; stop it after checking |

## Final Review Checklist

- [ ] All planned files were updated or explicitly deferred with a Consultation Note, step completion note, or active Blocker Note.
- [ ] Every completed step has a Progress Log entry.
- [ ] Validation commands were run and results recorded.
- [ ] The first screen is a run setup flow with sticky Run button.
- [ ] Recipient scope, source scope, model/settings, budgets, and peripheral toggles show defaults as editable values.
- [ ] Run presets show display name and last modified date.
- [ ] Preset load, reset defaults, save, save as, rename display name, and delete confirmation work.
- [ ] Model-setting presets are scoped to selected model and task.
- [ ] No fake independent task-model support was introduced.
- [ ] No unrelated files were changed.

## Progress Log

| Time | Agent | Step | Status | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-28 00:40 | Codex | Plan creation | Complete | Created implementation plan after inspecting README, SETTINGS, CONTEXT, ADR 0004, `news_pipeline/ui.py`, `news_pipeline/config.py`, `news_pipeline/pipeline.py`, preset YAML, and current tests. |

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
