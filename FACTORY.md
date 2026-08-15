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

Say **run the factory** (skill `run-factory`) in a Mode B session. That advances lights-off tickets (and only those). Parallel up to `factory.json` cap.

When something needs you (in-review, lights-on, “does this look right”): **that session should halt**. Cmux dings. The message should include the PR URL, the ticket ref, and a screenshot if it’s UI. SMS/iMessage is not wired yet — ding-on-halt is the current ping.

Do not keep a factory session spinning after it owes you a look.

### What happens without you

Unblocked lights-off tickets get claimed. Implementers write code. If the slice has a testable seam they should use **TDD** — red, green, the test is the ticket’s acceptance, not a souvenir. You do not type `/tdd`. A **draft PR** appears. The ticket goes **in review**.

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

Piyaz does **not** replace git. You are not duplicating the factory: a ticket says *what*, a PR says *the diff*. The implement workflow already opens a draft PR and can write `prUrl` back onto the Piyaz task. That is the join, not a second board.

`.scratch/` is the same tickets as files, for repos with no Piyaz project (news today) and for OSS clones with no Piyaz login. Prefer Piyaz when you want the sidebar. Create **Daily News** in Bradley’s Team (do not reuse PIN). Then we flip news `factory.json` to that project; `new-idea` publishes there.

## Lights

| You say | Meaning |
|---|---|
| lights-on | You sit it. Factory will not claim it. |
| lights-off, or nothing + it has a test gate | Factory may claim it. |
| taste / look / naming | Treat as on even if you forgot. |

## Optional tactic tags (on a ticket)

`oneshot` — one DeepSeek implementer.
`fusion-02` / `fusion-04` / `fusion-10` — Luna and DeepSeek in parallel, Grok fuses. Default is fusion-02.

You do not pick models. You pick “how hard is this slice?” if you care; otherwise the default is fine.
