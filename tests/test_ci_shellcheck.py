"""CI ShellCheck contract tests.

Guards the ShellCheck gate in `.github/workflows/ci.yml` against drift:

- The `test` job must contain a step whose exact `run` value is
  `shellcheck automation/*.sh`; a comment or unrelated mention of
  "shellcheck" does not satisfy the contract.
- README.md must document the identical command and state that CI runs it.
- The maintained script inputs covered by `automation/*.sh` are the two
  tracked scripts: `automation/deploy.sh` and `automation/scrub_history.sh`.

Style follows tests/test_secret_prevention.py: checked-in YAML/document
invariants asserted with pathlib + PyYAML, no new dependencies. The real
lint is performed by CI, not by this Python test (local machines may not
have the native ShellCheck binary).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"

SHELLCHECK_CMD = "shellcheck automation/*.sh"
SHELLCHECK_STEP_NAME = "ShellCheck automation scripts"
CI_RUNS_STATEMENT = "CI checks the maintained shell scripts"
MAINTAINED_SCRIPTS = (
    "automation/deploy.sh",
    "automation/scrub_history.sh",
)


class CiShellCheckTests(unittest.TestCase):
    def test_test_job_runs_exact_shellcheck_command(self) -> None:
        cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = cfg["jobs"]["test"]["steps"]
        matches = [
            step
            for step in steps
            if isinstance(step, dict) and step.get("run") == SHELLCHECK_CMD
        ]
        self.assertEqual(
            len(matches),
            1,
            f"test job must contain exactly one step with run {SHELLCHECK_CMD!r}",
        )
        # The step must be the named ShellCheck gate, and must not mask the
        # native failure status (no continue-on-error, no status-swallowing).
        self.assertEqual(matches[0].get("name"), SHELLCHECK_STEP_NAME)
        self.assertNotIn("continue-on-error", matches[0])

    def test_readme_documents_shellcheck_command_and_ci_statement(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            SHELLCHECK_CMD,
            readme,
            f"README.md must document the exact command {SHELLCHECK_CMD!r}",
        )
        self.assertIn(
            CI_RUNS_STATEMENT,
            readme,
            "README.md must state that CI runs the ShellCheck check",
        )

    def test_maintained_scripts_are_current_shellcheck_inputs(self) -> None:
        for rel in MAINTAINED_SCRIPTS:
            self.assertTrue(
                (ROOT / rel).is_file(),
                f"maintained ShellCheck input must exist: {rel}",
            )


if __name__ == "__main__":
    unittest.main()
