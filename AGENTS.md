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
- Need the deferral strategy and the human-facing action checklist -> `README.md`, Project Automation (Deferred work bullet + Human touchpoints).
- Need security audit results or the history-scrub runbook -> `docs/security/` (`audit-2026-08-02.md`, `history-scrub.md`).
- Need the security audit scanner or the gated scrub wrapper -> `automation/` (`security_audit.py` — exit 0 clean / 1 findings; `scrub_history.sh` — dry-run default, force-push requires human approval).
- Need a read-only board health report (stale runs, unknown blockers, unsatisfied deps) -> `python3 automation/board_health.py` (exit 0 always; prints findings).
- Need poller one-shot/dry-run checks -> `python3 automation/board_poller.py --once` / `--dry-run` (no mutations, one poll).
- Need to re-apply local archon workflow edits or restart the poller after a deploy -> `automation/deploy.sh` (idempotent; also covers archon reinstall, see `docs/archon-workflows.md`).
- Need the Archon workflow inventory (usable vs archived) -> `docs/archon-workflows.md`.
- Need the machine-local Archon setup/operations map (services, quirks, upgrade) -> `docs/archon-setup.md`.
- Adding/changing model aliases? Update the HF page URLs in README.md and SETTINGS.md too — `tests/test_config_helpers.py::test_docs_drift_guard_links_match_model_aliases` pins that the docs carry every alias's page URL.

## Always-on behavior
- Always-on communication: **caveman full** (pi-caveman extension). Always-on code discipline: **ponytail full** (@dietrichgebert/ponytail extension+skills). Do not disable these unless the user explicitly asks.

## cmux pane safety
- The agent session runs inside a cmux terminal surface. Before any cmux automation, identify it: `cmux identify` (caller `pane_ref`/`surface_ref`).
- NEVER close, reparent, or restructure your own pane/surface, and never target it with `close-surface`, `new-surface`, `open --pane`, or tab actions.
- Do not create or destroy panes/surfaces at all unless the user explicitly asks. cmux is the agent's own screen: GitHub, the archon console, and the app UI live on OTHER screens or machines (the user pastes URLs where they want).
- If the surface is closed, the session exits with SIGHUP (stdin ends) — normal terminal behavior, not a crash. Start a fresh session; history persists under `~/.omp/agent/sessions/`.

## Fresh-session quickstart (GitHub project/issue manager)
- Board: https://github.com/users/bradley-mankoff/projects/1 — lanes `Backlog` -> `Todo` -> `In Progress` -> `Blocked` -> `Ready for Review` -> `In Review` -> `Done`. The board drives everything; nothing starts from `Backlog`.
- Status checks: `launchctl list | grep news-board-poller` (poller alive), `tail -f automation/board_poller.log` (poller log), `archon workflow runs` (runs, from repo root), `archon workflow get <id> --json` (one run).
- Board ops: `python3 automation/move_item.py <issue> <lane>`; issues: `python3 automation/create_issue.py "<title>"`. Automation config: `automation/config.json` (lanes, workflow mapping, merge targets).
- Creating a Backlog issue: `python3 automation/create_issue.py "<title>"` (one step: create + board + default lane; `--label enhancement` → idea-to-pr dispatch, `--body` for context, `--lane` to override).
- Who moves what: the human drags to `Todo` (start work) and `In Review` (ship + quality review); the poller moves `In Progress` (on dispatch), `Blocked` (run completed with a `needs-input` label — awaiting human answer), `Ready for Review` (run completed + PR merged into develop), `Done` (ship PR merged into main after review).
- Branch model: `develop` = integration (repo default; workflow base); `main` = production (only via reviewed ship PRs); per-issue branches `archon/task-issue-<N>`.
- GitHub access convention: gh CLI + the automation scripts (no MCP server).
- Dev loop (check out develop + run UI): the `news-dev` skill; reply with only the URL.
- Archon execution: archon-pi build, home at `~/.local/share/archon-pi/archon-home/`; all workflows run pi/opencode-go (OpenCode Zen Go) at max effort (curated set + quirks in `docs/archon-workflows.md`).

## Project board protocol
- The GitHub project board (project #1, “Build public UI”, on `bradley-mankoff`) is the work queue. Lanes: `Backlog` -> `Todo` -> `In Progress` -> `Blocked` -> `Ready for Review` -> `In Review` -> `Done`.
- **Dependencies:** a `Depends on: #N` line in the issue body (one line; `#42, #57` for several) gates dispatch. An issue dragged to `Todo` with an unsatisfied dependency (referenced issue not in `Done`) is moved to `Blocked` with a comment instead of dispatching; it returns to `Todo` automatically when its deps ship. A dependency that closes without shipping posts a re-scope notice on each dependent. Dependencies must be board items in this repo. Fill `Depends on` before dragging to `Todo`; the issue templates include the field.
- **Ship merge is verdict-gated:** the poller merges the ship PR into `main` and closes the issue only when the review run completed AND its final comment carries `VERDICT: approve` (posted by the workflow's `report-verdict` node). Any other verdict (or none) holds the ship in `In Review` with a comment on the issue; fix findings and re-drag to re-review.
- **Completion records:** implementation workflows post a structured comment on the issue after each run: `## What shipped` (substance, not run narration), `## Decisions` (choice + why only), `## Acceptance criteria` (each criterion with cited evidence). The record also carries a `## Deferred work` section (one bullet per deferred item); when a run completes, the poller dedupes against existing issues and creates a Backlog issue for each deferred item that has no tracking issue, then comments the linkage. Deferral prose without the section posts a verification comment instead. When closing an issue without shipping, leave a comment with what was tried and why it was abandoned.
- Branch model: `main` is production (only via reviewed ship PRs); `develop` is the integration branch (repo default; workflow PRs and worktrees base on it); each issue works on its own branch (`archon/task-issue-<N>`) in an isolated worktree — issues in `Todo` run in parallel.
- New issues land in `Backlog`. Work never starts from creation or from `Backlog`: implementation begins only when an issue is moved into `Todo` (the board poller dispatches an Archon workflow; moving out and back in restarts). The poller also normalizes any board item without a status into the default lane (`Backlog`).
- While implementing an issue: work on a branch, keep the PR draft until ready.
- When the dispatched run completes, the poller marks the PR ready and **merges it into `develop`**, then moves the issue to `Ready for Review` — that is the signal to test the integration branch locally. (If the merge fails, the issue stays in `In Progress` and the poller logs why; drag it back to `Todo` to re-run.)
- When the human judges it working, move the issue to `In Review` with:
  `python3 automation/move_item.py <issue-number> "In Review"`
  The poller opens the ship PR (feature -> `main`), runs `archon-smart-pr-review` on it, and when the review run completes it **merges the ship PR into `main`** and moves the issue to `Done` automatically. (Failed reviews are logged and left in `In Review`.)
- **Ship conflicts auto-fix:** a ship PR that cannot merge (conflicts vs `main`) is handled without a human: the poller first merges `main` into the branch via the GitHub merge API; on real conflicts it dispatches `archon-fix-ship-conflicts` (merges base, resolves, validates, pushes, comments), once per conflict episode. If the fix run finishes and the PR is still conflicting, the poller comments on the issue asking for manual help.
- Issue lifecycle: issues stay OPEN until the ship PR merges into `main`, when the poller closes them explicitly (GitHub's `Fixes` keyword auto-close only fires on default-branch merges, so it cannot close at `main`). If a develop PR's auto-close keyword closes one early, the poller reopens it after the develop merge.

## Asking the human (NEEDS INPUT)
- When you hit a decision only the human should make (license, naming, scope, real tradeoffs), STOP. Do not guess and do not create a PR.
- Post a comment starting with `NEEDS INPUT:` — the question, 2-3 concrete options, and the tradeoffs — and @mention `bradley-mankoff` (they get a GitHub email).
- Add the label: `gh issue edit <N> --add-label needs-input`
- Make no further changes. The run ends; the poller moves the issue to `Blocked` (no develop merge).
- The human answers on the issue and drags it back to `Todo`; the poller resumes the workflow in the same worktree with their answer in the comments. Continue from where you stopped.
- **Never use GitHub auto-close keywords (`fix`/`fixes`/`fixed`/`closes`/`resolves` + `#N`) in commit messages or PR bodies on develop PRs** — GitHub closes the issue the moment the branch merges into develop. Write `Issue: #N` instead; the issue closes automatically when the ship PR reaches `main`.
- After the PR merges, the issue is moved to `Done` by the poller; manual move also works:
  `python3 automation/move_item.py <issue-number> Done`
- Workflow dispatch is label-aware: `bug` -> `archon-fix-github-issue`; `feature`/`enhancement` -> `archon-idea-to-pr`; default -> `archon-fix-github-issue`. Overrides live in `automation/config.json`.
- The poller runs as a launchd agent (`com.bradley-mankoff.news-board-poller`); state in `automation/state.json` (gitignored). First poll after restart is a snapshot and dispatches nothing.

## Review stages (two, by design)

- **Readiness review — inside the implementation workflows, before the human sees anything** (`archon-fix-github-issue` runs smart review + self-fix + simplify; `archon-idea-to-pr` runs the 5-agent review block + fixes): the bar is “the human should not have to check whether it works, is complete, or matches the issue’s intent.” PRs are left draft; the poller merges them into `develop` and moves the issue to `Ready for Review` when the run completes (unless the run ended with a `NEEDS INPUT` question — then the issue moves to `Blocked`, no develop merge, until the human answers and drags it back to `Todo`), and the human tests the integration branch from there.
- **Quality review — the In Review lane** (`archon-smart-pr-review`): after the human judges the feature working and moves the ticket to In Review, the poller opens the ship PR (feature -> `main`); the review targets code quality, conventions, and subtle/peripheral breakage, auto-fixing CRITICAL/HIGH findings, then the poller merges the ship PR into `main` and moves the issue to `Done`.
- Not redundant by design: the second review runs on the diff *after* human testing and feedback; the first one guarantees the diff is worth the human's time. (Human changes between the two make the second review see a different diff.)
- Only the pi-usable Archon workflows are installed; claude-only ones are archived (see `docs/archon-workflows.md`).
