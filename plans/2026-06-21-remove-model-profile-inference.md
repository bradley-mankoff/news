# Remove Legacy Model Profile Inference Precise Plan

## Plan Metadata

- Created: 2026-06-21 18:18 CDT
- Workspace: `/Users/home/personal_code/news`
- Request: "Remove legacy size-class model profile inference from NEWS_MODEL. Add explicit Task Model Assignment, user-owned Model Tuning Presets, model-specific defaults only when Hugging Face guidance exists, and keep the UI grouped by Run Settings, Run Presets, Model Selection, Model Tuning, Pipeline Budget, and Model Server Settings."
- Plan file: `plans/2026-06-21-remove-model-profile-inference.md`
- Status: `Complete`

## Objective

Make `NEWS_MODEL` mean only the default model selection. Runtime Config Resolution should stop inferring a hidden size-class profile from that value, and should stop bundling Model Tuning, Pipeline Budget, and Model Server Settings behind model aliases. Do not keep model-profile compatibility shims in live code.

Add explicit Task Model Assignment so Article Summarization and Story Drafting can use separate model references. Add user-owned Model Tuning Presets for one model or one model-task pair. Keep model-specific code defaults only where there is model-card guidance; otherwise omit sampling/server overrides and let the backend/model defaults ride. Keep the local UI usable by showing separate sections for Run Settings, Run Presets, Model Selection, Model Tuning, Pipeline Budget, and Model Server Settings, with advanced Model Tuning collapsed. Use `story_drafting` as the current task name; do not preserve old user-facing `final_synthesis` aliases.

## Non-Goals

- Do not add new dependencies or a new UI framework.
- Do not refresh Graphify or run semantic extraction.
- Do not implement a full multi-process local model-server manager unless the worker can do it as a small, well-tested extension of existing code. The minimum acceptable behavior is explicit task model/base URL support plus a clear error when a run asks one managed server to serve multiple different models.
- Do not preserve legacy model-profile compatibility in code, tests, UI, or new schemas. Existing physical DuckDB files may still contain old columns, but the current schema/write path should not mention `model_profile`.
- Do not preserve old user-facing `final_synthesis` model-tuning env aliases. Story Drafting is the vocabulary now.
- Do not change prompts, story selection policy, source catalog behavior, recipient behavior, image generation, or email delivery except where model configuration plumbing requires new labels.

## Worker Instructions

Read this whole plan before editing. Work steps in order. After completing a step, update that step's checkbox and add a Progress Log entry with the files changed, validation run, and result.

Use ponytail full: prefer the smallest faithful diff, existing helpers, plain dataclasses/YAML, and one focused runnable check for each non-trivial behavior. Delete legacy compatibility instead of adapting around it. Do not introduce abstractions for future model tasks beyond Article Summarization and Story Drafting unless the current code already requires them.

If a referenced file is missing, the code does not match the plan, a command fails unexpectedly, or a requirement is ambiguous, first use the bounded self-escalation rules from `implement-precise-plan` if the mismatch is small and local. If self-escalation does not clearly authorize continuing, stop. Do not skip the step or invent a different implementation. Mark the step as blocked, add a Blocker Note, and leave the repository in the least surprising state possible.

If a consultation later clears a blocker, the next implementation worker should resume at the first incomplete step unless a `Resume Point` or latest Consultation Note says otherwise.

## Self-Escalation Notes

No self-escalations yet. If `implement-precise-plan` encounters a small local mismatch before blocking, record the bounded decision here: trigger, classification, evidence inspected, allowed action, validation required, scope guard, and explicit return to worker mode.

## Consultation Notes

No consultations yet. If `mid-plan-consultation` is used, record the current branch/worktree state, resolved blocker, deferred step, plan edits, validation evidence, and resume point here.

## Resume Point

Resume at the first incomplete step unless a later consultation note names a different step. Steps marked `Complete - Deferred` count as complete for this plan and should not be retried by `implement-precise-plan`.

## Context Files To Read First

| Path | Why it matters | What to look for |
| --- | --- | --- |
| `AGENTS.md` | Local repo instructions | Ponytail mode, command routing, Graphify rule |
| `CONTEXT.md` | Project vocabulary | Runtime Config Resolution, Task Model Assignment, Model Tuning, Pipeline Budget, Model Server Settings |
| `docs/adr/0007-model-configuration-vocabulary.md` | Architectural decision that matches this task | Exact vocabulary and the decision to stop size-class inference from `NEWS_MODEL` |
| `README.md` | Public CLI and env var contract | Run Settings, Run Presets, current `NEWS_MODEL` docs, model-server command docs |
| `pyproject.toml` | Validation and dependency shape | `uv run news` entrypoint and test dependency |
| `config/run_presets.yaml` | Existing user-owned Run Presets | Current `NEWS_MODEL` usage in run preset env maps |
| `news_pipeline/config.py` | Main implementation target | `ModelRuntimeProfile`, `infer_model_profile_key`, `configured_model_profile`, `RuntimeConfig`, `runtime_knob_registry`, `load_runtime_config` |
| `news_pipeline/pipeline.py` | Model call plumbing and managed server lifecycle | `MODEL_PROFILE`, max-token constants, `build_chat_model`, `_ensure_main_model_server_ready`, diagnostics settings, report filename |
| `news_pipeline/article_summarization.py` | Article Summarization task caller | `task="article_summary"` and runtime max token use |
| `news_pipeline/story_drafting.py` | Story Drafting task caller | Current internal `task="final_synthesis"` string; rename it to `task="story_drafting"` |
| `news_pipeline/story_selection.py` | Uses model profile key today | `StorySelectionRuntime.model_profile_key` and diagnostics output |
| `news_pipeline/ui.py` | Local control panel | `schema_payload`, `_runtime_snapshot`, knob rendering, tabs, preset editor |
| `news_pipeline/diagnostics.py` | User-visible run review | "Legacy model profile", model settings, model activity sections |
| `news_pipeline/history_store.py` | Durable run storage | Remove `model_profile` from current schema/write path; keep new config details in `settings_json` |
| `tests/test_runtime_config_resolution.py` | Main config/UI test surface | Runtime config precedence, model server command, UI preview |
| `tests/test_gemma4_article_budget.py` | Tests legacy profile/budget coupling | Replace profile assertions with budget/model-selection assertions |
| `tests/test_history_store.py` | History and run review assertions | Replace legacy profile fixture text if needed |
| `tests/test_story_drafting.py` | Story drafting runtime surface | Ensure task string/model assignment changes do not break drafting tests |

## Files To Edit Or Create

| Path | Action | Purpose |
| --- | --- | --- |
| `news_pipeline/config.py` | Edit | Replace legacy `ModelRuntimeProfile` inference with explicit model selection, task assignments, model tuning, pipeline budget, and server settings resolution |
| `config/model_tuning_presets.yaml` | Create | User-owned Model Tuning Presets keyed by preset id, with optional `model` and optional `task` |
| `news_pipeline/pipeline.py` | Edit | Consume resolved task model assignments and tuning; remove `MODEL_PROFILE` as active config; update managed server guard and diagnostics settings |
| `news_pipeline/article_summarization.py` | Edit only if needed | Keep Article Summarization using the resolved article-summary task model and max tokens |
| `news_pipeline/story_drafting.py` | Edit | Replace `task="final_synthesis"` with `task="story_drafting"` |
| `news_pipeline/story_selection.py` | Edit | Rename profile-key plumbing to a neutral model/config label or remove it if unused |
| `news_pipeline/ui.py` | Edit | Group settings into separate sections, expose task model assignment and tuning preset controls, collapse advanced Model Tuning |
| `news_pipeline/diagnostics.py` | Edit | Stop rendering "Legacy model profile"; show task model assignments, pipeline budget, server settings, and tuning summary |
| `news_pipeline/history_store.py` | Edit | Remove `model_profile` from the current schema and row writes; rely on `settings_json` for new config snapshots |
| `README.md` | Edit | Document new env vars, tuning preset file, precedence, and server behavior |
| `CONTEXT.md` | Edit only if needed | Keep vocabulary aligned if implementation chooses exact env names not already described |
| `tests/test_runtime_config_resolution.py` | Edit | Add config, tuning preset, UI preview, and no-profile-inference tests |
| `tests/test_gemma4_article_budget.py` | Edit | Replace profile inference tests with explicit Pipeline Budget tests |
| `tests/test_history_store.py` | Edit | Update fixtures/assertions away from legacy profile wording |
| `tests/test_story_drafting.py` | Edit only if needed | Cover story drafting task model/tuning if pipeline-level tests do not |

## Assumptions To Verify

- [ ] `docs/adr/0007-model-configuration-vocabulary.md` is still the intended vocabulary. If the ADR has been superseded, stop and write a Blocker Note.
- [ ] `NEWS_MODEL_ARTICLE_SUMMARY` and `NEWS_MODEL_STORY_DRAFTING` are acceptable env names for task model assignment. If the repo already added different names, use bounded self-escalation only for an obvious rename.
- [ ] Story Drafting is currently implemented through `task="final_synthesis"` in `news_pipeline/story_drafting.py`. Rename that live task string to `story_drafting`; do not keep an alias.
- [ ] `config/model_tuning_presets.yaml` can be absent and should default to no user-owned presets. If config loading currently requires every config file to exist, create an empty file with `presets: {}`.
- [ ] No model-specific default should be added without an in-repo source comment or a checked Hugging Face model-card source. If the source cannot be verified, omit the default and let backend/model defaults ride.
- [ ] A run with different task models and one inherited `NEWS_MODEL_BASE_URL` should not silently use the wrong model. If multi-server management is too broad, fail early with a clear error and document that separate task models require explicit task base URLs or external servers.
- [ ] Existing physical history databases may contain old `model_profile` columns, but current code should not create, populate, read, or document that field.

## Step-By-Step Plan

### Step 1: Confirm Current Coupling And Baseline Tests

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - inspect `ModelRuntimeProfile`, `infer_model_profile_key`, `_override_profile_from_env`, `configured_model_profile`, `_build_runtime_config`
  - `news_pipeline/pipeline.py` - inspect `MODEL_PROFILE`, `MODEL_PROFILE_KEY`, max-token constants, `build_chat_model`, managed server functions
  - `tests/test_runtime_config_resolution.py` and `tests/test_gemma4_article_budget.py` - identify assertions tied to profile inference
- Files to edit/create:
  - This plan file only - update status/progress
- Instructions:
  1. Run `rg -n "ModelRuntimeProfile|infer_model_profile_key|configured_model_profile|MODEL_PROFILE|MODEL_PROFILE_KEY|big_conservative|tiny_codex|gemma_12b_optiq|Legacy model profile" news_pipeline tests README.md docs config`.
  2. Run the current focused tests before editing: `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store`.
  3. Record which tests fail before any code changes. Do not fix failures in this step.
- Validation:
  - Inspection plus the focused baseline command.
  - Expected result: current code still has profile inference and the baseline tests either pass or have recorded pre-existing failures.
- Stop if:
  - Profile inference has already been removed in a way that makes this plan stale.
  - Baseline failures are unrelated and make later validation ambiguous.
- Completion note:
  - Confirmed legacy model-profile coupling is still present in live code and tests. Baseline command `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store` passed under escalated execution.

### Step 2: Add Explicit Config Value Objects And Preset Loading

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - dataclasses near `ModelSamplingSettings` and `RuntimeConfig`
  - `news_pipeline/config.py` - `_load_yaml_mapping`, `load_run_presets`, `runtime_knob_registry`
  - `config/run_presets.yaml` - header/comment style for user-owned YAML
- Files to edit/create:
  - `news_pipeline/config.py` - add value objects and loading helpers
  - `config/model_tuning_presets.yaml` - create empty user-owned config if absent
- Instructions:
  1. Keep `ModelSamplingSettings` if useful, but allow unset sampling fields for backend defaults. The simplest shape is a frozen dataclass with optional fields: `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repetition_penalty`.
  2. Add frozen dataclasses for the new concepts:
     - `ModelTuningSettings`: app token limits and sampling by task. Include `model_max_input_tokens`, `article_summary_max_tokens`, `story_drafting_max_tokens`, `title_generation_max_tokens`, and `task_sampling`.
     - `PipelineBudget`: `article_text_token_limit`, `total_article_summary_cap`, `recent_window_hours`, `max_articles_per_source`, `min_articles_per_story`, `max_stories`, and existing story budget thresholds already in `RuntimeConfig`.
     - `ModelServerSettings`: `base_url`, `prefill_step_size`, `prompt_cache_size`, `prompt_cache_bytes`, `max_tokens`.
     - `TaskModelAssignment`: `task`, `reference`, `name`, `backend`, `base_url`, `server_command`, and `tuning`.
  3. Add constants for user-facing task names: `MODEL_TASK_ARTICLE_SUMMARY = "article_summary"` and `MODEL_TASK_STORY_DRAFTING = "story_drafting"`. Do not add an alias map for `"final_synthesis"`; Step 6 will rename the caller.
  4. Add `MODEL_TUNING_PRESETS_PATH = CONFIG_DIR / "model_tuning_presets.yaml"` and a small loader `load_model_tuning_presets(path: Path | None = None) -> dict[str, dict[str, Any]]`.
  5. Define the YAML shape in code comments and docs comments only once:
     ```yaml
     presets:
       concise-story-drafting:
         model: mlx-community/example-model
         task: story_drafting
         tuning:
           temperature: 0.2
           top_p: 0.9
           max_tokens: 1400
     ```
     `model` and `task` are optional. If present, they must match the resolved model assignment where the preset is applied.
  6. If creating `config/model_tuning_presets.yaml`, keep it minimal:
     ```yaml
     # Saved model tuning presets for the daily news pipeline.
     #
     # Presets are explicit overlays for one model or one model-task pair.
     presets: {}
     ```
  7. Do not wire these objects into runtime behavior yet.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - Add or update a focused test in `tests/test_runtime_config_resolution.py` that `load_model_tuning_presets()` returns `{}` for a missing file and parses a temp YAML preset with `model`, `task`, and `tuning`.
  - Expected result: tests pass and no production behavior changes yet.
- Stop if:
  - The new dataclasses require broad changes outside `config.py` before tests can import the module.
  - Existing YAML loader behavior cannot support an optional config file.
- Completion note:
  - Added explicit config value objects, `load_model_tuning_presets()`, the empty `config/model_tuning_presets.yaml` seed file, and a focused loader test. Runtime behavior remains unchanged.

### Step 3: Remove NEWS_MODEL Profile Inference In Runtime Config

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - `_configured_model_reference`, `resolve_model_name`, `infer_model_backend`, `_configured_model_backend`, `infer_model_profile_key`, `_configured_model_profile_key`, `configured_model_profile`, `_build_runtime_config`
  - `tests/test_runtime_config_resolution.py` - model resolution and server command assertions
  - `tests/test_gemma4_article_budget.py` - profile-derived cap assertions
- Files to edit/create:
  - `news_pipeline/config.py` - remove active profile inference and resolve explicit runtime fields
  - `tests/test_runtime_config_resolution.py` - update/add tests
  - `tests/test_gemma4_article_budget.py` - update/add budget tests
- Instructions:
  1. Delete `infer_model_profile_key`, `_configured_model_profile_key`, `MODEL_RUNTIME_PROFILES`, and `configured_model_profile`. Do not leave a compatibility shim.
  2. Keep `NEWS_MODEL` as the default model reference only:
     - `model_reference = _configured_model_reference()`
     - `model_name = resolve_model_name(model_reference)`
     - `model_backend = infer_model_backend(model_reference)`
  3. Add explicit task model assignment env vars:
     - `NEWS_MODEL_ARTICLE_SUMMARY`: defaults to `NEWS_MODEL`
     - `NEWS_MODEL_STORY_DRAFTING`: defaults to `NEWS_MODEL`
     Resolve each through `resolve_model_name()` and `infer_model_backend()`.
  4. Add optional task base URL env vars:
     - `NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`: defaults to `NEWS_MODEL_BASE_URL`
     - `NEWS_MODEL_STORY_DRAFTING_BASE_URL`: defaults to `NEWS_MODEL_BASE_URL`
  5. Add `RuntimeConfig` fields for `model_assignments` and explicit convenience fields if that keeps `pipeline.py` smaller. At minimum, include assignment entries for `default`, `article_summary`, and `story_drafting`.
  6. Add a generic `PipelineBudget` default independent of model choice. Preserve current practical defaults where possible:
     - `article_text_token_limit`: `4500` unless an existing test indicates another default is safer
     - `total_article_summary_cap`: `GEMMA_4_ARTICLE_SUMMARY_CAP` renamed or aliased to `DEFAULT_TOTAL_ARTICLE_SUMMARY_CAP = 40`
     - Keep explicit `NEWS_ARTICLE_TEXT_TOKEN_LIMIT` and `NEWS_TOTAL_ARTICLE_SUMMARY_CAP` overrides.
  7. Add tests proving model choice no longer changes pipeline budget:
     - Load config with `NEWS_MODEL=gemma-26b-moe`.
     - Load config with `NEWS_MODEL=GEMMA_12B_OPTIQ_MODEL_ALIAS`.
     - Assert the default `total_article_summary_cap` and `article_text_token_limit` are equal unless explicitly overridden.
  8. Add tests proving `NEWS_MODEL_ARTICLE_SUMMARY` and `NEWS_MODEL_STORY_DRAFTING` can differ from `NEWS_MODEL` in the resolved snapshot.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`
  - `rg -n "configured_model_profile|infer_model_profile_key|MODEL_RUNTIME_PROFILES|MODEL_PROFILE_KEY|big_conservative|tiny_codex|gemma_12b_optiq" news_pipeline/config.py tests`
  - Expected result: no runtime config path, test, README section, or config file infers or mentions a size-class profile from `NEWS_MODEL`.
- Stop if:
  - A documented external command cannot be updated inside this repo without a product decision.
  - A budget default cannot be chosen without a product decision.
- Completion note:
  - Replaced legacy profile inference with explicit model assignments, a generic pipeline budget, and model-server settings. Updated the pipeline/UI snapshot path and verified the step with the focused unittest pair plus the grep check.

### Step 4: Resolve Model Tuning Defaults And User Presets

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - current sampling constants, `_override_sampling_from_env`, `_profile_sampling_with_base_overrides`, `_override_profile_from_env`
  - `README.md` and ADR 0007 - expected precedence
  - Existing comments around `GEMMA_12B_DEFAULT_SAMPLING`
- Files to edit/create:
  - `news_pipeline/config.py` - implement tuning precedence and model-specific defaults
  - `tests/test_runtime_config_resolution.py` - add tuning default/preset/env override tests
  - `config/model_tuning_presets.yaml` - keep empty unless adding a harmless example is explicitly useful
- Instructions:
  1. Replace profile-based sampling with this precedence:
     - Backend/model defaults when a sampling field is unset.
     - Code-owned model-specific defaults only for models with verified guidance.
     - User-owned Model Tuning Preset selected by env.
     - Explicit `NEWS_` tuning env vars.
  2. Keep only verified code-owned model guidance. The existing comment says Gemma-4 family guidance starts at `temperature=1`, `top_p=0.95`, `top_k=64`; before retaining that as a code default, either find the model-card source in repo/docs or verify the Hugging Face model card. If not verified, remove that default and let backend/model defaults ride.
  3. Store code-owned defaults in a small mapping keyed by resolved model name, for example `MODEL_SPECIFIC_TUNING_DEFAULTS: dict[str, ModelTuningSettings]`. Do not add defaults for size classes such as "12b" or "26b".
  4. Add env vars for explicit preset selection:
     - `NEWS_MODEL_TUNING_PRESET`: applies to the default model assignment.
     - `NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET`: applies to Article Summarization.
     - `NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET`: applies to Story Drafting.
  5. Validate preset scope:
     - If a preset has `model`, compare it to both the raw reference and resolved model name for the target assignment.
     - If a preset has `task`, compare it to `article_summary` or `story_drafting`.
     - On mismatch, raise `ValueError` naming the preset id, expected model/task, and configured model/task.
  6. Keep current non-legacy direct env tuning overrides where possible:
     - `NEWS_MODEL_MAX_INPUT_TOKENS`
     - `NEWS_ARTICLE_SUMMARY_MAX_TOKENS`
     - Replace `NEWS_FINAL_SYNTHESIS_MAX_TOKENS` with `NEWS_STORY_DRAFTING_MAX_TOKENS`
     - `NEWS_TITLE_GENERATION_MAX_TOKENS`
     - Existing Article Summarization sampling suffixes such as `NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE`
  7. Replace old Story Drafting tuning env names with `NEWS_MODEL_STORY_DRAFTING_*`, for example `NEWS_MODEL_STORY_DRAFTING_TEMPERATURE`. Do not support or document old `NEWS_MODEL_FINAL_SYNTHESIS_*` env names.
  8. Adjust `build_chat_model` support in config data so unset sampling fields can be omitted later. Do not pass placeholder zeros just to preserve old profile behavior.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - Expected test additions:
    - A temp `model_tuning_presets.yaml` preset applies to one matching model-task pair.
    - A mismatched preset raises `ValueError`.
    - An explicit env var overrides a preset value.
    - With no verified model default and no preset/env override, sampling fields remain unset in the resolved tuning object.
- Stop if:
  - `ChatOpenAI` cannot be constructed when `temperature` is omitted. If so, record evidence and use the smallest neutral fallback that preserves backend defaults as much as the library allows.
  - Model-card guidance cannot be verified but product expectations require retaining old sampling values.
- Completion note:
  - Added the tuning precedence layer in `news_pipeline/config.py`, including preset selection env vars, preset scope validation, env override precedence, and no verified model-specific sampling defaults. Added focused resolution tests for preset success, preset mismatch, and unset sampling behavior. Validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution` and the broader `tests.test_runtime_config_resolution tests.test_gemma4_article_budget` smoke.

### Step 5: Separate Pipeline Budget And Model Server Settings

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - `build_model_server_command`, `_default_total_article_summary_cap`, concurrency defaults, runtime knob registry
  - `news_pipeline/pipeline.py` - model server log paths and preflight helpers
  - `tests/test_runtime_config_resolution.py` - model server command assertions
- Files to edit/create:
  - `news_pipeline/config.py` - resolve explicit Pipeline Budget and Model Server Settings
  - `tests/test_runtime_config_resolution.py` - add server settings and budget tests
- Instructions:
  1. Move article caps and article text truncation out of model tuning/profile behavior into `PipelineBudget`. Keep env overrides:
     - `NEWS_ARTICLE_TEXT_TOKEN_LIMIT`
     - `NEWS_TOTAL_ARTICLE_SUMMARY_CAP`
     - existing story budget env vars already in `RuntimeConfig`.
  2. Move server command knobs out of model profiles into `ModelServerSettings`. Add explicit env vars:
     - `NEWS_MODEL_SERVER_PREFILL_STEP_SIZE`
     - `NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE`
     - `NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES`
     - `NEWS_MODEL_SERVER_MAX_TOKENS`
  3. Add task server setting aliases only for base URLs at first:
     - `NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`
     - `NEWS_MODEL_STORY_DRAFTING_BASE_URL`
     If full per-task server CLI knobs look necessary, stop and consult before expanding the surface.
  4. Update `build_model_server_command` to accept optional server settings and include `mlx` command flags only when the corresponding value is set. Keep required model, host, port, concurrency, and log-level behavior.
  5. If `NEWS_MODEL_BASE_URL` has a non-8080 port, either derive the server command port from it with `urllib.parse.urlparse` or leave the existing hardcoded `8080` and write a Blocker Note. Do not silently document a false command.
  6. Add tests:
     - `NEWS_MODEL` change does not alter server settings.
     - Explicit `NEWS_MODEL_SERVER_MAX_TOKENS` appears in `model_server_command`.
     - Explicit `NEWS_TOTAL_ARTICLE_SUMMARY_CAP` still wins.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`
  - Expected result: server settings and budget tests pass; model-specific profile keys are not used.
- Stop if:
  - Existing MLX server commands require profile-derived values and fail without them.
  - Deriving port from base URL changes behavior for existing documented commands.
- Completion note:
  - Confirmed the separated `PipelineBudget` and `ModelServerSettings` resolution path in `news_pipeline/config.py`, including port derivation from `NEWS_MODEL_BASE_URL` and explicit server max-token support. Added focused regression coverage that model selection does not change server settings and that `NEWS_MODEL_SERVER_MAX_TOKENS` reaches the generated command. Validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`.

### Step 6: Wire Pipeline Calls To Task Assignments

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/pipeline.py` - global config constants, `_compat_runtime_values`, `build_chat_model`, managed server functions, diagnostics settings, report filename
  - `news_pipeline/article_summarization.py` - `task="article_summary"`
  - `news_pipeline/story_drafting.py` - `task="final_synthesis"`
  - `news_pipeline/story_selection.py` - `model_profile_key`
- Files to edit/create:
  - `news_pipeline/pipeline.py` - consume assignments/tuning and remove active profile globals
  - `news_pipeline/story_selection.py` - rename/remove profile key field
  - Tests touched by pipeline global names - update only where needed
- Instructions:
  1. Replace active globals derived from `MODEL_PROFILE` with resolved config objects:
     - `MODEL_ASSIGNMENTS = CONFIG.model_assignments`
     - `PIPELINE_BUDGET = CONFIG.pipeline_budget`
     - `MODEL_SERVER_SETTINGS = CONFIG.model_server_settings`
     - `MODEL_TUNING = CONFIG.model_tuning` or assignment-level tuning from `TaskModelAssignment`
  2. Do not keep old names for model-profile or `final_synthesis` concepts. If a generic module global is still needed by current pipeline free functions, name it with current vocabulary.
  3. Update max-token constants:
     - `ARTICLE_SUMMARY_MAX_TOKENS` from the Article Summarization assignment tuning.
     - `STORY_DRAFTING_MAX_TOKENS` from the Story Drafting assignment tuning.
     - `MODEL_MAX_INPUT_TOKENS` from the relevant assignment/default tuning used by story selection.
  4. Update `build_chat_model(max_tokens, task="default")`:
     - Use `task="story_drafting"` for Story Drafting. Remove old `final_synthesis` task handling.
     - Select base URL, model name, backend, and sampling from the normalized task assignment.
     - Build `ChatOpenAI` kwargs so unset sampling fields are not passed.
  5. Add a small helper in `pipeline.py` or `config.py` to describe whether task models require external servers. Minimum behavior:
     - If all assignments use the same resolved model and same base URL, existing managed server flow may start one server.
     - If assignments use different models but the same inherited base URL under managed server mode, raise a clear `RuntimeError` before starting Article Summarization.
     - If assignments use different base URLs, treat them as external and do not try to manage multiple local servers in this plan.
  6. Update preflight/probe functions only as much as needed for the default managed server. Do not rewrite the managed server lifecycle broadly.
  7. Update diagnostics settings emitted near `pipeline.py` run diagnostics:
     - Replace `model_profile` with `model_assignments`, `model_tuning`, `pipeline_budget`, and `model_server_settings` entries.
     - Keep `model` and `model_name` as default model summary fields.
  8. Replace report filenames using `MODEL_PROFILE_KEY` with a sanitized model label based on `MODEL_REFERENCE` or the Story Drafting assignment. Keep the filename deterministic and filesystem-safe.
  9. In `news_pipeline/story_drafting.py`, change `task="final_synthesis"` to `task="story_drafting"`.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references`
  - `rg -n "MODEL_PROFILE|MODEL_PROFILE_KEY|model_profile_key|Legacy model profile|big_conservative|tiny_codex|gemma_12b_optiq" news_pipeline tests`
  - Expected result: no active pipeline behavior depends on profile keys and no live code/test/config path mentions old profile keys or `final_synthesis` model-tuning names.
- Stop if:
  - Supporting separate task models requires a broad multi-server rewrite.
  - Existing pipeline tests require `MODEL_PROFILE_KEY` for behavior rather than only labels/fixtures.
- Completion note:
  - Wired the pipeline to explicit task assignments, removed active profile globals, renamed Story Drafting task plumbing, and validated the pipeline model-call surface with the focused unittest suite.

### Step 7: Update UI Sections And Runtime Snapshot

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/config.py` - `runtime_knob_registry`
  - `news_pipeline/ui.py` - `_runtime_snapshot`, `schema_payload`, `renderKnobs`, tabs, preset editor
  - `README.md` - UI description
- Files to edit/create:
  - `news_pipeline/config.py` - knob groups and labels
  - `news_pipeline/ui.py` - section rendering and runtime snapshot labels
  - `tests/test_runtime_config_resolution.py` - UI preview/schema assertions
- Instructions:
  1. Update `runtime_knob_registry()` groups so knobs appear under these user-facing groups:
     - `Run Settings`: source scope, recipient scope, URL reuse, image enabled, and other general run toggles.
     - `Model Selection`: `NEWS_MODEL`, `NEWS_MODEL_ARTICLE_SUMMARY`, `NEWS_MODEL_STORY_DRAFTING`.
     - `Model Tuning`: tuning preset selects, max-token tuning, and sampling controls.
     - `Pipeline Budget`: article text cap, article summary cap, recent window, source/article/story limits, story thresholds, concurrency where exposed.
     - `Model Server Settings`: base URLs and explicit server command knobs.
     - Keep `Run Presets` as the existing separate tab/editor.
  2. Mark advanced Model Tuning knobs with `advanced=True`, especially sampling controls. Keep basic tuning controls visible: preset selects and task max tokens.
  3. In `ui.py`, replace the global "Advanced" checkbox behavior for Model Tuning with native `<details>` for advanced tuning inside the `Model Tuning` group. A global show-advanced checkbox may remain for other advanced groups if that is the smallest diff, but Model Tuning must be collapsible.
  4. Keep the UI functional and simple. Do not build a full model tuning preset CRUD editor unless it is very small. It is acceptable for this plan to make presets user-owned through `config/model_tuning_presets.yaml` and expose only select controls in the UI.
  5. Update `_runtime_snapshot()`:
     - Replace `"profile": ...` with `"assignments"`, `"tuning"`, `"pipeline_budget"`, and `"server_settings"`.
     - Show Article Summarization and Story Drafting model refs/names/base URLs separately.
  6. Update dashboard stats so "Model" remains the default model and add compact task-model stats if space permits. Do not make the dashboard noisy.
  7. Add tests that `schema_payload()` or `preview_payload()` includes the new knob groups and no `"profile"` key in the active model snapshot.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`
  - Optional manual smoke if changing HTML heavily: `UV_CACHE_DIR=.uv-cache uv run news ui --host 127.0.0.1 --port 8766` and open `http://127.0.0.1:8766`.
  - Expected result: schema/preview tests pass and the page still renders grouped settings.
- Stop if:
  - UI changes require a separate frontend rewrite.
  - Existing Run Preset save/load breaks when applying selected presets to knobs.
- Completion note:
  - Reworked the knob registry into Run Settings, Model Selection, Model Tuning, Pipeline Budget, and Model Server Settings; added task-model snapshot fields; and verified the UI/config snapshot with `tests.test_runtime_config_resolution`.

### Step 8: Update Diagnostics, History, And Documentation

- Status: `[x] Complete`
- Context to read:
  - `news_pipeline/diagnostics.py` - run review and details Markdown model sections
  - `news_pipeline/history_store.py` - `_insert_run`, schema, tests
  - `README.md` - Run Settings and Model docs
  - `CONTEXT.md` and ADR 0007 - vocabulary consistency
- Files to edit/create:
  - `news_pipeline/diagnostics.py` - user-visible model config labels
  - `news_pipeline/history_store.py` - only if required by tests or settings serialization
  - `README.md` - document new config model
  - `CONTEXT.md` - edit only if implementation names need clarification
  - `tests/test_history_store.py` - update fixtures/assertions
- Instructions:
  1. In `diagnostics.py`, replace "Legacy model profile" with separate rows or bullets:
     - Default model
     - Article Summarization model
     - Story Drafting model
     - Model backend(s)
     - Model input cap / output caps if configured
     - Pipeline Budget summary
  2. In details Markdown, remove `({model_profile})` from the model line and add task assignments/tuning summaries from `settings`.
  3. In `history_store.py`, remove `model_profile` from the current `CREATE TABLE IF NOT EXISTS runs` schema and from `_insert_run`. Do not add a replacement column unless tests show it is necessary; ensure `settings_json` contains `model_assignments`, `model_tuning`, `pipeline_budget`, and `model_server_settings`.
  4. Update `tests/test_history_store.py` fixtures to use the new settings keys. Keep one assertion that run review Markdown contains `## Run Settings`.
  5. Update `README.md`:
     - `NEWS_MODEL` means default model selection only.
     - `NEWS_MODEL_ARTICLE_SUMMARY` and `NEWS_MODEL_STORY_DRAFTING` assign task models.
     - `config/model_tuning_presets.yaml` stores Model Tuning Presets.
     - Precedence: backend/model defaults, model-specific defaults with verified guidance, user preset, explicit env override.
     - Separate model runs require matching task base URLs or externally managed servers unless the implementation adds multi-server management.
     - Run Presets and Model Tuning Presets are different.
  6. Do not modify ADR 0007 unless the implementation deliberately chooses vocabulary different from the ADR. Historical ADR text may describe removed legacy behavior; live code/docs should not.
- Validation:
  - `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_history_store tests.test_runtime_config_resolution`
  - `rg -n "Legacy model profile|model runtime profile|hidden bundle|Today this also selects" README.md news_pipeline tests`
  - Expected result: user-facing docs, diagnostics, tests, and config files no longer mention model-profile inference or old `final_synthesis` tuning names.
- Stop if:
  - Removing `model_profile` from current settings breaks history export or existing database reads.
  - Documentation requires a product decision about managed multi-server behavior.
- Completion note:
  - Confirmed the diagnostics and history store already use the new model vocabulary, removed the legacy README profile wording, and validated the docs/history surface with the focused test suite and legacy-string search.

### Step 9: Final Focused Validation And Cleanup

- Status: `[x] Complete`
- Context to read:
  - `pyproject.toml` - test command shape
  - Files changed in previous steps
  - `README.md` - final public behavior
- Files to edit/create:
  - Any changed test/source/doc file - only for fixing failures caused by this plan
  - This plan file - final progress/checklist updates
- Instructions:
  1. Run the focused suite:
     `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references`
  2. Run a CLI config smoke:
     `NEWS_CODEX_TESTING=1 UV_CACHE_DIR=.uv-cache uv run news codex-model-server-command`
  3. Run the final search:
     `rg -n "ModelRuntimeProfile|infer_model_profile_key|configured_model_profile|MODEL_RUNTIME_PROFILES|MODEL_PROFILE_KEY|Legacy model profile|model_profile|big_conservative|tiny_codex|gemma_12b_optiq|final_synthesis|NEWS_FINAL_SYNTHESIS|NEWS_MODEL_FINAL_SYNTHESIS" news_pipeline tests README.md config`
  4. For any remaining match, classify it:
     - OK: no matches in the searched live paths.
     - Not OK: any match in live source, tests, README, or config. ADR history is outside this search and may remain historical context.
  5. Update this plan's Final Review Checklist and Progress Log.
- Validation:
  - Commands above.
  - Expected result: focused tests pass, CLI prints a Codex-safe server command, and no active runtime behavior mentions legacy profiles.
- Stop if:
  - Any validation failure cannot be traced to the planned changes.
  - The final search finds active profile inference still reachable from `load_runtime_config()`.
- Completion note:
  - Ran the final focused unittest suite, the Codex-safe `news codex-model-server-command` smoke, and the legacy-string search. No active profile inference remains in live source, tests, README, or config.

## Validation Plan

Run these after the relevant steps, not before implementing the plan:

| Command | When to run | Expected result |
| --- | --- | --- |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution` | After Steps 2, 4, and 7 | Runtime config and UI preview tests pass |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget` | After Steps 3 and 5 | Model selection no longer changes Pipeline Budget; explicit overrides still win |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references` | After Step 6 | Pipeline model-call plumbing and story drafting still pass |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_history_store tests.test_runtime_config_resolution` | After Step 8 | Diagnostics/history docs and config tests pass |
| `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references` | Final | Focused suite passes |
| `NEWS_CODEX_TESTING=1 UV_CACHE_DIR=.uv-cache uv run news codex-model-server-command` | Final | Prints a server command for the Codex-safe model without profile inference |
| `rg -n "ModelRuntimeProfile|infer_model_profile_key|configured_model_profile|MODEL_RUNTIME_PROFILES|MODEL_PROFILE_KEY|Legacy model profile|model_profile|big_conservative|tiny_codex|gemma_12b_optiq|final_synthesis|NEWS_FINAL_SYNTHESIS|NEWS_MODEL_FINAL_SYNTHESIS" news_pipeline tests README.md config` | Final | No matches in live source, tests, README, or config |

## Final Review Checklist

- [x] All planned files were updated or explicitly deferred with a Consultation Note, step completion note, or active Blocker Note.
- [x] Every completed step has a Progress Log entry.
- [x] Validation commands were run and results recorded.
- [x] `NEWS_MODEL` resolves only model identity, not Model Tuning, Pipeline Budget, or Model Server Settings.
- [x] Article Summarization and Story Drafting have explicit task model assignments.
- [x] Model Tuning Presets are user-owned and can target one model or one model-task pair.
- [x] Model-specific defaults exist only with verified model-specific guidance.
- [x] No live code, tests, README, or config mention legacy model profiles, old profile keys, `model_profile`, or old `final_synthesis` tuning names.
- [x] UI grouping separates Run Settings, Run Presets, Model Selection, Model Tuning, Pipeline Budget, and Model Server Settings.
- [x] Advanced Model Tuning is collapsible.
- [x] No unrelated files were changed.

## Progress Log

| Time | Agent | Step | Status | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-21 18:18 | Codex | Plan creation | Complete | Created this precise plan after reading ADR 0007, config, pipeline, UI, diagnostics, history, README, run presets, and focused tests. |
| 2026-06-21 19:04 | Codex | Step 1 | Complete | Searched for legacy profile coupling in live code/tests and ran `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store` successfully under escalated execution. |
| 2026-06-21 19:04 | Codex | Step 2 | Complete | Added explicit config value objects, `load_model_tuning_presets()`, the empty `config/model_tuning_presets.yaml` seed file, and a focused loader test. Validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution`. |
| 2026-06-21 19:04 | Codex | Step 3 | Complete | Replaced legacy profile inference with explicit model assignments, a generic pipeline budget, and model-server settings. Updated the pipeline/UI snapshot path and validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget` plus the required grep check. |
| 2026-06-21 19:38 | Codex | Step 4 | Complete | Added model tuning preset resolution, explicit preset selection env vars, scope validation, and env override precedence in `news_pipeline/config.py`. Extended `tests/test_runtime_config_resolution.py` with preset success, mismatch, and unset-sampling checks. Validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution` and `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`. |
| 2026-06-21 19:40 | Codex | Step 5 | Complete | Separated pipeline budget and model server settings resolution in `news_pipeline/config.py`, confirmed base-url port derivation, and added regression tests for server settings independence and explicit server max tokens. Validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget`. |
| 2026-06-21 19:56 | Codex | Step 6 | Complete | Verified the explicit task-assignment pipeline refactor in `news_pipeline/pipeline.py`, `news_pipeline/story_drafting.py`, and `news_pipeline/story_selection.py` against `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references` and the broader focused suite. |
| 2026-06-21 19:56 | Codex | Step 7 | Complete | Grouped the UI knobs into the new Run Settings, Model Selection, Model Tuning, Pipeline Budget, and Model Server Settings sections; surfaced task-specific snapshot fields; and validated with `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution` plus the broader focused suite. |
| 2026-06-21 19:56 | Codex | Step 8 | Complete | Refreshed the README to document default-model selection, task-model assignments, tuning presets, pipeline budget, and server settings. Confirmed the diagnostics/history changes already in the workspace via the focused suite and legacy-string sweep. |
| 2026-06-21 19:56 | Codex | Step 9 | Complete | Ran `UV_CACHE_DIR=.uv-cache uv run python -m unittest tests.test_runtime_config_resolution tests.test_gemma4_article_budget tests.test_history_store tests.test_story_drafting tests.test_topicless_global_pipeline tests.test_grouped_citation_references`, `NEWS_CODEX_TESTING=1 UV_CACHE_DIR=.uv-cache uv run news codex-model-server-command`, and the final `rg` sweep successfully. |

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
