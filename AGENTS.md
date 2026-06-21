## 0. Ponytail Always-On
- Use `ponytail` full: YAGNI first, stdlib/native/existing deps before custom code, smallest faithful diff, fewest files.
- Leave one runnable check for non-trivial logic. Trivial one-liners need no test.
- Named workflow skills still own process: `precise-plan` writes full plan files, `implement-precise-plan` follows the plan ledger, `mid-plan-consultation` resolves blockers. Ponytail may shrink implementation, not skip or reinterpret plan steps.
- `ponytail-review` / `ponytail-audit` are complexity-only. They do not replace correctness/security/performance review.

## 1. Command Routing, RTK, and Env
- **Default**: Use the normal command first unless RTK is explicitly useful for that command family. Do not run RTK first just to fall back to the same plain command.
- **RTK env**: When using RTK in this repo, prefix it with `RTK_DB_PATH="/Users/home/personal_code/news/.rtk/history.db"` so tracking stays project-local.
- **Use RTK for high-volume, stable wrappers**: `rtk git status`, `rtk git diff`, `rtk git log`, `rtk rg`, `rtk grep`, `rtk ls`, `rtk tree`, `rtk read`, `rtk pytest`, `rtk cargo test`, and similar first-class RTK wrappers.
- **Use plain `uv run`**: Python one-liners, scripts, pipeline commands, and tests should use `uv run ...` directly. Do not use `rtk uv run ...`; it commonly hits uv-cache/sandbox friction and adds fallback chatter without useful compression. In managed sandboxes, prefer `UV_CACHE_DIR=.uv-cache uv run ...` when uv needs cache writes.
- **Use plain `find` or `fd` for complex file predicates**: `rtk find` is only for simple name/type scans. Use plain `find` or `fd` when a query needs `-not`, `!`, `-o`, parentheses, `-exec`, `-delete`, `-prune`, or path-exclusion logic.
- **Fallbacks**: If RTK is tried and fails because the command is outside RTK's supported shape, switch once to the plain command without repeating RTK attempts.

## 2. graphify
Knowledge graph in local generated `graphify-out/`. Directory is gitignored; never stage or commit it.
- **Command**: User type `/graphify` → call `skill` tool with `skill: "graphify"` before other tasks.
- **Querying**: For codebase, architecture, dependency, or business-logic questions, first try `graphify query "<question>"` when `graphify-out/graph.json` exists. Use `graphify path "<A>" "<B>"` for relations and `graphify explain "<concept>"` for concepts.
- **Navigation**: Use `graphify-out/wiki/index.md` when present. Read `GRAPH_REPORT.md` only for broad architecture reviews or when query/path/explain do not surface enough context.
- **Manual Refresh**: Do not auto-refresh Graphify after edits. If the graph seems missing or stale, remind the user to run `graphify update .` from the repo root. Only run `graphify update .` or `graphify export wiki` when the user explicitly asks.
- **Cost Policy**: Semantic extraction is manual only and may incur API cost. Use `graphify extract . --backend claude` or `graphify label . --backend claude` only when the user explicitly requests semantic recompute or labeling.