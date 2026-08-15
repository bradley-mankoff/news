"""CI pytest workflow contract tests.

Guards the `test` job in `.github/workflows/ci.yml` against drift:

- The workflow is named `CI` and runs the repository pytest suite for pull
  requests targeting `develop` or `main` and for pushes to those branches
  (exactly those two events, exactly those two branch filters).
- The job uses the declared uv-managed development environment on
  `ubuntu-latest`: checkout, pinned `astral-sh/setup-uv@v5` with cache
  enabled, `uv sync --group dev`, and the final direct command
  `uv run python -m pytest -q`.
- The final pytest step must fail natively: no `continue-on-error`, no
  status-swallowing shell wrapper (`|| true`, `set +e`), and nothing may
  run after it in the job. A failing pytest process must fail the check.
- README.md must document the same trigger scope and the exact parity
  commands, including the reliable local fallback.

Style follows tests/test_ci_shellcheck.py: checked-in YAML/document
invariants asserted with pathlib + PyYAML, no new dependencies. Native
failure propagation is exercised with a real subprocess run of the exact
workflow command against a deliberately failing test file (mirrors
tests/test_scrub_history.py's return-code assertions).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"

WORKFLOW_NAME = "CI"
JOB_ID = "test"
RUNS_ON = "ubuntu-latest"
CHECKOUT_ACTION = "actions/checkout@v4"
SETUP_UV_ACTION = "astral-sh/setup-uv@v5"
SETUP_UV_WITH = {"enable-cache": True}
INSTALL_STEP_NAME = "Install dependencies"
RUN_STEP_NAME = "Run tests"
SYNC_COMMAND = "uv sync --group dev"
PYTEST_COMMAND = "uv run python -m pytest -q"
TARGET_BRANCHES = ("develop", "main")
FALLBACK_COMMAND = ".venv/bin/python3 -m pytest tests/ -q"
# Exact two-line fenced block the README must contain (parity commands).
PARITY_BLOCK = f"{SYNC_COMMAND}\n{PYTEST_COMMAND}"


def _load_workflow() -> tuple[str, dict]:
    text = WORKFLOW.read_text(encoding="utf-8")
    # PyYAML treats the YAML 1.1 `on:` key as boolean True; keep the raw
    # text for the trigger spelling assertions and parse with safe_load
    # for structure (same approach as tests/test_ci_secret_scanning.py).
    return text, yaml.safe_load(text)


def _test_job_steps(cfg: dict) -> list[dict]:
    steps = cfg["jobs"][JOB_ID]["steps"]
    return [step for step in steps if isinstance(step, dict)]


class CiWorkflowTests(unittest.TestCase):
    def test_workflow_name_and_triggers_target_only_develop_and_main(self) -> None:
        text, cfg = _load_workflow()
        self.assertEqual(cfg.get("name"), WORKFLOW_NAME)
        # YAML 1.1 parses the `on:` key as boolean True; accept both.
        triggers = cfg.get("on") or cfg.get(True)
        self.assertIsNotNone(triggers, "workflow triggers must parse")
        self.assertEqual(
            set(triggers),
            {"pull_request", "push"},
            "workflow must trigger only on pull_request and push",
        )
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                branches = tuple(triggers[event]["branches"])
                self.assertEqual(
                    branches,
                    TARGET_BRANCHES,
                    f"{event} trigger must target only {TARGET_BRANCHES}",
                )
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)

    def test_test_job_uses_uv_env_and_exact_install_and_pytest_steps(self) -> None:
        text, cfg = _load_workflow()
        self.assertNotIn("pull_request_target", text)
        job = cfg["jobs"][JOB_ID]
        self.assertEqual(job.get("runs-on"), RUNS_ON)
        steps = _test_job_steps(cfg)
        # Checkout must come first, followed by the pinned uv setup with
        # caching enabled (other pre-existing steps may sit between them).
        self.assertEqual(steps[0].get("uses"), CHECKOUT_ACTION)
        setup_uv = next(
            step for step in steps if step.get("uses") == SETUP_UV_ACTION
        )
        self.assertEqual(setup_uv.get("with"), SETUP_UV_WITH)

        install = next(
            step for step in steps if step.get("run") == SYNC_COMMAND
        )
        run_tests = next(
            step for step in steps if step.get("run") == PYTEST_COMMAND
        )
        self.assertEqual(install.get("name"), INSTALL_STEP_NAME)
        self.assertEqual(run_tests.get("name"), RUN_STEP_NAME)
        self.assertLess(steps.index(install), steps.index(run_tests))
        # The pytest run must be the job's final step: nothing may run
        # after it that could rewrite or mask the native exit status.
        self.assertIs(steps[-1], run_tests)

    def test_run_tests_step_does_not_mask_native_failure_status(self) -> None:
        _, cfg = _load_workflow()
        run_tests = next(
            step
            for step in _test_job_steps(cfg)
            if step.get("run") == PYTEST_COMMAND
        )
        # Exactly the direct command: no `|| true`, no `set +e`, no shell
        # wrapper, and no option that swallows pytest's exit status.
        self.assertEqual(run_tests["run"], PYTEST_COMMAND)
        self.assertNotIn("continue-on-error", run_tests)
        self.assertNotIn("shell", run_tests)
        self.assertNotIn("if", run_tests)
        self.assertFalse(run_tests.get("with", {}))

    def test_pytest_command_fails_natively_on_failing_suite(self) -> None:
        # The job's failure status depends on pytest's native exit code:
        # the exact workflow command must exit non-zero for a failing test
        # (mirrors tests/test_scrub_history.py's subprocess assertions).
        with tempfile.TemporaryDirectory() as tmp:
            failing_test = Path(tmp) / "test_failing.py"
            failing_test.write_text(
                "def test_failing():\n    assert False\n", encoding="utf-8"
            )
            result = subprocess.run(
                ["uv", "run", "python", "-m", "pytest", "-q", str(failing_test)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(
            result.returncode, 0, result.stdout + result.stderr
        )

    def test_readme_documents_trigger_scope_and_parity_commands(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### Tests and CI", readme)
        self.assertIn(
            "pull requests targeting\n`develop` or `main`",
            readme,
            "README.md must document the pull-request trigger scope",
        )
        self.assertIn(
            "pushes to those branches",
            readme,
            "README.md must document the push trigger scope",
        )
        self.assertIn(
            PARITY_BLOCK,
            readme,
            "README.md must document the exact parity commands as a fenced block",
        )
        self.assertIn(
            FALLBACK_COMMAND,
            readme,
            "README.md must keep the reliable local fallback command",
        )


if __name__ == "__main__":
    unittest.main()
