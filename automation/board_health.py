#!/usr/bin/env python3
"""Board health report for the Daily News GitHub project.

Read-only snapshot of board + run state, pointing out items that need a human
or a re-drag. Exit code is always 0; findings are printed as a plain list.

Usage: python3 automation/board_health.py

Checks: lane counts and progress, stale In Progress items (no active run),
Blocked items with no known blocker, unsatisfied dependencies in started
lanes, and In Review items whose review run is not active.
"""

import json
import os
import sys

from board_poller import (  # same-dir import
    ROOT,
    dep_gate,
    fetch_project,
    fetch_runs_by_message,
    find_ship_pr,
    fmt_deps,
    gh,
    load_config,
    parse_dep_refs,
    run_status_for,
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
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    run_lookup = fetch_runs_by_message(env)
    if isinstance(run_lookup, tuple):
        runs_by_msg, runs_ok = run_lookup
    else:  # Compatibility with older integrations that return only the mapping.
        runs_by_msg, runs_ok = run_lookup, True
    if not runs_ok:
        print("ERROR: Archon run status unavailable; retry the health check")
        return 0

    lane_names = {v: k for k, v in cfg["lanes"].items()}
    done_lane = lane_names.get("done")
    todo_lane = lane_names.get("todo")
    blocked_lane = lane_names.get("blocked")
    needs_input_lane = lane_names.get("needs_input")
    in_progress_lane = lane_names.get("in_progress")
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
        rec = state.get(item["id"], {})
        labels = [n["name"] for n in content["labels"]["nodes"]]

        if lane == in_progress_lane:
            msg = rec.get("dispatch_msg")
            if not msg:
                findings.append(
                    f"#{number} in {in_progress_lane} with no dispatch record — re-drag to Todo")
            else:
                status = run_status_for(runs_by_msg, msg)
                if status not in ("running", "pending", "queued", "completed"):
                    findings.append(
                        f"#{number} in {in_progress_lane} with no active run "
                        f"(status {status or 'unknown'}) — re-drag to Todo or move it")
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
