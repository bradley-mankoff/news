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

- **Lights** — will you judge this by looking? If yes, on. If pytest can fail for the thing you care about, off.
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

Unblocked lights-off tickets get claimed. Implementers write code. If the slice has a testable seam they should use **TDD** — red, green, the test is the ticket’s acceptance, not a souvenir. You do not type `/tdd`. A **draft PR** appears. The ticket goes **in review** on a later poller cycle once the run completes successfully; if the tracker sync fails it is retried on the next cycle. The poller still never marks **done** — that gate is yours (or QA’s).

### When something comes back

Open the draft PR. You are not doing a full code review unless you want to. You are checking:

1. Does this match the **ticket**, not a larger dream?
2. Is there a test or a machine check that would fail if the story were a lie?
3. Anything that is actually taste? If yes, it should have been lights-on — bounce it rather than rubber-stamp.

Then: merge (or ask QA to), mark the ticket **done**. Done means you (or QA) *saw* the ACs. Confident prose is not evidence.

**Lights-on tickets:** you stay in the session. The factory will not claim them. You are the criterion.

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
| lights-on | You sit it. Factory will not claim it. |
| lights-off, or nothing + it has a test gate | Factory may claim it. |
| taste / look / naming | Treat as on even if you forgot. |

## Optional tactic tags (on a ticket)

`oneshot` — one implementer on the `small` tier. **Default.**
`fusion-02` / `fusion-04` / `fusion-10` — two `small` implementers in parallel (isolated copies), then a `large` judge fuses both into one result. Tagging is the only way to get parallel work inside a slice.

The default local Archon profile maps `large` to Muse Spark 1.2 Contributor
at `xhigh` and `small`/`medium` to Ox Alpha Free at `max`. Override those
tiers in your Archon user config when the work needs a different profile.
