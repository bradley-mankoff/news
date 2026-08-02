# ADR 0010: Project license

Status: Accepted

Date: 2026-08-02

## Context

The repository had no `LICENSE`, `LICENSE.md`, `LICENSE.txt`, or `COPYING`
file (recorded at HANDOFF.md:77), which blocks the open-source release
preparation in progress (ADR 0009). Choosing a license is a human-only
decision per `AGENTS.md:46-56`; the owner was asked via the NEEDS INPUT
protocol and chose **Apache-2.0** on issue #21 (comment 5159412290,
2026-08-02). All 13 direct dependencies are permissively licensed (MIT / BSD /
Apache-2.0 / HPND — audited via the PyPI JSON API), so no dependency forced a
copyleft license; the choice is purely a positioning decision.

## Decision

The project is licensed under the Apache License 2.0 (SPDX `Apache-2.0`). The
canonical text is in the root `LICENSE` file; `pyproject.toml` declares
`license = "Apache-2.0"` and `license-files = ["LICENSE"]` (PEP 639). The
copyright holder is Bradley Mankoff (recorded here — the license text itself
carries no copyright line). Per-file license headers are not added: Apache's
Appendix boilerplate is a recommendation, not a requirement. Rejected:
AGPL-3.0 (network copyleft — would force anyone running a modified version as
a hosted service to open their source; the owner chose a permissive posture).

## Consequences

- Users may embed, modify, and distribute the project under Apache-2.0 terms
  without disclosing source; contributors receive an explicit patent grant.
- Downstream modifiers must state changes and retain notices (§4); no
  contributor agreement is needed (§5 — contributions are submitted under the
  license); trademark use is not granted (§6).
- PyPI publication (ADR 0009) now has its licensing prerequisite met.
