# Beehiiv Decision Log

Decisions are append-only. Each entry is dated and states: decision,
context, why, and what we are explicitly not doing.

---

## 2026-07-06 — D-001: Path for the trial window

**Decision.** During the 14-day free trial, the only viable
publishing path is **manual paste into the beehiiv Post Builder**.
The pipeline will emit a clean, paste-ready Markdown file alongside
the existing email send. The user copies it into the editor.

**Context.** The Create Post (Send) API is Enterprise-only and in
beta. The free trial is on the Launch plan, so programmatic
publishing is not available. beehiiv has no email-to-post feature.
The free editor accepts Markdown natively. The HTML Snippet block
is premium and is not available on the free plan.

**Why not the alternatives.**

- *Build the API integration now.* The Send API is Enterprise-only,
  so any code we write cannot be exercised against a real publication
  during the trial. Building untestable integration code in a
  fast-moving project is the kind of speculative work we avoid.
- *Pivot to PDF upload.* beehiiv does not expose a PDF-to-post
  import. PDFs would have to be screenshotted into image blocks —
  worse fidelity than pasted Markdown.
- *Use a third-party automation tool (Zapier, Make, n8n).* These
  ultimately call the same Send API, so the same Enterprise gating
  applies. They add a moving part without unlocking anything new.

**What we are not doing (yet).** We are not writing the v2 API
client, not designing a `beehiiv` feature flag, and not splitting
the rendering pipeline into "email" vs "post" content trees. The
single newsletter body that already gets emailed is good enough for
paste into beehiiv during the trial.

---

## 2026-07-06 — D-002: Format is Markdown for the trial

**Decision.** The paste-ready artifact is a single Markdown file.

**Context.** Beehiiv's free editor renders Markdown natively. The
HTML Snippet block that would let us paste full HTML is premium.
The pipeline's `build_report_body` already produces the newsletter
in a Markdown-friendly form.

**Why not HTML.** The free plan's editor will not let us drop in
raw HTML — there is no HTML Snippet block on the Launch plan. The
workaround (paste into the rich text editor and let it re-encode)
loses fidelity and is fragile.

**Why not PDF.** beehiiv has no PDF import for posts.

**Note for later.** If we ever upgrade to a paid plan, the right
move is to use the HTML Snippet block and feed it the same
`build_report_html` output the email send already uses — single
source of truth, no rendering fork.

---

## 2026-07-06 — D-003: Pipeline does not change the email path

**Decision.** The current email send (to `bradley` and any other
configured recipients via `config/recipients.yaml`) stays in place.
The beehiiv artifact is an *additional* output, not a replacement.

**Context.** Two things matter here.

1. The user said this is exploratory ("I don't care about
   aesthetics much, except gains we can get via templates and/or
   vibe coding"). The trial is for evaluating beehiiv, not for
   committing to it.
2. beehiiv becomes the audience-facing delivery channel; the email
   becomes a private review surface. That is a useful split during
   the trial: the user can keep receiving the report by email while
   evaluating whether beehiiv is the right public home for it.

**What we are not doing.** We are not removing the email send, not
gating it on a flag, and not adding a "do not email" recipient
scope for this trial. The simplest possible change.

---

## 2026-07-06 — D-004: Defer the API integration

**Decision.** We are explicitly deferring the beehiiv v2 API
client until one of the following is true:

- We upgrade to Enterprise and get Send API beta access.
- beehiiv expands the Send API to Scale or Max plans.
- We hit a manual-paste volume or fidelity ceiling during the
  trial that makes the manual path untenable.

**Context.** Building integration code that cannot run against
the real publication produces untested glue. The compounding cost
of untested glue is well above the cost of paste for one post per
day for fourteen days.

**When this decision is revisited.** If the user upgrades or
hires a recipient count large enough to justify Enterprise, this
decision is reopened. Tracked in `plan.md`.
