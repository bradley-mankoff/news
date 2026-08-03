# Archon Workflow Inventory

Machine-local archon (archon-pi build, v0.7.0 = stock Archon) lives at
`~/.local/share/archon-pi/archon-home/`.

## Execution model

- Provider: `pi` (the `pi` CLI); model `opencode-go/deepseek-v4-flash` at `effort: max`
  (xhigh thinking) for every tier (`small`/`medium`/`large`) — configured in
  `archon-home/config.yaml`.
- Bundled workflow defaults are disabled (repo `.archon/config.yaml` →
  `defaults.loadDefaultWorkflows: false`). The usable set is the 15 files in
  `archon-home/workflows/`.
- Provider pins on bundled workflows are **overridden when the node's `model:`
  resolves to a tier** — the tier's provider wins (dag-executor). E.g.
  `archon-fix-github-issue` declares `provider: claude, model: medium` but runs
  entirely on pi/opencode-go via the `medium` tier. Only workflows pinned to claude
  with **no tier model** stay claude-locked (archived below).

## Usable workflows (pi/opencode-go)

| Workflow | Intent |
|---|---|
| `archon-fix-github-issue` | Classify issue → investigate (bug) or plan (feature) → implement → validate → **draft PR** → smart review (code-review always + conditional error-handling/test-coverage/comment-quality/docs-impact) → self-fix → simplify → report on the issue. Leaves the PR draft — the human tests locally, then moves the issue to In Review. Local copy adds a `completion-comment` node: posts `## What shipped` / `## Decisions` / `## Acceptance criteria` (with evidence) on the issue. |
| `archon-idea-to-pr` | Feature idea/issue → plan → implement → validate → ready PR → comprehensive review block (5 parallel review agents) → synthesize → fix → summary comment. Local copy adds a `completion-comment` node with the same structured record as fix-github-issue. |
| `archon-plan-to-pr` | Execute an existing plan file end to end (same review block as idea-to-pr). |
| `archon-feature-development` | Implement from a plan file or a GitHub issue containing a plan. |
| `archon-comprehensive-pr-review` | Full 5-agent review of a PR with auto-fixes (always all agents). |
| `archon-smart-pr-review` | Classify PR complexity → run only the relevant review agents → synthesize → auto-fix CRITICAL/HIGH. **The In Review lane trigger.** Local copy has the ntfy MCP notify nodes stripped (MCP is claude-only; pi ignores it) and adds a `report-verdict` node: after the fix pass it posts `VERDICT: approve|request-changes|block` as the last line of a PR comment — the board poller merges the ship PR only on `approve`. |
| `archon-issue-review-full` | Full fix + comprehensive review pipeline for one issue. |
| `archon-validate-pr` | E2E bug validation: run main vs feature branch, produce verdict report. |
| `archon-create-issue` | File a bug report with reproduction evidence. Requires the `agent-browser` skill only for web-UI repro playbooks (installed globally). |
| `archon-resolve-conflicts` | Resolve PR merge conflicts. |
| `archon-fix-ship-conflicts` | Resolve conflicts on a ship PR (In Review lane) so the verdict-gated merge can proceed: merge base into head, resolve, validate, push, comment. Dispatched by the board poller when the mechanical merge API hits real conflicts; never posts a `VERDICT:` line (the review workflow owns that). Fully inline prompt node — no DB commands. |
| `archon-fix-develop-conflicts` | Resolve conflicts on a develop PR (In Progress lane) so the completion merge can proceed: merge develop into head, resolve, validate (`.venv` pytest), push, comment. Dispatched by the board poller's develop-conflict gate; never posts a `VERDICT:` line. Fully inline prompt node. |
| `archon-assist` | Fallback general-purpose agent. |
| `archon-workflow-builder` | Author a new workflow from a description. |
| `archon-test-loop-dag` | Loop-mechanics test workflow (used in smoke tests). |
| `archon-review-block` | Building block included by idea-to-pr / plan-to-pr / issue-review-full — not standalone. |
| `archon-pi-default` | Minimal stock-pi oneshot. |

## Archived workflows (claude-only — not discovered)

Reason: pinned to the claude provider with **no tier model reference**, so the
tier override does not apply; they need the Claude Code binary and/or
claude-only features (hooks, interactive relay). This project runs exclusively
on pi/opencode-go. Original YAMLs are preserved at
`~/.local/share/archon-pi/archon-home/workflows-archived/`.

| Workflow | Original intent | Why archived |
|---|---|---|
| `archon-adversarial-dev` | GAN-style planner/generator/evaluator build loop | claude pin, no tier model |
| `archon-architect` | Architecture sweep / complexity reduction with per-node hooks | hooks (claude-only) |
| `archon-interactive-prd` | Guided PRD conversation | claude pin, interactive relay |
| `archon-piv-loop` | Plan-Implement-Validate loop with human gates | claude pin, interactive relay |
| `archon-ralph-dag` | Ralph story-loop implementation | claude pin, no tier model |
| `archon-refactor-safely` | Behavior-preserving refactor with continuous validation | claude pin + hooks |
| `archon-remotion-generate` | Remotion video generation | irrelevant to this repo (no Remotion project, missing skill) |

Restore any archived workflow with:

```bash
archon workflow install <slug>            # marketplace copy
# or copy the YAML back into archon-home/workflows/
```

## Local edits to usable workflows (re-apply after archon reinstalls)

**Automated:** run `python3 automation/apply_workflow_edits.py` (idempotent;
restores everything below, including recreating `archon-fix-ship-conflicts.yaml`
from its embedded template). `automation/deploy.sh` also restarts the board
poller. Only the manual checklist follows, for when the script cannot run.

The three added nodes are inline `prompt:` nodes in the workflow YAMLs — they do
not depend on DB-registered commands, so they survive archon updates unless the
YAML is overwritten. If a workflow file is replaced, re-apply:

- `archon-smart-pr-review.yaml`: `report-verdict` node after `implement-fixes`
  (posts `VERDICT: <approve|request-changes|block>` on the PR).
- `archon-fix-github-issue.yaml`: `completion-comment` node after `report`
  (posts the structured completion record on the issue, including the
  `## Deferred work` section the board poller parses).
- `archon-idea-to-pr.yaml`: `completion-comment` node after `workflow-summary`
  (same structured record + `## Deferred work` section).
- Both implementation workflows: `sync-with-develop` node before the PR node
  (merges develop into the branch, resolves, validates) — `create-pr` /
  `finalize-pr` depend on it.
- `archon-fix-develop-conflicts.yaml`: fully inline (one `prompt` node, no DB
  commands); the develop-lane twin of `archon-fix-ship-conflicts`.
- `archon-fix-ship-conflicts.yaml`: fully inline (one `prompt` node, no DB
  commands); if missing, re-create it per `docs/archon-workflows.md` (same
  shape as the other local workflows — `name`, `description`, `nodes`, `effort`).
