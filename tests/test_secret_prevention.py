"""Secret-scanning prevention tests.

Covers the checked-in Gitleaks pre-commit hook (`.pre-commit-config.yaml`):

- Config invariants: the pinned upstream `gitleaks` hook at `v8.30.1`, the
  redacted staged-only entry left unoverridden, and `pre-commit>=4.6.1` in
  the dev dependency group only.
- Integration: a normal `git commit` (no `--no-verify`, no `SKIP=gitleaks`)
  containing a deterministic Gitleaks-detectable secret is rejected before
  the commit is created, with redacted output and an isolated
  `PRE_COMMIT_HOME`.

The test secret is assembled from parts so the tracked test file never
contains the complete detector match (mirrors tests/test_security_audit.py).
The test must FAIL (not skip) if pre-commit is unavailable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"

UPSTREAM_REPO = "https://github.com/gitleaks/gitleaks"
UPSTREAM_REV = "v8.30.1"
UPSTREAM_ENTRY = "gitleaks git --pre-commit --redact --staged --verbose"

# Deterministic Gitleaks-detectable AWS access key (AKIA + 16
# alphanumerics; the aws-access-token rule has no entropy threshold),
# assembled from parts: never embed the complete token in tracked source.
_TOKEN = "AKIA" + "ABCDEFGHIJKLMNOP"


def _safe_diagnostic(text: str) -> str:
    """Keep the assembled fixture out of test failure output."""
    return text.replace(_TOKEN, "REDACTED")


def _git(tmpdir: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(tmpdir), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {_safe_diagnostic(r.stderr.strip())}")
    return r


def _pre_commit_binary() -> str:
    """Locate pre-commit, failing the test (not skipping) when absent."""
    candidates = [ROOT / ".venv" / "bin" / "pre-commit", shutil.which("pre-commit")]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise AssertionError(
        "pre-commit is not installed; run `uv sync --group dev` in the repo root"
    )


class PreCommitConfigTests(unittest.TestCase):
    def test_config_pins_upstream_gitleaks_hook(self) -> None:
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(cfg["minimum_pre_commit_version"], "4.6.1")
        self.assertEqual(len(cfg["repos"]), 1)
        repo = cfg["repos"][0]
        self.assertEqual(repo["repo"], UPSTREAM_REPO)
        self.assertEqual(repo["rev"], UPSTREAM_REV)
        self.assertEqual(len(repo["hooks"]), 1)
        hook = repo["hooks"][0]
        self.assertEqual(hook["id"], "gitleaks")
        # The upstream redacted staged-only entry must not be overridden.
        self.assertIn(hook.get("entry"), (None, UPSTREAM_ENTRY))
        self.assertNotIn("args", hook)

    def test_pre_commit_is_dev_only_dependency(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")
        match = re.search(r"\[dependency-groups\]\s*dev\s*=\s*\[(.*?)\]", text, re.S)
        self.assertIsNotNone(match, "dev dependency group must exist")
        dev = match.group(1)
        self.assertIn('"pre-commit>=4.6.1"', dev)
        self.assertNotIn("pre-commit", text.split("[dependency-groups]")[0])


class SecretPreventionHookTests(unittest.TestCase):
    def test_secret_diagnostics_redact_fixture(self) -> None:
        self.assertEqual(
            _safe_diagnostic(f"hook output: {_TOKEN}"),
            "hook output: REDACTED",
        )

    def test_commit_with_staged_secret_is_rejected_and_redacted(self) -> None:
        pre_commit = _pre_commit_binary()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _git(tmpdir, "init", "-q")
            # The hook needs the project config; the whole temp dir (config,
            # working tree, hook cache) is deleted when the test exits.
            shutil.copy2(CONFIG, tmpdir / ".pre-commit-config.yaml")
            env = dict(
                os.environ,
                PRE_COMMIT_HOME=str(tmpdir / "pre-commit-home"),
                PATH=str(ROOT / ".venv" / "bin") + os.pathsep + os.environ.get("PATH", ""),
            )
            r = subprocess.run(
                [pre_commit, "install"], cwd=tmpdir, env=env,
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(r.returncode, 0, _safe_diagnostic(r.stderr))

            (tmpdir / "notes.txt").write_text(
                f"temporary api key: {_TOKEN}\n", encoding="utf-8"
            )
            _git(tmpdir, "add", "notes.txt")
            before = subprocess.run(
                ["git", "-C", str(tmpdir), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(before.returncode, 0, "fixture repo must start empty")

            r = subprocess.run(
                [
                    "git", "-C", str(tmpdir),
                    "-c", "user.name=Test User",
                    "-c", "user.email=test@example.com",
                    "commit", "-qm", "add notes",
                ],
                env=env, capture_output=True, text=True, timeout=600,
            )
            combined = r.stdout + r.stderr

            # Rejected: non-zero exit, no commit created, secret redacted.
            safe_combined = _safe_diagnostic(combined)
            self.assertNotEqual(r.returncode, 0, "secret commit unexpectedly succeeded")
            after = subprocess.run(
                ["git", "-C", str(tmpdir), "rev-parse", "--verify", "HEAD"],
                capture_output=True, text=True,
            )
            self.assertEqual(
                (after.returncode, after.stdout),
                (before.returncode, before.stdout),
                "rejected commit unexpectedly changed HEAD",
            )
            self.assertIn("REDACTED", combined, safe_combined)
            self.assertNotIn(_TOKEN, combined, safe_combined)

            # No history was created, and nothing else captured the token.
            log = _git(tmpdir, "log", "--all", "--format=%B")
            self.assertNotIn(_TOKEN, log.stdout, _safe_diagnostic(log.stdout))
            for rel in _git(ROOT, "ls-files").stdout.splitlines():
                path = ROOT / rel
                if path.is_file():
                    self.assertNotIn(
                        _TOKEN,
                        path.read_text(encoding="utf-8", errors="ignore"),
                        f"tracked file {rel} contains the assembled test token",
                    )


if __name__ == "__main__":
    unittest.main()
