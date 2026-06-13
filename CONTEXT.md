# Context

This file names project concepts so architecture reviews and ADRs can use the
same domain language as the code.

## Daily News Run

A Daily News Run is one end-to-end execution of the news pipeline. It collects
articles, groups stories, drafts and selects reports, optionally generates
images, writes run artifacts, and records history.

## Run Session

A Run Session is one execution of the daily news run. It owns the run's config
snapshot, output paths, progress, diagnostics, run logs, and managed model server
lifecycle.

## Runtime Config

Runtime Config is the resolved settings snapshot for a Daily News Run. It covers
model profiles, source scope, output paths, recipients, feature flags, and
history paths.

## Source Catalog

The Source Catalog is the configured set of news sources and source metadata in
`config/sources.yaml`. It includes source identifiers, feed URLs, source tiers,
language settings, and translation settings.

## Feed Probe

A Feed Probe checks whether a source feed can be fetched, parsed, normalized,
and sampled into usable article data. It reports source health without running a
full Daily News Run.

## Article Collection

Article Collection is the top-of-funnel work that fetches source feeds, converts
scrape outcomes into article candidates, deduplicates URLs, records source-run
diagnostics, and updates URL history.

## Article Candidate

An Article Candidate is a fetched article record that may enter story clustering.
It carries source metadata, URL, title, publication time, article text, and any
translation or rejection details.

## Story Record

A Story Record is the internal representation of a news story as it moves through
clustering, drafting, selection, ranking, overlap checks, and debug
serialization.

## Article Summary Record

An Article Summary Record is structured article-summary data used inside the
pipeline. Markdown belongs at the LLM and report Seams, not in the domain
shape the rest of the pipeline should have to understand.

## Citation Evidence

Citation Evidence is the source material and marker metadata used to validate,
deduplicate, order, and render citations for selected stories.

## Run Diagnostics

Run Diagnostics are the structured event and metric records for one Run Session.
They include top-of-funnel counts, source runs, article budgets, model-call
stats, activity snapshots, report metadata, run artifacts, and completion or
failure events.

## Run Artifact

A Run Artifact is a file produced by a Daily News Run and recorded for review,
history import, or reuse. Examples include run details JSON, run review
Markdown, article summary debug files, final reports, images, URL lists, and run
logs.

## Run Review

A Run Review is the human-readable Markdown view of a Run Session. It is derived
from Run Diagnostics and report output so a run can be inspected after success,
early abort, or failure.

## Run History

Run History is the DuckDB-backed record of Daily News Runs, articles, summaries,
reports, artifacts, logs, and URL reuse state.

## Run Finalizer

The Run Finalizer turns the current Run Session state into durable end-of-run
records. It finishes Run Diagnostics, writes rolling review/details artifacts,
imports run status into Run History, and preserves failed-run information.
Callers should record outcomes and artifacts as they happen, then let the Run
Finalizer finish the run once.

## Report Assembly

Report Assembly turns selected stories, citation evidence, generated image
metadata, and recipient settings into final report text, HTML, email metadata,
and report artifacts.

## Stage Runtime

A Stage Runtime is the dependency bundle for an LLM-backed pipeline stage such as
article summarization, story drafting, or story selection. It holds the Adapters
that genuinely vary between production and tests.
