#!/usr/bin/env python3
"""Idempotently re-apply the project's local edits to the archon workflows.

Restores the checklist in docs/archon-workflows.md ("Local edits to usable
workflows") after an archon reinstall replaces the bundled YAMLs:

  - `completion-comment` node (with the `## Deferred work` human-follow-up
    contract) in archon-fix-github-issue.yaml and archon-idea-to-pr.yaml
  - `report-verdict` node in archon-smart-pr-review.yaml
  - Rigorous nodes use Pi's OpenAI Codex OAuth model (`provider: pi`,
    `model: openai-codex/gpt-5.6-luna`, `effort: max`), matching the current
    ChatGPT/Codex session model. Routine nodes use DeepSeek V4 Flash through
    the Pi tier at `effort: max`.
  - the full archon-fix-ship-conflicts.yaml (inline prompt node, no DB
    commands)

Safe to run repeatedly: checks marker strings, no-ops when already applied,
prints what changed. Exit 0 always.

Usage: python3 automation/apply_workflow_edits.py
       (or `automation/deploy.sh`, which also restarts the board poller)
"""

import os
import re
import sys
from pathlib import Path

ARCHON_HOME = Path(os.environ.get("ARCHON_HOME",
                                  "~/.local/share/archon-pi/archon-home")).expanduser()
WORKFLOWS = ARCHON_HOME / "workflows"

MODEL_PROVIDER = "pi"
MODEL_ID = "openai-codex/gpt-5.6-luna"
MODEL_EFFORT = "max"

# Explicit rigorous assignments. All other AI nodes inherit the
# DeepSeek V4 Flash tier/default from archon-home/config.yaml.
RIGOROUS_NODES: dict[str, tuple[str, ...]] = {
    "archon-assist.yaml": ("assist",),
    "archon-pi-default.yaml": ("agent",),
    "archon-fix-develop-conflicts.yaml": ("resolve",),
    "archon-fix-ship-conflicts.yaml": ("resolve",),
    "archon-comprehensive-pr-review.yaml": (
        "scope", "code-review", "error-handling", "test-coverage",
        "comment-quality", "docs-impact", "synthesize", "implement-fixes",
    ),
    "archon-create-issue.yaml": (
        "investigate", "reproduce", "report-failure", "draft-issue",
    ),
    "archon-fix-github-issue.yaml": (
        "investigate", "plan", "review-scope", "review-classify",
        "code-review", "error-handling", "test-coverage", "comment-quality",
        "docs-impact", "synthesize", "self-fix",
        "completion-comment",
    ),
    "archon-idea-to-pr.yaml": (
        "create-plan", "plan-setup", "confirm-plan", "completion-comment",
    ),
    "archon-issue-review-full.yaml": ("investigate",),
    "archon-resolve-conflicts.yaml": ("resolve",),
    "archon-plan-to-pr.yaml": ("plan-setup", "confirm-plan"),
    "archon-review-block.yaml": (
        "review-scope", "spec-review", "code-review", "error-handling",
        "test-coverage", "comment-quality", "docs-impact", "synthesize",
        "implement-fixes",
    ),
    "archon-smart-pr-review.yaml": (
        "scope", "classify", "code-review", "error-handling", "test-coverage",
        "comment-quality", "docs-impact", "synthesize", "implement-fixes",
        "report-verdict",
    ),
    "archon-validate-pr.yaml": (
        "code-review-main", "code-review-feature", "classify-testability",
        "e2e-test-main", "e2e-test-feature", "final-report",
    ),
    "archon-workflow-builder.yaml": ("extract-intent", "generate-yaml"),
}

# The testing contract shared by both completion-comment nodes. The board
# poller partitions this section into machine checks and human steps for the
# Ready for Review comment; the ready-review QA agent reuses recorded
# evidence instead of re-running checks.
TESTING_COMMAND_RULE = """      Keep copy-paste commands standalone: put explanations on
      separate prose lines, never after a shell command using `#`.
"""
TESTING_CONTRACT = """      ## How to test
      Record what the MACHINE already verified and what still needs a HUMAN.
      The board machinery runs every check it can on its own, so a command in
      this section is executed by the machine (or reused from recorded
      evidence), never handed to the human as homework.
      - `### Machine checks` — every automated check this run executed:
        copy-paste command plus its recorded result on the next prose line
        (test counts, zero exit, clean compile). The ready-review QA agent
        re-runs only a check that lacks recorded evidence.
      - `### Human checks` — only steps that genuinely require a person:
        product review, taste, visual inspection, decisions. State the action
        and the expected result. If nothing needs a human, write
        `None — all recorded checks are machine-runnable.`
      - For UI/API/CLI changes, name the exact command or URL the ready-review
        QA agent (not the human) runs to verify each acceptance criterion; say
        what output means pass.
      - If no automated validation exists, write `Not manually testable —
        <reason>` and name the best available evidence.
""" + TESTING_COMMAND_RULE

# The Deferred-work contract shared by both completion-comment nodes. This
# records human follow-up; the board poller does not parse the section or create
# tracking issues from it.
CONTRACT = """      ## Deferred work
      If ANY work is being deferred — anything intentionally not done now that still
      needs doing later — it MUST be listed here, one bullet per item. This includes:
      - explicit "later"/"future"/"out of scope"/"deferred" decisions, including
        anything you wrote into an ADR, README, or the report above (the board poller
        does not parse this section or create tracking issues from it);
      - NOT-Building / scope-limit exclusions that are future work (not "never");
      - skipped or blocked review findings that warrant follow-up.
      Trace the ORIGINAL issue ask first (the issue body from the Inputs above):
      compare every acceptance criterion, described behavior, and named component
      in the issue body — including its **Out of scope** section — against what
      this run actually shipped:
      - anything NOT done and NOT in the issue's Out of scope section is an
        UNMET acceptance criterion, not deferred work: mark it
        `- [ ] <criterion> — <why not met>` in the Acceptance criteria section
        above. It is part of this issue's delivery — the human will send the
        issue back, and it remains an unmet criterion for human review.
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
          (record the relationship for human follow-up).
        - it is genuinely rejected — never-to-be-done, superseded by context:
          stamp `**Out of scope:** <kebab-concept-slug>` (the poller records it
          in `.out-of-scope/<slug>.md` so the same request does not resurrect).
        - otherwise leave the item bare for human follow-up.
      If you are unsure whether an issue covers the same deliverable, treat them
      as distinct (bare) and explain the uncertainty for the human reviewer.
      SIZE BAR — not every finding deserves a separate follow-up record. Apply
      the "would this ever be scheduled on its own?" test before recording:
      - small chores, test tweaks, doc fixes, and cleanups that a competent dev
        would knock out in under ~an hour as part of the parent's follow-up are
        not separate follow-up work: stamp `**Skip:** folded into #<parent> — <why>`.
      - review findings whose fix belongs with the parent issue's remaining
        scope: same — Skip with the reason.
      - record only genuine, independently schedulable deliverables — features,
        subsystems, cross-cutting fixes, or anything that gates other work.
      Err toward Skip for small items (a backlog full of chores is worse than a
      tiny task picked up later); err toward bare only for real deliverables.
      Format, exactly — keep each follow-up item self-contained:
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

""" + TESTING_CONTRACT + CONTRACT + """
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

""" + TESTING_CONTRACT + CONTRACT + """
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

SHIP_CONFLICTS_WF = r"""name: archon-fix-ship-conflicts
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
         Follow `.archon/commands/resolving-merge-conflicts.md` for hunk-by-hunk
         discipline: preserve both intents, never invent behavior, never `--abort`.

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

DEVELOP_CONFLICTS_WF = r"""
name: archon-fix-develop-conflicts
description: |
  Use when: a develop PR (feature -> develop, In Progress lane) has merge
            conflicts that block the board poller's completion merge.
  Triggers: dispatched by the board poller; the dispatch message carries the PR number.
  Does: merges develop into the PR head, resolves conflicts, validates,
        pushes the resolution, comments on the PR.
  NOT for: ship-PR conflicts (use archon-fix-ship-conflicts), review requests,
        feature work, verdicts. Never post a VERDICT line — the review
        workflow owns the verdict.

nodes:
  - id: resolve
    prompt: |
      You are resolving merge conflicts on a develop PR so the board poller can
      merge it into develop.

      ## Inputs

      Dispatch message: $ARGUMENTS

      ## Steps

      1. PR number: extract it from the dispatch message with the pattern
         `develop PR #(\d+)`. If the pattern is absent, report the error and stop.
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
         Follow `.archon/commands/resolving-merge-conflicts.md` for hunk-by-hunk
         discipline: preserve both intents, never invent behavior, never `--abort`.

         - Resolve every conflict:
           * Keep BOTH sides' intent: the feature's changes (PR title + diff)
             AND develop's changes. Drop nothing without a reason.
           * When both sides changed the same area, combine them; when one side
             supersedes the other, prefer the newer intent and note it in the
             PR comment.
           * If a conflict genuinely needs a human decision, do NOT guess:
             leave the merge uncommitted, post a comment on the PR starting with
             `NEEDS INPUT:` explaining the two options and their tradeoffs, and
             stop WITHOUT pushing.
      4. Validate before pushing: run the repo's test suite —
         `.venv/bin/python3 -m pytest tests/ -q` (this machine's reliable
         invocation; `uv run pytest -q` is flaky here), falling back to
         `python3 -m unittest discover -s tests -q`. Fix ONLY merge-related
         breakage (e.g. an import that moved in develop); do not refactor or
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

SYNC_FIX_NODE = r"""
  - id: sync-with-develop
    prompt: |
      Sync this branch with the latest develop before the PR is created, so
      parallel runs merge cleanly.

      ## Steps

      1. Fetch develop: `git fetch origin develop`. If the fetch fails, log the
         error and continue (the develop merge in the workflow will surface it).
      2. If the current branch has no commits yet (fresh worktree), skip the
         merge — nothing can conflict. Log "no commits; skipping sync".
      3. Otherwise merge develop into the branch: `git merge origin/develop`.
         Follow `.archon/commands/resolving-merge-conflicts.md` for hunk-by-hunk
         discipline: preserve both intents, never invent behavior, never `--abort`.

         - On conflicts, resolve them keeping BOTH sides' intent: this
           feature's changes AND develop's changes. When both sides touched the
           same area, combine them; when one side supersedes the other, prefer
           the newer intent and note it in the PR body.
         - If a conflict genuinely needs a human decision, do NOT guess: leave
           the merge uncommitted, post a comment on the issue starting with
           `NEEDS INPUT:` explaining the two options and their tradeoffs, and
           stop WITHOUT pushing (the board poller moves the issue to Blocked;
           the human's answer resumes the run in this worktree).
      4. Validate the merge: run the repo's test suite —
         `.venv/bin/python3 -m pytest tests/ -q` (this machine's reliable
         invocation), falling back to
         `python3 -m unittest discover -s tests -q`. Fix ONLY merge-related
         breakage (e.g. an import that moved in develop); do not refactor or
         add features.
      5. Commit the merge (or the resolution commits). Never push develop;
         never push anything other than the current branch.
    depends_on: [check-blocked]
    when: "$check-blocked.output == 'clear'"
    context: fresh
"""

SYNC_IDEA_NODE = r"""
  - id: sync-with-develop
    prompt: |
      Sync this branch with the latest develop before the PR is finalized, so
      parallel runs merge cleanly.

      ## Steps

      1. Fetch develop: `git fetch origin develop`. If the fetch fails, log the
         error and continue (the develop merge in the workflow will surface it).
      2. If the current branch has no commits yet (fresh worktree), skip the
         merge — nothing can conflict. Log "no commits; skipping sync".
      3. Otherwise merge develop into the branch: `git merge origin/develop`.
         Follow `.archon/commands/resolving-merge-conflicts.md` for hunk-by-hunk
         discipline: preserve both intents, never invent behavior, never `--abort`.

         - On conflicts, resolve them keeping BOTH sides' intent: this
           feature's changes AND develop's changes. When both sides touched the
           same area, combine them; when one side supersedes the other, prefer
           the newer intent and note it in the PR body.
         - If a conflict genuinely needs a human decision, do NOT guess: leave
           the merge uncommitted, post a comment on the issue starting with
           `NEEDS INPUT:` explaining the two options and their tradeoffs, and
           stop WITHOUT pushing (the board poller moves the issue to Blocked;
           the human's answer resumes the run in this worktree).
      4. Validate the merge: run the repo's test suite —
         `.venv/bin/python3 -m pytest tests/ -q` (this machine's reliable
         invocation), falling back to
         `python3 -m unittest discover -s tests -q`. Fix ONLY merge-related
         breakage (e.g. an import that moved in develop); do not refactor or
         add features.
      5. Commit the merge (or the resolution commits). Never push develop;
         never push anything other than the current branch.
    depends_on: [validate]
    context: fresh
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
    """Insert completion-comment contracts into an existing node if missing,
    and refresh an outdated How-to-test contract."""
    text = path.read_text()
    start = text.find(f"- id: {node_id}")
    if start == -1:
        return None  # node insertion is handled separately
    end = text.find("context: fresh", start)
    if end == -1:
        return f"node {node_id} end not found in {path.name}"
    node = text[start:end]
    deferred = text.find("      ## Deferred work", start, end)
    needs_testing = "      ## How to test" not in node
    needs_shell_rule = "Keep copy-paste commands standalone" not in node
    needs_deferred = "      ## Deferred work" not in node
    stale_deferred = (
        "reads this section and creates a tracking issue" in node
        or "poller creates the tracking issue" in node
        or "the poller creates a new issue" in node
        or "the poller parses" in node
    )
    stale_testing = False
    howto = -1
    if not needs_testing and deferred != -1:
        howto = text.find("      ## How to test", start, deferred)
        if howto != -1 and text[howto:deferred] != TESTING_CONTRACT:
            stale_testing = True
    if (not needs_testing and not needs_shell_rule
            and not needs_deferred and not stale_testing and not stale_deferred):
        return None
    factual = text.find("The comment must be factual", start, end)
    if factual == -1:
        return f"node {node_id} insertion point not found in {path.name}"
    insertion = deferred if deferred != -1 else factual
    addition = ""
    if needs_testing or stale_testing:
        addition += TESTING_CONTRACT
    elif needs_shell_rule:
        addition += TESTING_COMMAND_RULE
    if needs_deferred or stale_deferred:
        addition += CONTRACT
    if stale_testing:
        replacement_end = factual if stale_deferred else deferred
        text = text[:howto] + addition + text[replacement_end:]
    elif stale_deferred:
        text = text[:deferred] + addition + text[factual:]
    else:
        text = text[:insertion] + addition + text[insertion:]
    path.write_text(text)
    added = []
    if needs_testing or stale_testing:
        added.append("How-to-test")
    elif needs_shell_rule:
        added.append("copy-paste command")
    if needs_deferred or stale_deferred:
        added.append("Deferred-work")
    return f"added {' and '.join(added)} contracts to {node_id} in {path.name}"



def ensure_sync_node(path: Path, anchor: str, node_text: str, target_id: str,
                     dep_old: str, dep_new: str) -> str | None:
    """Insert the pre-PR sync node after `anchor` and rewire the downstream
    node's depends_on to it. Idempotent on the node id."""
    text = path.read_text()
    if "- id: sync-with-develop" in text:
        return None
    if anchor not in text:
        return f"anchor not found in {path.name}; insert sync-with-develop manually"
    idx = text.find(anchor)
    end = idx + len(anchor)
    while end < len(text) and text[end] == "\n":
        end += 1
    text = text[:end] + "\n" + node_text + "\n" + text[end:]
    tidx = text.find(f"- id: {target_id}")
    if tidx == -1:
        return f"target node {target_id} not found in {path.name}; rewire manually"
    didx = text.find(dep_old, tidx)
    if didx == -1:
        return f"depends_on line not found after {target_id} in {path.name}"
    text = text[:didx] + dep_new + text[didx + len(dep_old):]
    path.write_text(text)
    return f"added sync-with-develop to {path.name}"


def ensure_spec_review(path: Path) -> str | None:
    """Insert the Spec-axis review node into archon-review-block.yaml and add
    it to synthesize's dependencies. Idempotent on the node id."""
    text = path.read_text()
    if "- id: spec-review" in text:
        return None
    sync_block = ("  - id: sync\n"
                  "    command: archon-sync-pr-with-main\n"
                  "    depends_on: [review-scope]\n"
                  "    context: fresh\n")
    if sync_block not in text:
        return "sync block not found in archon-review-block.yaml; insert spec-review manually"
    spec_node = (sync_block + "\n"
                 "  - id: spec-review\n"
                 "    prompt: |\n"
                 "      Review the PR diff against the ORIGINATING ISSUE — the Spec axis of a\n"
                 "      two-axis review (Standards + Spec; see `.archon/commands/code-review.md`).\n"
                 "\n"
                 "      ## Steps\n"
                 "\n"
                 "      1. Identify the originating issue: from the PR body (`Issue: #N` /\n"
                 "         `Fixes #N`), the branch name (`archon/task-issue-<N>`), or the commit\n"
                 "         messages. Fetch it: `gh issue view <n> -R bradley-mankoff/news\n"
                 "         --json title,body,labels`.\n"
                 "      2. Read the issue's TITLE and INITIAL DESCRIPTION (never the comments):\n"
                 "         acceptance criteria, described behaviors, named components, and the\n"
                 "         **Out of scope** section.\n"
                 "      3. Review the diff against that spec (three-dot from the merge base):\n"
                 "         - every acceptance criterion met? every described behavior present?\n"
                 "         - anything shipped the issue did not ask for (gold-plating)?\n"
                 "         - anything from the issue left undone that is NOT in its Out of scope\n"
                 "           section — that is an unmet criterion, not a deferral.\n"
                 "      4. Write findings to `$ARTIFACTS_DIR/review/spec-review-findings.md` in\n"
                 "         the same format as the other review agents (severity-prefixed lines,\n"
                 "         each citing the criterion and the evidence or gap).\n"
                 "      5. Post a 3-6 line summary as a PR comment: criteria met vs unmet,\n"
                 "         gold-plating, and any unmet criteria that will bounce the issue at\n"
                 "         Ready for Review. Do NOT post a `VERDICT:` line — the review workflow\n"
                 "         owns the verdict.\n"
                 "    depends_on: [sync]\n"
                 "    context: fresh\n")
    text = text.replace(sync_block, spec_node)
    old = "depends_on: [code-review, error-handling, test-coverage, comment-quality, docs-impact]"
    if old not in text:
        return "synthesize deps not found in archon-review-block.yaml; rewire manually"
    text = text.replace(old, old + ", spec-review")
    path.write_text(text)
    return "added spec-review node to archon-review-block.yaml"


def ensure_rigorous_models(path: Path, node_ids: tuple[str, ...]) -> str | None:
    """Pin selected AI nodes to Pi's OpenAI Codex OAuth model at maximum effort."""
    text = path.read_text()
    original = text
    missing: list[str] = []
    for node_id in node_ids:
        node = re.search(rf"(?m)^  - id: {re.escape(node_id)}[ \t]*\n", text)
        if node is None:
            missing.append(node_id)
            continue
        following = re.search(r"(?m)^  - id: ", text[node.end():])
        block_end = node.end() + following.start() if following else len(text)
        block = text[node.start():block_end]
        body = re.sub(
            r"(?m)^    (?:provider|model|effort|modelReasoningEffort):[^\n]*\n",
            "",
            block[len(node.group(0)):],
        )
        replacement = (
            node.group(0)
            + f"    provider: {MODEL_PROVIDER}\n"
            + f"    model: {MODEL_ID}\n"
            + f"    effort: {MODEL_EFFORT}\n"
            + body
        )
        text = text[:node.start()] + replacement + text[block_end:]

    if text != original:
        path.write_text(text)
        note = f"pinned rigorous Pi Codex nodes in {path.name}"
    else:
        note = None
    if missing:
        suffix = f" (missing nodes: {', '.join(missing)})"
        return (note or f"checked rigorous Pi Codex nodes in {path.name}") + suffix
    return note


def apply_rigorous_models(changed: list[str]) -> None:
    for fname, node_ids in RIGOROUS_NODES.items():
        path = WORKFLOWS / fname
        if not path.exists():
            changed.append(f"MISSING {fname} for rigorous Pi Codex assignments (workflows dir: {WORKFLOWS})")
            continue
        note = ensure_rigorous_models(path, node_ids)
        if note:
            changed.append(note)




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

    sync_fix_anchor = """  - id: check-blocked
    bash: |
      ISSUE_NUM=$(echo $extract-issue-number.output | grep -oE '[0-9]+' | head -1)
      if gh issue view "$ISSUE_NUM" --json labels -q '.labels[].name' 2>/dev/null | grep -qx 'needs-input'; then
        echo "blocked"
      else
        echo "clear"
      fi
    depends_on: [validate]
"""
    note = ensure_sync_node(
        WORKFLOWS / "archon-fix-github-issue.yaml", sync_fix_anchor,
        SYNC_FIX_NODE, "create-pr",
        "    depends_on: [check-blocked]\n    context: fresh",
        "    depends_on: [sync-with-develop]\n    context: fresh")
    if note:
        changed.append(note)
    sync_idea_anchor = """  - id: validate
    command: archon-validate
    depends_on: [implement-tasks]
    context: fresh
"""
    note = ensure_sync_node(
        WORKFLOWS / "archon-idea-to-pr.yaml", sync_idea_anchor,
        SYNC_IDEA_NODE, "finalize-pr",
        "    depends_on: [validate]\n    context: fresh",
        "    depends_on: [sync-with-develop]\n    context: fresh")
    if note:
        changed.append(note)

    note = ensure_spec_review(WORKFLOWS / "archon-review-block.yaml")
    if note:
        changed.append(note)


    ship = WORKFLOWS / "archon-fix-ship-conflicts.yaml"
    if not ship.exists():
        ship.write_text(SHIP_CONFLICTS_WF)
        changed.append("created archon-fix-ship-conflicts.yaml")
    elif "name: archon-fix-ship-conflicts" not in ship.read_text():
        changed.append("archon-fix-ship-conflicts.yaml exists but looks wrong — "
                       "check its contents manually")

    develop = WORKFLOWS / "archon-fix-develop-conflicts.yaml"
    if not develop.exists():
        develop.write_text(DEVELOP_CONFLICTS_WF)
        changed.append("created archon-fix-develop-conflicts.yaml")
    elif "name: archon-fix-develop-conflicts" not in develop.read_text():
        changed.append("archon-fix-develop-conflicts.yaml exists but looks wrong — "
                       "check its contents manually")
    apply_rigorous_models(changed)

    if changed:
        print("\n".join(f"- {c}" for c in changed))
    else:
        print(f"no changes needed ({WORKFLOWS})")
    print("\nNext: archon validate workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
