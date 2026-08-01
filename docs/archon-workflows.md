# Archon Workflow Inventory

Machine-local archon (archon-pi build, v0.6.0 = stock Archon `e67940a3`) lives at
`~/.local/share/archon-pi/archon-home/`.

## Execution model

- Provider: `pi` (the `pi` CLI); model `deepseek/deepseek-v4-flash` at `effort: max`
  (xhigh thinking) for every tier (`small`/`medium`/`large`) — configured in
  `archon-home/config.yaml`.
- Bundled workflow defaults are disabled (repo `.archon/config.yaml` →
  `defaults.loadDefaultWorkflows: false`). The usable set is the 15 files in
  `archon-home/workflows/`.
- Provider pins on bundled workflows are **overridden when the node's `model:`
  resolves to a tier** — the tier's provider wins (dag-executor). E.g.
  `archon-fix-github-issue` declares `provider: claude, model: medium` but runs
  entirely on pi/deepseek via the `medium` tier. Only workflows pinned to claude
  with **no tier model** stay claude-locked (archived below).

## Usable workflows (pi/deepseek)

| Workflow | Intent |
|---|---|
| `archon-fix-github-issue` | Classify issue → investigate (bug) or plan (feature) → implement → validate → **draft PR** → smart review (code-review always + conditional error-handling/test-coverage/comment-quality/docs-impact) → self-fix → simplify → report on the issue. Leaves the PR draft — the human tests locally, then moves the issue to In Review. |
| `archon-idea-to-pr` | Feature idea/issue → plan → implement → validate → ready PR → comprehensive review block (5 parallel review agents) → synthesize → fix → summary comment. |
| `archon-plan-to-pr` | Execute an existing plan file end to end (same review block as idea-to-pr). |
| `archon-feature-development` | Implement from a plan file or a GitHub issue containing a plan. |
| `archon-comprehensive-pr-review` | Full 5-agent review of a PR with auto-fixes (always all agents). |
| `archon-smart-pr-review` | Classify PR complexity → run only the relevant review agents → synthesize → auto-fix CRITICAL/HIGH. **The In Review lane trigger.** Local copy has the ntfy MCP notify nodes stripped (MCP is claude-only; pi ignores it). |
| `archon-issue-review-full` | Full fix + comprehensive review pipeline for one issue. |
| `archon-validate-pr` | E2E bug validation: run main vs feature branch, produce verdict report. |
| `archon-create-issue` | File a bug report with reproduction evidence. Requires the `agent-browser` skill only for web-UI repro playbooks (installed globally). |
| `archon-resolve-conflicts` | Resolve PR merge conflicts. |
| `archon-assist` | Fallback general-purpose agent. |
| `archon-workflow-builder` | Author a new workflow from a description. |
| `archon-test-loop-dag` | Loop-mechanics test workflow (used in smoke tests). |
| `archon-review-block` | Building block included by idea-to-pr / plan-to-pr / issue-review-full — not standalone. |
| `archon-pi-default` | Minimal stock-pi oneshot. |

## Archived workflows (claude-only — not discovered)

Reason: pinned to the claude provider with **no tier model reference**, so the
tier override does not apply; they need the Claude Code binary and/or
claude-only features (hooks, interactive relay). This project runs exclusively
on pi/deepseek. Original YAMLs are preserved at
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
