# Deferred-Work Guard — Design Spec

Status: proposed (2026-08-02)
Audience: the human reviewing the plan before/alongside the implementation.

## Problem

When an implementation run defers work ("later", "out of scope", "not now"),
nothing guarantees that the deferred item becomes a tracked issue. Issue #24
("Decide initially supported runtime matrix") explicitly deferred
`llama.cpp`/GGUF enablement — it was recorded in ADR 0010 and mentioned in the
resolution report's Summary — but no follow-up issue was created, and no
mechanism checked whether one already existed. The human caught it at
Ready for Review.

Two independent gaps:

1. **Detection** — the completion record is free-form; nothing forces a
   deferral mentioned anywhere in the run (ADR, report prose, scope limits,
   skipped/blocked review findings) to surface as a structured item.
2. **Execution** — even when follow-ups are *suggested* (text in a comment),
   nothing searches for an existing issue, creates one, or puts it on the
   board.

## Design overview

Two cooperating parts, both following patterns already established in this
repo after commit `b2b8d82`:

- **Workflow side (contract):** the machine-local `completion-comment` nodes
  (added to `archon-fix-github-issue` / `archon-idea-to-pr` in the "Local
  edits to usable workflows" pattern, documented in `docs/archon-workflows.md`)
  must emit a machine-parseable `## Deferred work` section in the completion
  comment. The LLM does the *detection* (it reads the artifacts, ADRs, scope
  limits, and review reports — the part regex cannot do reliably).
- **Poller side (execution):** `board_poller.py` parses that section
  deterministically at run completion, before the issue moves to
  Ready for Review. For each item it dedupes against existing issues, creates
  the issue if missing, puts it on the board in the default (Backlog) lane,
  and comments the linkage on the source issue. A fallback path warns the
  human when deferral language appears without the structured section — the
  exact #24 failure class — but never auto-creates an issue from fuzzy prose.

Division of labor: the LLM only fills a structured field (reliable); the
poller guarantees search/create/link/board (deterministic, repo-versioned,
tested). Covers both implementation workflows (fix-github-issue, idea-to-pr)
at once.

## The contract — `## Deferred work` section

Posted inside the completion-comment on the issue (same comment as
`## What shipped` / `## Decisions` / `## Acceptance criteria`), exactly:

```markdown
## Deferred work
- **Title:** Add llama.cpp/GGUF backend support
  **Description:** Port the model layer to llama.cpp so GGUF models run cross-platform.
  **Reason:** Packaging and driver work beyond the runtime-matrix decision.
  **Label:** feature
- **Title:** Extract shared readiness helper
  **Description:** Pull the two readiness loops into one helper.
  **Reason:** Cleanup; both loops work today.
```

Rules (stated in the node prompt, enforced by the parser):

- **Title:** imperative, ≤72 chars, self-contained (searchable on its own).
- **Description:** 1–2 sentences; what "done" looks like.
- **Reason:** why deferred now (optional in parsing, required by the prompt).
- **Label:** optional; must already exist in the repo (e.g. `feature` →
  dispatches `archon-idea-to-pr` when the issue reaches Todo). Applied only if
  it exists; otherwise ignored.
- One bullet per deferred item. If nothing was deferred, the section contains
  exactly `*None.*`.
- **The rule that closes the detection gap:** anything deferred *anywhere* in
  the run — ADR, report prose, `NOT Building`/scope exclusions that are future
  work, skipped/blocked review findings that warrant follow-up — MUST appear
  here. Mentioning a deferral without listing it is a contract violation.

Parser behavior (`parse_deferred_work` in `board_poller.py`):

- Section = text after a `## Deferred work` heading (case-insensitive) up to
  the next `## ` heading.
- `*None.*` / empty / absent → `[]`.
- Items = lines starting `- **Title:**`; indented `**Field:** value` lines
  (Description / Reason / Label) attach to the current item. Items with an
  empty title are skipped.

## Poller algorithm

New pure functions (unit-tested, no gh access):

- `parse_deferred_work(body) -> list[dict]` — the contract parser above.
- `normalize_title(title) -> str` — casefold + strip non-alphanumerics.
- `dedupe_deferred(item, open_issues, closed_issues) -> (action, ref)`:
  - exact normalized-title match in **open** issues → `("link", n)`
  - exact normalized-title match in **closed** issues → `("create-ref", n)`
    (a new issue referencing the closed one — the work still needs tracking)
  - no match → `("create", None)`
- `has_deferral_language(body) -> bool` — conservative regex over
  `defer*`, `out of scope`, `not in scope`, `follow-up`, `for later`.

New gh-facing function `reconcile_deferred_work(...)`, called in the
completion-reconciliation branch of `poll()` (between the develop merge and
the move to Ready for Review):

1. Skip when `deferred_work.enabled` is false, or when the state marker
   `rec["deferred_handled"]` equals the current dispatch message (a fresh
   re-dispatch creates a new message → the guard re-runs; the dedupe keeps it
   idempotent).
2. Read the issue comments; parse the **newest** comment containing a
   `## Deferred work` section. (Old comments from earlier runs may be
   re-parsed after a re-dispatch; dedupe turns that into harmless link
   comments, never duplicates.)
3. Items found → for each: `dedupe_deferred` against the repo's open and
   closed issue titles (fetch once, `--limit 200` — this repo is small).
   - `create`: `gh issue create` with title, body (description, reason,
     source issue + PR context), optional label (only if it exists in the
     repo); then `gh project item-add` + move to the default lane (Backlog)
     via the existing `move_to_lane` (DRY_RUN-aware).
   - `link` / `create-ref`: no creation; record the reference.
   - Post ONE aggregate comment on the source issue:
     `Deferred work from this run:` followed by one line per item —
     `**<title>** → #N (created, Backlog)` | `**<title>** → already tracked in #N` |
     `**<title>** → #N (created; supersedes closed #M)`.
4. No items, but `fallback_warn` is on and the **newest** comment contains
   deferral language → post once (marker `rec["deferred_warned"]`):
   a verification comment asking the human to check whether a follow-up issue
   exists. No auto-creation from prose.
5. Set `rec["deferred_handled"] = <dispatch msg>` only when every step
   succeeded. On any gh failure: log, leave the marker unset, and leave the
   item in In Progress (dispatch message intact) so the guard retries on the
   next poll — same retry philosophy as the develop-merge failure path. The
   guard never blocks a successful merge, and dedupe makes retries safe.

`--dry-run`: log every intended mutation (`[dry-run] DEFERRED CREATE …`),
perform none, return success — consistent with the existing dry-run contract
(no dispatch, no lane moves, no comments, no merges, no state write).

## Config (`automation/config.json`)

```json
"deferred_work": {
  "enabled": true,
  "fallback_warn": true
}
```

Lane for created issues = existing `default_lane` (Backlog). No new labels are
created automatically.

## Files touched

| File | Change |
|---|---|
| `automation/board_poller.py` | pure functions + `reconcile_deferred_work()` + hook in the completion branch |
| `automation/config.json` | `deferred_work` block |
| `tests/test_board_poller.py` | tests for parse / normalize / dedupe / language scan |
| `~/.local/share/archon-pi/archon-home/workflows/archon-fix-github-issue.yaml` | `completion-comment` node: add `## Deferred work` contract (machine-local; re-apply after archon reinstalls per `docs/archon-workflows.md`) |
| `~/.local/share/archon-pi/archon-home/workflows/archon-idea-to-pr.yaml` | same |
| `docs/archon-workflows.md` | note the section in the Local-edits list |
| `AGENTS.md` | route line + one board-protocol bullet |
| `docs/deferred-work-guard.md` | this spec |

## Tests

Unit tests only for the pure functions (repo convention: `unittest`,
`tests/test_board_poller.py`):

- `parse_deferred_work`: full section, `*None.*`, absent, empty, malformed
  item skipped, section terminates at the next `## ` heading.
- `normalize_title` + `dedupe_deferred`: open match → link; closed match →
  create-ref; no match → create; case/punctuation-insensitive matching.
- `has_deferral_language`: matches `explicitly deferred`, `out of scope`,
  `follow-up`; does not match clean text.

## Rollout / ops

1. Commit on `develop` (this poller is repo-versioned; the machine-local
   workflow edits are documented for re-application).
2. Restart the launchd agent so the running poller picks up the new code:
   `launchctl kickstart -k gui/$(id -u)/com.bradley-mankoff.news-board-poller`.
3. Watch `automation/board_poller.log` for `DEFERRED` lines on the next run
   completion.
4. After any archon reinstall, re-apply the workflow YAML edits (the
   `docs/archon-workflows.md` Local-edits section is the checklist).

## Edge cases and non-goals

- **Re-dispatch / multiple runs per issue:** new run → new dispatch message →
  guard re-runs; dedupe links existing issues; no duplicates.
- **Guard failure mid-way:** retried next poll; dedupe keeps it safe; the
  issue still moves to Ready for Review (guard failure only holds the item in
  In Progress, it does not block the merge that already happened).
- **Deferral mentioned in prose only (the #24 failure class):** fallback
  warning comment, once per run — the human decides; no junk issue.
- **Auto-`Depends on`:** deliberately NOT added — a deferred item does not
  necessarily depend on its source issue. The source issue is referenced in
  the body as context only.
- **Splitting a "major project" deferral:** the section supports multiple
  items; splitting large deferred projects into sub-issues stays a human
  planning decision at Todo time.
- **Non-goals:** no poller-side LLM extraction (no `completion` calls), no new
  labels, no changes to the review lane or ship flow.
