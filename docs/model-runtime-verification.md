# Model Runtime Verification Record

This document records the manual Apple-Silicon runtime gate that a code-owned
curated model must pass before it may join the built-in Model Catalog
(`news_pipeline/model_catalog.py`). It is an audit surface, not the catalog
itself: an entry is curated only after its code review, and is runtime-verified
only after the procedure below succeeds on a real Apple-Silicon host using the
repository's locked packages.

Scope: Qwen3 MLX language-model verification for issue #89
(`qwen3-8b-4bit`, `qwen3-14b-4bit`).

## Protocol

A candidate qualifies for a catalog entry only when:

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

## Evidence: `qwen3-8b-4bit`

| Field | Value |
|-------|-------|
| Alias | `qwen3-8b-4bit` |
| Repository | `mlx-community/Qwen3-8B-4bit` |
| Model revision (Hugging Face `sha`, `main` at verification) | `545dc4251c05440727734bcd94334791f6ab0192` |
| Hub last modified (model card) | `2025-04-28T21:51:20Z` |
| Pipeline tag / library | `text-generation` / `mlx` |
| Declared backend | `mlx-lm` |
| Generated server command (port as printed by the application) | `uv run python -m mlx_lm server --model mlx-community/Qwen3-8B-4bit --decode-concurrency 4 --prompt-concurrency 4 --host 127.0.0.1 --port 8080 --prefill-step-size 512 --prompt-cache-size 2 --prompt-cache-bytes 512MB --max-tokens 1800 --log-level INFO` |

Readiness: after launch on `127.0.0.1:8080`, `GET /v1/models` returned HTTP 200
in ~5 s; the OpenAI-style response listed
`mlx-community/Qwen3-8B-4bit` among the served model ids.

Bounded completions (all HTTP 200, port released after shutdown):

| Probe | Call shape | Result |
|-------|-----------|--------|
| Structured-output JSON contract (production call shape: `chat_template_kwargs={"enable_thinking": false}`, as the pipeline always sends) | `POST /v1/chat/completions`, `max_tokens: 200`, `temperature: 0` | `{"topic": "finance", "sentiment": "positive"}`; `finish_reason: stop`; 1.0 s |
| Speed probe (same call shape) | `POST /v1/chat/completions`, `max_tokens: 60` | Coherent one-sentence summary; `finish_reason: stop`; 0.7 s (prompt cache hit) |

Task-contract caveat (recorded honestly): with the model's default thinking
mode (`enable_thinking` unset) and a tight completion bound (120–300 tokens),
the reasoning channel consumed the whole budget and `message.content` came
back empty with `finish_reason: "length"`. The project's pipeline disables
hidden reasoning on every completion
(`pipeline.VISIBLE_CONTENT_CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}`,
`news_pipeline/pipeline.py`), and the structured-output smoke above passed
under exactly that production call shape. Any caller that leaves thinking
enabled must budget tokens for the reasoning channel.

Supported task notes (see `model_catalog.py`): `speed`, `structured_output`.

## Evidence: `qwen3-14b-4bit`

| Field | Value |
|-------|-------|
| Alias | `qwen3-14b-4bit` |
| Repository | `mlx-community/Qwen3-14B-4bit` |
| Model revision (Hugging Face `sha`, `main` at verification) | `a4d9b2df59d2c150bef02fcbe0d91046b7ca33a4` |
| Hub last modified (model card) | `2025-04-29T02:47:57Z` |
| Pipeline tag / library | `text-generation` / `mlx` |
| Declared backend | `mlx-lm` |
| Generated server command (port as printed by the application) | `uv run python -m mlx_lm server --model mlx-community/Qwen3-14B-4bit --decode-concurrency 4 --prompt-concurrency 4 --host 127.0.0.1 --port 8080 --prefill-step-size 512 --prompt-cache-size 2 --prompt-cache-bytes 512MB --max-tokens 1800 --log-level INFO` |

Readiness: after launch on `127.0.0.1:8080`, `GET /v1/models` returned HTTP 200
in ~5 s; the OpenAI-style response listed
`mlx-community/Qwen3-14B-4bit` among the served model ids.

Bounded completions (all HTTP 200, port released after shutdown):

| Probe | Call shape | Result |
|-------|-----------|--------|
| Factual-extraction JSON contract | `POST /v1/chat/completions` (default thinking mode), `max_tokens: 140`, `temperature: 0` | `{"subject": "The Fed", "verb": "raised"}`; `finish_reason: stop`; 1.8 s |
| Synthesis contract | `POST /v1/chat/completions`, `max_tokens: 300` | Two coherent sentences combining both provided facts; `finish_reason: stop`; 6.4 s |
| Citation-fidelity contract (`[[S1]]`/`[[S2]]` markers) | `POST /v1/chat/completions`, `max_tokens: 300` | Both markers preserved and attached to their own facts; `finish_reason: stop`; 2.3 s |

Supported task notes (see `model_catalog.py`): `factual_extraction`,
`synthesis`, `citation_fidelity`.

## Notes and limitations

- Verification covers launch, readiness, and representative bounded
  completions on one 64 GB-class Apple-Silicon host. It is **not** a promise
  of universal hardware fit: memory use on smaller hosts can differ, and the
  14B 4-bit entry in particular is host-sensitive. Context length metadata
  (`40960`, from the converted model's `config.json`) is factual and is not a
  throughput promise.
- The selected entries are Apache 2.0 per their model-card license; the
  license was re-checked on the verification date in the candidate research.
- Hugging Face repository contents can change after verification. The
  revision id and retrieval date above make future drift visible; the manual
  gate should be re-run before any later catalog change. No CI job downloads
  or starts these weights.
- The 8B structured-output smoke used the same `enable_thinking: false` call
  shape the pipeline always sends, so the passing result reflects the
  production contract; the default-thinking-mode empty-content observation is
  a documented caller caveat, not a claim about quality.