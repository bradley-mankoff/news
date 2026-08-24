---
description: Forced-fuse judge for two plans. Writes $ARTIFACTS_DIR/fused-plan.md then emits JSON.
argument-hint: the original user request
---

# Fusion judge — two plans, forced fuse

Two panels planned the request. Write your own fused plan. Do not merely pick one.

## Request

$ARGUMENTS

## Candidates

- `$ARTIFACTS_DIR/plans/a/PLAN.md` (+ NOTES.md)
- `$ARTIFACTS_DIR/plans/b/PLAN.md` (+ NOTES.md)

Write `$ARTIFACTS_DIR/fused-plan.md` (complete plan, not a pointer), then emit:

```json
{"verdict": "...", "mode": "fuse", "chosen": "a"}
```

`chosen` is `"a"` or `"b"` — fallback only.
