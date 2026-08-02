# ADR 0010: Initially supported runtime matrix

Status: Accepted

Date: 2026-08-02

## Context

The pipeline must decide which model runtimes are initially supported so that
config surfaces, documentation, and the UI match reality (HANDOFF: "Initial
runtime scope should be honest").

Today the managed runtime is Apple-Silicon-only MLX/MLX-VLM:

- Backend inference (`news_pipeline/config.py` `infer_model_backend`) knows
  `mlx-lm` and `mlx-vlm` only.
- Managed server commands (`news_pipeline/config.py` `build_model_server_command`)
  spawn `mlx_lm`/`mlx_vlm` servers; the pyproject dependency markers are
  `darwin`/`arm64`.
- External OpenAI-compatible endpoints already work for the LLM calls
  (`langchain-openai` `ChatOpenAI`, `news_pipeline/pipeline.py`), but only for
  *per-task* models with a distinct base URL. The default model cannot be
  external: the pipeline preflights `NEWS_MODEL_BASE_URL`, sees a live server,
  and raises "Model server endpoint is already in use" instead of using it.
- `MANAGED_MODEL_SERVER_EXTERNAL` (`news_pipeline/pipeline.py`) is a pre-wired
  flag that is never set — the external-mode hook exists but is unused.
- GGUF is currently loadable only through `mlx-vlm` (Qwythos aliases); there is
  no `llama.cpp` adapter and no managed cross-platform GGUF path.

## Decision

The initially supported runtime matrix is exactly:

1. **`mlx-lm`** — managed local MLX language-model server on Apple Silicon
   (`darwin` + `arm64`, matching the pyproject markers).
2. **`mlx-vlm`** — managed local MLX vision-language-model server on Apple
   Silicon (same platform constraint).
3. **`external`** — any OpenAI-compatible endpoint, declared for the default
   model with `NEWS_MODEL_BACKEND=external` plus `NEWS_MODEL_BASE_URL`, or for
   a task model via that task's `_BASE_URL` env var (distinct base URL).

`NEWS_MODEL_BACKEND` values are validated against the closed set
(`mlx-lm`, `mlx-vlm`, `external`); invalid values fail fast with a `ValueError`
listing the valid options. When unset, the backend is inferred from the model
reference as before.

**Not initially supported:** managed cross-platform GGUF via `llama.cpp`. GGUF
files keep working only through `mlx-vlm` on Apple Silicon. A real `llama.cpp`
adapter is a deliberate later addition and requires its own issue; nothing in
this ADR should be read as promising it.

## Consequences

- The default model can be served by an external OpenAI-compatible endpoint:
  with `NEWS_MODEL_BACKEND=external` the pipeline waits for and probes the
  endpoint and never spawns a managed server (nor raises "already in use").
- Managed server startup happens only for `mlx-lm`/`mlx-vlm`.
- Per-task external models continue to work via a distinct per-task base URL;
  per-task backend env vars are out of scope until a user need exists.
- Model pickers and validation surfaces must constrain backends to the closed
  set (`SUPPORTED_MODEL_BACKENDS`); future work (Model Catalog, hardware
  compatibility links) builds on that single source of truth.
- `news model-server-command` reports that no managed server command exists
  for the external backend.
- The runtime matrix is documented in the README so operators know what is and
  is not supported in the first release.
