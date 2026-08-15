"""CI secret-scanning contract tests.

Guards the `secret-scan` gate in `.github/workflows/ci.yml` against drift:

- The job must exist, run only for `pull_request` events targeting `develop`
  and `main`, and hold only `contents: read`.
- Checkout must fetch full history of the PR head with no persisted
  credentials, so the `BASE_SHA..HEAD_SHA` range is resolvable and no token
  is left in the checkout's git config.
- The scanner must use a base-owned policy, remove PR policy files from its
  target, ignore inline `gitleaks:allow` comments, include merge-resolution
  patches, and preserve native scanner failures.
- README.md and docs/security/secret-prevention.md must document the same
  version, merge-aware PR-range scope, and redaction boundary.
- When Docker is available, the exact workflow shell invokes the pinned
  scanner against clean, finding, and merge-resolution fixtures. No complete
  synthetic secret is embedded here; all detector fixtures are assembled at
  runtime.

Style follows tests/test_ci_shellcheck.py and tests/test_secret_prevention.py:
checked-in YAML/document invariants are asserted with pathlib + PyYAML, and
Docker-backed behavior uses isolated temporary Git repositories.
"""

from __future__ import annotations

import os
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
SCAN_COMMAND = "--redact --no-banner --no-color --verbose --exit-code 1"
LOG_OPTS = '--log-opts="--diff-merges=first-parent ${BASE_SHA}..${HEAD_SHA}"'
BASE_SHA_EXPR = "${{ github.event.pull_request.base.sha }}"
HEAD_SHA_EXPR = "${{ github.event.pull_request.head.sha }}"
PREPARE_NAME = "Prepare trusted scanner inputs"
# Deterministic AWS access key assembled at runtime; never store the complete
# detector match in this tracked test file.
_TOKEN = "AKIA" + "ABCDEFGHIJKLMNOP"
# Exact sentence the README must contain (mirrors test_ci_shellcheck.py).
CI_RUNS_STATEMENT = (
    "CI checks the PR's new commit range with the same pinned Gitleaks v8.30.1"
)


def _job_steps(cfg: dict) -> list[dict]:
    return cfg["jobs"][JOB_ID]["steps"]


def _scanner_steps(cfg: dict) -> list[dict]:
    return [
        step
        for step in _job_steps(cfg)
        if isinstance(step, dict) and IMAGE in step.get("run", "")
    ]


def _workflow_script(cfg: dict) -> str:
    prepare = next(step for step in _job_steps(cfg) if step.get("name") == PREPARE_NAME)
    scan = next(step for step in _job_steps(cfg) if step.get("name") == JOB_NAME)
    return f"{prepare['run']}\n{scan['run']}"


def _safe_output(text: str) -> str:
    return text.replace(_TOKEN, "REDACTED")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {_safe_output(result.stderr.strip())}"
        )
    return result


def _init_fixture(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Secret Scan Test")
    _git(repo, "config", "user.email", "secret-scan@example.test")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _simple_fixture(repo: Path) -> tuple[str, str]:
    _init_fixture(repo)
    (repo / "README.txt").write_text("safe base\n", encoding="utf-8")
    base = _commit(repo, "safe base")
    # These PR-owned policy files and the inline allow comment must not be
    # able to suppress the finding in the scan target.
    (repo / ".gitleaks.toml").write_text(
        "[extend]\nuseDefault = true\n[[allowlists]]\npaths = ['.*']\n",
        encoding="utf-8",
    )
    (repo / ".gitleaksignore").write_text("notes.txt\n", encoding="utf-8")
    (repo / "notes.txt").write_text(
        f"temporary access key: {_TOKEN} # gitleaks:allow\n", encoding="utf-8"
    )
    head = _commit(repo, "secret in PR")
    return base, head


def _merge_fixture(repo: Path) -> tuple[str, str]:
    _init_fixture(repo)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "merge base")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "shared.txt").write_text("feature\n", encoding="utf-8")
    _commit(repo, "feature change")
    _git(repo, "checkout", "-qb", "upstream", base)
    (repo / "shared.txt").write_text("upstream\n", encoding="utf-8")
    _commit(repo, "upstream change")
    _git(repo, "checkout", "feature")
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "upstream", "--no-edit"],
        capture_output=True,
        text=True,
    )
    if merge.returncode == 0:
        raise AssertionError("merge fixture unexpectedly had no conflict")
    (repo / "shared.txt").write_text(
        f"resolved during merge: {_TOKEN}\n", encoding="utf-8"
    )
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-qm", "resolve merge with secret")
    return base, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_workflow_scan(
    script: str, repo: Path, runner_temp: Path, base: str, head: str
) -> subprocess.CompletedProcess[str]:
    runner_temp.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=repo,
        env={
            **os.environ,
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "GITHUB_WORKSPACE": str(repo),
            "RUNNER_TEMP": str(runner_temp),
        },
        capture_output=True,
        text=True,
        timeout=600,
    )


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
        pull_request = triggers["pull_request"]
        self.assertEqual(set(pull_request["branches"]), {"develop", "main"})

        job = cfg["jobs"][JOB_ID]
        self.assertEqual(job.get("name"), JOB_NAME)
        self.assertEqual(job.get("runs-on"), "ubuntu-latest")
        self.assertEqual(job.get("if"), "github.event_name == 'pull_request'")
        self.assertEqual(job.get("permissions"), {"contents": "read"})

    def test_prepare_step_uses_base_owned_policy_and_sanitized_checkout(self) -> None:
        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        prepare = next(
            step for step in _job_steps(cfg) if step.get("name") == PREPARE_NAME
        )
        run = prepare["run"]
        for fragment in (
            'git show "${BASE_SHA}:.gitleaks.toml"',
            'git show "${BASE_SHA}:.gitleaksignore"',
            'rm -f "$scan_root/.gitleaks.toml" "$scan_root/.gitleaksignore"',
            "useDefault = true",
            '::error::Secret scan requires github.event.pull_request.base.sha',
        ):
            self.assertIn(fragment, run)
        self.assertIn("trusted-gitleaks", run)
        self.assertIn("gitleaks-repo", run)

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
            '--volume "$RUNNER_TEMP/gitleaks-repo:/repo:ro"',
            '--volume "$RUNNER_TEMP/trusted-gitleaks:/trusted:ro"',
            "git --config /trusted/gitleaks.toml",
            "--gitleaks-ignore-path /trusted/.gitleaksignore",
            "--ignore-gitleaks-allow",
            "/repo",
        ):
            self.assertIn(fragment, run)
        self.assertIn('if [[ -z "${BASE_SHA:-}" ]]; then', run)
        self.assertIn('if [[ -z "${HEAD_SHA:-}" ]]; then', run)
        self.assertIn("::error::Secret scan requires github.event.pull_request.base.sha", run)
        self.assertIn("::error::Secret scan requires github.event.pull_request.head.sha", run)
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

    def test_real_scanner_detects_secrets_and_merge_resolution(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("Docker is required for the scanner integration test")
        daemon = subprocess.run(
            [docker, "info"], capture_output=True, text=True, timeout=30
        )
        if daemon.returncode != 0:
            self.skipTest("Docker daemon is unavailable for the scanner integration test")

        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        script = _workflow_script(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            simple_repo = root / "simple-repo"
            base, head = _simple_fixture(simple_repo)
            clean = _run_workflow_scan(
                script, simple_repo, root / "clean-runner", base, base
            )
            self.assertEqual(clean.returncode, 0, _safe_output(clean.stderr))

            finding = _run_workflow_scan(
                script, simple_repo, root / "finding-runner", base, head
            )
            finding_output = finding.stdout + finding.stderr
            self.assertNotEqual(finding.returncode, 0, _safe_output(finding_output))
            self.assertIn("REDACTED", finding_output, _safe_output(finding_output))
            self.assertNotIn(_TOKEN, finding_output, _safe_output(finding_output))

            merge_repo = root / "merge-repo"
            merge_base, merge_head = _merge_fixture(merge_repo)
            merge_finding = _run_workflow_scan(
                script, merge_repo, root / "merge-runner", merge_base, merge_head
            )
            merge_output = merge_finding.stdout + merge_finding.stderr
            self.assertNotEqual(
                merge_finding.returncode, 0, _safe_output(merge_output)
            )
            self.assertIn("REDACTED", merge_output, _safe_output(merge_output))
            self.assertNotIn(_TOKEN, merge_output, _safe_output(merge_output))

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
            self.assertIn(
                "diff-merges=first-parent",
                doc,
                f"{name} must document merge-aware scanning",
            )
            self.assertIn(
                "ignore-gitleaks-allow",
                doc,
                f"{name} must document allow-comment handling",
            )
            self.assertIn("redact", doc, f"{name} must document redacted output")


if __name__ == "__main__":
    unittest.main()
