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


if __name__ == "__main__":
    unittest.main()
