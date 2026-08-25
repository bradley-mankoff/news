# News project memory policy

## Identity
project_id: news

## Instructions
Store durable, reusable knowledge for future work on this repository. Treat `AGENTS.md`, `CONTEXT.md`, `README.md`, `docs/adr/`, `config/`, `knowledge/`, and the implementation as the canonical sources; include the source path and ADR number when recording a decision. Capture accepted architecture decisions, domain vocabulary, project-wide conventions, operational runbook facts, and recurring bugs with their root causes. Prefer one concise memory per decision or invariant. Mark superseded decisions explicitly instead of silently replacing them. Never store credentials, tokens, private data, raw article or report content, generated logs, transient branch or issue state, one-off task chatter, speculative ideas, or unverified guesses.

## Categories
- architecture_decisions
- coding_conventions
- domain_glossary
- tooling_setup
- bug_fixes
- task_learnings
- security_constraints

## Retention
architecture_decisions: forever
coding_conventions: forever
domain_glossary: forever
security_constraints: forever
tooling_setup: 365d
bug_fixes: 365d
task_learnings: 180d
session_state: 30d

## Ignore
- output/
- automation/*.log
- automation/state.json
- .pytest_cache/
- __pycache__/
- *.pyc
- *.sqlite
- *.db
