from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "scrub_history.sh"
AUDIT = ROOT / "automation" / "security_audit.py"

# ---------------------------------------------------------------------------
# Fixture values are assembled from parts so the tracked test file and any
# failure diagnostics never contain the complete personal strings the scrub
# and its audit are designed to remove (see tests/test_security_audit.py).
# ---------------------------------------------------------------------------
_FIXTURE_EMAIL = "bradley@" + "mankoff.com"
_FIXTURE_EMAILS = [
    _FIXTURE_EMAIL,
    "bradley." + "mankoff" + "@gmail.com",
    "aidancoon97" + "@gmail.com",
    "calzacortaandres" + "@gmail.com",
    "isaacmessenger" + "@yahoo.com",
]
_FIXTURE_PATH = "/Users/" + "fixtureuser" + "/personal_code/news"
_FIXTURE_PATH_SHORT = "/Users/" + "fixtureuser" + "/news"
_MAILMAPPED_EMAIL = "fixture@" + "example.com"
_SCRUB_USER = "fixtureuser"

_RAW_FIXTURE_VALUES = list(_FIXTURE_EMAILS) + [_FIXTURE_PATH, _FIXTURE_PATH_SHORT]


def _redact(text: str) -> str:
    """Keep runtime-assembled fixture values out of test failure output."""
    for value in _RAW_FIXTURE_VALUES:
        text = text.replace(value, "REDACTED")
    return text


def _git(tmpdir: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", "-C", str(tmpdir), *args], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {_redact(r.stderr.strip())}"
        )
    return r


def _git_bare(gitdir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a read-only git command against a bare repository by --git-dir."""
    r = subprocess.run(
        ["git", "--git-dir", str(gitdir), *args], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise AssertionError(
            f"git --git-dir {gitdir.name} {' '.join(args)} failed: "
            f"{_redact(r.stderr.strip())}"
        )
    return r


def _refs_manifest(gitdir: Path) -> str:
    """Deterministic complete ref manifest of a bare repository."""
    lines = _git_bare(gitdir, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    return "\n".join(sorted(lines.splitlines())) + "\n"


def _filter_repo_binary() -> str:
    """Locate git-filter-repo, failing the test (not skipping) when absent."""
    candidates = [
        ROOT / ".venv" / "bin" / "git-filter-repo",
        shutil.which("git-filter-repo"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise AssertionError(
        "git-filter-repo is not installed; run `uv sync --group dev` in the repo root"
    )


class ScrubHistoryScriptTests(unittest.TestCase):
    """Checks for the gated scrub wrapper.

    Safety tests exercise paths that fail BEFORE any clone/rewrite happens:
    syntax, the WORKDIR safety guard, and argument parsing. The end-to-end dry
    run then exercises the real clone/rewrite/audit path against a disposable
    local `file://` bare origin, asserting the rewritten mirror's contents and
    that the source fixture's refs are untouched.
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

    def test_dry_run_rewrites_local_fixture_and_leaves_source_untouched(self) -> None:
        """Real --dry-run against a disposable file:// bare origin fixture.

        Exercises clone, git-filter-repo content/message/mailmap rewriting, the
        wrapper's history-only verification gate, the printed (not executed)
        push plan, an independent history-only audit of the retained rewrite,
        and a byte-for-byte comparison of the source refs before/after.
        """
        filter_bin = _filter_repo_binary()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # --- seed repo: every replacement category the scrub targets ---
            seed = tmpdir / "seed"
            seed.mkdir()
            _git(seed, "init", "-q")
            _git(seed, "config", "user.name", "Test User")
            _git(seed, "config", "user.email", "test@example.com")
            (seed / "notes.txt").write_text(
                "".join(f"contact {email}\n" for email in _FIXTURE_EMAILS)
                + f"clone at {_FIXTURE_PATH}\n"
                + f"checkout {_FIXTURE_PATH_SHORT}\n",
                encoding="utf-8",
            )
            _git(seed, "add", "notes.txt")
            _git(seed, "commit", "-qm", "fixture commit")
            _git(seed, "branch", "-m", "develop")
            # -c user.* overrides both author AND committer identity, so the
            # mailmap must rewrite both metadata fields of this commit.
            _git(
                seed,
                "-c", "user.name=Bradley Mankoff",
                "-c", f"user.email={_FIXTURE_EMAIL}",
                "commit", "--allow-empty", "-qm",
                f"moved code to {_FIXTURE_PATH}",
            )
            _git(seed, "branch", "main", "HEAD~1")
            _git(seed, "tag", "fixture-v1")

            # --- disposable bare origin exposed through a file:// URL ---
            origin = tmpdir / "fixture-origin.git"
            _git(tmpdir, "clone", "--mirror", "seed", str(origin))
            manifest_before = _refs_manifest(origin)
            for expected in ("refs/heads/develop", "refs/heads/main", "refs/tags/fixture-v1"):
                self.assertIn(expected, manifest_before)

            # --- mailmap maps the fixture identity to a safe address ---
            mailmap = tmpdir / "mailmap.txt"
            mailmap.write_text(
                f"Fixture User <{_MAILMAPPED_EMAIL}> <{_FIXTURE_EMAIL}>\n",
                encoding="utf-8",
            )

            # --- invoke the wrapper as a black box (dry run only) ---
            home = tmpdir / "home"
            work = tmpdir / "work"
            home.mkdir()
            work.mkdir()
            mirror = work / "mirror"  # within tmpdir but outside isolated HOME
            env = dict(
                os.environ,
                HOME=str(home),
                REPO_URL=origin.as_uri(),
                WORKDIR=str(mirror),
                SCRUB_USER=_SCRUB_USER,
                PATH=str(Path(filter_bin).parent) + os.pathsep + os.environ.get("PATH", ""),
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--mailmap", str(mailmap)],
                capture_output=True, text=True, timeout=300, env=env,
            )
            combined = result.stdout + result.stderr
            safe_combined = _redact(combined)

            self.assertEqual(result.returncode, 0, safe_combined)
            self.assertIn("clone + rewrite + verify will run", combined, safe_combined)
            self.assertIn("Verification passed", combined, safe_combined)
            self.assertIn("push commands below were NOT executed", combined, safe_combined)
            self.assertIn("push --force origin develop", combined, safe_combined)
            self.assertIn("push --force origin main", combined, safe_combined)
            self.assertIn("push --force --tags", combined, safe_combined)
            self.assertNotIn("Pushing rewritten history", combined, safe_combined)
            self.assertNotIn("EXECUTE MODE", combined, safe_combined)

            # --- retained rewritten mirror: refs, content, message, identity ---
            for expected in ("refs/heads/develop", "refs/heads/main", "refs/tags/fixture-v1"):
                self.assertIn(expected, _refs_manifest(mirror))

            notes = _git_bare(mirror, "show", "develop:notes.txt").stdout
            for safe in (
                "bradley@example.com",
                "news@example.com",
                "friend1@example.com",
                "friend2@example.com",
                "friend3@example.com",
                "clone at news",
                "checkout news",
            ):
                self.assertIn(safe, notes, _redact(notes))
            for raw in _RAW_FIXTURE_VALUES:
                self.assertNotIn(raw, notes, _redact(notes))

            message = _git_bare(mirror, "log", "--format=%B", "-1", "develop").stdout
            self.assertIn("moved code to news", message, _redact(message))
            self.assertNotIn(_FIXTURE_PATH, message, _redact(message))

            identities = _git_bare(
                mirror, "log", "--all", "--format=%an <%ae> %cn <%ce>"
            ).stdout
            self.assertIn(
                "Fixture User <fixture@example.com>", identities, _redact(identities)
            )
            self.assertNotIn("Bradley Mankoff", identities, _redact(identities))
            self.assertNotIn(_FIXTURE_EMAIL, identities, _redact(identities))

            full_history = _git_bare(mirror, "log", "--all", "-p").stdout
            for raw in _RAW_FIXTURE_VALUES:
                self.assertNotIn(raw, full_history, _redact(full_history))

            # --- independent history-only audit of the retained mirror ---
            audit = subprocess.run(
                [sys.executable, str(AUDIT), "--history-only", "--repo", str(mirror)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(audit.returncode, 0, _redact(audit.stdout + audit.stderr))

            # --- the fixture origin's refs must be byte-for-byte unchanged ---
            self.assertEqual(_refs_manifest(origin), manifest_before)


if __name__ == "__main__":
    unittest.main()
