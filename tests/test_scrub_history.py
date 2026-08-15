from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "scrub_history.sh"


class ScrubHistoryScriptTests(unittest.TestCase):
    """Safety-focused checks for the gated scrub wrapper.

    The script is destructive (rm -rf of WORKDIR, force-push). These tests
    cover syntax, pre-rewrite safety guards, argument parsing, and a local
    fixture proving that execute mode consumes the retained dry-run mirror.
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

    def test_execute_consumes_verified_mirror_and_restores_origin(self) -> None:
        def git(*args: str, cwd: Path | None = None) -> None:
            subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.git"
            seed = root / "seed"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home = root / "home"
            home.mkdir()

            git("init", "--bare", str(source))
            git("init", str(seed))
            git("config", "user.name", "Fixture User", cwd=seed)
            git("config", "user.email", "fixture@example.com", cwd=seed)
            (seed / "README").write_text("clean fixture\n")
            git("add", "README", cwd=seed)
            git("commit", "-m", "fixture", cwd=seed)
            git("branch", "-M", "develop", cwd=seed)
            git("branch", "main", cwd=seed)
            git("tag", "fixture-v1", cwd=seed)
            git("remote", "add", "origin", source.as_uri(), cwd=seed)
            git("push", "origin", "develop", "main", "--tags", cwd=seed)

            fake_filter = fake_bin / "git-filter-repo"
            fake_filter.write_text(
                "#!/bin/sh\ngit remote remove origin\nprintf removed > \"$FILTER_LOG\"\n"
            )
            fake_filter.chmod(0o755)
            filter_log = root / "filter.log"
            env = dict(
                os.environ,
                HOME=str(home),
                PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                REPO_URL=source.as_uri(),
                WORKDIR=str(root / "mirror"),
                FILTER_LOG=str(filter_log),
            )

            dry_run = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("Verification passed", dry_run.stdout)
            self.assertEqual(filter_log.read_text(), "removed")

            mirror = Path(env["WORKDIR"])
            state = Path(f"{env['WORKDIR']}.scrub-state")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(mirror), "remote", "get-url", "origin"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                source.as_uri(),
            )
            self.assertTrue(state.is_file())
            self.assertEqual(stat.S_IMODE(state.stat().st_mode) & 0o077, 0)

            # The marker would disappear if execute mode deleted and recloned
            # the mirror instead of consuming the retained artifact.
            marker = mirror / "operator-reviewed.marker"
            marker.write_text("retained\n")

            execute = subprocess.run(
                ["bash", str(SCRIPT), "--execute"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(execute.returncode, 0, execute.stderr)
            self.assertIn("No clone or rewrite will run", execute.stdout)
            self.assertTrue(marker.is_file())


if __name__ == "__main__":
    unittest.main()
