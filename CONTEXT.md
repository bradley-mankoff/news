# Context

## Run Session

A Run Session is one execution of the daily news run. It owns the run's config
snapshot, output paths, progress, diagnostics, run logs, and managed model server
lifecycle.

## Article Collection Funnel

The Article Collection Funnel is the run stage that fetches configured sources,
scrapes articles, rejects source mismatches, dedupes URLs, records Source Run
diagnostics, persists candidate URL history, and yields fresh article candidates.

## Source Catalog

The Source Catalog is the editable `config/sources.yaml` source list. It owns
source records, YAML layout, source-language tags, translation retagging, and
source removal.

## Runtime Config Resolution

Runtime Config Resolution turns base environment values, saved run presets, and
explicit overrides into one Runtime Config snapshot. It is the seam where Run
Settings become immutable before a Run Session starts. It owns setting metadata,
Run Preset overlay rules, command environment deltas, and removed-setting
validation.

## Model Configuration Vocabulary

Run Settings are the whole set of user-controllable values for one Run Session.
A Run Preset is a saved Run Settings overlay in `config/run_presets.yaml`.
Task Model Assignment means choosing which model handles a model-using task,
such as Article Summarization or Story Drafting. Model Tuning means explicit
inference settings such as sampling and token limits for a selected model. Model
Defaults are the model or backend defaults used when no Model Tuning is set.
Pipeline Budget covers non-model limits such as article caps, story caps, text
truncation, and recency windows. Model Server Settings are adapter settings for
the local OpenAI-compatible model server.

## Article Summary Record

An Article Summary Record is the normalized result of summarizing one retained
article. It owns article title, source, published time, URL, Article ID, story
assignment, summary prose, and Markdown compatibility rendering for downstream
report and history adapters.

## Story Record

A Story Record is the normalized story-level record produced by global story
clustering and consumed by story drafting, story selection, budget trimming, and
debug reporting. It owns story key, story title, selected Article IDs, cluster
Article IDs, ranking metrics, overlap comparison, and story debug projection.
