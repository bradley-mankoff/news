#!/usr/bin/env python3
"""Read-only health report for the configured PM harness board.

Exit code is always zero; findings identify recovery, capacity, dependency,
readiness, or review state that needs attention.

Usage: python3 automation/board_health.py
"""

import json
import os
import sys

try:
    from automation.pm_harness import archon, github, model, policy, runtime
except ImportError:  # direct `python3 automation/board_health.py`
    from pm_harness import archon, github, model, policy, runtime

ROOT = runtime.ROOT
dep_gate = policy.dep_gate
fetch_project = github.fetch_project
fetch_workflow_run = archon.fetch_workflow_run
fetch_workflow_runs = archon.fetch_workflow_runs
find_issue_pr = github.find_issue_pr
find_ship_pr = github.find_ship_pr
fmt_deps = policy.fmt_deps
gh = runtime.gh
inspect_worktree = archon.inspect_worktree
latest_workflow_run = archon.latest_workflow_run
load_config = runtime.load_config
parse_dep_refs = policy.parse_dep_refs
run_status_for = policy.run_status_for
workflow_run_details = model.workflow_run_details
workflow_status_by_message = policy.workflow_status_by_message

def recovery_finding(number: int, rec: dict, run: dict) -> str:
    details = workflow_run_details(run, branch=rec.get("branch"))
    worktree = inspect_worktree(
        details.get("working_path")
        or ((rec.get("recovery") or {}).get("worktree") or {}).get("path")
    )
    action = ((rec.get("recovery") or {}).get("action")
              or ("resume_required" if worktree.get("dirty") else "manual_review"))
    step = details.get("failed_step") or "unknown step"
    classification = details.get("failure_class") or "unknown"
    worktree_state = (
        "dirty" if worktree.get("dirty") else
        "clean" if worktree.get("exists") else "missing"
    )
    next_step = {
        "resume_required": "resume or discard the worktree",
        "retry_available": "one clean-worktree retry is available",
        "manual_review": "inspect before requeueing",
    }.get(action, action)
    return (
        f"#{number} run {details.get('status') or 'unknown'} at {step} "
        f"({classification}); worktree {worktree_state}; "
        f"recovery={next_step} — "
        f"python3 automation/workflow_recovery.py status {number}"
    )



def main() -> int:
    cfg = load_config()
    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()

    try:
        _project_id, _field_id, _options, items = fetch_project(cfg, env)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 0

    state_path = ROOT / cfg["state_file"]
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    workflow_runs = fetch_workflow_runs(env)
    runs_by_msg = workflow_status_by_message(workflow_runs)

    lane_names = {v: k for k, v in cfg["lanes"].items()}
    done_lane = lane_names.get("done")
    todo_lane = lane_names.get("todo")
    blocked_lane = lane_names.get("blocked")
    needs_input_lane = lane_names.get("needs_input")
    in_progress_lane = lane_names.get("in_progress")
    ready_lane = lane_names.get("ready")
    review_lane = lane_names.get("review")

    issues = []
    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        issues.append(item)

    number_lane = {c["content"]["number"]: c["status"] for c in issues}
    number_state = {c["content"]["number"]: (c["content"].get("state") or "") for c in issues}

    counts: dict[str, int] = {}
    for item in issues:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    total = len(issues)
    done_count = counts.get(lane_names.get("done", ""), 0)
    order = [lane_names.get(v) for v in
             ("backlog", "todo", "in_progress", "blocked", "needs_input",
              "ready", "review", "done")
             if lane_names.get(v)]
    summary = "Board: " + ", ".join(
        f"{name} {counts.get(name, 0)}" for name in order) + f" (total {total})"
    print(summary)
    if total:
        print(f"Progress: {done_count}/{total} done ({100 * done_count // total}%)")

    findings = []
    for item in issues:
        content = item["content"]
        number = content["number"]
        lane = item["status"]
        rec = state.get(str(number), state.get(item["id"], {}))
        labels = [n["name"] for n in content["labels"]["nodes"]]

        if lane == in_progress_lane:
            msg = rec.get("dispatch_msg")
            run = fetch_workflow_run(env, rec.get("run_id"))
            if run is None:
                run = latest_workflow_run(
                    workflow_runs,
                    message=msg,
                ) if msg else latest_workflow_run(
                    workflow_runs,
                    issue_number=number,
                )
            if run:
                status = str(run.get("status") or "").lower()
                if status in {"failed", "cancelled"}:
                    findings.append(recovery_finding(number, rec, run))
                elif status not in ("running", "pending", "queued", "completed"):
                    findings.append(
                        f"#{number} in {in_progress_lane} with no active run "
                        f"(status {status or 'unknown'}) — inspect recovery state")
            elif not msg:
                findings.append(
                    f"#{number} in {in_progress_lane} with no dispatch record and "
                    "no matching Archon run — inspect before re-dragging")
            else:
                status = run_status_for(runs_by_msg, msg)
                if status not in ("running", "pending", "queued", "completed"):
                    findings.append(
                        f"#{number} in {in_progress_lane} with no active run "
                        f"(status {status or 'unknown'}) — inspect recovery state")
        elif lane == ready_lane:
            base = cfg["dispatch"]["todo"].get(
                "merge_develop_base", "develop")
            pr, ok = find_issue_pr(cfg, env, number, base=base)
            if not ok:
                findings.append(
                    f"#{number} in {ready_lane} but integration proof is unreadable")
            elif not pr or pr.get("state") != "MERGED":
                detail = (
                    "no linked integration PR"
                    if not pr else f"PR #{pr.get('number')} is {pr.get('state')}"
                )
                findings.append(
                    f"#{number} false Ready: {detail}; bounce to {in_progress_lane}")
        elif lane == blocked_lane:
            if not rec.get("dep_blocked"):
                findings.append(
                    f"#{number} in {blocked_lane} without a dependency marker — "
                    "unknown blocker, check manually")
        elif lane == needs_input_lane:
            if "needs-input" not in labels:
                findings.append(
                    f"#{number} in {needs_input_lane} without the needs-input label — "
                    "unknown blocker, check manually")
        elif lane == review_lane:
            rmsg = rec.get("review_msg")
            if not rmsg:
                findings.append(
                    f"#{number} in {review_lane} with no review run on record — "
                    "drag out and back in to re-run")
            else:
                status = run_status_for(runs_by_msg, rmsg)
                if status not in ("running", "pending", "queued", "completed"):
                    findings.append(
                        f"#{number} in {review_lane} with no active review run "
                        f"(status {status or 'unknown'}) — check the ship PR and re-drag")
            ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
            if cfg["dispatch"]["review"].get("conflict_fix_workflow"):
                ship = find_ship_pr(cfg, env, number, ship_to)
                if ship and ship.get("mergeable") == "CONFLICTING":
                    fix_msg = rec.get("conflict_fix_msg")
                    if fix_msg:
                        fix_status = run_status_for(runs_by_msg, fix_msg)
                        if fix_status in ("running", "pending", "queued", "scheduled"):
                            detail = "auto-fix run active"
                        else:
                            detail = "auto-fix run finished, still conflicting — needs human"
                    elif rec.get("conflict_mech_failed"):
                        detail = "mechanical merge failed — fix dispatch pending"
                    else:
                        detail = "mechanical merge pending"
                    findings.append(
                        f"#{number} ship PR #{ship['number']} conflicting ({detail})")
        if rec.get("capacity_deferred"):
            findings.append(
                f"#{number} queued in {lane}: workflow capacity is full")

        if done_lane and lane in (todo_lane, in_progress_lane, blocked_lane, review_lane):
            deps = parse_dep_refs(content.get("body") or "")
            if deps:
                unsatisfied, _ = dep_gate(deps, number_lane, number_state, done_lane)
                if unsatisfied:
                    findings.append(
                        f"#{number} depends on {fmt_deps(unsatisfied)} — not in the Done "
                        f"lane (item is {lane})")

    if findings:
        print(f"Findings ({len(findings)}):")
        for f in findings:
            print(f"- {f}")
    else:
        print("Findings: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
