# Ticket workflow menu

Read this when processing an idea into a ticket. Pick ONE doing workflow and
ONE review workflow per ticket based on the ticket's content, then attach them
(label convention below). The menu is the minimum set — all workflows are
Archon's bundled, tried-and-tested ones installed in the archon home.

## Doing (4)

| Workflow | Attach when the ticket is… | Label |
|---|---|---|
| `archon-fix-github-issue` | a bug, defect, regression, or small concrete task | `bug` (also the default for unlabeled) |
| `archon-idea-to-pr` | a feature, enhancement, or new capability | `feature` |
| `archon-plan-to-pr` | already carries an implementation plan (plan file committed at `.agents/plans/<slug>.md` before labeling) | `plan` |
| `archon-assist` | nothing above fits (exploration, one-off, infra) | — |

## Reviewing (2)

| Workflow | Attach when… |
|---|---|
| `archon-smart-pr-review` | default for every PR (adapts to PR complexity) |
| `archon-comprehensive-pr-review` | the PR is large or risky (broad blast radius, cross-cutting changes, auth/data paths) |

## Model routing (fixed)

| Model | Role | Basis |
|---|---|---|
| **Qwen3.8 27B Q4 (local)** | every node — ticket creation, scout/routine, planning, implementation, and review — the `small`/`medium`/`large` tiers all resolve to `local-qwen/qwen3.8-27b-q4` on the `pi` provider | local llama-server at `http://127.0.0.1:8080/v1` (`--parallel 1` serial capacity, max reasoning) |

Do not add third-party or discounted models to this menu. The fusion
experiment that used them is abandoned (2026-08-17).

## Dispatch rules

- **8pm CST cutoff (hard, poller-enforced):** no NEW workflow dispatches after
  8pm CST (= 02:00 UTC). This is a legacy operational quiet-hours policy
  predating the local-Qwen setup — not a model-cost requirement (the local
  model has no per-token pricing). The board poller enforces this
  mechanically:
  `automation/config.json` → `no_dispatch_after_local_hour: 20` — dispatch,
  resume, and conflict-fix starts are deferred past that hour and queued for
  the next morning. Running workflows are allowed to finish and merge.
  Tickets created after 8pm CST queue and dispatch the next morning.
- Attachment mechanics (news board): the board poller reads
  `automation/config.json` `dispatch.todo.label_overrides` — a ticket's label
  selects its doing workflow; `review.workflow` is the review default. Set the
  label when creating the ticket; escalate a PR to
  `archon-comprehensive-pr-review` explicitly for large/risky changes.
