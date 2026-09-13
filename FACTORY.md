# Factory (news)

The Mode B factory wraps this repo; the rules live there, not here.
Manual: `../omp-modes/docs/human.md`. Behavior defaults are shared — this
repo carries identity plus deliberate deviations only (see `factory.json`).

Repo identity:

- Piyaz project **DN** ("Daily News", Bradley's Team) — the task graph.
- Base branch `develop`. Git/GitHub stays the code layer.
- Gate: `.venv/bin/python3 -m pytest tests/ -q`.
- Deviations: 3 concurrent workflows, 3 attempts (shared: 1 and 2).

Your job is ideas and gates. Taste waits for you; mechanical work with full
evidence and green checks finishes on its own and flips its ticket done.
