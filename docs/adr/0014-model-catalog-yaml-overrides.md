# ADR 0014: Model Catalog YAML overrides

Status: Accepted

Date: 2026-08-13 (amended 2026-08-14: backend-scoped identity rules with the
llama.cpp file-qualified reference form, issue #75)

## Context

`news_pipeline/model_catalog.py` hard-codes the curated catalog entries, while
`news_pipeline/config.py` hard-codes the aliases consumed by runtime
resolution and UI selectors. The catalog is visible in the CLI/UI but cannot
be customized by a user; editing only one of those registries would create a
catalog/runtime mismatch. The built-in entries are code-reviewed contracts,
so the question was how to let a local operator add or reword catalog entries
without forking application code or weakening the reviewed identity rules
(ADR 0017 runtime matrix, issue #124 GGUF boundary).

The baseline decision this overlay extends — who owns the curated catalog
and its runtime-fit verdicts — is recorded in
[`docs/adr/0019-model-catalog-owns-curated-models-and-runtime-fit-verdicts.md`](0019-model-catalog-owns-curated-models-and-runtime-fit-verdicts.md).
This record therefore extends, rather than defines, catalog ownership.

## Decision

Add an optional, user-editable `config/model_catalog.yaml` overlay to the
Python Model Catalog. `news_pipeline/model_catalog.py` keeps the built-in
entries as the reviewed baseline (`BUILTIN_CATALOG_MODELS`) and merges a
validated YAML overlay into the existing per-process `CATALOG_MODELS`
snapshot, which remains the single registry consumed by the CLI, UI schema,
model selector, alias resolution, backend inference, and Hugging Face
runtime-fit annotations.

- The checked-in file is an empty, documented template (`models: {}`);
  missing files, `models: {}`, and YAML `null` payloads preserve the
  built-in catalog exactly.
- Existing aliases may override only `name`, `description`,
  `context_length`, and `task_notes` (task notes merge by task key).
  `reference`, `backend`, and `hf_repo` must be absent or match the built-in
  identity exactly.
- New aliases must provide `reference`, `name`, `backend`, `hf_repo`, and
  `description`; `context_length` is optional/null and `task_notes` defaults
  to `{}`. Aliases must be non-empty, trimmed, and match the safe lowercase
  pattern (letters, digits, `.`, `_`, `-`, starting with a letter or digit).
- Backends are limited to the closed set (`mlx-lm`, `mlx-vlm`, `external`,
  `llama.cpp`); identity rules are backend-scoped (issue #75):
  - `mlx-lm`, `mlx-vlm`, and `external` entries keep `reference == hf_repo`
    and reject file-qualified `.gguf` references, preserving the issue #92
    drift guard.
  - `llama.cpp` entries use a file-qualified `owner/repo/file.gguf`
    `reference` whose first two segments equal a bare `hf_repo` page id.
    The file name must be a safe, traversal-free `.gguf` name (validated by
    the stdlib-only adapter so catalog identity and launch parsing cannot
    disagree); `hf_repo` remains the repo id used by Hugging Face search.
  `reference == hf_repo` is required and file-qualified `.gguf` references
  are rejected, preserving the ADR 0017 runtime matrix and the issue #92
  drift guard.
- Unknown top-level keys, entry fields, and recommendation keys are errors.
  Malformed or unsafe YAML fails closed with a path-specific `ValueError`
  from every public catalog/config consumer; it never silently falls back to
  an empty catalog.
- `NEWS_MODEL_CATALOG_YAML` selects an alternate path; relative paths resolve
  from the repository root. The default path is `config/model_catalog.yaml`.
- The merged catalog is a per-process snapshot: it is loaded on first use and
  cached, so editing the YAML requires restarting the CLI or UI. Hot reload
  is out of scope; a mid-process reload would make selector options, aliases,
  and active runs disagree.
- `DEFAULT_CATALOG_MODEL_ALIAS` remains code-owned; a YAML addition is never
  silently made the default. YAML entries are user-verified, not
  Apple-Silicon verified by this project, and runtime-fit verdicts remain
  advisory.

The loader stays independent of `config` at module level (stdlib imports only;
`yaml` is imported lazily inside the loader) to preserve the existing import
cycle boundary, and `config.py` consumes the merged registry through
`custom_catalog_aliases()` and `catalog_model_backend()` helpers.

## Consequences

- Easier: a local operator can reword catalog metadata and add a verified HF
  entry in YAML; the entry then appears in `news models catalog`, UI
  schema/cards, selector options, recommendations, alias resolution, backend
  inference, and runtime-fit matching without Python changes.
- Safer: identity fields, backends, and repo shapes stay validated; the
  legacy Qwythos GGUF aliases are curated built-ins again (issue #75) and a
  custom alias/reference colliding with a built-in identity fails closed
  before it can become a selector option.
- Harder/off-limits: no UI editor, CRUD endpoint, or persistence API for the
  YAML file; no live Hugging Face verification or hardware-fit guarantee for
  user entries; no multimodal GGUF or per-model chat-template editing; no
  hot reload; the catalog stays a per-process snapshot.

## Alternatives considered

- Replace Python built-ins with YAML — rejected: built-ins remain
  code-reviewed contracts and the empty/default behavior must stay stable.
- Let only the UI or CLI read YAML — rejected: alias resolution, selectors,
  presets, and model-server startup would disagree with the catalog.
- Allow arbitrary file-qualified/GGUF references — rejected before issue
  #75 by ADR 0017 and the `reference == hf_repo` invariant; now permitted
  only for `llama.cpp` entries with the strict `owner/repo/file.gguf` form
  under a bare `hf_repo`.
- Add a model-registration database/API — rejected: this is a local,
  offline-first configuration feature and existing config inputs are YAML.
- Hot reload the catalog — rejected: Runtime Config is a per-process
  snapshot and hot reload would make selector options, aliases, and active
  runs disagree.
