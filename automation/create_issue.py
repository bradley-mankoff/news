#!/usr/bin/env python3
"""Create a GitHub issue and land it on the board in one step.

Replaces the manual 3-step (gh issue create + gh project item-add + lane
move): the new issue is created, added to the project, and moved to the
default lane (Backlog) immediately.

Usage:
  python3 automation/create_issue.py "<title>" --body "<shaped markdown>"
      [--label <name> | --decision] [--lane <lane>]

The body must include What and why, binary acceptance criteria, Out of
scope, Ownership, and Depends on sections. Without --label, `[Bug]` titles
use `bug`; other titles use `enhancement`.

Lane names come from automation/config.json ("lanes" keys). Default lane:
the config's default_lane (Backlog).
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ADD_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""

MOVE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""

FIELD_QUERY = """
query($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""


def gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=120)


REQUIRED_BODY_SECTIONS = (
    "What and why",
    "Acceptance criteria",
    "Out of scope",
    "Ownership",
    "Depends on",
)
SECTION_RE = re.compile(
    r"(?ims)^##\s+{heading}\s*$\n(.*?)(?=^##\s+|\Z)"
)
PLACEHOLDER_RE = re.compile(r"acceptance criteria to be filled", re.I)


def issue_section(body: str, heading: str) -> str:
    """Return the content of one required level-2 issue section."""
    pattern = SECTION_RE.pattern.format(heading=re.escape(heading))
    match = re.search(pattern, body or "")
    return match.group(1).strip() if match else ""


def validate_issue_body(body: str) -> list[str]:
    """Return errors for an issue body that is not ready for the board."""
    errors: list[str] = []
    if not body or not body.strip():
        return ["--body is required; shape the issue before creating it."]
    for heading in REQUIRED_BODY_SECTIONS:
        content = issue_section(body, heading)
        if not content:
            errors.append(f"missing `## {heading}` section")
    acceptance = issue_section(body, "Acceptance criteria")
    if acceptance and not any(
        line.lstrip().startswith(("- ", "* "))
        for line in acceptance.splitlines()
    ):
        errors.append("Acceptance criteria must contain at least one bullet")
    ownership = issue_section(body, "Ownership").lower()
    if "declare before moving" in ownership or ownership in {"tbd", "todo"}:
        errors.append("Ownership must name concrete files or components")
    dependencies = " ".join(issue_section(body, "Depends on").split())
    if dependencies and not re.fullmatch(
        r"(?:None\.?|Depends on:\s*#\d+(?:\s*,\s*#\d+)*)",
        dependencies,
        re.I,
    ):
        errors.append("Depends on must be `None.` or comma-separated issue refs")
    if PLACEHOLDER_RE.search(body):
        errors.append("replace the acceptance-criteria placeholder with binary criteria")
    return errors


def default_label_for_title(title: str) -> str:
    """Route bug titles to the fix workflow and other issues to idea-to-pr."""
    return "bug" if re.match(r"^\s*\[bug\](?:\s|:|$)", title, re.I) else "enhancement"


def main() -> int:
    ap = argparse.ArgumentParser(description="Create an issue on the board.")
    ap.add_argument("title")
    ap.add_argument("--body", default=None, help="shaped issue body (markdown)")
    routing = ap.add_mutually_exclusive_group()
    routing.add_argument("--label", default=None, help="repo label (must exist)")
    routing.add_argument(
        "--decision",
        action="store_true",
        help="mark as decision-only; never dispatch an implementation workflow",
    )
    ap.add_argument("--lane", default=None, help="board lane (default: config default_lane)")
    args = ap.parse_args()

    if args.body is None:
        print(
            "error: --body is required; create the issue only after shaping "
            "its What and why, acceptance criteria, scope, ownership, and "
            "dependency sections.",
            file=sys.stderr,
        )
        return 2
    errors = validate_issue_body(args.body)
    if errors:
        print("error: issue body is not ready:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    cfg = json.loads((ROOT / "automation" / "config.json").read_text())
    lane = args.lane or cfg.get("default_lane", "Backlog")
    body = args.body
    decision_label = str(
        (cfg.get("decision_only") or {}).get("label", "decision-only"))
    label = decision_label if args.decision else (
        args.label or default_label_for_title(args.title))
    if args.decision:
        ensured = gh([
            "label", "create", decision_label, "-R", cfg["repo"],
            "--color", "5319e7",
            "--description", "Owner decision; no implementation workflow",
            "--force",
        ])
        if ensured.returncode != 0:
            print(f"error: cannot ensure decision label: "
                  f"{ensured.stderr.strip()[:300]}", file=sys.stderr)
            return 1

    cmd = ["issue", "create", "-R", cfg["repo"], "--title", args.title,
           "--body", body]
    if label:
        cmd += ["--label", label]
    r = gh(cmd)
    if r.returncode != 0:
        print(f"error: {r.stderr.strip()[:500]}", file=sys.stderr)
        return 1
    url = r.stdout.strip()
    m = re.search(r"issues/(\d+)$", url)
    if not m:
        print(f"error: cannot parse issue number from: {url!r}", file=sys.stderr)
        return 1
    issue_number = int(m.group(1))

    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
            "--json", "id"])
    if r.returncode != 0:
        print(f"error: cannot read issue id: {r.stderr.strip()[:300]}",
              file=sys.stderr)
        return 1
    content_id = json.loads(r.stdout)["id"]

    r = gh(["api", "graphql", "-f", f"query={FIELD_QUERY}",
            "-F", f"login={cfg['project_owner']}",
            "-F", f"number={cfg['project_number']}"])
    if r.returncode != 0:
        print(f"error: {r.stderr.strip()[:500]}", file=sys.stderr)
        return 1
    project = json.loads(r.stdout)["data"]["user"]["projectV2"]
    project_id = project["id"]
    field = next((f for f in project["fields"]["nodes"]
                  if f.get("name") == cfg["status_field"]), None)
    if field is None:
        print(f"error: status field '{cfg['status_field']}' not found",
              file=sys.stderr)
        return 1
    option = next((o for o in field["options"] if o["name"] == lane), None)
    if option is None:
        print(f"error: lane '{lane}' not found; options: "
              f"{[o['name'] for o in field['options']]}", file=sys.stderr)
        return 1

    r = gh(["api", "graphql", "-f", f"query={ADD_MUTATION}",
            "-F", f"projectId={project_id}", "-F", f"contentId={content_id}"])
    if r.returncode != 0:
        print(f"error: board add failed: {r.stderr.strip()[:300]}",
              file=sys.stderr)
        return 1
    item_id = json.loads(r.stdout)["data"]["addProjectV2ItemById"]["item"]["id"]

    r = gh(["api", "graphql", "-f", f"query={MOVE_MUTATION}",
            "-F", f"projectId={project_id}", "-F", f"itemId={item_id}",
            "-F", f"fieldId={field['id']}", "-F", f"optionId={option['id']}"])
    if r.returncode != 0:
        print(f"error: lane move failed: {r.stderr.strip()[:300]}",
              file=sys.stderr)
        return 1
    print(f"created #{issue_number} -> {lane}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
