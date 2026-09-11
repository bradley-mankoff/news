# ADR 0020: MiniCPM5-2B replaces Gemma 4 E2B in the curated catalog

Status: Accepted

Date: 2026-09-11

## Context

The curated Model Catalog (ADR 0019) held the five official Gemma 4
instruction variants, each as an `mlx-community` 4-bit MLX distribution and a
Unsloth `UD-Q4_K_XL` GGUF. The small/fast slot (both E2B entries, the
Codex-safe test model, the `dev` preset) needs a stronger
instruction-following model under a permissive license: the Gemma E2B entry
carries the Gemma license, and vendor-reported head-to-head numbers put
MiniCPM5-2B well ahead of Gemma 4 E2B on reasoning, instruction following,
and agentic tasks (unverified here; no health/news-workload comparison
exists either way).

MiniCPM5-2B (`openbmb`) ships official distributions for both managed
runtimes — 4-bit MLX (`openbmb/MiniCPM5-2B-MLX`, `mlx-lm`) and `Q4_K_M` GGUF
(`openbmb/MiniCPM5-2B-GGUF`, `llama.cpp`) — as a standard
`LlamaForCausalLM`, so no loader or backend changes are required. Context
length is 131,072, matching the E2B slot it takes over.

## Decision

Replace both Gemma 4 E2B entries with `minicpm5-2b-it-mlx-4bit` and
`minicpm5-2b-it-gguf-q4-k-m` (issue #327). The catalog stays at ten entries:
eight Gemma 4 plus two MiniCPM5-2B. The Codex-safe test model, the `dev`
preset, and the `speed` recommendation note move to the MiniCPM5-2B MLX
entry. `is_gemma_4_model_reference()` and the `gemma_4_derived` budget
provenance are unchanged: they describe Gemma models, and the cap value
itself was always model-independent.


The MiniCPM5-2B MLX entry carries its `speed` note pending the runtime
verification protocol in `docs/model-runtime-verification.md`; the E2B
evidence there is marked superseded.

## Consequences

The drift guards (`test_docs_drift_guard_links_match_model_aliases`, catalog
completeness, codex-guard tests) now pin the MiniCPM5-2B aliases and
`openbmb` page URLs. ADRs 0017/0019 keep their Gemma-4-era counts as
historical record; this ADR is the current statement of catalog composition.
Runtime verification of the MiniCPM5-2B MLX entry is tracked in
`docs/model-runtime-verification.md`, not here.
