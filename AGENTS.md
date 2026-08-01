# Daily News Agent Notes

Project goal: build, review, and send a daily news report from configured sources with reproducible run history and local model support.

## Route
- Need setup, commands, UI usage, PR review flow, or operational runbook -> `README.md`.
- Need project vocabulary, business rules, or architecture context -> `CONTEXT.md`.
- Need accepted architectural decisions -> `docs/adr/`.
- Need source definitions -> `config/sources.yaml`.
- Need recipient definitions -> `config/recipients.yaml`.
- Need run presets -> `config/run_presets.yaml`.
- Need model tuning presets -> `config/model_tuning_presets.yaml`.
- Need board automation details -> `automation/` (`board_poller.py`, `config.json`).
- Need the Archon workflow inventory (usable vs archived) -> `docs/archon-workflows.md`.

## Always-on behavior
- Always-on communication: **caveman full** (pi-caveman extension). Always-on code discipline: **ponytail full** (@dietrichgebert/ponytail extension+skills). Do not disable these unless the user explicitly asks.

## cmux pane safety
- The agent session runs inside a cmux terminal surface. Before any cmux automation, identify it: `cmux identify` (caller `pane_ref`/`surface_ref`).
- NEVER close, reparent, or restructure your own pane/surface, and never target it with `close-surface`, `new-surface`, `open --pane`, or tab actions.
- Do not create or destroy panes/surfaces in the user's workspace unless asked. The user owns the layout (status quo: agent upper-left, blank lower-left, GitHub board upper-right, archon console bottom-right).
- If the surface is closed, the session exits with SIGHUP (stdin ends) — normal terminal behavior, not a crash. Start a fresh session; history persists under `~/.omp/agent/sessions/`.

## Project board protocol
- The GitHub project board (project #1, “Build public UI”, on `bradley-mankoff`) is the work queue. Lanes: `Backlog` -> `Todo` -> `In Progress` -> `Ready for Review` -> `In Review` -> `Done`.
- New issues land in `Backlog`. Work never starts from creation or from `Backlog`: implementation begins only when an issue is moved into `Todo` (the board poller dispatches an Archon workflow; moving out and back in restarts).
- While implementing an issue: work on a branch, keep the PR draft until ready.
- When the dispatched run completes, the poller moves the issue to `Ready for Review` automatically — that is the signal that a draft PR is waiting for the human. Test the branch locally.
- When the human judges it working, move the issue to `In Review` with:
  `python3 automation/move_item.py <issue-number> "In Review"`
  and post a summary comment on the PR. The review loop takes over from there (poller -> `archon-smart-pr-review`).
- After the PR merges, move the issue to `Done`:
  `python3 automation/move_item.py <issue-number> Done`
- Workflow dispatch is label-aware: `bug` -> `archon-fix-github-issue`; `feature`/`enhancement` -> `archon-idea-to-pr`; default -> `archon-fix-github-issue`. Overrides live in `automation/config.json`.
- The poller runs as a launchd agent (`com.bradley-mankoff.news-board-poller`); state in `automation/state.json` (gitignored). First poll after restart is a snapshot and dispatches nothing.

## Review stages (two, by design)

- **Readiness review — inside the implementation workflows, before the human sees anything** (`archon-fix-github-issue` runs smart review + self-fix + simplify; `archon-idea-to-pr` runs the 5-agent review block + fixes): the bar is “the human should not have to check whether it works, is complete, or matches the issue’s intent.” PRs are left draft; the poller moves the issue to `Ready for Review` when the run completes, and the human tests the branch locally from there.
- **Quality review — the In Review lane trigger** (`archon-smart-pr-review`): after the human judges the feature working and moves the ticket to In Review, the review targets code quality, conventions, and subtle/peripheral breakage, auto-fixing CRITICAL/HIGH findings.
- Not redundant by design: the second review runs on the diff *after* human testing and feedback; the first one guarantees the diff is worth the human's time. (Human changes between the two make the second review see a different diff.)
- Only the pi-usable Archon workflows are installed; claude-only ones are archived (see `docs/archon-workflows.md`).
