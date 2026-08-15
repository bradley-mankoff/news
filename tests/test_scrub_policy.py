import json
import unittest
from pathlib import Path
from unittest import mock

from automation import scrub_policy as sp


class ScrubPolicyTest(unittest.TestCase):
    def _pr(self, number, head, labels=()):
        return {
            "number": number,
            "headRefName": head,
            "title": f"PR {number}",
            "mergeable": "MERGEABLE",
            "labels": {"nodes": [{"name": l} for l in labels]},
            "_labels": set(labels),
        }

    def test_classify_keeps_mainline_and_labeled_prs(self):
        prs = [
            self._pr(1, "feature/a", labels=["rewrite-with-keep-set"]),
            self._pr(2, "feature/b"),  # unlabeled
            self._pr(3, "feature/c", labels=["close-on-scrub"]),
        ]
        heads = [
            "refs/heads/develop",
            "refs/heads/main",
            "refs/heads/feature/a",
            "refs/heads/feature/b",
            "refs/heads/feature/c",
        ]
        with mock.patch.object(sp, "tags", return_value=["v1.0"]):
            keep, delete, close = sp.classify(prs, heads)
        self.assertIn("refs/heads/develop", keep)
        self.assertIn("refs/heads/main", keep)
        self.assertIn("refs/heads/feature/a", keep)
        self.assertIn("refs/tags/v1.0", keep)
        self.assertIn("refs/heads/feature/b", delete)  # unlabeled -> delete
        self.assertNotIn("refs/heads/feature/c", delete)  # close-on-scrub
        self.assertEqual([p["number"] for p in close], [3])

    def test_gates_fail_without_freeze(self):
        prs = [self._pr(1, "feature/a")]
        results = sp.gates({}, prs)
        self.assertFalse(dict((n, ok) for n, ok, _ in results)["freeze flag set"])

    def test_gates_unlabeled_pr_fails(self):
        freeze = {"freeze": True, "start": "2000-01-01T00:00:00+00:00",
                  "end": "2099-01-01T00:00:00+00:00"}
        prs = [self._pr(1, "feature/a")]  # no label
        with (
            mock.patch.object(sp, "board_in_progress", return_value=[]),
            mock.patch.object(sp, "backup_fresh_and_clean",
                              return_value=(True, "backup ok")),
            mock.patch.object(sp, "dry_run_artifact_ok",
                              return_value=(True, "dry-run ok")),
        ):
            results = sp.gates(freeze, prs)
        by_name = dict((n, ok) for n, ok, _ in results)
        self.assertFalse(by_name["every open PR labeled"])

    def test_gates_all_pass_when_frozen_labeled_and_clean(self):
        freeze = {"freeze": True, "start": "2000-01-01T00:00:00+00:00",
                  "end": "2099-01-01T00:00:00+00:00"}
        prs = [self._pr(1, "feature/a", labels=["rewrite-with-keep-set"])]
        with (
            mock.patch.object(sp, "board_in_progress", return_value=[]),
            mock.patch.object(sp, "backup_fresh_and_clean",
                              return_value=(True, "backup ok")),
            mock.patch.object(sp, "dry_run_artifact_ok",
                              return_value=(True, "dry-run ok")),
        ):
            results = sp.gates(freeze, prs)
        by_name = dict((n, ok) for n, ok, _ in results)
        self.assertTrue(all(ok for _, ok, _ in results), by_name)

    def test_freeze_roundtrip(self):
        tmp = sp.FREEZE_FILE
        sp.set_freeze("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
        try:
            freeze = sp.load_freeze()
            self.assertTrue(freeze["freeze"])
            self.assertEqual(freeze["end"], "2026-01-02T00:00:00+00:00")
        finally:
            sp.clear_freeze()
        self.assertEqual(sp.load_freeze(), {})


if __name__ == "__main__":
    unittest.main()
