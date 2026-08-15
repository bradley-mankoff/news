# Archon Setup & Operations (machine-local)

The Archon workflow engine on this machine is the **archon-pi** product: stock
Archon workflow engine + stock Pi provider, no custom extensions.

## Where things live

| Thing | Location |
|---|---|
| archon-pi product repo | `~/personal_code/archon-pi` (`bin/`, `config/`, `scripts/install.sh`) |
| Archon home (`ARCHON_HOME`) | `~/.local/share/archon-pi/archon-home` |
| `archon` command | `~/.local/bin/archon` — a wrapper script that exports `ARCHON_HOME` + pi defaults and execs `archon-pi/bin/archon` (stock v0.7.0 binary) |
| `archon-pi` command | shim with the same env + `doctor`/`ui` helpers |
| Config | `archon-home/config.yaml`: `defaultAssistant: pi`, `assistants.pi.model: opencode-go/deepseek-v4-flash`, tiers small/medium/large → pi + opencode-go + effort max; rigorous workflow nodes override with `provider: pi`, `model: openai-codex/gpt-5.6-luna`, and `effort: max` |
| Curated workflows | `archon-home/workflows/` (17 pi-usable workflows, overrides bundled by name) |
| Archived workflows | `archon-home/workflows-archived/` (7 claude-only, not discovered) |
| Database | `archon-home/archon.db` (SQLite) |
| Web UI build | `archon-home/web-dist/` (0.7.0) |
| Env | `archon-home/.env`: `DEFAULT_AI_ASSISTANT=pi`, telemetry off, `MAX_CONCURRENT_CONVERSATIONS=10` |

## Services

- **archon-web** — hub-managed detached `archon serve` on `http://localhost:3090`
  (`hub ps` / `hub logs archon-web` / `hub restart archon-web`). Known upstream
  issue: the console runs view requires manual refresh
  (coleam00/Archon#2381).
- **board poller** — launchd agent `com.bradley-mankoff.news-board-poller`
  (`launchctl list | grep news-board-poller`; log `automation/board_poller.log`;
  state `automation/state.json`).
- The board poller enforces the committed
  `automation/config.json:max_concurrent_workflows` limit (`4` here), counts
  active/paused Archon runs **for this repo's codebase** before dispatching
  (foreign repos' runs in the shared archon home do not consume news slots),
  reserves slots within a poll, and holds dispatches if the status lookup fails.

### Automatic capacity fill

After the initial snapshot, each poll uses remaining workflow capacity to
promote eligible repository Issues from `Backlog` to `Todo`, in issue-number
order. An issue must be open, must not carry the configured decision-only or
`needs-input` label, and must have all `Depends on` references satisfied.
Closed, decision-only, `needs-input`, dependency-blocked, and failed-to-move
items remain in Backlog. The initial poll is read-only; later promotions run
after the current dispatch pass, so promoted Todo items dispatch on the next
poll. `CONCURRENCY GAP` log lines explain why available capacity was not filled.

- **news UI** — `uv run news ui` on `http://127.0.0.1:8766` (see the `news-dev` skill).

## Execution model

- Unassigned workflow nodes run on `pi` / `opencode-go/deepseek-v4-flash` at max effort.
- Planning, review, and vision-capable nodes explicitly run on Pi's OpenAI
  Codex OAuth backend: `provider: pi`, `model: openai-codex/gpt-5.6-luna`, and
  `effort: max`.
- A workflow's `provider:` pin is overridden when its `model:` resolves to a
  tier — the tier's provider wins (bundled workflows pinned to claude still run
  on pi via the tiers). Explicit Luna nodes set `provider: pi`.
- Tier-level `effort` does not route to pi; the curated workflows carry
  workflow-level `effort: max`, and Luna nodes also pin `effort: max`.

## Build quirks (learned the hard way)

- **`--detach` is broken** in this build (the child spawns its own binary path
  as a command). The poller spawns its own detached children — never use
  `--detach` on this machine.
- `archon continue <branch> --workflow <wf> "<msg>"` needs the **full
  namespaced branch** (`archon/task-issue-N`, from `archon isolation list`),
  not the shorthand passed to `workflow run --branch`. It prepends a
  "Prior Context" preamble to the message — the poller matches runs
  substring-style.
- GitHub auto-close keywords (`Fixes/Closes/Resolves #N`) in PR bodies or
  commit messages close the issue when merged into the default branch
  (`develop`). The poller reopens after develop merges (waits 6s for GitHub's
  async close) and closes the issue explicitly when the ship PR reaches `main`.
- The title-generator logs `Claude Code not found` noise on every run —
  harmless; titles fall back to pi.
- Updating Archon: replace `archon-pi/bin/archon` (and `.archon/bin/archon`)
  with the stock release binary, keep the env wrapper in place, then
  `archon validate workflows` and `hub restart archon-web`. Backups of the
  previous binary are kept as `.bak-v0.6.0`.

## Verification

```bash
archon version                     # v0.7.0
archon validate workflows          # 17 valid
archon ai tier list --json         # all tiers pi/opencode-go/deepseek-v4-flash, effort max
archon workflow runs               # recent runs (from the repo root)
launchctl list | grep news-board-poller
```
