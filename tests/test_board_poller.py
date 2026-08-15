from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch
import automation.pm_harness.archon as archon_adapter
import automation.pm_harness.cycle as cycle_adapter
import automation.pm_harness.dispatch as dispatch_adapter
import automation.pm_harness.github as github_adapter
import automation.pm_harness.model as model_adapter
import automation.pm_harness.policy as policy_adapter
import automation.pm_harness.recovery as recovery_adapter
from automation.pm_harness.archon import (
    latest_workflow_run,
    parse_isolation_list,
)
from automation.pm_harness.dispatch import (
    dispatch,
    fetch_active_workflow_count,
    prepare_dispatch_budget,
)
from automation.pm_harness.github import (
    branch_empty_vs_main,
    fetch_project,
    match_issue_pr,
    merge_pr_to_base,
    post_ready_for_review_comment,
    sync_runnable_labels,
    try_merge_base_into_head,
)
from automation.pm_harness.model import (
    DispatchResult,
    WorkflowRuns,
    build_recovery_comment,
    classify_workflow_failure,
    failed_workflow_step,
    issue_number_from_message,
    recovery_action,
    workflow_run_details,
)
from automation.pm_harness.policy import (
    build_ready_for_review_comment,
    conflict_episode_action,
    dep_gate,
    develop_conflict_action,
    extract_test_guidance,
    fmt_deps,
    issue_is_runnable,
    parse_dep_refs,
    parse_verdict,
    pick_workflow,
    run_status_for,
    split_test_guidance,
)
from automation.pm_harness.recovery import fresh_issue_dispatch_guard


class DispatchCapacityTest(unittest.TestCase):
    def test_active_workflow_count_ignores_terminal_runs(self):
        result = subprocess.CompletedProcess(
            ["archon"], 0,
            json.dumps({"runs": [
                {"status": "running"},
                {"status": "paused"},
                {"status": "completed"},
            ]}),
            "",
        )
        with patch("automation.pm_harness.dispatch.subprocess.run", return_value=result):
            self.assertEqual(fetch_active_workflow_count({}), 2)

    def test_active_workflow_count_scopes_to_this_repo(self):
        result = subprocess.CompletedProcess(
            ["archon"], 0,
            json.dumps({"runs": [
                {"status": "running",
                 "working_path": "/Users/x/.local/share/archon-pi/archon-home/"
                                 "workspaces/bradley-mankoff/news/worktrees/archon/task-1"},
                {"status": "running",
                 "working_path": str(dispatch_adapter.ROOT)},  # no-worktree run
                {"status": "running",
                 "working_path": "/Users/x/.local/share/archon-pi/archon-home/"
                                 "workspaces/_local/piyaz_trial/worktrees/archon/task-2"},
                {"status": "paused"},  # unlocatable: counted (fail toward holding)
                {"status": "completed",
                 "working_path": "/Users/x/.local/share/archon-pi/archon-home/"
                                 "workspaces/bradley-mankoff/news/worktrees/archon/task-3"},
            ]}),
            "",
        )
        with patch("automation.pm_harness.dispatch.subprocess.run", return_value=result):
            self.assertEqual(
                fetch_active_workflow_count({}, "bradley-mankoff/news"), 3)

    def test_active_workflow_count_fails_closed(self):
        result = subprocess.CompletedProcess(["archon"], 1, "", "unavailable")
        with patch("automation.pm_harness.dispatch.subprocess.run", return_value=result):
            self.assertIsNone(fetch_active_workflow_count({}))

    def test_budget_reserves_slots_after_existing_runs(self):
        with (
            patch.object(dispatch_adapter, "DRY_RUN", False),
            patch.object(
                dispatch_adapter, "fetch_active_workflow_count", return_value=2),
        ):
            prepare_dispatch_budget({"max_concurrent_workflows": 3}, {})
            self.assertEqual(dispatch_adapter._DISPATCH_BUDGET, 1)

    def test_budget_holds_when_status_lookup_fails(self):
        with (
            patch.object(dispatch_adapter, "DRY_RUN", False),
            patch.object(
                dispatch_adapter, "fetch_active_workflow_count", return_value=None),
        ):
            prepare_dispatch_budget({"max_concurrent_workflows": 3}, {})
            self.assertEqual(dispatch_adapter._DISPATCH_BUDGET, 0)

    def test_dispatch_holds_when_budget_is_exhausted(self):
        with (
            patch.object(dispatch_adapter, "DRY_RUN", False),
            patch.object(dispatch_adapter, "_DISPATCH_BUDGET", 0),
            patch("automation.pm_harness.dispatch.subprocess.Popen") as popen,
        ):
            self.assertFalse(dispatch({}, {}, "workflow", "branch", "message", "item", 7))
            popen.assert_not_called()

    def test_dispatch_consumes_reserved_slot(self):
        with (
            patch.object(dispatch_adapter, "DRY_RUN", False),
            patch.object(dispatch_adapter, "_DISPATCH_BUDGET", 1),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("automation.pm_harness.dispatch.subprocess.Popen") as popen,
        ):
            popen.return_value.pid = 123
            self.assertTrue(dispatch({}, {}, "workflow", "branch", "message", "item", 7))
            self.assertEqual(dispatch_adapter._DISPATCH_BUDGET, 0)


class WorkflowRecoveryTest(unittest.TestCase):
    def test_classifies_known_failures_and_extracts_step(self):
        transient = (
            "DAG workflow 'archon-fix-github-issue' completed with failures: "
            "'web-research': Node 'web-research' failed: SDK returned error - "
            "Stream ended without finish_reason"
        )
        orchestration = (
            "DAG workflow 'archon-idea-to-pr' completed with failures: "
            "'review__verify-pr-base': No open PR found for branch archon/task-issue-61"
        )
        self.assertEqual(classify_workflow_failure("failed", transient), "transient")
        self.assertEqual(classify_workflow_failure("failed", orchestration),
                         "orchestration")
        self.assertEqual(failed_workflow_step(transient), "web-research")

    def test_issue_number_and_newest_run_matching(self):
        runs = [
            {"id": "old", "started_at": "2026-08-06 22:00:00",
             "user_message": "Build feature from issue #141"},
            {"id": "new", "started_at": "2026-08-06 22:10:00",
             "user_message": "Prior Context: Build feature from issue #141"},
        ]
        self.assertEqual(issue_number_from_message(runs[0]["user_message"]), 141)
        self.assertEqual(latest_workflow_run(runs, issue_number=141)["id"], "new")
        self.assertEqual(latest_workflow_run(runs, message="issue #141")["id"], "new")

    def test_dirty_transient_failure_requires_resume(self):
        self.assertEqual(recovery_action("failed", "transient", True),
                         "resume_required")
        self.assertEqual(recovery_action("failed", "transient", False),
                         "retry_available")
        self.assertEqual(recovery_action("failed", "orchestration", False),
                         "manual_review")

    def test_cancelled_run_is_transient_recovery(self):
        self.assertEqual(classify_workflow_failure("cancelled", ""), "transient")
        self.assertEqual(recovery_action("cancelled", "transient", True),
                         "resume_required")
        self.assertEqual(recovery_action("cancelled", "transient", False),
                         "retry_available")

    def test_isolation_parser_keeps_branch_and_path(self):
        output = (
            "https://github.com/o/r.git:\n"
            "  archon/task-issue-141\n"
            "    Path: /tmp/issue-141\n"
            "    Type: task | Platform: cli\n"
        )
        self.assertEqual(
            parse_isolation_list(output),
            {"archon/task-issue-141": {"path": "/tmp/issue-141"}},
        )
    def test_isolation_parser_accepts_short_issue_branch(self):
        output = (
            "https://github.com/o/r.git:\n"
            "  issue-61\n"
            "    Path: /tmp/issue-61\n"
        )
        self.assertEqual(
            parse_isolation_list(output),
            {"issue-61": {"path": "/tmp/issue-61"}},
        )

    def test_resolve_worktree_info_accepts_short_issue_branch(self):
        with patch.object(
            archon_adapter,
            "fetch_archon_worktrees",
            return_value={"issue-61": {"path": "/tmp/issue-61"}},
        ):
            self.assertEqual(
                archon_adapter.resolve_worktree_info({}, 61),
                {"branch": "issue-61", "path": "/tmp/issue-61"},
            )


    def test_recovery_comment_exposes_safe_actions(self):
        body = build_recovery_comment(
            141,
            {
                "run_id": "run-141",
                "failed_step": "validate",
                "failure_class": "transient",
                "error": "stream ended",
            },
            {"path": "/tmp/issue-141", "exists": True, "dirty": True},
            "resume_required",
        )
        self.assertIn("workflow_recovery.py status 141", body)
        self.assertIn("workflow_recovery.py resume 141", body)
        self.assertIn("workflow_recovery.py discard 141", body)
    def test_fresh_dispatch_refuses_dirty_existing_worktree(self):
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(
                    {"branch": "archon/task-issue-141", "path": "/tmp/141"},
                    None,
                ),
            ),
            patch.object(
                recovery_adapter,
                "inspect_worktree",
                return_value={"path": "/tmp/141", "exists": True, "dirty": True},
            ),
            patch.object(recovery_adapter, "fetch_workflow_runs") as fetch,
        ):
            allowed, reason = fresh_issue_dispatch_guard({}, 141)
        self.assertFalse(allowed)
        self.assertIn("dirty", reason)
        fetch.assert_not_called()

    def test_fresh_dispatch_defers_when_run_lookup_is_unavailable(self):
        unavailable = WorkflowRuns(error="archon_timeout")
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(None, None),
            ),
            patch.object(recovery_adapter, "fetch_workflow_runs", return_value=unavailable),
            patch.object(recovery_adapter, "log") as log,
        ):
            allowed, reason = fresh_issue_dispatch_guard({}, 141)
        self.assertFalse(allowed)
        self.assertEqual(reason, "Archon run lookup unavailable: archon_timeout")
        log.assert_called_once_with(
            "FRESH DISPATCH DEFERRED issue=141: "
            "run lookup unavailable (archon_timeout)"
        )

    def test_fresh_dispatch_defers_when_worktree_lookup_is_unavailable(self):
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(None, "archon_isolation_timeout"),
            ),
            patch.object(recovery_adapter, "fetch_workflow_runs") as fetch,
            patch.object(recovery_adapter, "log") as log,
        ):
            allowed, reason = fresh_issue_dispatch_guard({}, 141)
        self.assertFalse(allowed)
        self.assertEqual(
            reason,
            "Archon worktree lookup unavailable: archon_isolation_timeout",
        )
        fetch.assert_not_called()
        log.assert_called_once_with(
            "FRESH DISPATCH DEFERRED issue=141: "
            "worktree lookup unavailable (archon_isolation_timeout)"
        )

    def test_fresh_dispatch_defers_when_worktree_cleanliness_is_unknown(self):
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(
                    {"branch": "archon/task-issue-141", "path": "/tmp/141"},
                    None,
                ),
            ),
            patch.object(
                recovery_adapter,
                "inspect_worktree",
                return_value={
                    "path": "/tmp/141",
                    "exists": True,
                    "dirty": None,
                    "error": "git unavailable",
                },
            ),
            patch.object(recovery_adapter, "fetch_workflow_runs") as fetch,
        ):
            allowed, reason = fresh_issue_dispatch_guard({}, 141)
        self.assertFalse(allowed)
        self.assertEqual(
            reason,
            "existing worktree cannot be verified: git unavailable",
        )
        fetch.assert_not_called()

    def test_fresh_dispatch_allows_healthy_empty_lookup(self):
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(None, None),
            ),
            patch.object(
                recovery_adapter,
                "fetch_workflow_runs",
                return_value=WorkflowRuns(),
            ),
        ):
            self.assertEqual(
                fresh_issue_dispatch_guard({}, 141),
                (True, ""),
            )

    def test_fresh_dispatch_refuses_matching_active_run(self):
        active = WorkflowRuns([{
            "id": "run-141",
            "user_message": "Build feature from issue #141",
            "started_at": "2026-08-07T10:00:00Z",
            "status": "running",
        }])
        with (
            patch.object(
                recovery_adapter,
                "resolve_worktree_info_with_health",
                return_value=(None, None),
            ),
            patch.object(
                recovery_adapter,
                "fetch_workflow_runs",
                return_value=active,
            ),
        ):
            allowed, reason = fresh_issue_dispatch_guard({}, 141)
        self.assertFalse(allowed)
        self.assertEqual(reason, "Archon run run-141 is still active")


class MatchIssuePrTest(unittest.TestCase):
    def _ship(self):
        return {"number": 51, "baseRefName": "main",
                "body": "Issue #21. Shipped from develop after human testing."}

    def _develop(self):
        return {"number": 47, "baseRefName": "develop", "body": "Issue: #21"}

    def test_base_filter_prefers_the_develop_pr(self):
        self.assertEqual(
            match_issue_pr([self._ship(), self._develop()], 21, "develop")["number"],
            47)

    def test_no_base_returns_newest_first(self):
        self.assertEqual(
            match_issue_pr([self._ship(), self._develop()], 21)["number"], 51)

    def test_base_filter_excludes_other_base(self):
        self.assertIsNone(match_issue_pr([self._ship()], 21, "develop"))

    def test_title_fallback_with_base(self):
        pr = {"number": 9, "baseRefName": "main", "body": "",
              "title": "Ship: Choose a license (#21)"}
        self.assertEqual(match_issue_pr([pr], 21, "main")["number"], 9)

    def test_no_reference_no_match(self):
        pr = {"number": 9, "baseRefName": "main", "body": "Nothing here",
              "title": "Unrelated"}
        self.assertIsNone(match_issue_pr([pr], 21))
    def test_incidental_issue_mention_is_not_a_link(self):
        pr = {
            "number": 153,
            "baseRefName": "develop",
            "body": "Issue #124 cleanup removed an old model reference.",
            "title": "Anchor curated-match prefix (#92)",
        }
        self.assertIsNone(match_issue_pr([pr], 124))

    def test_issue_link_line_with_following_text_is_accepted(self):
        pr = {
            "number": 11,
            "baseRefName": "develop",
            "body": "Issue: #21\n\nDetails follow.",
            "title": "Implement the change",
        }
        self.assertEqual(match_issue_pr([pr], 21)["number"], 11)

    def test_branch_fallback_matches_worktree_pr(self):
        pr = {
            "number": 220,
            "baseRefName": "develop",
            "headRefName": "archon/task-issue-80",
            "body": "Plan ... (Build feature from issue #80)",
            "title": "Serve model recommendations via the UI schema",
        }
        self.assertEqual(match_issue_pr([pr], 80, "develop")["number"], 220)

    def test_branch_fallback_matches_implements_keyword_body(self):
        pr = {
            "number": 221,
            "baseRefName": "develop",
            "headRefName": "archon/task-issue-82",
            "body": "Implements #82.",
            "title": "Serve model task and runtime-fit labels",
        }
        self.assertEqual(match_issue_pr([pr], 82, "develop")["number"], 221)

    def test_branch_fallback_matches_body_without_reference(self):
        pr = {
            "number": 223,
            "baseRefName": "develop",
            "headRefName": "archon/task-issue-100",
            "body": "## Summary\nReplaces prompt readouts.",
            "title": "Full-template LLM prompt editors",
        }
        self.assertEqual(match_issue_pr([pr], 100, "develop")["number"], 223)

    def test_branch_fallback_respects_base_filter(self):
        ship = {"number": 219, "baseRefName": "main",
                "headRefName": "archon/task-issue-90", "body": "", "title": "Ship: x"}
        dev = {"number": 213, "baseRefName": "develop",
               "headRefName": "archon/task-issue-90", "body": "", "title": "x"}
        self.assertEqual(
            match_issue_pr([ship, dev], 90, "develop")["number"], 213)
        self.assertEqual(
            match_issue_pr([ship, dev], 90, "main")["number"], 219)

    def test_branch_fallback_does_not_confuse_similar_issue_numbers(self):
        pr = {"number": 220, "baseRefName": "develop",
              "headRefName": "archon/task-issue-80", "body": "", "title": "x"}
        self.assertIsNone(match_issue_pr([pr], 8, "develop"))
        self.assertIsNone(match_issue_pr([pr], 800, "develop"))


class ConflictEpisodeActionTest(unittest.TestCase):
    def test_mergeable_no_episode(self):
        self.assertEqual(conflict_episode_action("MERGEABLE", None, None, False), "none")

    def test_conflict_no_fix_yet_tries_mechanical(self):
        self.assertEqual(conflict_episode_action("CONFLICTING", None, None, False), "update")

    def test_conflict_mech_failed_dispatches(self):
        self.assertEqual(conflict_episode_action("CONFLICTING", None, None, True), "dispatch")

    def test_fix_run_active_waits(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "running", True), "active")

    def test_fix_run_pending_waits(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "queued", True), "active")

    def test_paused_fix_run_waits(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "paused", True), "active")

    def test_fix_run_completed_still_conflicting(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "completed", True), "retry")

    def test_fix_run_failed_status(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "failed", True), "retry")

    def test_fix_run_retried_failed_status_escalates(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "failed", True,
                                    retried=True), "failed")

    def test_fix_run_retried_completed_escalates(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "completed", True,
                                    retried=True), "failed")

    def test_unknown_waits(self):
        self.assertEqual(conflict_episode_action("UNKNOWN", None, None, False), "wait")

    def test_mergeable_clears_fix_episode(self):
        self.assertEqual(
            conflict_episode_action("MERGEABLE", "m", "completed", True), "clear")

    def test_mergeable_clears_mech_failed_only(self):
        self.assertEqual(conflict_episode_action("MERGEABLE", None, None, True), "clear")


class DevelopConflictActionTest(unittest.TestCase):
    def test_fresh_conflict_tries_mechanical(self):
        self.assertEqual(develop_conflict_action(False, None, None), "mech")


class BranchEmptyVsMainTest(unittest.TestCase):
    def _gh(self, stdout="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    @patch("automation.pm_harness.github.gh")
    def test_empty_branch_is_already_shipped(self, gh):
        gh.return_value = self._gh('{"ahead_by": 0}')
        self.assertTrue(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.pm_harness.github.gh")
    def test_ahead_branch_is_shippable(self, gh):
        gh.return_value = self._gh('{"ahead_by": 5}')
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.pm_harness.github.gh")
    def test_api_error_is_not_shipped(self, gh):
        gh.return_value = self._gh("", returncode=1)
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.pm_harness.github.gh")
    def test_unparseable_is_not_shipped(self, gh):
        gh.return_value = self._gh("not json")
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    def test_mech_failed_dispatches_resolver(self):
        self.assertEqual(develop_conflict_action(True, None, None), "dispatch")

    def test_resolver_active_waits(self):
        for st in ("running", "pending", "queued", "scheduled", "paused"):
            self.assertEqual(develop_conflict_action(True, "m", st), "active")

    def test_resolver_done_still_failing_retries_once(self):
        self.assertEqual(develop_conflict_action(True, "m", "completed"), "retry")
        self.assertEqual(develop_conflict_action(True, "m", "failed"), "retry")

    def test_resolver_retried_done_escalates(self):
        self.assertEqual(
            develop_conflict_action(True, "m", "completed", retried=True), "failed")
        self.assertEqual(
            develop_conflict_action(True, "m", "failed", retried=True), "failed")


class ParseDepRefsTest(unittest.TestCase):
    def test_inline_line(self):
        self.assertEqual(parse_dep_refs("Do a thing\nDepends on: #42\nMore."), [42])

    def test_bullet_and_multiple(self):
        self.assertEqual(parse_dep_refs("- Depends on: #42, #57\n"), [42, 57])

    def test_form_heading_with_refs_next_line(self):
        body = "## Notes\n### Depends on\n#42, #57\n"
        self.assertEqual(parse_dep_refs(body), [42, 57])

    def test_form_heading_empty_value(self):
        body = "### Depends on\n\nNo blockers.\n"
        self.assertEqual(parse_dep_refs(body), [])

    def test_bold_label(self):
        self.assertEqual(parse_dep_refs("**Depends on:** #7"), [7])

    def test_no_colon(self):
        self.assertEqual(parse_dep_refs("Depends on #3"), [3])

    def test_no_refs(self):
        self.assertEqual(parse_dep_refs("Nothing depends on anything"), [])
        self.assertEqual(parse_dep_refs(""), [])
        self.assertEqual(parse_dep_refs("### Depends on\n\n(none)"), [])

    def test_dedupes_and_sorts(self):
        self.assertEqual(parse_dep_refs("Depends on: #9, #9, #2"), [2, 9])


class DepGateTest(unittest.TestCase):
    def test_all_done(self):
        self.assertEqual(
            dep_gate([1, 2], {1: "Done", 2: "Done"}, {1: "OPEN", 2: "OPEN"}, "Done"),
            ([], []))

    def test_unsatisfied_open(self):
        self.assertEqual(
            dep_gate([1], {1: "In Progress"}, {1: "OPEN"}, "Done"), ([1], []))

    def test_cancelled_dep(self):
        self.assertEqual(
            dep_gate([1], {1: "Blocked"}, {1: "CLOSED"}, "Done"), ([1], [1]))

    def test_closed_in_done_is_satisfied(self):
        self.assertEqual(
            dep_gate([1], {1: "Done"}, {1: "CLOSED"}, "Done"), ([], []))

    def test_off_board_is_unsatisfied_not_cancelled(self):
        self.assertEqual(dep_gate([1], {}, {}, "Done"), ([1], []))

    def test_dep_in_todo_unsatisfied(self):
        self.assertEqual(
            dep_gate([5], {5: "Todo"}, {5: "OPEN"}, "Done"), ([5], []))



class RunnableLabelTest(unittest.TestCase):
    def _issue(self, number, status, body="", labels=None, state="OPEN"):
        return {
            "id": f"item-{number}",
            "status": status,
            "content": {
                "__typename": "Issue",
                "number": number,
                "state": state,
                "body": body,
                "repository": {"nameWithOwner": "r"},
                "labels": {"nodes": [{"name": name} for name in (labels or [])]},
            },
        }

    def test_only_open_todo_issue_with_satisfied_deps_is_runnable(self):
        issue = self._issue(8, "Todo", "Depends on: #7")
        self.assertTrue(issue_is_runnable(
            issue["content"], issue["status"],
            {7: "Done"}, {7: "CLOSED"}, "Todo", "Done"))
        self.assertFalse(issue_is_runnable(
            issue["content"], "In Progress",
            {7: "Done"}, {7: "CLOSED"}, "Todo", "Done"))
        self.assertFalse(issue_is_runnable(
            issue["content"], issue["status"],
            {7: "Todo"}, {7: "OPEN"}, "Todo", "Done"))

    @patch("automation.pm_harness.github.gh")
    def test_sync_adds_and_removes_label(self, gh):
        gh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        items = [
            self._issue(8, "Todo"),
            self._issue(9, "In Progress", labels=["runnable"]),
        ]
        sync_runnable_labels(
            {"repo": "r", "runnable_label": "runnable"},
            {},
            items,
            {8: "Todo", 9: "In Progress"},
            {8: "OPEN", 9: "OPEN"},
            "Todo",
            "Done",
        )
        commands = [call.args[0] for call in gh.call_args_list]
        self.assertIn(
            ["label", "create", "runnable", "-R", "r", "--color", "0e8a16",
             "--description", "Todo issue with satisfied dependencies", "--force"],
            commands,
        )
        self.assertIn(
            ["issue", "edit", "8", "-R", "r", "--add-label", "runnable"],
            commands,
        )
        self.assertIn(
            ["issue", "edit", "9", "-R", "r", "--remove-label", "runnable"],
            commands,
        )

class ParseVerdictTest(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(parse_verdict(["Reviewed. VERDICT: approve"]), "approve")

    def test_case_insensitive_value(self):
        self.assertEqual(parse_verdict(["VERDICT: APPROVE"]), "approve")

    def test_last_wins_across_comments(self):
        bodies = ["VERDICT: request-changes", "fixes applied\nVERDICT: approve"]
        self.assertEqual(parse_verdict(bodies), "approve")

    def test_block(self):
        self.assertEqual(parse_verdict(["VERDICT: block"]), "block")

    def test_request_changes(self):
        self.assertEqual(parse_verdict(["VERDICT: request-changes"]), "request-changes")

    def test_absent_or_malformed_is_none(self):
        self.assertIsNone(parse_verdict([]))
        self.assertIsNone(parse_verdict(["no verdict here"]))
        self.assertIsNone(parse_verdict(["VERDICT: maybe"]))

    def test_markdown_wrapped_marker(self):
        self.assertEqual(parse_verdict(["**VERDICT: approve**"]), "approve")

    def test_lowercase_marker(self):
        self.assertEqual(parse_verdict(["verdict: approve"]), "approve")

    def test_embedded_token_marker_ignored(self):
        self.assertIsNone(parse_verdict(["XVERDICT: approve"]))
        self.assertIsNone(parse_verdict(["REVERDICT: approve"]))

    def test_multiline_body(self):
        self.assertEqual(
            parse_verdict(["line one\nVERDICT: approve\nline three"]), "approve")


class ReadyForReviewCommentTest(unittest.TestCase):
    def test_explicit_guidance_wins_over_older_validation(self):
        comments = [
            {"body": "## Validation\n`pytest tests/test_old.py -q`"},
            {"body": ("## How to test\n"
                      "Run `news ui` and open http://localhost:8766.\n"
                      "### Expected\nThe page loads.")},
        ]
        self.assertEqual(
            extract_test_guidance(comments),
            ("Run `news ui` and open http://localhost:8766.\n"
             "### Expected\nThe page loads."),
        )

    def test_validation_backfills_older_completion_records(self):
        comments = [
            {"body": "### Validation\n```bash\npytest tests/test_model_catalog.py -q\n```"},
            {"body": "### Validation\n✅ Tests (478 passed, 0 failed)."},
            {"body": "## How to test\n*None.*"},
        ]
        self.assertEqual(
            extract_test_guidance(comments),
            "```bash\npytest tests/test_model_catalog.py -q\n```",
        )

    def test_build_includes_branch_pr_and_promotion_command(self):
        body = build_ready_for_review_comment(
            92, "develop", 153, "Run `pytest tests/test_model_catalog.py -q`."
        )
        self.assertIn("Develop PR #153 was merged into `develop`.", body)
        self.assertIn("pytest tests/test_model_catalog.py -q", body)
        self.assertIn(
            'python3 automation/move_item.py 92 "In Review"', body)

    def test_build_splits_machine_checks_from_human_steps(self):
        body = build_ready_for_review_comment(
            92,
            "develop",
            153,
            "From the merged `develop` checkout:\n\n"
            "```bash\nuv run pytest -q tests/test_model_catalog.py\n```\n\n"
            "Passing output confirms the catalog contract; passed 12 tests.\n\n"
            "Review the generated report for tone before shipping.\n",
        )
        self.assertIn("### Machine checks", body)
        self.assertIn("### Human checks", body)
        self.assertIn("uv run pytest -q tests/test_model_catalog.py", body)
        self.assertIn("passed 12 tests", body)
        self.assertIn(
            "Review the generated report for tone before shipping.", body)
        self.assertIn("re-runs only a check that lacks recorded evidence", body)

    def test_build_removes_inline_shell_comments_from_commands(self):
        body = build_ready_for_review_comment(
            92,
            "develop",
            153,
            "```bash\n"
            ".venv/bin/python -m pytest tests/test_ui.py -q # integration consumers\n"
            "```",
        )
        self.assertIn(
            ".venv/bin/python -m pytest tests/test_ui.py -q", body)
        self.assertNotIn("# integration consumers", body)

    def test_build_is_explicit_when_no_test_path_was_recorded(self):
        body = build_ready_for_review_comment(7, "develop", None, None)
        self.assertIn("no linked develop PR was available", body)
        self.assertIn("No runnable machine checks were recorded", body)
        self.assertIn(
            "After the machine checks pass, the ready-review QA agent moves "
            "this issue to In Review.", body)

    def test_build_says_no_manual_steps_when_all_checks_are_machine(self):
        body = build_ready_for_review_comment(
            92,
            "develop",
            153,
            "```bash\nuv run pytest -q tests/test_model_catalog.py\n```\n",
        )
        self.assertIn(
            "No manual steps are required — every recorded check is "
            "machine-runnable.", body)

    @patch("automation.pm_harness.github.gh")
    def test_post_fetches_guidance_then_comments(self, gh):
        gh.side_effect = [
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({
                    "comments": [{"body": "## How to test\nRun the focused check."}],
                }), stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        with patch.object(github_adapter, "DRY_RUN", False):
            self.assertTrue(
                post_ready_for_review_comment(
                    {"repo": "o/r"}, {}, 7, "develop", 9))
        self.assertEqual(gh.call_args_list[0].args[0][:2], ["issue", "view"])
        comment_args = gh.call_args_list[1].args[0]
        self.assertEqual(comment_args[:2], ["issue", "comment"])
        self.assertIn("Run the focused check.", comment_args[-1])


class SplitTestGuidanceTest(unittest.TestCase):
    def test_commands_and_evidence_are_machine_checks(self):
        guidance = (
            "From the merged `develop` checkout:\n\n"
            "```bash\nuv run pytest -q tests/test_x.py\n```\n\n"
            "Passing output confirms the behavior; the latest run passed 12 "
            "tests.\n\n"
            "Review the generated report for tone.\n"
        )
        machine, human = split_test_guidance(guidance)
        self.assertIn("uv run pytest -q tests/test_x.py", machine)
        self.assertIn("passed 12 tests", machine)
        self.assertEqual("Review the generated report for tone.", human)

    def test_prose_only_guidance_is_all_human(self):
        machine, human = split_test_guidance(
            "Run `news ui` and open http://localhost:8766.\n"
            "### Expected\nThe page loads.")
        self.assertIsNone(machine)
        self.assertIn("Run `news ui`", human)
        self.assertIn("The page loads.", human)

    def test_explicit_subsections_win(self):
        guidance = (
            "### Machine checks\n"
            "- `uv run pytest -q tests/test_y.py` — passed (10 tests)\n\n"
            "### Human checks\n"
            "- Review the report visually.\n"
        )
        machine, human = split_test_guidance(guidance)
        self.assertIn("tests/test_y.py", machine)
        self.assertEqual("- Review the report visually.", human)

    def test_machine_subsection_without_human_heading(self):
        guidance = (
            "### Automated checks\n"
            "- `uv run pytest -q tests/test_z.py` — passed (7 tests)\n"
        )
        machine, human = split_test_guidance(guidance)
        self.assertIn("tests/test_z.py", machine)
        self.assertIsNone(human)

    def test_human_step_after_commands_is_not_swallowed(self):
        guidance = (
            "```bash\nuv run news run\n```\n\n"
            "Open the generated report and confirm the tone matches the "
            "profile.\n"
        )
        machine, human = split_test_guidance(guidance)
        self.assertIn("uv run news run", machine)
        self.assertIn("confirm the tone matches the profile", human)

    def test_no_guidance(self):
        self.assertEqual(split_test_guidance(None), (None, None))
        self.assertEqual(split_test_guidance(""), (None, None))


class ReadyForReviewTransitionTest(unittest.TestCase):
    def test_stored_run_id_completes_when_run_list_is_malformed(self):
        item_id = "item-92"
        item = {
            "id": item_id,
            "status": "In Progress",
            "content": {
                "__typename": "Issue",
                "number": 92,
                "title": "Anchor curated-match prefix",
                "url": "https://github.com/o/r/issues/92",
                "body": "",
                "state": "OPEN",
                "repository": {"nameWithOwner": "o/r"},
                "labels": {"nodes": []},
            },
        }
        cfg = {
            "repo": "o/r",
            "state_file": "state.json",
            "lanes": {
                "Backlog": "backlog",
                "Todo": "todo",
                "In Progress": "in_progress",
                "Ready for Review": "ready",
                "In Review": "review",
                "Done": "done",
            },
            "dispatch": {
                "todo": {
                    "complete_move_to": "Ready for Review",
                    "merge_develop_base": "develop",
                },
                "review": {
                    "merge_ship_on_approve": False,
                    "ship_to": "main",
                    "done_lane": "Done",
                },
            },
        }
        state = {
            "_meta": {"snapshot_done": True},
            item_id: {
                "status": "In Progress",
                "issue_number": 92,
                "dispatch_msg": "original dispatch text",
                "run_id": "direct-run-92",
            },
        }
        with (
            patch.object(
                cycle_adapter,
                "fetch_project",
                return_value=("p", "f", {"Ready for Review": "ready"}, [item]),
            ),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "fetch_workflow_run", return_value={
                "id": "direct-run-92",
                "user_message": "Prior Context omitted the original dispatch text",
                "status": "completed",
                "started_at": "2026-08-08T10:00:00Z",
            }),
            patch.object(
                cycle_adapter,
                "fetch_workflow_runs",
                return_value=WorkflowRuns(error="archon_json"),
            ),
            patch.object(cycle_adapter, "issue_has_label", return_value=False),
            patch.object(cycle_adapter, "find_issue_pr", side_effect=[
                ({"number": 153, "state": "OPEN",
                  "headRefName": "issue-92"}, True),
                ({"number": 153, "state": "MERGED",
                  "headRefName": "issue-92"}, True),
            ]),
            patch.object(
                cycle_adapter, "merge_pr_to_base", return_value=(True, "merged")),
            patch.object(cycle_adapter, "run_hook", return_value="hook skipped"),
            patch.object(cycle_adapter, "move_to_lane", return_value=True) as move,
            patch.object(
                cycle_adapter,
                "post_ready_for_review_comment",
                return_value=True,
            ) as post,
            patch.object(cycle_adapter, "save_state"),
        ):
            cycle_adapter.poll(cfg, {}, state)
        move.assert_called_once_with(cfg, {}, "p", "item-92", "f", "ready")
        post.assert_called_once_with(cfg, {}, 92, "develop", 153)
        self.assertTrue(state[item_id]["ready_test_comment"])


class TransientClassifierPrecedenceTest(unittest.TestCase):
    """Validation and orchestration failures are never transient, even when
    the error blob also mentions transport text (websocket/timeout)."""

    def test_test_coverage_step_with_websocket_error_is_validation(self):
        error = "SDK returned error — WebSocket closed 1006"
        step = "review__test-coverage"
        self.assertEqual(classify_workflow_failure("failed", error, step),
                         "validation")

    def test_transport_error_without_validation_step_stays_transient(self):
        error = "SDK returned error — WebSocket closed 1006"
        step = "web-research"
        self.assertEqual(classify_workflow_failure("failed", error, step),
                         "transient")

    def test_validation_blob_beats_transport_text(self):
        error = "test coverage failed: SDK returned error — WebSocket closed 1006"
        self.assertEqual(classify_workflow_failure("failed", error), "validation")

    def test_orchestration_blob_beats_transport_text(self):
        error = "No open PR found for branch archon/task-issue-61; " \
                "SDK returned error — WebSocket closed 1006"
        self.assertEqual(classify_workflow_failure("failed", error),
                         "orchestration")


class ShipHeldLogSpamTest(unittest.TestCase):
    """SHIP HELD logs once per hold episode, not every poll."""

    def _ctx(self, state):
        cfg = {
            "repo": "o/r",
            "lanes": {"In Review": "review", "Done": "done"},
            "dispatch": {"review": {
                "ship_to": "main",
                "done_lane": "Done",
                "merge_ship_on_approve": True,
            }},
        }
        return cycle_adapter.PollContext(
            cfg=cfg, env={}, state=state, project_id="p", field_id="f",
            status_options={"Done": "done-opt"}, items=[],
            first_run=False, done_lane_name="Done", todo_lane_name="Todo",
            blocked_lane_name="Blocked", ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={}, number_state={}, seen=set(), fresh_dispatched=set())

    def test_repeat_hold_logs_once_per_episode(self):
        state = {"item-5": {
            "status": "In Review",
            "issue_number": 5,
            "ship_pr": 51,
            "review_msg": "Review PR #51 (ship to main for issue #5: X).",
            "review_run_id": "r1",
        }}
        ctx = self._ctx(state)
        run = {"id": "r1", "status": "completed"}
        with (
            patch.object(cycle_adapter, "gh") as gh,
            patch.object(cycle_adapter, "fetch_workflow_run", return_value=run),
            patch.object(cycle_adapter, "fetch_verdict", return_value=(None, True)),
            patch.object(cycle_adapter, "comment_issue", return_value=True) as comment,
            patch.object(cycle_adapter, "log") as log,
        ):
            gh.return_value = _cp(stdout=json.dumps(
                {"number": 51, "state": "OPEN", "baseRefName": "main"}))
            cycle_adapter._complete_reviews(ctx, None, None)
            cycle_adapter._complete_reviews(ctx, None, None)
        held_logs = [
            c.args[0] for c in log.call_args_list if "SHIP HELD" in c.args[0]]
        self.assertEqual(len(held_logs), 1)
        comment.assert_called_once()
        self.assertTrue(state["item-5"]["review_held"])
        self.assertEqual(state["item-5"]["review_held_notice"], "none")

    def test_failed_review_run_logs_once_per_run(self):
        state = {"item-6": {
            "status": "In Review",
            "issue_number": 6,
            "ship_pr": 61,
            "review_msg": "Review PR #61 (ship to main for issue #6: X).",
            "review_run_id": "r6",
        }}
        ctx = self._ctx(state)
        run = {"id": "r6", "status": "failed"}
        with (
            patch.object(cycle_adapter, "gh") as gh,
            patch.object(cycle_adapter, "fetch_workflow_run", return_value=run),
            patch.object(cycle_adapter, "fetch_verdict", return_value=(None, True)),
            patch.object(cycle_adapter, "update_recovery_state",
                         return_value=({"failure_class": "unknown"}, {"dirty": True}, "manual_review")),
            patch.object(cycle_adapter, "notify_workflow_recovery",
                         return_value=True),
            patch.object(cycle_adapter, "log") as log,
        ):
            gh.return_value = _cp(stdout=json.dumps(
                {"number": 61, "state": "OPEN", "baseRefName": "main"}))
            cycle_adapter._complete_reviews(ctx, None, None)
            cycle_adapter._complete_reviews(ctx, None, None)
        failed_logs = [
            c.args[0] for c in log.call_args_list if "REVIEW FAILED" in c.args[0]]
        self.assertEqual(len(failed_logs), 1)

    def test_requeue_after_clear_dispatches_review(self):
        state = {"item-11": {"status": "In Review", "issue_number": 11}}
        item = {
            "id": "item-11",
            "status": "In Review",
            "content": {
                "__typename": "Issue",
                "number": 11,
                "title": "X",
                "repository": {"nameWithOwner": "o/r"},
            },
        }
        cfg = {
            "repo": "o/r",
            "lanes": {"In Review": "review", "Done": "done"},
        }
        ctx = cycle_adapter.PollContext(
            cfg=cfg, env={}, state=state, project_id="p", field_id="f",
            status_options={"Done": "done-opt"}, items=[item],
            first_run=False, done_lane_name="Done", todo_lane_name="Todo",
            blocked_lane_name="Blocked", ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={}, number_state={}, seen=set(), fresh_dispatched=set())
        with patch.object(
            cycle_adapter, "ensure_ship_review",
            return_value=("ok", "m", 111),
        ) as ensure:
            cycle_adapter._recheck_review_dispatch(ctx)
        ensure.assert_called_once()


class ShipReviewCapacityTest(unittest.TestCase):
    def test_capacity_deferral_is_not_dispatch_failure(self):
        cfg = {
            "repo": "o/r",
            "max_concurrent_workflows": 1,
            "lanes": {"Done": "done"},
            "dispatch": {"todo": {"merge_develop_base": "develop"},
                         "review": {"workflow": "archon-smart-pr-review",
                                    "ship_to": "main"}},
        }
        rec = {}
        with (
            patch.object(cycle_adapter, "find_issue_pr", return_value=(None, True)),
            patch.object(cycle_adapter, "branch_empty_vs_main",
                         return_value=False),
            patch.object(cycle_adapter, "find_or_create_ship_pr",
                         return_value={"number": 51}),
            patch.object(cycle_adapter, "dispatch",
                         return_value=DispatchResult(False, "capacity")),
            patch.object(cycle_adapter, "note_capacity_deferred") as note,
            patch.object(cycle_adapter, "log") as log,
        ):
            result = cycle_adapter.ensure_ship_review(
                cfg, {}, "item-5", 5, "Title", "p", "f", {}, None, rec)
        self.assertIsNone(result)
        note.assert_called_once()
        failed = [
            c.args[0] for c in log.call_args_list
            if "SHIP REVIEW DISPATCH FAILED" in c.args[0]]
        self.assertEqual(failed, [])

    def test_real_spawn_failure_still_logs_failed(self):
        cfg = {
            "repo": "o/r",
            "max_concurrent_workflows": 1,
            "lanes": {"Done": "done"},
            "dispatch": {"todo": {"merge_develop_base": "develop"},
                         "review": {"workflow": "archon-smart-pr-review",
                                    "ship_to": "main"}},
        }
        rec = {}
        with (
            patch.object(cycle_adapter, "find_issue_pr", return_value=(None, True)),
            patch.object(cycle_adapter, "branch_empty_vs_main",
                         return_value=False),
            patch.object(cycle_adapter, "find_or_create_ship_pr",
                         return_value={"number": 51}),
            patch.object(cycle_adapter, "dispatch",
                         return_value=DispatchResult(False, "spawn_failed")),
            patch.object(cycle_adapter, "log") as log,
        ):
            result = cycle_adapter.ensure_ship_review(
                cfg, {}, "item-5", 5, "Title", "p", "f", {}, None, rec)
        self.assertIsNone(result)
        failed = [
            c.args[0] for c in log.call_args_list
            if "SHIP REVIEW DISPATCH FAILED" in c.args[0]]
        self.assertEqual(len(failed), 1)


class ConflictDispatchSerializationTest(unittest.TestCase):
    """At most one conflict-resolver run at a time; siblings defer."""

    def setUp(self):
        cycle_adapter._POLL_CONFLICT_FIXERS.clear()

    def _cfg(self):
        return {
            "repo": "o/r",
            "lanes": {"In Review": "review", "Done": "done"},
            "dispatch": {"review": {
                "conflict_fix_workflow": "archon-fix-ship-conflicts",
                "ship_to": "main",
                "done_lane": "Done",
                "merge_ship_on_approve": True,
            }},
        }

    def _ctx(self, state):
        return cycle_adapter.PollContext(
            cfg=self._cfg(), env={}, state=state, project_id="p", field_id="f",
            status_options={}, items=[],
            first_run=False, done_lane_name="Done", todo_lane_name="Todo",
            blocked_lane_name="Blocked", ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={}, number_state={}, seen=set(), fresh_dispatched=set())

    def _ship(self, cfg, env, number, base):
        return {"number": 100 + number, "mergeable": "CONFLICTING",
                "headRefName": f"issue-{number}"}

    def test_second_conflicting_item_defers_while_fixer_active(self):
        state = {
            "item-1": {
                "status": "In Review", "issue_number": 1,
                "conflict_fix_msg": "Resolve merge conflicts on ship PR #101 "
                                    "(issue #1).",
                "conflict_mech_failed": True,
            },
            "item-2": {
                "status": "In Review", "issue_number": 2,
                "conflict_mech_failed": True,
            },
        }
        runs_by_msg = {
            "Resolve merge conflicts on ship PR #101 (issue #1).": "running",
        }
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "find_ship_pr",
                         side_effect=self._ship) as find,
            patch.object(cycle_adapter, "dispatch") as disp,
            patch.object(cycle_adapter, "comment_issue", return_value=True),
            patch.object(cycle_adapter, "log") as log,
        ):
            cycle_adapter._remediate_ship_conflicts(ctx, runs_by_msg,
                                                    "In Review", "main")
        disp.assert_not_called()
        self.assertTrue(state["item-2"]["conflict_fix_deferred"])
        deferred_logs = [
            c.args[0] for c in log.call_args_list
            if "SHIP CONFLICT DISPATCH DEFERRED" in c.args[0]]
        self.assertEqual(len(deferred_logs), 1)

    def test_two_fresh_conflicts_dispatch_only_one_per_poll(self):
        state = {
            "item-1": {"status": "In Review", "issue_number": 1,
                       "conflict_mech_failed": True},
            "item-2": {"status": "In Review", "issue_number": 2,
                       "conflict_mech_failed": True},
        }
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "find_ship_pr",
                         side_effect=self._ship),
            patch.object(cycle_adapter, "dispatch",
                         return_value=DispatchResult(True, pid=1)) as disp,
            patch.object(cycle_adapter, "comment_issue", return_value=True),
            patch.object(cycle_adapter, "log"),
        ):
            cycle_adapter._remediate_ship_conflicts(ctx, {}, "In Review", "main")
        disp.assert_called_once()
        self.assertIn("conflict_fix_msg", state["item-1"])
        self.assertTrue(state["item-2"]["conflict_fix_deferred"])

    def test_clear_ends_deferral_marker(self):
        state = {
            "item-3": {
                "status": "In Review", "issue_number": 3,
                "conflict_fix_deferred": True,
                "conflict_fix_msg": "fix",
                "conflict_mech_failed": True,
                "conflict_fix_noted": True,
                "ship_pr": 103,
            },
        }
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "find_ship_pr",
                         return_value={"number": 103, "mergeable": "MERGEABLE",
                                       "headRefName": "issue-3"}),
            patch.object(cycle_adapter, "fetch_verdict",
                         return_value=(None, True)),
            patch.object(cycle_adapter, "log"),
        ):
            cycle_adapter._remediate_ship_conflicts(ctx, {}, "In Review", "main")
        self.assertNotIn("conflict_fix_deferred", state["item-3"])

    def test_failed_fix_run_redispatches_once(self):
        state = {
            "item-4": {
                "status": "In Review", "issue_number": 4,
                "conflict_fix_msg": "Resolve merge conflicts on ship PR #104 "
                                    "(issue #4).",
                "conflict_mech_failed": True,
                "conflict_fix_retried": True,
            },
        }
        runs_by_msg = {
            "Resolve merge conflicts on ship PR #104 (issue #4).": "failed",
        }
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "find_ship_pr",
                         return_value={"number": 104, "mergeable": "CONFLICTING",
                                       "headRefName": "issue-4"}),
            patch.object(cycle_adapter, "dispatch") as disp,
            patch.object(cycle_adapter, "comment_issue", return_value=True) as cm,
            patch.object(cycle_adapter, "log"),
        ):
            cycle_adapter._remediate_ship_conflicts(ctx, runs_by_msg,
                                                    "In Review", "main")
        disp.assert_not_called()
        cm.assert_called_once()
        self.assertTrue(state["item-4"]["conflict_fix_noted"])

    def test_failed_fix_run_escalates_after_retry(self):
        state = {
            "item-4": {
                "status": "In Review", "issue_number": 4,
                "conflict_fix_msg": "Resolve merge conflicts on ship PR #104 "
                                    "(issue #4).",
                "conflict_mech_failed": True,
            },
        }
        runs_by_msg = {
            "Resolve merge conflicts on ship PR #104 (issue #4).": "failed",
        }
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "find_ship_pr",
                         return_value={"number": 104, "mergeable": "CONFLICTING",
                                       "headRefName": "issue-4"}),
            patch.object(cycle_adapter, "dispatch",
                         return_value=DispatchResult(True, pid=1)) as disp,
            patch.object(cycle_adapter, "comment_issue", return_value=True) as cm,
            patch.object(cycle_adapter, "log"),
        ):
            cycle_adapter._remediate_ship_conflicts(ctx, runs_by_msg,
                                                    "In Review", "main")
        disp.assert_called_once()
        cm.assert_not_called()
        self.assertTrue(state["item-4"]["conflict_fix_retried"])
        self.assertFalse(state["item-4"]["conflict_fix_noted"])


class FillConcurrencyGapTest(unittest.TestCase):
    """The poller self-fills free dispatch slots from runnable Backlog."""

    def _ctx(self, state, cap=6, repo="o/r"):
        cfg = {
            "repo": repo,
            "default_lane": "Backlog",
            "max_concurrent_workflows": cap,
            "lanes": {"Backlog": "backlog", "Todo": "todo",
                      "In Progress": "in_progress", "Done": "done"},
            "dispatch": {"todo": {"default": "archon-fix-github-issue"}},
        }
        return cycle_adapter.PollContext(
            cfg=cfg, env={}, state=state, project_id="p", field_id="f",
            status_options={"Todo": "todo-opt", "Done": "done-opt"},
            items=[], first_run=False,
            done_lane_name="Done", todo_lane_name="Todo",
            blocked_lane_name="Blocked", ready_lane_name="Ready for Review",
            in_progress_lane_name="In Progress",
            number_lane={}, number_state={}, seen=set(), fresh_dispatched=set())

    def _item(self, number, labels=(), body="", state="OPEN"):
        return {
            "id": f"item-{number}",
            "status": "Backlog",
            "content": {
                "__typename": "Issue",
                "number": number,
                "title": f"Issue {number}",
                "state": state,
                "url": f"https://example.com/{number}",
                "repository": {"nameWithOwner": "o/r"},
                "body": body,
                "labels": {"nodes": [{"name": l} for l in labels]},
            },
        }

    def test_fills_free_slots_from_runnable_backlog(self):
        state = {}
        items = [self._item(1), self._item(2), self._item(3)]
        ctx = self._ctx(state, cap=2)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget",
                         return_value=6),
            patch.object(cycle_adapter, "move_to_lane",
                         return_value=True) as move,
            patch.object(cycle_adapter, "log") as log,
        ):
            promoted = cycle_adapter._fill_concurrency_gap(
                ctx, items, {}, {})
        self.assertEqual(promoted, 2)
        self.assertEqual(move.call_count, 2)
        self.assertEqual([i["status"] for i in items[:2]], ["Todo", "Todo"])
        self.assertEqual(items[2]["status"], "Backlog")

    def test_does_not_exceed_cap(self):
        state = {}
        items = [self._item(n) for n in range(1, 8)]
        ctx = self._ctx(state, cap=6)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget",
                         return_value=6),
            patch.object(cycle_adapter, "move_to_lane", return_value=True),
            patch.object(cycle_adapter, "log"),
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, items, {}, {})
        self.assertEqual(promoted, 6)

    def test_respects_active_runs(self):
        state = {}
        items = [self._item(1), self._item(2), self._item(3)]
        ctx = self._ctx(state, cap=6)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget",
                         return_value=2),
            patch.object(cycle_adapter, "move_to_lane", return_value=True) as move,
            patch.object(cycle_adapter, "log"),
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, items, {}, {})
        self.assertEqual(promoted, 2)
        self.assertEqual(move.call_count, 2)

    def test_skips_decision_only_needs_input_closed_dep_blocked(self):
        state = {}
        items = [
            self._item(1, labels=["decision-only"]),
            self._item(2, labels=["needs-input"]),
            self._item(3, state="CLOSED"),
            self._item(4, body="Depends on: #99\n"),
            self._item(5),
        ]
        number_lane = {99: "Backlog"}
        number_state = {99: "OPEN"}
        ctx = self._ctx(state, cap=6)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget",
                         return_value=6),
            patch.object(cycle_adapter, "move_to_lane", return_value=True) as move,
            patch.object(cycle_adapter, "log") as log,
        ):
            promoted = cycle_adapter._fill_concurrency_gap(
                ctx, items, number_lane, number_state)
        self.assertEqual(promoted, 1)  # only #5
        self.assertEqual(move.call_count, 1)
        gap = [
            c.args[0] for c in log.call_args_list
            if "CONCURRENCY GAP" in c.args[0]]
        self.assertEqual(len(gap), 1)
        self.assertIn("dep_blocked=1", gap[0])


    def test_failed_move_does_not_promote_or_seed_state(self):
        state = {}
        item = self._item(8)
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget", return_value=1),
            patch.object(cycle_adapter, "move_to_lane", return_value=False) as move,
            patch.object(cycle_adapter, "log") as log,
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, [item], {}, {})
        self.assertEqual(promoted, 0)
        self.assertEqual(item["status"], "Backlog")
        self.assertEqual(state, {})
        move.assert_called_once()
        self.assertTrue(any(
            "CONCURRENCY FILL MOVE FAILED issue=8" in call.args[0]
            for call in log.call_args_list
        ))

    def test_lane_move_timeout_does_not_abort_fill(self):
        state = {}
        item = self._item(8)
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget", return_value=1),
            patch.object(
                cycle_adapter,
                "move_to_lane",
                side_effect=subprocess.TimeoutExpired("gh", 60),
            ),
            patch.object(cycle_adapter, "log") as log,
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, [item], {}, {})
        self.assertEqual(promoted, 0)
        self.assertEqual(item["status"], "Backlog")
        self.assertEqual(state, {})
        self.assertTrue(any(
            "TimeoutExpired" in call.args[0] for call in log.call_args_list
        ))

    def test_missing_todo_lane_is_reported(self):
        state = {}
        item = self._item(8)
        ctx = self._ctx(state)
        ctx.status_options = {}
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget", return_value=1),
            patch.object(cycle_adapter, "move_to_lane") as move,
            patch.object(cycle_adapter, "log") as log,
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, [item], {}, {})
        self.assertEqual(promoted, 0)
        move.assert_not_called()
        self.assertTrue(any(
            "MOVE SKIPPED issue=8" in call.args[0] for call in log.call_args_list
        ))

    def test_promotes_issue_when_all_dependencies_are_done(self):
        state = {}
        item = self._item(8, body="Depends on: #7")
        ctx = self._ctx(state)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget", return_value=1),
            patch.object(cycle_adapter, "move_to_lane", return_value=True),
        ):
            promoted = cycle_adapter._fill_concurrency_gap(
                ctx, [item], {7: "Done"}, {7: "CLOSED"})
        self.assertEqual(promoted, 1)
        self.assertEqual(item["status"], "Todo")

    def test_full_budget_returns_zero(self):
        state = {}
        items = [self._item(1)]
        ctx = self._ctx(state, cap=6)
        with (
            patch.object(cycle_adapter, "remaining_dispatch_budget",
                         return_value=0),
            patch.object(cycle_adapter, "move_to_lane") as move,
            patch.object(cycle_adapter, "log"),
        ):
            promoted = cycle_adapter._fill_concurrency_gap(ctx, items, {}, {})
        self.assertEqual(promoted, 0)
        move.assert_not_called()


class PollConcurrencyFillTest(unittest.TestCase):
    """Verify fill wiring, first-poll safety, and next-poll dispatch timing."""

    def _cfg(self):
        return {
            "repo": "o/r",
            "state_file": "state.json",
            "lanes": {
                "Backlog": "backlog",
                "Todo": "todo",
                "In Progress": "in_progress",
                "Ready for Review": "ready",
                "In Review": "review",
                "Done": "done",
            },
            "max_concurrent_workflows": 1,
            "dispatch": {
                "todo": {"default": "archon-fix-github-issue"},
                "review": {
                    "merge_ship_on_approve": False,
                    "ship_to": "main",
                    "done_lane": "Done",
                },
            },
        }

    def _item(self, status="Backlog"):
        return {
            "id": "item-8",
            "status": status,
            "content": {
                "__typename": "Issue",
                "number": 8,
                "title": "Runnable backlog issue",
                "url": "https://github.com/o/r/issues/8",
                "body": "",
                "state": "OPEN",
                "repository": {"nameWithOwner": "o/r"},
                "labels": {"nodes": []},
            },
        }

    def test_snapshot_then_promotes_and_dispatches_on_next_poll(self):
        state = {}
        snapshot_item = self._item()
        promoted_item = self._item()
        todo_item = self._item("Todo")
        with (
            patch.object(
                cycle_adapter,
                "fetch_project",
                side_effect=[
                    ("p", "f", {"Todo": "todo-opt"}, [snapshot_item]),
                    ("p", "f", {"Todo": "todo-opt"}, [promoted_item]),
                    ("p", "f", {"Todo": "todo-opt"}, [todo_item]),
                ],
            ),
            patch.object(cycle_adapter, "prepare_dispatch_budget"),
            patch.object(cycle_adapter, "remaining_dispatch_budget", return_value=1),
            patch.object(cycle_adapter, "sync_runnable_labels"),
            patch.object(cycle_adapter, "reconcile_untracked_runs"),
            patch.object(cycle_adapter, "_recheck_review_dispatch"),
            patch.object(cycle_adapter, "_unblock_dependencies"),
            patch.object(cycle_adapter, "_reconcile_completions", return_value=(None, {})),
            patch.object(cycle_adapter, "_enforce_ready_proof"),
            patch.object(
                cycle_adapter,
                "_complete_reviews",
                return_value=(None, {}, "In Review", "main"),
            ),
            patch.object(cycle_adapter, "_remediate_ship_conflicts"),
            patch.object(cycle_adapter, "fresh_issue_dispatch_guard", return_value=(True, "")),
            patch.object(cycle_adapter, "dispatch", return_value=DispatchResult(True)) as dispatch,
            patch.object(cycle_adapter, "move_to_lane", return_value=True) as move,
            patch.object(cycle_adapter, "save_state"),
        ):
            cycle_adapter.poll(self._cfg(), {}, state)
            self.assertEqual(snapshot_item["status"], "Backlog")
            dispatch.assert_not_called()

            cycle_adapter.poll(self._cfg(), {}, state)
            self.assertEqual(promoted_item["status"], "Todo")
            dispatch.assert_not_called()

            cycle_adapter.poll(self._cfg(), {}, state)

        move.assert_called_once_with(
            self._cfg(), {}, "p", "item-8", "f", "todo-opt")
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[-1], 8)


class FmtDepsTest(unittest.TestCase):
    def test_formats_refs(self):
        self.assertEqual(fmt_deps([42, 57]), "#42, #57")

    def test_empty(self):
        self.assertEqual(fmt_deps([]), "")


class PickWorkflowTest(unittest.TestCase):
    def _cfg(self):
        return {"dispatch": {"todo": {
            "default": "archon-fix-github-issue",
            "label_overrides": {"feature": "archon-idea-to-pr"},
        }}}

    def test_label_override_wins(self):
        self.assertEqual(pick_workflow(self._cfg(), ["enhancement", "feature"]),
                         "archon-idea-to-pr")

    def test_label_match_is_case_insensitive(self):
        self.assertEqual(pick_workflow(self._cfg(), ["Feature"]),
                         "archon-idea-to-pr")

    def test_falls_back_to_default(self):
        self.assertEqual(pick_workflow(self._cfg(), ["docs"]),
                         "archon-fix-github-issue")

    def test_empty_labels(self):
        self.assertEqual(pick_workflow(self._cfg(), []),
                         "archon-fix-github-issue")


class RunStatusForTest(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(run_status_for({"m1": "completed"}, "m1"), "completed")

    def test_substring_match(self):
        self.assertEqual(
            run_status_for({"Prior Context m1": "running"}, "m1"), "running")

    def test_no_match(self):
        self.assertIsNone(run_status_for({"other": "completed"}, "m1"))

    def test_empty_map(self):
        self.assertIsNone(run_status_for({}, "m1"))


class MergePrToBaseTest(unittest.TestCase):
    def test_already_merged_short_circuits(self):
        ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                    {"number": 1, "state": "MERGED"}, "develop", 5)
        self.assertTrue(ok)
        self.assertIn("already merged", note)

    def test_closed_without_merge_is_a_loud_failure(self):
        ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                    {"number": 1, "state": "CLOSED"}, "develop", 5)
        self.assertFalse(ok)
        self.assertIn("without merging", note)

    def _reopen_flow(self, issue_state="CLOSED"):
        calls = []

        def fake_gh(args, env, timeout=90):
            calls.append(args)
            if args[0:2] == ["pr", "edit"]:
                return _cp()
            if args[0:2] == ["pr", "ready"]:
                return _cp()
            if args[0:2] == ["pr", "merge"]:
                return _cp()
            if args[0:2] == ["issue", "view"]:
                return _cp(stdout=json.dumps({"state": issue_state}))
            if args[0:2] == ["issue", "reopen"]:
                return _cp()
            return _cp(returncode=1)
        return calls, fake_gh

    def test_reopens_issue_after_keyword_auto_close(self):
        calls, fake_gh = self._reopen_flow("CLOSED")
        with patch("automation.pm_harness.github.gh", side_effect=fake_gh), \
                patch("automation.pm_harness.github.time.sleep"):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"},
                                        "develop", 5)
        self.assertTrue(ok)
        self.assertIn("reopened", note)
        self.assertTrue(any(a[0:2] == ["issue", "reopen"] for a in calls))

    def test_no_reopen_when_issue_still_open(self):
        calls, fake_gh = self._reopen_flow("OPEN")
        with patch("automation.pm_harness.github.gh", side_effect=fake_gh), \
                patch("automation.pm_harness.github.time.sleep"):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"},
                                        "develop", 5)
        self.assertTrue(ok)
        self.assertFalse(any(a[0:2] == ["issue", "reopen"] for a in calls))

    def test_merge_without_issue_number_skips_reopen_check(self):
        calls, fake_gh = self._reopen_flow("CLOSED")
        with patch("automation.pm_harness.github.gh", side_effect=fake_gh):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"}, "develop")
        self.assertTrue(ok)
        self.assertFalse(any(a[0:2] == ["issue", "view"] for a in calls))

    def test_merge_failure_reported(self):
        def fake_gh(args, env, timeout=90):
            if args[0:2] == ["pr", "edit"]:
                return _cp()
            if args[0:2] == ["pr", "ready"]:
                return _cp()
            if args[0:2] == ["pr", "merge"]:
                return _cp(returncode=1, stderr="merge failed: conflict")
            return _cp(returncode=1)
        with patch("automation.pm_harness.github.gh", side_effect=fake_gh):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"}, "develop")
        self.assertFalse(ok)
        self.assertIn("merge failed", note)


class TryMergeBaseIntoHeadTest(unittest.TestCase):
    def _run(self, stderr="", returncode=1):
        with patch("automation.pm_harness.github.gh",
                   return_value=_cp(returncode=returncode, stderr=stderr)):
            return try_merge_base_into_head({"repo": "o/r"}, {}, 7, "head", "main")

    def test_success(self):
        self.assertEqual(self._run(returncode=0),
                         (True, "base merged into head"))

    def test_conflict_bucket(self):
        self.assertEqual(self._run(stderr="Merge conflict in file.py"),
                         (False, "conflict"))

    def test_no_commits_between_bucket(self):
        self.assertEqual(self._run(stderr="no commits between main and head"),
                         (True, "no-op (head already contains base)"))

    def test_already_up_to_date_bucket(self):
        self.assertEqual(self._run(stderr="already up to date"),
                         (True, "no-op (head already contains base)"))

    def test_transient_bucket(self):
        ok, note = self._run(stderr="Server error (500)")
        self.assertFalse(ok)
        self.assertTrue(note.startswith("transient:"))


class FetchProjectTest(unittest.TestCase):
    def _cfg(self):
        return {"project_number": 1, "project_owner": "o", "status_field": "Status"}

    def test_missing_project_raises(self):
        data = {"data": {"user": {"projectV2": None}}}
        with patch("automation.pm_harness.github.graphql", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                fetch_project(self._cfg(), {})

    def test_missing_status_field_raises(self):
        data = {"data": {"user": {"projectV2": {
            "id": "pv1",
            "fields": {"nodes": []},
            "items": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }}}}
        with patch("automation.pm_harness.github.graphql", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "status field"):
                fetch_project(self._cfg(), {})

    def test_pagination_assembles_all_items(self):
        def fake_graphql(cfg, env, cursor):
            if cursor is None:
                return {"data": {"user": {"projectV2": {
                    "id": "pv1",
                    "fields": {"nodes": [{"id": "f1", "name": "Status", "options": [{"name": "Todo", "id": "o1"}]}]},
                    "items": {"nodes": [{"id": "i1", "statusValue": {"name": "Todo"}, "content": {"number": 1}}],
                              "pageInfo": {"hasNextPage": True, "endCursor": "c2"}},
                }}}}
            return {"data": {"user": {"projectV2": {
                "items": {"nodes": [{"id": "i2", "statusValue": None, "content": {"number": 2}}],
                          "pageInfo": {"hasNextPage": False}},
            }}}}

        with patch("automation.pm_harness.github.graphql", side_effect=fake_graphql):
            pid, fid, options, items = fetch_project(self._cfg(), {})
        self.assertEqual(options, {"Todo": "o1"})
        self.assertEqual([i["id"] for i in items], ["i1", "i2"])
        self.assertEqual(items[1]["status"], "No status")


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)





if __name__ == "__main__":
    unittest.main()
