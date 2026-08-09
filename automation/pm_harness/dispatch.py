"""Capacity-bounded Archon dispatch operations."""

from __future__ import annotations

import json
import subprocess

from .archon import resolve_worktree_branch
from .model import ACTIVE_WORKFLOW_STATUSES, DispatchResult
from .runtime import DRY_RUN, ROOT, gh, log

_DISPATCH_BUDGET: int | None = None

def resume_existing_worktree(
    cfg: dict,
    env: dict,
    full_branch: str,
    wf: str,
    issue_number: int,
    message: str,
    *,
    remove_needs_input: bool = False,
) -> tuple[bool, str, int | None]:
    """Start `archon continue` without creating a second worktree."""
    if DRY_RUN:
        log(f"[dry-run] RESUME issue={issue_number} branch={full_branch} wf={wf}")
        return True, message, None
    if not _dispatch_slot_available(wf, issue_number):
        return False, "Archon workflow capacity is full", None
    log_path = ROOT / "automation" / "archon-runs.log"
    try:
        with open(log_path, "a") as out:
            proc = subprocess.Popen(
                ["archon", "continue", full_branch, "--workflow", wf, message],
                cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        log(f"RESUME FAILED issue={issue_number}: {exc}")
        return False, message, None
    _consume_dispatch_slot()
    if remove_needs_input:
        gh(["issue", "edit", str(issue_number), "-R", cfg["repo"],
            "--remove-label", "needs-input"], env)
    log(f"RESUMED issue={issue_number} branch={full_branch} wf={wf} pid={proc.pid}")
    return True, message, proc.pid


def resume_issue(cfg: dict, env: dict, branch: str, wf: str,
                 issue_number: int) -> tuple[bool, str]:
    """Resume a blocked issue in its existing worktree after human input.

    Uses `archon continue` so the workflow picks up in the same worktree with
    prior context; the human's latest comment is passed as the message. Removes
    the needs-input label (the run is no longer blocked).
    """
    full_branch = resolve_worktree_branch(env, issue_number) or branch
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"], "--json", "comments",
            "-q", ".comments[-1].body"], env)
    answer = (r.stdout or "").strip()[:600] if r.returncode == 0 else ""
    msg = (f"Resuming issue #{issue_number} after human input."
           + (f" Latest comment from the human: {answer}" if answer else ""))
    ok, _message, _pid = resume_existing_worktree(
        cfg,
        env,
        full_branch,
        wf,
        issue_number,
        msg,
        remove_needs_input=True,
    )
    return ok, msg


def fetch_active_workflow_count(env: dict) -> int | None:
    """Return active Archon runs, or None when the status lookup is unusable."""
    try:
        result = subprocess.run(
            ["archon", "workflow", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    runs = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        return None
    return sum(
        1 for run in runs
        if isinstance(run, dict)
        and (run.get("status") or "").lower() in ACTIVE_WORKFLOW_STATUSES
    )


def prepare_dispatch_budget(cfg: dict, env: dict) -> None:
    """Reserve this poll's dispatch slots from Archon's conversation cap."""
    global _DISPATCH_BUDGET
    if DRY_RUN:
        _DISPATCH_BUDGET = None
        return
    try:
        limit = max(0, int(cfg.get("max_concurrent_workflows", 10)))
    except (TypeError, ValueError):
        limit = 10
    active = fetch_active_workflow_count(env)
    if active is None:
        _DISPATCH_BUDGET = 0
        log("DISPATCH HOLD: active Archon workflow count unavailable")
        return
    _DISPATCH_BUDGET = max(0, limit - active)
    if _DISPATCH_BUDGET == 0:
        log(f"DISPATCH HOLD: Archon workflow capacity reached ({limit})")


def _dispatch_slot_available(wf: str, number: int) -> bool:
    if _DISPATCH_BUDGET is not None and _DISPATCH_BUDGET <= 0:
        log(f"DISPATCH DEFERRED issue={number} wf={wf}: "
            "Archon workflow capacity is full")
        return False
    return True


def _consume_dispatch_slot() -> None:
    global _DISPATCH_BUDGET
    if _DISPATCH_BUDGET is not None:
        _DISPATCH_BUDGET -= 1


def dispatch(cfg: dict, env: dict, wf: str, branch: str, message: str,
             item_id: str, number: int) -> DispatchResult:
    """Start one Archon workflow and report why it did or did not start."""
    if DRY_RUN:
        log(f"[dry-run] DISPATCH wf={wf} branch={branch} issue={number}")
        return DispatchResult(True, "dry-run")
    if not _dispatch_slot_available(wf, number):
        return DispatchResult(False, "capacity")
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
        return DispatchResult(False, "spawn_failed")
    _consume_dispatch_slot()
    log(f"DISPATCHED item={item_id} issue={number} wf={wf} "
        f"branch={branch} pid={proc.pid}")
    return DispatchResult(True, pid=proc.pid)
