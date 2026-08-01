---
type: Domain Concept
title: Article Summary Record
description: The normalized result of summarizing one retained article, including identity, provenance, story assignment, and summary prose.
tags: [daily-news, article, summary]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-article-record
    resource: ../../CONTEXT.md
    title: Daily News Context — Article Summary Record
  - id: adr-article-record
    resource: ../../docs/adr/0005-article-summary-record-owns-summary-normalization.md
    title: ADR 0005 — Article Summary Record
  - id: article-record-code
    resource: ../../news_pipeline/article_summary_records.py
    title: ArticleSummaryRecord implementation
  - id: summarization-code
    resource: ../../news_pipeline/article_summarization.py
    title: Article summarization stage
---

# Definition

An **Article Summary Record** owns the article title, source, published time,
original URL, Article ID, story assignment, summary prose, and compatibility
rendering needed by downstream report and history adapters. Structured records
are the input to portable OKF article concepts; rendered Markdown is not the
canonical record.

# Authority

`news_pipeline/article_summary_records.py` and ADR 0005 define normalization
and fallback behavior. Report rendering, DuckDB history, and the OKF Run Bundle
are separate adapters of this record.
