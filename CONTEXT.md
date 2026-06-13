# Context

## Run Session

A Run Session is one execution of the daily news run. It owns the run's config
snapshot, output paths, progress, diagnostics, run logs, and managed model server
lifecycle.

## Article Collection Funnel

The Article Collection Funnel is the run stage that fetches configured sources,
scrapes articles, rejects source mismatches, dedupes URLs, records Source Run
diagnostics, persists candidate URL history, and yields fresh article candidates.
