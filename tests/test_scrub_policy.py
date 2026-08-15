import json
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
        self.assertNotIn("refs/heads/feature/c", delete)  # close-on-scrub
        self.assertEqual([p["number"] for p in close], [3])

    def test_gates_fail_without_freeze(self):
        prs = [self._pr(1, "feature/a")]
        with (
            mock.patch.object(sp, "board_in_progress", return_value=[]),
            mock.patch.object(sp, "backup_fresh_and_clean",
                              return_value=(True, "backup ok")),
            mock.patch.object(sp, "dry_run_artifact_ok",
                              return_value=(True, "dry-run ok")),
        ):
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

    def test_dry_run_manifest_must_match_current_refs_and_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror = Path(tmpdir) / "mirror"
            mirror.mkdir()
            manifest_path = Path(tmpdir) / "dry-run.json"
            refs = {"refs/heads/main": "abc123"}
            manifest_path.write_text(json.dumps({
                "manifest_version": 1,
                "kind": "dry-run",
                "repo_url": f"https://github.com/{sp.REPO}",
                "mirror_path": str(mirror),
                "remote_refs": refs,
                "declared_identities": sp.DECLARED_IDENTITIES,
                "audit_status": 0,
                "completed_at": sp.now(),
            }))
            with (
                mock.patch.object(sp, "DRY_RUN_MANIFEST", manifest_path),
                mock.patch.object(sp, "remote_ref_snapshot", return_value=refs),
            ):
                ok, detail = sp.dry_run_artifact_ok()
            self.assertTrue(ok, detail)

            manifest_path.write_text(manifest_path.read_text().replace("abc123", "changed"))
            with (
                mock.patch.object(sp, "DRY_RUN_MANIFEST", manifest_path),
                mock.patch.object(sp, "remote_ref_snapshot", return_value=refs),
            ):
                ok, detail = sp.dry_run_artifact_ok()
            self.assertFalse(ok)
            self.assertIn("does not match", detail)

    def test_backup_manifest_must_match_current_refs(self):
        refs = {"refs/heads/main": "abc123"}
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="news-scrub-backup-") as tmpdir:
            candidate = Path(tmpdir)
            (candidate / sp.BACKUP_MANIFEST_NAME).write_text(json.dumps({
                "manifest_version": 1,
                "kind": "backup",
                "repo_url": f"https://github.com/{sp.REPO}",
                "mirror_path": str(candidate.resolve()),
                "remote_refs": refs,
                "completed_at": sp.now(),
                "fsck_status": 0,
            }))
            mirror_refs = mock.Mock(
                returncode=0,
                stdout="abc123 refs/heads/main\n",
                stderr="",
            )
            clean = mock.Mock(returncode=0, stderr="")
            with (
                mock.patch.object(sp, "remote_ref_snapshot", return_value=refs),
                mock.patch.object(sp, "run", side_effect=[mirror_refs, clean]) as run,
            ):
                ok, detail = sp.backup_fresh_and_clean()
            self.assertTrue(ok, detail)
            run.assert_has_calls([
                mock.call(["git", "-C", str(candidate), "show-ref", "--heads"]),
                mock.call(["git", "-C", str(candidate), "fsck", "--no-dangling"]),
            ])

    def test_freeze_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(sp, "FREEZE_FILE", Path(tmpdir) / "freeze.json"):
                sp.set_freeze(
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-02T00:00:00+00:00",
                )
                freeze = sp.load_freeze()
                self.assertTrue(freeze["freeze"])
                self.assertEqual(freeze["end"], "2026-01-02T00:00:00+00:00")
                sp.clear_freeze()
                self.assertEqual(sp.load_freeze(), {})

    def test_execute_aborts_before_any_mutation_when_a_gate_fails(self):
        prs = [self._pr(1, "feature/a", labels=["rewrite-with-keep-set"])]
        with (
            mock.patch.object(sp, "validate_repo_root"),
            mock.patch.object(sp, "open_prs", return_value=prs),
            mock.patch.object(
                sp, "gates", return_value=[("freeze flag set", False, "not set")]
            ),
            mock.patch.object(sp, "gh") as gh,
            mock.patch.object(sp, "run") as run,
        ):
            code = sp.main(["execute"])
        self.assertEqual(code, 1)
        gh.assert_not_called()
        run.assert_not_called()

    def test_execute_stops_after_failed_pr_close(self):
        prs = [self._pr(1, "feature/a", labels=["close-on-scrub"])]
        failed_close = mock.Mock(returncode=1, stderr="permission denied")
        with (
            mock.patch.object(sp, "validate_repo_root"),
            mock.patch.object(sp, "open_prs", return_value=prs),
            mock.patch.object(sp, "gates", return_value=[("all", True, "ok")]),
            mock.patch.object(sp, "remote_refs", return_value=["refs/heads/main"]),
            mock.patch.object(
                sp,
                "classify",
                return_value=(
                    ["refs/heads/main"],
                    ["refs/heads/feature/a"],
                    prs,
                ),
            ),
            mock.patch.object(sp, "gh", return_value=failed_close) as gh,
            mock.patch.object(sp, "run") as run,
        ):
            code = sp.main(["execute"])
        self.assertEqual(code, 1)
        gh.assert_called_once_with(["pr", "close", "1", "-R", sp.REPO])
        run.assert_not_called()

    def test_execute_stops_after_failed_branch_delete(self):
        prs = []
        failed_delete = mock.Mock(returncode=1, stderr="remote rejected")
        with (
            mock.patch.object(sp, "validate_repo_root"),
            mock.patch.object(sp, "open_prs", return_value=prs),
            mock.patch.object(sp, "gates", return_value=[("all", True, "ok")]),
            mock.patch.object(sp, "remote_refs", return_value=["refs/heads/main"]),
            mock.patch.object(
                sp,
                "classify",
                return_value=(
                    ["refs/heads/main"],
                    ["refs/heads/feature/a"],
                    [],
                ),
            ),
            mock.patch.object(sp, "gh") as gh,
            mock.patch.object(sp, "run", return_value=failed_delete) as run,
        ):
            code = sp.main(["execute"])
        self.assertEqual(code, 1)
        gh.assert_not_called()
        run.assert_called_once_with(
            ["git", "push", "origin", ":feature/a"]
        )

    def test_execute_rewrites_only_after_cleanup_succeeds(self):
        prs = []
        success = mock.Mock(returncode=0, stderr="")
        with (
            mock.patch.object(sp, "validate_repo_root"),
            mock.patch.object(sp, "open_prs", return_value=prs),
            mock.patch.object(sp, "gates", return_value=[("all", True, "ok")]),
            mock.patch.object(sp, "remote_refs", return_value=["refs/heads/main"]),
            mock.patch.object(
                sp,
                "classify",
                return_value=(
                    ["refs/heads/main"],
                    ["refs/heads/feature/a"],
                    [],
                ),
            ),
            mock.patch.object(sp, "gh") as gh,
            mock.patch.object(sp, "run", side_effect=[success, success]) as run,
        ):
            code = sp.main(["execute"])
        self.assertEqual(code, 0)
        gh.assert_not_called()
        self.assertEqual(run.call_count, 2)
        run.assert_has_calls([
            mock.call(["git", "push", "origin", ":feature/a"]),
            mock.call(
                ["bash", "automation/scrub_history.sh", "--execute"],
                timeout=1800,
            ),
        ])


if __name__ == "__main__":
    unittest.main()
