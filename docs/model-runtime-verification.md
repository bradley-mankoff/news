# Model Runtime Verification Record

This document records manual Apple-Silicon runtime evidence for the code-owned
Model Catalog (`news_pipeline/model_catalog.py`). It is an audit surface, not
the catalog itself: code review establishes the curated identity and this
record distinguishes entries that have passed an actual launch/completion
probe under the repository's locked packages.

Scope: the curated Gemma 4 model family: one `mlx-community` MLX
distribution and one Unsloth `UD-Q4_K_XL` GGUF distribution per official
instruction variant. The recorded runtime smoke below covers the smallest
MLX entry, `gemma-4-e2b-it-mlx-4bit`.

## Protocol
A candidate receives runtime-verified task notes only when it passes all of:

1. The host is Apple Silicon with macOS: `uname -s` = `Darwin`,
   `uname -m` = `arm64`.
2. The repository's locked environment is installed (`uv sync`) and the locked
   `mlx-lm` version is the runtime under test.
3. The exact managed server command is printed by the application
   (`NEWS_MODEL=<alias> NEWS_MODEL_BACKEND=mlx-lm uv run news
   model-server-command`) — never a handwritten invocation — and resolves to
   the exact Hugging Face repository and `python -m mlx_lm server`.
4. The printed command starts on a free local port and reaches readiness:
   `GET http://127.0.0.1:<port>/v1/models` returns HTTP 200.
5. A bounded `POST /v1/chat/completions` returns HTTP 200 with the requested
   model id and a completion result inside the configured token bound.
6. The server is stopped and the port is confirmed released before the next
   verification begins.
7. Sanitized evidence (host class, package versions, model revision, commands,
   readiness, completion results) is recorded below. No tokens, credentials,
   private paths, hostnames, or raw environment dumps are ever recorded.

A successful catalog listing, a Hugging Face metadata verdict, or a model
card claim is **not** runtime verification. If the server starts but a task
contract fails, the failure is recorded and that task is left out of the
entry's `task_notes` (or the entry is omitted entirely if launchability
fails).

## Verification host

| Field | Value |
|-------|-------|
| OS / architecture | `Darwin` / `arm64` |
| Machine class | Apple MacBookPro18,2 (representative Apple Silicon developer laptop) |
| Chip / memory class | Apple M1 Max, 64 GB RAM class, 10 cores |
| Verification date | 2026-08-15 (14:00–14:08 local, UTC-5) |

Only the coarse machine model and memory class are recorded; the hostname and
username are not.

## Locked packages under test

From the repository `uv.lock` (unchanged by this work):

| Package | Version |
|---------|---------|
| `mlx-lm` | 0.31.3 |
| `mlx` | 0.31.2 |
| `huggingface-hub` | 1.26.0 |
| `pyyaml` | 6.0.3 |
| Python (venv) | 3.12.13 |

## Evidence: `gemma-4-e2b-it-mlx-4bit`

| Field | Value |
|-------|-------|
| Alias | `gemma-4-e2b-it-mlx-4bit` |
| Repository | `mlx-community/gemma-4-e2b-it-4bit` |
| Model revision (Hugging Face `sha`, `main` at verification) | `238767527555cb75a05732a84dff5d6ba0dd6809` |
| Hub last modified (model card) | `2026-07-06T00:14:55Z` |
| Pipeline tag / library | `image-text-to-text` / `mlx` |
| Declared backend | `mlx-lm` |
| Generated server command (port as printed by the application) | `uv run python -m mlx_lm server --model mlx-community/gemma-4-e2b-it-4bit --decode-concurrency 4 --prompt-concurrency 4 --host 127.0.0.1 --port 8080 --prefill-step-size 512 --prompt-cache-size 2 --prompt-cache-bytes 512MB --max-tokens 1800 --log-level INFO` |

Verification date: 2026-09-01. Readiness reached on `127.0.0.1:8080`;
`GET /v1/models` returned HTTP 200. The production-shaped text probe
returned the expected model id and completion inside the two-token bound.

| Probe | Call shape | Result |
|-------|-----------|--------|
| Text health probe | `POST /v1/chat/completions`, `max_tokens: 2`, `temperature: 0`, `chat_template_kwargs={"enable_thinking": false}` | `ok`; `finish_reason: stop`; ~1.0 s |

This entry is verified for the text-only `mlx-lm` contract used by the
pipeline. The Hub metadata advertises `image-text-to-text`; this record does
not claim image-input support through `mlx-lm`.

## Rejected candidate: LM Studio Gemma 4 E2B MLX

The consistent publisher choice is `mlx-community`, not the LM Studio mirror.
The exact probe of `lmstudio-community/gemma-4-E2B-it-MLX-4bit` downloaded
successfully but failed during model load under the locked `mlx-vlm` runtime:

```text
ValueError: Expected shape (128, 3, 3, 1) but received shape (128, 3, 1, 3)
for parameter audio_tower.subsample_conv_projection.layer0.conv.weight
```

That repository is not a catalog entry. The failure is a runtime incompatibility
in the candidate's audio tower, not a reason to weaken the loader or add a
special case. The selected `mlx-community` E2B repository passed the
`mlx-lm` probe above.

- Verification covers launch, readiness, and representative bounded
  completions on one 64 GB-class Apple-Silicon host. It is **not** a promise
  of universal hardware fit: memory use on smaller hosts can differ, and
  larger Gemma variants are host-sensitive. Context-length metadata is
  catalog metadata, not a throughput promise.
- The Gemma E2B entry carries the Gemma license according to its model card;
  re-check licenses for every variant before shipping.
- Hugging Face repository contents can change after verification. The revision
  id and retrieval date above make future drift visible; the manual gate should
  be re-run before any later catalog change. No CI job downloads or starts
  these weights.
- The health smoke used the same `enable_thinking: false` call shape the
  pipeline always sends, so the passing result reflects the production
  contract; this record does not claim image-input support through `mlx-lm`.