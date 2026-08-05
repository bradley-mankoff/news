# Session Handoff — Local Dev-Loop Automation (board poller → develop sync → UI refresh)

## Goal
Automate the local develop dev loop so that when the board poller merges a PR into
`develop` (server-side, via the GitHub API), the **local** develop checkout is refreshed
and the local control-panel UI is restarted automatically — the user's "merge lands, then
I check it on develop locally" flow should need zero manual steps. Also investigated and
fixed the incident that motivated this: the UI had been serving stale code for two days.

This session **implemented and shipped** the automation. Everything below is current and
verified; a follow-up agent should be able to continue from here without re-deriving context.

## Constraints & Preferences
- Repository policy: correctness first, reuse existing conventions, avoid unnecessary
  abstractions, no redundant documentation. The board poller is **Python stdlib only**
  (no new dependencies allowed).
- The poller is **never destructive to git state**: no forced updates, no resets, no
  automatic merges of divergent local work. Unpushed local commits / dirty trees are
  human decision points — skip with a logged reason.
- `develop` = integration branch (repo default, workflow base); `main` = production
  (reviewed ship PRs only, verdict-gated). Board protocol lives in `AGENTS.md`.
- UI restarts may interrupt an in-flight form in the control panel — accepted tradeoff,
  the whole point is "refresh shows the new code".
- The user chose, after being asked, to **push local develop as-is** (bypassing the PR
  flow for the previously-unpushed commits) to unblock the sync.

## Progress
### Done
- [x] Diagnosed the stale-UI incident (root cause, see Critical Context).
- [x] Pulled local `develop` up to date (13 commits behind) by merging `origin/develop`
      into the divergent local branch; resolved the one conflict (docstring-only in
      `automation/board_poller.py` — both sides' intent kept).
- [x] Restored the missing `news` console-script entrypoint in `.venv` (`uv pip install -e .`).
- [x] Restarted the UI on current code and verified the HF model catalog is served
      (3 entries via `/api/schema`).
- [x] Designed + implemented `sync_local_develop()` in `automation/board_poller.py`:
      fetch → fast-forward-only merge → UI restart, with strict skip boundaries.
- [x] Wired it at **two** call sites: after every successful develop merge (inside
      `poll()`, before the deferred-work guard and the lane move to Ready for Review),
      and once at poller startup (catches merges that landed while the agent was down).
- [x] Added 10 boundary tests in `tests/test_board_poller.py`
      (`SyncLocalDevelopTest`); full suite green: **103 poller tests, 438 total**
      (35 subtests) in ~12s.
- [x] Committed as `cd2f018` ("Board poller: sync local develop + restart UI after
      develop merges") on top of the local merge commit `7362001` and the user's two
      previously-unpushed commits (`afb2704`, `b9e6eaf`).
- [x] Redeployed the poller via `automation/deploy.sh` (the documented path — applies
      archon workflow edits + `launchctl kickstart -k`).
- [x] Pushed local `develop` to origin as the user chose: `76b61d0..cd2f018`.
      Local is now in sync with `origin/develop` (clean tree, no ahead/behind).
- [x] Live-verified both observable paths in `automation/board_poller.log`:
  - Skip boundary: `LOCAL SYNC SKIP: local develop has 4 unpushed commit(s); sync blocked until they are pushed (fast-forward only)` (pre-push)
  - Happy no-op: `LOCAL SYNC: develop already up to date` (post-push, after `launchctl kickstart -k`)

### In Progress
- [ ] None.

### Pending
- [ ] None outstanding from this session. The only decision was the push choice, made by the user.
- [x] Decide the public project and package name (`news-pipeline`, ADR 0009).
- [x] Choose a project license. Apache-2.0 chosen by owner on issue #21 (ADR 0010); AGPL-3.0 rejected (network copyleft).
- [x] Audit repository history for secrets and personal data before making the repository public — see `docs/security/audit-2026-08-02.md` and the gated scrub runbook `docs/security/history-scrub.md`.
- [x] Replace real recipients, personal email defaults, and personal filesystem paths with safe examples — tree sanitized (issue #22); history scrub remains gated behind the runbook.
- [x] Decide the initially supported runtime matrix (ADR 0010, Accepted): MLX/MLX-VLM on Apple Silicon + external OpenAI-compatible endpoints; `llama.cpp` adapter deferred.
- [x] Implement a Prompt Catalog and built-in Prompt Profiles.
- [x] Refactor hard-coded prompt construction to compose editable editorial instructions with pipeline-owned protocol requirements.
- [x] Prompt-profile selection and per-stage prompt editing in the normal UI (PR #70, issue #27).
- [x] Add `huggingface-hub` as a direct dependency (declared in `pyproject.toml`; API usage is tracked under the Model Catalog item above).

## Key Decisions
- **The poller owns the sync, not a watcher or git hooks.** Hooks are a dead end
  (merges happen server-side via `gh pr merge`, no local git event fires); a separate
  watcher duplicates polling machinery for no benefit. The poller already knows the exact
  merge moment and runs as a launchd agent with the repo checked out.
- **Fast-forward only, and only when safe.** Skip (never force) when: tree is dirty, HEAD
  is not `develop`, or local `develop` has unpushed commits (pre-detected via
  `git rev-list --count origin/develop..HEAD`). Auto-merging divergent local work would
  silently create merge commits and churn every cycle — that is a human decision.
- **Restart the UI only if it is running** (TCP probe of 127.0.0.1:8766). If it is not
  running, the next manual start picks up fresh code anyway. Never surprise-start it.
- **Restart via `.venv/bin/news` directly, not `uv run news`.** `uv run` proved flaky this
  session (failed to spawn `news`/`pytest` even though the environment was "checked");
  plain invocation is one less failure mode. Restart failure is loud: logged as
  `LOCAL SYNC WARNING` with the log path, never fatal to the poll loop.
- **Commit-before-deploy was required**: the sync skips on a dirty tree, so the feature
  could not work while `board_poller.py` was modified-but-uncommitted.
- **Startup sync covers downtime**: `main()` runs `sync_local_develop()` once before the
  poll loop (skipped under `--dry-run`), so merges landing while the machine/agent was
  down are caught on restart. Idempotent — up-to-date is a no-op.

## Critical Context
- **Repo/state**: local `develop` at `cd2f018`, in sync with `origin/develop`, clean tree.
  Local history: `cd2f018` (sync feature) → `7362001` (merge of origin/develop) →
  `afb2704`, `b9e6eaf` (user's deferred-work dedupe commits) → `76b61d0` (origin).
- **Poller runtime**: launchd agent `com.bradley-mankoff.news-board-poller`
  (plist `~/Library/LaunchAgents/com.bradley-mankoff.news-board-poller.plist`,
  WorkingDirectory = repo root, `KeepAlive` true, `ThrottleInterval` 10s).
  Poll cadence: `poll_interval_seconds` = 45 in `automation/config.json`.
  Log: `automation/board_poller.log` (stdout/stderr redirect). State: `automation/state.json`
  (gitignored). A **second, unrelated** agent `com.bradley-mankoff.yugioh-ebay-board-poller`
  runs the same framework copy-pasted into `~/personal_code/yugioh_ebay` — don't confuse them.
- **Poller structure** (`automation/board_poller.py`, current line numbers):
  - `sync_local_develop()` — line 191; helpers `_port_open` 156, `_ui_running` 165,
    `_restart_ui` 169; constants `UI_HOST`/`UI_PORT`/`UI_LOG_PATH` 151–153.
  - Post-merge call: line 1297, inside `poll()`, gated on `merge_ok`, right after the
    `DEVELOP MERGE` log and before the deferred-work guard (`reconcile_deferred_work`)
    and the `Ready for Review` lane move.
  - Startup call: line 1541 in `main()`, gated `if not DRY_RUN`.
  - Develop merges happen via `gh pr merge` (`merge_pr_to_base`, line 528) — server-side,
    which is why the local checkout never learned about them (the root-cause insight).
- **UI runtime**: `news ui` = stdlib `http.server` (`serve_ui`/`NewsUIServer` in
  `news_pipeline/ui.py` ~line 2719) — no reload mechanism; refresh requires process restart.
  Serving at http://127.0.0.1:8766. Log: `/tmp/news-ui.log`. Stop: `pkill -f "news ui"`.
- **Redeploy path** (documented in AGENTS.md): `automation/deploy.sh` — idempotent,
  re-applies archon workflow edits (`automation/apply_workflow_edits.py`) then
  `launchctl kickstart -k gui/$(id -u)/com.bradley-mankoff.news-board-poller`. Always use it
  after editing the poller — the running process holds old code in memory.
- **Verification commands that work in this repo**:
  - Tests: `.venv/bin/python3 -m pytest tests/ -q` — plain `uv run pytest` / `uv run news`
    fail to spawn intermittently ("Failed to spawn") even when the venv is healthy.
  - Board: `gh project item-list 1 --owner bradley-mankoff --format json`
    (note: the `status` field is a plain string like `"Done"`, not an object — a jq on
    `.status.name` fails).
  - UI schema endpoint is `/api/schema` — `/schema` returns `{"error": "Not found."}`.
  - `pgrep -f board_poller | head -1` picks the *lowest* PID — that was the yugioh agent,
    not the news one; always verify with `ps -p <pid>`.
- **Incident timeline (2026-08-03)**: user reported HF model catalog absent from the
  develop UI after refresh. Cause: UI process started Aug 1 15:32 from a local `develop`
  13 commits behind origin (HF work merged Aug 2: PR #73 issue #30 model catalog, PR #68
  issue #32 HF links, PR #69 issue #26); API-side merges never touch the local checkout,
  so no browser refresh could ever help. Secondary finding: local `develop` had 2
  unpushed user commits (`afb2704`, `b9e6eaf`), which blocked `git pull` (diverged) —
  the exact case the new skip boundary handles.
- **Pre-existing log noise**: occasional `poll error: graphql failed ... connection reset`
  lines are transient GitHub network errors; the poll loop recovers by design
  (3 consecutive failures → `POLLER STUCK` line). Also seen: `SHIP CONFLICT DISPATCH/RESOLVED`
  for issue #23 — normal ship-conflict auto-fix flow, unrelated to this session.
- **Feature behavior matrix** (each outcome logs one line):
  | Condition | Result |
  |---|---|
  | `--dry-run` | no-op, "dry-run" line |
  | fetch fails | `LOCAL SYNC FAILED: git fetch: <stderr>` |
  | not on `develop` | `LOCAL SYNC SKIP: not on develop (branch=…)` |
  | dirty tree | `LOCAL SYNC SKIP: working tree dirty (N file(s))` |
  | unpushed local commits | `LOCAL SYNC SKIP: … N unpushed commit(s); sync blocked until they are pushed` |
  | up to date | `LOCAL SYNC: develop already up to date` |
  | ff-only merge fails | `LOCAL SYNC FAILED: fast-forward merge: <stderr>` |
  | merged, UI not running | `LOCAL SYNC: develop updated (…); UI not running, left as is` |
  | merged, UI restarted | `LOCAL SYNC: develop updated (…); UI restarted` |
  | merged, restart failed | `LOCAL SYNC WARNING: … UI restart failed - see /tmp/news-ui.log` |
- **Test coverage**: `SyncLocalDevelopTest` (10 tests) fakes `subprocess.run` per git
  subcommand via a dispatch plan; `rev-list` plans are sequence-aware (ahead check runs
  before behind check — the test plan must be a list of two lambdas). Patterns to copy:
  `patch("automation.board_poller.subprocess.run", side_effect=…)`,
  `patch("automation.board_poller._ui_running" / "_restart_ui", return_value=…)`.
- This `HANDOFF.md` replaces the prior session's handoff (prompt-first UI analysis);
  the old content is recoverable from git history.

## Next Steps
1. **Watch the first real merge** end-to-end: when the next develop PR merges, expect
   `DEVELOP MERGE …` then `LOCAL SYNC: develop updated (…); UI restarted` in
   `automation/board_poller.log`, and confirm the UI (http://127.0.0.1:8766) serves the
   new code. This is the only path not yet observed live (skip + up-to-date paths were;
   merge+restart is unit-tested and the manual equivalent was exercised during the incident).
2. **Keep local `develop` clean and pushed.** Any unpushed local commit or dirty tree
   silently disables the sync (by design, with a log line). If the user wants to work
   directly on develop again, they must push or the automation stays blocked.
3. **Watch the venv drift**: the `news` entrypoint vanished from `.venv/bin` once while
   uv reported the environment healthy. If the UI restart starts failing, check
   `ls .venv/bin/news` and re-run `uv pip install -e .`. Root cause was never determined.
4. Optional: mention the auto-sync in `README.md`/AGENTS.md dev-loop docs if the user
   wants the behavior documented for humans (not done this session — no doc changes were
   requested beyond this handoff).

