from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.apply_workflow_edits import CONTRACT, ensure_contract, ensure_node


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
                note, "added Deferred-work contract to completion-comment in wf.yaml")
            text = path.read_text()
            self.assertIn("reads this section and creates a tracking issue", text)
            self.assertLess(text.index(CONTRACT[:40]), text.index("The comment must be factual"))

    def test_already_present_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "wf.yaml"
            path.write_text(self._node().replace(
                "      Post a completion record.\n",
                "      Post a completion record.\n" + CONTRACT), encoding="utf-8")
            self.assertIsNone(ensure_contract(path, "completion-comment"))

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
