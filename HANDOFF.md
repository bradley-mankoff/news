## Goal
Reframe the Daily News Pipeline as a local-first open-source project where users control editorial prompts, Hugging Face models, sources, translation, and delivery. Replace the current knob-heavy primary UI with a prompt-first guided interface while moving model tuning, pipeline budgets, server settings, and raw runtime controls behind an Advanced Settings gate.

The handoff should be saved as `HANDOFF.md` at the repository root: `/Users/home/news/HANDOFF.md`. Do not place it in a hidden directory.

## Constraints & Preferences
- The normal UI must prioritize user-customizable prompts.
- Expose the project’s default prompts and provide “restore default” and comparison behavior.
- Users should be able to customize every meaningful LLM pipeline stage.
- Ship suggested prompt profiles:
  - Consensus/convergence juxtaposed with real contradictions among stories
  - Playful tone
  - Facts only
  - Explain like I’m five
- Users should be able to choose models, including through Hugging Face integration.
- Provide task-specific model recommendations.
- Delegate hardware-fit estimation to Hugging Face’s native Hardware Compatibility feature rather than rebuilding it.
- Reintroduce translation only for explicitly known non-English inputs. Do not detect language from article text or scripts.
- Avoid over-engineering. Keep machine-required output contracts separate from ordinary editorial customization.
- Preserve reproducibility by recording resolved prompts, model assignments, and translation policy in each Run Session.
- Open sourcing does not remove scraping, copyright, terms-of-service, or model-license concerns. Defaults and UI should retain citations and surface model licenses.
- Repository policy: correctness first, reuse existing conventions, avoid unnecessary abstractions, and do not create redundant documentation.
- No implementation was requested or performed in the prior session; the work completed was repository-grounded product and architecture analysis.

## Progress
### Done
- [x] Mapped current project semantics from `CONTEXT.md`, operational behavior from `README.md`, settings from `SETTINGS.md`, and architecture decisions from `docs/adr/`.
- [x] Confirmed GitNexus index state:
  - Repository: `/Users/home/news`
  - Branch: `main`
  - Indexed/current commit: `06ef7b0`
  - Status: up to date
- [x] Identified five current LLM prompt stages:
  - Article summarization: `news_pipeline/article_summarization.py`, `build_article_summary_prompt_messages`, lines 47–100
  - Story scale screening: `news_pipeline/story_selection.py`, `_global_scale_screening_prompt_messages`, lines 241–325
  - Story drafting: `news_pipeline/story_drafting.py`, `build_story_synthesis_prompt_messages`, lines 147–227
  - Report title generation: `news_pipeline/pipeline.py`, `generate_report_title`, lines 2488–2512
  - Image art direction: `news_pipeline/pipeline.py`, `generate_image_art_brief`, around lines 3238–3288
- [x] Confirmed current prompt contracts:
  - Article summarization requires a `DATABASE_ENTRY:` Markdown block and retries with `ARTICLE_SUMMARY_FORMAT_ERROR_MESSAGE`.
  - Story drafting requires `Headline:`, `Main story:`, `Contradictions:`, and sentence-end `[[S1]]` citation markers.
  - Story title generation requires one plain title line.
  - Image art direction requires strict JSON with `image_prompt` and `overlay_headline`.
- [x] Confirmed current story-drafting prompt already contains a strong contradiction definition at `news_pipeline/story_drafting.py:198-207`:
  - Contradiction means incompatible factual claims about the same count, timeline, attribution, status, quote, or outcome.
  - Omission, different focus, or updates over time are not contradictions.
- [x] Confirmed current model configuration:
  - `TaskModelAssignment` is defined in `news_pipeline/config.py:189-197`.
  - Only article summarization and story drafting receive explicit assignments in `_configured_model_assignments`, around `news_pipeline/config.py:390-462`.
  - `MODEL_TASK_SAMPLING_ENV_PREFIXES` also lists `story_discovery`, `story_scale_screening`, and `title_generation` at `news_pipeline/config.py:313-319`.
  - Image art direction currently calls `build_chat_model(..., task="title_generation")`.
  - Current aliases and defaults are in `news_pipeline/config.py:64-110`.
- [x] Confirmed current UI structure:
  - Guided run setup starts at `news_pipeline/ui.py:1199`.
  - Separate Advanced Settings view starts at `news_pipeline/ui.py:1202`.
  - Article and story model cards and collapsible tuning sections are rendered around `news_pipeline/ui.py:1713-1762`.
  - Advanced raw settings are rendered by `renderAdvancedKnobs` around `news_pipeline/ui.py:2087`.
- [x] Reviewed ADR 0007, `docs/adr/0007-model-configuration-vocabulary.md`.
  - It already decides that model identity, Model Tuning, Pipeline Budget, and Model Server Settings are separate concepts.
  - It explicitly recommends keeping advanced tuning collapsible.
  - Status is currently `Proposed`.
- [x] Reviewed translation history around commit `06ef7b0`.
  - The removed implementation had explicit fields such as `language`, `requires_translation`, and `translation_source_language`.
  - It also had incorrect automatic behavior through `_text_looks_non_english`, `_infer_script_translation_language`, `_article_translation_decision`, and source-catalog retagging.
  - `news_pipeline/source_catalog.py` previously defined `MarkTranslationRequired` and `_apply_translation`.
  - `news_pipeline/config.py` previously defined `DEFAULT_TRANSLATION_MODEL = "google/translategemma-4b-it"` and `NEWS_TRANSLATION_ENABLED`.
  - The translation removal deleted approximately 562 lines from `news_pipeline/pipeline.py`.
- [x] Confirmed current source-language policy:
  - Source catalog field ordering still includes `language` at `news_pipeline/source_catalog.py:14-31`.
  - `_source_enabled_for_scope` currently rejects every source whose language is not `en` at `news_pipeline/config.py:1383-1396`.
  - `load_sources` maps `language` into runtime source records at `news_pipeline/config.py:1419-1486`.
- [x] Researched official Hugging Face facilities:
  - Hub search through `HfApi.list_models`: https://huggingface.co/docs/huggingface_hub/guides/search
  - Native hardware compatibility for saved GPU/CPU/Apple Silicon hardware and GGUF/MLX quantizations: https://huggingface.co/docs/hub/hardware
- [x] Confirmed `huggingface-hub` appears transitively in `uv.lock` but is not a direct dependency in `pyproject.toml`.
- [x] Audited immediate open-source blockers:
  - No `LICENSE`, `LICENSE.md`, `LICENSE.txt`, or `COPYING` was found — resolved: Apache-2.0 `LICENSE` added (ADR 0010).
  - `pyproject.toml:2` previously used the private folder name; renamed to `news-pipeline` (ADR 0009).
  - `README.md` quickstart previously referenced the private path; now clone-based (ADR 0009).
  - `config/recipients.yaml` contains real names and email addresses.
  - `news_pipeline/config.py:1637` defaults to `bradley@mankoff.com`.
  - `news_pipeline/config.py:1648` defaults to `bradley.mankoff@gmail.com`.
  - `news_pipeline/ui.py:58` includes Bradley-specific recipient guidance.
- [x] Produced a repository-grounded architecture and rollout proposal in the prior response.
- [x] No source files, tests, configuration, or documentation were modified.
- [x] No tests were run because there was no behavioral implementation.
- [x] Saved this handoff as top-level `HANDOFF.md`.

### In Progress
- [ ] None.

### Pending
- [x] Decide the public project and package name (`news-pipeline`, ADR 0009).
- [x] Choose a project license. Apache-2.0 chosen by owner on issue #21 (ADR 0010); AGPL-3.0 rejected (network copyleft).
- [ ] Audit repository history for secrets and personal data before making the repository public.
- [ ] Replace real recipients, personal email defaults, and personal filesystem paths with safe examples.
- [x] Decide the initially supported runtime matrix (ADR 0010, Accepted): MLX/MLX-VLM on Apple Silicon + external OpenAI-compatible endpoints; `llama.cpp` adapter deferred.
- [ ] Implement a Prompt Catalog and built-in Prompt Profiles.
- [ ] Refactor hard-coded prompt construction to compose editable editorial instructions with pipeline-owned protocol requirements.
- [ ] Add prompt-profile selection and per-stage prompt editing to the normal UI.
- [ ] Move all tuning, budgets, clustering thresholds, server controls, and raw environment overrides behind Advanced Settings.
- [ ] Add task-specific model assignments for every actual LLM stage or explicitly document inheritance.
- [ ] Add a curated Model Catalog plus Hugging Face search/model metadata integration.
- [x] Add `huggingface-hub` as a direct dependency (declared in `pyproject.toml`; API usage is tracked under the Model Catalog item above).
- [ ] Add direct links from model choices to Hugging Face model pages and native hardware compatibility.
- [ ] Reintroduce explicit translation without content-based language detection.
- [ ] Record exact prompt/model/translation snapshots in Run Session history.
- [ ] Update or supersede ADR 0007 after the product and configuration decisions are accepted.
- [ ] Run focused tests and an end-to-end UI smoke test after implementation.

## Key Decisions
- **Prompt-first product**: The project should be positioned as user-controlled editorial synthesis rather than a private operator control panel.
- **Prompt Profiles are bundles**: A profile should provide task-specific instructions for article summarization, scale screening, story drafting, title generation, and image art direction rather than one global text blob.
- **Balanced is the default profile**: Preserve current behavior as the visible default and allow restoration at any time.
- **Consensus/contradiction becomes a first-class profile**: Reuse the existing precise contradiction semantics from `build_story_synthesis_prompt_messages`.
- **Editorial instructions and machine contracts remain separate**: Normal users edit editorial intent; the pipeline continues to own parsable formats, citation markers, retry corrections, source payloads, and strict JSON requirements.
- **Full-template editing belongs in Advanced Settings**: Advanced users may replace complete templates, but required placeholders and output-contract compatibility must be validated before saving.
- **Prompt themes vary by task**: For example, Playful should affect story prose and titles but should not weaken factual extraction or screening.
- **Every Run Session records resolved configuration**: Persist profile ID, exact rendered prompt content or immutable prompt snapshots, model repository/revision per task, tuning, and translation policy.
- **Model recommendations are task-specific and curated**: Recommend models based on factual extraction, structured output, synthesis, citation fidelity, speed, context length, or translation support—not parameter count or popularity alone.
- **Do not recreate Hugging Face hardware fitting**: Link users to the native Hugging Face Hardware Compatibility panel for GGUF and MLX quantizations.
- **Model picker must validate runtime support**: Do not let users select arbitrary Hugging Face repositories that the configured backend cannot launch.
- **Translation is deterministic and explicit**: Translate only when translation is enabled, the source language is declared, and it differs from the target language.
- **No automatic language detection**: Do not inspect Unicode scripts, guess languages from article content, retag source YAML, or mutate the Source Catalog.
- **Unknown language does not trigger translation**: Surface a configuration status/error rather than guessing.
- **Preserve translation provenance**: Keep original and translated title/text plus source language, target language, model, and status.
- **Open source is not legal immunity**: Preserve citations and source URLs, expose model licenses, avoid bundling scraped article bodies in public examples, and treat source/model compliance as explicit user-facing concerns.
- **Initial runtime scope should be honest**: A practical first release can support Apple Silicon managed models plus external OpenAI-compatible endpoints; managed cross-platform GGUF can follow through a real `llama.cpp` adapter.

## Critical Context
- Repository root: `/Users/home/news`
- Intended visible handoff path: `/Users/home/news/HANDOFF.md`
- Current branch/commit observed: `main` at `06ef7b0`
- GitNexus index was current at `06ef7b0`.
- Commands executed during discovery:
  - `gitnexus status`
  - `git log --all --date=short --format=%h%x09%ad%x09%s --regexp-ignore-case --grep=translat`
  - `git log --all --date=short --format=%h%x09%ad%x09%s -G "requires_translation|TRANSLATION_MODEL|translate_non_english|translation_enabled" -- news_pipeline config tests`
  - `git show --stat --oneline 06ef7b0`
  - `git diff --unified=8 06ef7b0^ 06ef7b0 -- news_pipeline/source_catalog.py`
  - `git diff --unified=6 06ef7b0^ 06ef7b0 -- news_pipeline/config.py`
  - `git diff --unified=6 06ef7b0^ 06ef7b0 -- news_pipeline/pipeline.py`
- Relevant current types and symbols:
  - `ModelSamplingSettings` — `news_pipeline/config.py:147`
  - `ModelTuningSettings` — `news_pipeline/config.py:157`
  - `PipelineBudget` — `news_pipeline/config.py:166`
  - `ModelServerSettings` — `news_pipeline/config.py:180`
  - `TaskModelAssignment` — `news_pipeline/config.py:189`
  - `RuntimeConfig` — `news_pipeline/config.py:200`
  - `_configured_model_assignments` — around `news_pipeline/config.py:380-462`
  - `build_article_summary_prompt_messages`
  - `_global_scale_screening_prompt_messages`
  - `build_story_synthesis_prompt_messages`
  - `generate_report_title`
  - `generate_image_art_brief`
  - `_source_enabled_for_scope`
  - `load_sources`
- Proposed Prompt Profile shape:

```yaml
id: consensus-and-contradiction
name: Consensus and Contradiction
prompts:
  article_summary: |
    Preserve concrete claims, uncertainty, and facts that can be compared
    against other reporting.
  story_scale_screening: |
    Prefer developments independently reported across regions and outlets.
  story_drafting: |
    Highlight where sources converge. Separately identify direct factual
    disagreements about the same claim.
  title_generation: |
    Prefer a title expressing the day's central shared development.
  image_art_direction: |
    Depict the central event without sensationalism.
```

- Proposed translation rule:

```text
translation enabled
AND source.language is declared
AND source.language != target_language
→ translate

missing or unknown source.language
→ do not translate; surface configuration status

source.language == target_language
→ do not translate
```

- Proposed normal UI:
  1. Sources and recipients
  2. Editorial approach / Prompt Profile
  3. Per-stage prompt editors with visible defaults
  4. Default model plus optional task-specific assignments
  5. Explicit translation controls
  6. Preview and run
- Proposed Advanced Settings:
  - Sampling
  - Token budgets
  - Concurrency
  - Clustering thresholds
  - Base URLs and model server flags
  - Failure/fallback controls
  - Full prompt templates
  - Raw environment overrides
- Suggested recommendation criteria:
  - Article summarization: factual extraction, instruction following, context length
  - Story scale screening: speed, conservative classification, structured-output reliability
  - Story drafting: multi-document synthesis, citation fidelity, long context
  - Title generation: small and fast
  - Image art direction: strict JSON reliability
  - Translation: known language-pair support and translation quality
- Tool/research failures observed:
  - First GitNexus query failed because multiple repositories were indexed: `archon-harness, client_config, data, oh-my-pi, news, tura`. Retried with `repo: "news"` successfully.
  - A broad license-file glob timed out and explicitly reported an incomplete scan. Direct reads then confirmed `LICENSE`, `LICENSE.md`, `LICENSE.txt`, and `COPYING` were absent.
  - A parallel read attempting missing license files aborted on `Path 'LICENSE' not found`; alternate filenames were then checked individually.
- No test failures exist because no tests were run.
- No partial edits or working-tree mutations were made.

## Next Steps
1. Ask the user only for the materially necessary product decisions that tools cannot determine:
   - Apache-2.0 versus AGPL-3.0
   - Initial supported runtimes/platforms
2. Before any public release, sanitize personal paths, recipient data, email defaults, package metadata, and repository history.
3. Produce one implementation plan covering Prompt Catalog extraction, UI simplification, Model Catalog/Hugging Face integration, deterministic translation, run-history snapshots, tests, and browser smoke verification.
4. Implement the Prompt Catalog first because the UI, model recommendations, and reproducibility records all depend on stable task identifiers and prompt interfaces.
5. Refactor one prompt stage at a time while preserving existing output contracts and focused tests.
6. Add the simplified normal UI after prompt interfaces are stable; keep existing advanced controls available but gated.
7. Add Hugging Face search and curated recommendations only after supported runtime validation is explicit.
8. Reintroduce translation last, using declared source language only and verifying that non-English sources remain excluded unless translation is explicitly enabled.
9. Run focused contract tests followed by `uv run news ui --open` and exercise prompt selection, model selection, translation gating, preview, and an end-to-end local run.
