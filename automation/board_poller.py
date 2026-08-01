#!/usr/bin/env python3
"""Board poller for the Daily News GitHub project.

Watches the project board and dispatches Archon workflows when items move
between lanes. Contract:

- New issues land in the FIRST lane (Backlog). Work never starts on creation —
  it starts only when an item is MOVED INTO the todo lane (e.g. "Todo").
- The first poll after (re)start is a snapshot: state is recorded, nothing is
  dispatched. This prevents backlog bursts after the poller was down.
- Every real transition into a dispatch lane fires one run: move into "Todo"
  starts implementation, move into "In Review" starts a PR review. Moving back
  out and in again fires again (re-work / re-review).
- Dispatch = `archon workflow run <wf> --branch <branch> --detach "<msg>"`
  executed in the repo root.

Config: automation/config.json (repo, committed).
State:  automation/state.json (gitignored, machine-local).
Log:    stdout; the launchd agent redirects to automation/board_poller.log.

Requires: gh CLI (authenticated), archon CLI on PATH. Python stdlib only.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

QUERY = """
query($login: String!, $number: Int!, $statusField: String!, $cursor: String) {
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
      items(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValueByName(name: $statusField) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue {
              number title url
              repository { nameWithOwner }
              labels(first: 20) { nodes { name } }
            }
            ... on PullRequest {
              number title url
              repository { nameWithOwner }
            }
          }
        }
      }
    }
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
  }) { projectV2Item { id } }
}
"""


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_config() -> dict:
    cfg = json.loads((ROOT / "automation" / "config.json").read_text())
    cfg.setdefault("poll_interval_seconds", 45)
    return cfg


def gh(args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, env=env
    )


def graphql(cfg: dict, env: dict, cursor: str | None) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={QUERY}",
           "-F", f"login={cfg['project_owner']}",
           "-F", f"number={cfg['project_number']}",
           "-F", f"statusField={cfg['status_field']}"]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"graphql failed: {r.stderr.strip()[:500]}")
    return json.loads(r.stdout)


def fetch_project(cfg: dict, env: dict) -> tuple[str, str, dict, list[dict]]:
    """Returns (project_id, status_field_id, status_options: name->id, items)."""
    data = graphql(cfg, env, None)
    project = data["data"]["user"]["projectV2"]
    if project is None:
        raise RuntimeError(
            f"project {cfg['project_number']} not found for owner {cfg['project_owner']}"
        )
    field = next(
        (f for f in project["fields"]["nodes"]
         if f.get("name") == cfg["status_field"]), None)
    if field is None:
        raise RuntimeError(f"status field '{cfg['status_field']}' not found on project")
    options = {o["name"]: o["id"] for o in field["options"]}
    items: list[dict] = []
    page = project["items"]
    items.extend(page["nodes"])
    while page["pageInfo"]["hasNextPage"]:
        data = graphql(cfg, env, page["pageInfo"]["endCursor"])
        page = data["data"]["user"]["projectV2"]["items"]
        items.extend(page["nodes"])
    return project["id"], field["id"], options, items


def find_linked_pr(cfg: dict, env: dict, issue_number: int) -> int | None:
    r = gh(["pr", "list", "-R", cfg["repo"], "--state", "open",
            "--json", "number,title,body"], env)
    if r.returncode != 0:
        return None
    prs = json.loads(r.stdout)
    pat = re.compile(
        rf"\b(?:fix(?:es)?|clos(?:es|e)|resolv(?:es|e))\s+#{issue_number}\b", re.I
    )
    for pr in prs:
        if pat.search(pr.get("body") or ""):
            return pr["number"]
    for pr in prs:
        if f"#{issue_number}" in (pr.get("title") or ""):
            return pr["number"]
    return None


def pick_workflow(cfg: dict, labels: list[str]) -> str:
    todo_cfg = cfg["dispatch"]["todo"]
    for label in labels:
        wf = todo_cfg["label_overrides"].get(label.lower())
        if wf:
            return wf
    return todo_cfg["default"]


def move_to_lane(cfg: dict, env: dict, project_id: str, item_id: str,
                 field_id: str, option_id: str) -> bool:
    r = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={MOVE_MUTATION}",
         "-F", f"projectId={project_id}",
         "-F", f"itemId={item_id}",
         "-F", f"fieldId={field_id}",
         "-F", f"optionId={option_id}"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return r.returncode == 0


def dispatch(cfg: dict, env: dict, wf: str, branch: str, message: str,
             item_id: str, number: int) -> bool:
    """Start an Archon workflow run in a detached child process.

    Deliberately does NOT use `archon ... --detach`: the archon-pi build's
    detached-child spawn is broken (it passes the binary path as the command).
    The child is put in its own session so it survives the poller (and
    launchd restarts of it). Output appends to automation/archon-runs.log.
    Returns True when the process spawned.
    """
    log_path = ROOT / "automation" / "archon-runs.log"
    try:
        with open(log_path, "a") as out:
            proc = subprocess.Popen(
                ["archon", "workflow", "run", wf, "--branch", branch, message],
                cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        log(f"DISPATCH FAILED item={item_id} wf={wf}: {exc}")
        return False
    log(f"DISPATCHED item={item_id} issue={number} wf={wf} branch={branch} pid={proc.pid}")
    return True


def poll(cfg: dict, env: dict, state: dict) -> None:
    project_id, field_id, status_options, items = fetch_project(cfg, env)
    first_run = not state.get("_meta", {}).get("snapshot_done")

    seen: set[str] = set()
    for item in items:
        item_id = item["id"]
        seen.add(item_id)
        content = item.get("content")
        if not content or content["__typename"] not in ("Issue", "PullRequest"):
            continue
        repo = content["repository"]["nameWithOwner"]
        if repo != cfg["repo"]:
            continue
        status_val = (item.get("fieldValueByName") or {}).get("name") or "No status"
        rec = state.get(item_id, {})
        prev = rec.get("status")

        if not first_run and prev != status_val:
            lane = cfg["lanes"].get(status_val)
            if lane == "todo" and content["__typename"] == "Issue":
                labels = [n["name"] for n in content["labels"]["nodes"]]
                wf = pick_workflow(cfg, labels)
                msg = (
                    f"Implement GitHub issue #{content['number']}: {content['title']} "
                    f"({repo}). Full issue: {content['url']}"
                )
                if wf == "archon-idea-to-pr":
                    msg = (
                        f"Build feature from issue #{content['number']}: {content['title']} "
                        f"({repo}). Full issue: {content['url']}"
                    )
                ok = dispatch(cfg, env, wf, f"issue-{content['number']}", msg,
                              item_id, content["number"])
                target = cfg["dispatch"]["todo"].get("move_to")
                if ok and target:
                    option_id = status_options.get(target)
                    if option_id and move_to_lane(
                            cfg, env, project_id, item_id, field_id, option_id):
                        log(f"MOVED item={item_id} issue={content['number']} -> {target}")
                    elif option_id is None:
                        log(f"MOVE SKIPPED item={item_id}: lane '{target}' not on board")
                    else:
                        log(f"MOVE FAILED item={item_id} -> {target}")
            elif lane == "review":
                if content["__typename"] == "PullRequest":
                    pr_number = content["number"]
                    msg = f"Review PR #{pr_number} ({content['title']})"
                    branch = f"review/pr-{pr_number}"
                else:
                    pr_number = find_linked_pr(cfg, env, content["number"])
                    msg = (
                        f"Review the pull request for issue #{content['number']}: "
                        f"{content['title']} ({repo})."
                        + (f" Linked PR: #{pr_number}." if pr_number else "")
                        + " If no PR is linked, find it with: gh pr list --search"
                        f" '#{content['number']}'"
                    )
                    branch = f"review/issue-{content['number']}"
                dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"], branch,
                         msg, item_id, content["number"])

        state[item_id] = {"status": status_val}

    # Prune items that left the board.
    for item_id in list(state):
        if item_id != "_meta" and item_id not in seen:
            del state[item_id]

    if first_run:
        state["_meta"] = {"snapshot_done": True, "project_id": project_id,
                          "snapshot_at": datetime.now(timezone.utc).isoformat()}
        log(f"snapshot taken: {len(seen)} items on board, dispatch armed")
    save_state(cfg, state)


def save_state(cfg: dict, state: dict) -> None:
    path = ROOT / cfg["state_file"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def main() -> int:
    cfg = load_config()
    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()

    state_path = ROOT / cfg["state_file"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    once = "--once" in sys.argv
    while True:
        try:
            poll(cfg, env, state)
        except Exception as exc:  # keep the loop alive on transient failures
            log(f"poll error: {exc}")
        if once:
            return 0
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    sys.exit(main())
