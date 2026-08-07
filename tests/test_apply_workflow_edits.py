from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.apply_workflow_edits import (
    CONTRACT,
    TESTING_COMMAND_RULE,
    TESTING_CONTRACT,
    ensure_contract,
    ensure_node,
    ensure_rigorous_models,
    ensure_spec_review,
    ensure_sync_node,
)


class WorkflowMigrationTest(unittest.TestCase):
    def test_sync_node_rewires_target_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(
                "  - id: check-blocked\n"
                "    context: fresh\n"
                "  - id: create-pr\n"
                "    depends_on: [check-blocked]\n"
                "    context: fresh\n",
                encoding="utf-8",
            )
            anchor = "  - id: check-blocked\n    context: fresh\n"
            node = "  - id: sync-with-develop\n    context: fresh"
            old_dep = "    depends_on: [check-blocked]\n    context: fresh"
            new_dep = "    depends_on: [sync-with-develop]\n    context: fresh"

            note = ensure_sync_node(
                path, anchor, node, "create-pr", old_dep, new_dep)
            self.assertEqual(note, "added sync-with-develop to wf.yaml")
            before = path.read_text(encoding="utf-8")
            self.assertIn("depends_on: [sync-with-develop]", before)
            self.assertEqual(
                ensure_sync_node(path, anchor, node, "create-pr", old_dep, new_dep),
                None,
            )
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_spec_review_insertion_rewires_synthesize(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.yaml"
            path.write_text(
                "  - id: sync\n"
                "    command: archon-sync-pr-with-main\n"
                "    depends_on: [review-scope]\n"
                "    context: fresh\n\n"
                "  - id: synthesize\n"
                "    depends_on: [code-review, error-handling, test-coverage, "
                "comment-quality, docs-impact]\n",
                encoding="utf-8",
            )
            note = ensure_spec_review(path)
            self.assertEqual(note, "added spec-review node to archon-review-block.yaml")
            text = path.read_text(encoding="utf-8")
            self.assertIn("- id: spec-review", text)
            self.assertIn("depends_on: [sync]", text)
            self.assertIn("docs-impact, spec-review]", text)
            self.assertIsNone(ensure_spec_review(path))

    def test_rigorous_model_pinning_replaces_existing_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workflow.yaml"
            path.write_text(
                "  - id: review\n"
                "    provider: claude\n"
                "    model: old-model\n"
                "    effort: low\n"
                "    modelReasoningEffort: old\n"
                "    prompt: review\n",
                encoding="utf-8",
            )
            note = ensure_rigorous_models(path, ("review",))
            self.assertEqual(note, "pinned rigorous Pi Codex nodes in workflow.yaml")
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("provider:"), 1)
            self.assertEqual(text.count("model:"), 1)
            self.assertIn("provider: pi", text)
            self.assertIn("model: openai-codex/gpt-5.6-luna", text)
            self.assertIn("effort: max", text)
            self.assertNotIn("modelReasoningEffort", text)
            self.assertIsNone(ensure_rigorous_models(path, ("review",)))


class EnsureNodeTest(unittest.TestCase):
    def test_inserts_node_after_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("- id: report\n    context: fresh\n", encoding="utf-8")
            note = ensure_node(path, "completion-comment",
                               "  - id: completion-comment\n    context: fresh",
                               "- id: report")
            self.assertEqual(note, "added completion-comment node to wf.yaml")
            text = path.read_text()
            self.assertIn("completion-comment", text)
            # inserted after the anchor line, before its context
            self.assertLess(text.index("- id: report"),
                            text.index("completion-comment"))

    def test_already_present_is_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("- id: report\n  - id: completion-comment\n",
                            encoding="utf-8")
            self.assertIsNone(ensure_node(path, "completion-comment",
                                          "  - id: completion-comment\n",
                                          "- id: report"))
            self.assertIsNone(ensure_node(path, "completion-comment",
                                          "  - id: completion-comment\n",
                                          "- id: report"))
            self.assertEqual(path.read_text().count("completion-comment"), 1)

    def test_missing_anchor_reports_manual_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("- id: other\n", encoding="utf-8")
            note = ensure_node(path, "completion-comment", "TEXT\n", "- id: report")
            self.assertIn("anchor not found", note)
            self.assertIn("manually", note)
            self.assertNotIn("completion-comment", path.read_text())


class EnsureContractTest(unittest.TestCase):
    def _node(self) -> str:
        return ("  - id: completion-comment\n"
                "    prompt: |\n"
                "      Post a completion record.\n\n"
                "      The comment must be factual; never claim a criterion\n"
                "      is met without citable evidence.\n"
                "    depends_on: [report]\n"
                "    context: fresh\n")

    def test_inserts_contract_before_factual_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self._node(), encoding="utf-8")
            note = ensure_contract(path, "completion-comment")
            self.assertEqual(
                note,
                "added How-to-test and Deferred-work contracts to "
                "completion-comment in wf.yaml")
            text = path.read_text()
            self.assertIn("## How to test", text)
            self.assertIn("reads this section and creates a tracking issue", text)
            self.assertLess(text.index(TESTING_CONTRACT[:40]),
                            text.index("The comment must be factual"))

    def test_already_present_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self._node().replace(
                "      Post a completion record.\n",
                "      Post a completion record.\n"
                + TESTING_CONTRACT + CONTRACT), encoding="utf-8")
            self.assertIsNone(ensure_contract(path, "completion-comment"))

    def test_updates_existing_testing_contract_with_command_rule(self) -> None:
        legacy_testing = TESTING_CONTRACT.replace(TESTING_COMMAND_RULE, "")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self._node().replace(
                "      Post a completion record.\n",
                "      Post a completion record.\n"
                + legacy_testing + CONTRACT), encoding="utf-8")
            note = ensure_contract(path, "completion-comment")
            self.assertIn("copy-paste command", note)
            self.assertIn("Keep copy-paste commands standalone", path.read_text())

    def test_missing_node_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("- id: report\n", encoding="utf-8")
            self.assertIsNone(ensure_contract(path, "completion-comment"))

    def test_missing_context_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("  - id: completion-comment\n    prompt: |\n      x\n",
                            encoding="utf-8")
            note = ensure_contract(path, "completion-comment")
            self.assertIn("end not found", note)


if __name__ == "__main__":
    unittest.main()
