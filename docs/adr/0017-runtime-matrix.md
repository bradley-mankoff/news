# ADR 0017: Initially supported runtime matrix

Status: Accepted

Date: 2026-08-02 (amended 2026-08-14: managed `llama.cpp`/GGUF added, issue #75; amended 2026-08-14: fixed default backend replaces selected-model inference, issue #169)

## Context

The pipeline must decide which model runtimes are initially supported so that
config surfaces, documentation, and the UI match reality (HANDOFF: "Initial
runtime scope should be honest").

The managed runtime started as Apple-Silicon-only MLX/MLX-VLM:

- Backend inference (`news_pipeline/config.py` `infer_model_backend`) knows
  the closed `SUPPORTED_MODEL_BACKENDS` set.
- Managed server commands (`news_pipeline/config.py` `build_model_server_command`)
  spawn `mlx_lm`/`mlx_vlm` servers or delegate to the llama.cpp adapter; the
  pyproject dependency markers for the MLX packages are `darwin`/`arm64`.
- External OpenAI-compatible endpoints already work for the LLM calls
  (`langchain-openai` `ChatOpenAI`, `news_pipeline/pipeline.py`), but only for
  *per-task* models with a distinct base URL. The default model cannot be
  external without `NEWS_MODEL_BACKEND=external` plus `NEWS_MODEL_BASE_URL`.
- GGUF was not loadable by any managed backend until issue #75 added the
  managed llama.cpp adapter: `mlx-vlm` rejects file-qualified GGUF references
  (`owner/repo/file.gguf`) with `HFValidationError` (issue #124), and there
  was no managed cross-platform GGUF path.

## Decision

The supported runtime matrix is exactly:

1. **`mlx-lm`** — managed local MLX language-model server on Apple Silicon
   (`darwin` + `arm64`, matching the pyproject markers).
2. **`mlx-vlm`** — managed local MLX vision-language-model server on Apple
   Silicon (same platform constraint). Requires `mlx-vlm>=0.6.4,<0.7.0` to
   launch Gemma 4 models (`gemma4_unified` model type, issue #124); the upper
   bound is a deliberate 0.x-semver guard — mlx-vlm is pre-1.0, so minor
   releases may break the managed-server contract, and the 0.6.10 cascade
   (`mlx>=0.32.0`, `transformers>=5.14.0`) does not resolve against the
   current lock. Revisit the bound when a newer release is triaged.
   Management applies only to repositories already carrying MLX-compatible
   vision weights plus an `mmproj` asset; `mlx-vlm` does not convert source
   Transformers weights at launch. A Hugging Face search result whose
   metadata says `transformers` + `safetensors` + `image-text-to-text` is
   therefore `external_only` — the repository is an ordinary Transformers
   vision model, not a pre-converted MLX VLM.
3. **`llama.cpp`** — managed local text-generation GGUF server for
   text-generation GGUF models (issue #75). The application launches an
   operator-installed official native `llama-server` binary (selected with
   `NEWS_LLAMA_CPP_SERVER`, default `llama-server`) through the same managed
   process/readiness/log/teardown lifecycle as MLX; the stdlib-only adapter
   (`news_pipeline/llama_cpp_adapter.py`) translates HF
   `owner/repo/file.gguf` references to `--hf-repo`/`--hf-file`, bare HF
   repos to `--hf-repo` (default quantization), and local `.gguf` paths to
   `--model`, always with `--alias`, `--host 127.0.0.1`, `--port`,
   `--parallel`, and optional `--n-predict`. The application never
   downloads, installs, or replaces the native binary; a missing binary
   fails at run launch with actionable guidance. Multimodal GGUF (separate
   `mmproj` file) and image-input routing are explicitly not managed.
4. **`external`** — any OpenAI-compatible endpoint, declared for the default
   model with `NEWS_MODEL_BACKEND=external` plus `NEWS_MODEL_BASE_URL`, or for
   a task model via that task's `_BASE_URL` env var (distinct base URL).

`NEWS_MODEL_BACKEND` values are validated against the closed set
(`mlx-lm`, `mlx-vlm`, `external`, `llama.cpp`); invalid values fail fast
with a `ValueError` listing the valid options. When unset, the backend is
**not** inferred from the model reference: the fixed product default
`DEFAULT_MODEL_BACKEND` (`mlx-vlm`, the backend of the default Gemma 4 12B
model) applies (issue #169). A known catalog model whose declared backend
differs from that default — for example `gemma-e2b-tiny` (`mlx-lm`) and the
`qwythos-9b-*` GGUF aliases (`llama.cpp`) — must set `NEWS_MODEL_BACKEND`
explicitly; config resolution fails fast with an actionable message naming
the required value instead of silently launching the wrong managed server.
Raw `.gguf` references likewise require an explicit
`NEWS_MODEL_BACKEND=llama.cpp`. Explicit overrides keep the documented behavior: known catalog
MLX/llama.cpp mismatches fail fast, `mlx-lm`/`mlx-vlm` cross-overrides keep
their existing behavior, and an unknown bare HF repo explicitly selected
with `NEWS_MODEL_BACKEND=llama.cpp` is supported so llama-server applies its
documented default quantization. `infer_model_backend()` remains for
per-task model assignments, catalog/runtime-fit metadata, and compatibility
logic only.

## Consequences

- The default model can be served by an external OpenAI-compatible endpoint:
  with `NEWS_MODEL_BACKEND=external` the pipeline waits for and probes the
  endpoint and never spawns a managed server (nor raises "already in use").
  Authenticated endpoints are supported via `NEWS_MODEL_API_KEY`, sent as a
  `Bearer` token on `/models` and `/chat/completions` requests; HTTP 401/403
  responses fail fast instead of waiting out the readiness deadline.
- Managed server startup happens only for `mlx-lm`/`mlx-vlm`/`llama.cpp`;
  the llama.cpp executable is checked immediately before `Popen` and only
  for the llama backend, so Runtime Config resolution and UI previews never
  require the native binary.
- The legacy `qwythos-9b-4bit`/`qwythos-9b-8bit` aliases are supported
  again as curated llama.cpp catalog entries (file-qualified references with
  a bare `hf_repo` page id); the Gemma MLX default is unchanged.
- Per-task external models continue to work via a distinct per-task base URL;
  per-task backend env vars are out of scope until a user need exists.
- Model pickers and validation surfaces must constrain backends to the closed
  set (`SUPPORTED_MODEL_BACKENDS`); the Model Catalog
  (`news_pipeline/model_catalog.py` + `config/model_catalog.yaml`, ADR 0014)
  builds on that single source of truth, as do the hardware compatibility
  links (issue #32) already shipped in the model picker.
- Text-generation GGUF search results report `managed_llama_cpp`; multimodal
  GGUF results remain `external_only` (mmproj files are not managed).
  Transformers+safetensors vision search results (`image-text-to-text`) are
  also `external_only`: `mlx-vlm` requires pre-converted MLX weights plus an
  `mmproj` asset and does not convert ordinary Transformers repositories at
  launch. Use the external OpenAI-compatible backend for these, or convert
  them outside this application. MLX-library vision repositories and curated
  MLX VLM entries keep the `managed_mlx_vlm` verdict.
- `news model-server-command` reports that no managed server command exists
  for the external backend and prints the resolved llama.cpp command for
  managed GGUF selections without starting a server or downloading a model.
- The runtime matrix is documented in the README so operators know what is
  and is not supported in the first release.
