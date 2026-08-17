# Archon Workflow Inventory

**Ticket creator:** the minimal assignable menu (doing + review) lives in
[`ticket-workflow-menu.md`](ticket-workflow-menu.md) — grok reads that when
processing ideas into tickets. Dispatch rules include the hard 8pm CST cutoff
(no new work after 8pm CST = 02:00 UTC, DeepSeek's peak window).

Machine-local archon (archon-pi build, v0.7.0 = stock Archon) lives at
`~/.local/share/archon-pi/archon-home/`.

## Execution model

- Routine nodes use the `pi` CLI with
  `opencode-go/deepseek-v4-flash` at `effort: max` via the `small`/`medium`/`large`
  tiers in `archon-home/config.yaml`.
- Rigorous nodes use Pi's OpenAI Codex OAuth backend with
  `provider: pi`, `model: openai-codex/gpt-5.6-luna`, and `effort: max`, matching
  this session's `openai-codex/gpt-5.6-luna` model.
  It covers planning, review, conflict resolution, issue drafting, and the
  completion records that classify deferred work for human follow-up; the board
  poller does not parse this section or create issues from it.
- Both paths run at their maximum Pi reasoning setting: DeepSeek and the
  OpenAI Codex backend both use `effort: max`.
- Pi OAuth credentials are configured with the interactive `/login` command.
  The OpenAI Codex subscription is stored in `~/.pi/agent/auth.json`; the
  routine OpenCode credential remains alongside it.
- Bundled workflow defaults are disabled (repo `.archon/config.yaml` →
  `defaults.loadDefaultWorkflows: false`). The usable set is the 17 files in
  `archon-home/workflows/`.
- Tier model refs use the tier's provider; explicit Pi model refs select the
  OpenAI Codex backend. Routine nodes therefore remain on Pi/DeepSeek while
  rigorous nodes use Pi/OpenAI Codex.
- Only workflows pinned to claude with **no tier model** stay claude-locked
  (archived below).

## Usable workflows (Pi: OpenAI Codex + OpenCode)

| Workflow | Intent |
|---|---|
| `archon-fix-github-issue` | Classify issue → investigate (bug) or plan (feature) → implement → validate → **draft PR** → smart review (code-review always + conditional error-handling/test-coverage/comment-quality/docs-impact) → self-fix → report on the issue. Leaves the PR draft — the machine runs the recorded checks (implementation run + ready-review QA) and the human covers only the `### Human checks` steps, then the issue moves to In Review. Local copy adds a `completion-comment` node: posts `## What shipped` / `## Decisions` / `## Acceptance criteria` / `## How to test` (with evidence) on the issue; the `## How to test` section separates `### Machine checks` (commands + recorded results) from `### Human checks` (steps that need a person), and the poller partitions it the same way for the Ready for Review comment. |
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
| `archon-pi-default` | Minimal fallback oneshot. |

## Archived workflows (claude-only — not discovered)

Reason: pinned to the claude provider with **no tier model reference**, so the
tier override does not apply; they need the Claude Code binary and/or
claude-only features (hooks, interactive relay). Routine nodes use
pi/opencode-go; rigorous nodes use Pi's OpenAI Codex backend. Original YAMLs are preserved at
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
  `## How to test` handoff and the `## Deferred work` section for human
  follow-up; the board poller does not parse it or create tracking issues).
- `archon-idea-to-pr.yaml`: `completion-comment` node after `workflow-summary`
  (same structured record + `## How to test` handoff + `## Deferred work`
  section for human follow-up; the board poller does not parse it or create
  tracking issues).
- Both implementation workflows: `sync-with-develop` node before the PR node
  (merges develop into the branch, resolves, validates) — `create-pr` /
  `finalize-pr` depend on it.
- `archon-review-block.yaml`: `spec-review` node after `sync` (Spec axis of
  the two-axis review — the diff vs the originating issue's criteria and Out
  of scope; posts a summary on the PR, never a VERDICT); `synthesize`
  depends on it.
- `archon-fix-develop-conflicts.yaml`: fully inline (one `prompt` node, no DB
  commands); the develop-lane twin of `archon-fix-ship-conflicts`.

## Vendored matt pocock skills (MIT, adapted)

Wrapper-facing skills (grilling, to-spec, to-tickets, triage, wayfinder, and
the execution disciplines) are installed at `~/.claude/skills/` (home level,
auto-discovered by the omp wrapper — attribution + license in
`~/.claude/skills/LICENSE-mattpocock-skills.txt`). The execution disciplines
used inside workflows are also vendored as repo-level commands in
`.archon/commands/` (`implement`, `tdd`, `code-review`, `diagnosing-bugs`,
`prototype`, `research`, `domain-modeling`, `codebase-design`,
`resolving-merge-conflicts`, `handoff` — `archon validate commands` checks
them). Workflow prompts reference `resolving-merge-conflicts` (sync + resolver
nodes) and `code-review` (spec-review node). Update: re-pull upstream, re-adapt
(the adaptations are mechanical: de-slash, tracker mechanics → this repo).
- `archon-fix-ship-conflicts.yaml`: fully inline (one `prompt` node, no DB
  commands); if missing, re-create it per `docs/archon-workflows.md` (same
  shape as the other local workflows — `name`, `description`, `nodes`, `effort`).
