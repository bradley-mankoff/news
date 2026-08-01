#!/usr/bin/env python3
"""Move a GitHub issue to a lane on the project board (project #1, "Build public UI").

Usage: move_item.py <issue-number> <lane>

Lane names come from automation/config.json ("lanes" keys are the status
option names). Resolves project/field/option/item ids via GraphQL and updates
the single-select Status field.

Example: python3 automation/move_item.py 12 "In Review"
"""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
      items(first: 100) {
        nodes {
          id
          content {
            __typename
            ... on Issue { number }
            ... on PullRequest { number }
          }
        }
      }
    }
  }
}
"""

MUTATION = """
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


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    issue_number, lane = sys.argv[1], sys.argv[2]

    cfg = json.loads((ROOT / "automation" / "config.json").read_text())
    r = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={FIELD_QUERY}",
         "-F", f"login={cfg['project_owner']}",
         "-F", f"number={cfg['project_number']}"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"error: {r.stderr.strip()[:500]}", file=sys.stderr)
        return 1
    project = json.loads(r.stdout)["data"]["user"]["projectV2"]

    status_field = next(
        (f for f in project["fields"]["nodes"]
         if f.get("name") == cfg["status_field"]), None)
    if status_field is None:
        print(f"error: field '{cfg['status_field']}' not found on project", file=sys.stderr)
        return 1
    option = next(
        (o for o in status_field["options"] if o["name"] == lane), None)
    if option is None:
        print(f"error: lane '{lane}' not found; options: "
              f"{[o['name'] for o in status_field['options']]}", file=sys.stderr)
        return 1
    item = None
    # Projects v2 reads are eventually consistent: the item may not be
    # visible for a moment after item-add. Retry before giving up.
    for attempt in range(3):
        item = next(
            (i for i in project["items"]["nodes"]
             if i.get("content") and i["content"]["__typename"] == "Issue"
             and str(i["content"]["number"]) == str(issue_number)), None)
        if item is not None:
            break
        time.sleep(2)
        r = subprocess.run(
            ["gh", "api", "graphql",
             "-f", f"query={FIELD_QUERY}",
             "-F", f"login={cfg['project_owner']}",
             "-F", f"number={cfg['project_number']}"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"error: {r.stderr.strip()[:500]}", file=sys.stderr)
            return 1
        project = json.loads(r.stdout)["data"]["user"]["projectV2"]
        status_field = next(
            (f for f in project["fields"]["nodes"]
             if f.get("name") == cfg["status_field"]), None)
        if status_field is not None:
            option = next(
                (o for o in status_field["options"] if o["name"] == lane), None)
    if item is None:
        print(f"error: issue #{issue_number} not on the board", file=sys.stderr)
        return 1

    r = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={MUTATION}",
         "-F", f"projectId={project['id']}",
         "-F", f"itemId={item['id']}",
         "-F", f"fieldId={status_field['id']}",
         "-F", f"optionId={option['id']}"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"error: {r.stderr.strip()[:500]}", file=sys.stderr)
        return 1
    print(f"moved issue #{issue_number} -> {lane}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
