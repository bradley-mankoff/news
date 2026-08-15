"""Persisted recovery decisions for stopped or detached Archon runs."""

from __future__ import annotations

from datetime import datetime, timezone

from .archon import (
    fetch_workflow_runs,
    inspect_worktree,
    latest_workflow_run,
    resolve_worktree_info,
)
from .dispatch import dispatch
from .github import comment_issue, note_capacity_deferred
from .model import (
    ACTIVE_WORKFLOW_STATUSES,
    build_recovery_comment,
    issue_number_from_message,
    recovery_action,
    workflow_run_details,
)
from .runtime import log

def _as_worktree(value: object, path: str = "") -> dict:
    """Accept dict probes or legacy bool/None dirty results."""
    if isinstance(value, dict):
        return value
    if value is True or value is False:
        return {"path": path or "", "exists": True, "dirty": value}
    return {"path": path or "", "exists": bool(path), "dirty": None}


def update_recovery_state(
    rec: dict,
    run: dict,
    *,
    branch: str | None = None,
    attempt_count: int | None = None,
) -> tuple[dict, dict, str]:
    """Persist the latest run, failure classification, and worktree snapshot."""
    details = workflow_run_details(run, branch=branch)
    recovery = rec.get("recovery")
    previous_action = recovery.get("action") if isinstance(recovery, dict) else ""
    worktree_path = details["working_path"]
    if not worktree_path and isinstance(recovery, dict):
        worktree_path = ((recovery.get("worktree") or {}).get("path") or "")
    worktree = _as_worktree(inspect_worktree(worktree_path), worktree_path)
    action = recovery_action(
        details["status"],
        details["failure_class"],
        worktree.get("dirty"),
    )
    if action == "monitoring" and previous_action == "resume_requested":
        action = "resumed"
    if (
        action == "retry_available"
        and int(rec.get("automatic_retry_count") or 0) >= 1
    ):
        action = "manual_review"
    rec["last_run"] = details
    rec["attempt_count"] = attempt_count or rec.get("attempt_count") or 1
    rec["recovery"] = {
        "action": action,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": details["run_id"],
        "failure_class": details["failure_class"],
        "failed_step": details["failed_step"],
        "worktree": worktree,
    }
    attempts = rec.setdefault("attempts", [])
    if details["run_id"]:
        previous = next(
            (attempt for attempt in attempts
             if attempt.get("run_id") == details["run_id"]),
            None,
        )
        if previous is None:
            attempts.append(details)
        else:
            previous.update(details)
        rec["attempts"] = attempts[-8:]
    return details, worktree, action


def notify_workflow_recovery(
    cfg: dict,
    env: dict,
    issue_number: int,
    rec: dict,
    details: dict,
    worktree: dict,
    action: str,
    *,
    retry_number: int | None = None,
) -> bool:
    """Post one recovery comment per Archon run."""
    run_id = details.get("run_id") or details.get("message")
    if rec.get("recovery_notified_run_id") == run_id:
        return True
    body = build_recovery_comment(
        issue_number,
        details,
        worktree,
        action,
        retry_number=retry_number,
    )
    if not comment_issue(cfg, env, issue_number, body):
        return False
    rec["recovery_notified_run_id"] = run_id
    return True


def reconcile_untracked_runs(
    cfg: dict,
    env: dict,
    state: dict,
    items: list[dict],
    in_progress_lane: str | None,
) -> None:
    """Attach recent Archon runs to board items whose dispatch marker was lost."""
    runs: list[dict] | None = None
    for item in items:
        if item.get("status") != in_progress_lane:
            continue
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        rec = state.setdefault(item["id"], {})
        if rec.get("dispatch_msg"):
            continue
        if runs is None:
            runs = fetch_workflow_runs(env)
            lookup_error = getattr(runs, "error", None)
            if lookup_error:
                log(
                    f"RUN LOOKUP UNAVAILABLE: {lookup_error}; "
                    "retaining untracked-run markers"
                )
                return
        number = content["number"]
        run = latest_workflow_run(runs, issue_number=number)
        if not run:
            continue
        message = str(run.get("user_message") or "")
        if not message:
            continue
        rec["issue_number"] = number
        rec["dispatch_msg"] = message
        rec["wf"] = run.get("workflow_name") or rec.get("wf")
        rec["branch"] = rec.get("branch") or f"issue-{number}"
        matching = [
            candidate for candidate in runs
            if issue_number_from_message(
                str(candidate.get("user_message") or "")) == number
        ]
        details, worktree, action = update_recovery_state(
            rec,
            run,
            branch=rec["branch"],
            attempt_count=len(matching) or 1,
        )
        if details["status"] in {"failed", "cancelled"}:
            notify_workflow_recovery(
                cfg, env, number, rec, details, worktree, action)


def fresh_issue_dispatch_guard(
    env: dict,
    issue_number: int,
) -> tuple[bool, str]:
    """Refuse a fresh run while an existing issue worktree is unsafe."""
    worktree_info = resolve_worktree_info(env, issue_number)
    lookup_error = (
        worktree_info.get("error")
        if isinstance(worktree_info, dict)
        else None
    )
    if lookup_error:
        log(
            f"FRESH DISPATCH DEFERRED issue={issue_number}: "
            f"worktree lookup unavailable ({lookup_error})"
        )
        return False, f"Archon worktree lookup unavailable: {lookup_error}"
    if worktree_info:
        worktree = inspect_worktree(worktree_info.get("path"))
        if worktree.get("dirty") is True:
            return False, (
                f"existing worktree is dirty: {worktree.get('path')}; "
                f"resume or discard issue #{issue_number}"
            )
    runs = fetch_workflow_runs(env)
    lookup_error = getattr(runs, "error", None)
    if lookup_error:
        log(
            f"FRESH DISPATCH DEFERRED issue={issue_number}: "
            f"run lookup unavailable ({lookup_error})"
        )
        return False, f"Archon run lookup unavailable: {lookup_error}"
    latest = latest_workflow_run(runs, issue_number=issue_number)
    if latest and str(latest.get("status") or "").lower() in ACTIVE_WORKFLOW_STATUSES:
        return False, (
            f"Archon run {latest.get('id') or 'unknown'} is still active"
        )
    return True, ""


def auto_retry_transient_failure(
    cfg: dict,
    env: dict,
    item_id: str,
    issue_number: int,
    rec: dict,
    details: dict,
    worktree: dict,
    matching_runs: list[dict],
) -> bool:
    """Retry one clean-worktree transport failure, never a partial one."""
    if details.get("failure_class") != "transient" or worktree.get("dirty") is not False:
        return False
    if int(rec.get("automatic_retry_count") or 0) >= 1 or rec.get("retrying"):
        return False
    wf = rec.get("wf")
    branch = rec.get("branch") or f"issue-{issue_number}"
    message = rec.get("dispatch_msg") or details.get("message")
    if not wf or not message:
        return False
    result = dispatch(cfg, env, wf, branch, message, item_id, issue_number)
    if not result:
        if getattr(result, "reason", "") == "capacity":
            note_capacity_deferred(cfg, env, issue_number, rec)
        return False
    rec.pop("run_id", None)
    rec.pop("capacity_deferred", None)
    rec["retrying"] = True
    rec["automatic_retry_count"] = 1
    recovery = rec.setdefault("recovery", {})
    recovery["action"] = "retrying"
    recovery["run_id"] = details.get("run_id") or recovery.get("run_id") or ""
    notify_workflow_recovery(
        cfg,
        env,
        issue_number,
        rec,
        details,
        worktree,
        "retrying",
        retry_number=1,
    )
    return True
