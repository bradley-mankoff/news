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
_ADR_0013 = _REPO / "docs/adr/0013-local-daily-automation-uses-launchagent.md"
_DELIVERY_PROFILE = _REPO / "knowledge/domain/delivery-profile.md"
_README = _REPO / "README.md"
_SETTINGS = _REPO / "SETTINGS.md"

_ADR_0007_LINK = "docs/adr/0007-model-configuration-vocabulary.md"
_PROMPT_CATALOG_ADR_LINK = (
    "docs/adr/0018-prompt-catalog-owns-editorial-instructions.md"
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
        slice_b = adr.split("### Delivery slices")[1].split("## Consequences")[0]
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

    def test_adr_0007_and_0017_guard_independent_model_backend_concurrency(self) -> None:
        """Accepted ADRs, README, and SETTINGS must describe the issue #169
        ownership boundary: model selection never infers a backend or
        workload/server concurrency, the fixed default backend is `mlx-vlm`,
        and stage/server concurrency defaults are fixed model-neutral values."""
        adr_0007 = _ADR_0007.read_text(encoding="utf-8")
        adr_0017 = (_REPO / "docs/adr/0017-runtime-matrix.md").read_text(
            encoding="utf-8"
        )
        readme = _README.read_text(encoding="utf-8")
        settings = _SETTINGS.read_text(encoding="utf-8")

        # Independent ownership: model selection never picks a backend.
        self.assertIn("never infers a backend", adr_0007)
        self.assertIn("**not** inferred from the model reference", adr_0017)
        self.assertIn("it never picks a backend", readme)
        self.assertIn("never selects a backend", settings)

        # Fixed default backend documented everywhere.
        for doc, text in (
            ("ADR 0007", adr_0007),
            ("ADR 0017", adr_0017),
            ("README.md", readme),
            ("SETTINGS.md", settings),
        ):
            self.assertIn("mlx-vlm", text, f"{doc} must name the fixed default backend")
        self.assertIn("DEFAULT_MODEL_BACKEND", adr_0017)
        self.assertIn("NEWS_MODEL_BACKEND", settings)
        self.assertIn("`mlx-vlm`", settings)

        # The old selected-model inference policy must not survive.
        self.assertNotIn(
            "the backend is\ninferred from the model reference",
            adr_0017,
            "ADR 0017 must not retain selected-model backend inference",
        )
        self.assertNotIn(
            "inferred from the model\nreference otherwise",
            readme,
            "README must not retain selected-model backend inference",
        )

        # Fixed model-neutral concurrency defaults.
        self.assertIn("Stage concurrency defaults are fixed pipeline values", adr_0007)
        self.assertIn("model-neutral", adr_0007)
        self.assertIn("every model choice", readme)
        self.assertIn("NEWS_MODEL_CONCURRENCY", settings)

        # Inherited task assignments use the resolved default backend.
        self.assertIn("Inherited task assignments", adr_0007)
        normalized_0007 = re.sub(r"\s+", " ", adr_0007)
        self.assertIn(
            "carry the resolved default backend",
            normalized_0007,
        )

    def test_adr_collision_references_and_targets_are_repaired(self) -> None:
        targets = {
            "docs/adr/0016-project-license.md": "0016",
            "docs/adr/0017-runtime-matrix.md": "0017",
            "docs/adr/0018-prompt-catalog-owns-editorial-instructions.md": "0018",
        }
        for relative, number in targets.items():
            path = _REPO / relative
            self.assertTrue(path.is_file(), relative)
            self.assertRegex(
                path.read_text(encoding="utf-8").splitlines()[0],
                rf"^# ADR {number}:",
            )

        for doc in (_README, _SETTINGS):
            self.assertIn(_PROMPT_CATALOG_ADR_LINK, doc.read_text(encoding="utf-8"))
        self.assertIn(
            "docs/adr/0017-runtime-matrix.md",
            _README.read_text(encoding="utf-8"),
        )

        for relative in (
            "README.md",
            "SETTINGS.md",
            "docs/adr/0007-model-configuration-vocabulary.md",
            "docs/adr/0011-prompt-contracts-pipeline-owned-and-validated.md",
            "docs/adr/0014-model-catalog-yaml-overrides.md",
            "docs/adr/0015-advanced-prompt-template-overrides.md",
            "news_pipeline/model_catalog.py",
        ):
            text = (_REPO / relative).read_text(encoding="utf-8")
            self.assertNotIn("docs/adr/0010-", text, relative)
            self.assertNotIn("ADR 0010", text, relative)

    def test_adrs_are_uniquely_numbered_and_well_formed(self) -> None:
        adr_paths = sorted(_REPO.glob("docs/adr/[0-9][0-9][0-9][0-9]-*.md"))
        self.assertTrue(adr_paths, "docs/adr must contain at least one ADR")

        numbers = [
            int(match.group(1))
            for path in adr_paths
            if (match := re.match(r"(\d{4})-", path.name))
        ]
        self.assertEqual(numbers, sorted(numbers), "ADR numbers must be sorted")
        # ADR records must have unique numbers in the current tree. This guard
        # catches duplicates anywhere in the sequence; it cannot detect
        # historical renumbering or require the next number to extend the
        # existing range. ADR 0010 remains intentionally unused after the
        # collision repair.
        self.assertEqual(
            len(numbers), len(set(numbers)), "ADR numbers must be unique"
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
        # Issue #75 ships a managed llama.cpp backend: text-generation GGUF is
        # launchable (operator-installed llama-server), multimodal GGUF is not.
        # The README must state the restriction instead of a blanket
        # "not launchable" claim.
        self.assertIn(
            "Text-generation GGUF is supported",
            readme,
            "README Runtime Matrix must state text-GGUF launchability",
        )
        self.assertIn(
            "multimodal GGUF",
            readme,
            "README Runtime Matrix must state the multimodal GGUF restriction",
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

    def test_adr_0013_daily_automation_decision_and_links(self) -> None:
        adr = _ADR_0013.read_text(encoding="utf-8")
        self.assertTrue(
            re.search(r"^Status: Accepted$", adr, re.M),
            "ADR 0013 must have the exact status line 'Status: Accepted'",
        )
        for term in (
            "StartCalendarInterval",
            "launchctl",
            "daily_schedule.json",
            "com.bradley-mankoff.news-daily-run",
            "RunAtLoad",
            "KeepAlive",
            "run_pipeline",
            "env.json",
        ):
            self.assertIn(term, adr, f"ADR 0013 missing vocabulary term {term!r}")
        # The secret boundary is load-bearing in the ADR text.
        self.assertIn("NEWS_SMTP_PASSWORD", adr)
        self.assertIn("NEWS_UNSUBSCRIBE_SECRET", adr)
        self.assertIn("NEWS_MODEL_API_KEY", adr)
        # No hosted/cloud scheduler may be shipped; cron appears only as an
        # explicit exclusion in the decision.
        self.assertIn("no cron", adr)
        self.assertIn("no hosted", adr)

    def test_adr_0012_slice_c_is_implemented_and_links_adr_0013(self) -> None:
        adr = _ADR_0012.read_text(encoding="utf-8")
        slice_c = adr.split("### Delivery slices")[1].split("## Consequences")[0]
        self.assertIn("implemented", slice_c)
        self.assertIn("0013-local-daily-automation-uses-launchagent.md", slice_c)
        self.assertIn("daily_schedule.json", slice_c)
        self.assertIn("com.bradley-mankoff.news-daily-run", slice_c)
        self.assertNotIn("future work", slice_c)

    def test_runtime_docs_document_daily_automation_contract(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        settings = _SETTINGS.read_text(encoding="utf-8")
        for doc, text in (("README.md", readme), ("SETTINGS.md", settings)):
            self.assertIn("schedule status", text, f"{doc} missing schedule status command")
            self.assertIn("schedule enable", text, f"{doc} missing schedule enable command")
            self.assertIn("schedule disable", text, f"{doc} missing schedule disable command")
            self.assertIn("schedule run", text, f"{doc} missing schedule run command")
            self.assertIn("owner", text, f"{doc} missing owner-first delivery vocabulary")
            self.assertIn("launchd", text, f"{doc} missing launchd vocabulary")
            self.assertIn("automation/", text, f"{doc} missing board-automation disambiguation")
        # Credential boundary documented in both runtime docs.
        for doc, text in (("README.md", readme), ("SETTINGS.md", settings)):
            self.assertIn("env.json", text, f"{doc} missing env.json credential fallback note")
        self.assertIn("daily_schedule.json", readme)
        self.assertIn("daily_schedule.json", settings)
        self.assertIn("com.bradley-mankoff.news-daily-run.plist", settings)
        # Owner-only scheduled default is explicit, not implied.
        self.assertIn("owner-only", readme)
        self.assertTrue(
            "owner`, `disabled`" in settings or "`owner` (default" in settings
        )
        # Product Daily Automation stays distinct from board automation.
        self.assertIn("board automation", readme)


if __name__ == "__main__":
    unittest.main()
