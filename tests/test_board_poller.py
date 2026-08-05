from __future__ import annotations

import contextlib
import json
import subprocess
import unittest
from unittest.mock import patch
import automation.board_poller as board_poller
from automation.board_poller import (
    branch_empty_vs_main,
    build_ready_for_review_comment,
    conflict_episode_action,
    dedupe_deferred,
    dep_gate,
    dispatch,
    develop_conflict_action,
    extract_test_guidance,
    fetch_active_workflow_count,
    fetch_project,
    find_unchecked_criteria,
    fmt_deps,
    has_deferral_language,
    issue_is_runnable,
    match_issue_pr,
    merge_pr_to_base,
    normalize_title,
    parse_dep_refs,
    parse_deferred_work,
    parse_verdict,
    pick_workflow,
    post_ready_for_review_comment,
    prepare_dispatch_budget,
    reconcile_deferred_work,
    run_status_for,
    sync_local_develop,
    sync_runnable_labels,
    try_merge_base_into_head,
)


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
        with patch("automation.board_poller.subprocess.run", return_value=result):
            self.assertEqual(fetch_active_workflow_count({}), 2)

    def test_active_workflow_count_fails_closed(self):
        result = subprocess.CompletedProcess(["archon"], 1, "", "unavailable")
        with patch("automation.board_poller.subprocess.run", return_value=result):
            self.assertIsNone(fetch_active_workflow_count({}))

    def test_budget_reserves_slots_after_existing_runs(self):
        with (
            patch.object(board_poller, "DRY_RUN", False),
            patch.object(board_poller, "fetch_active_workflow_count", return_value=2),
        ):
            prepare_dispatch_budget({"max_concurrent_workflows": 3}, {})
            self.assertEqual(board_poller._DISPATCH_BUDGET, 1)

    def test_budget_holds_when_status_lookup_fails(self):
        with (
            patch.object(board_poller, "DRY_RUN", False),
            patch.object(board_poller, "fetch_active_workflow_count", return_value=None),
        ):
            prepare_dispatch_budget({"max_concurrent_workflows": 3}, {})
            self.assertEqual(board_poller._DISPATCH_BUDGET, 0)

    def test_dispatch_holds_when_budget_is_exhausted(self):
        with (
            patch.object(board_poller, "DRY_RUN", False),
            patch.object(board_poller, "_DISPATCH_BUDGET", 0),
            patch("automation.board_poller.subprocess.Popen") as popen,
        ):
            self.assertFalse(dispatch({}, {}, "workflow", "branch", "message", "item", 7))
            popen.assert_not_called()

    def test_dispatch_consumes_reserved_slot(self):
        with (
            patch.object(board_poller, "DRY_RUN", False),
            patch.object(board_poller, "_DISPATCH_BUDGET", 1),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("automation.board_poller.subprocess.Popen") as popen,
        ):
            popen.return_value.pid = 123
            self.assertTrue(dispatch({}, {}, "workflow", "branch", "message", "item", 7))
            self.assertEqual(board_poller._DISPATCH_BUDGET, 0)


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
            conflict_episode_action("CONFLICTING", "m", "completed", True), "failed")

    def test_fix_run_failed_status(self):
        self.assertEqual(
            conflict_episode_action("CONFLICTING", "m", "failed", True), "failed")

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

    @patch("automation.board_poller.gh")
    def test_empty_branch_is_already_shipped(self, gh):
        gh.return_value = self._gh('{"ahead_by": 0}')
        self.assertTrue(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.board_poller.gh")
    def test_ahead_branch_is_shippable(self, gh):
        gh.return_value = self._gh('{"ahead_by": 5}')
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.board_poller.gh")
    def test_api_error_is_not_shipped(self, gh):
        gh.return_value = self._gh("", returncode=1)
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    @patch("automation.board_poller.gh")
    def test_unparseable_is_not_shipped(self, gh):
        gh.return_value = self._gh("not json")
        self.assertFalse(branch_empty_vs_main({"repo": "r"}, {}, "head", "main"))

    def test_mech_failed_dispatches_resolver(self):
        self.assertEqual(develop_conflict_action(True, None, None), "dispatch")

    def test_resolver_active_waits(self):
        for st in ("running", "pending", "queued", "scheduled", "paused"):
            self.assertEqual(develop_conflict_action(True, "m", st), "active")

    def test_resolver_done_still_failing_needs_human(self):
        self.assertEqual(develop_conflict_action(True, "m", "completed"), "failed")
        self.assertEqual(develop_conflict_action(True, "m", "failed"), "failed")


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

    @patch("automation.board_poller.gh")
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

    def test_build_is_explicit_when_no_test_path_was_recorded(self):
        body = build_ready_for_review_comment(7, "develop", None, None)
        self.assertIn("no linked develop PR was available", body)
        self.assertIn("No runnable issue-specific instructions", body)

    @patch("automation.board_poller.gh")
    def test_post_fetches_guidance_then_comments(self, gh):
        gh.side_effect = [
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({
                    "comments": [{"body": "## How to test\nRun the focused check."}],
                }), stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        with patch.object(board_poller, "DRY_RUN", False):
            self.assertTrue(
                post_ready_for_review_comment(
                    {"repo": "o/r"}, {}, 7, "develop", 9))
        self.assertEqual(gh.call_args_list[0].args[0][:2], ["issue", "view"])
        comment_args = gh.call_args_list[1].args[0]
        self.assertEqual(comment_args[:2], ["issue", "comment"])
        self.assertIn("Run the focused check.", comment_args[-1])


class ReadyForReviewTransitionTest(unittest.TestCase):
    def test_completed_run_posts_handoff_after_develop_merge(self):
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
            "deferred_work": {"enabled": True},
        }
        state = {
            "_meta": {"snapshot_done": True},
            item_id: {
                "status": "In Progress",
                "issue_number": 92,
                "dispatch_msg": "run",
            },
        }
        with (
            patch.object(board_poller, "fetch_project",
                         return_value=("p", "f", {"Ready for Review": "ready"}, [item])),
            patch.object(board_poller, "prepare_dispatch_budget"),
            patch.object(board_poller, "sync_runnable_labels"),
            patch.object(board_poller, "fetch_runs_by_message",
                         return_value={"run": "completed"}),
            patch.object(board_poller, "issue_has_label", return_value=False),
            patch.object(board_poller, "find_issue_pr",
                         return_value=({"number": 153, "state": "OPEN",
                                        "headRefName": "issue-92"}, True)),
            patch.object(board_poller, "merge_pr_to_base",
                         return_value=(True, "merged")),
            patch.object(board_poller, "sync_local_develop",
                         return_value="local sync skipped"),
            patch.object(board_poller, "reconcile_deferred_work", return_value=True),
            patch.object(board_poller, "move_to_lane", return_value=True) as move,
            patch.object(board_poller, "post_ready_for_review_comment",
                         return_value=True) as post,
            patch.object(board_poller, "save_state"),
        ):
            board_poller.poll(cfg, {}, state)
        move.assert_called_once_with(cfg, {}, "p", "item-92", "f", "ready")
        post.assert_called_once_with(cfg, {}, 92, "develop", 153)
        self.assertTrue(state[item_id]["ready_test_comment"])


class ParseDeferredWorkTest(unittest.TestCase):
    def _record(self, section):
        return ("## What shipped\nStuff.\n\n## Deferred work\n" + section
                + "\n\n## Decisions\nNone.")

    def test_full_section(self):
        body = self._record(
            "- **Title:** Add llama.cpp/GGUF backend support\n"
            "  **Description:** Port the model layer to llama.cpp.\n"
            "  **Reason:** Packaging work beyond this decision.\n"
            "  **Label:** feature\n"
            "- **Title:** Extract shared readiness helper\n"
            "  **Description:** Merge the two readiness loops.\n")
        self.assertEqual(parse_deferred_work(body), [
            {"title": "Add llama.cpp/GGUF backend support",
             "description": "Port the model layer to llama.cpp.",
             "reason": "Packaging work beyond this decision.",
             "label": "feature",
             "links_to": None, "supersedes": None, "skip": "",
             "out_of_scope": ""},
            {"title": "Extract shared readiness helper",
             "description": "Merge the two readiness loops.",
             "reason": "", "label": "",
             "links_to": None, "supersedes": None, "skip": "",
             "out_of_scope": ""},
        ])

    def test_none_marker(self):
        self.assertEqual(parse_deferred_work(self._record("*None.*")), [])
        self.assertEqual(parse_deferred_work(self._record("\n*none*\n")), [])

    def test_absent_section(self):
        self.assertIsNone(parse_deferred_work("## What shipped\nNo deferrals."))
        self.assertIsNone(parse_deferred_work(""))

    def test_section_terminates_at_next_heading(self):
        body = self._record(
            "- **Title:** First\n  **Description:** one\n\n## Decisions\n"
            "- **Title:** Not mine\n")
        self.assertEqual(parse_deferred_work(body), [
            {"title": "First", "description": "one",
             "reason": "", "label": "",
             "links_to": None, "supersedes": None, "skip": "",
             "out_of_scope": ""}])

    def test_case_insensitive_heading(self):
        body = "## DEFERRED WORK\n- **Title:** X\n"
        self.assertEqual(parse_deferred_work(body)[0]["title"], "X")

    def test_malformed_item_skipped(self):
        body = self._record(
            "- **Title:** Good one\n  **Description:** fine\n"
            "- **Description:** orphan (no title)\n")
        items = parse_deferred_work(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Good one")

    def test_links_to_field(self):
        body = self._record(
            "- **Title:** GGUF backend\n"
            "  **Description:** port to llama.cpp\n"
            "  **Links to:** #75\n")
        item = parse_deferred_work(body)[0]
        self.assertEqual(item["links_to"], 75)
        self.assertIsNone(item["supersedes"])
        self.assertEqual(item["skip"], "")

    def test_links_to_bare_number(self):
        body = self._record("- **Title:** X\n  **Links to:** 52\n")
        self.assertEqual(parse_deferred_work(body)[0]["links_to"], 52)

    def test_supersedes_field(self):
        body = self._record("- **Title:** X\n  **Supersedes:** #9\n")
        item = parse_deferred_work(body)[0]
        self.assertEqual(item["supersedes"], 9)
        self.assertIsNone(item["links_to"])

    def test_skip_field(self):
        body = self._record("- **Title:** X\n  **Skip:** HANDOFF forbids this\n")
        item = parse_deferred_work(body)[0]
        self.assertEqual(item["skip"], "HANDOFF forbids this")

    def test_out_of_scope_field(self):
        body = self._record("- **Title:** X\n  **Out of scope:** dark-mode\n")
        item = parse_deferred_work(body)[0]
        self.assertEqual(item["out_of_scope"], "dark-mode")
        self.assertEqual(item["skip"], "")

    def test_bare_item_has_no_stamps(self):
        body = self._record("- **Title:** X\n  **Description:** d\n")
        item = parse_deferred_work(body)[0]
        self.assertIsNone(item["links_to"])
        self.assertIsNone(item["supersedes"])
        self.assertEqual(item["skip"], "")

    def test_multiple_items_keep_stamps_separate(self):
        body = self._record(
            "- **Title:** A\n  **Links to:** #30\n"
            "- **Title:** B\n  **Skip:** already covered\n")
        a, b = parse_deferred_work(body)
        self.assertEqual(a["links_to"], 30)
        self.assertEqual(b["skip"], "already covered")
        self.assertIsNone(b["links_to"])


class DeferredDedupeTest(unittest.TestCase):
    def test_open_match_links(self):
        item = {"title": "Add llama.cpp/GGUF backend support"}
        open_issues = [{"number": 52, "title": "add llama.cpp GGUF backend support!"}]
        self.assertEqual(dedupe_deferred(item, open_issues, []), ("link", 52))

    def test_closed_match_creates_with_ref(self):
        item = {"title": "Extract shared readiness helper"}
        closed = [{"number": 9, "title": "Extract shared readiness helper"}]
        self.assertEqual(dedupe_deferred(item, [], closed), ("create-ref", 9))

    def test_no_match_creates(self):
        self.assertEqual(
            dedupe_deferred({"title": "Brand new thing"}, [], []),
            ("create", None))

    def test_punctuation_and_case_insensitive(self):
        item = {"title": "Update OR supersede ADR 0007"}
        open_issues = [{"number": 35, "title": "Update or supersede ADR-0007"}]
        self.assertEqual(dedupe_deferred(item, open_issues, []), ("link", 35))

    def test_open_wins_over_closed(self):
        item = {"title": "Same title"}
        open_issues = [{"number": 1, "title": "Same title"}]
        closed = [{"number": 2, "title": "Same title"}]
        self.assertEqual(dedupe_deferred(item, open_issues, closed), ("link", 1))

    def test_empty_title_creates(self):
        self.assertEqual(dedupe_deferred({"title": ""}, [], []), ("create", None))

    def test_normalize_title(self):
        self.assertEqual(normalize_title("  Add GGUF (v2) support! "), "addggufv2support")
        self.assertEqual(normalize_title(""), "")


class HasDeferralLanguageTest(unittest.TestCase):
    def test_matches_deferral_phrasing(self):
        for text in ("explicitly deferred", "this is deferred to a later phase",
                     "out of scope for this issue", "not in scope",
                     "recorded as a follow-up issue", "deferring the packaging work"):
            self.assertTrue(has_deferral_language(text), text)

    def test_clean_text_does_not_match(self):
        for text in ("All review findings were addressed in the PR.",
                     "249 passed + 25 subtests", "README.md updated", ""):
            self.assertFalse(has_deferral_language(text), text)


class FindUncheckedCriteriaTest(unittest.TestCase):
    def test_extracts_unchecked_lines(self):
        body = ("## Acceptance criteria\n"
                "- [x] MLX backend works — test_mlx\n"
                "- [ ] llama.cpp adapter — not built\n"
                "- [ ] GGUF loading — deferred\n")
        self.assertEqual(find_unchecked_criteria(body),
                         ["llama.cpp adapter — not built", "GGUF loading — deferred"])

    def test_checked_lines_ignored(self):
        self.assertEqual(find_unchecked_criteria("- [x] done\n[x] bare\n"), [])

    def test_empty_body(self):
        self.assertEqual(find_unchecked_criteria(""), [])


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
        with patch("automation.board_poller.gh", side_effect=fake_gh), \
                patch("automation.board_poller.time.sleep"):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"},
                                        "develop", 5)
        self.assertTrue(ok)
        self.assertIn("reopened", note)
        self.assertTrue(any(a[0:2] == ["issue", "reopen"] for a in calls))

    def test_no_reopen_when_issue_still_open(self):
        calls, fake_gh = self._reopen_flow("OPEN")
        with patch("automation.board_poller.gh", side_effect=fake_gh), \
                patch("automation.board_poller.time.sleep"):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"},
                                        "develop", 5)
        self.assertTrue(ok)
        self.assertFalse(any(a[0:2] == ["issue", "reopen"] for a in calls))

    def test_merge_without_issue_number_skips_reopen_check(self):
        calls, fake_gh = self._reopen_flow("CLOSED")
        with patch("automation.board_poller.gh", side_effect=fake_gh):
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
        with patch("automation.board_poller.gh", side_effect=fake_gh):
            ok, note = merge_pr_to_base({"repo": "o/r"}, {},
                                        {"number": 7, "state": "OPEN"}, "develop")
        self.assertFalse(ok)
        self.assertIn("merge failed", note)


class TryMergeBaseIntoHeadTest(unittest.TestCase):
    def _run(self, stderr="", returncode=1):
        with patch("automation.board_poller.gh",
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


class ReconcileDeferredWorkTest(unittest.TestCase):
    def _cfg(self):
        return {"deferred_work": {"fallback_warn": True},
                "default_lane": "Backlog", "repo": "o/r"}

    def _env(self):
        return {}

    def _run(self, comments, rec=None, runs_msg="run-1"):
        rec = {} if rec is None else rec
        calls = []

        def fake_gh(args, env, timeout=90):
            calls.append(args)
            if args[0:2] == ["issue", "view"]:
                return _cp(stdout=json.dumps({"title": "T",
                                              "comments": comments}))
            if args[0:2] == ["issue", "comment"]:
                return _cp()
            if args[0:2] == ["issue", "list"]:
                return _cp(stdout="[]")
            return _cp(returncode=1)
        with patch("automation.board_poller.gh", side_effect=fake_gh):
            ok = reconcile_deferred_work(
                self._cfg(), self._env(), 5, None, rec,
                runs_msg, "p", "f", {"Backlog": "o1"})
        return ok, rec, calls

    def test_fallback_warn_posts_once_and_marks_handled(self):
        comments = [{"body": "Completed. Some work explicitly deferred to a later phase."}]
        ok, rec, calls = self._run(comments)
        self.assertTrue(ok)
        self.assertTrue(rec["deferred_warned"])
        self.assertEqual(rec["deferred_handled"], "run-1")
        self.assertEqual(
            sum(1 for a in calls if a[0:2] == ["issue", "comment"]), 1)
        self.assertIn("has no `## Deferred work` section",
                      calls[-1][-1])

    def test_handled_marker_skips_entirely(self):
        rec = {"deferred_handled": "run-1"}
        with patch("automation.board_poller.gh") as m:
            ok = reconcile_deferred_work(
                self._cfg(), self._env(), 5, None, rec,
                "run-1", "p", "f", {"Backlog": "o1"})
        self.assertTrue(ok)
        m.assert_not_called()

    def test_fetch_failure_returns_false_without_markers(self):
        def fake_gh(args, env, timeout=90):
            if args[0:2] == ["issue", "view"]:
                return _cp(returncode=1, stderr="rate limited")
            return _cp(returncode=1)
        with patch("automation.board_poller.gh", side_effect=fake_gh):
            rec = {}
            ok = reconcile_deferred_work(
                self._cfg(), self._env(), 5, None, rec,
                "run-1", "p", "f", {"Backlog": "o1"})
        self.assertFalse(ok)
        self.assertEqual(rec, {})

    def test_none_section_with_deferral_language_does_not_warn(self):
        comments = [{"body": ("Completed.\n\n## Deferred work\n*None.*\n\n"
                               "Nothing deferred to a later phase.")}]
        ok, rec, calls = self._run(comments)
        self.assertTrue(ok)
        self.assertNotIn("deferred_warned", rec)
        self.assertEqual(rec["deferred_handled"], "run-1")
        self.assertEqual(
            sum(1 for a in calls if a[0:2] == ["issue", "comment"]), 0)

    def test_empty_section_is_not_fallback_warn(self):
        comments = [{"body": "## Deferred work\n\n(nothing)\n"}]
        ok, rec, _ = self._run(comments)
        self.assertTrue(ok)
        self.assertNotIn("deferred_warned", rec)

    def test_newest_comment_with_section_wins_over_older_items(self):
        comments = [
            {"body": "## Deferred work\n- **Title:** Old item\n"},
            {"body": "## Deferred work\n*None.*\n"},
        ]
        ok, rec, calls = self._run(comments)
        self.assertTrue(ok)
        self.assertEqual(rec["deferred_handled"], "run-1")
        # newest *None.* section must NOT resurface the older run's items
        self.assertNotIn("Deferred work from this run",
                         " ".join(a[-1] for a in calls if a[0:2] == ["issue", "comment"]))

class FetchProjectTest(unittest.TestCase):
    def _cfg(self):
        return {"project_number": 1, "project_owner": "o", "status_field": "Status"}

    def test_missing_project_raises(self):
        data = {"data": {"user": {"projectV2": None}}}
        with patch("automation.board_poller.graphql", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                fetch_project(self._cfg(), {})

    def test_missing_status_field_raises(self):
        data = {"data": {"user": {"projectV2": {
            "id": "pv1",
            "fields": {"nodes": []},
            "items": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }}}}
        with patch("automation.board_poller.graphql", return_value=data):
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

        with patch("automation.board_poller.graphql", side_effect=fake_graphql):
            pid, fid, options, items = fetch_project(self._cfg(), {})
        self.assertEqual(options, {"Todo": "o1"})
        self.assertEqual([i["id"] for i in items], ["i1", "i2"])
        self.assertEqual(items[1]["status"], "No status")


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class SyncLocalDevelopTest(unittest.TestCase):
    """Boundaries of the post-merge local sync: never destructive, restart
    only when the UI is running."""

    def _fake_run(self, plan):
        """Dispatch subprocess.run per git subcommand; anything unplanned fails."""
        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "git":
                if cmd[1] == "rev-list":
                    entries = plan["rev-list"]
                    return entries.pop(0)() if isinstance(entries, list) else entries()
                key = cmd[1] if cmd[1] in ("fetch", "rev-parse", "status",
                                           "merge") else cmd[1]
                return plan[key]()
            if cmd[0] == "pkill":
                return _cp()
            raise AssertionError(f"unexpected command: {cmd}")
        return fake_run

    def _run_with(self, plan, ui_running=None, restart_ok=None):
        patches = [patch("automation.board_poller.subprocess.run",
                         side_effect=self._fake_run(plan))]
        if ui_running is not None:
            patches.append(patch("automation.board_poller._ui_running",
                                 return_value=ui_running))
        if restart_ok is not None:
            patches.append(patch("automation.board_poller._restart_ui",
                                 return_value=restart_ok))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return sync_local_develop()

    def test_dry_run_never_touches_git(self):
        with patch("automation.board_poller.DRY_RUN", True), \
                patch("automation.board_poller.subprocess.run",
                      side_effect=AssertionError("must not run")) as m:
            self.assertIn("dry-run", sync_local_develop())
            m.assert_not_called()

    def test_fetch_failure_is_loud(self):
        msg = self._run_with({"fetch": lambda: _cp(1, stderr="boom")})
        self.assertIn("LOCAL SYNC FAILED", msg)
        self.assertIn("fetch", msg)

    def test_not_on_develop_skips(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="main\n"),
        }
        msg = self._run_with(plan)
        self.assertIn("LOCAL SYNC SKIP", msg)
        self.assertIn("main", msg)

    def test_dirty_tree_skips(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(stdout=" M automation/board_poller.py\n"),
        }
        msg = self._run_with(plan)
        self.assertIn("LOCAL SYNC SKIP", msg)
        self.assertIn("dirty", msg)

    def test_unpushed_commits_skip(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": lambda: _cp(stdout="2\n"),
        }
        msg = self._run_with(plan)
        self.assertIn("LOCAL SYNC SKIP", msg)
        self.assertIn("2 unpushed", msg)

    def test_up_to_date_is_a_noop(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": lambda: _cp(stdout="0\n"),
        }
        msg = self._run_with(plan)
        self.assertIn("up to date", msg)

    def test_behind_merges_and_skips_ui_when_not_running(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": [lambda: _cp(stdout="0\n"), lambda: _cp(stdout="3\n")],
            "merge": lambda: _cp(stdout="Fast-forward\n 3 files changed\n"),
        }
        msg = self._run_with(plan, ui_running=False)
        self.assertIn("develop updated", msg)
        self.assertIn("UI not running", msg)

    def test_behind_restarts_ui_when_running(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": [lambda: _cp(stdout="0\n"), lambda: _cp(stdout="3\n")],
            "merge": lambda: _cp(stdout="Fast-forward\n 3 files changed\n"),
        }
        msg = self._run_with(plan, ui_running=True, restart_ok=True)
        self.assertIn("UI restarted", msg)

    def test_restart_failure_warns(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": [lambda: _cp(stdout="0\n"), lambda: _cp(stdout="3\n")],
            "merge": lambda: _cp(stdout="Fast-forward\n 3 files changed\n"),
        }
        msg = self._run_with(plan, ui_running=True, restart_ok=False)
        self.assertIn("LOCAL SYNC WARNING", msg)

    def test_ff_only_merge_failure_is_loud(self):
        plan = {
            "fetch": lambda: _cp(),
            "rev-parse": lambda: _cp(stdout="develop\n"),
            "status": lambda: _cp(),
            "rev-list": [lambda: _cp(stdout="0\n"), lambda: _cp(stdout="3\n")],
            "merge": lambda: _cp(1, stderr="Not possible to fast-forward"),
        }
        msg = self._run_with(plan)
        self.assertIn("LOCAL SYNC FAILED", msg)
        self.assertIn("fast-forward", msg)


if __name__ == "__main__":
    unittest.main()
