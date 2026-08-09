"""Cross-document consistency guards for checked-in documentation.

These tests enforce the vocabulary and format conventions that AGENTS.md and
docs/adr/README.md document but that no runtime code exercises:

- ADR 0012's canonical delivery outcome vocabulary must match the knowledge
  bundle concept and must not be restated in a conflicting compressed form
  (guards the Slice B implementation contract).
- New ADRs must be uniquely numbered (never re-using or renumbering an
  existing decision) and carry the required Status/Date/Context/Decision/
  Consequences sections.
- CONTEXT.md vocabulary sections must have matching knowledge concepts.

Style follows tests/test_okf.py: checked-in markdown invariants asserted with
pathlib + regex, no new dependencies.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ADR_0012 = _REPO / "docs/adr/0012-desktop-first-application-optional-delivery.md"
_DELIVERY_PROFILE = _REPO / "knowledge/domain/delivery-profile.md"

_CANONICAL_OUTCOME_TOKENS = {
    "skipped: not_configured",
    "skipped: user_disabled",
    "`sent`",
    "`failed`",
}
_OUTCOME_TOKEN_RE = re.compile(
    r"skipped: (?:not_configured|user_disabled)|`(?:sent|failed)`"
)
_ADR_SECTIONS = ("Status:", "Date:", "## Context", "## Decision", "## Consequences")


class DocsConsistencyTests(unittest.TestCase):
    def test_adr_delivery_outcome_vocabulary_matches_knowledge_bundle(self) -> None:
        adr = _ADR_0012.read_text(encoding="utf-8")
        concept = _DELIVERY_PROFILE.read_text(encoding="utf-8")

        adr_tokens = set(_OUTCOME_TOKEN_RE.findall(adr))
        concept_tokens = set(_OUTCOME_TOKEN_RE.findall(concept))
        self.assertTrue(
            _CANONICAL_OUTCOME_TOKENS <= adr_tokens,
            "ADR 0012 must define the full delivery outcome vocabulary",
        )
        self.assertTrue(
            _CANONICAL_OUTCOME_TOKENS <= concept_tokens,
            "delivery-profile.md must match ADR 0012 vocabulary",
        )

        # The knowledge concept must not introduce sibling bare states outside
        # the canonical vocabulary.
        for token in ("`skipped`", "`user_disabled`"):
            self.assertNotIn(token, concept, f"{token} is not a canonical state")

        # Slice B's summary must not use the compressed form that collapses
        # the two skip reasons or elevates `user_disabled` to a sibling state.
        slice_b = adr.split("### Delivery slices")[1].split("## Consequences")[0]
        self.assertIn("skipped: not_configured", slice_b)
        self.assertIn("skipped: user_disabled", slice_b)
        self.assertNotIn("`user_disabled`", slice_b)
        self.assertNotIn("`skipped`", slice_b)

    def test_adrs_are_uniquely_numbered_and_well_formed(self) -> None:
        adr_paths = sorted(_REPO.glob("docs/adr/[0-9][0-9][0-9][0-9]-*.md"))
        self.assertTrue(adr_paths, "docs/adr must contain at least one ADR")

        numbers = [
            int(match.group(1))
            for path in adr_paths
            if (match := re.match(r"(\d{4})-", path.name))
        ]
        self.assertEqual(numbers, sorted(numbers), "ADR numbers must be sorted")
        # New ADRs must extend, never re-use or renumber, existing decisions.
        self.assertEqual(
            numbers[-1], max(numbers), "duplicate ADR number detected"
        )

        for path in adr_paths:
            text = path.read_text(encoding="utf-8")
            for required in _ADR_SECTIONS:
                self.assertIn(required, text, f"{path.name} missing {required}")

    def test_context_vocabulary_sections_have_knowledge_concepts(self) -> None:
        context = (_REPO / "CONTEXT.md").read_text(encoding="utf-8")
        sections = set(re.findall(r"^## (.+)$", context, re.M))
        self.assertTrue(
            {
                "Daily News Application",
                "Daily News Report",
                "Delivery Profile",
                "Automation",
            }
            <= sections,
            "CONTEXT.md must keep the ADR 0012 vocabulary sections",
        )
        self.assertTrue(_DELIVERY_PROFILE.is_file())
        # The Automation section must disambiguate from the repo's board
        # automation so the term collision stays documented.
        automation_section = context.split("## Automation")[1].split("## ")[0]
        self.assertIn("board automation", automation_section)
        self.assertIn("`automation/`", automation_section)


if __name__ == "__main__":
    unittest.main()
