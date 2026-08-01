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


def find_issue_pr(cfg: dict, env: dict, issue_number: int,
                  state: str = "all") -> dict | None:
    """Find the PR linked to an issue (any state) by body/title reference."""
    r = gh(["pr", "list", "-R", cfg["repo"], "--state", state,
            "--json", "number,title,body,headRefName,baseRefName,state"], env)
    if r.returncode != 0:
        return None
    prs = json.loads(r.stdout)
    pat = re.compile(
        rf"(?:\b(?:fix(?:es)?|clos(?:es|e)|resolv(?:es|e))\s+#{issue_number}\b)"
        rf"|(?:\bissue\s*:?\s*#{issue_number}\b)", re.I
    )
    for pr in prs:
        if pat.search(pr.get("body") or ""):
            return pr
    for pr in prs:
        if f"#{issue_number}" in (pr.get("title") or ""):
            return pr
    return None


def merge_pr_to_base(cfg: dict, env: dict, pr: dict, base: str,
                     issue_number: int | None = None) -> tuple[bool, str]:
    """Retarget a PR to `base`, mark it ready, merge with a merge commit.

    If the merge auto-closes the issue (a `Fixes #N` keyword in the PR body),
    reopen it: the issue must stay open until the ship PR merges into main.
    """
    num = pr["number"]
    if pr.get("state") == "MERGED":
        return True, "already merged"
    if pr.get("baseRefName") != base:
        r = gh(["pr", "edit", str(num), "-R", cfg["repo"], "--base", base], env)
        if r.returncode != 0:
            return False, f"retarget failed: {r.stderr.strip()[:200]}"
    gh(["pr", "ready", str(num), "-R", cfg["repo"]], env)  # no-op if already ready
    r = gh(["pr", "merge", str(num), "-R", cfg["repo"], "--merge"], env)
    if r.returncode != 0:
        return False, r.stderr.strip()[:300]
    if issue_number:
        q = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
                "--json", "state"], env)
        if q.returncode == 0 and json.loads(q.stdout).get("state") == "CLOSED":
            gh(["issue", "reopen", str(issue_number), "-R", cfg["repo"]], env)
            return True, f"merged into {base} (issue #{issue_number} reopened)"
    return True, f"merged into {base}"


def find_or_create_ship_pr(cfg: dict, env: dict, head: str, title: str,
                           issue_number: int, base: str) -> dict | None:
    """Reuse the open ship PR for this head/base, or create one."""
    r = gh(["pr", "list", "-R", cfg["repo"], "--head", head, "--state", "open",
            "--json", "number,headRefName,baseRefName,state"], env)
    if r.returncode == 0:
        for pr in json.loads(r.stdout):
            if pr.get("baseRefName") == base:
                return pr
    body = (f"Fixes #{issue_number}\n\nShipped from develop after human testing. "
            "Reviewed by archon-smart-pr-review before merge.")
    r = gh(["pr", "create", "-R", cfg["repo"], "--base", base, "--head", head,
            "--title", title, "--body", body], env)
    if r.returncode != 0:
        log(f"SHIP PR CREATE FAILED head={head}: {r.stderr.strip()[:300]}")
        return None
    return {"number": int(r.stdout.strip().rstrip("/").split("/")[-1]),
            "headRefName": head, "baseRefName": base, "state": "OPEN"}


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
        dispatched_msg = None
        review_msg = None
        ship_pr_num = None

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
                if ok:
                    dispatched_msg = msg
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
                    dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"], branch,
                             msg, item_id, content["number"])
                else:
                    # Ensure the feature is in develop, then review the ship PR
                    # (feature -> main); on review completion the poller merges it.
                    pr = find_issue_pr(cfg, env, content["number"])
                    if pr:
                        merge_base = cfg["dispatch"]["todo"].get(
                            "merge_develop_base", "develop")
                        ok, note = merge_pr_to_base(cfg, env, pr, merge_base,
                                                    content["number"])
                        log(f"DEVELOP MERGE issue={content['number']} PR=#{pr['number']}: {note}"
                            if ok else
                            f"DEVELOP MERGE FAILED issue={content['number']}: {note}")
                    head = ((pr or {}).get("headRefName")
                            or f"archon/task-issue-{content['number']}")
                    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
                    ship = find_or_create_ship_pr(
                        cfg, env, head,
                        f"Ship: {content['title']} (#{content['number']})",
                        content["number"], ship_to)
                    if ship:
                        msg = (f"Review PR #{ship['number']} (ship to {ship_to} for issue "
                               f"#{content['number']}: {content['title']}).")
                        branch = f"review/issue-{content['number']}"
                        if dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"],
                                    branch, msg, item_id, content["number"]):
                            review_msg = msg
                            ship_pr_num = ship["number"]

        rec = state.get(item_id, {})
        rec["status"] = status_val
        if dispatched_msg:
            rec["dispatch_msg"] = dispatched_msg
            rec["issue_number"] = content["number"]
        if review_msg:
            rec["review_msg"] = review_msg
            rec["ship_pr"] = ship_pr_num
        state[item_id] = rec

    # Completion reconciliation: when a dispatched run finishes, merge the
    # feature PR into develop and move the item to Ready for Review.
    complete_move_to = cfg["dispatch"]["todo"].get("complete_move_to")
    in_progress_name = next(
        (k for k, v in cfg["lanes"].items() if v == "in_progress"), None)
    runs_by_msg = None
    if complete_move_to and in_progress_name:
        for item_id, rec in list(state.items()):
            if item_id == "_meta":
                continue
            msg = rec.get("dispatch_msg")
            if not msg or rec.get("status") != in_progress_name:
                continue
            if runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            run_status = runs_by_msg.get(msg)
            if run_status == "completed":
                issue_number = rec.get("issue_number")
                merge_base = cfg["dispatch"]["todo"].get("merge_develop_base")
                merge_ok = True
                if issue_number and merge_base:
                    pr = find_issue_pr(cfg, env, issue_number)
                    if pr:
                        merge_ok, note = merge_pr_to_base(cfg, env, pr, merge_base,
                                                          issue_number)
                        log(f"DEVELOP MERGE issue={issue_number} PR=#{pr['number']}: {note}"
                            if merge_ok else
                            f"DEVELOP MERGE FAILED issue={issue_number}: {note}")
                    else:
                        log(f"no PR found for issue #{issue_number}; skipping develop merge")
                if not merge_ok:
                    log(f"left item={item_id} in {in_progress_name} (develop merge failed)")
                    rec.pop("dispatch_msg", None)
                    continue
                option_id = status_options.get(complete_move_to)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    rec.pop("dispatch_msg", None)
                    log(f"MOVED item={item_id} -> {complete_move_to} (run completed)")
                elif option_id is None:
                    log(f"MOVE SKIPPED item={item_id}: lane '{complete_move_to}' not on board")
                else:
                    log(f"MOVE FAILED item={item_id} -> {complete_move_to}")
            elif run_status in ("failed", "cancelled"):
                log(f"RUN {run_status.upper()} item={item_id}; left in {in_progress_name}")
                rec.pop("dispatch_msg", None)

    # Review completion: merge the ship PR to its base (main) and move the
    # item to Done when the review run finishes.
    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
    review_lane_name = next(
        (k for k, v in cfg["lanes"].items() if v == "review"), None)
    done_name = cfg["dispatch"]["review"].get("done_lane", "Done")
    if (review_lane_name and done_name
            and cfg["dispatch"]["review"].get("merge_ship_on_review_complete")):
        for item_id, rec in list(state.items()):
            if item_id == "_meta":
                continue
            rmsg = rec.get("review_msg")
            if not rmsg or rec.get("status") != review_lane_name:
                continue
            if runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            rstatus = runs_by_msg.get(rmsg)
            if rstatus == "completed":
                ship_num = rec.get("ship_pr")
                ship = None
                if ship_num:
                    r = gh(["pr", "view", str(ship_num), "-R", cfg["repo"],
                            "--json", "number,state,baseRefName"], env)
                    if r.returncode == 0:
                        ship = json.loads(r.stdout)
                if ship and ship.get("state") != "MERGED":
                    rr = gh(["pr", "merge", str(ship_num), "-R", cfg["repo"],
                             "--merge"], env)
                    if rr.returncode == 0:
                        log(f"SHIPPED PR #{ship_num} -> {ship_to} (review completed)")
                    else:
                        log(f"SHIP MERGE FAILED PR #{ship_num}: {rr.stderr.strip()[:300]}")
                        continue
                elif not ship:
                    log(f"SHIP PR #{ship_num} not found for item={item_id}")
                    continue
                option_id = status_options.get(done_name)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    log(f"MOVED item={item_id} -> {done_name} (shipped)")
                rec.pop("review_msg", None)
                rec.pop("ship_pr", None)
            elif rstatus in ("failed", "cancelled"):
                log(f"REVIEW {rstatus.upper()} item={item_id}; left in {review_lane_name}")
                rec.pop("review_msg", None)

    # Prune items that left the board.
    for item_id in list(state):
        if item_id != "_meta" and item_id not in seen:
            del state[item_id]

    if first_run:
        state["_meta"] = {"snapshot_done": True, "project_id": project_id,
                          "snapshot_at": datetime.now(timezone.utc).isoformat()}
        log(f"snapshot taken: {len(seen)} items on board, dispatch armed")
    save_state(cfg, state)


def fetch_runs_by_message(env: dict) -> dict[str, str]:
    """Map exact run user_message -> status of the NEWEST matching run.

    Re-dispatches reuse the same message for the same issue, so multiple runs
    can share it; the newest run's status is the one that counts.
    """
    r = subprocess.run(["archon", "workflow", "runs", "--json"],
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(ROOT))
    if r.returncode != 0:
        return {}
    data = json.loads(r.stdout)
    runs = data.get("runs") if isinstance(data, dict) else data
    best: dict[str, tuple[str, str]] = {}
    for run in runs:
        msg = run.get("user_message")
        if not msg:
            continue
        started = run.get("started_at") or ""
        if started > best.get(msg, ("", ""))[1]:
            best[msg] = (run.get("status") or "", started)
    return {msg: status for msg, (status, _) in best.items()}


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
