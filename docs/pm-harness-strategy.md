# PM Harness Extraction

**Status:** implemented on 2026-08-08

## Goal

Make the board/Archon loop a repository-neutral tool that can be copied into a
new repository without carrying Daily News UI, model, launchd, checkout, or
role-policy assumptions. Preserve the proven board behavior while replacing
the 3,400-line poller monolith and its process-global/item-ID state.

## Board issue inventory

| Issue | Requirement | Implementation |
|---|---|---|
| #177 | Treat cancelled transport failures as recoverable and log once | `pm_harness/recovery.py`; clean runs retry once, dirty runs require resume/discard |
| #178 | Cap concurrency at two; disable deferred issue creation | `automation/config.json`; capacity remains config-driven |
| #179 | Keep outer PM policy out of product worker instructions | PM constitution remains in `~/.omp/profiles/pm/agent/`; repo `AGENTS.md` is worker-only |
| #182 | Require merged integration PR before Ready and make correction sticky | `_enforce_ready_proof` bounces stale/manual Ready items to In Progress |
| #183 | Requeue review after a ship conflict resolves | resolved episodes clear markers and dispatch review once when no verdict exists |
| #184 | Make capacity holds visible | one issue comment per capacity-hold episode; health report includes held Todo items |
| #185 | Never dispatch implementation for decision-only tickets | `decision_only.label` routes open decisions to the configured human-input lane and closed decisions to Done |
| #187 | Reconcile completion by issue/run identity, not dispatch-message substring | persisted `run_id` is queried directly; issue/message matching is recovery fallback only |

The closed #18 dispatch-smoke issue is historical test debris, not a remaining
product requirement.

## Plan-only requirements

The previous strategy also identified requirements that were not board issues:

- **Reusable package:** `automation/pm_harness/` contains the state machine;
  repository scripts are thin adapters.
- **Durable identity:** schema-v2 state is keyed by GitHub issue number and
  hydrated to the current Projects item ID on each poll.
- **Bounded supervisor:** every poll runs in an isolated process group with a
  configured timeout; a wedged `gh`/Archon child cannot deadlock future polls.
- **Product lifecycle seam:** `hooks.after_integration_merge` is an argv array.
  News implements it in `automation/news_ui_runtime.py`; the PM package owns no
  UI or checkout behavior.
- **Safe UI adapter:** News updates a dedicated clean `develop` worktree and
  restarts only the UI process registered as owned. It never resets a developer
  checkout or kills an unknown listener.
- **Portable config:** `example_config.json` contains placeholders only;
  `load_config` validates the required project, lane, dispatch, capacity, and
  hook interface before polling.

## Module seam

- `model.py`: run records and recovery classification
- `policy.py`: pure routing, dependency, readiness, verdict, and conflict rules
- `archon.py`: run/worktree adapter
- `github.py`: Projects/issues/pull-request adapter
- `dispatch.py`: capacity-bounded launch and resume
- `recovery.py`: stopped and detached run recovery
- `deferred.py`: optional deferred-work reconciliation
- `cycle.py`: one board reconciliation cycle and explicit transition phases
- `runtime.py`: config, hooks, logging, and durable state
- `engine.py`: public compatibility exports and bounded poll supervisor

`automation/board_poller.py` is now a compatibility entrypoint, not an
implementation layer. Product behavior must use a configured hook or sibling
adapter rather than return to `cycle.py`.

## Portability check

From a clean clone:

```bash
python3 -m automation.pm_harness --dry-run --once
python3 automation/board_health.py
.venv/bin/python3 -m pytest tests/test_pm_harness.py tests/test_board_poller.py -q
```

A consumer copies `automation/pm_harness/`, the thin entrypoints it needs, and
an edited `example_config.json`. Runtime files and credentials remain outside
the package.
