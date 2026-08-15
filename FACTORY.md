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

### The idea ritual

#### 1. Colloquial idea

In `omp`, in the projectified repo, say it like you’d say it to a coworker. One paragraph. Name the user-visible change if you can. You do **not** need files, APIs, or ticket titles yet.

Stamp **lights-on** in that first message if any of these is true:

- you will judge it by looking (UI, copy, naming, “does this feel right”)
- there is no test that could fail for the thing you care about
- you want to sit the session anyway

If it is “make X true and pytest can see it,” omit the stamp.

#### 2. `/grill-me`

This is the interview. The agent should not start coding. It should make you choose.

Typical questions, in English: who is this for; what is already true; what must stay true; what you are willing to leave broken; what “done” looks like in one sitting; whether this is really one idea or three.

**When the grill should change tools (you do not have to remember the names):**

| The conversation is about… | Skill that should join |
|---|---|
| What we *call* things, CONTEXT.md, an ADR | `/domain-modeling` |
| Where a module’s interface sits, how to test it, “deep vs shallow” | `/codebase-design` |
| “I don’t know the terrain” | `/wayfinder` |

Those skills exist so the spec inherits Pocock’s words (`module`, `interface`, `seam`, `depth`) instead of a new private jargon. If the grill never needed them, skip them. Don’t run them as a checklist.

You are done grilling when you are slightly annoyed and the remaining questions are implementation. Then stop.

#### 3. `/to-spec`

No second interview. It writes down what you already decided.

You should see: problem in the user’s voice, solution in the user’s voice, a long list of user stories, implementation decisions **without file paths**, testing decisions (seams, not test filenames).

**Your gate:** read the stories and the “out of scope.” If a story is gold-plating or a missing story is the actual product, send it back. Approving the spec is you saying “if the tickets implement *this*, I will accept the PRs.”

#### 4. `/to-tickets`

Cuts the spec into **vertical slices**. Vertical means each ticket walks through the layers that ticket needs (data, logic, UI, a test) far enough that you could demo *that* ticket alone. Horizontal means “all the schema this week, all the UI next week” — that is the cut we refuse.

Each ticket names **blocked by**. A ticket with no blockers can start the moment you approve.

**Your gate:** too coarse (a ticket you couldn’t review in one sitting) / too fine (tickets that cannot demo) / wrong edges (B listed as blocked by A when A does not actually gate B). Say merge, split, or rewire. Then stop. The tickets now exist.

That is intake. Lights-off work can proceed without you.

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

## Two trackers (same ritual)

`/to-spec` and `/to-tickets` publish to **whichever tracker the repo is pointed at**. You do not change how you talk.

| | **Piyaz** | **`.scratch/`** |
|---|---|---|
| What you see | Team sidebar, Graph, Notes, PIN-12 | Markdown under `.scratch/<feature>/` |
| Where it lives | Piyaz’s servers | **This git repo** |
| Who can use the factory | You, after a Piyaz login | Anyone who cloned the repo |
| Offline / airplane | No | Yes |
| Review the ticket text in a PR | No (tickets are off-repo) | Yes (the slice files *are* the PR) |
| “What’s ready?” UI | Graph / ready view | `Status:` + `Blocked by:` in files |

**Why keep `.scratch` if Piyaz is nicer**

Not primarily “Piyaz might charge later.” That is a side benefit, not the design reason.

1. **The factory has to work without a SaaS account.** omp-modes is meant to be something you can hand someone with stock OMP. Requiring Piyaz would make Mode B a Piyaz customer feature. `.scratch` is the zero-account backend.
2. **Tickets in git are reviewable.** A slice’s wording is a product decision. On `.scratch` that decision is a file: greppable, `git log`, same PR as the code if you want. Piyaz is a better *cockpit*; it is a worse *archive* for an open repo.
3. **News is already on `.scratch`** because Daily News is not a Piyaz project yet. Pinball is PIN. Do not reuse PIN. When you create **Daily News** in Bradley’s Team, we point news `factory.json` at it and new `/to-tickets` land in the sidebar. Old scratch files can stay as history or get imported once — that is a one-time move, not a reason to delete the adapter.

Use Piyaz when you want the Graph and to sit in the driver’s seat in a browser. Use `.scratch` when the repo should be self-contained (OSS, offline, or “I have not made a Piyaz project yet”). Same grill → spec → tickets.

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
