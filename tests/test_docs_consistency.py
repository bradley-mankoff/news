"""Cross-document consistency guards for checked-in documentation.

These tests enforce the vocabulary and format conventions that AGENTS.md and
docs/adr/README.md document but that no runtime code exercises:

- ADR 0012's canonical delivery outcome vocabulary must match the knowledge
  bundle concept and must not be restated in a conflicting compressed form
  (guards the Slice B implementation contract).
- ADR 0007 must stay `Accepted` with the model-configuration boundary
  vocabulary, README.md and SETTINGS.md must link the accepted decision, and
  SETTINGS.md must keep Prompt Profile ownership with the Prompt Catalog ADR
  (guards the model-configuration vocabulary contract).
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
_ADR_0007 = _REPO / "docs/adr/0007-model-configuration-vocabulary.md"
_ADR_0012 = _REPO / "docs/adr/0012-desktop-first-application-optional-delivery.md"
_DELIVERY_PROFILE = _REPO / "knowledge/domain/delivery-profile.md"
_README = _REPO / "README.md"
_SETTINGS = _REPO / "SETTINGS.md"

_ADR_0007_LINK = "docs/adr/0007-model-configuration-vocabulary.md"
_PROMPT_CATALOG_ADR_LINK = (
    "docs/adr/0010-prompt-catalog-owns-editorial-instructions.md"
)
_ADR_0007_BOUNDARY_TERMS = (
    "Task Model Assignment",
    "Model Tuning",
    "Pipeline Budget",
    "Model Server Settings",
    "Run Preset",
    "Runtime Config Snapshot",
)
_ADR_0007_TASK_TERMS = (
    "Article Summarization",
    "Story Drafting",
    "Story Scale Screening",
    "Title Generation",
    "Image Art Direction",
    "Story Discovery",
    "NEWS_MODEL_STORY_DISCOVERY",
)

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
        slice_b = adr.split("### Follow-up slices")[1].split("## Consequences")[0]
        self.assertIn("skipped: not_configured", slice_b)
        self.assertIn("skipped: user_disabled", slice_b)
        self.assertNotIn("`user_disabled`", slice_b)
        self.assertNotIn("`skipped`", slice_b)

    def test_adr_0007_is_accepted_and_linked(self) -> None:
        adr = _ADR_0007.read_text(encoding="utf-8")
        readme = _README.read_text(encoding="utf-8")
        settings = _SETTINGS.read_text(encoding="utf-8")

        # The decision must carry the exact accepted status line, never a
        # regression back to Proposed.
        self.assertTrue(
            re.search(r"^Status: Accepted$", adr, re.M),
            "ADR 0007 must have the exact status line 'Status: Accepted'",
        )
        self.assertNotIn("Status: Proposed", adr)

        # The accepted record must define the ownership boundaries and the
        # current task assignments/inheritance rules.
        for term in _ADR_0007_BOUNDARY_TERMS + _ADR_0007_TASK_TERMS:
            self.assertIn(term, adr, f"ADR 0007 missing vocabulary term {term!r}")

        # Both runtime documents must link the accepted decision.
        for doc, text in (("README.md", readme), ("SETTINGS.md", settings)):
            self.assertIn(
                _ADR_0007_LINK,
                text,
                f"{doc} must link to the accepted ADR 0007",
            )

        # Prompt Profile ownership stays with the Prompt Catalog ADR, not with
        # Model Tuning.
        self.assertIn(
            _PROMPT_CATALOG_ADR_LINK,
            settings,
            "SETTINGS.md must link Prompt Profile ownership to the Prompt "
            "Catalog ADR",
        )

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

    def test_readme_runtime_matrix_is_unique_and_consistent(self) -> None:
        readme = (_REPO / "README.md").read_text(encoding="utf-8")
        # Regression for PR #157 conflict resolution: the merge kept BOTH
        # parents' Runtime Matrix sections. The stale (main-side) section
        # claimed "GGUF files run through mlx-vlm", contradicting the
        # surviving section and the shipped fail-fast config.
        self.assertEqual(
            readme.count("### Runtime Matrix"),
            1,
            "README.md must contain exactly one Runtime Matrix section",
        )
        self.assertNotIn("GGUF files run through", readme)
        self.assertIn(
            "GGUF files are not launchable by any managed backend",
            readme,
            "README Runtime Matrix must state the GGUF restriction",
        )

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
