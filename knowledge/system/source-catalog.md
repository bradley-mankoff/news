---
type: Configuration Concept
title: Source Catalog
description: The configured source records used to scope and collect Daily News articles.
tags: [daily-news, sources, configuration]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-source-catalog
    resource: ../../CONTEXT.md
    title: Daily News Context — Source Catalog
  - id: sources-yaml
    resource: ../../config/sources.yaml
    title: Daily News Source Catalog YAML
  - id: source-catalog-code
    resource: ../../news_pipeline/source_catalog.py
    title: Source catalog helpers
  - id: config-loader
    resource: ../../news_pipeline/config.py
    title: Source configuration loading
---

# Definition

The **Source Catalog** is `config/sources.yaml`. It owns source records, YAML
layout, active/source-scope selection, source-language tags, and source edits.
Collection code reads this catalog; it does not make the OKF bundle the source
of source definitions.

# Language gate

`language` is a human-edited, authoritative tag on each source record. Normal
source selection keeps the current English-only behavior: only records with
`language == "en"` in the selected tiers are active. When
`NEWS_TRANSLATION_ENABLED=1` is set, the translation-enabled loader path also
includes selected-tier records with any declared non-empty `language`; their
scraped article bodies are translated before global story clustering (issue
#172). Records without a declared language stay excluded in both modes, and
the catalog is never automatically retagged: the translation stage consumes
declared languages and never sniffs or guesses a language from article text.

# Authority

The YAML file and its loaders are canonical. The checked-in knowledge bundle
only describes that contract and records provenance back to it.
