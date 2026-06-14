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
explicit overrides into one Runtime Config snapshot. It owns runtime knob
metadata, preset overlay rules, command environment deltas, and removed-setting
validation before a Run Session starts.
