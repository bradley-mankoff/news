"""CI secret-scanning contract tests.

Guards the `secret-scan` gate in `.github/workflows/ci.yml` against drift:

- The job must exist, run only for `pull_request` events, and hold only
  `contents: read`.
- Checkout must fetch full history of the PR head with no persisted
  credentials, so the `BASE_SHA..HEAD_SHA` range is resolvable and no token
  is left in the checkout's git config.
- Exactly one step must invoke the pinned official
  `ghcr.io/gitleaks/gitleaks:v8.30.1` container over the PR non-merge commit
  range with redaction and an explicit failing exit code. The step must not
  mask the scanner's native status (`continue-on-error`, `|| true`), pass a
  `GITHUB_TOKEN` or `GITLEAKS_LICENSE` into the container, or scan full
  history.
- README.md and docs/security/secret-prevention.md must document the same
  version, PR-range scope, and redaction boundary.

Style follows tests/test_ci_shellcheck.py and tests/test_secret_prevention.py:
checked-in YAML/document invariants asserted with pathlib + PyYAML, no new
dependencies. The real scan is performed by CI, not by this Python test
(local machines may not have Docker). No complete synthetic secret is
embedded here; tests/test_secret_prevention.py owns the staged-secret
integration fixture.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"
RUNBOOK = ROOT / "docs" / "security" / "secret-prevention.md"

JOB_ID = "secret-scan"
JOB_NAME = "Secret scan"
IMAGE = "ghcr.io/gitleaks/gitleaks:v8.30.1"
SCAN_COMMAND = "git --redact --no-banner --no-color --verbose --exit-code 1"
LOG_OPTS = '--log-opts="--no-merges ${BASE_SHA}..${HEAD_SHA}"'
BASE_SHA_EXPR = "${{ github.event.pull_request.base.sha }}"
HEAD_SHA_EXPR = "${{ github.event.pull_request.head.sha }}"
# Exact sentence the README must contain (mirrors test_ci_shellcheck.py).
CI_RUNS_STATEMENT = (
    "CI checks the PR's new commit range with the same pinned Gitleaks v8.30.1"
)


def _scanner_steps(cfg: dict) -> list[dict]:
    steps = cfg["jobs"][JOB_ID]["steps"]
    return [
        step
        for step in steps
        if isinstance(step, dict) and IMAGE in step.get("run", "")
    ]


class CiSecretScanningTests(unittest.TestCase):
    def test_secret_scan_job_is_pr_only_and_read_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text)
        # PyYAML treats the YAML 1.1 `on:` key as boolean True; accept both
        # spellings so the trigger assertion is never accidentally vacuous.
        self.assertIn("pull_request:", text)
        self.assertNotIn("pull_request_target", text)
        triggers = cfg.get("on") or cfg.get(True)
        self.assertIsNotNone(triggers, "workflow triggers must parse")
        self.assertIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)

        job = cfg["jobs"][JOB_ID]
        self.assertEqual(job.get("name"), JOB_NAME)
        self.assertEqual(job.get("runs-on"), "ubuntu-latest")
        self.assertEqual(job.get("if"), "github.event_name == 'pull_request'")
        self.assertEqual(job.get("permissions"), {"contents": "read"})

    def test_checkout_is_full_history_head_ref_and_credential_free(self) -> None:
        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = cfg["jobs"][JOB_ID]["steps"]
        checkout = steps[0]
        self.assertEqual(checkout.get("uses"), "actions/checkout@v4")
        with_opts = checkout.get("with", {})
        self.assertEqual(with_opts.get("fetch-depth"), 0)
        self.assertEqual(with_opts.get("ref"), HEAD_SHA_EXPR)
        self.assertFalse(with_opts.get("persist-credentials", True))

    def test_scanner_step_pins_image_scope_and_redaction(self) -> None:
        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        matches = _scanner_steps(cfg)
        self.assertEqual(
            len(matches),
            1,
            "secret-scan job must contain exactly one Gitleaks scanner step",
        )
        step = matches[0]
        run = step["run"]
        self.assertEqual(step.get("name"), JOB_NAME)
        for fragment in (
            SCAN_COMMAND,
            LOG_OPTS,
            '--volume "$GITHUB_WORKSPACE:/repo:ro"',
            "/repo",
        ):
            self.assertIn(fragment, run)
        guard_lines = {line.strip() for line in run.splitlines()}
        self.assertIn('test -n "$BASE_SHA"', guard_lines)
        self.assertIn('test -n "$HEAD_SHA"', guard_lines)
        self.assertNotIn('test -n "$BASE_SHA" && test -n "$HEAD_SHA"', run)
        # The gate must not scan full repository history (legacy findings
        # are the audit/scrub controls' responsibility, not this PR gate).
        self.assertNotIn("--all", run)
        # Native failure status must be preserved: findings and scanner
        # errors are failures, never masked.
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("|| true", run)
        self.assertNotIn("||true", run)
        # Container environment: only the PR SHAs; no token or license.
        env = step.get("env", {})
        self.assertEqual(env.get("BASE_SHA"), BASE_SHA_EXPR)
        self.assertEqual(env.get("HEAD_SHA"), HEAD_SHA_EXPR)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GITLEAKS_LICENSE", env)

    def test_sha_guard_fails_closed_before_scanner(self) -> None:
        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        run = _scanner_steps(cfg)[0]["run"]
        guard = run.split("docker run", maxsplit=1)[0]

        for base_sha, head_sha in (("", "deadbeef"), ("deadbeef", "")):
            with self.subTest(base_sha=base_sha, head_sha=head_sha):
                result = subprocess.run(
                    ["bash", "-e", "-c", f"{guard}\necho DOCKER_REACHED"],
                    env={
                        **os.environ,
                        "BASE_SHA": base_sha,
                        "HEAD_SHA": head_sha,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("DOCKER_REACHED", result.stdout)

        result = subprocess.run(
            ["bash", "-e", "-c", f"{guard}\necho DOCKER_REACHED"],
            env={
                **os.environ,
                "BASE_SHA": "base-sha",
                "HEAD_SHA": "head-sha",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("DOCKER_REACHED", result.stdout)

    @unittest.skipUnless(
        os.environ.get("CI") == "true" and shutil.which("docker"),
        "Docker Gitleaks integration runs in CI only",
    )
    def test_pinned_scanner_detects_a_secret_in_the_requested_range(self) -> None:
        """Exercise the exact container contract against a temporary repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "CI test",
                "GIT_AUTHOR_EMAIL": "ci@example.invalid",
                "GIT_COMMITTER_NAME": "CI test",
                "GIT_COMMITTER_EMAIL": "ci@example.invalid",
            }

            def commit(name: str, content: str) -> str:
                (repo / name).write_text(content, encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", name], check=True)
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-qm", name],
                    check=True,
                    env=git_env,
                )
                return subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    text=True,
                ).strip()

            base_sha = commit("clean.txt", "clean\n")
            generated_token = "github_pat_" + secrets.token_hex(24)
            head_sha = commit("candidate.txt", generated_token + "\n")
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--volume",
                    f"{repo}:/repo:ro",
                    IMAGE,
                    "git",
                    "--redact",
                    "--no-banner",
                    "--no-color",
                    "--verbose",
                    "--exit-code",
                    "1",
                    f"--log-opts=--no-merges {base_sha}..{head_sha}",
                    "/repo",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(generated_token, result.stdout + result.stderr)

    def test_workflow_has_no_extra_scan_integrations(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        # The rejected Gitleaks Action (API token, comments, SARIF upload)
        # and any artifact/comment integration must stay out of the workflow.
        self.assertNotIn("gitleaks-action", text)
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("sarif", text)

    def test_docs_document_ci_gate_version_and_scope(self) -> None:
        readme = README.read_text(encoding="utf-8")
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(
            CI_RUNS_STATEMENT,
            readme,
            "README.md must state that CI scans the PR commit range with the pinned image",
        )
        for doc, name in (
            (readme, "README.md"),
            (runbook, "docs/security/secret-prevention.md"),
        ):
            self.assertIn("v8.30.1", doc, f"{name} must pin Gitleaks v8.30.1")
            self.assertIn("commit range", doc, f"{name} must document the PR-range scope")
            self.assertIn("redact", doc, f"{name} must document redacted output")


if __name__ == "__main__":
    unittest.main()
