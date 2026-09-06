# ADR 0013: Local daily automation uses a per-user launchd agent

Status: Accepted

Date: 2026-08-13

## Context

ADR 0012 defined Daily Automation (Slice C) as scheduled Run Sessions with an
optional Delivery Profile, owner-only default, and explicit opt-ins, but it did
not choose a scheduling mechanism. The desktop application needs one durable,
user-owned daily schedule that survives UI restart without a resident UI
process, a hosted scheduler, root privileges, or a second report/history store.
The repository's `automation/` directory is reserved for GitHub-board
automation and must not become the product scheduler.

## Decision

Daily Automation is implemented as exactly one per-user macOS LaunchAgent plus
one atomic local JSON schedule record:

- **Fixed identity**: one LaunchAgent label
  `com.bradley-mankoff.news-daily-run` with a generated plist at
  `~/Library/LaunchAgents/com.bradley-mankoff.news-daily-run.plist` using
  `plistlib`, argv-based `ProgramArguments`
  (`<absolute python> -m news_pipeline.cli schedule run`), absolute
  `WorkingDirectory`, `StartCalendarInterval: {Hour, Minute}`, `RunAtLoad:
  false`, and no `KeepAlive`. It is loaded in the per-user `gui/<uid>` domain
  through bounded `launchctl bootstrap`/`bootout`/`print` subprocess calls.
- **One schedule record**: `~/.config/news/daily_schedule.json` stores the
  validated local time, saved Run Preset ID, canonical delivery mode
  (`disabled|owner|recipients`, default `owner`), a safe non-secret
  environment snapshot, fixed paths, and a bounded last-run projection. Writes
  are atomic (sibling temp file plus `os.replace`) with `0600` file / `0700`
  directory permissions, serialized by an advisory file lock.
- **Secret boundary**: credentials and API keys
  (`NEWS_SMTP_PASSWORD`, `NEWS_UNSUBSCRIBE_SECRET`, `NEWS_MODEL_API_KEY`, and
  any future secret-named setting) never enter schedule state, the plist,
  API responses, or logs. The existing ignored `env.json` password fallback
  remains the SMTP credential path. `NEWS_PRESET`/`NEWS_ACTIVE_PRESET` marker
  variables are never persisted; a schedule binds a preset by ID.
- **Trigger only**: `news schedule run` reads the enabled record, revalidates
  the preset, takes the schedule lock, rechecks enabled, applies the saved
  preset then the safe base environment then explicit overrides with the
  persisted delivery mode taking precedence, and calls the existing
  `run_pipeline()` in the foreground. It records only a bounded lifecycle
  projection (`running`, `completed`, `failed`, `interrupted`); Run Session,
  report, DuckDB/CSV history, OKF bundles, and Delivery Profile outcome remain
  the execution and persistence authorities.
- **Fail closed**: unsupported platforms or missing `launchctl` report
  `supported=false`/`launchd_status=unavailable`; bootstrap failure is never
  reported as healthy; corrupt or disabled state cannot trigger a run; a dead
  `running` PID reconciles to `interrupted` without inventing a report
  outcome.

## Consequences

- The desktop application can enable, update, and disable exactly one daily
  schedule without the UI staying open; launchd owns invocation.
- Scheduling adds no new dependency, no resident scheduler thread, no hosted
  service, no cron, no system LaunchDaemon, and no second report/history
  store. Multi-user, hosted, and non-email scheduling remain out of scope.
- Users must accept LaunchAgent user-session semantics: launchd runs the job
  for the logged-in user; a logged-out, asleep, or powered-off machine does
  not fire and does not queue a catch-up — the next eligible daily
  `StartCalendarInterval` window (`HH:MM` local time, once daily, shown as
  `HH:MM (local time, once daily)` in the Schedule tab and
  `news schedule status [--json]`) runs instead, or run
  `news schedule run` immediately. Model choice is consumer-owned (e.g.
  `gemma-4-12b-it-4bit` vs `qwythos-9b-*` vs `qwen3-*`; see
  `config/model_catalog.yaml` and Hardware Compatibility panel; mismatched
  `NEWS_MODEL_BACKEND` fails fast, OOM retries with a smaller alias) and
  delivery outcome stays independent (`skipped: not_configured` /
  `skipped: user_disabled` / `failed` never hide a report; delivery phase in
  `latest_run_details.json`; Schedule tab/`status --json` surface
  `enabled`/`loaded`/`not-loaded`/`unavailable` plus last run id/time/report/delivery
  without secrets).
- The schedule state and plist are operational projections, not history:
  disabling or removing a schedule never touches existing reports,
  DuckDB/CSV history, or OKF bundles.
- Product Daily Automation remains distinct from the repository's board
  automation under `automation/`; the two must not share scheduling or
  lifecycle code.
