# Daily News Agent Notes

Project goal: build, review, and send a daily news report from configured
sources with reproducible run history and local model support.

This file is for **product workers only** (issue implementation, review, and
direct fix sessions). It is not a project-management constitution.

## Route

- Setup, CLI/UI, runbook → `README.md`
- Domain vocabulary / business rules → `CONTEXT.md`
- OKF knowledge bundle → `knowledge/` (tests: `tests/test_okf.py`)
- ADRs → `docs/adr/`
- Sources / recipients / presets → `config/*.yaml`
- Model Catalog (built-ins, YAML overlay, runtime-fit) → `config/model_catalog.yaml` + `news_pipeline/model_catalog.py`
- Security audit / history scrub → `docs/security/` +
  `automation/security_audit.py` / `scrub_policy.py` / `scrub_history.sh`
  (policy-gated, human-approved execute)
- Secret-prevention (Gitleaks local hook + PR CI gate) →
  `docs/security/secret-prevention.md` + `.pre-commit-config.yaml` +
  `.github/workflows/ci.yml` (pinned `v8.30.1`; staged-only local prevention
  and PR-range CI prevention, distinct from history audit/scrub)
- Archon workflow inventory / machine setup → `docs/archon-workflows.md`,
  `docs/archon-setup.md`
- Vendored matt skills (wrapper: `~/.claude/skills/`; archon commands:
  `.archon/commands/`) → `docs/archon-workflows.md` (Local edits)

## Always-on behavior

- Communication: **caveman full** (pi-caveman).
- Code discipline: **ponytail full** (@dietrichgebert/ponytail).
- Off only if the human explicitly asks.

## cmux pane safety

- Before cmux automation: `cmux identify`.
- NEVER close/reparent/restructure your own pane/surface; no `close-surface` /
  `new-surface` / `open --pane` on self.
- Do not create or destroy panes/surfaces unless the human asks. cmux is the
  agent screen; GitHub/archon/UI live elsewhere.
- Surface closed → SIGHUP; fresh session. History: `~/.omp/agent/sessions/`.

## Worker rules

- Issue branch: `archon/task-issue-<N>` worktree.
- Base integration branch: `develop`. Production: `main` only via the ship path.
- Leave implementation PRs **draft**. Do not self-merge to `main`.
- **Never** put GitHub auto-close keywords (`Fixes` / `Closes` / `Resolves` +
  `#N`) on develop PR bodies or commits. Use `Issue: #N`.
- Human-only decisions (license, naming, scope, recipients, real taste
  tradeoffs): stop, comment `NEEDS INPUT:` with 2–3 options + tradeoffs,
  @ `bradley-mankoff`, add label `needs-input`, make no further changes.
- Completion record on the issue must include `## What shipped`,
  `## Decisions`, `## Acceptance criteria`, `## How to test`,
  `## Deferred work` (section required even if empty / all skipped).
- Coding: match existing patterns; shortest correct diff; tests defend
  observable contracts.
- Reliable test invocation on this machine:
  `.venv/bin/python3 -m pytest tests/ -q`
  (plain `uv run pytest` can flake on spawn).
- Implement the assigned issue only. Do not move project-board lanes, promote
  backlog work, or start workflows by hand.

## Ideas

Describe the idea to the human session. Shape it into what/why, binary
acceptance criteria, out of scope, ownership, and `Depends on`, then create with:

```bash
python3 automation/create_issue.py "<title>" --body "<shaped markdown>"
```

That lands the issue in Backlog. Do not start implementation until you are on
an assigned issue branch / issue body.
