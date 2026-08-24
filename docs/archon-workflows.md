# Archon Workflow Inventory

**Ticket creator:** the minimal assignable menu (doing + review) lives in
[`ticket-workflow-menu.md`](ticket-workflow-menu.md) — the ticket creator
reads that when processing ideas into tickets. Dispatch rules include the
poller-enforced 8pm CST quiet-hours cutoff (no new work after 8pm CST; a
legacy operational policy, not a model-cost requirement).

Machine-local archon (archon-pi build, v0.7.0 = stock Archon) lives at
`~/.local/share/archon-pi/archon-home/`.

## Execution model

- Every node runs on the `pi` CLI with `local-qwen/qwen3.8-27b-q4` — the
  local llama-server worker at `http://127.0.0.1:8080/v1` — at `effort: max`.
  The `small`, `medium`, and `large` tiers in `archon-home/config.yaml` and
  the default assistant (`assistants.pi`) all resolve to that same local
  model. There is no separate opencode assistant (local-qwen is Pi-only) and
  no per-token cost.
- All paths run at the maximum Pi reasoning setting: the tiers carry
  `effort: max`, and the curated workflows pin workflow-level `effort: max`.
- The local model needs no OAuth: `local-qwen` is registered in
  `~/.pi/agent/models.json` with a dummy local key (`apiKey:
  sk-local-qwen-dummy`) pointing at the local server. Pi tool credentials, if
  any, remain separate in `~/.pi/agent/auth.json`.
- Bundled workflow defaults are disabled (repo `.archon/config.yaml` →
  `defaults.loadDefaultWorkflows: false`). The usable set is the 17 files in
  `archon-home/workflows/`.
- Tier model refs use the tier's provider; every tier's provider is `pi`, so
  routine and rigorous nodes alike run on the same local model.
- Only workflows pinned to claude with **no tier model** stay claude-locked
  (archived below).

## Parallelism: serial by default

llama-server runs the local model with `--parallel 1` (one slot), so AI
requests are serialized at the model. Two opt-in knobs create parallelism,
and both end up queueing behind that single slot:

- **Mode B factory** (`news/factory.json`): `max_concurrent: 1` +
  `default_tactic: "oneshot"` — workflows dispatch one at a time, serially.
  Raising `max_concurrent` fans independent workflows out in parallel; they
  then share the one llama-server slot.
- **Fusion** (`allow_fusion: true`): fusion-tagged workflows embed parallel
  AI nodes inside a single workflow run (parallel planners/implementers plus
  a judge — e.g. the `archon-review-block` five-agent review block). Those
  parallel nodes are real AI nodes, but they still share the one
  llama-server slot, so they queue behind each other rather than running
  concurrently at the model.

Higher factory caps and fusion are retained opt-ins; serial execution is the
default because the local worker serves one request at a time.

## Usable workflows (Pi: local Qwen)

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
claude-only features (hooks, interactive relay). All pi-usable nodes run on
the local Qwen model. Original YAMLs are preserved at
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
  `## How to test` handoff and the `## Deferred work` section the board poller
  parses).
- `archon-idea-to-pr.yaml`: `completion-comment` node after `workflow-summary`
  (same structured record + `## How to test` handoff + `## Deferred work`
  section).
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
