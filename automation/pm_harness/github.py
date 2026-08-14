"""GitHub Projects, issue, comment, and pull-request adapter."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone

from .policy import (
    build_ready_for_review_comment,
    extract_test_guidance,
    is_decision_only,
    issue_is_runnable,
    match_issue_pr,
    parse_verdict,
)
from .runtime import DRY_RUN, gh, log

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
          statusValue: fieldValueByName(name: $statusField) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue {
              number title url state body
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
    """Returns (project_id, status_field_id, status_options: name->id, items).

    Each item is {"id", "status", "content"}; status is the lane name
    (or "No status" when the field is unset).
    """
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
    while True:
        for node in page["nodes"]:
            items.append({
                "id": node["id"],
                "status": ((node.get("statusValue") or {}).get("name"))
                          or "No status",
                "content": node.get("content") or {},
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        page = graphql(cfg, env, page["pageInfo"]["endCursor"])["data"]["user"]["projectV2"]["items"]
    return project["id"], field["id"], options, items

def sync_runnable_labels(cfg: dict, env: dict, items: list[dict],
                         number_lane: dict[int, str],
                         number_state: dict[int, str],
                         todo_lane: str | None,
                         done_lane: str | None) -> None:
    """Keep the dispatch-eligible label aligned with board state."""
    label = cfg.get("runnable_label")
    if not label:
        return
    if DRY_RUN:
        log(f"[dry-run] ensure label {label!r} exists")
    else:
        try:
            created = gh(
                ["label", "create", label, "-R", cfg["repo"],
                 "--color", "0e8a16",
                 "--description", "Todo issue with satisfied dependencies",
                 "--force"],
                env,
            )
        except Exception as exc:
            log(f"RUNNABLE LABEL ENSURE FAILED: {exc}")
            return
        if created.returncode != 0:
            log(f"RUNNABLE LABEL ENSURE FAILED: "
                f"{created.stderr.strip()[:200]}")
            return

    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        number = content["number"]
        label_names = {
            node["name"] for node in (content.get("labels") or {}).get("nodes", [])
        }
        present = label in label_names
        desired = (
            not is_decision_only(cfg, list(label_names))
            and issue_is_runnable(
                content,
                item["status"],
                number_lane,
                number_state,
                todo_lane,
                done_lane,
            )
        )
        if present == desired:
            continue
        action = "--add-label" if desired else "--remove-label"
        if DRY_RUN:
            log(f"[dry-run] {action} {label} issue={number}")
            continue
        try:
            updated = gh(
                ["issue", "edit", str(number), "-R", cfg["repo"],
                 action, label],
                env,
            )
        except Exception as exc:
            log(f"RUNNABLE LABEL UPDATE FAILED issue={number}: {exc}")
            continue
        if updated.returncode != 0:
            log(f"RUNNABLE LABEL UPDATE FAILED issue={number}: "
                f"{updated.stderr.strip()[:200]}")
            continue
        log(f"RUNNABLE LABEL {'ADDED' if desired else 'REMOVED'} issue={number}")

def find_issue_pr(cfg: dict, env: dict, issue_number: int,
                  state: str = "all", base: str | None = None) -> tuple[dict | None, bool]:
    """(pr, ok) for the PR linked to an issue (any state).

    Pass `base` (e.g. "develop") when a specific PR role is required — an
    issue accumulates several PRs over its life and the newest is not
    necessarily the one a pass wants. ok=False means the gh lookup itself
    failed (network/auth/rate limit) — callers must NOT treat None as
    "no PR exists" and advance; they should retry on the next poll.
    """
    r = gh(["pr", "list", "-R", cfg["repo"], "--state", state,
            "--limit", "200",
            "--json", "number,title,body,headRefName,baseRefName,state"], env)
    if r.returncode != 0:
        log(f"find_issue_pr: gh pr list failed for issue #{issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None, False
    try:
        prs = json.loads(r.stdout)
    except ValueError as exc:
        log(f"find_issue_pr: unparseable gh output for issue #{issue_number}: {exc}")
        return None, False
    return match_issue_pr(prs, issue_number, base), True

def merge_pr_to_base(cfg: dict, env: dict, pr: dict, base: str,
                     issue_number: int | None = None) -> tuple[bool, str]:
    """Retarget a PR to `base`, mark it ready, merge with a merge commit.

    Callers pass `issue_number` for develop merges so the issue can be
    reopened if GitHub's keyword auto-close (`Fixes #N` in the PR body or
    commit message) closes it early — the issue must stay open until the
    ship PR merges into main. Without an issue number the merge proceeds
    and the reopen check is skipped; callers should not merge develop PRs
    that are not linked to an issue. A PR already merged short-circuits as
    success; a PR closed WITHOUT merging is a loud failure (never treat it
    as merged — the code is not in `base`).
    """
    if DRY_RUN:
        log(f"[dry-run] MERGE PR #{pr.get('number')} -> {base}")
        return True, "dry-run (no merge)"
    if pr.get("state") == "MERGED":
        return True, "already merged"
    if pr.get("state") == "CLOSED":
        return False, "PR closed without merging; re-drag the issue to Todo to re-run"
    r = gh(["pr", "edit", str(pr["number"]), "-R", cfg["repo"], "--base", base], env)
    if r.returncode != 0:
        return False, f"retarget failed: {r.stderr.strip()[:200]}"
    gh(["pr", "ready", str(pr["number"]), "-R", cfg["repo"]], env)
    rr = gh(["pr", "merge", str(pr["number"]), "-R", cfg["repo"], "--merge"], env)
    if rr.returncode != 0:
        return False, f"merge failed: {rr.stderr.strip()[:200]}"
    if issue_number:
        # GitHub applies keyword-based auto-close asynchronously after the
        # merge (PR-body keywords AND commit-message keywords like "Fix #N"),
        # so wait for it to land before checking whether we must reopen.
        time.sleep(6)
        q = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
                "--json", "state"], env)
        if q.returncode == 0 and json.loads(q.stdout).get("state") == "CLOSED":
            gh(["issue", "reopen", str(issue_number), "-R", cfg["repo"]], env)
            return True, f"merged into {base} (issue #{issue_number} reopened)"
    return True, f"merged into {base}"

def find_or_create_ship_pr(cfg: dict, env: dict, head: str, title: str,
                           issue_number: int, base: str) -> dict | None:
    """Reuse the open ship PR for this head/base, or create one."""
    if DRY_RUN:
        log(f"[dry-run] SHIP PR for head={head} base={base} (reuse or create)")
        return {"number": 0, "headRefName": head}
    r = gh(["pr", "list", "-R", cfg["repo"], "--head", head, "--state", "open",
            "--json", "number,baseRefName"], env)
    if r.returncode != 0:
        log(f"SHIP PR LIST FAILED head={head}: {r.stderr.strip()[:200]}")
        return None
    else:
        try:
            prs = json.loads(r.stdout or "[]")
        except (TypeError, ValueError) as exc:
            log(f"SHIP PR LIST PARSE FAILED head={head}: {exc}")
            return None
        if not isinstance(prs, list):
            log(f"SHIP PR LIST PARSE FAILED head={head}: expected a list")
            return None
        for pr in prs:
            if isinstance(pr, dict) and pr.get("baseRefName") == base:
                return pr
    body = (f"Issue #{issue_number}. Shipped from develop after human testing. "
            "Reviewed by archon-smart-pr-review before merge.")
    r = gh(["pr", "create", "-R", cfg["repo"], "--base", base, "--head", head,
            "--title", title, "--body", body], env)
    if r.returncode != 0:
        log(f"SHIP PR CREATE FAILED head={head} issue={issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None
    m = re.search(r"pull/(\d+)", r.stdout or "")
    return {"number": int(m.group(1)), "headRefName": head} if m else None

def branch_empty_vs_main(cfg: dict, env: dict, head: str, base: str) -> bool:
    """True when `head` has no commits beyond `base` — its work already
    reached base via another ship PR's develop merge (the #24 case), so a
    ship PR would be empty and a review would be meaningless."""
    r = gh(["api", f"repos/{cfg['repo']}/compare/{base}...{head}"], env)
    if r.returncode != 0:
        return False  # unknown — treat as shippable (safe default)
    try:
        return int(json.loads(r.stdout).get("ahead_by", 1)) == 0
    except (ValueError, TypeError):
        return False

def move_to_lane(cfg: dict, env: dict, project_id: str, item_id: str,
                 field_id: str, option_id: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] MOVE item={item_id} -> option {option_id}")
        return True
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

def comment_issue(cfg: dict, env: dict, issue_number: int, body: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] COMMENT issue={issue_number}: {body[:100]}")
        return True
    r = gh(["issue", "comment", str(issue_number), "-R", cfg["repo"],
            "--body", body], env)
    return r.returncode == 0

def note_capacity_deferred(
    cfg: dict,
    env: dict,
    issue_number: int,
    rec: dict,
) -> None:
    """Expose one capacity hold per episode on the issue and in persisted state."""
    if rec.get("capacity_deferred"):
        return
    limit = cfg.get("max_concurrent_workflows")
    body = (
        "Automation is queued because workflow capacity is full"
        + (f" ({limit} active slots)." if limit is not None else ".")
        + " It remains in Todo and will dispatch automatically when a slot opens."
    )
    if comment_issue(cfg, env, issue_number, body):
        rec["capacity_deferred"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")

def note_integration_blocked(
    cfg: dict,
    env: dict,
    issue_number: int,
    rec: dict,
    reason: str,
) -> None:
    """Keep false-Ready protection sticky without repeating issue comments."""
    if rec.get("integration_blocked") == reason:
        return
    body = (
        "Not ready for review: "
        + reason
        + " The issue stays In Progress until its implementation PR is merged "
        f"into `{cfg['dispatch']['todo'].get('merge_develop_base', 'develop')}`."
    )
    if comment_issue(cfg, env, issue_number, body):
        rec["integration_blocked"] = reason

def fetch_issue_comments(cfg: dict, env: dict,
                         issue_number: int) -> list[dict] | None:
    """Fetch issue comments, returning None only when GitHub lookup fails."""
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
            "--json", "comments"], env)
    if r.returncode != 0:
        log(f"READY TEST FETCH FAILED issue #{issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError as exc:
        log(f"READY TEST PARSE FAILED issue #{issue_number}: {exc}")
        return None
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        log(f"READY TEST PARSE FAILED issue #{issue_number}: comments is not a list")
        return None
    return comments

def post_ready_for_review_comment(cfg: dict, env: dict, issue_number: int,
                                  base: str, pr_number: int | None = None) -> bool:
    """Post the test handoff after an issue reaches Ready for Review."""
    if DRY_RUN:
        return comment_issue(
            cfg, env, issue_number,
            build_ready_for_review_comment(issue_number, base, pr_number, None),
        )
    comments = fetch_issue_comments(cfg, env, issue_number)
    if comments is None:
        return False
    guidance = extract_test_guidance(comments)
    body = build_ready_for_review_comment(issue_number, base, pr_number, guidance)
    ok = comment_issue(cfg, env, issue_number, body)
    if not ok:
        log(f"READY TEST COMMENT FAILED issue #{issue_number}")
    return ok

def issue_has_label(cfg: dict, env: dict, issue_number: int, label: str) -> bool:
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"], "--json", "labels",
            "-q", ".labels[].name"], env)
    return r.returncode == 0 and label in r.stdout.split()

def fetch_verdict(cfg: dict, env: dict, pr_number: int) -> tuple[str | None, bool]:
    """(verdict, ok). ok=False means the lookup failed (gh error or
    unparseable output) — the caller must NOT treat the verdict as a real
    non-approve and must NOT pop retry markers; retry next poll instead."""
    r = gh(["pr", "view", str(pr_number), "-R", cfg["repo"],
            "--json", "comments"], env)
    if r.returncode != 0:
        log(f"VERDICT FETCH FAILED PR #{pr_number}: {r.stderr.strip()[:200]}")
        return None, False
    try:
        data = json.loads(r.stdout)
    except (TypeError, ValueError) as exc:
        log(f"VERDICT PARSE FAILED PR #{pr_number}: {exc}")
        return None, False
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list) or not all(
        isinstance(comment, dict) for comment in comments
    ):
        log(f"VERDICT PARSE FAILED PR #{pr_number}: comments is not a list")
        return None, False
    return parse_verdict([c.get("body") or "" for c in comments]), True

def find_ship_pr(cfg: dict, env: dict, issue_number: int, base: str) -> dict | None:
    """Open ship PR (head -> base) linked to an issue; None when absent.

    The ship PR shares its head branch with the issue's develop PR, so this
    resolves the issue's PR first, then looks for an open PR with that head
    targeting `base`.
    """
    pr, pr_ok = find_issue_pr(cfg, env, issue_number)
    if not pr_ok or not pr:
        return None
    r = gh(["pr", "list", "-R", cfg["repo"], "--head", pr.get("headRefName") or "",
            "--state", "open", "--json", "number,baseRefName,mergeable,headRefName"],
           env)
    if r.returncode != 0:
        return None
    try:
        prs = json.loads(r.stdout or "[]")
    except (TypeError, ValueError) as exc:
        log(f"SHIP PR LIST PARSE FAILED issue={issue_number}: {exc}")
        return None
    if not isinstance(prs, list):
        log(f"SHIP PR LIST PARSE FAILED issue={issue_number}: expected a list")
        return None
    for p in prs:
        if isinstance(p, dict) and p.get("baseRefName") == base:
            return p
    return None

def try_merge_base_into_head(cfg: dict, env: dict, pr_number: int,
                             head: str, base: str) -> tuple[bool, str]:
    """Merge `base` into the PR head via the GitHub merge API.

    Same operation as the "Update branch" button. Returns (True, note) when
    the merge applied or was a no-op; (False, "conflict") when the branches
    genuinely conflict; (False, note) on transient errors (caller retries
    next poll without escalating).
    """
    if DRY_RUN:
        log(f"[dry-run] SHIP CONFLICT UPDATE PR #{pr_number}: merge {base} -> {head}")
        return True, "dry-run (no merge)"
    r = gh(["api", "-X", "POST", f"repos/{cfg['repo']}/merges",
            "-f", f"base={head}", "-f", f"head={base}"], env)
    if r.returncode == 0:
        return True, "base merged into head"
    err = (r.stderr or "") + (r.stdout or "")
    low = err.lower()
    if "conflict" in low:
        return False, "conflict"
    if "no commits between" in low or "already up to date" in low:
        return True, "no-op (head already contains base)"
    return False, f"transient: {err.strip()[:200]}"


def label_exists(cfg: dict, env: dict, label: str) -> bool:
    """True when `label` already exists in the repo (gh fails on unknown
    labels, so never pass one through unchecked)."""
    r = gh(["label", "list", "-R", cfg["repo"], "--json", "name"], env)
    if r.returncode != 0:
        return False
    try:
        labels = json.loads(r.stdout or "[]")
    except (TypeError, ValueError) as exc:
        log(f"LABEL LIST PARSE FAILED: {exc}")
        return False
    return isinstance(labels, list) and any(
        isinstance(label_item, dict) and label_item.get("name") == label
        for label_item in labels
    )
