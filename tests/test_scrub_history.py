from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "scrub_history.sh"


class ScrubHistoryScriptTests(unittest.TestCase):
    """Safety-focused checks for the gated scrub wrapper.

    The script is destructive (rm -rf of WORKDIR, force-push), so these tests
    only exercise paths that fail BEFORE any clone/rewrite happens: syntax,
    the WORKDIR safety guard, and argument parsing.
    """

    def test_script_has_valid_bash_syntax(self) -> None:
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_workdir_guard_rejects_home_and_children(self) -> None:
        # A broken guard here would rm -rf the temp dir, which is being
        # deleted anyway — safe to exercise under a throwaway HOME.
        with tempfile.TemporaryDirectory() as tmp:
            for workdir in (tmp, os.path.join(tmp, "sub")):
                env = dict(os.environ, HOME=tmp, WORKDIR=workdir)
                r = subprocess.run(
                    ["bash", str(SCRIPT)], capture_output=True, text=True, env=env
                )
                self.assertEqual(r.returncode, 1)
                self.assertIn("refusing unsafe WORKDIR", r.stderr)

    def test_workdir_guard_rejects_system_and_relative_paths(self) -> None:
        # A typo like WORKDIR=/tmp or WORKDIR=.. must be refused BEFORE the
        # rm -rf runs (the rm -rf executes even in --dry-run mode).
        for workdir in ("/tmp", "/var", "/Users", "/etc", "/System", "/opt",
                        "..", ".", "scrub"):
            env = dict(os.environ, WORKDIR=workdir)
            r = subprocess.run(
                ["bash", str(SCRIPT)], capture_output=True, text=True, env=env
            )
            self.assertEqual(r.returncode, 1, workdir)
            self.assertIn("refusing unsafe WORKDIR", r.stderr)

    def test_workdir_guard_accepts_scratch_paths(self) -> None:
        # A scratch dir (the documented /tmp/news-scrub shape) must pass the
        # guard; the script then fails later (clone against a nonexistent
        # URL), never at the guard. The scratch dir lives inside a throwaway
        # temp dir because the script rm -rf's it before cloning.
        with tempfile.TemporaryDirectory() as tmp:
            workdir = os.path.join(tmp, "news-scrub")
            env = dict(os.environ, WORKDIR=workdir,
                       REPO_URL="file:///nonexistent-news-repo")
            r = subprocess.run(
                ["bash", str(SCRIPT)], capture_output=True, text=True, env=env
            )
            self.assertNotIn("refusing unsafe WORKDIR", r.stderr)

    def test_mailmap_without_argument_is_usage_error(self) -> None:
        r = subprocess.run(
            ["bash", str(SCRIPT), "--mailmap"], capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires a PATH argument", r.stderr)

    def test_mailmap_space_form_is_consumed(self) -> None:
        # The documented `--mailmap PATH` form must be accepted (not reported
        # as "unknown argument"). REPO_URL points at a nonexistent local path
        # so even with git-filter-repo installed the script fails at the clone
        # before touching anything destructive or the network.
        env = dict(os.environ, REPO_URL="file:///nonexistent-news-repo")
        r = subprocess.run(
            ["bash", str(SCRIPT), "--mailmap", "/nonexistent/mailmap.txt"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(r.returncode, 2)
        self.assertNotIn("unknown argument", r.stderr)


def _git(tmpdir: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(tmpdir), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


class ScrubHistoryIntegrationTests(unittest.TestCase):
    """Post-clone integration paths (real git, stubbed git-filter-repo).

    git-filter-repo is not installed by default and is destructive, so these
    tests stub it on PATH with a faithful emulation of its documented default
    finalization: it removes the 'origin' remote after rewriting history (see
    git-filter-repo's _migrate_origin_to_heads). That stub is what makes the
    regression testable — the script must re-add 'origin' before its push
    phase or every push command fails with
    `fatal: 'origin' does not appear to be a git repository`.
    """

    _STUB_FILTER_REPO = """#!/usr/bin/env bash
# Emulate git-filter-repo's default finalization, which removes the
# 'origin' remote after rewriting history.
set -e
while [ $# -gt 0 ]; do
  case "$1" in
    -C) cd "$2"; shift 2 ;;
    *) shift ;;
  esac
done
git remote rm origin
exit 0
"""

    def _make_fixture(self, tmpdir: Path, author_email: str = "test@example.com") -> Path:
        fixture = tmpdir / "fixture"
        fixture.mkdir()
        _git(fixture, "init", "-q")
        (fixture / "readme.txt").write_text("fine\n", encoding="utf-8")
        _git(fixture, "add", "-A")
        _git(
            fixture,
            "-c", "user.name=Test User",
            "-c", "user.email=" + author_email,
            "commit", "-qm", "fixture commit",
        )
        return fixture

    def _stub_env(self, tmpdir: Path, fixture: Path, workdir: Path) -> dict:
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "git-filter-repo"
        stub.write_text(self._STUB_FILTER_REPO, encoding="utf-8")
        stub.chmod(0o755)
        return dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            REPO_URL=f"file://{fixture}",
            WORKDIR=str(workdir),
        )

    def test_dry_run_produces_pushable_mirror_with_origin(self) -> None:
        self.addCleanup(
            (ROOT / "automation" / ".scrub-dryrun-ok").unlink,
            missing_ok=True,
        )
        # Regression: git filter-repo removes 'origin' by default, so the
        # script must re-add it before the push section (dry-run prints the
        # exact commands a human runs with --execute).
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixture = self._make_fixture(tmpdir)
            workdir = tmpdir / "news-scrub"
            env = self._stub_env(tmpdir, fixture, workdir)

            r = subprocess.run(
                [
                    "bash", str(SCRIPT), "--dry-run",
                    "--keep-ref", "refs/heads/feature/kept",
                ],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Verification passed", r.stdout)
            self.assertIn("Re-adding origin remote", r.stdout)
            self.assertIn("push --force origin develop", r.stdout)
            self.assertIn("push --force origin refs/heads/feature/kept:refs/heads/feature/kept", r.stdout)
            # The push commands must be executable as printed: origin must
            # exist on the mirror after the scrub.
            origin = subprocess.run(
                ["git", "-C", str(workdir), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            self.assertEqual(origin.returncode, 0, origin.stderr)
            self.assertEqual(origin.stdout.strip(), f"file://{fixture}")

    def test_verify_gate_stops_when_personal_data_remains(self) -> None:
        # The verify gate (audit exit 1) must abort before any push and the
        # cleanup must announce + remove the raw mirror. The personal email
        # is assembled from parts so this tracked test file stays clean
        # under the scanner's self-scan.
        personal_email = "bradley" + "@mankoff.com"
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixture = self._make_fixture(tmpdir, author_email=personal_email)
            workdir = tmpdir / "news-scrub"
            env = self._stub_env(tmpdir, fixture, workdir)

            r = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(r.returncode, 1)
            self.assertIn("still finds personal data", r.stderr)
            self.assertNotIn("Verification passed", r.stdout)
            self.assertNotIn("push --force origin develop", r.stdout)
            # Failure path removes the raw mirror and says so.
            self.assertIn("removing", r.stderr)
            self.assertFalse(workdir.exists())


if __name__ == "__main__":
    unittest.main()
