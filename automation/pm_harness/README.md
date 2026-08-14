# PM Harness

A reusable, Python-standard-library state machine connecting a GitHub Projects
v2 board to Archon workflows.

The harness owns mechanical board behavior only: lane transitions, dependency
gates, bounded dispatch, run recovery, integration/ship pull-request gates, and
durable state. Product-specific lifecycle work belongs in configured hooks.
Human/worker role policy does **not** belong here and must not be imported from
product repo docs.

## Requirements

- Python 3.12+
- Authenticated `gh` CLI
- `archon` CLI on `PATH`
- A GitHub Projects v2 board whose items are issues from the configured repository

## Run

Copy `automation/pm_harness/` and the thin `automation/board_health.py`
adapter into a repository. Edit `example_config.json` and store it at
`automation/config.json`, or set `PM_HARNESS_CONFIG` and `PM_HARNESS_ROOT`.

```bash
python3 -m automation.pm_harness --once
python3 -m automation.pm_harness
python3 automation/board_health.py
```

Each poll runs in a separate process group and is terminated after `poll_timeout_seconds`. The supervisor continues after failures; one stuck CLI call cannot deadlock later polls.

## Configuration

See `example_config.json`. The interface is:

- Repository/project identity: `repo`, `project_owner`, `project_number`, `status_field`.
- Lane roles: human lane names map to `backlog`, `todo`, `in_progress`, `blocked`, `needs_input`, `ready`, `review`, and `done`.
- Dispatch: implementation and review workflow names, integration/ship branches, optional label-to-workflow overrides, and conflict workflows.
- Capacity: `max_concurrent_workflows`; deferred Todo items remain visible and receive one capacity comment per episode.
- Decisions: issues carrying `decision_only.label` never dispatch implementation. Open decisions move to `decision_only.move_to`; closed decisions move to Done.
- Hooks: argv arrays under `hooks`. `after_integration_merge` runs after a verified integration merge. Hook failure is logged and never changes board truth.

Commands are argv arrays, not shell strings. The harness never evaluates hook text through a shell.

## Durable identity

`state_file` uses schema v2. Records are persisted by GitHub issue number, then hydrated to the current Project item ID for each poll. Recreating a board item therefore does not lose the run, branch, recovery, review, or conflict episode.

A dispatched Archon `run_id` is authoritative. Completion and recovery query that exact run first. Issue/message matching exists only to recover old or detached records. Malformed bulk run-list JSON cannot hide a known run.

## State machine

```text
Backlog -> Todo -> In Progress -> Ready for Review -> In Review -> Done
             |          |                 |               |
             |          +-- recovery -----+               +-- verdict/conflict gate
             +-- dependency/capacity/decision gates
```

- First poll is a snapshot and dispatches nothing.
- Todo transition dispatches once after dependency, decision, worktree, and capacity gates pass.
- Completed implementation must have a linked pull request verified merged into the integration branch before Ready.
- Ready is sticky-truthful: a manual/stale Ready item without merge proof returns to In Progress.
- Ship merge requires `VERDICT: approve`.
- Resolved conflicts clear episode markers; an unreviewed resolved ship is requeued for review.
- Cancelled/transport-failed clean runs retry once. Dirty worktrees require explicit resume or discard.

## Package map

- `policy.py`: pure dependency, routing, readiness, verdict, and conflict rules.
- `model.py`: run records and recovery classification.
- `archon.py`: bounded Archon/run/worktree adapter.
- `github.py`: GitHub Projects, issue, comment, and pull-request adapter.
- `dispatch.py`: capacity-bounded workflow launch.
- `recovery.py`: persisted stopped-run recovery.
- `cycle.py`: one board reconciliation cycle, split into explicit transition phases.
- `runtime.py`: config, hooks, logging, and schema-v2 state.
- `engine.py`: public exports and the isolated poll supervisor.

## Repository-specific integrations

Do not add product behavior to `cycle.py`. Configure a hook or keep a sibling
adapter; the reusable package contains no UI, launchd, checkout, or
product-runtime assumptions.

Outer-PM constitution belongs in the operator's agent profile, not this package.
