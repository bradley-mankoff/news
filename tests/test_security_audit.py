from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from automation.security_audit import (
    _match_categories,
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
            findings, skipped = scan_working_tree(tmpdir)

        self.assertEqual(skipped, 0)
        self.assertTrue(
            any(f.category == "personal-email" and f.path == "recipients.txt" for f in findings)
        )

    def test_tree_scan_finds_personal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"notes.txt": f"Checkout lives at {_PERSONAL_PATH}.\n"})
            findings, _ = scan_working_tree(tmpdir)

        self.assertTrue(
            any(f.category == "personal-path" and f.path == "notes.txt" for f in findings)
        )

    def test_secret_patterns_detect_common_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"secrets.txt": "\n".join(_SECRET_SAMPLES) + "\n"})
            findings, _ = scan_working_tree(tmpdir)

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
            findings, skipped = scan_working_tree(tmpdir)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertEqual(findings, [])
        self.assertEqual(skipped, 0)
        self.assertEqual(code, 0)

    def test_findings_exit_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"leak.txt": f"{_PERSONAL_EMAIL}\n"})
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertEqual(code, 1)

    # --- report redaction (regression for the raw-secret leak) ---------------

    def test_report_redacts_secret_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"secrets.txt": "\n".join(_SECRET_SAMPLES) + "\n"})
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        report = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("secret:", report)  # findings are still reported...
        for sample in _SECRET_SAMPLES:
            self.assertNotIn(sample, report)  # ...but never raw
        # Round-trip guarantee: the redacted report must not itself re-trigger
        # the scanner, so it can be checked in safely.
        self.assertEqual(_match_categories(report), [])

    def test_report_redacts_personal_email_in_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"leak.txt": f"{_PERSONAL_EMAIL}\n"})
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        report = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("bradley[@]mankoff[.]com", report)
        self.assertNotIn(_PERSONAL_EMAIL, report)
        self.assertEqual(_match_categories(report), [])

    def test_report_redacts_local_host_author_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"readme.txt": "hello\n"})
            _git(
                tmpdir,
                "-c", "user.name=Owner",
                "-c", "user.email=" + "bradley_mankoff" + "@" + "macmini" + ".local",
                "commit", "--allow-empty", "-qm", "personal commit",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        report = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("bradley_mankoff[@]***", report)
        self.assertNotIn("macmini.local", report)
        self.assertIn("- [x] `env.json` was never committed", report)

    def test_report_file_written_and_matches_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"readme.txt": "all clear\n"})
            out_file = tmpdir / "report.md"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir), "--report", str(out_file)])

            self.assertEqual(code, 0)
            self.assertIn("report written to", stdout.getvalue())
            # Read inside the TemporaryDirectory block: the dir is deleted on exit.
            text = out_file.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("# Security Audit Report"))
        self.assertIn("- [x] `env.json` was never committed", text)

    # --- history content / messages / env.json -------------------------------

    def test_history_scan_reports_content_messages_and_env_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(
                tmpdir,
                {
                    "c.txt": f"contact {_PERSONAL_EMAIL}\npath {_PERSONAL_PATH}\n",
                    "env.json": '{"SMTP_PASSWORD": "x"}\n',
                },
            )
            _git(
                tmpdir,
                "-c", "user.name=T",
                "-c", "user.email=t@example.com",
                "commit", "--allow-empty", "-qm", f"added {_PERSONAL_PATH} files",
            )
            history = scan_history(tmpdir)

        cats = {s.category for s in history.content_stats}
        self.assertIn("personal-email", cats)
        self.assertIn("personal-path", cats)
        self.assertTrue(any(h[1] == "personal-path" for h in history.message_hits))
        self.assertTrue(history.env_json_commits)
        self.assertTrue(history.has_findings)

    def test_message_body_secrets_are_scanned_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"readme.txt": "hello\n"})
            secret = _SECRET_SAMPLES[0]
            _git(
                tmpdir,
                "-c", "user.name=Test User",
                "-c", "user.email=test@example.com",
                "commit", "--allow-empty",
                "-m", "add key",
                "-m", f"rotating: {secret}",
            )
            history = scan_history(tmpdir)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertTrue(any(h[1] == "secret" for h in history.message_hits))
        report = stdout.getvalue()
        self.assertNotIn(secret, report)
        self.assertIn("[REDACTED]", report)

    # --- --history-only / bare mirror (scrub verify gate) --------------------

    def test_history_only_skips_tree_and_scans_bare_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"clean.txt": "fine\n"})
            _git(
                tmpdir,
                "-c", "user.name=Owner",
                "-c", f"user.email={_PERSONAL_EMAIL}",
                "commit", "--allow-empty", "-qm", "personal commit",
            )
            # Uncommitted data must not matter: the mirror holds refs only and
            # --history-only skips the tree scan entirely.
            (tmpdir / "uncommitted-leak.txt").write_text(f"{_PERSONAL_EMAIL}\n")
            bare = tmpdir / "bare.git"
            _git(tmpdir, "clone", "--mirror", str(tmpdir), str(bare))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = audit_main(["--history-only", "--repo", str(bare)])

        self.assertEqual(code, 1)  # history finding (personal author email)

    def test_bare_repo_without_history_only_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _make_repo(tmpdir, {"clean.txt": "fine\n"})
            bare = tmpdir / "bare.git"
            _git(tmpdir, "clone", "--mirror", str(tmpdir), str(bare))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = audit_main(["--repo", str(bare)])

        # A bare repo has no worktree, so the tree scan cannot run; the failure
        # must surface as a clean usage-error exit, not a traceback.
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    # --- failure modes --------------------------------------------------------

    def test_non_git_repo_returns_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)  # plain directory, no .git
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = audit_main(["--repo", str(tmpdir)])

        self.assertEqual(code, 2)
        self.assertIn("error:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
