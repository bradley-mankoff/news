# Daily News Pipeline

Python 3.12 `uv` project for building and sending the daily news report. Run
commands from the repo root so `uv` uses this project environment.

```bash
git clone https://github.com/bradley-mankoff/news.git
cd news
uv sync
uv run python -c 'import platform; print(platform.machine())'
```

On Apple Silicon, the platform check should print `arm64`. `uv` picks the
Python version from `.python-version` (3.12) automatically.

> **Package status**: `news-pipeline` (ADR 0009) is the public distribution
> name, but the package is not published to PyPI yet — install instructions
> will be added here at release time.

## Project Automation

The repo runs a fully automated agentic loop driven by the GitHub project board
(Projects v2, project #1 “Build public UI”, owner `bradley-mankoff`).

### Board flow

- Lanes: `Backlog` → `Todo` → `In Progress` → `Blocked` → `Needs Input` → `Ready for Review` → `In Review` → `Done`.
- `Blocked` = dependency-gated: an issue dragged to `Todo` whose `Depends on:`
  refs are not all in `Done` moves here with a comment, and returns to `Todo`
  (auto-dispatch) when its dependencies ship.
- `Needs Input` = the agent asked the human a question (see the `NEEDS INPUT`
  comment + `needs-input` label on the issue); answer on the issue and drag
  the ticket back to `Todo` — the poller resumes the workflow in the same
  worktree (`archon continue`) instead of starting over.
- Branch model: `main` = production; `develop` = integration (repo default branch,
  workflow PRs target it); every issue works on its own branch
  (`archon/task-issue-<N>`) in an isolated worktree, so issues in `Todo` run in
  parallel.
- The poller caps concurrent Archon workflows at `max_concurrent_workflows`
  (currently `10`, matching `MAX_CONCURRENT_CONVERSATIONS`). It counts active
  and paused runs before dispatching, reserves slots for dispatches made in the
  current poll, and holds new dispatches when the status lookup is unavailable.
- Creating an issue lands it in `Backlog`; nothing starts from `Backlog`.
- **Slicing convention:** slice issues as tracer bullets — narrow, end-to-end
  vertical slices (schema → API → UI → tests), each demoable and sized to one
  context window — and keep parallel runs merge-safe by declaring file/area
  ownership in the plan. Ownership may be function- or component-level when
  two changes truly touch different parts of one file. Two issues may run in
  parallel only when their planned ownership areas are disjoint; overlapping
  ownership means the later one declares `Depends on: #<earlier>`. Any set of
  issues with satisfied dependencies can be triggered together.
- A `Depends on: #N` line (one line; `#42, #57` for several) gates dispatch:
  an issue dragged to `Todo` with an unsatisfied dependency moves to `Blocked`
  with a comment and returns to `Todo` (auto-dispatch) when the dependency
  ships. `Blocked` is exclusively dependency gating; `Needs Input` is
  exclusively NEEDS INPUT questions.
- `priority: critical` is the human queue-priority label for production
  blockers. Filter the project by that label to bubble critical bugs above
  normal backlog work; labels do not change the board's manual card order.
- `runnable` is maintained by the poller: it marks open issues in `Todo` whose
  declared dependencies are all in `Done`, and is removed when the issue is
  blocked, dispatched, closed, or otherwise leaves `Todo`.
- Moving an issue into `Todo` triggers an Archon workflow (label-aware: `bug`
  → `archon-fix-github-issue`, `feature`/`enhancement` → `archon-idea-to-pr`,
  default → `archon-fix-github-issue`), and the poller moves the issue to
  `In Progress`.
- When the dispatched run completes, the poller marks the PR ready and merges
  it into `develop`, then moves the issue to `Ready for Review`. It posts a
  final handoff comment at the bottom of the issue with the recorded
  issue-specific test steps (or an explicit no-runnable-path fallback).
  The human tests the integration branch from there.
- **Develop conflicts auto-fix:** implementation workflows sync with `develop`
  before opening their PR (parallel runs progressively absorb each other); a
  PR that still conflicts at merge time is resolved automatically — the
  poller first merges `develop` into the branch via the GitHub merge API, then
  dispatches `archon-fix-develop-conflicts` once per conflict episode. If the
  fix run finishes and the PR is still conflicting, the poller comments on the
  issue asking for manual help (merge develop into the branch; the poller
  merges automatically once it is mergeable).
- Deferred work is auto-tracked (the deferral strategy): the completion record
  on the issue carries a `## Deferred work` section — one bullet per deferred
  item with `**Title:**` / `**Description:**` / `**Reason:**` / optional
  `**Label:**` — enforced by the workflow-side `completion-comment` nodes, which
  must trace the original issue ask (every criterion, described behavior, or
  named component in the issue body that the run did NOT ship is deferred work
  and must be listed). The node also judges each item against a size bar:
  only independently schedulable deliverables are spawned as issues; small
  chores, test/doc tweaks, and review findings belonging to the parent are
  stamped `**Skip:**` (preserved in the record, not the backlog). It then
  dedupes by consulting all open/closed issue titles + initial bodies and repo
  context (pending checklists, ADRs, `.out-of-scope/`) and stamps each item
  `**Links to:** #N` (already tracked), `**Supersedes:** #N` (closed — new
  issue referencing it), `**Out of scope:** <slug>` (durable rejection — the
  poller records it in `.out-of-scope/<slug>.md`), `**Skip:** <reason>`, or
  leaves it bare. When a run completes, the poller
  executes mechanically: links, creates (boarded in the default lane),
  skips, and comments the linkage on the source issue (an exact-title safety
  check links but never creates). Deferral language or
  unchecked acceptance criteria (`- [ ]`) without the section post a
  verification comment instead — never an auto-created issue from prose.
- Moving an issue into `In Review` makes the poller open the ship PR
  (feature → `main`), run `archon-smart-pr-review` on it, and on review
  completion merge it into `main` and move the issue to `Done` automatically.
- Agents move issues with `python3 automation/move_item.py <issue> <lane>`.

### Two review stages (by design)

1. **Readiness review** — inside the implementation workflows, before the human
   sees anything: `archon-fix-github-issue` runs a smart review (code review +
   conditional error-handling/test/comment/docs) then self-fixes and simplifies;
   `archon-idea-to-pr` runs a 5-agent review block and fixes findings. The bar:
   “the human should not have to check whether it works, is complete, or
   matches the issue intent.” Implementation PRs are left **draft** so you can
   test the branch locally first.
2. **Quality review** — the `In Review` lane trigger (`archon-smart-pr-review`):
   after you judge the feature working and move the ticket, the review targets
   code quality, conventions, and subtle/peripheral breakage, and auto-fixes
   CRITICAL/HIGH findings. It runs on the final diff — including anything you
   changed during testing.

The workflows are the stock Archon 0.7.0 pi-usable set, curated in the archon
home (`workflows/`); claude-only workflows are archived, not discovered. Full
inventory: `docs/archon-workflows.md`.

### Human touchpoints

Everything else in the loop is automated (dispatch, merges, lanes, deferred
issue creation, conflict auto-fix). These are the only actions that need a
human, by design:

- **Test + promote:** when an issue lands in `Ready for Review`, follow the
  bottom-of-issue test handoff comment against the integration branch; when it
  works, move it to `In Review` with
  `python3 automation/move_item.py <N> "In Review"`.
- **Answer Needs Input:** an issue in `Needs Input` carries a `NEEDS INPUT:`
  comment (with `needs-input` label) — answer on the issue, then drag it back
  to `Todo` to resume the workflow in place.
- **Re-review after a held ship:** if the ship review posts anything other
  than `VERDICT: approve`, fix the findings and drag the issue back to
  `In Review`.
- **Ship-conflict manual help:** if the poller comments that it could not
  resolve ship-PR conflicts automatically, merge `main` into the branch (or
  rewrite the conflicting lines) and drag the issue back to `In Review`.
- **Develop-conflict manual help:** if the poller comments that the develop
  resolver could not fix a conflict, merge `develop` into the branch (or
  rewrite the conflicting lines) — the poller merges automatically once the
  branch is mergeable; no re-drag needed.
- **Security gate (deliberate):** the history scrub requires human approval —
  run `automation/scrub_history.sh --dry-run`, review, then `--execute`
  (runbook: `docs/security/history-scrub.md`).
- **Periodic health check:** `python3 automation/board_health.py` prints stale
  runs, unknown blockers, and unsatisfied dependencies (read-only).
- **Deploy after changes:** after pulling poller/automation changes or
  reinstalling archon, run `automation/deploy.sh` (re-applies local workflow
  edits, restarts the poller).
- **New ideas (top of funnel):** describe the idea in the agent session for
  this repo (any format). The agent grills you — what & why, binary acceptance
  criteria, out of scope, `Depends on` — then creates the issue in `Backlog`
  via `python3 automation/create_issue.py`. Nothing auto-detects ideas from
  chat; the board is the source of truth and work starts only when you drag
  the issue to `Todo`.
- **New issues:** `python3 automation/create_issue.py "<title>"` creates the
  issue, boards it, and lands it in the default lane in one step (add
  `--label enhancement` for the idea-to-pr workflow; fill `Depends on` in the body
  when gating). Work starts only when the issue is dragged to `Todo`.

### Components

- `automation/board_poller.py` — polls the board every 45s, dispatches Archon
  runs on lane transitions; moves the item to `In Progress` on dispatch, merges
  the feature PR into `develop` and moves to `Ready for Review` when the run
  completes, and on review completion merges the ship PR into `main` and moves
  to `Done`. First poll after (re)start is a snapshot: state is recorded,
  nothing is dispatched (prevents backlog bursts after downtime).
- `automation/config.json` — repo, project, lanes, and workflow mapping.
- `automation/move_item.py` — move an issue to a lane from the CLI.
- `automation/create_issue.py` — create an issue and land it on the board in
  the default lane in one step.
- `automation/board_health.py` — read-only board health report: stale runs,
  unknown blockers, unsatisfied dependencies (exit 0 always).
- `automation/deploy.sh` — re-apply local archon workflow edits and restart
  the board poller after a deploy or archon reinstall.
- `automation/apply_workflow_edits.py` — idempotently re-apply the local
  archon workflow edits (completion-comment nodes with the Deferred-work
  contract, `report-verdict`, `archon-fix-ship-conflicts`) after an archon
  reinstall; `automation/deploy.sh` also restarts the poller.
- `automation/security_audit.py` — stdlib-only scanner for secrets and personal
  data in the working tree and full history; exits 0 when clean. Report:
  `docs/security/audit-2026-08-02.md`.
- `automation/scrub_history.sh` — gated `git filter-repo` history scrub; prints
  push commands by default (`--dry-run`), requires explicit `--execute` and
  human approval. Runbook: `docs/security/history-scrub.md`.
- The poller runs as a launchd agent (`com.bradley-mankoff.news-board-poller`,
  plist in `~/Library/LaunchAgents/`). Logs: `automation/board_poller.log`;
  state: `automation/state.json` (gitignored).
- Archon executes all workflows on OpenCode Zen Go (`opencode-go/deepseek-v4-flash`,
  max effort → xhigh thinking) via the Pi provider; tiers are configured in
  the archon-pi home `config.yaml`.

### Manual review

Review a PR by hand:

```bash
archon workflow run archon-smart-pr-review "Review PR #123"
```

### Monitoring

- Archon runs: `archon workflow runs` (run from the repo root).
- Poller: `launchctl list | grep news-board-poller`, or
  `tail -f automation/board_poller.log`.
- Board: `gh project item-list 1 --owner bradley-mankoff --format json`.

### Local dev loop (automatic)

When the poller merges a PR into `develop` server-side, it also refreshes the
local checkout and restarts the UI automatically — no manual steps:

- `sync_local_develop()` in `automation/board_poller.py`: `git fetch` →
  fast-forward-only merge → UI restart (only if the UI is running on
  127.0.0.1:8766). Runs after every successful develop merge and once at
  poller startup (catches merges that landed while the poller was down).
- Strict skip boundaries (each logs one line in `automation/board_poller.log`):
  `--dry-run`, fetch failure, not on `develop`, dirty tree, unpushed local
  commits. Never forced, never destructive — unpushed local work is a human
  decision point (push it and the sync resumes).
- Test invocation that works reliably on this machine:
  `.venv/bin/python3 -m pytest tests/ -q` — plain `uv run pytest` / `uv run
  news` are intermittently flaky ("Failed to spawn") even with a healthy
  venv; if the `news` entrypoint ever vanishes from `.venv/bin`, re-run
  `uv pip install -e .`.

## UI

Start the guided local control panel from the repo root:

```bash
uv run news ui --open
```

By default it listens at `http://127.0.0.1:8766`. If you do not want the
browser opened automatically, omit `--open`:

```bash
uv run news ui
```

Use another port or host when needed:

```bash
uv run news ui --port 8770
uv run news ui --host 0.0.0.0 --port 8766
```

The UI runs until you stop it with `Ctrl-C` in the terminal. It can preview the
exact command and resolved Runtime Config Snapshot, launch and stop pipeline
runs, set `NEWS_` overrides for UI-launched commands, save/load Run Presets and
Model Tuning Presets, run source utilities, and edit `config/sources.yaml` or
`config/recipients.yaml`. Source and recipient edits write those YAML files
directly.

The **Report Review** tab is the read-only review surface for generated
reports. It shows the current report from `latest_run.md`/`latest_run_details.json`
(run id/time, run status, report status, preset/duration, and delivery status
as separate badges), lists recent completed and failed sessions from durable
DuckDB history, and can open a historical run's stable OKF `report.md`.
Report text is rendered as escaped plain text. When a run finishes in the UI,
the live stream closes and the review/history panels refresh automatically; a
completed report navigates to Report Review, while a failed run without a
report leaves you on Run Setup with the failure visible.

Report-generation status and optional email delivery status are independent:
a run with no sender/recipient/SMTP configuration finishes with delivery
`skipped: not_configured`, and a delivery failure is recorded as delivery
`failed` without failing the run or hiding the completed report. Runs recorded
before delivery tracking show `not recorded`. The UI only reads known rolling
and OKF artifacts; it never replaces or deletes DuckDB/CSV history or OKF
bundles, and it exposes no arbitrary filesystem routes.

The main Run Setup view is prompt-first: routing, editorial prompt profile, and
default model selection. Per-task model selectors, model tuning, pipeline
budgets, clustering thresholds, server settings, full prompt templates, and raw
environment overrides live under Advanced Settings.

## CLI

Run with a saved preset or explicit overrides:

```bash
uv run news run --preset NAME
NEWS_SOURCE_SCOPE=peripheral NEWS_RECIPIENT_SCOPE=primary uv run news run
```

Useful utility commands:

```bash
uv run news model-server-command
uv run news check-sources --only-failures
uv run news prune-sources --recent-days 7
uv run news source-languages --sources-yaml config/sources.yaml --json
uv run news serve-unsubscribe
```

## Run Settings

Most Run Settings are controlled by `NEWS_` environment variables. The core
ones are Run Preset selection, source/recipient scope, URL reuse blocking, model
selection, and image generation.

The accepted vocabulary separating Run Presets, Task Model Assignment, Model
Tuning, Pipeline Budget, and Model Server Settings is defined in
[`docs/adr/0007-model-configuration-vocabulary.md`](docs/adr/0007-model-configuration-vocabulary.md).

When running from a shell, put `NEWS_` assignments on the same command line or
export them first:

```bash
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news run
export NEWS_MODEL=gemma-e2b-tiny
uv run news run
```

### Run Presets

Run Presets live in `config/run_presets.yaml` as env-style Run Settings maps.
Preset IDs are opaque data; the code applies the selected Run Preset and then
applies any explicit shell/UI overrides on top.

```bash
uv run news run --preset NAME
NEWS_MODEL=gemma-4-12b-it-4bit NEWS_IMAGE_ENABLED=1 uv run news run
```

Key Run Settings:

- `NEWS_SOURCE_SCOPE=core|peripheral`: `peripheral` includes both core and
  peripheral sources.
- `NEWS_RECIPIENT_SCOPE=primary|all`: send to the primary recipient only or all active
  configured recipients.
- `NEWS_BLOCK_REUSED_URLS=0|1`: every run records URL history; only `1` makes
  previously recorded URLs block future reuse.
- `NEWS_IMAGE_ENABLED=0|1`: report image generation, default off unless a
  preset enables it.
- `NEWS_MODEL`: default model selection only. Task models are assigned
  separately with `NEWS_MODEL_ARTICLE_SUMMARY`, `NEWS_MODEL_STORY_DRAFTING`,
  `NEWS_MODEL_STORY_SCALE_SCREENING`, and `NEWS_MODEL_TITLE_GENERATION`.
  Stages with no LLM call of their own inherit a task model: image art
  direction runs on the Title Generation model (one shared LLM call), and
  story discovery has no LLM stage (embedding/TF-IDF clustering) so it
  inherits the default model.
- `NEWS_MODEL_BACKEND`: optional backend override for the default model
  (`mlx-lm`, `mlx-vlm`, or `external`; inferred from the model reference
  otherwise — see [Runtime Matrix](#runtime-matrix)).

### Prompt Profiles

Prompt Profiles are built-in editorial tone bundles for the five LLM prompt
stages (article summary, story scale screening, story drafting, title
generation, image art direction). They swap editorial instruction sentences
only; the pipeline's machine-required output contracts are unchanged. Prompt
Profile ownership is governed by the Prompt Catalog ADR
([`docs/adr/0010-prompt-catalog-owns-editorial-instructions.md`](docs/adr/0010-prompt-catalog-owns-editorial-instructions.md)),
not by Model Tuning.

```bash
uv run news run --prompt-profile playful
NEWS_PROMPT_PROFILE=facts-only uv run news run
```

Built-in profiles: `balanced` (default), `consensus-and-contradiction`,
`playful`, `facts-only`, `explain-like-im-five`. The UI's "Editorial approach"
panel selects a profile, edits per-stage prompts (defaults visible), and
restores defaults per stage or globally. Per-stage edits are stored in
`NEWS_PROMPT_OVERRIDE_<TASK>` env vars and layer on top of the selected
profile (override wins). The full per-task prompt templates and diffs against
`balanced` are under Advanced Settings. Profiles can also be pinned inside a
Run Preset's `env` map.

### Model Selection

```bash
NEWS_MODEL=gemma-e2b-tiny uv run news run
NEWS_MODEL=gemma-4-12b-it-4bit uv run news run --preset NAME
```

Task-specific model assignments inherit from `NEWS_MODEL` unless you set them
```bash
NEWS_MODEL_ARTICLE_SUMMARY=gemma-e2b-tiny uv run news run
NEWS_MODEL_STORY_DRAFTING=gemma-4-12b-it-4bit uv run news run --preset NAME
NEWS_MODEL_STORY_SCALE_SCREENING=gemma-e2b-tiny uv run news run
NEWS_MODEL_TITLE_GENERATION=gemma-4-12b-it-4bit uv run news run --preset NAME
```
Every actual LLM stage has its own assignment: Article Summarization, Story
Drafting, Story Scale Screening, and Title Generation. Two stages inherit by
design: `image_art_direction` shares the Title Generation LLM call (one prompt
produces both the art direction and the overlay headline, so it runs on the
Title Generation model), and `story_discovery` has no LLM stage — it is
algorithmic embedding/TF-IDF clustering and inherits the default model. There
is no `NEWS_MODEL_IMAGE_ART_DIRECTION` env var.
Built-in aliases:

- `gemma-e2b-tiny`: [`deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit`](https://huggingface.co/deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit) (Codex-safe test model)
- `gemma-4-12b-it-4bit`: [`mlx-community/gemma-4-12B-it-4bit`](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit) (default; the standard Gemma 4 12B instruction model, 256K-token context)

The legacy `qwythos-9b-*` aliases are **unsupported**: mlx-vlm cannot launch
file-qualified GGUF references, so stale configs fail fast with an actionable
error instead of a half-started server.

Each model page shows Hugging Face's native Hardware Compatibility panel
(GGUF/MLX quantizations) — the UI model picker links directly to it.

### Model Catalog

The Model Catalog is the code-owned registry of models verified for the
supported backends, with recommendations per task — factual extraction,
structured output, synthesis, citation fidelity, speed, context length, and
translation — rather than parameter count or popularity:

```bash
uv run news models catalog
uv run news models search --query gemma --task text-generation --limit 5
```

Curated models (2):

- `gemma-4-12b-it-4bit` — mlx-vlm, 256K-token context, default model
  ([Hugging Face](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit))
- `gemma-e2b-tiny` — mlx-lm, Codex-safe test model
  ([Hugging Face](https://huggingface.co/deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit))

Hugging Face search results carry runtime-fit verdicts (`managed_mlx_lm`,
`managed_mlx_vlm`, or `external_only`) so unlaunchable repos are never picked
for a managed backend (ADR 0010 runtime matrix); hardware fitting itself lives
on the Hugging Face model page. The UI's "Model catalog" panel shows curated
cards, task recommendations, and search with the same verdicts.

### Runtime Matrix

Initially supported runtimes (recorded in
[`docs/adr/0010-runtime-matrix.md`](docs/adr/0010-runtime-matrix.md)):

- `mlx-lm` — managed local MLX language-model server on Apple Silicon.
- `mlx-vlm` — managed local MLX vision-language-model server on Apple Silicon.
- `external` — any OpenAI-compatible endpoint.

Managed cross-platform GGUF via `llama.cpp` is **not** initially supported;
GGUF files are not launchable by any managed backend (file-qualified GGUF
references raise `HFValidationError` in `mlx-vlm`), so curated defaults are
MLX repo ids and GGUF repos are `external_only` for the model picker.

The default model's backend is inferred from the model reference unless
`NEWS_MODEL_BACKEND` is set to `mlx-lm`, `mlx-vlm`, or `external` (any other
value fails fast). To run the default model against an external
OpenAI-compatible endpoint — no managed server is started; the pipeline waits
for and probes the endpoint:

```bash
NEWS_MODEL_BACKEND=external NEWS_MODEL_BASE_URL=https://api.example.com/v1 NEWS_MODEL=<server-model-id> uv run news run
```

Authenticated endpoints are supported by setting `NEWS_MODEL_API_KEY`; it is
sent as a `Bearer` token on `/models` and `/chat/completions` requests (unset
sends no credentials). An endpoint that rejects the request with HTTP 401/403
fails fast instead of waiting out the readiness deadline.

`news model-server-command` reports that no managed server command exists for
the external backend. Per-task models can also use external endpoints by
giving that task a distinct base URL (`NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`,
`NEWS_MODEL_STORY_DRAFTING_BASE_URL`, `NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL`,
`NEWS_MODEL_TITLE_GENERATION_BASE_URL`).

Normal report runs start the matching local MLX server, wait until it is ready,
run the pipeline, and stop the managed server when the run exits. To keep a
server warm manually, print the matching command and run it in another terminal:

```bash
NEWS_MODEL=gemma-4-12b-it-4bit uv run news model-server-command
```

If Article Summarization, Story Drafting, Story Scale Screening, or Title
Generation uses a different model, give that
task a matching base URL or run it on an externally managed server. The current
runtime supports one managed local server per shared model/base URL; it does not
automatically coordinate multiple local servers for one run.

### Model Tuning

Model Tuning Presets live in `config/model_tuning_presets.yaml`. They are saved
overlays for one model or one model-task pair and are separate from Run
Presets.

Use these env vars to select a preset:

- `NEWS_MODEL_TUNING_PRESET`
- `NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET`
- `NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET`
- `NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET`
- `NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET`

Precedence is:

1. Backend/model defaults when a tuning field is unset.
2. Verified model-specific code defaults, if any exist.
3. The selected Model Tuning Preset.
4. Explicit `NEWS_` tuning overrides.

Direct tuning overrides still win, such as `NEWS_MODEL_MAX_INPUT_TOKENS`,
`NEWS_ARTICLE_SUMMARY_MAX_TOKENS`, `NEWS_STORY_DRAFTING_MAX_TOKENS`,
`NEWS_STORY_SCALE_SCREENING_MAX_TOKENS`, `NEWS_TITLE_GENERATION_MAX_TOKENS`, and
sampling env vars like `NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE`.

### Pipeline Budget

Pipeline Budget settings are separate from model selection and tuning. They
cover article text caps, article summary caps, recency windows, article/story
limits, and story thresholds.

### Model Server Settings

Model Server Settings control the local MLX/OpenAI-compatible server
configuration:

- `NEWS_MODEL_BASE_URL`
- `NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL`
- `NEWS_MODEL_STORY_DRAFTING_BASE_URL`
- `NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL`
- `NEWS_MODEL_TITLE_GENERATION_BASE_URL`
- `NEWS_MODEL_SERVER_PREFILL_STEP_SIZE`
- `NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE`
- `NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES`
- `NEWS_MODEL_SERVER_MAX_TOKENS`

The base URL also determines the printed server port. If you point a task model
at a different base URL, the task needs its own matching server endpoint.

### Image

`NEWS_IMAGE_ENABLED` controls report image generation:

```bash
NEWS_IMAGE_ENABLED=0 uv run news run --preset NAME
NEWS_MODEL=gemma-e2b-tiny NEWS_IMAGE_ENABLED=0 uv run news run
NEWS_IMAGE_ENABLED=1 uv run news run --preset NAME
```

Image generation defaults off unless enabled by a preset or explicit override.
The image model, dimensions, crop, step count, and fail-open behavior are
hard-coded defaults rather than normal Run Settings.

## Configuration

- `config/sources.yaml`: single source list. Normal runs select active English
  sources using `NEWS_SOURCE_SCOPE`.
- `config/recipients.yaml`: active and paused recipients. `NEWS_RECIPIENT_SCOPE`
  chooses primary-only or all active recipients.
- `config/model_tuning_presets.yaml`: saved Model Tuning Presets keyed by id.

Normal collection accepts active English sources. Removed topic-scoped runtime
variables and source topic fields are rejected when present.

## Outputs

Current run review files are written under `output/daily_outputs/`:

- `latest_run.md`: latest human-readable report.
- `latest_run.log`: latest captured terminal log.
- `latest_run_details.json`: latest backend audit details (includes the
  normalized delivery outcome when a delivery attempt was possible).

Durable run history is written to `output/history/news_history.duckdb`, with CSV
exports in `output/history/` for quick review. Each `runs` row carries the
run status, report metadata, and an independent `delivery_status`/`delivery`
record (`sent`, `skipped: not_configured`, `skipped: user_disabled`, or
`failed`; older rows read as `not recorded`). A run with a non-empty newsletter
body also writes paste-ready Markdown to `output/beehiiv/YYYY-MM-DD.md` for
manual publication.

### Open Knowledge Format projections

The pipeline also writes two portable OKF v0.2 bundle forms:

- `knowledge/` is the checked-in system/domain knowledge bundle. It documents current concepts and links back to `CONTEXT.md`, accepted ADRs, `news_pipeline/`, `config/`, and runtime stores; it contains no generated run output.
- `output/history/okf/<run_id>/` is the generated **OKF Run Bundle** for one run, derived from structured Article Summary Record and Story Record data plus the rendered report body. It contains `report.md`, `articles/`, `stories/`, progressive-disclosure indexes, and `log.md`.

These are portable projections, not a second source of truth. Runtime behavior remains in `news_pipeline/`, vocabulary and accepted decisions remain in `CONTEXT.md` and `docs/adr/`, editable inputs remain in `config/`, the report remains the rendered output, and DuckDB/CSV remain canonical run history. A completed diagnostic run is `stable`; failed, aborted, or unknown runs are `draft`.

History maintenance:

```bash
uv run news history backfill --dry-run
uv run news history backfill --apply
uv run news history cleanup --dry-run
uv run news history cleanup --apply
uv run news history export
```

## License

Licensed under the [Apache License 2.0](LICENSE).

## Fast Test Run

For a quick local test that minimizes runtime and sends to a single
recipient, use the `dev` preset:

```bash
uv run news run --preset dev
```

The `dev` preset:

- Uses `gemma-e2b-tiny` (the smallest model — the only one we keep for
  local testing now that the standard Gemma 4 12B model is the default).
- Sets `NEWS_SOURCE_SCOPE=core` (the narrowest source pool).
- Sets `NEWS_RECIPIENT_SCOPE=primary` (sends only to the primary
  recipient).
- Disables image generation and URL reuse blocking.
- Sets `NEWS_MIN_ARTICLES_PER_STORY=2` and relaxes story drafting guards.

For even faster runs, override the model explicitly and tighten the
recency window:

```bash
NEWS_MODEL=gemma-e2b-tiny NEWS_RECENT_WINDOW_HOURS=6 uv run news run
```

To preview the resolved config before launching a run, use the UI or
the model-server command:

```bash
uv run news model-server-command
```
