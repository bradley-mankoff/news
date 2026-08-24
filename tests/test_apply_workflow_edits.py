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
    ensure_tier_models,
)


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

    def test_refreshes_outdated_testing_contract(self) -> None:
        legacy_testing = TESTING_CONTRACT.replace(TESTING_COMMAND_RULE, "")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self._node().replace(
                "      Post a completion record.\n",
                "      Post a completion record.\n"
                + legacy_testing + CONTRACT), encoding="utf-8")
            note = ensure_contract(path, "completion-comment")
            self.assertIn("How-to-test", note)
            text = path.read_text()
            self.assertEqual(text.count("## How to test"), 1)
            self.assertEqual(text.count("## Deferred work"), 1)
            self.assertIn("Keep copy-paste commands standalone", text)
            self.assertIn("### Machine checks", text)

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


class EnsureTierModelsTest(unittest.TestCase):
    LEGACY_BLOCK = (
        "  - id: resolve\n"
        "    provider: pi\n"
        "    model: openai-codex/gpt-5.6-luna\n"
        "    modelReasoningEffort: max\n"
        "    effort: max\n"
        "    prompt: |\n"
        "      Resolve conflicts.\n"
        "    depends_on: []\n"
        "    context: fresh\n"
    )
    EXPECTED_BLOCK = (
        "  - id: resolve\n"
        "    model: large\n"
        "    prompt: |\n"
        "      Resolve conflicts.\n"
        "    depends_on: []\n"
        "    context: fresh\n"
    )

    def test_legacy_pinned_block_becomes_tier_based(self) -> None:
        # Regression: a node pinned by the old patcher (provider + literal
        # model + effort) must become exactly one correctly indented
        # `model: large`, with no leftover provider/effort/model fields.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self.LEGACY_BLOCK, encoding="utf-8")
            note = ensure_tier_models(path, ("resolve",))
            self.assertEqual(note, "pinned model: large on nodes in wf.yaml")
            text = path.read_text()
            self.assertEqual(text, self.EXPECTED_BLOCK)
            self.assertEqual(text.count("model: large"), 1)
            self.assertNotIn("provider:", text)
            self.assertNotIn("effort:", text)
            self.assertNotIn("modelReasoningEffort", text)
            self.assertNotIn("gpt-5.6-luna", text)

    def test_repeated_application_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self.LEGACY_BLOCK, encoding="utf-8")
            self.assertIsNotNone(ensure_tier_models(path, ("resolve",)))
            rewritten = path.read_text()
            self.assertIsNone(ensure_tier_models(path, ("resolve",)))
            self.assertIsNone(ensure_tier_models(path, ("resolve",)))
            self.assertEqual(path.read_text(), rewritten)
            self.assertEqual(path.read_text().count("model: large"), 1)

    def test_clean_node_still_gets_tier_assignment(self) -> None:
        # A target node with no model fields at all still gets `model: large`.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("  - id: agent\n    prompt: |\n      Assist.\n"
                            "    context: fresh\n", encoding="utf-8")
            note = ensure_tier_models(path, ("agent",))
            self.assertEqual(note, "pinned model: large on nodes in wf.yaml")
            text = path.read_text()
            self.assertIn("    model: large\n    prompt: |", text)
            self.assertEqual(text.count("model: large"), 1)

    def test_unrelated_nodes_untouched(self) -> None:
        # Only target nodes are rewritten; other nodes keep their fields,
        # so unrelated workflow surgery survives re-running the helper.
        other = ("  - id: other\n"
                 "    provider: pi\n"
                 "    model: openai-codex/gpt-5.6-luna\n"
                 "    effort: max\n"
                 "    context: fresh\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self.LEGACY_BLOCK + "\n" + other, encoding="utf-8")
            ensure_tier_models(path, ("resolve",))
            text = path.read_text()
            self.assertIn(self.EXPECTED_BLOCK, text)
            self.assertIn(other, text)
            self.assertEqual(text.count("model: large"), 1)

    def test_missing_node_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text("  - id: other\n    context: fresh\n",
                            encoding="utf-8")
            note = ensure_tier_models(path, ("resolve",))
            self.assertIn("missing nodes: resolve", note)
            self.assertIn("checked large-tier nodes", note)


if __name__ == "__main__":
    unittest.main()
