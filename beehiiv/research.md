# Beehiiv Research

Compiled 2026-07-06 from the beehiiv developer docs, the beehiiv help
center, and third-party write-ups (Medium, Reddit, blog reviews).

## 1. The Create Post / Send API is Enterprise-only

**This is the single most important fact for getting started.** The
`POST /v2/publications/{publication_id}/posts` endpoint, and the broader
"Send API" it belongs to, is restricted to Enterprise plans and is
currently in beta.

- General API access (subscribers, posts *read*, custom fields, webhooks)
  is available on Launch (free), Scale, and Max.
- The Create Post endpoint is **not** on Launch. The Launch plan is the
  free plan and supports up to 2,500 subscribers with unlimited email
  sends, but the Send API is gated.
- To use the Send API, you need to be on Enterprise and typically have
  to contact beehiiv's success team for beta access.

Sources: beehiiv.com (Send API guide, June 2026 update; current pricing
page) and contemporaneous reviews (Medium, aigrowthguys.com, late 2025
and 2026). Earlier 2024 guides said the API was free on all plans, but
this has changed — current docs and pricing are consistent that the
Create Post endpoint is Enterprise-only.

Practical implication: a 14-day free trial is on the Launch (free) plan.
We cannot programmatically publish posts to beehiiv during the trial.

## 2. The Post Builder on the free plan

The Post Builder is the in-app editor beehiiv ships for composing posts
and emails. On the free plan:

- It accepts pasted text and renders it into the post body. The editor
  is designed to preserve most basic formatting from paste.
- It accepts **Markdown directly**: typing `*text*` for italic,
  `**text**` for bold, `#` for headings, etc. applies formatting
  inline. Pasting Markdown "generally works" with occasional minor bugs
  around inline HTML and linked images.
- The **HTML Snippet** block — the only way to insert arbitrary HTML
  into a post — is a **Premium** block. It is not available on the
  Launch (free) plan.

Sources: beehiiv.com help center (Compose / Post Builder, AI writing
assistant, "How to use the HTML Snippet block"). Third-party reviews
confirm the HTML Snippet is gated to paid plans (marketermilk,
emailvendorselection, latteinsights).

## 3. No email-to-post / forwarding import

beehiiv does **not** expose a feature to forward an email to a special
address that creates a draft post. Import paths are:

- Subscriber import (CSV or paste list) — for the audience, not posts.
- Content import from Substack, WordPress, Ghost, Mailchimp — these
  import historical posts. The pipeline is none of these, so this
  path is also out.
- Manual paste into the Post Builder — the only path that works for us.

Source: beehiiv.com help center (Compose, Content Import tool). No
mention of an email-to-draft feature in either the docs or third-party
walkthroughs.

## 4. Constraints even if you upgrade

Even on a paid plan, the Post Builder has structural limits worth
knowing:

- The email `<head>` is fixed by beehiiv. The CAN-SPAM / GDPR footer
  is mandatory and not removable. You cannot send a 100% custom HTML
  email that overrides these.
- `<style>` and `<link>` tags are stripped from post HTML; only
  inline styles are preserved.
- Custom CSS/HTML for the email header and footer can be set under
  `Style > Advanced > Email Header > Code` and `Style > Advanced >
  Email Footer > Custom` in the Post Builder.
- Iframes can appear in the *web* version of a post but not in the
  *email* version.
- Rate limit on the API: 180 requests per minute per organization.
- The API's `Create post` accepts either structured `blocks` (the same
  widgets available in the UI — paragraphs, headings, images, buttons,
  polls, HTML snippets) or a single `body_content` string of raw HTML.
  Not both. `body_content` is wrapped internally as an HTML Snippet
  block, so it inherits the same sanitization rules.

These matter for a future API integration but not for the manual-paste
path during the trial.

## 5. What the pipeline already produces

The news pipeline already has two complementary report renderers (see
`news_pipeline/pipeline.py`):

- `build_report_body(...)` — text/Markdown form.
- `build_report_html(...)` — HTML form, used for the email body.

It also writes a `output/daily_outputs/latest_run.md` run-review
artifact (KPIs, settings, top stories) that is *not* the newsletter
itself. The newsletter text lives in the body of the email send.

For beehiiv manual paste, the useful input is the text/Markdown form of
the newsletter, **not** the run-review file. We need a small command
that writes just the body of the newsletter as a single clean
Markdown file the user can paste into beehiiv's Post Builder.
