#!/usr/bin/env python3
"""Inspect, resume, or explicitly discard one Archon issue worktree.

This command never starts a fresh workflow. ``resume`` uses ``archon continue``
against the existing worktree; ``discard`` requires an explicit subcommand and
refuses dirty worktrees unless ``--force`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from automation.pm_harness import archon, dispatch, model, runtime
except ImportError:  # direct `python3 automation/workflow_recovery.py`
    from pm_harness import archon, dispatch, model, runtime

ACTIVE_WORKFLOW_STATUSES = model.ACTIVE_WORKFLOW_STATUSES
ROOT = runtime.ROOT
fetch_workflow_runs = archon.fetch_workflow_runs
fetch_workflow_run = archon.fetch_workflow_run
gh = runtime.gh
inspect_worktree = archon.inspect_worktree
latest_workflow_run = archon.latest_workflow_run
load_config = runtime.load_config
issue_number_from_message = model.issue_number_from_message
resolve_worktree_info = archon.resolve_worktree_info
resume_existing_worktree = dispatch.resume_existing_worktree
workflow_run_details = model.workflow_run_details


def _env() -> dict:
    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()
    return env


class RecoveryStateError(RuntimeError):
    """Raised when durable recovery state cannot be read or validated."""


def _load_state(cfg: dict) -> tuple[Path, dict]:
    path = ROOT / cfg["state_file"]
    if not path.exists():
        return path, {}
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RecoveryStateError(
            f"cannot read recovery state {path}: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise RecoveryStateError(
            f"recovery state {path} must contain a JSON object"
        )
    return path, state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, path)


def _record_for_issue(state: dict, issue_number: int) -> dict | None:
    for key, record in state.items():
        if key == "_meta" or not isinstance(record, dict):
            continue
        if record.get("issue_number") == issue_number:
            return record
    return None


def _mark_recovery(state: dict, issue_number: int, marker: dict) -> None:
    record = _record_for_issue(state, issue_number)
    if record is not None:
        record["recovery"] = marker
        return
    meta = state.setdefault("_meta", {})
    meta.setdefault("workflow_recovery", {})[str(issue_number)] = marker


def _status_payload(cfg: dict, env: dict, issue_number: int) -> dict:
    _state_path, state = _load_state(cfg)
    record = _record_for_issue(state, issue_number) or {}
    runs = fetch_workflow_runs(env)
    run = fetch_workflow_run(env, record.get("run_id"))
    if run is None:
        run = latest_workflow_run(runs, issue_number=issue_number)
    worktree_info = resolve_worktree_info(env, issue_number)
    stored = record.get("recovery") if isinstance(record.get("recovery"), dict) else {}
    stored_worktree = stored.get("worktree") if isinstance(stored.get("worktree"), dict) else {}
    run_details = workflow_run_details(
        run,
        branch=worktree_info.get("branch") if worktree_info else record.get("branch"),
    ) if run else {}
    path = (
        run_details.get("working_path")
        or (worktree_info or {}).get("path")
        or stored_worktree.get("path")
    )
    worktree = inspect_worktree(path)
    status = str(run_details.get("status") or "").lower()
    if status in ACTIVE_WORKFLOW_STATUSES:
        recovery = "resumed" if stored.get("action") == "resume_requested" else "active"
    elif stored.get("action") == "discarded" and not worktree.get("exists"):
        recovery = "discarded"
    elif worktree.get("dirty") is True:
        recovery = "resume_required"
    elif status in {"failed", "cancelled"}:
        recovery = stored.get("action") or "manual_review"
    elif worktree.get("exists"):
        recovery = "existing_worktree"
    else:
        recovery = "absent"
    return {
        "issue_number": issue_number,
        "recovery": recovery,
        "run": run_details,
        "worktree": worktree,
        "stored_recovery": stored,
        "branch": (worktree_info or {}).get("branch") or record.get("branch") or "",
        "workflow": run_details.get("workflow") or record.get("wf") or "",
        "attempt_count": sum(
            issue_number_from_message(
                str(candidate.get("user_message") or "")) == issue_number
            for candidate in runs
        ),
    }


def _print_status(payload: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0
    run = payload["run"]
    worktree = payload["worktree"]
    print(f"Issue #{payload['issue_number']}")
    print(f"Recovery: {payload['recovery']}")
    print(f"Run: {run.get('run_id') or 'none'} ({run.get('status') or 'none'})")
    if run.get("failed_step"):
        print(f"Failed step: {run['failed_step']}")
    if run.get("failure_class"):
        print(f"Classification: {run['failure_class']}")
    print(f"Workflow: {payload['workflow'] or 'unknown'}")
    print(f"Branch: {payload['branch'] or 'unknown'}")
    print(f"Worktree: {worktree.get('path') or 'none'}")
    if worktree.get("exists"):
        print(f"Worktree state: {'dirty' if worktree.get('dirty') else 'clean'}")
    if payload["recovery"] == "resume_required":
        n = payload["issue_number"]
        print(f"Resume:  python3 automation/workflow_recovery.py resume {n}")
        print(f"Discard: python3 automation/workflow_recovery.py discard {n}")
    elif payload["recovery"] == "manual_review":
        n = payload["issue_number"]
        if run.get("failure_class") == "orchestration":
            print("Next: repair the workflow/PR handoff before requeueing this issue.")
        else:
            print("Next: inspect the failure and preserve or discard the worktree explicitly.")
        print(f"Discard: python3 automation/workflow_recovery.py discard {n}")
    return 0


def _resume_message(
    cfg: dict,
    env: dict,
    issue_number: int,
    payload: dict,
) -> str:
    result = gh(
        ["issue", "view", str(issue_number), "-R", cfg["repo"],
         "--json", "title,body,url"],
        env,
    )
    issue: dict = {}
    if result.returncode == 0:
        try:
            parsed = json.loads(result.stdout)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            issue = parsed
    title = str(issue.get("title") or "").strip()
    url = str(issue.get("url") or
              f"https://github.com/{cfg['repo']}/issues/{issue_number}").strip()
    body = str(issue.get("body") or "").strip()
    context = [
        f"Resume the existing worktree for issue #{issue_number}.",
        "This is automatic recovery after a transient workflow failure.",
        "Treat the issue context below as authoritative; preserve completed work, "
        "finish validation, and create the PR. Do not start unrelated work.",
        "",
        f"Issue: {title}" if title else f"Issue #{issue_number}",
        f"Full issue: {url}",
    ]
    if body:
        context.extend(["", "Issue body:", body[:12000]])
    context.extend([
        "",
        "Prior run failed during validation. Continue the existing worktree and "
        "repair or complete the implementation rather than creating a fresh branch.",
    ])
    return "\n".join(context)


def _resume(cfg: dict, env: dict, issue_number: int) -> int:
    payload = _status_payload(cfg, env, issue_number)
    status = str(payload["run"].get("status") or "").lower()
    if status in ACTIVE_WORKFLOW_STATUSES:
        print(f"Issue #{issue_number} already has an active run {payload['run'].get('run_id')}",
              file=sys.stderr)
        return 2
    if not payload["worktree"].get("exists") or not payload["branch"]:
        print(f"Issue #{issue_number} has no existing Archon worktree to resume", file=sys.stderr)
        return 2
    workflow = payload["workflow"]
    if not workflow:
        print(f"Issue #{issue_number} has no workflow record to resume", file=sys.stderr)
        return 2
    message = _resume_message(cfg, env, issue_number, payload)
    ok, _message, pid = resume_existing_worktree(
        cfg, env, payload["branch"], workflow, issue_number, message)
    if not ok:
        print("Resume did not start; check the poller log.", file=sys.stderr)
        return 1
    state_path, state = _load_state(cfg)
    _mark_recovery(state, issue_number, {
        "action": "resume_requested",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": pid,
        "worktree": payload["worktree"],
    })
    record = _record_for_issue(state, issue_number)
    if record is not None:
        for marker in (
            "run_id",
            "recovery_notified_run_id",
            "recovery_logged_run_id",
            "retrying",
        ):
            record.pop(marker, None)
    _save_state(state_path, state)
    gh(["issue", "comment", str(issue_number), "-R", cfg["repo"], "--body",
        f"Automation recovery resume started (pid `{pid or 'unknown'}`). "
        f"Check `python3 automation/workflow_recovery.py status {issue_number}`; "
        "the poller will show the resumed run and its final outcome."], env)
    print(f"Resumed issue #{issue_number} in {payload['branch']} (pid {pid or 'unknown'})")
    return 0


def _discard(cfg: dict, env: dict, issue_number: int, force: bool) -> int:
    payload = _status_payload(cfg, env, issue_number)
    status = str(payload["run"].get("status") or "").lower()
    if status in ACTIVE_WORKFLOW_STATUSES:
        print(f"Issue #{issue_number} still has an active run; refuse to discard", file=sys.stderr)
        return 2
    branch = payload["branch"]
    if not branch:
        print(f"Issue #{issue_number} has no Archon worktree to discard")
        return 0
    if payload["worktree"].get("dirty") and not force:
        print(
            f"Refusing to discard dirty worktree {payload['worktree'].get('path')}. "
            "Re-run with --force only after confirming the changes are unwanted.",
            file=sys.stderr,
        )
        return 2
    result = subprocess.run(
        ["archon", "complete", branch],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env=env,
    )
    if result.returncode != 0:
        print(result.stderr.strip()[:500] or "Archon discard failed", file=sys.stderr)
        return 1
    state_path, state = _load_state(cfg)
    _mark_recovery(state, issue_number, {
        "action": "discarded",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "worktree": payload["worktree"],
    })
    record = _record_for_issue(state, issue_number)
    if record is not None:
        for marker in (
            "run_id",
            "dispatch_msg",
            "branch",
            "wf",
            "retrying",
            "automatic_retry_count",
            "recovery_notified_run_id",
            "recovery_logged_run_id",
        ):
            record.pop(marker, None)
    _save_state(state_path, state)
    gh(["issue", "comment", str(issue_number), "-R", cfg["repo"], "--body",
        f"Archon worktree `{branch}` was explicitly discarded. "
        "No fresh run was started; requeue only after the issue/workflow is ready."], env)
    print(f"Discarded issue #{issue_number} worktree {branch}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show run and worktree recovery state")
    status.add_argument("issue", type=int)
    status.add_argument("--json", action="store_true", dest="as_json")
    resume = subparsers.add_parser("resume", help="continue the existing worktree")
    resume.add_argument("issue", type=int)
    discard = subparsers.add_parser("discard", help="remove the existing worktree")
    discard.add_argument("issue", type=int)
    discard.add_argument("--force", action="store_true",
                         help="allow discarding a dirty worktree")
    args = parser.parse_args(argv)
    cfg = load_config()
    env = _env()
    try:
        if args.command == "status":
            return _print_status(_status_payload(cfg, env, args.issue), args.as_json)
        if args.command == "resume":
            return _resume(cfg, env, args.issue)
        return _discard(cfg, env, args.issue, args.force)
    except RecoveryStateError as exc:
        print(f"Recovery state error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
