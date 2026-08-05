# ADR 0012: Desktop-first application with optional delivery

Status: Accepted

Date: 2026-08-04

## Context

The Daily News Report and its email delivery are coupled in one flow:
`run_daily_news` always calls `maybe_email_report` (pipeline.py:4959) after
rendering. Delivery is only implicitly optional — it skips when SMTP env vars or
recipients are absent (pipeline.py:2998–3011) and surfaces as a progress detail
line, never as a recorded delivery status. Run history stores a single status
derived from pipeline events (`completed`/`failed`/`aborted`, from
`run_status_from_events`, diagnostics.py:977–983), so "report built but not
delivered" and "report built and delivered" are indistinguishable.

Recipient policy is scattered across `NEWS_PRIMARY_RECIPIENT`,
`NEWS_RECIPIENT_SCOPE` (config.py:107–109), `NEWS_EMAIL_RECIPIENTS`, SMTP env
vars (SETTINGS.md:114–117), and `config/recipients.yaml` (`pause: true`), with
no explicit "delivery disabled" state and no canonical vocabulary for the
product surface, delivery policy, or automation. The product should be a
desktop-first Daily News Application where the generated report is complete and
reviewable without any email configuration, and email is an optional delivery
channel governed by an explicit Delivery Profile whose outcome is recorded
separately from the Run Session outcome.

## Decision

### Vocabulary

- **Daily News Application**: the product surface. It supports local desktop
  review of the generated report and optional automation; it is desktop-first,
  with the report rendered for review before any delivery step.
- **Run Session**: one execution of the daily news run (unchanged). It owns the
  run's config snapshot, output paths, progress, diagnostics, run logs, managed
  model server lifecycle, artifacts, and durable history.
- **Daily News Report**: the generated artifact. A completed report is complete
  and reviewable even when no delivery is configured.
- **Delivery Profile**: an optional policy controlling whether to send, the
  owner recipient, additional recipients, and transport configuration.
- **Automation**: a scheduled Run Session with an optional Delivery Profile.

### Outcome separation

Run Session/report outcome and Delivery Profile outcome are two independent
dimensions.

Run Session outcome is the existing `run_status_from_events` vocabulary
(diagnostics.py:977–983): `completed`, `failed`, `aborted`.

Delivery Profile outcome per delivery attempt is:

- `skipped: not_configured` — missing sender, recipient, or transport
  configuration.
- `skipped: user_disabled` — explicit no-delivery, e.g. a `pause: true`
  recipient or a disabled profile.
- `sent` — send success.
- `failed` — send rejection or error.

The delivery outcome NEVER changes the Run Session outcome. Missing or rejected
email is surfaced as delivery status, not as a failed run.

### Manual runs

Manual runs require no sender, recipient, or SMTP configuration and remain
reviewable in the desktop application. A run succeeds and produces a complete,
reviewable report with zero delivery configuration.

### Scheduled default and precedence

Scheduled personal use defaults to sending only to the owner recipient.
Additional recipients are explicit opt-ins for a run or a schedule. Precedence:
run-level explicit opt-ins > schedule-level opt-ins > owner-only default.
`NEWS_RECIPIENT_SCOPE=all` maps to the legacy opt-in list (owner plus all
active `config/recipients.yaml` entries).

### Identity rules

An email sender MAY equal the owner recipient (the same address may serve both
roles). Sender identity belongs to transport configuration, not to the Daily
News Report domain.

### Placeholder rule

Placeholder addresses (`you@example.com`, `primary@example.com`,
`news@example.com` — the checked-in defaults in `config/recipients.yaml` and
SETTINGS.md:114) and placeholder credentials must never look like configured
personal delivery. Placeholder/default values mean `skipped: not_configured`,
never `sent`.

### Migration table

| Legacy | New vocabulary |
|---|---|
| `NEWS_PRIMARY_RECIPIENT` | Delivery Profile owner recipient |
| `NEWS_RECIPIENT_SCOPE=primary` | Owner-only delivery (default) |
| `NEWS_RECIPIENT_SCOPE=all` | Owner + all active `config/recipients.yaml` entries (legacy explicit opt-in list) |
| `config/recipients.yaml` (`email`, `name`, `pause`) | Delivery Profile additional recipients; `pause: true` ⇒ `skipped: user_disabled` |
| `NEWS_EMAIL_RECIPIENTS` | Legacy fallback recipient list (transport-adjacent; retained) |
| `NEWS_EMAIL_FROM` | Delivery Profile transport sender (may equal owner) |
| `NEWS_SMTP_HOST`/`NEWS_SMTP_PORT`/`NEWS_SMTP_USERNAME`/`NEWS_SMTP_USE_SSL`/`NEWS_SMTP_PASSWORD` | Delivery Profile transport configuration |
| `NEWS_UNSUBSCRIBE_BASE_URL`/`NEWS_UNSUBSCRIBE_HOST`/`NEWS_UNSUBSCRIBE_PORT`/`NEWS_UNSUBSCRIBE_SECRET` | Delivery Profile transport unsubscribe configuration |
| `maybe_email_report` implicit skip (pipeline.py:2998–3011) | Explicit Delivery Profile evaluation with recorded delivery status (Slice B) |

### Follow-up slices

- **Slice A — Desktop-first local report review**: the Daily News Application
  renders the report for desktop review (HTML preview plus `latest_run.md`
  review); runs succeed with zero delivery configuration; report layout is
  authored desktop-first with email as a derived, constrained rendering.
- **Slice B — Owner-first delivery**: Delivery Profile data model; delivery
  status recorded per run (`skipped`/`sent`/`failed`/`user_disabled`)
  separately from run status; owner-only default; explicit opt-ins; placeholder
  detection.
- **Slice C — Daily automation**: scheduled Run Sessions with an optional
  Delivery Profile; owner-only default; explicit opt-ins.

## Consequences

- Report generation and delivery stop sharing one success path: a broken or
  absent delivery setup never fails a run, and delivery outcomes are recorded
  distinctly from run outcomes.
- The desktop application becomes the canonical render target; the email HTML
  (`build_report_html`, pipeline.py:3767–3779, mobile-first 600px media query)
  is a constrained derived rendering, not the primary surface.
- The UI control panel already trends desktop-first (ui.py:1269
  `max-width: min(1180px, ...)`, ui.py:1284 780px media query) — this decision
  makes that direction load-bearing for the whole product.
- Legacy settings keep working through the migration mapping until the
  follow-up slices implement the Delivery Profile.
- Delivery decisions (outcome states, precedence, identity, placeholders)
  cannot be re-litigated without new evidence.
