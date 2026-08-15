# ADR 0019: Model Catalog owns curated models and runtime-fit verdicts

Status: Accepted

Date: 2026-08-15

## Context

The Model Catalog feature (issue #30) shipped a code-owned baseline of curated
models that the pipeline's backends can actually launch, with per-task
recommendation metadata and conservative runtime-fit verdicts for Hugging
Face search results. The runtime contract is implemented by
`news_pipeline/model_catalog.py` (immutable `CatalogModel` records, the
`BUILTIN_CATALOG_MODELS` baseline, `list_model_catalog()` /
`recommend_models()` projections, `runtime_fit_for_hf_model()` classification,
and the `search_huggingface_models()` / `fetch_model_metadata()` network
boundary) and is consumed by the CLI commands `news models catalog` and
`news models search`, the UI schema/cards/model picker, and
`news_pipeline/config.py` alias/backend lookup.

The decision was extended but never baselined:
[`docs/adr/0014-model-catalog-yaml-overrides.md`](docs/adr/0014-model-catalog-yaml-overrides.md)
records only the later YAML-overlay extension of the catalog,
[`docs/adr/0017-runtime-matrix.md`](docs/adr/0017-runtime-matrix.md)
records the supported runtime matrix, and
[`docs/adr/0007-model-configuration-vocabulary.md`](docs/adr/0007-model-configuration-vocabulary.md)
records the model-configuration vocabulary. No
accepted ADR records *why* curated metadata and runtime-fit classification
belong to the Model Catalog or how those decisions relate. Without a baseline
record, a future contributor could treat Hugging Face metadata (popularity,
downloads, parameter count, tags, card data) or a backend heuristic as
project verification, add a second model registry without knowing the
existing drift guards, or overclaim hardware support from a `managed_*`
label.

## Decision

`news_pipeline/model_catalog.py` owns the Model Catalog. Each curated entry
is an immutable `CatalogModel` record (`alias`, `reference`, `name`,
`backend`, `hf_repo`, `context_length`, `description`, `task_notes`), and the
merged per-process `CATALOG_MODELS` snapshot — built-ins first, optional YAML
overlay entries merged on top (ADR 0014) — is the single registry consumed by
CLI listing, UI schema/cards, selector options, alias extension, backend
lookup, and Hugging Face runtime-fit classification.

### Curated baseline

`BUILTIN_CATALOG_MODELS` is the code-reviewed baseline, currently four
entries (the code remains authoritative for exact references):

- `gemma-4-12b-it-4bit` — `mlx-vlm`, the default model, 256K-token context
  (`262_144`); the verified structured-output/synthesis/citation-fidelity
  pick.
- `gemma-e2b-tiny` — `mlx-lm`, the Codex-safe speed/test model.
- `qwythos-9b-4bit` — `llama.cpp`, file-qualified GGUF reference served by
  the managed llama.cpp backend (issue #75).
- `qwythos-9b-8bit` — `llama.cpp`, file-qualified GGUF reference served by
  the managed llama.cpp backend (issue #75).

Curated entries carry deterministic per-task recommendation notes across the
fixed task vocabulary (factual extraction, structured output, synthesis,
citation fidelity, speed, context length, translation), not parameter count
or popularity; `recommend_models()` projects ordered picks and returns an
honest empty list for tasks with no verified pick (translation).

### Runtime-fit verdicts

Every Hugging Face search/metadata result is classified by
`runtime_fit_for_hf_model()` into exactly one status from the closed
vocabulary:

- `RUNTIME_FIT_MANAGED_MLX_LM` (`managed_mlx_lm`)
- `RUNTIME_FIT_MANAGED_MLX_VLM` (`managed_mlx_vlm`)
- `RUNTIME_FIT_MANAGED_LLAMA_CPP` (`managed_llama_cpp`)
- `RUNTIME_FIT_EXTERNAL_ONLY` (`external_only`)

Classification precedence and boundaries:

- Catalog entries win by exact match (issue #92): an exact alias or
  `hf_repo` equality against the merged snapshot is the anchored match. A
  bare org id or prefix sibling never matches. Built-in entries are
  project-verified for their declared backend; user YAML entries that
  declare a managed backend carry advisory verdicts.
- Generic Hugging Face results are classified from repository metadata
  heuristics: text-generation GGUF tags classify as `managed_llama_cpp`
  (multimodal GGUF stays `external_only` because mmproj files are not
  managed); MLX library/tags classify as managed MLX statuses;
  transformers + safetensors within the supported pipeline tags classify as
  managed MLX statuses; everything else is `external_only`.
- `external_only` is a verdict, not an assertion that the repository
  metadata is invalid: the repo may be perfectly usable through
  `NEWS_MODEL_BACKEND=external` with an OpenAI-compatible endpoint.

### Advisory boundary

Runtime-fit verdicts are conservative picker/configuration hints, not
hardware benchmarks or launch guarantees. A `managed_*` verdict does not
guarantee hardware fit, quality, safety, chat-template compatibility, or
successful launch on every operator machine. The picker links to each Hugging
Face model page's native hardware compatibility panel, and ADR 0017 owns the
meaning and limits of supported backends
([`docs/adr/0017-runtime-matrix.md`](docs/adr/0017-runtime-matrix.md)).
YAML-added entries are
user-verified, not Apple-Silicon/llama-server verified by this project, and
never silently become the default model.

### Catalog versus network surface

The curated catalog and its recommendations are offline-first:
`list_model_catalog()` and `recommend_models()` never touch the network.
Hugging Face `search_huggingface_models()` / `fetch_model_metadata()` are a
lazy network boundary that annotates results with the fit verdict and an
`in_catalog` flag; errors propagate to the CLI/UI, which own their error
envelopes. The catalog itself remains the offline source of truth.

### Integration seams

`custom_catalog_aliases()` and `catalog_model_backend()` are the integration
seams consumed by `news_pipeline/config.py`; `config.MODEL_ALIASES` remains
the built-in runtime compatibility map guarded by the existing exact-reference
drift guard in `tests/test_model_catalog.py`. The UI projects catalog,
recommendations, task labels, and fit labels from the same snapshot without
duplicate maps.

### Ownership boundaries

- [`docs/adr/0017-runtime-matrix.md`](docs/adr/0017-runtime-matrix.md)
  owns the supported runtime matrix and backend semantics; this record does
  not restate them.
- [`docs/adr/0014-model-catalog-yaml-overrides.md`](docs/adr/0014-model-catalog-yaml-overrides.md)
  owns the optional YAML overlay (validation, identity rules, and snapshot
  behavior); this record defines the baseline it extends.
- [`docs/adr/0007-model-configuration-vocabulary.md`](docs/adr/0007-model-configuration-vocabulary.md)
  owns model selection/tuning/budget/server vocabulary; this record does
  not reassign that vocabulary.
- This record accepts the current Model Catalog ownership; it does not
  introduce a second model-registration abstraction or a catalog
  database/API.

### Non-goals

- No hardware benchmarking, automatic Hugging Face verification/download/
  install, runtime fallback, catalog database/API, YAML editor, or hot
  reload.
- No moving built-in model data from Python to YAML (ADR 0014 keeps
  code-reviewed built-ins in Python).
- No new runtime error/logging code, dependencies, or behavior changes; no
  CLI/UI/config surface changes.
- No reassignment of prompt ownership (ADR 0011), model tuning/budget/server
  settings (ADR 0007), runtime matrix semantics (ADR 0017), or YAML overlay
  validation (ADR 0014).

## Consequences

- Future model additions have one durable ownership decision: curated
  entries, recommendations, and runtime-fit verdicts belong to the Model
  Catalog, so a contributor adding a model keeps catalog, runtime, CLI, UI,
  and config in one registry instead of forking a second abstraction.
- Operators can distinguish curated support (code-verified for the declared
  backend), advisory YAML entries, and external-only search results; a
  `managed_*` label cannot be mistaken for a hardware or launch guarantee.
- The catalog/runtime vocabulary is guarded: `tests/test_docs_consistency.py`
  pins the accepted status, required vocabulary, cross-links, and unique
  numbering, while `tests/test_model_catalog.py` remains authoritative for
  exact references and behavior.
- Easier to review: README, SETTINGS, CONTEXT, the knowledge bundle, and
  sibling ADRs 0014/0017 now point at one accepted baseline decision.
- Harder/off-limits: no one may claim `managed_*` means hardware-fit,
  quality, safety, chat-template compatibility, or successful launch on every
  operator machine, and no scope expansion into benchmarking, automatic HF
  verification, runtime fallback, or a catalog service may hide behind this
  record.

## Alternatives considered

- Create the requested `docs/adr/0011-model-catalog...` file — rejected: ADR
  0011 already documents pipeline-owned prompt contracts and is referenced by
  code and tests; duplicate ADR numbers are rejected by
  `tests/test_docs_consistency.py`.
- Renumber or overwrite ADRs 0011–0018 — rejected: that rewrites established
  architecture references and violates the repository's never-reuse/
  never-renumber documentation policy.
- Add only a README paragraph — rejected: it would not create durable
  architecture memory or protect the ownership vocabulary from drift.
- Change runtime behavior while adding this record — rejected: the accepted
  decision describes behavior already implemented by issue #30 and subsequent
  catalog/overlay/runtime work; a documentation record must not carry
  behavior changes.
- Use Hugging Face popularity, downloads, or a live hardware probe as the
  catalog authority — rejected: the current design is task-oriented,
  offline-first for curated entries, and explicitly advisory about hardware.