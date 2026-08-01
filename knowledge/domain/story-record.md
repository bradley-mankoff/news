---
type: Domain Concept
title: Story Record
description: The normalized story-level record produced by global clustering and consumed by drafting, selection, budget trimming, and diagnostics.
tags: [daily-news, story, clustering]
status: stable
generated: {by: openai-codex/gpt-5.6, at: 2026-07-27T00:00:00Z}
sources:
  - id: context-story-record
    resource: ../../CONTEXT.md
    title: Daily News Context — Story Record
  - id: adr-story-record
    resource: ../../docs/adr/0006-story-record-owns-story-lifecycle.md
    title: ADR 0006 — Story Record lifecycle
  - id: story-record-code
    resource: ../../news_pipeline/story_records.py
    title: StoryRecord implementation
  - id: clustering-code
    resource: ../../news_pipeline/story_clustering.py
    title: Global story clustering
  - id: selection-code
    resource: ../../news_pipeline/story_selection.py
    title: Global story selection
---

# Definition

A **Story Record** owns a story key and title, selected and cluster Article IDs,
article counts, source counts, similarity and cohesion metrics, pruning
information, ranking, and safe story-level debug extras. Story lifecycle
invariants live in this record adapter rather than in repeated dict conventions.

# Authority

`news_pipeline/story_records.py` and ADR 0006 define the lifecycle projection.
The OKF Run Bundle links story concepts to article concepts and preserves useful
scalar story metrics without becoming the runtime story store.
