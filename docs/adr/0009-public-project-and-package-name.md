# ADR 0009: Public project and package name

Status: Accepted

Date: 2026-08-01

## Context

The open-source release is blocked by the private distribution name (leaked
from the original local folder path) in `pyproject.toml:2`, and README/HANDOFF
carried the same private/stale paths.
`news` itself is taken on PyPI (unrelated placeholder project, verified via the
PyPI JSON API). `news-pipeline`, `daily-news-pipeline`, and `news-pipeline-cli`
were verified available (HTTP 404), with `requests`/`django` control checks
(HTTP 200) confirming the probe works.

## Decision

The public project/distribution name is `news-pipeline` — the PEP 503-normalized
form of the existing `news_pipeline` import package. The import package stays
`news_pipeline`, the CLI stays `news`, the env prefix stays `NEWS_`, and the
GitHub repo stays `bradley-mankoff/news` (renaming it would break the automation
surface for zero benefit). The private name is removed from all tracked files.
Rejected candidates: `news` (taken), `daily-news-pipeline` / `news-pipeline-cli`
(free but deviate from the existing `news_pipeline` naming identity — recorded
as fallbacks if `news-pipeline` is ever contested).

## Consequences

- `pip install news-pipeline` maps unambiguously to `import news_pipeline`; no
  import churn or behavioral change.
- Publishing requires re-verifying PyPI availability at release time.
- The generic name's squatting risk is mitigated by the `bradley-mankoff` GitHub
  namespace and the recorded fallbacks.
