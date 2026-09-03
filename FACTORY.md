# You, the human

Two ways to open a folder. Same `omp`. Different contract.

## Mode A — talk

```
cd some-folder
omp
```

Vibe. Sketch. Break things. Magic words (`orchestrate`, `ultrathink`) mean what OMP says. This is not a factory.

When the folder starts to matter: say **projectify**. After that, you are in Mode B in *that* repo.

## Mode B — factory

Your job is **ideas and gates**. You do not implement. You do not manage a GitHub Project board.

### The idea ritual (`new-idea`)

This is an **Archon workflow**, not a vibe. Mode A is for noodling. When the idea belongs in the factory:

1. In Mode A, `/handoff` → a `*handoff*.md`.
2. Open a Mode B session in the projectified repo.
3. Paste that file, or say **new-idea**. **Tag lights-on or lights-off here** — that is the stamp.
4. Sit the three gates already in the workflow: lights, spec, ticket cut.
5. It publishes tickets to **Piyaz** (or `.scratch/` if this repo has no Piyaz project). Then it **stops**. No code.

```
archon workflow run new-idea --no-worktree @/path/to/handoff.md lights-off
```

Or tell omp `new-idea` and point at the handoff. Same chain.

What each gate is asking:

- **Lights** — human gate is *only* UI aesthetics/user-friendliness + final newsletter/output review. If you will judge by looking at the app or the rendered report/newsletter, lights-on. Everything else — even “just run a command and verify output” — is lights-off. Machine runs pytest + archon-smart-pr-review and records the evidence; you never re-run commands to confirm what the machine already checked.
- **Grill** (inside the workflow) — who, what’s already true, what may stay broken, done-in-one-sitting. `/domain-modeling` / `/codebase-design` / `/wayfinder` join only if the talk needs them. You’re done when remaining questions are implementation.
- **Spec** — stories + out of scope. Approving means later PRs that implement *this* you will accept.
- **Tickets** — vertical slices (demo that ticket alone), each with blocked-by. Too coarse / too fine / wrong edges → send back.

### Kick the factory

Say **run the factory** (skill `run-factory`) in a Mode B session. That advances lights-off tickets (and only those). By default it runs **one workflow at a time** (`max_concurrent: 1`) and each slice uses the `oneshot` tactic — serial lights-off work, one slice implemented and pushed before the next is claimed.

Two separate opt-ins add parallelism:

- **`max_concurrent > 1`** in `factory.json` — the poller may run *multiple workflows in parallel*, each on its own slice. This is workflow concurrency.
- A **`fusion-*` tag** on a ticket — that *one* workflow runs two parallel implementers (isolated copies) and a judge fuses the results. This is parallel nodes *inside* one workflow.

Nothing is parallel unless you turn one of these on. With a local model, expect each slice to take noticeably longer than hosted work — that is the lights-off trade you chose.

When something needs you (in-review, lights-on, “does this look right”): **that session should halt**. Cmux dings. The message should include the PR URL, the ticket ref, and a screenshot if it’s UI. SMS/iMessage is not wired yet — ding-on-halt is the current ping.

Do not keep a factory session spinning after it owes you a look.

### Stale runs and fresh briefs

Each dispatch writes a local `.scratch/factory-ledger.json` record with the
ticket ref, workflow, branch, PID, attempt, and last observed state. The next
poller cycle reconciles it with live `workflow status` plus history `workflow
runs` snapshots.

If Archon disappears from the live/history snapshots after the dispatch grace
period, or exceeds the configured runtime, the poller prints
`STALE <ref>: <reason>`. It does not mark the ticket done,
invent a tracker status or automatically retry. Re-open the ticket in
Piyaz or change a scratch ticket back to `ready-for-agent` when you have
decided to retry. Retries are bounded by `max_attempts` in `factory.json`.

Every Archon dispatch receives a fresh brief containing the ticket text or
Piyaz task details, acceptance criteria, decisions, the repository gate, and
the in-review/PR contract. The brief is passed through the workflow request;
the task graph remains the source of truth.

### What happens without you

Unblocked lights-off tickets get claimed. Implementers write code. If the slice has a testable seam they should use **TDD** — red, green, the test is the ticket’s acceptance, not a souvenir. You do not type `/tdd`. Before creating or pushing a draft PR, the factory runs a deterministic validation barrier that prepares declared dev dependencies with `uv sync --group dev` when uv is available, then runs `uv run python -m pytest -q`; without uv it falls back to `.venv/bin/python3 -m pytest tests/ -q`. It stages the candidate tree with `git add -A`, then checks the staged candidate diff with `git diff --cached --check` after pytest; failed validation leaves no new PR head to notify on. A **draft PR** appears only after that barrier passes. The ticket goes **in review** on a later poller cycle once the run completes successfully; if the tracker sync…

### When something comes back

Open the draft PR only to judge what only you can judge:

1. Does the UI look right? (layout, copy, controls, placement). If yes/no — that’s taste, and it should have been lights-on.
2. Does the finished newsletter/output look right? (report rendering, story selection, images). Open the report.

Do NOT re-run commands or re-check pytest output — the machine already recorded `Machine checks` on the issue and in the PR. The `## How to test` section on every issue separates `### Machine checks` (already executed, with recorded pass/fail) from `### Human checks` (only UI look + final output). If the machine checks are green, accept them.

Then: merge, mark the ticket **done**. Done means the machine checks saw the ACs; confident prose is not evidence.

**Wrong spec or wrong cut:** say so. Revise tickets. Do not let anyone “just add a bit” off-graph.

### What you never do

- Restart the old news GitHub-Projects poller.
- Type `orchestrate` expecting tickets. That is Mode A.
- Mark **done** because the agent sounded sure.

## Piyaz and git (not two products)

**Piyaz is the task graph.** Ideas, slices, blocked-by, lights, in-review, done.
**Git / GitHub is the code.** Commits, draft PRs, review comments, merge.

Piyaz does **not** replace git. You are not duplicating the factory: a ticket says *what*, a PR says *the diff*. The implement workflow opens a draft PR; the PR stays in GitHub. The deterministic join is the ticket’s status moving to **in review** — the task ref on the PR links the two. That is the join, not a second board.

`.scratch/` is the same tickets as files, for repos with no Piyaz project and for OSS clones with no Piyaz login. Prefer Piyaz when you want the sidebar.

## Lights

| You say | Meaning |
|---|---|
| lights-on | You will judge UI aesthetics/user-friendliness or the final newsletter/report by looking. Factory never claims it. |
| lights-off, or nothing | Factory may claim it. Machine runs every `Machine checks` (pytest, archon-smart-pr-review, `output/**` artifacts) and records pass/fail. You never re-run commands to verify output. |
| `just run X and check output` | Still lights-off — machine already did it. Tell the human only about UI look + final output. |

## Optional tactic tags (on a ticket)

`oneshot` — one implementer on the `small` tier. **Default.**
`fusion-02` / `fusion-04` / `fusion-10` — two `small` implementers in parallel (isolated copies), then a `large` judge fuses both into one result. Tagging is the only way to get parallel work inside a slice.

The default local Archon profile maps `large` to Muse Spark 1.2 Contributor
at `xhigh` and `small`/`medium` to Ox Alpha Free at `max`. Override those
tiers in your Archon user config when the work needs a different profile.
