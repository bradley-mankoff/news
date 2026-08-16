# News Mode B map

**Status: superseded.** The queue now lives in the Piyaz project **DN**
("Daily News", app.piyaz.ai, Bradley's Team); this file is kept as the
historical GitHub-issue map. New slices come from `new-idea` and publish to
DN directly.

Historical map (all GH issues below are CLOSED — shipped via the GitHub flow):

| Slice | GH | Tactic | Blocked by | Outcome |
|---|---|---|---|---|
| 01-translation-policy | #33 | fusion-02 | | shipped |
| 02-translation-stage | #172 | fusion-04 | 01 | shipped |
| 03-translation-recommend | #88 | oneshot | 02 | shipped |
| 04-readme-publish-install | #60 | oneshot | | shipped |
| 05-settings-profile-failfast | #104 | oneshot | | shipped |
| 06-prompt-profile-once | #107 | oneshot | | shipped |
| 07-editorial-blocklist-tests | #109 | oneshot | | shipped |
| 08-knowledge-model-catalog | #95 | oneshot | | shipped |
| 09-registry-advanced-knobs | #114 | fusion-02 | | shipped |
| 10-model-knob-labels | #121 | oneshot | | shipped |
| 11-diagnostics-allowlist | #132 | oneshot | | shipped |
| 12-provenance-docs | #173 | oneshot | | shipped |
| 13-runlog-sink-isolation | #174 | fusion-02 | | shipped |
| 14-runlog-reducer-tests | #175 | oneshot | | shipped |
| 15-hw-compat-panel | #98 | fusion-02 | lights-on | shipped |

## Open work imported into DN (2026-08-16)

| DN task | GH | Title |
|---|---|---|
| DN-1 | #229 | Fix poller run-list truncation above 200 runs |
| DN-2 | #169 | Decouple NEWS_MODEL default from backend concurrency defaults |
| DN-3 | #100 | Add full-template LLM prompt editors to Advanced Settings |
| DN-4 | #89 | Expand curated catalog with runtime-verified entries |
| DN-5 | #86 | Record ADR 0011 Model Catalog ownership |
| DN-6 | #82 | Serve model task and runtime-fit labels via /api/schema |
| DN-7 | #81 | Demote transformers vision search results to external_only |
| DN-8 | #79 | Audit programmatic model-knob setter paths |
| DN-9 | #63 | Test scrub_history dry-run against a local fixture mirror |

Not in queue (human): #62 history scrub, #170 GitHub protections.
