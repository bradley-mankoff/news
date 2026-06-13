## 1. RTK Commands & Env
- **Env**: Always prefix cmd `RTK_DB_PATH="/Users/home/personal_code/news/.rtk/history.db" rtk ` (e.g. `rtk git status`, `rtk git diff`, `rtk rg`, `rtk find`, `rtk ls`, `rtk cat`, `rtk npm test`, `rtk pytest`, `rtk uv run pytest`).
- **Rule**: No plain `git status`, `git diff`, `rg`, `find`, `ls`, `cat`, test cmds first.
- **Fallback**: RTK cmd fail? Retry once plain. State fallback.

## 2. graphify
Knowledge graph in local generated `graphify-out/`. Directory is gitignored; never stage or commit it.
- **Command**: User type `/graphify` → call `skill` tool with `skill: "graphify"` before other tasks.
- **Querying**: Questions → `graphify query "<question>"` (requires local `graphify-out/graph.json`). If missing/stale, run `graphify update .` first. Use `graphify path "<A>" "<B>"` for relations, `graphify explain "<concept>"` for concepts.
- **Navigation**: Use `graphify-out/wiki/index.md` when present; otherwise regenerate with `graphify update .` then `graphify export wiki`. Read `GRAPH_REPORT.md` only for big reviews.
- **Update**: After any code change, run `graphify update .` (AST-only, free), then `graphify export wiki`. Leave resulting `graphify-out/` files untracked.

## 3. Karpathy Execution Guidelines
### A. Think Before Coding (No Assumptions)
- State assumptions before code. Uncertain? Stop, ask.
- Show many interpretations—no silent pick. Push back bad user approach with tradeoffs.

### B. Simplicity First (No Speculation)
- Write minimal code for request. No extra features, single-use abstractions, needless config.
- Code too long? Rewrite. Simplify if senior eng call it overcomplicated.

### C. Surgical Changes (No Renovation)
- Touch only target lines/files. No refactor unbroken code, no clean adjacent format/comments. Match style.
- Remove imports/vars/funcs *your* change made unused. No delete old dead code unless asked.

### D. Goal-Driven Execution (Verify)
- Make tasks verifiable goals (e.g. "Write tests for bad inputs, then pass").
- Multi-step? State brief plan with verify steps: `1. [Step] -> verify: [check]`.
