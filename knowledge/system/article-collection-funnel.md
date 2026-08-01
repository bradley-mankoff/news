---
type: System Concept
title: Article Collection Funnel
description: The collection stage that fetches configured sources, validates article context, deduplicates URLs, and yields fresh candidates.
tags: [daily-news, collection, diagnostics]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-collection-funnel
    resource: ../../CONTEXT.md
    title: Daily News Context — Article Collection Funnel
  - id: collection-code
    resource: ../../news_pipeline/article_collection.py
    title: Article collection implementation
  - id: pipeline-collection-call
    resource: ../../news_pipeline/pipeline.py
    title: Pipeline collection orchestration
  - id: history-store
    resource: ../../news_pipeline/history_store.py
    title: URL history store
---

# Definition

The **Article Collection Funnel** fetches configured source contexts, rejects
source mismatches, deduplicates URLs, records Source Run diagnostics, persists
candidate URL history, and returns fresh article candidates to global story
clustering.

# Boundary

Collection owns acquisition and freshness decisions. Article Summary Record
owns normalized summary data after collection; DuckDB history remains the
canonical durable runtime store.
