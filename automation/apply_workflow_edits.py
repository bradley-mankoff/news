#!/usr/bin/env python3
"""Idempotently re-apply the project's local edits to the archon workflows.

Restores the checklist in docs/archon-workflows.md ("Local edits to usable
workflows") after an archon reinstall replaces the bundled YAMLs:

  - `completion-comment` node (with the `## Deferred work` contract the board
    poller parses) in archon-fix-github-issue.yaml and archon-idea-to-pr.yaml
  - `report-verdict` node in archon-smart-pr-review.yaml
  - the full archon-fix-ship-conflicts.yaml (inline prompt node, no DB
    commands)

Safe to run repeatedly: checks marker strings, no-ops when already applied,
prints what changed. Exit 0 always.

Usage: python3 automation/apply_workflow_edits.py
       (or `automation/deploy.sh`, which also restarts the board poller)
"""

import os
import sys
from pathlib import Path

ARCHON_HOME = Path(os.environ.get("ARCHON_HOME",
                                  "~/.local/share/archon-pi/archon-home")).expanduser()
WORKFLOWS = ARCHON_HOME / "workflows"

# The Deferred-work contract shared by both completion-comment nodes. The
# board poller parses the `## Deferred work` section (docs: README, Project
# Automation -> Deferred work guard).
CONTRACT = """      Trace the ORIGINAL issue ask first (the issue body from the Inputs above):
      compare every acceptance criterion, described behavior, and named component
      in the issue body — including its **Out of scope** section — against what
      this run actually shipped:
      - anything NOT done and NOT in the issue's Out of scope section is an
        UNMET acceptance criterion, not deferred work: mark it
        `- [ ] <criterion> — <why not met>` in the Acceptance criteria section
        above. It is part of this issue's delivery — the human will send the
        issue back, and the poller will not create a tracking issue for it.
      - anything in the issue's Out of scope section that is future work (not
        "never") IS deferred work and MUST be listed here.
      DEDUPE — judge every item before finalizing the section:
      - check the codebase for an existing implementation of the item (search by
        domain concept, not wording — grep the module/API names). Already
        implemented -> `**Skip:** already implemented — <pointer>`. Never write
        built things to the out-of-scope KB.
      - read `.out-of-scope/*.md` (concept files: reasons + prior requests). An
        item matching a recorded concept -> `**Skip:** rejected — .out-of-scope/<file>`
        (do not re-litigate; the human can revisit by editing the file).
      - fetch the existing issues: `gh issue list -R bradley-mankoff/news
        --state open --limit 200 --json number,title,body` (and `--state closed`
        for the Supersedes check). Read each candidate's TITLE and INITIAL
        DESCRIPTION (the body — never the comments). Using the repo context
        (pending checklists, ADRs, this issue) decide per item:
        - an open issue already covers the SAME deliverable (the same work, not
          just the same topic or family) — including the issue being implemented
          when the deferred item is part of ITS remaining scope, and any
          pending-checklist/ADR item that already has a tracking issue:
          add `**Links to:** #N`.
        - a closed issue covered it (done or abandoned): add `**Supersedes:** #N`
          (the poller creates a new issue referencing it).
        - it is genuinely rejected — never-to-be-done, superseded by context:
          stamp `**Out of scope:** <kebab-concept-slug>` (the poller records it
          in `.out-of-scope/<slug>.md` so the same request does not resurrect).
        - otherwise leave the item bare — the poller creates the tracking issue.
      If you are unsure whether an issue covers the same deliverable, treat them
      as distinct (bare) — a new tracking issue is cheap and the human can merge.
      SIZE BAR — not every finding deserves a tracking issue. Apply the
      "would this ever be scheduled on its own?" test before stamping:
      - small chores, test tweaks, doc fixes, and cleanups that a competent dev
        would knock out in under ~an hour as part of the parent's follow-up are
        NOT tracking issues: stamp `**Skip:** folded into #<parent> — <why>`.
        The completion record preserves them; the backlog does not grow.
      - review findings whose fix belongs with the parent issue's remaining
        scope: same — Skip with the reason.
      - only items that are genuine, independently schedulable deliverables —
        features, subsystems, cross-cutting fixes, anything that gates other
        work — get spawned (left bare).
      Err toward Skip for small items (a backlog full of chores is worse than a
      tiny task picked up later); err toward bare only for real deliverables.
      Format, exactly — the poller parses this:
      - **Title:** <imperative title, ≤72 chars, self-contained and searchable>
        **Description:** <1-2 sentences; what "done" looks like>
        **Reason:** <why deferred now>
        **Label:** <optional; must already exist in the repo, e.g. enhancement>
        **Links to:** #N    (or **Supersedes:** #N / **Out of scope:** <slug> / **Skip:** <reason> — optional)
      If nothing was deferred, write exactly: *None.*
"""

FIX_NODE = """  - id: completion-comment
    prompt: |
      Post a structured completion record on the GitHub issue for this work, so the
      issue carries durable context (execution record) instead of dying with the PR merge.

      ## Inputs

      - Issue: $fetch-issue.output
      - Implementation record: read $ARTIFACTS_DIR/implementation.md
      - Validation record: read $ARTIFACTS_DIR/validation.md
      - PR: number from $ARTIFACTS_DIR/.pr-number (if present)

      ## Format

      Write the comment body to /tmp/completion.md and post with
      `gh issue comment <issue-number> --body-file /tmp/completion.md`.
      The body MUST have exactly these sections:

      ## What shipped
      The symbols, files, endpoints, and data shapes changed — substance, not run narration.
      No commit SHAs, no agent run details.

      ## Decisions
      Each real choice made during implementation, as `choice — why`. Open questions are
      NOT decisions; leave them out.

      ## Acceptance criteria
      For each criterion in the issue body: `- [x] <criterion> — <evidence>` or
      `- [ ] <criterion> — <why not met>`, where evidence is a test name, diff path, or
      command output. If the issue has no explicit criteria, list the criteria the work
      satisfied, with evidence.

""" + CONTRACT + """
      The comment must be factual; never claim a criterion is met without citable evidence.
    depends_on: [report]
    context: fresh
"""

IDEA_NODE = """  - id: completion-comment
    prompt: |
      Post a structured completion record on the GitHub issue for this work, so the
      issue carries durable context (execution record) instead of dying with the PR merge.

      ## Inputs

      - Implementation record: read $ARTIFACTS_DIR/implementation.md (or plan.md and
        validation.md if implementation.md is absent)
      - Review outcome: read $ARTIFACTS_DIR/review/consolidated-review.md if present
      - PR: number from $ARTIFACTS_DIR/.pr-number (if present)
      - Issue: the issue number this PR references, from the PR body or $ARGUMENTS
      - Original issue body: fetch it with
        `gh issue view <issue-number> --json title,body,labels` and read it —
        the deferred-work trace below compares against it.

      ## Format

      Write the comment body to /tmp/completion.md and post with
      `gh issue comment <issue-number> --body-file /tmp/completion.md`.
      The body MUST have exactly these sections:

      ## What shipped
      The symbols, files, endpoints, and data shapes changed — substance, not run narration.
      No commit SHAs, no agent run details.

      ## Decisions
      Each real choice made during implementation, as `choice — why`. Open questions are
      NOT decisions; leave them out.

      ## Acceptance criteria
      For each criterion in the issue body: `- [x] <criterion> — <evidence>` or
      `- [ ] <criterion> — <why not met>`, where evidence is a test name, diff path, or
      command output. If the issue has no explicit criteria, list the criteria the work
      satisfied, with evidence.

""" + CONTRACT + """
      The comment must be factual; never claim a criterion is met without citable evidence.
    depends_on: [workflow-summary]
    context: fresh
"""

VERDICT_NODE = """  # Final gate: post the review verdict on the PR so the board poller can
  # gate the merge on it.
  - id: report-verdict
    prompt: |
      You are the final gate of a PR review run. Post the review verdict on the PR
      so the board poller can gate the merge on it.

      ## Inputs

      - Dispatch message: $ARGUMENTS
      - Fix-pass summary: $implement-fixes.output
      - Review findings: files under $ARTIFACTS_DIR/review/ (read consolidated-review.md if present)

      ## Steps

      1. PR number: extract from the dispatch message with the pattern `Review PR #(\\d+)`.
         If absent, read `$ARTIFACTS_DIR/.pr-number`; if that fails, list open PRs with
         `gh pr list --state open --json number,baseRefName -q '.[] | select(.baseRefName == "main") | .number'`
         and pick the one for this issue.
      2. Decide the verdict:
         - Any CRITICAL finding that remains unresolved after the fix pass -> `block`
         - Otherwise, any HIGH finding unresolved -> `request-changes`
         - Otherwise -> `approve`
         An "unresolved" finding is one the fix pass did not claim to fix; if the fix pass
         says all findings were fixed (or none existed), the verdict is `approve`.
      3. Post the verdict on the PR: `gh pr comment <number> --body "<summary>"` where the
         summary is 3-6 lines covering what was reviewed and the fix-pass outcome.
         The LAST line of the body MUST be exactly `VERDICT: <approve|request-changes|block>`.
         No other line may start with `VERDICT:`.
    depends_on: [implement-fixes]
    context: fresh
"""

SHIP_CONFLICTS_WF = """name: archon-fix-ship-conflicts
description: |
  Use when: a ship PR (feature -> main, In Review lane) has merge conflicts that
            block the board poller's verdict-gated merge.
  Triggers: dispatched by the board poller; the dispatch message carries the PR number.
  Does: merges the base branch into the PR head, resolves conflicts, validates,
        pushes the resolution, comments on the PR.
  NOT for: develop-branch conflicts, review requests, feature work, verdicts.
        Never post a VERDICT line — the review workflow owns the verdict.

nodes:
  - id: resolve
    prompt: |
      You are resolving merge conflicts on a ship PR so the board poller can merge it.

      ## Inputs

      Dispatch message: $ARGUMENTS

      ## Steps

      1. PR number: extract it from the dispatch message with the pattern
         `ship PR #(\\d+)`. If the pattern is absent, report the error and stop.
      2. Inspect the PR: `gh pr view <n> -R bradley-mankoff/news
         --json number,state,mergeable,headRefName,baseRefName`
         - If `state` is MERGED, or `mergeable` is not "CONFLICTING": comment on
           the PR "No conflicts to resolve (mergeable: <value>)." and stop.
      3. Resolve the conflicts locally (work only in this worktree; never touch
         or push any branch other than the PR head):
         - `git fetch origin <baseRefName> <headRefName>`
         - `git checkout -B conflict-fix origin/<headRefName>` (fresh local branch
           from the PR head; safe to force-reset later if needed)
         - `git merge origin/<baseRefName>`
         - Resolve every conflict:
           * Keep BOTH sides' intent: the feature's changes (PR title + diff)
             AND the base's changes. Drop nothing without a reason.
           * When both sides changed the same area, combine them; when one side
             supersedes the other, prefer the newer intent and note it in the
             PR comment.
           * If a conflict genuinely needs a human decision, do NOT guess:
             leave the merge uncommitted, post a comment on the PR starting with
             `NEEDS INPUT:` explaining the two options and their tradeoffs, and
             stop WITHOUT pushing.
      4. Validate before pushing: run the repo's test suite —
         `uv run pytest -q` (preferred; the repo is a uv project), falling back
         to `python3 -m unittest discover -s tests -q`. Fix ONLY merge-related
         breakage (e.g. an import that moved in the base); do not refactor or
         add features.
      5. Push the resolution to the PR head branch without checking it out here:
         `git push origin conflict-fix:<headRefName>`
      6. Comment on the PR (3-6 lines): what was merged (base -> head), which
         files had conflicts and how they were resolved, and the validation
         result. Do NOT post a `VERDICT:` line.
    depends_on: []
    context: fresh
    model: medium

effort: max
"""


def ensure_node(path: Path, node_id: str, node_text: str,
                anchor: str) -> str | None:
    """Insert `node_text` after `anchor` when `node_id` is missing.

    Returns a short description of what changed, or None when already applied.
    """
    text = path.read_text()
    if f"- id: {node_id}" in text:
        return None
    if anchor not in text:
        return f"anchor not found in {path.name}; insert {node_id} manually"
    idx = text.find(anchor)
    end = idx + len(anchor)
    while end < len(text) and text[end] == "\n":
        end += 1
    path.write_text(text[:end] + "\n" + node_text + "\n" + text[end:])
    return f"added {node_id} node to {path.name}"


def ensure_contract(path: Path, node_id: str) -> str | None:
    """Insert CONTRACT into an existing completion-comment node if missing."""
    text = path.read_text()
    start = text.find(f"- id: {node_id}")
    if start == -1:
        return None  # node insertion is handled separately
    if "reads this section and creates a tracking issue" in text[start:]:
        return None
    end = text.find("context: fresh", start)
    if end == -1:
        return f"node {node_id} end not found in {path.name}"
    factual = text.find("The comment must be factual", start, end)
    if factual == -1:
        return f"node {node_id} insertion point not found in {path.name}"
    text = text[:factual] + CONTRACT + "\n" + text[factual:]
    path.write_text(text)
    return f"added Deferred-work contract to {node_id} in {path.name}"


def main() -> int:
    changed: list[str] = []
    for fname, node_id, node_text, anchor in (
        ("archon-fix-github-issue.yaml", "completion-comment", FIX_NODE,
         "- id: report\n    command: archon-issue-completion-report\n"
         "    depends_on: [simplify]\n    context: fresh"),
        ("archon-idea-to-pr.yaml", "completion-comment", IDEA_NODE,
         "- id: workflow-summary\n    command: archon-workflow-summary\n"
         "    depends_on: [review]\n    context: fresh"),
        ("archon-smart-pr-review.yaml", "report-verdict", VERDICT_NODE,
         "- id: implement-fixes\n    command: archon-implement-review-fixes\n"
         "    depends_on: [synthesize]"),
    ):
        path = WORKFLOWS / fname
        if not path.exists():
            changed.append(f"MISSING {fname} (workflows dir: {WORKFLOWS})")
            continue
        if node_id == "completion-comment":
            note = ensure_contract(path, node_id)
            if note:
                changed.append(note)
        note = ensure_node(path, node_id, node_text, anchor)
        if note:
            changed.append(note)

    ship = WORKFLOWS / "archon-fix-ship-conflicts.yaml"
    if not ship.exists():
        ship.write_text(SHIP_CONFLICTS_WF)
        changed.append("created archon-fix-ship-conflicts.yaml")
    elif "name: archon-fix-ship-conflicts" not in ship.read_text():
        changed.append("archon-fix-ship-conflicts.yaml exists but looks wrong — "
                       "check its contents manually")

    if changed:
        print("\n".join(f"- {c}" for c in changed))
    else:
        print(f"no changes needed ({WORKFLOWS})")
    print("\nNext: archon validate workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
