# ADR 0019: Article translation policy

Status: Accepted

Date: 2026-08-15

## Context

The pipeline needs an opt-in way to make declared non-English article bodies
available to English clustering without changing source metadata or making
language guesses. Translation crosses model assignment, pipeline ordering,
managed-server lifecycle, fallback behavior, and durable history. Those
boundaries must remain explicit so a future change does not silently move the
stage, make it destructive, or break legacy history rows.

## Decision

- Translation is disabled by default and runs after article collection and
  before global story clustering. It consumes the Source Catalog's
  human-declared language tags; it never sniffs article text, guesses a
  language, or retags a source.
- English/target-language bodies pass through unchanged. A non-empty declared
  language that differs from the target is a translation candidate. Missing
  language remains a skipped, unchanged result. An unsupported declared code
  may reach TranslateGemma; model rejection is recorded as
  `translation_failed` while preserving the original body.
- Translation is the sixth LLM task assignment and uses TranslateGemma's
  structured language-code message contract. It is intentionally excluded
  from the five editorial Prompt Profile and full-template surfaces.
- The dedicated translation model endpoint is owned by the Run Session. It is
  started lazily only when an actual translation call is needed and is stopped
  with the run. Ordinary model, construction, and response-normalization
  failures are visible per-article fallbacks; managed-server exit failures
  remain fatal so lifecycle failures are not hidden.
- Request bodies are bounded to 5,000 characters per model call. Longer bodies
  are translated in ordered chunks and reassembled, while durable history
  stores only bounded 300-character original and translated previews.
- The raw `candidate` article stage remains separate from the post-translation
  `translated` stage. Translation status, reason, source/target language,
  model, and previews are nullable history fields so additive schema migration
  keeps legacy rows valid.

## Consequences

- Clustering and summarization can use translated bodies without losing the raw
  collected candidate or changing catalog language authority.
- Translation failures are non-destructive and diagnosable at article and run
  levels, while managed-server lifecycle failures still finalize as failed
  runs and tear down owned endpoints.
- The translation task has independent model tuning and endpoint settings, but
  no editorial profile or arbitrary full-template override.
- Chunking adds model calls for long bodies and cannot guarantee model quality;
  each chunk remains subject to the shared retry and fallback policy.
