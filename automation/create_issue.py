#!/usr/bin/env python3
"""Create a GitHub issue and land it on the board in one step.

Replaces the manual 3-step (gh issue create + gh project item-add + lane
move): the new issue is created, added to the project, and moved to the
default lane (Backlog) immediately.

Usage:
  python3 automation/create_issue.py "<title>" [--body "<text>"]
      [--label <name>] [--lane <lane>]

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Create an issue on the board.")
    ap.add_argument("title")
    ap.add_argument("--body", default="", help="issue body (markdown)")
    ap.add_argument("--label", default=None, help="repo label (must exist)")
    ap.add_argument("--lane", default=None, help="board lane (default: config default_lane)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "automation" / "config.json").read_text())
    lane = args.lane or cfg.get("default_lane", "Backlog")

    body = args.body or (
        f"**What and why:** {args.title}\n\n"
        "## Ownership\n\n"
        "Files/areas: declare before moving this issue to Todo.\n\n"
        "## Depends on\n\n"
        "None.\n\n"
        "Acceptance criteria to be filled when planned."
    )
    cmd = ["issue", "create", "-R", cfg["repo"], "--title", args.title,
           "--body", body]
    if args.label:
        cmd += ["--label", args.label]
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
