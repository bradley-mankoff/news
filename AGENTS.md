# Daily News Agent Notes

Project goal: build, review, and send a daily news report from configured sources with reproducible run history and local model support.

## Roles

- **Worker** (Archon / pi on an issue, or a direct doer session): reads this file + `CONTEXT.md` + issue body; implements/reviews the issue only — **do not manage the board** (no lane moves; lane moves during direct fixes collide with the poller).
- **Outer PM**: deliberately absent from this repo. Its constitution, `PM.md`, and lessons live in the pm profile (`~/.omp/profiles/pm/agent/`, opened with `omp --profile pm`). This repo contains no PM-role guidance, so direct sessions never see mixed signals.

## Route (product)

- Setup, CLI/UI, runbook → `README.md`
- Domain vocabulary / business rules → `CONTEXT.md`
- OKF knowledge bundle → `knowledge/` (tests: `tests/test_okf.py`)
- ADRs → `docs/adr/`
- Sources / recipients / presets → `config/*.yaml`
- Security audit / history scrub → `docs/security/` + `automation/security_audit.py` / `scrub_history.sh` (human-gated execute)
- Archon workflow inventory / machine setup → `docs/archon-workflows.md`, `docs/archon-setup.md`
- Vendored matt skills (wrapper: `~/.claude/skills/`; archon commands: `.archon/commands/`) → `docs/archon-workflows.md` (Local edits)

## Always-on behavior

- Communication: **caveman full** (pi-caveman). Code discipline: **ponytail full** (@dietrichgebert/ponytail). Off only if the human explicitly asks.

## cmux pane safety

- Before cmux automation: `cmux identify`.
- NEVER close/reparent/restructure your own pane/surface; no `close-surface` / `new-surface` / `open --pane` on self.
- Do not create or destroy panes/surfaces unless the human asks. cmux is the agent screen; GitHub/archon/UI live elsewhere.
- Surface closed → SIGHUP; fresh session. History: `~/.omp/agent/sessions/`.

## Worker rules (Archon / pi)

- Issue branch: `archon/task-issue-<N>` worktree. Base integration branch: `develop`. Production: `main` only via ship path.
- Leave implementation PRs **draft** for the poller; do not self-merge to `main`.
- **Never** put GitHub auto-close keywords (`Fixes`/`Closes`/`Resolves` + `#N`) on develop PR bodies or commits. Use `Issue: #N`.
- Human-only decisions (license, naming, scope, recipients, real taste tradeoffs): stop, comment `NEEDS INPUT:` with 2–3 options + tradeoffs, @ `bradley-mankoff`, add label `needs-input`, make no further changes.
- Completion record on the issue must include `## What shipped`, `## Decisions`, `## Acceptance criteria`, `## How to test`, `## Deferred work` (section required even if empty / all skipped).
- Coding: match existing patterns; shortest correct diff; tests defend observable contracts.
- Reliable test invocation on this machine: `.venv/bin/python3 -m pytest tests/ -q` (plain `uv run pytest` can flake on spawn).

## Ideas (only the human or the pm profile boards)

Describe idea → grill into what/why, binary acceptance criteria, out of scope, ownership, `Depends on` →
`python3 automation/create_issue.py "<title>" --body "<shaped markdown>"` → lands **Backlog**.
Work starts only on move to **Todo** (human or pm profile). Workers never promote Todo to start themselves.
