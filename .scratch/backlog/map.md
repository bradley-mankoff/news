# News Mode B map

GitHub Projects is not the board. Slices: 15 re-sliced Mode B backlog tickets
created 2026-08-15 during Mode B adoption (commit 8984b9b), originally as
`.scratch/backlog/issues/`, now loaded into the Piyaz project **DN** ("Daily
News", app.piyaz.ai, Bradley's Team) as DN-10..DN-24.

| Slice | GH | Tactic | Blocked by | DN task |
|---|---|---|---|---|
| 01-translation-policy | #33 | fusion-02 | | DN-10 |
| 02-translation-stage | #172 | fusion-04 | 01 | DN-11 |
| 03-translation-recommend | #88 | oneshot | 02 | DN-12 |
| 04-readme-publish-install | #60 | oneshot | | DN-13 |
| 05-settings-profile-failfast | #104 | oneshot | | DN-14 |
| 06-prompt-profile-once | #107 | oneshot | | DN-15 |
| 07-editorial-blocklist-tests | #109 | oneshot | | DN-16 |
| 08-knowledge-model-catalog | #95 | oneshot | | DN-17 |
| 09-registry-advanced-knobs | #114 | fusion-02 | | DN-18 |
| 10-model-knob-labels | #121 | oneshot | | DN-19 |
| 11-diagnostics-allowlist | #132 | oneshot | | DN-20 |
| 12-provenance-docs | #173 | oneshot | | DN-21 |
| 13-runlog-sink-isolation | #174 | fusion-02 | | DN-22 |
| 14-runlog-reducer-tests | #175 | oneshot | | DN-23 |
| 15-hw-compat-panel | #98 | fusion-02 | lights-on | DN-24 |

Not in queue (human): #62 history scrub, #170 GitHub protections.
Obsolete (old poller): #279, #289.

Notes:
- The GH issues these slices reference were closed NOT_PLANNED at 21:57
  Aug 15 as part of the Mode B adoption (board cleanup), NOT because the work
  shipped. #172 and #114 have partial work on unmerged worktree branches
  (archon/task-issue-172, archon/task-issue-114; PRs 278/260 closed without
  merge) — that is the "partial work erased" from the midstream shutdown.
- 01-translation-policy was claimed and dispatched once (fusion-02 run
  e026be37, 2026-08-15 21:58); that run was abandoned as a zombie on
  2026-08-16 and the ticket reset to ready-for-agent. DN-10 is the live
  copy of that slice.
