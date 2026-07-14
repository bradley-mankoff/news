# Beehiiv

Working notes and decisions for the news project's beehiiv integration.

Trial publication: `bradleys-newsletter-0e10ac.beehiiv.com`
Trial window: 14-day free trial, currently active.

## Files

- `research.md` — what the beehiiv docs and the broader internet say about
  programmatic publishing, the Create Post API, the editor, and import paths.
- `decisions.md` — decision log: what we chose, why, what we deferred.
- `plan.md` — concrete next-step plan once the path is locked in.

## TL;DR

- beehiiv's **Create Post (Send) API is Enterprise-only and in beta.** A
  14-day free trial is on the Launch (free) plan, so programmatic publishing
  is **not available during the trial**, and would only open up if we
  upgrade to Enterprise (custom pricing, 100k+ subscribers) or get into
  the beta.
- There is **no email-to-post** feature, so we can't simply forward the
  current email to a beehiiv address to create a draft.
- The path that actually works on the free plan is **manual paste into
  the Post Builder**. The editor accepts Markdown natively. The "HTML
  Snippet" block is a **premium feature** and is not on the free plan.
- The pipeline already produces an HTML email body and a run-review
  Markdown. The only new work for the trial is a small "render for
  beehiiv paste" command that emits a clean Markdown file the user
  copies into the beehiiv editor.

## Daily Workflow

Every run of `uv run news run` writes a paste-ready Markdown file to
`output/beehiiv/YYYY-MM-DD.md` alongside the email send. The path is a
sibling of `output/daily_outputs/`, so the daily-output cleanup pass
never touches it. Empty runs (no report body) skip the write.

To publish on beehiiv:

1. Run `uv run news run` (or `uv run news run --preset NAME`).
2. Open `output/beehiiv/YYYY-MM-DD.md` — that is today's newsletter in
   beehiiv-friendly Markdown.
3. In beehiiv, go to **Posts → Start writing**. The first line of the
   file is the post title (rendered as `# Daily News Summary - DATE`);
   paste everything from the second line into the body. Beehiiv's editor
   will turn the Markdown headings, lists, and links into the rendered
   post. The Sources section at the bottom is part of the body.
4. Add a thumbnail and any tags in the Post Builder, then **Save as
   draft** or **Send** when ready.

The email send to `bradley@…` and any other configured recipients is
unchanged. The beehiiv artifact is an additional output, not a
replacement.

## What this code change actually does

`news_pipeline/run_finalizer.py` got a new method,
`_write_beehiiv_paste`, called from `finish()` after `_write_review()`.
It writes `self.report_body` (the same string the email body uses) to
`config.beehiiv_paste_dir / "<YYYY-MM-DD>.md"`. The production wiring
sets `beehiiv_paste_dir = output_dir.parent / "beehiiv"`, i.e.
`output/beehiiv/`. Failure is non-fatal: a write error emits a warning
and the run continues.

## Reopen conditions

- The user upgrades to Enterprise or joins the Send API beta → build
  the v2 API client. `decisions.md` D-004 is the trigger.
- The user wants the email path to disappear → revisit D-003.
- The user wants the beehiiv output to look like the email visually →
  revisit D-002 (likely: upgrade to a paid plan and use the HTML
  Snippet block).
