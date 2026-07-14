# Beehiiv Plan

This file is the working plan for the trial window. It is rewritten
when decisions change. See `decisions.md` for the why.

## Goal

Validate beehiiv as a long-term publishing home for the daily news
report during the 14-day free trial, with zero speculative integration
work.

## Scope (trial)

1. Add a CLI command that writes a single paste-ready Markdown file
   under `output/beehiiv/`.
2. The file contains the body of the newsletter (the same content
   that is emailed) in Markdown form, suitable for paste into the
   beehiiv Post Builder.
3. The file is written as part of the normal `uv run news run` flow.
4. Email send is unchanged.
5. Document the manual paste workflow in `README.md` (short
   paragraph) and in `beehiiv/README.md` (step-by-step).

## Out of scope (trial)

- beehiiv API client. Deferred (see D-004).
- HTML fidelity work. Markdown is the format for the trial
  (D-002).
- Subscriber import. We start with no audience on beehiiv and
  the user's own email; the email send remains the recipient path.
- Templates, themes, or design passes on the beehiiv publication.
  The user explicitly de-prioritized aesthetics.

## Implementation sketch

The newsletter body already lives in the pipeline's `build_report_body`
function. The change is small:

1. Add `news beehiiv-paste` (or similar) CLI command that
   runs the same path as `news run` but writes a single
   `output/beehiiv/YYYY-MM-DD.md` instead of sending email.
   Implementation: extract the report-body rendering into a
   helper that can be called from both the email path and the
   new command, and add a new subcommand.
2. Also call the same helper from the normal `news run` flow
   so the file is written automatically each day alongside the
   email. This is the lowest-friction change: zero behavior
   change for email, one new file per run.
3. The Markdown file should include a header line with the
   beehiiv post title and the run date, plus a footer note that
   tells the user to paste into the Post Builder. The body
   itself is identical to the email body.

## Validation

- Run `uv run news run --preset <smoke>` and confirm
  `output/beehiiv/YYYY-MM-DD.md` is written.
- Confirm the file is paste-safe: no platform-specific inline
  styles, no images that need re-hosting, no broken links.
- Manually paste the file into the trial publication and
  preview the post. The trial publication URL is
  `bradleys-newsletter-0e10ac.beehiiv.com`.

## Reopen conditions

- The user upgrades to Enterprise or joins the Send API beta
  → build the v2 API client. `decisions.md` D-004 is the
  trigger.
- The user wants the email path to disappear → revisit D-003.
- The user wants the beehiiv output to look like the email
  visually → revisit D-002 (likely: upgrade to a paid plan and
  use the HTML Snippet block).
