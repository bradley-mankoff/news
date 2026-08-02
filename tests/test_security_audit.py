from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from automation.security_audit import (
    main as audit_main,
    scan_history,
    scan_working_tree,
)

# Fixture values are assembled from parts so the tracked test file never
# contains the raw personal-data/secret strings the scanner detects.
_PERSONAL_EMAIL = "bradley@" + "mankoff.com"
_PERSONAL_PATH = "/Users/" + "home/news"
_SECRET_SAMPLES = [
    "sk-" + "AbCdEfGhIjKlMnOpQrStUvWxYz",
    "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "AIza" + "Sy" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456",
    "xoxb-" + "1234567890123-1234567890123-abcdefghijkl",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
]


def _git(tmpdir: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(tmpdir), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def _make_repo(tmpdir: Path, files: dict[str, str]) -> None:
    _git(tmpdir, "init", "-q")
    for rel, content in files.items():
        path = tmpdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(tmpdir, "add", "-A")
    _git(
        tmpdir,
        "-c", "user.name=Test User",
        "-c", "user.email=test@example.com",
        "commit", "-qm", "fixture commit",
    )


class SecurityAuditTests(unittest.TestCase):
    def test_tree_scan_finds_personal_email_in_fixture_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"recipients.txt": f"Contact: {_PERSONAL_EMAIL}\n"})
            findings = scan_working_tree(tmpdir)

        self.assertTrue(
            any(f.category == "personal-email" and f.path == "recipients.txt" for f in findings)
        )

    def test_tree_scan_finds_personal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"notes.txt": f"Checkout lives at {_PERSONAL_PATH}.\n"})
            findings = scan_working_tree(tmpdir)

        self.assertTrue(
            any(f.category == "personal-path" and f.path == "notes.txt" for f in findings)
        )

    def test_secret_patterns_detect_common_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"secrets.txt": "\n".join(_SECRET_SAMPLES) + "\n"})
            findings = scan_working_tree(tmpdir)

        categories = {f.category for f in findings}
        self.assertTrue(any(c.startswith("secret:") for c in categories))
        for expected in (
            "secret:openai-api-key",
            "secret:github-token",
            "secret:aws-access-key",
            "secret:google-api-key",
            "secret:slack-token",
            "secret:private-key-header",
        ):
            self.assertIn(expected, categories)

    def test_history_scan_reports_author_emails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"readme.txt": "hello\n"})
            _git(
                tmpdir,
                "-c", "user.name=Owner",
                "-c", f"user.email={_PERSONAL_EMAIL}",
                "commit", "--allow-empty", "-qm", "personal commit",
            )
            history = scan_history(tmpdir)

        personal = [email for email, _, count, flagged in history.authors if flagged]
        self.assertIn(_PERSONAL_EMAIL, personal)
        self.assertEqual(history.total_commits, 2)

    def test_clean_tree_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"readme.txt": "all clear\n"})
            findings = scan_working_tree(tmpdir)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertEqual(findings, [])
        self.assertEqual(code, 0)

    def test_findings_exit_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"leak.txt": f"{_PERSONAL_EMAIL}\n"})
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
