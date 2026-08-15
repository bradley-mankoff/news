# ADR 0015: Advanced full prompt template overrides

Status: Accepted

Date: 2026-08-14

## Context

Advanced Settings labeled its prompt surface "Full prompt templates" but only
rendered the selected Prompt Profile's editorial instruction sentences
(ADR 0018) as readouts; the actual system/user messages stayed assembled
inside the stage modules and `pipeline.py`. An advanced user could not change
prompt structure, ordering, or framing without editing Python, and a custom
prompt that dropped a required input or parser marker would only surface as a
degraded model response or fallback — never as a rejected edit. ADR 0011
already anticipated full-template validation through `validate_prompt_contract()`;
this ADR records the accepted implementation of that surface.

The five editorial LLM stages with full-template support are Article
Summarization, Story Scale Screening, Story Drafting, Title Generation, and
Image Art Direction. Translation is a sixth LLM assignment, but its structured
language-code prompt is intentionally outside this editorial template surface.
Story Discovery has no LLM stage (embedding/TF-IDF clustering) and is out of
scope. Existing
sentence-level Prompt Profiles, `config/prompt_overrides.yaml`, and
`NEWS_PROMPT_OVERRIDE_<TASK>` values are backward-compatible and keep their
current semantics.

## Decision

Introduce a stdlib-only full-template catalog
(`news_pipeline/prompt_templates.py`) with:

- A separate `NEWS_PROMPT_TEMPLATE_<TASK>` env namespace derived from the
  canonical `PROMPT_TASKS`; existing `NEWS_PROMPT_OVERRIDE_<TASK>` values
  remain editorial sentence overrides and are never reinterpreted as full
  templates.
- A frozen `PromptTemplate` record per task with `system` and `user` message
  templates using Python `string.Template` placeholders (`$name`/`${name}`,
  `$$` for a literal dollar sign). Built-in templates are extracted verbatim
  from the stage renderers so default/no-override rendered messages stay
  byte-identical (ADR 0011 golden snapshots remain authoritative).
- Per-task required placeholders: dynamic input placeholders
  (`$now_label`, `$recent_window_hours`, `$article_payload`,
  `$story_blocks`, `$story_title`, `$source_summary_lines`, `$report_title`,
  `$synthesis_body`) and code-owned contract placeholders (`$output_contract`,
  `$citation_contract`, `$scale_contract`, `$title_contract`,
  `$overlay_protocol`, `$image_contract`) whose values are the pipeline-owned
  constants from `prompt_contracts.py`. `$editorial_instructions` is optional
  and is the only path through which profile/editorial sentences enter a
  custom template.
- A single pure parser/validator (`parse_prompt_template_override` +
  `validate_prompt_template`) that rejects malformed JSON, unknown JSON keys,
  non-string/empty roles, malformed `$` syntax, unknown placeholders, missing
  required placeholders, and rendered pairs that fail
  `validate_prompt_contract()` — used identically by runtime config
  resolution, Run Preset CRUD, and the UI validation endpoint, so the three
  surfaces can never diverge.

Templates are carried by Run Presets, command previews, CLI env, and
scheduled runs through the existing `NEWS_` env overlay transport (no new
settings database). Runtime config resolves all templates once per process
(`PROMPT_TEMPLATES`) and stages render their selected template with the
existing dynamic value map; retry corrections, deterministic fallbacks,
parsers, citation rendering, and sanitizers remain pipeline-owned. Run
diagnostics record `prompt_template_overrides` plus a compact per-task source
map (`default`/`custom`); exact rendered prompt snapshots remain the
authoritative reproducibility record.

Advanced Settings renders one System and one User textarea per task from
`schema.prompt_templates` (never a second hard-coded task list), with
placeholder reference, per-task Validate/Restore, restore-all, and a clear
precedence notice. Changed task pairs serialize as one JSON env value;
untouched pairs round-trip the raw current value so malformed overrides are
never silently replaced; pairs equal to the built-in default are omitted.

## Consequences

- Advanced users can change complete stage prompt structure without editing
  Python, while inputs and output protocols stay protected: unknown/malformed
  placeholders and contract-marker loss fail closed before a preset save or
  run launch, and rendered custom messages pass `validate_prompt_contract()`
  before any model invocation.
- The default path remains byte-identical; sentence-level profiles,
  `config/prompt_overrides.yaml`, and `NEWS_PROMPT_OVERRIDE_<TASK>` semantics
  are unchanged and retain their precedence when no full template is set.
- A custom template without `$editorial_instructions` intentionally replaces
  the profile text for that task; the UI and docs state this precedence
  explicitly.
- Image Art Direction and Title Generation keep independent templates, model
  task names, snapshots, parsers, and fallbacks; a failure in one call never
  suppresses the other.
- Exclusions: no new model providers or sampling controls; no `story_discovery`
  template editor; no editing of retry messages, fallbacks, parsers,
  sanitizers, or contract constants; no template repository/versioning/
  permissions; no automatic migration of sentence overrides into full
  templates; no promise of model quality beyond structural/protocol
  validation — parsers and fallbacks still govern model responses.
- `string.Template` was chosen over Jinja/Python/shell to avoid code
  execution; user text is never `eval`'d, never run through a second
  `.format()` pass, and never re-parsed as a template after substitution.
