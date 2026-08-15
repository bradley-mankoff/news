import json
import subprocess
import sys
import tempfile
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
        self.assertIn("refs/heads/feature/c", delete)  # close then delete
        self.assertEqual([p["number"] for p in close], [3])

    def test_open_prs_normalizes_cli_label_list(self):
        response = mock.Mock(
            returncode=0,
            stdout=json.dumps([{
                "number": 7,
                "headRefName": "feature/labeled",
                "labels": [{"name": "rewrite-with-keep-set"}],
            }]),
            stderr="",
        )
        with mock.patch.object(sp, "gh", return_value=response):
            prs = sp.open_prs()
        self.assertEqual(prs[0]["_labels"], {"rewrite-with-keep-set"})

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
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            sp, "FREEZE_FILE", Path(directory) / "freeze.json"
        ):
            sp.set_freeze("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
            freeze = sp.load_freeze()
            self.assertTrue(freeze["freeze"])
            self.assertEqual(freeze["end"], "2026-01-02T00:00:00+00:00")
            sp.clear_freeze()
            self.assertEqual(sp.load_freeze(), {})

    def test_execute_gate_failure_has_no_mutations(self):
        with (
            mock.patch.object(sys, "argv", ["scrub_policy.py", "execute"]),
            mock.patch.object(sp, "open_prs", return_value=[]),
            mock.patch.object(sp, "gates", return_value=[("freeze", False, "not set")]),
            mock.patch.object(sp, "gh") as gh,
            mock.patch.object(sp, "run") as run,
        ):
            self.assertEqual(sp.main(), 1)
        gh.assert_not_called()
        run.assert_not_called()

    def test_plan_is_read_only(self):
        prs = [self._pr(1, "feature/a", labels=["close-on-scrub"])]
        with (
            mock.patch.object(sp, "remote_refs", return_value=["refs/heads/main"]),
            mock.patch.object(sp, "tags", return_value=[]),
            mock.patch.object(sp, "run") as run,
        ):
            text = sp.plan_text(prs)
        self.assertIn("KEEP", text)
        self.assertIn("CLOSE PRs", text)
        run.assert_not_called()

    def test_execute_aborts_before_rewrite_after_remote_failure(self):
        failed = subprocess.CompletedProcess([], 1, "", "delete failed")
        with (
            mock.patch.object(sys, "argv", ["scrub_policy.py", "execute"]),
            mock.patch.object(sp, "open_prs", return_value=[]),
            mock.patch.object(sp, "gates", return_value=[("all", True, "ok")]),
            mock.patch.object(sp, "remote_refs", return_value=["refs/heads/old"]),
            mock.patch.object(
                sp,
                "classify",
                return_value=([], ["refs/heads/old"], []),
            ),
            mock.patch.object(sp, "run", return_value=failed) as run,
        ):
            self.assertEqual(sp.main(), 1)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("scrub_history.sh", " ".join(run.call_args.args[0]))

    def test_execute_passes_kept_pr_heads_to_scrub(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(sys, "argv", ["scrub_policy.py", "execute"]),
            mock.patch.object(sp, "open_prs", return_value=[]),
            mock.patch.object(sp, "gates", return_value=[("all", True, "ok")]),
            mock.patch.object(sp, "remote_refs", return_value=[]),
            mock.patch.object(
                sp,
                "classify",
                return_value=(
                    ["refs/heads/develop", "refs/heads/main", "refs/heads/feature/a"],
                    [],
                    [],
                ),
            ),
            mock.patch.object(sp, "run", return_value=ok) as run,
        ):
            self.assertEqual(sp.main(), 0)
        scrub_command = run.call_args.args[0]
        self.assertIn("--keep-ref", scrub_command)
        self.assertIn("refs/heads/feature/a", scrub_command)


if __name__ == "__main__":
    unittest.main()
