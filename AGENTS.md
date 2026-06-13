## 1. RTK Commands & Env
- **Env**: Always prefix cmd `RTK_DB_PATH="/Users/home/personal_code/news/.rtk/history.db" rtk ` (e.g. `rtk git status`, `rtk git diff`, `rtk rg`, `rtk find`, `rtk ls`, `rtk cat`, `rtk npm test`, `rtk pytest`, `rtk uv run pytest`).
- **Rule**: No plain `git status`, `git diff`, `rg`, `find`, `ls`, `cat`, test cmds first.
- **Fallback**: RTK cmd fail? Retry once plain. State fallback.

## 2. graphify
Knowledge graph in `graphify-out/`.
- **Command**: User type `/graphify` → call `skill` tool with `skill: "graphify"` before other tasks.
- **Querying**: Questions → `graphify query "<question>"` (needs `graphify-out/graph.json`). `graphify path "<A>" "<B>"` for relations, `graphify explain "<concept>"` for concepts.
- **Skip Conditions**: Dirty files in `graphify-out/` normal. Skip only if debug bad graph output or user forbid.
- **Navigation**: `graphify-out/wiki/index.md` exist? Use it, not raw source. Read `GRAPH_REPORT.md` only for broad review.
- **Update**: Run `graphify update .` (AST-only, free) after code change.

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
