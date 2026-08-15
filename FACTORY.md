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

### The idea ritual (do this every time)

1. **Colloquial idea.** In an `omp` session in the projectified repo, say the thing in your own words. One paragraph is enough. Stamp **lights-on** if it is taste / look / naming / “does this look right.” Omit that for mechanical work with a test gate.
2. **`/grill-me`.** Relentless interview. Answer until the idea has edges. If the idea is really a design or a language problem, the session should pull in `/codebase-design` or `/domain-modeling` (or `/wayfinder`) *during the grill* — those are how we steal Pocock’s language instead of inventing our own.
3. **`/to-spec`.** No second interview. It writes a spec from what you already said and publishes it (Piyaz note/spec, or `.scratch/<slug>/spec.md`). You approve the spec or send it back.
4. **`/to-tickets`.** Cuts the spec into **vertical slices** — each one demoable, one sitting, with “blocked by.” You approve granularity (too coarse / too fine / merge / split). Then the tickets exist.

That is the whole intake. After tickets exist, you can walk away from lights-off work.

### What happens without you

Unblocked lights-off tickets get claimed. An implementer (sometimes two, fused) writes code. **TDD** is the implementer’s craft skill when the slice has a testable seam — you do not invoke it. A **draft PR** shows up on git. Status becomes **in review**.

### What you do when something comes back

- **Lights-off, mechanical:** glance the PR if you want; QA (or you) ticks the acceptance boxes; someone marks the ticket **done** and merges.
- **Lights-on:** you sit the run. You are the criterion. “Does this look right?” is not delegated.
- **Wrong cut / wrong spec:** say so. Tickets get revised. Do not silently implement around them.

### What you never do

- Restart the old news GitHub-Projects poller.
- Type `orchestrate` expecting Piyaz tickets. That is Mode A.
- Mark **done** because the agent sounded confident. Done means the ACs were seen.

## Piyaz vs this folder

Piyaz’s left sidebar lists **projects in Bradley’s Team**. Today that is **Build Browser Pinball (PIN)** — the pinball graph. There is no Daily News project because nobody created one.

News Mode B is currently **`.scratch/` in the news repo** (same ticket shape, no sidebar). That is why you do not see news in Piyaz.

When you want the sidebar: in Bradley’s Team, **new project** named Daily News (do not reuse PIN). Then we point news `factory.json` at that project and `/to-tickets` publishes there. Same ritual. Better UI.

## Lights (one stamp)

| You say | Meaning |
|---|---|
| lights-on | You sit it. Factory will not claim it. |
| lights-off, or nothing + it has a test gate | Factory may claim it. |
| taste / look / naming | Treat as on even if you forgot. |

## Optional tactic tags (on a ticket)

`oneshot` — one DeepSeek implementer.
`fusion-02` / `fusion-04` / `fusion-10` — Luna and DeepSeek in parallel, Grok fuses. Default is fusion-02.

You do not pick models. You pick “how hard is this slice?” if you care; otherwise the default is fine.
