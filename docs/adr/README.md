# Architecture Decision Records

This folder holds Architecture Decision Records (ADRs) for decisions that future
architecture reviews should respect.

Use one numbered Markdown file per decision:

`NNNN-short-kebab-title.md`

Each ADR should include:

- `Status`: `Proposed`, `Accepted`, `Superseded`, or `Rejected`
- `Date`: `YYYY-MM-DD`
- `Context`: the pressure or constraint that forced a decision
- `Decision`: the choice future work should treat as load-bearing
- `Consequences`: what this makes easier, harder, or off-limits

Use `CONTEXT.md` for domain vocabulary. Use ADRs for decisions that should keep
future reviews from re-litigating settled tradeoffs.
