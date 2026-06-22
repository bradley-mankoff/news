# ADR 0004: Runtime Config Resolution owns env overlays

Status: Accepted

Date: 2026-06-14

## Context

Runtime config was already valuable, but its Interface leaked environment
variable names, saved preset overlay rules, UI preview mutation, command env
deltas, and some story runtime defaults into callers. The UI had to patch
`os.environ` to preview a config, while Run Session still read several runtime
knobs from process env after its Runtime Config snapshot was built.

## Decision

Treat Runtime Config Resolution as the Module that turns base environment
values, saved run presets, and explicit overrides into one Runtime Config
snapshot. It owns preset precedence, Run Settings metadata, command environment
deltas, and removed-setting validation.

CLI and UI code may act as Adapters into Runtime Config Resolution. New Run
Settings should be resolved into Runtime Config before a Run Session starts
rather than read directly from process env inside pipeline compatibility
globals.

## Consequences

- Runtime defaults, preset rules, and validation gain Locality in one Module.
- UI previews can resolve config without mutating process environment.
- Run Session receives a fuller immutable config snapshot.
- Compatibility helpers that mutate `os.environ` may remain for edge callers,
  but new behavior should prefer explicit resolution inputs.
