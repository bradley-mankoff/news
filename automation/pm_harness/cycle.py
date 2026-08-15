"""One board reconciliation cycle and its transition gates."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass

from .archon import (
    fetch_workflow_run,
    fetch_workflow_runs,
    latest_workflow_run,
    runs_by_message_from,
)
from .dispatch import (
    dispatch,
    prepare_dispatch_budget,
    remaining_dispatch_budget,
    resume_issue,
)
from .github import (
    branch_empty_vs_main,
    comment_issue,
    fetch_project,
    fetch_verdict,
    find_issue_pr,
    find_or_create_ship_pr,
    find_ship_pr,
    issue_has_label,
    merge_pr_to_base,
    move_to_lane,
    note_capacity_deferred,
    note_integration_blocked,
    post_ready_for_review_comment,
    sync_runnable_labels,
    try_merge_base_into_head,
)
from .model import (
    ACTIVE_WORKFLOW_STATUSES,
    WorkflowRuns,
    WorkflowRunStatusMap,
    issue_number_from_message,
)
from .policy import (
    conflict_episode_action,
    dep_gate,
    develop_conflict_action,
    fmt_deps,
    is_decision_only,
    parse_dep_refs,
    pick_workflow,
    run_status_for,
)
from .recovery import (
    auto_retry_transient_failure,
    fresh_issue_dispatch_guard,
    notify_workflow_recovery,
    reconcile_untracked_runs,
    update_recovery_state,
)
from .runtime import (
    DRY_RUN,
    gh,
    hydrate_state_for_items,
    log,
    run_hook,
    save_state,
)

@dataclass
class PollContext:
    cfg: dict
    env: dict
    state: dict
    project_id: str
    field_id: str
    status_options: dict
    items: list[dict]
    first_run: bool
    done_lane_name: str | None
    todo_lane_name: str | None
    blocked_lane_name: str | None
    ready_lane_name: str | None
    in_progress_lane_name: str | None
    number_lane: dict[int, str]
    number_state: dict[int, str]
    seen: set[str]
    fresh_dispatched: set[str]


def _recheck_review_dispatch(ctx: PollContext):
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options, items = ctx.status_options, ctx.items
    fresh_dispatched = ctx.fresh_dispatched
    done_lane_name = ctx.done_lane_name
    # Review-lane recheck: an issue sitting in In Review with no review run
    # on record (a transition consumed by a transient failure, or a ship-PR
    # creation that failed silently) retries the ship flow every poll.
    # review_held marks verdict-holds and failed runs so they do NOT
    # auto-redispatch — the human re-drags those after fixing findings.
    review_lane_name = next(
        (k for k, v in cfg["lanes"].items() if v == "review"), None)
    if review_lane_name:
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != review_lane_name:
                continue
            item_id = item["id"]
            if item_id in fresh_dispatched:
                continue
            rec = state.get(item_id, {})
            if rec.get("review_msg") or rec.get("review_held"):
                continue
            number = content["number"]
            result = ensure_ship_review(
                cfg, env, item_id, number, content["title"],
                project_id, field_id, status_options, done_lane_name, rec)
            if isinstance(result, tuple):
                rec["review_msg"] = result[1]
                rec["ship_pr"] = result[2]
                fresh_dispatched.add(item_id)
            state[item_id] = rec

def _unblock_dependencies(ctx: PollContext):
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options, items = ctx.status_options, ctx.items
    todo_lane_name = ctx.todo_lane_name
    blocked_lane_name = ctx.blocked_lane_name
    done_lane_name = ctx.done_lane_name
    number_lane, number_state = ctx.number_lane, ctx.number_state
    # Dep-unblock pass: dep-marked items in the Blocked lane re-check every
    # poll, so a shipped dependency releases them (moves them to Todo, which
    # dispatches on the next poll) without a manual drag.
    if todo_lane_name and blocked_lane_name and done_lane_name:
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != blocked_lane_name:
                continue
            item_id = item["id"]
            rec = state.get(item_id, {})
            if not rec.get("dep_blocked"):
                continue
            # The current body is authoritative: clearing the Depends-on line
            # (or rewiring it) must release or re-block the item immediately.
            deps = parse_dep_refs(content.get("body") or "")
            unsatisfied, cancelled = dep_gate(
                deps, number_lane, number_state, done_lane_name)
            if unsatisfied:
                noted = set(rec.get("dep_cancelled_noted") or [])
                fresh = [n for n in cancelled if n not in noted]
                for n in fresh:
                    comment_issue(
                        cfg, env, content["number"],
                        f"#{n} closed without shipping, but this issue depends on it. "
                        "Re-scope, rewire the Depends on line, or close this issue.")
                    noted.add(n)
                if fresh:
                    rec["dep_cancelled_noted"] = sorted(noted)
                    state[item_id] = rec
                continue
            option_id = status_options.get(todo_lane_name)
            if option_id and move_to_lane(
                    cfg, env, project_id, item_id, field_id, option_id):
                if deps:
                    comment_issue(
                        cfg, env, content["number"],
                        f"Dependencies satisfied ({fmt_deps(deps)} in Done). Moved to Todo.")
                rec.pop("dep_blocked", None)
                rec.pop("dep_cancelled_noted", None)
                state[item_id] = rec
                log(f"DEP-UNBLOCKED item={item_id} issue={content['number']} -> {todo_lane_name}")

def _reconcile_completions(
    ctx: PollContext,
) -> tuple[WorkflowRuns | None, dict[str, str] | None]:
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options = ctx.status_options
    fresh_dispatched = ctx.fresh_dispatched
    ready_lane_name = ctx.ready_lane_name
    # Completion reconciliation: when a dispatched run finishes, merge the
    # feature PR into develop and move the item to Ready for Review.
    complete_move_to = cfg["dispatch"]["todo"].get("complete_move_to")
    in_progress_name = next(
        (k for k, v in cfg["lanes"].items() if v == "in_progress"), None)
    runs_snapshot: WorkflowRuns | None = None
    runs_by_msg = None
    if complete_move_to and in_progress_name:
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or item_id in fresh_dispatched:
                continue
            msg = rec.get("dispatch_msg") or rec.get("completed_dispatch_msg")
            awaiting_integration = bool(rec.get("awaiting_integration"))
            if (
                rec.get("status") != in_progress_name
                or (not msg and not awaiting_integration)
            ):
                continue
            issue_number = rec.get("issue_number")
            run = None
            if awaiting_integration:
                run_status = "completed"
            else:
                direct_run = fetch_workflow_run(env, rec.get("run_id"))
                if runs_snapshot is None:
                    fetched = fetch_workflow_runs(env)
                    runs_snapshot = (
                        fetched
                        if isinstance(fetched, WorkflowRuns)
                        else WorkflowRuns(list(fetched or []))
                    )
                    runs_by_msg = runs_by_message_from(runs_snapshot)
                lookup_error = getattr(runs_snapshot, "error", None)
                if lookup_error:
                    # A direct lookup for the persisted run is targeted evidence;
                    # an incomplete list is not evidence that no newer run exists.
                    runs_by_msg = None
                    if not direct_run:
                        log(
                            f"RUN LOOKUP UNAVAILABLE: {lookup_error}; "
                            "retaining run markers"
                        )
                        state[item_id] = rec
                        continue
                newest_run = None
                if msg:
                    newest_run = latest_workflow_run(
                        runs_snapshot, message=msg)
                if newest_run is None and issue_number:
                    newest_run = latest_workflow_run(
                        runs_snapshot, issue_number=issue_number)
                run = direct_run or newest_run
                if not run:
                    log(
                        f"RUN LOOKUP DEFERRED item={item_id} "
                        f"issue={issue_number or '?'} "
                        f"error={lookup_error or 'not found'}"
                    )
                    state[item_id] = rec
                    continue
                rec["run_id"] = str(run.get("id") or "")
                rec["last_observed_run_id"] = rec["run_id"]
                run_status = str(run.get("status") or "").lower()
            if run_status == "completed":
                issue_number = rec.get("issue_number")
                needs_input_name = next(
                    (k for k, v in cfg["lanes"].items() if v == "needs_input"), None)
                if (issue_number and needs_input_name
                        and issue_has_label(cfg, env, issue_number, "needs-input")):
                    option_id = status_options.get(needs_input_name)
                    if option_id and move_to_lane(
                            cfg, env, project_id, item_id, field_id, option_id):
                        log(f"NEEDS INPUT item={item_id} issue={issue_number} -> "
                            f"{needs_input_name} (awaiting human input)")
                    rec.pop("dispatch_msg", None)
                    continue
                merge_base = cfg["dispatch"]["todo"].get("merge_develop_base")
                merge_ok = False
                pr = None
                pr_num = None
                if not issue_number or not merge_base:
                    if issue_number:
                        note_integration_blocked(
                            cfg, env, issue_number, rec,
                            "the integration branch is not configured.")
                    state[item_id] = rec
                    continue
                pr, pr_ok = find_issue_pr(
                    cfg, env, issue_number, base=merge_base)
                if not pr_ok:
                    log(f"DEVELOP MERGE DEFERRED issue={issue_number}: "
                        "PR lookup failed; retrying next poll")
                    state[item_id] = rec
                    continue
                if not pr:
                    note_integration_blocked(
                        cfg, env, issue_number, rec,
                        "no linked integration PR was found.")
                    state[item_id] = rec
                    continue
                pr_num = pr.get("number")
                merge_ok, note = merge_pr_to_base(
                    cfg, env, pr, merge_base, issue_number)
                log(
                    f"DEVELOP MERGE issue={issue_number} PR=#{pr_num}: {note}"
                    if merge_ok else
                    f"DEVELOP MERGE FAILED issue={issue_number}: {note}"
                )
                if merge_ok and not DRY_RUN:
                    merged_pr, verify_ok = find_issue_pr(
                        cfg, env, issue_number, base=merge_base)
                    if (
                        not verify_ok
                        or not merged_pr
                        or merged_pr.get("state") != "MERGED"
                    ):
                        note_integration_blocked(
                            cfg, env, issue_number, rec,
                            f"integration PR #{pr_num} is not confirmed merged.")
                        state[item_id] = rec
                        continue
                    pr = merged_pr
                    pr_num = merged_pr.get("number")
                if merge_ok:
                    rec.pop("integration_blocked", None)
                    rec.pop("awaiting_integration", None)
                if not merge_ok:
                    # Develop-merge conflict gate: mirror the ship-lane state
                    # machine. Episode markers: dev_conflict_mech /
                    # dev_conflict_fix_msg / dev_conflict_noted. The outer
                    # merge retries every poll; this block only escalates.
                    dev_wf = cfg["dispatch"]["todo"].get("conflict_fix_workflow")
                    head = (pr or {}).get("headRefName")
                    if (issue_number and pr_num and head and dev_wf
                            and runs_by_msg is not None):
                        fix_msg = rec.get("dev_conflict_fix_msg")
                        act = develop_conflict_action(
                            bool(rec.get("dev_conflict_mech")), fix_msg,
                            (run_status_for(runs_by_msg, fix_msg)
                             if fix_msg else None),
                            retried=bool(rec.get("dev_conflict_retried")))
                        if act == "mech":
                            ok, note = try_merge_base_into_head(
                                cfg, env, pr_num, head, merge_base)
                            log(f"DEVELOP CONFLICT MECHANICAL PR #{pr_num} "
                                f"issue={issue_number}: {note}")
                            if not ok and note == "conflict":
                                rec["dev_conflict_mech"] = True
                        elif act == "dispatch":
                            if _conflict_fixer_active(
                                    runs_by_msg, _POLL_CONFLICT_FIXERS):
                                if not rec.get("dev_conflict_deferred"):
                                    log(f"DEVELOP CONFLICT DISPATCH DEFERRED PR #{pr_num} "
                                        f"issue={issue_number} — another conflict fix "
                                        "run is active")
                                    rec["dev_conflict_deferred"] = True
                            else:
                                dmsg = (f"Resolve merge conflicts on develop PR #{pr_num} "
                                        f"(issue #{issue_number}).")
                                if dispatch(cfg, env, dev_wf,
                                            f"fix-develop-issue-{issue_number}", dmsg,
                                            item_id, issue_number):
                                    _POLL_CONFLICT_FIXERS.add(dmsg)
                                    rec["dev_conflict_fix_msg"] = dmsg
                                    rec["dev_conflict_noted"] = False
                                    rec.pop("dev_conflict_deferred", None)
                                    comment_issue(
                                        cfg, env, issue_number,
                                        f"Develop PR #{pr_num} has merge conflicts. "
                                        f"Resolving automatically (merging develop into "
                                        f"the branch, then {dev_wf} if needed).")
                                    log(f"DEVELOP CONFLICT DISPATCH PR #{pr_num} "
                                        f"issue={issue_number} wf={dev_wf}")
                        elif act == "retry":
                            if _conflict_fixer_active(
                                    runs_by_msg, _POLL_CONFLICT_FIXERS):
                                if not rec.get("dev_conflict_deferred"):
                                    log(f"DEVELOP CONFLICT RETRY DEFERRED PR #{pr_num} "
                                        f"issue={issue_number} — another conflict fix "
                                        "run is active")
                                    rec["dev_conflict_deferred"] = True
                            else:
                                dmsg = (f"Resolve merge conflicts on develop PR #{pr_num} "
                                        f"(issue #{issue_number}). "
                                        f"Retry {int(bool(rec.get('dev_conflict_retried'))) + 1}.")
                                if dispatch(cfg, env, dev_wf,
                                            f"fix-develop-issue-{issue_number}", dmsg,
                                            item_id, issue_number):
                                    _POLL_CONFLICT_FIXERS.add(dmsg)
                                    rec["dev_conflict_fix_msg"] = dmsg
                                    rec["dev_conflict_noted"] = False
                                    rec["dev_conflict_retried"] = True
                                    rec.pop("dev_conflict_deferred", None)
                                    log(f"DEVELOP CONFLICT RETRY DISPATCH PR #{pr_num} "
                                        f"issue={issue_number} wf={dev_wf}")
                        elif act == "failed":
                            if not rec.get("dev_conflict_noted"):
                                comment_issue(
                                    cfg, env, issue_number,
                                    f"Could not resolve the merge conflicts on develop "
                                    f"PR #{pr_num} automatically — the fix run finished "
                                    "but the PR is still conflicting. Merge develop into "
                                    "the branch manually (or rewrite the conflicting "
                                    "lines); the poller will merge automatically once the "
                                    "branch is mergeable.")
                                rec["dev_conflict_noted"] = True
                            log(f"DEVELOP CONFLICT UNRESOLVED PR #{pr_num} "
                                f"issue={issue_number} — needs human")
                        # "active" falls through: keep the markers, retry next poll.
                        state[item_id] = rec
                        log(f"left item={item_id} in {in_progress_name} "
                            "(develop merge conflict episode active)")
                        continue
                    log(f"left item={item_id} in {in_progress_name} (develop merge failed)")
                    rec.pop("dispatch_msg", None)
                    continue
                if merge_ok:
                    for marker in (
                        "dev_conflict_mech",
                        "dev_conflict_fix_msg",
                        "dev_conflict_noted",
                        "dev_conflict_deferred",
                    ):
                        rec.pop(marker, None)
                    log(run_hook(cfg, "after_integration_merge"))
                option_id = status_options.get(complete_move_to)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    rec["completed_dispatch_msg"] = msg
                    rec.pop("awaiting_integration", None)
                    rec.pop("dispatch_msg", None)
                    if complete_move_to == ready_lane_name and issue_number:
                        if pr_num is not None:
                            rec["develop_pr"] = pr_num
                        posted = post_ready_for_review_comment(
                            cfg, env, issue_number, merge_base or "develop", pr_num)
                        if posted:
                            rec["ready_test_comment"] = True
                            log(f"READY TEST COMMENTED issue={issue_number}")
                        else:
                            rec.pop("ready_test_comment", None)
                            log(f"READY TEST COMMENT DEFERRED issue={issue_number}")
                    log(f"MOVED item={item_id} -> {complete_move_to} (run completed)")
                elif option_id is None:
                    log(f"MOVE SKIPPED item={item_id}: lane '{complete_move_to}' not on board")
                else:
                    log(f"MOVE FAILED item={item_id} -> {complete_move_to}")
            elif run_status in ("failed", "cancelled"):
                issue_number = rec.get("issue_number")
                run_id = (
                    str(run.get("id")).strip()
                    if isinstance(run, dict)
                    and isinstance(run.get("id"), str)
                    and str(run.get("id")).strip()
                    else ""
                )
                if not run_id:
                    # Status can race ahead of a full record. Keep the marker
                    # and wait for a known run identity.
                    state[item_id] = rec
                    continue
                if (
                    rec.get("dispatch_baseline_run_id") == run_id
                    and not rec.get("retrying")
                ):
                    # New episode still points at the prior terminal row.
                    state[item_id] = rec
                    continue
                if (
                    rec.get("retrying")
                    and (rec.get("recovery") or {}).get("run_id") == run_id
                ):
                    # Retry was dispatched; wait for a newer run row.
                    state[item_id] = rec
                    continue
                matching_runs = [
                    candidate for candidate in (runs_snapshot or [])
                    if (
                        (
                            issue_number is not None
                            and issue_number_from_message(
                                str(candidate.get("user_message") or "")
                            ) == issue_number
                        )
                        or (
                            msg
                            and msg in str(candidate.get("user_message") or "")
                        )
                    )
                ]
                if not matching_runs:
                    matching_runs = [run]
                details, worktree, action = update_recovery_state(
                    rec,
                    run,
                    branch=rec.get("branch"),
                    attempt_count=len(matching_runs) or 1,
                )
                if issue_number and auto_retry_transient_failure(
                        cfg, env, item_id, issue_number, rec, details,
                        worktree, matching_runs):
                    if rec.get("recovery_logged_run_id") != run_id:
                        log(
                            f"RUN {run_status.upper()} item={item_id} "
                            f"issue={issue_number}: "
                            f"{details.get('failure_class') or 'unknown'} "
                            "failure, clean worktree — automatic retry "
                            f"dispatched (run {run_id})"
                        )
                        rec["recovery_logged_run_id"] = run_id
                else:
                    if (
                        action == "retry_available"
                        and (
                            not issue_number
                            or not rec.get("wf")
                        )
                    ):
                        action = "manual_review"
                        rec.setdefault("recovery", {})["action"] = action
                    if issue_number:
                        notify_workflow_recovery(
                            cfg, env, issue_number, rec, details, worktree, action
                        )
                    if rec.get("recovery_logged_run_id") != run_id:
                        log(
                            f"RUN {run_status.upper()} item={item_id} "
                            f"issue={issue_number or '?'}: "
                            f"{details.get('failure_class') or 'unknown'} -> "
                            f"{action} (run {run_id})"
                        )
                        rec["recovery_logged_run_id"] = run_id
                state[item_id] = rec
    return runs_snapshot, runs_by_msg


def _enforce_ready_proof(ctx: PollContext, in_progress_name: str | None):
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options, items = ctx.status_options, ctx.items
    ready_lane_name = ctx.ready_lane_name
    first_run = ctx.first_run
    # Ready is truthful only after a linked PR is merged into the integration
    # branch. Manual moves and stale completion markers are bounced once and
    # rechecked from In Progress until that proof exists.
    if ready_lane_name and not first_run:
        ready_base = cfg["dispatch"]["todo"].get("merge_develop_base", "develop")
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != ready_lane_name:
                continue
            item_id = item["id"]
            rec = state.get(item_id, {})
            issue_number = content["number"]
            pr, pr_ok = find_issue_pr(
                cfg, env, issue_number, base=ready_base)
            if not pr_ok:
                log(f"READY PROOF UNAVAILABLE issue={issue_number}")
                continue
            if not pr or pr.get("state") != "MERGED":
                reason = (
                    "no linked integration PR was found."
                    if not pr else
                    f"integration PR #{pr.get('number')} is not merged."
                )
                note_integration_blocked(
                    cfg, env, issue_number, rec, reason)
                option_id = status_options.get(in_progress_name)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    rec["status"] = in_progress_name
                    rec["awaiting_integration"] = True
                    rec["issue_number"] = issue_number
                    rec.pop("ready_test_comment", None)
                    state[item_id] = rec
                    log(f"FALSE READY BOUNCED issue={issue_number} -> "
                        f"{in_progress_name}")
                continue
            rec.pop("integration_blocked", None)
            rec["develop_pr"] = pr.get("number")
            if rec.get("ready_test_comment"):
                state[item_id] = rec
                continue
            if post_ready_for_review_comment(
                    cfg, env, issue_number, ready_base, pr.get("number")):
                rec["issue_number"] = issue_number
                rec["ready_test_comment"] = True
                state[item_id] = rec
                log(f"READY TEST COMMENTED issue={issue_number} (recheck)")

def _complete_reviews(ctx: PollContext, runs_snapshot: WorkflowRuns | None, runs_by_msg: dict[str, str] | None):
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options, items = ctx.status_options, ctx.items
    fresh_dispatched = ctx.fresh_dispatched
    # A verdict is the ship gate. Once `VERDICT: approve` exists, merge does
    # not depend on the fragile Archon run-list projection.
    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
    review_lane_name = next(
        (k for k, v in cfg["lanes"].items() if v == "review"), None)
    done_name = cfg["dispatch"]["review"].get("done_lane", "Done")
    if (review_lane_name and done_name
            and cfg["dispatch"]["review"].get("merge_ship_on_approve")):
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or item_id in fresh_dispatched:
                continue
            if rec.get("status") != review_lane_name:
                continue
            issue_number = rec.get("issue_number")
            ship_num = rec.get("ship_pr")
            if not ship_num and issue_number:
                found = find_ship_pr(cfg, env, issue_number, ship_to)
                ship_num = found.get("number") if found else None
                if ship_num:
                    rec["ship_pr"] = ship_num
            if not ship_num:
                continue
            result = gh(
                ["pr", "view", str(ship_num), "-R", cfg["repo"],
                 "--json", "number,state,baseRefName"],
                env,
            )
            if result.returncode != 0:
                log(f"SHIP PR #{ship_num} unreadable for item={item_id}")
                continue
            try:
                ship = json.loads(result.stdout)
            except ValueError:
                log(f"SHIP PR #{ship_num} returned invalid JSON")
                continue
            verdict, verdict_ok = fetch_verdict(cfg, env, ship_num)
            if not verdict_ok:
                log(f"SHIP VERDICT UNREADABLE PR #{ship_num}; retrying next poll")
                continue
            if verdict == "approve":
                if ship.get("state") != "MERGED" and not DRY_RUN:
                    merged = gh(
                        ["pr", "merge", str(ship_num), "-R", cfg["repo"], "--merge"],
                        env,
                    )
                    if merged.returncode != 0:
                        log(f"SHIP MERGE FAILED PR #{ship_num}: "
                            f"{merged.stderr.strip()[:300]}")
                        continue
                    log(f"SHIPPED PR #{ship_num} -> {ship_to} (approved)")
                elif DRY_RUN:
                    log(f"[dry-run] MERGE PR #{ship_num} -> {ship_to}")
                if issue_number and not DRY_RUN:
                    gh(["issue", "close", str(issue_number), "-R", cfg["repo"]], env)
                    log(f"CLOSED issue #{issue_number} (shipped)")
                option_id = status_options.get(done_name)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    log(f"MOVED item={item_id} -> {done_name} (shipped)")
                for marker in (
                    "review_msg",
                    "review_run_id",
                    "review_held",
                    "review_held_notice",
                    "ship_pr",
                ):
                    rec.pop(marker, None)
                state[item_id] = rec
                continue

            rmsg = rec.get("review_msg")
            review_run = fetch_workflow_run(env, rec.get("review_run_id"))
            verdict_holds_review = verdict in {"request-changes", "block"}
            if rmsg and not verdict_holds_review:
                if runs_snapshot is None:
                    fetched = fetch_workflow_runs(env)
                    runs_snapshot = (
                        fetched
                        if isinstance(fetched, WorkflowRuns)
                        else WorkflowRuns(list(fetched or []))
                    )
                    runs_by_msg = runs_by_message_from(runs_snapshot)
                lookup_error = getattr(runs_snapshot, "error", None)
                if lookup_error and not review_run:
                    log(
                        f"RUN LOOKUP UNAVAILABLE: {lookup_error}; "
                        "retaining review run markers"
                    )
                    state[item_id] = rec
                    continue
                if not lookup_error:
                    newest_review = latest_workflow_run(
                        runs_snapshot, message=rmsg)
                    review_run = review_run or newest_review
            rstatus = str((review_run or {}).get("status") or "").lower()
            if review_run:
                rec["review_run_id"] = str(review_run.get("id") or "")
            held = verdict in {"request-changes", "block"}
            held = held or (rstatus == "completed" and verdict is None)
            if held:
                held_value = verdict or "none"
                # Log SHIP HELD once per hold episode (rising edge of the
                # notice marker), not every poll: the item stays held until a
                # human re-drag or a conflict-clear requeue.
                if rec.get("review_held_notice") != held_value:
                    if issue_number and not comment_issue(
                            cfg, env, issue_number,
                            f"Ship review did not approve (VERDICT: {held_value}). "
                            "Fix the findings, then drag the issue back to In Review "
                            "to re-review."):
                        continue
                    rec["review_held_notice"] = held_value
                    log(f"SHIP HELD PR #{ship_num}: verdict={held_value}")
                rec["review_held"] = True
                rec.pop("review_msg", None)
                state[item_id] = rec
            elif rstatus in {"failed", "cancelled"}:
                details, worktree, _action = update_recovery_state(
                    rec, review_run, branch=rec.get("branch"))
                rec.setdefault("recovery", {})["action"] = "manual_review"
                if issue_number:
                    notify_workflow_recovery(
                        cfg, env, issue_number, rec, details, worktree,
                        "manual_review")
                rec["review_held"] = True
                run_id = str((review_run or {}).get("id") or "")
                if rec.get("review_failed_logged_run_id") != run_id:
                    log(f"REVIEW {rstatus.upper()} item={item_id}; "
                        f"left in {review_lane_name} for human re-drag")
                    rec["review_failed_logged_run_id"] = run_id
                state[item_id] = rec
    return runs_snapshot, runs_by_msg, review_lane_name, ship_to


def _remediate_ship_conflicts(ctx: PollContext, runs_by_msg: dict[str, str] | None, review_lane_name: str | None, ship_to: str):
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    # Ship-conflict remediation: a CONFLICTING ship PR blocks the verdict-gated
    # merge. Try a mechanical base-into-head merge first (free, instant); on
    # real conflicts dispatch the dedicated fix workflow once per episode
    # (markers: conflict_fix_msg / conflict_mech_failed / conflict_fix_noted).
    # Episodes end when the PR is mergeable; a fix run that finishes with the
    # PR still conflicting posts a human-help comment once.
    fix_wf = cfg["dispatch"]["review"].get("conflict_fix_workflow")
    if review_lane_name and fix_wf:
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or rec.get("status") != review_lane_name:
                continue
            issue_number = rec.get("issue_number")
            if not issue_number:
                continue
            ship = find_ship_pr(cfg, env, issue_number, ship_to)
            if not ship:
                continue
            ship_num = ship.get("number")
            mergeable = ship.get("mergeable") or "UNKNOWN"
            # Never remediate while a review run is active — its sync/fix
            # nodes also write to the branch; concurrent writers race.
            rmsg = rec.get("review_msg")
            if runs_by_msg is None and (
                    rmsg or rec.get("conflict_fix_msg")
                    or rec.get("conflict_mech_failed")):
                runs_by_msg = fetch_runs_by_message(env)
            lookup_error = (
                getattr(runs_by_msg, "error", None)
                if runs_by_msg is not None
                else None
            )
            if lookup_error:
                log(
                    f"RUN LOOKUP UNAVAILABLE: {lookup_error}; "
                    "retaining run markers"
                )
                continue
            review_active = bool(
                rmsg
                and runs_by_msg
                and run_status_for(runs_by_msg, rmsg)
                in ACTIVE_WORKFLOW_STATUSES
            )
            if review_active:
                continue
            fix_msg = rec.get("conflict_fix_msg")
            mech_failed = bool(rec.get("conflict_mech_failed"))
            fix_status = (
                run_status_for(runs_by_msg, fix_msg)
                if fix_msg and runs_by_msg
                else None
            )
            retried = bool(rec.get("conflict_fix_retried"))
            action = conflict_episode_action(
                mergeable, fix_msg, fix_status, mech_failed, retried=retried)
            if action == "update":
                ok, note = try_merge_base_into_head(
                    cfg, env, ship_num, ship.get("headRefName") or "", ship_to)
                log(f"SHIP CONFLICT UPDATE PR #{ship_num}: {note}")
                if not ok and note == "conflict":
                    rec["conflict_mech_failed"] = True
                    action = "dispatch"
                elif not ok:
                    action = "wait"  # transient error — retry next poll
            if action == "dispatch":
                if _conflict_fixer_active(runs_by_msg, _POLL_CONFLICT_FIXERS):
                    if not rec.get("conflict_fix_deferred"):
                        log(f"SHIP CONFLICT DISPATCH DEFERRED PR #{ship_num} "
                            f"issue={issue_number} — another conflict fix run is active")
                        rec["conflict_fix_deferred"] = True
                else:
                    msg = (f"Resolve merge conflicts on ship PR #{ship_num} "
                           f"(issue #{issue_number}).")
                    branch = f"fix-ship-issue-{issue_number}"
                    if dispatch(cfg, env, fix_wf, branch, msg, item_id, issue_number):
                        _POLL_CONFLICT_FIXERS.add(msg)
                        rec["conflict_fix_msg"] = msg
                        rec["conflict_fix_noted"] = False
                        rec.pop("conflict_fix_deferred", None)
                        comment_issue(
                            cfg, env, issue_number,
                            f"Ship PR #{ship_num} has merge conflicts. Resolving automatically "
                            f"(merging main into the branch, then {fix_wf} if needed).")
                        log(f"SHIP CONFLICT DISPATCH PR #{ship_num} issue={issue_number} "
                            f"wf={fix_wf}")
            elif action == "retry":
                # Fix run finished terminal but the PR is still conflicting.
                # Give the resolver one bounded re-dispatch before escalating:
                # transport drops (WebSocket 1006 etc.) kill a healthy run
                # mid-resolution, and the LLM must be allowed to finish.
                if _conflict_fixer_active(runs_by_msg, _POLL_CONFLICT_FIXERS):
                    if not rec.get("conflict_fix_deferred"):
                        log(f"SHIP CONFLICT RETRY DEFERRED PR #{ship_num} "
                            f"issue={issue_number} — another conflict fix run is active")
                        rec["conflict_fix_deferred"] = True
                else:
                    msg = (f"Resolve merge conflicts on ship PR #{ship_num} "
                           f"(issue #{issue_number}). Retry {int(retried) + 1}.")
                    branch = f"fix-ship-issue-{issue_number}"
                    if dispatch(cfg, env, fix_wf, branch, msg, item_id, issue_number):
                        _POLL_CONFLICT_FIXERS.add(msg)
                        rec["conflict_fix_msg"] = msg
                        rec["conflict_fix_noted"] = False
                        rec["conflict_fix_retried"] = True
                        rec.pop("conflict_fix_deferred", None)
                        log(f"SHIP CONFLICT RETRY DISPATCH PR #{ship_num} "
                            f"issue={issue_number} wf={fix_wf}")
                    else:
                        log(f"SHIP CONFLICT RETRY DISPATCH FAILED PR #{ship_num} "
                            f"issue={issue_number} — retrying next poll")
            elif action == "failed":
                if not rec.get("conflict_fix_noted"):
                    if comment_issue(
                            cfg, env, issue_number,
                            f"Could not resolve the merge conflicts on ship PR #{ship_num} "
                            "automatically — the fix run finished but the PR is still "
                            "conflicting. Merge main into the branch manually (or rewrite "
                            "the conflicting lines), then drag the issue back to In Review."):
                        rec["conflict_fix_noted"] = True
                    else:
                        log(f"SHIP CONFLICT HELP NOTICE FAILED issue={issue_number}; "
                            "will retry next poll")
                log(f"SHIP CONFLICT UNRESOLVED PR #{ship_num} issue={issue_number} "
                    "— needs human")
            elif action == "clear":
                log(f"SHIP CONFLICT RESOLVED PR #{ship_num} issue={issue_number}")
                verdict, verdict_ok = fetch_verdict(cfg, env, ship_num)
                if verdict_ok and verdict is None and rec.get("review_held"):
                    rec.pop("review_held", None)
                    rec.pop("review_held_notice", None)
                    rec.pop("review_msg", None)
                    rec.pop("review_run_id", None)
                    rec["ship_pr"] = ship_num
                    log(f"SHIP REVIEW REQUEUED PR #{ship_num} issue={issue_number}")
                rec.pop("conflict_fix_msg", None)
                rec.pop("conflict_mech_failed", None)
                rec.pop("conflict_fix_noted", None)
                rec.pop("conflict_fix_deferred", None)
                rec.pop("conflict_fix_retried", None)
            state[item_id] = rec

def ensure_ship_review(cfg: dict, env: dict, item_id: str, issue_number: int,
                       title: str, project_id: str, field_id: str,
                       status_options: dict, done_name: str | None,
                       rec: dict) -> tuple[str, str, int] | str | None:
    """Open/verify the ship PR for an issue in the review lane and dispatch
    the review workflow.

    Returns ("ok", msg, ship_num) when a review run was dispatched;
    "shipped" when the branch has no commits beyond main (issue closed and
    moved to Done — nothing left to review); None when nothing could be done
    this poll (logged; the recheck pass retries).
    """
    merge_base = cfg["dispatch"]["todo"].get("merge_develop_base", "develop")
    pr, _ok = find_issue_pr(cfg, env, issue_number, base=merge_base)
    if pr:
        ok, note = merge_pr_to_base(cfg, env, pr, merge_base, issue_number)
        log(f"DEVELOP MERGE issue={issue_number} PR=#{pr['number']}: {note}"
            if ok else
            f"DEVELOP MERGE FAILED issue={issue_number}: {note}")
    head = (pr or {}).get("headRefName") or f"archon/task-issue-{issue_number}"
    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
    if branch_empty_vs_main(cfg, env, head, ship_to):
        if DRY_RUN:
            log(f"[dry-run] ALREADY SHIPPED issue={issue_number} head={head}")
        else:
            comment_issue(
                cfg, env, issue_number,
                "Closing as shipped: this branch has no commits beyond main — "
                "its work already reached main via an earlier ship PR's develop "
                "merge. No review needed.")
            gh(["issue", "close", str(issue_number), "-R", cfg["repo"]], env)
            log(f"ALREADY SHIPPED issue={issue_number} head={head} -> {done_name}")
        if done_name:
            option_id = status_options.get(done_name)
            if option_id:
                move_to_lane(cfg, env, project_id, item_id, field_id, option_id)
        rec.pop("review_msg", None)
        rec.pop("ship_pr", None)
        rec.pop("review_held", None)
        return "shipped"
    ship = find_or_create_ship_pr(
        cfg, env, head, f"Ship: {title} (#{issue_number})", issue_number, ship_to)
    if not ship:
        log(f"SHIP PR UNAVAILABLE issue={issue_number} head={head} — retrying next poll")
        return None
    msg = (f"Review PR #{ship['number']} (ship to {ship_to} for issue "
           f"#{issue_number}: {title}).")
    branch = f"review/issue-{issue_number}"
    result = dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"],
                      branch, msg, item_id, issue_number)
    if not result:
        if getattr(result, "reason", "") == "capacity":
            # Expected backpressure, not a failure: the budget gate already
            # logged DISPATCH DEFERRED; surface the hold on the issue once
            # and retry next poll without touching any review state.
            note_capacity_deferred(cfg, env, issue_number, rec)
            return None
        log(f"SHIP REVIEW DISPATCH FAILED issue={issue_number} — retrying next poll")
        return None
    rec.pop("review_held", None)
    return "ok", msg, ship["number"]


def _dispatch_guard_is_deferred(reason: str) -> bool:
    """Identify guard failures caused by unavailable safety lookups."""
    return reason.startswith(
        (
            "Archon run lookup unavailable:",
            "Archon worktree lookup unavailable:",
        )
    )

def _fill_concurrency_gap(
    ctx: PollContext,
    items: list[dict],
    number_lane: dict[int, str],
    number_state: dict[int, str],
) -> int:
    """Desire full throughput: promote runnable Backlog items to Todo when
    the dispatch budget has free slots, and log the diagnosis when it cannot.

    The poller's dispatch loop only starts work on the Backlog->Todo lane
    transition (the PM was the sole promoter, so idle capacity went unused
    until a human nudged). This pass makes the system self-desiring: it
    fills free slots from runnable Backlog items and records why slots stay
    empty (no runnable items, dep-blocked, decision-only, needs-input,
    closed) so the gap is visible and fixable.

    Mutates `item['status']` in place so the next poll's dispatch loop sees
    the Todo transition. This helper runs after the current poll's
    transition/dispatch loop, so promoted items dispatch on the next poll.

    Returns the number of items promoted.
    """
    cfg, env, state = ctx.cfg, ctx.env, ctx.state
    project_id, field_id = ctx.project_id, ctx.field_id
    status_options = ctx.status_options
    todo_lane = ctx.todo_lane_name
    done_lane = ctx.done_lane_name
    if not todo_lane or not done_lane:
        return 0
    limit = cfg.get("max_concurrent_workflows", 10)
    # Budget was computed at poll start as (limit - active) and decremented
    # per dispatch; whatever remains is free capacity this poll.
    budget = remaining_dispatch_budget()
    if budget is None or budget <= 0:
        return 0
    free = min(int(budget), int(limit))

    backlog = [
        item for item in items
        if (item.get("content") or {}).get("__typename") == "Issue"
        and (item.get("content") or {}).get("repository", {}).get(
            "nameWithOwner") == cfg["repo"]
        and item.get("status") == cfg.get("default_lane", "Backlog")
    ]
    promoted = 0
    move_failed = 0
    lane_unavailable = 0
    for item in sorted(
        backlog,
        key=lambda it: (it.get("content") or {}).get("number") or 0,
    ):
        if promoted >= free:
            break
        content = item["content"]
        number = content["number"]
        labels = [
            node["name"] for node in (content.get("labels") or {}).get("nodes", [])
        ]
        if is_decision_only(cfg, labels):
            continue
        if "needs-input" in labels:
            continue
        if content.get("state") != "OPEN":
            continue
        deps = parse_dep_refs(content.get("body") or "")
        if deps:
            unsatisfied, _ = dep_gate(
                deps, number_lane, number_state, done_lane)
            if unsatisfied:
                continue
        item_id = item["id"]
        option_id = status_options.get(todo_lane)
        if option_id is None:
            lane_unavailable += 1
            log(f"CONCURRENCY FILL MOVE SKIPPED issue={number}: "
                f"lane '{todo_lane}' is not on board")
            continue
        try:
            moved = move_to_lane(
                cfg, env, project_id, item_id, field_id, option_id)
        except (OSError, subprocess.SubprocessError) as exc:
            move_failed += 1
            log(f"CONCURRENCY FILL MOVE FAILED issue={number}: "
                f"{type(exc).__name__}: {exc}")
            continue
        if not moved:
            move_failed += 1
            log(f"CONCURRENCY FILL MOVE FAILED issue={number}: "
                "GitHub rejected the lane update")
            continue
        item["status"] = todo_lane  # next poll sees the transition
        rec = state.setdefault(item_id, {})
        rec["issue_number"] = number
        log(f"CONCURRENCY FILL issue={number} -> {todo_lane} "
            f"(free={free - promoted}/{limit})")
        promoted += 1

    remaining_backlog = [
        item for item in backlog
        if item.get("status") == cfg.get("default_lane", "Backlog")
    ]
    if promoted < free and remaining_backlog:
        # Diagnose why the pipeline cannot fill: surface the blockers so
        # the factory can be fed (file issues, unblock deps, split scope).
        decision = needs_input = dep_blocked = closed = 0
        blocked_by: dict[int, list[int]] = {}
        for item in remaining_backlog:
            content = item["content"]
            number = content["number"]
            labels = [
                node["name"]
                for node in (content.get("labels") or {}).get("nodes", [])
            ]
            if is_decision_only(cfg, labels):
                decision += 1
            elif "needs-input" in labels:
                needs_input += 1
            elif content.get("state") != "OPEN":
                closed += 1
            else:
                deps = parse_dep_refs(content.get("body") or "")
                if deps:
                    unsatisfied, _ = dep_gate(
                        deps, number_lane, number_state, done_lane)
                    if unsatisfied:
                        dep_blocked += 1
                        blocked_by[number] = unsatisfied
        detail = (
            f"CONCURRENCY GAP: free={free}, {len(remaining_backlog)} backlog items "
            f"not promoted — decision_only={decision}, needs_input={needs_input}, "
            f"closed={closed}, dep_blocked={dep_blocked}, "
            f"lane_unavailable={lane_unavailable}, move_failed={move_failed}"
        )
        if blocked_by:
            first = sorted(blocked_by.items())[0]
            detail += f" (e.g. #{first[0]} depends on {fmt_deps(first[1])})"
        log(detail)
    return promoted


def poll(cfg: dict, env: dict, state: dict) -> None:
    project_id, field_id, status_options, items = fetch_project(cfg, env)
    hydrate_state_for_items(state, items)
    prepare_dispatch_budget(cfg, env)
    first_run = not state.get("_meta", {}).get("snapshot_done")
    _POLL_CONFLICT_FIXERS.clear()

    lane_names = {v: k for k, v in cfg["lanes"].items()}
    done_lane_name = lane_names.get("done")
    todo_lane_name = lane_names.get("todo")
    blocked_lane_name = lane_names.get("blocked")
    ready_lane_name = lane_names.get("ready")
    in_progress_lane_name = lane_names.get("in_progress")

    if not first_run and in_progress_lane_name:
        reconcile_untracked_runs(
            cfg, env, state, items, in_progress_lane_name)

    # Issue number -> lane / open-state on this board, for the dep gate.
    number_lane: dict[int, str] = {}
    number_state: dict[int, str] = {}
    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        number_lane[content["number"]] = item["status"]
        number_state[content["number"]] = content.get("state") or ""

    sync_runnable_labels(
        cfg,
        env,
        items,
        number_lane,
        number_state,
        todo_lane_name,
        done_lane_name,
    )

    seen: set[str] = set()
    # Items dispatched in THIS poll: their run rows appear in `archon workflow
    # runs` asynchronously, so the completion passes must not read a stale run
    # with the same message (re-dispatches reuse the message) and pop the
    # marker before the new run registers.
    fresh_dispatched: set[str] = set()
    for item in items:
        item_id = item["id"]
        seen.add(item_id)
        content = item.get("content")
        if not content or content["__typename"] not in ("Issue", "PullRequest"):
            continue
        repo = content["repository"]["nameWithOwner"]
        if repo != cfg["repo"]:
            continue
        status_val = item["status"]
        # Convention: items on the board without a status land in the default lane.
        default_lane = cfg.get("default_lane", "Backlog")
        if status_val == "No status":
            option_id = status_options.get(default_lane)
            if option_id and move_to_lane(
                    cfg, env, project_id, item_id, field_id, option_id):
                status_val = default_lane
                log(f"NORMALIZED item={item_id} -> {default_lane}")
        rec = state.get(item_id, {})
        prev = rec.get("status")
        lane = cfg["lanes"].get(status_val)
        labels = [
            node["name"] for node in (content.get("labels") or {}).get("nodes", [])
        ]
        if (
            not first_run
            and lane == "todo"
            and content["__typename"] == "Issue"
            and is_decision_only(cfg, labels)
        ):
            number = content["number"]
            if not rec.get("decision_only_noted"):
                body = (
                    "Decision-only issues do not dispatch implementation workflows. "
                    "Record the owner decision, then close the issue; do not move it "
                    "through Todo as code work."
                )
                if comment_issue(cfg, env, number, body):
                    rec["decision_only_noted"] = True
            target = (
                done_lane_name
                if (content.get("state") or "").upper() == "CLOSED"
                else (cfg.get("decision_only") or {}).get(
                    "move_to", lane_names.get("needs_input"))
            )
            option_id = status_options.get(target) if target else None
            if option_id and move_to_lane(
                    cfg, env, project_id, item_id, field_id, option_id):
                status_val = target
                log(f"DECISION ONLY item={item_id} issue={number} -> {target}")
            rec["status"] = status_val
            rec["issue_number"] = number
            state[item_id] = rec
            continue
        dispatched_msg = None
        review_msg = None
        ship_pr_num = None
        dispatched_wf = None
        dispatched_branch = None
        dep_gate_ran = False
        dep_blocked_marker = None
        dep_cancelled_noted = None

        dispatch_guard_retry = (
            lane == "todo" and bool(rec.get("dispatch_guard_deferred"))
        )
        if not first_run and (prev != status_val or dispatch_guard_retry):
            if lane == "todo" and content["__typename"] == "Issue":
                # Dependency gate: an issue whose Depends-on refs are not all
                # in the Done lane does not dispatch; it moves to the Blocked
                # lane and the unblock pass returns it to Todo (which
                # dispatches) when the deps ship. Needs Input stays exclusive
                # to NEEDS INPUT questions.
                deps = parse_dep_refs(content.get("body") or "")
                if deps and done_lane_name:
                    dep_gate_ran = True
                    unsatisfied, cancelled = dep_gate(
                        deps, number_lane, number_state, done_lane_name)
                    if unsatisfied or cancelled:
                        noted = set(rec.get("dep_cancelled_noted") or [])
                        fresh = [n for n in cancelled if n not in noted]
                        for n in fresh:
                            comment_issue(
                                cfg, env, content["number"],
                                f"#{n} closed without shipping, but this issue depends on it. "
                                "Re-scope, rewire the Depends on line, or close this issue.")
                            noted.add(n)
                        if not rec.get("dep_blocked"):
                            comment_issue(
                                cfg, env, content["number"],
                                f"Moved to Blocked: depends on {fmt_deps(unsatisfied)} "
                                "— not in the Done lane. Will move to Todo automatically "
                                "when they ship. If a dependency is abandoned, re-scope or "
                                "close this issue.")
                        if blocked_lane_name:
                            option_id = status_options.get(blocked_lane_name)
                            if option_id and move_to_lane(
                                    cfg, env, project_id, item_id, field_id, option_id):
                                log(f"DEP-BLOCKED item={item_id} "
                                    f"issue={content['number']} -> {blocked_lane_name}")
                                status_val = blocked_lane_name
                        dep_blocked_marker = deps
                        dep_cancelled_noted = sorted(noted)
                    else:
                        dep_blocked_marker = None
                if not dep_gate_ran or dep_blocked_marker is None:
                    labels = [n["name"] for n in content["labels"]["nodes"]]
                    wf = pick_workflow(cfg, labels)
                    branch = f"issue-{content['number']}"
                    ok = False
                    if "needs-input" in labels and rec.get("branch") and rec.get("wf"):
                        ok, msg = resume_issue(cfg, env, rec["branch"], rec["wf"],
                                               content["number"])
                        if ok:
                            wf = rec["wf"]
                    if not ok:
                        msg = (
                            f"Implement GitHub issue #{content['number']}: {content['title']} "
                            f"({repo}). Full issue: {content['url']}"
                        )
                        if wf == "archon-idea-to-pr":
                            msg = (
                                f"Build feature from issue #{content['number']}: {content['title']} "
                                f"({repo}). Full issue: {content['url']}"
                            )
                        guard_ok, guard_reason = fresh_issue_dispatch_guard(
                            env, content["number"])
                        if guard_ok:
                            # Clear before dispatch so a failed board move cannot
                            # cause the same successful guard to dispatch again.
                            rec.pop("dispatch_guard_deferred", None)
                            dispatch_result = dispatch(
                                cfg, env, wf, branch, msg, item_id, content["number"])
                            ok = bool(dispatch_result)
                            if not ok and dispatch_result.reason == "capacity":
                                rec["dispatch_guard_deferred"] = True
                                note_capacity_deferred(
                                    cfg, env, content["number"], rec)
                        elif _dispatch_guard_is_deferred(guard_reason):
                            rec["dispatch_guard_deferred"] = True
                            rec.pop("recovery", None)
                            log(
                                f"DISPATCH DEFERRED issue={content['number']}: "
                                f"{guard_reason}"
                            )
                        else:
                            rec.pop("dispatch_guard_deferred", None)
                            log(f"DISPATCH REFUSED issue={content['number']}: {guard_reason}")
                            rec["recovery"] = {
                                "action": "resume_required",
                                "updated_at": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"),
                                "reason": guard_reason,
                            }
                            if rec.get("dispatch_guard_notice") != guard_reason:
                                if comment_issue(
                                        cfg, env, content["number"],
                                        "Fresh dispatch refused: " + guard_reason + ".\n"
                                        f"Inspect: `python3 automation/workflow_recovery.py "
                                        f"status {content['number']}`\n"
                                        f"Resume: `python3 automation/workflow_recovery.py "
                                        f"resume {content['number']}`\n"
                                        f"Discard: `python3 automation/workflow_recovery.py "
                                        f"discard {content['number']}`"):
                                    rec["dispatch_guard_notice"] = guard_reason
                    if ok:
                        rec.pop("dispatch_guard_deferred", None)
                        dispatched_msg = msg
                        dispatched_wf = wf
                        dispatched_branch = branch
                        fresh_dispatched.add(item_id)
                        rec.pop("capacity_deferred", None)
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
                    # (feature -> main); on an approving review the poller merges
                    # it. The helper also closes issues whose work already
                    # reached main (empty ship PR) and logs failures so the
                    # recheck pass can retry.
                    rec = state.get(item_id, {})
                    result = ensure_ship_review(
                        cfg, env, item_id, content["number"], content["title"],
                        project_id, field_id, status_options, done_lane_name, rec)
                    if isinstance(result, tuple):
                        review_msg = result[1]
                        ship_pr_num = result[2]
                        fresh_dispatched.add(item_id)
                    elif result == "shipped":
                        if done_lane_name:
                            status_val = done_lane_name

        rec = state.get(item_id, {})
        rec["status"] = status_val
        if prev != status_val and status_val != ready_lane_name:
            rec.pop("ready_test_comment", None)
        if dispatched_msg:
            baseline = (
                ((rec.get("recovery") or {}).get("run_id") if isinstance(rec.get("recovery"), dict) else None)
                or rec.get("last_observed_run_id")
                or rec.get("run_id")
            )
            rec.pop("ready_test_comment", None)
            rec.pop("develop_pr", None)
            rec.pop("dispatch_guard_notice", None)
            rec.pop("dispatch_guard_deferred", None)
            rec.pop("run_id", None)
            rec.pop("retrying", None)
            rec.pop("recovery_logged_run_id", None)
            rec.pop("recovery", None)
            rec.pop("automatic_retry_count", None)
            rec.pop("attempts", None)
            rec.pop("last_run", None)
            rec.pop("attempt_count", None)
            rec.pop("last_observed_run_id", None)
            if baseline:
                rec["dispatch_baseline_run_id"] = str(baseline)
            else:
                rec.pop("dispatch_baseline_run_id", None)
            rec["dispatch_msg"] = dispatched_msg
            rec["issue_number"] = content["number"]
            rec["wf"] = dispatched_wf
            rec["branch"] = dispatched_branch
        if review_msg:
            rec["review_msg"] = review_msg
            rec["ship_pr"] = ship_pr_num
            rec["issue_number"] = content["number"]
            rec.pop("review_run_id", None)
        if dep_gate_ran:
            if dep_blocked_marker:
                rec["dep_blocked"] = dep_blocked_marker
            else:
                rec.pop("dep_blocked", None)
            if dep_cancelled_noted:
                rec["dep_cancelled_noted"] = dep_cancelled_noted
            else:
                rec.pop("dep_cancelled_noted", None)
        state[item_id] = rec

    ctx = PollContext(
        cfg=cfg,
        env=env,
        state=state,
        project_id=project_id,
        field_id=field_id,
        status_options=status_options,
        items=items,
        first_run=first_run,
        done_lane_name=done_lane_name,
        todo_lane_name=todo_lane_name,
        blocked_lane_name=blocked_lane_name,
        ready_lane_name=ready_lane_name,
        in_progress_lane_name=in_progress_lane_name,
        number_lane=number_lane,
        number_state=number_state,
        seen=seen,
        fresh_dispatched=fresh_dispatched,
    )
    # Desired-state pass: fill free dispatch slots from runnable Backlog
    # items so the factory stays near capacity without human nudges. Do not
    # mutate existing board state while taking the initial snapshot; later
    # polls promote items after the transition loop, and they dispatch on the
    # next poll's Todo transition. The first poll establishes a snapshot and
    # must not mutate pre-existing Backlog work.
    if not ctx.first_run:
        _fill_concurrency_gap(ctx, items, number_lane, number_state)
    _recheck_review_dispatch(ctx)
    _unblock_dependencies(ctx)
    runs_snapshot, runs_by_msg = _reconcile_completions(ctx)
    _enforce_ready_proof(ctx, in_progress_lane_name)
    runs_snapshot, runs_by_msg, review_lane_name, ship_to = _complete_reviews(
        ctx, runs_snapshot, runs_by_msg)
    _remediate_ship_conflicts(
        ctx, runs_by_msg, review_lane_name, ship_to)

    # Prune items that left the board.
    for item_id in list(state):
        if item_id != "_meta" and item_id not in seen:
            del state[item_id]

    if first_run:
        state["_meta"] = {"snapshot_done": True, "project_id": project_id,
                          "snapshot_at": datetime.now(timezone.utc).isoformat()}
        log(f"snapshot taken: {len(seen)} items on board, dispatch armed")
    save_state(cfg, state)


def fetch_runs_by_message(env: dict) -> WorkflowRunStatusMap:
    """Map exact run messages to newest statuses without losing lookup health."""
    return runs_by_message_from(fetch_workflow_runs(env))


# Conflict-fix serialization: at most one conflict-resolver run (ship or
# develop) may be active at a time; sibling conflict episodes defer with a
# once-per-episode note instead of stacking concurrent resolvers on the same
# branches. `_POLL_CONFLICT_FIXERS` tracks messages dispatched within the
# current poll (their run rows appear asynchronously, so runs_by_msg cannot
# see them yet).
_POLL_CONFLICT_FIXERS: set[str] = set()

def _conflict_fixer_active(runs_by_msg: dict | None,
                           fresh_messages: set[str]) -> bool:
    """True when a conflict-resolver run is in flight or dispatched this poll."""
    if fresh_messages:
        return True
    if not runs_by_msg:
        return False
    return any(
        "Resolve merge conflicts" in str(msg or "")
        and status in ACTIVE_WORKFLOW_STATUSES
        for msg, status in runs_by_msg.items()
    )
