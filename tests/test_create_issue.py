from __future__ import annotations

import json
import subprocess
import sys
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

from automation import create_issue


VALID_BODY = """## What and why

Ship a shaped feature.

## Acceptance criteria

- [ ] The feature produces the documented result.

## Out of scope

- Unrelated behavior.

## Ownership

Files/areas: news_pipeline/example.py and tests/test_example.py.

## Depends on

None.
"""


class CreateIssueTests(unittest.TestCase):
    def test_validate_issue_body_accepts_shaped_feature(self) -> None:
        self.assertEqual(create_issue.validate_issue_body(VALID_BODY), [])

    def test_validate_issue_body_rejects_placeholders_and_missing_sections(self) -> None:
        body = VALID_BODY.replace("## Ownership\n\nFiles/areas: news_pipeline/example.py and tests/test_example.py.\n", "")
        body = body.replace(
            "- [ ] The feature produces the documented result.",
            "Acceptance criteria to be filled when planned.",
        )
        errors = create_issue.validate_issue_body(body)
        self.assertIn("missing `## Ownership` section", errors)
        self.assertIn("replace the acceptance-criteria placeholder with binary criteria", errors)

    def test_main_requires_body_before_calling_github(self) -> None:
        with (
            patch.object(sys, "argv", ["create_issue.py", "A feature"]),
            patch("automation.create_issue.gh") as gh,
        ):
            self.assertEqual(create_issue.main(), 2)
        gh.assert_not_called()

    def test_default_label_routes_bug_titles_to_fix_workflow(self) -> None:
        self.assertEqual(create_issue.default_label_for_title("[Bug]: broken"), "bug")
        self.assertEqual(create_issue.default_label_for_title("[Feature]: useful"), "enhancement")
        self.assertEqual(create_issue.default_label_for_title("New capability"), "enhancement")

    def test_decision_option_ensures_and_applies_non_dispatch_label(self) -> None:
        calls: list[list[str]] = []

        def fake_gh(args: list[str]) -> subprocess.CompletedProcess:
            calls.append(args)
            if args[:2] == ["label", "create"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["issue", "create"]:
                return subprocess.CompletedProcess(
                    args, 0, "https://github.com/example/widgets/issues/12\n", "")
            if args[:2] == ["issue", "view"]:
                return subprocess.CompletedProcess(args, 0, '{"id":"issue-id"}', "")
            query = next((part for part in args if part.startswith("query=")), "")
            if "projectV2(number" in query:
                payload = {
                    "data": {
                        "user": {
                            "projectV2": {
                                "id": "project-id",
                                "fields": {
                                    "nodes": [{
                                        "id": "field-id",
                                        "name": "Status",
                                        "options": [{"id": "backlog-id", "name": "Backlog"}],
                                    }]
                                },
                            }
                        }
                    }
                }
            elif "addProjectV2ItemById" in query:
                payload = {
                    "data": {
                        "addProjectV2ItemById": {
                            "item": {"id": "item-id"}
                        }
                    }
                }
            else:
                payload = {"data": {"updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "item-id"}
                }}}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "automation").mkdir()
            (root / "automation" / "config.json").write_text(json.dumps({
                "repo": "example/widgets",
                "project_owner": "example",
                "project_number": 1,
                "status_field": "Status",
                "default_lane": "Backlog",
                "decision_only": {"label": "decision-only"},
            }))
            with (
                patch.object(create_issue, "ROOT", root),
                patch.object(create_issue, "gh", side_effect=fake_gh),
                patch.object(
                    sys,
                    "argv",
                    [
                        "create_issue.py",
                        "Choose the public name",
                        "--body",
                        VALID_BODY,
                        "--decision",
                    ],
                ),
            ):
                self.assertEqual(create_issue.main(), 0)

        self.assertEqual(calls[0][:3], ["label", "create", "decision-only"])
        create = next(args for args in calls if args[:2] == ["issue", "create"])
        self.assertEqual(create[create.index("--label") + 1], "decision-only")


if __name__ == "__main__":
    unittest.main()
