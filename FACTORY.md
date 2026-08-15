# How a human talks to this

## Mode A

Open a folder. Run `omp`. Talk. That is the whole contract.

Do not type `orchestrate` expecting a Piyaz factory. That word is stock OMP multi-agent, nothing more.

To promote later: say **projectify** and run `projectify.py` in that repo.

## Mode B — new idea

1. Write the idea in the Piyaz UI (or a `.scratch/<slug>/spec.md`).
2. Stamp **lights-on** if you want to sit the gates (taste, look, naming). Omit it for mechanical work with a named test gate — triage defaults to lights-off then.
3. Approve the spec (`to-spec`).
4. Approve the vertical-slice cut (`to-tickets`). Each slice is demoable, one context window, with blocking edges.
5. Lights-off: the poller may claim unblocked `planned` slices.
6. Lights-on: you or the PM start the run.
7. Review the **git PR**. Mark the Piyaz / scratch task **done** only after QA (or you) pass the ACs.

## Lights

| Stamp | Meaning |
|---|---|
| `lights-on` tag (Piyaz) or `Lights: on` (scratch) | Human sits the run. Poller will not claim. |
| `lights-off` or omitted + mechanical + a gate | Poller may claim. |
| Taste / UI look / naming / no binary criterion | Treat as on even if you forgot the stamp. |

## Tactics (optional tags)

`oneshot` · `fusion-02` · `fusion-04` · `fusion-10`

Default: **fusion-02** (Luna + DeepSeek, Grok fuse). Tag `oneshot` for a single DeepSeek implementer. Luna is not a Mode A worker — only vision, and Mode B panel-a.

## What you never do

- Restart `com.bradley-mankoff.news-board-poller`. That factory is retired.
- Expect the new poller to merge PRs or invent GitHub Project lanes.
