from __future__ import annotations

import contextlib
import json
import subprocess
import unittest
from unittest.mock import MagicMock, mock_open, patch

from automation import board_poller as bp
from automation.board_poller import (
    branch_empty_vs_main,
    conflict_episode_action,
    dedupe_deferred,
    dep_gate,
    issue_is_runnable,
    develop_conflict_action,
    fetch_project,

    find_unchecked_criteria,
    fmt_deps,
    has_deferral_language,
    match_issue_pr,
    merge_pr_to_base,
    normalize_title,
    parse_dep_refs,
    parse_deferred_work,
    parse_verdict,
    pick_workflow,
    reconcile_deferred_work,
    run_status_for,
    sync_local_develop,
    try_merge_base_into_head,
    sync_runnable_labels,
)


REPO = "bradley-mankoff/news"


def _cfg() -> dict:
    return {
        "repo": REPO,
        "project_owner": "bradley-mankoff",
        "project_number": 1,
        "state_file": "automation/state.json",
        "default_lane": "Backlog",
        "lanes": {
            "Backlog": "backlog",
            "Todo": "todo",
            "In Progress": "in_progress",
            "Blocked": "blocked",
            "Ready for Review": "ready",
            "In Review": "review",
            "Done": "done",
        },
        "dispatch": {
            "todo": {
                "default": "archon-fix-github-issue",
                "move_to": "In Progress",
                "complete_move_to": "Ready for Review",
                "merge_develop_base": "develop",
                "label_overrides": {"feature": "archon-idea-to-pr"},
            },
            "review": {
                "workflow": "archon-smart-pr-review",
                "ship_to": "main",
                "merge_ship_on_review_complete": True,
                "done_lane": "Done",
            },
        },
    }


ISOLATION_LIST = (
    "https://github.com/someone/other.git:\n"
    "  archon/task-issue-21\n"          # same-named worktree, wrong repo
    "    Path: /tmp/other/worktrees/archon/task-issue-21\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    f"https://github.com/{REPO}.git:\n"
    "  archon/task-issue-20\n"
    "    Path: /tmp/news/worktrees/archon/task-issue-20\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    "  archon/task-issue-21\n"          # the target worktree
    "    Path: /tmp/news/worktrees/archon/task-issue-21\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"
    "  archon/task-issue-210\n"         # must NOT match issue 21 (\b boundary)
    "    Path: /tmp/news/worktrees/archon/task-issue-210\n"
    "    Type: task | Platform: cli | Last activity: 0d ago\n"

)


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
        for st in ("running", "pending", "queued", "scheduled"):
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


class PR145SyncLocalDevelopTest(unittest.TestCase):
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

    def test_subprocess_exceptions_are_fail_closed(self):
        for error in (
            subprocess.TimeoutExpired(["git", "fetch"], 90),
            OSError("git is unavailable"),
        ):
            with self.subTest(error=error):
                def raise_error(*_args, **_kwargs):
                    raise error

                with patch("automation.board_poller.subprocess.run",
                           side_effect=raise_error):
                    msg = sync_local_develop()
                self.assertIn("LOCAL SYNC FAILED", msg)
                self.assertIn("fetch", msg)

    def test_git_probe_failures_are_fail_closed(self):
        cases = (
            {"fetch": lambda: _cp(), "rev-parse": lambda: _cp(stdout="develop\n"),
             "status": lambda: _cp(1, stderr="status failed")},
            {"fetch": lambda: _cp(), "rev-parse": lambda: _cp(stdout="develop\n"),
             "status": lambda: _cp(),
             "rev-list": lambda: _cp(1, stderr="ahead failed")},
            {"fetch": lambda: _cp(), "rev-parse": lambda: _cp(stdout="develop\n"),
             "status": lambda: _cp(),
             "rev-list": [lambda: _cp(stdout="0\n"),
                           lambda: _cp(1, stderr="behind failed")]},
        )
        for plan in cases:
            with self.subTest(plan=plan):
                msg = self._run_with(plan)
                self.assertIn("LOCAL SYNC FAILED", msg)

    def test_sync_uses_exact_git_command_contract(self):
        calls = []
        responses = {
            ("git", "fetch", "origin"): _cp(),
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): _cp(stdout="develop\n"),
            ("git", "status", "--porcelain"): _cp(),
            ("git", "rev-list", "--count", "origin/develop..HEAD"): _cp(stdout="0\n"),
            ("git", "rev-list", "--count", "HEAD..origin/develop"): _cp(stdout="3\n"),
            ("git", "merge", "--ff-only", "origin/develop"): _cp(stdout="Fast-forward\n"),
        }

        def fake_run(cmd, *args, **kwargs):
            calls.append((cmd, kwargs))
            return responses[tuple(cmd)]

        with patch("automation.board_poller.subprocess.run", side_effect=fake_run), \
                patch("automation.board_poller._ui_running", return_value=False):
            msg = sync_local_develop()

        self.assertIn("develop updated", msg)
        self.assertEqual(
            [cmd for cmd, _kwargs in calls],
            [list(key) for key in responses],
        )
        self.assertTrue(all(kwargs["cwd"] == str(bp.ROOT) for _cmd, kwargs in calls))

class IssueHasLabelTests(unittest.TestCase):
    """HIGH: a gh failure must return None (undetermined), never False —
    the caller defers instead of routing a blocked issue into the normal
    completion path (develop merge + ship PR)."""

    def test_true_when_label_present(self) -> None:
        fake = MagicMock(returncode=0, stdout="bug\nneeds-input\n")
        with patch.object(bp, "gh", return_value=fake):
            self.assertTrue(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))

    def test_false_when_label_absent(self) -> None:
        fake = MagicMock(returncode=0, stdout="bug\n")
        with patch.object(bp, "gh", return_value=fake):
            self.assertFalse(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))

    def test_none_and_logged_when_gh_fails(self) -> None:
        fake = MagicMock(returncode=1, stdout="", stderr="rate limit exceeded")
        with patch.object(bp, "gh", return_value=fake), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))
        log.assert_called_once()
        self.assertIn("LABEL CHECK FAILED", log.call_args[0][0])

    def test_none_when_gh_times_out(self) -> None:
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("gh", timeout=90)

        with patch.object(bp, "gh", side_effect=boom), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.issue_has_label(_cfg(), {}, 21, "needs-input"))
        log.assert_called_once()
        self.assertIn("LABEL CHECK TIMEOUT", log.call_args[0][0])


class ResolveWorktreeBranchTests(unittest.TestCase):
    """HIGH: repo-scoped parse; failures logged; no silent wrong match."""

    def _fake(self, stdout: str = ISOLATION_LIST, returncode: int = 0) -> MagicMock:
        return MagicMock(returncode=returncode, stdout=stdout)

    def test_parses_own_repo_section_only(self) -> None:
        with patch.object(bp.subprocess, "run", return_value=self._fake()):
            self.assertEqual(
                bp.resolve_worktree_branch({}, 21, REPO),
                "archon/task-issue-21")

    def test_ignores_same_named_worktree_in_other_repo(self) -> None:
        """The other repo's archon/task-issue-21 appears FIRST in the listing;
        the scoped parse must skip it and still find this repo's."""
        with patch.object(bp.subprocess, "run", return_value=self._fake()):
            result = bp.resolve_worktree_branch({}, 21, REPO)
        self.assertEqual(result, "archon/task-issue-21")
        self.assertNotEqual(result, "archon/task-issue-21\n    Path: /tmp/other/...")

    def test_skips_json_log_lines_inside_repo_section(self) -> None:
        """archon's JSON log lines land on stdout and must never be read as
        the worktree branch."""
        listing = (
            f"https://github.com/{REPO}.git:\n"
            "  archon/task-issue-20\n"
            '    {"ts": "2026-08-02T10:00:00Z", "msg": "resuming archon/task-issue-21"}\n'
            "  archon/task-issue-21\n"
            "    Path: /tmp/news/worktrees/archon/task-issue-21\n"
        )
        with patch.object(bp.subprocess, "run", return_value=self._fake(listing)):
            self.assertEqual(
                bp.resolve_worktree_branch({}, 21, REPO),
                "archon/task-issue-21")

    def test_returns_none_when_repo_section_missing(self) -> None:
        listing = "https://github.com/someone/other.git:\n  archon/task-issue-21\n"
        with patch.object(bp.subprocess, "run",
                          return_value=self._fake(stdout=listing)):
            self.assertIsNone(bp.resolve_worktree_branch({}, 21, REPO))

    def test_returns_none_and_logs_on_failure(self) -> None:
        with patch.object(bp.subprocess, "run",
                          return_value=self._fake(returncode=1)), \
             patch.object(bp, "log") as log:
            self.assertIsNone(bp.resolve_worktree_branch({}, 21, REPO))
        self.assertTrue(any("WORKTREE LIST FAILED" in c.args[0]
                            for c in log.call_args_list))


class ResumeIssueTests(unittest.TestCase):
    """HIGH: defer when the branch can't be resolved; verify the spawn;
    only remove the needs-input label after verified spawn + successful edit."""

    def test_deferred_when_worktree_branch_not_found(self) -> None:
        with patch.object(bp, "resolve_worktree_branch", return_value=None), \
             patch.object(bp, "gh") as gh, \
             patch.object(bp, "subprocess") as sub:
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertEqual(full, None)
        gh.assert_not_called()
        sub.Popen.assert_not_called()

    def test_happy_path_spawns_continue_and_removes_label(self) -> None:
        proc = MagicMock()
        proc.pid = 42
        proc.poll.return_value = None  # still running after grace period
        comment = MagicMock(returncode=0, stdout="The answer is: Apache-2.0")
        label = MagicMock(returncode=0, stdout="")
        gh = MagicMock(side_effect=[comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time") as time, \
             patch.object(bp.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(bp, "gh", gh), \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertTrue(ok)
        self.assertEqual(full, "archon/task-issue-21")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:4],
                         ["archon", "continue", "archon/task-issue-21",
                          "--workflow"])
        self.assertIn("The answer is: Apache-2.0", msg)
        time.sleep.assert_called_once_with(2)
        # label removal invoked with --remove-label needs-input
        remove = [c for c in gh.call_args_list
                  if "--remove-label" in c.args[0]][0].args[0]
        self.assertIn("needs-input", remove)

    def test_fails_when_continue_exits_immediately(self) -> None:
        """A run that dies instantly must not be reported as resumed. The
        label is already removed by then (it gates the spawn), so the caller's
        fresh dispatch replaces the dead run without a re-block cycle — but a
        second run is only ever started after the child is confirmed dead."""
        proc = MagicMock()
        proc.poll.return_value = 1
        comment = MagicMock(returncode=0, stdout="answer")
        label = MagicMock(returncode=0, stdout="")
        gh_calls = iter([comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp, "time"), \
             patch.object(bp.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(bp, "gh",
                          side_effect=lambda *a, **k: next(gh_calls)) as gh, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        popen.assert_called_once()
        # the label edit (the spawn gate) happened before the dead child
        self.assertTrue(any("--remove-label" in c.args[0]
                            for c in gh.call_args_list))

    def test_fails_when_label_removal_fails(self) -> None:
        """The label edit gates the spawn: a failed removal must leave NO
        child running, so the caller's fresh-dispatch fallback cannot start a
        second concurrent run for the same issue/worktree (the double-run
        hazard the reorder eliminates)."""
        comment = MagicMock(returncode=0, stdout="answer")
        label = MagicMock(returncode=1, stdout="", stderr="boom")
        gh_calls = iter([comment, label])
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp.subprocess, "Popen") as popen, \
             patch.object(bp, "gh", side_effect=lambda *a, **k: next(gh_calls)), \
             patch.object(bp, "log") as log, \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)
        popen.assert_not_called()  # no child -> caller fallback is safe
        self.assertTrue(any("LABEL REMOVE FAILED" in c.args[0]
                            for c in log.call_args_list))

    def test_fails_on_oserror_from_popen(self) -> None:
        with patch.object(bp, "resolve_worktree_branch",
                          return_value="archon/task-issue-21"), \
             patch.object(bp.subprocess, "Popen", side_effect=OSError("no archon")), \
             patch.object(bp, "gh",
                          return_value=MagicMock(returncode=0, stdout="")), \
             patch("builtins.open", mock_open()):
            ok, msg, full = bp.resume_issue(_cfg(), {}, "issue-21",
                                            "archon-fix-github-issue", 21)
        self.assertFalse(ok)
        self.assertIsNone(full)


class FindIssuePrTest(unittest.TestCase):
    """HIGH: the (pr, ok) tri-state contract that gates the develop merge.

    A gh failure must return (None, False) so callers defer instead of
    advancing past the integration gate; a genuinely absent PR is
    (None, True) — the two must stay distinguishable.
    """

    def _cfg(self):
        return {"repo": "o/r"}

    def test_gh_failure_returns_ok_false(self):
        with patch("automation.board_poller.gh",
                   return_value=_cp(returncode=1, stderr="rate limited")), \
             patch("automation.board_poller.log"):
            pr, ok = bp.find_issue_pr(self._cfg(), {}, 21)
        self.assertIsNone(pr)
        self.assertFalse(ok)          # caller must NOT advance

    def test_unparseable_output_returns_ok_false(self):
        with patch("automation.board_poller.gh",
                   return_value=_cp(stdout="not json")), \
             patch("automation.board_poller.log"):
            pr, ok = bp.find_issue_pr(self._cfg(), {}, 21)
        self.assertIsNone(pr)
        self.assertFalse(ok)

    def test_no_pr_returns_ok_true(self):
        with patch("automation.board_poller.gh", return_value=_cp(stdout="[]")):
            pr, ok = bp.find_issue_pr(self._cfg(), {}, 21)
        self.assertIsNone(pr)
        self.assertTrue(ok)           # genuinely absent is distinguishable

    def test_base_filter_propagates(self):
        prs = json.dumps([
            {"number": 47, "baseRefName": "develop", "body": "Issue: #21",
             "title": "", "headRefName": "h", "state": "OPEN"},
            {"number": 51, "baseRefName": "main", "body": "Issue #21",
             "title": "", "headRefName": "h", "state": "OPEN"},
        ])
        with patch("automation.board_poller.gh", return_value=_cp(stdout=prs)):
            pr, ok = bp.find_issue_pr(self._cfg(), {}, 21, base="develop")
        self.assertTrue(ok)
        self.assertEqual(pr["number"], 47)


class FindOrCreateShipPrTest(unittest.TestCase):
    """Ship-PR create failures must be logged, never silently ignored — the
    review lane was stranding items with no log entry when gh failed."""

    def _cfg(self):
        return {"repo": "o/r"}

    def test_reuses_existing_open_pr_for_head_and_base(self):
        prs = json.dumps([{"number": 88, "baseRefName": "main"}])
        with patch("automation.board_poller.gh", return_value=_cp(stdout=prs)):
            pr = bp.find_or_create_ship_pr(
                self._cfg(), {}, "archon/task-issue-5", "Ship: X (#5)", 5, "main")
        self.assertEqual(pr["number"], 88)

    def test_create_parses_pull_number_from_url(self):
        def fake_gh(args, env, timeout=90):
            if args[0:2] == ["pr", "list"]:
                return _cp(stdout="[]")
            if args[0:2] == ["pr", "create"]:
                return _cp(stdout="https://github.com/o/r/pull/77")
            return _cp(returncode=1)
        with patch("automation.board_poller.gh", side_effect=fake_gh):
            pr = bp.find_or_create_ship_pr(
                self._cfg(), {}, "head", "T", 5, "main")
        self.assertEqual(pr["number"], 77)

    def test_create_failure_logged_and_returns_none(self):
        def fake_gh(args, env, timeout=90):
            if args[0:2] == ["pr", "list"]:
                return _cp(stdout="[]")
            return _cp(returncode=1, stderr="boom")
        with patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.log") as log:
            pr = bp.find_or_create_ship_pr(
                self._cfg(), {}, "head", "T", 5, "main")
        self.assertIsNone(pr)
        self.assertTrue(any("pr create failed" in c[0][0]
                            for c in log.call_args_list))

    def test_unparseable_stdout_logged_and_returns_none(self):
        def fake_gh(args, env, timeout=90):
            if args[0:2] == ["pr", "list"]:
                return _cp(stdout="[]")
            if args[0:2] == ["pr", "create"]:
                return _cp(stdout="something unexpected")
            return _cp(returncode=1)
        with patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.log") as log:
            pr = bp.find_or_create_ship_pr(
                self._cfg(), {}, "head", "T", 5, "main")
        self.assertIsNone(pr)
        self.assertTrue(any("cannot parse PR number" in c[0][0]
                            for c in log.call_args_list))


class PollFlowTest(unittest.TestCase):
    """poll()-level flow tests: the state-machine glue between helpers.

    The pre-rewrite suite's PollFlowTest was deleted in this PR; these restore
    coverage of the highest-risk orchestration: the todo dep gate, the
    review-lane gh-failure deferral, the needs-input gate, and the
    verdict-gated ship merge. External CLIs are mocked; no network required.
    """

    def _cfg(self):
        cfg = _cfg()
        cfg["dispatch"]["review"]["merge_ship_on_approve"] = True
        return cfg

    def _item(self, number, lane, body="", labels=None):
        return {"id": f"item-{number}", "status": lane, "content": {
            "__typename": "Issue", "number": number,
            "title": f"Issue {number}",
            "url": f"https://github.com/{REPO}/issues/{number}",
            "repository": {"nameWithOwner": REPO},
            "labels": {"nodes": [{"name": l} for l in (labels or [])]},
            "state": "OPEN", "body": body}}

    def _project(self, items):
        return ("project-1", "field-1",
                {"Backlog": "o-backlog", "Todo": "o-todo",
                 "In Progress": "o-ip", "Blocked": "o-blocked",
                 "Ready for Review": "o-ready", "In Review": "o-review",
                 "Done": "o-done"}, items)

    def test_unsatisfied_dep_blocks_dispatch_and_moves_to_blocked(self):
        state = {"_meta": {"snapshot_done": True},
                 "item-31": {"status": "Backlog"},
                 "item-30": {"status": "Todo"}}  # dep item: no transition
        items = [self._item(31, "Todo", body="Depends on: #30"),
                 self._item(30, "Todo")]
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.move_to_lane",
                   return_value=True) as move, \
             patch("automation.board_poller.dispatch") as dispatch, \
             patch("automation.board_poller.comment_issue",
                   return_value=True) as comment, \
             patch("automation.board_poller.log"), \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        dispatch.assert_not_called()      # never dispatch before deps ship
        # routed to the Blocked lane with the dep marker recorded
        self.assertTrue(any(c.args[5] == "o-blocked"
                            for c in move.call_args_list))
        self.assertEqual(state["item-31"]["dep_blocked"], [30])
        self.assertTrue(any("depends on #30" in c.args[3]
                            for c in comment.call_args_list))
        self.assertEqual(state["item-31"]["status"], "Blocked")

    def test_review_lane_gh_lookup_failure_defers(self):
        """A gh failure on the develop-PR lookup must never build a ship PR
        from a guessed head branch or dispatch a review."""
        state = {"_meta": {"snapshot_done": True},
                 "item-5": {"status": "Ready for Review"}}
        items = [self._item(5, "In Review")]
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.find_issue_pr",
                   return_value=(None, False)) as find_pr, \
             patch("automation.board_poller.find_or_create_ship_pr") as ship, \
             patch("automation.board_poller.dispatch") as dispatch, \
             patch("automation.board_poller.merge_pr_to_base") as merge, \
             patch("automation.board_poller.log") as log, \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        find_pr.assert_called_once()
        ship.assert_not_called()
        dispatch.assert_not_called()
        merge.assert_not_called()
        # status NOT recorded -> lane re-entered next poll (retry)
        self.assertEqual(state["item-5"]["status"], "Ready for Review")
        self.assertTrue(any("REVIEW PREP DEFERRED" in c[0][0]
                            for c in log.call_args_list))

    def test_needs_input_label_moves_completed_run_to_blocked(self):
        state = {"_meta": {"snapshot_done": True},
                 "item-5": {"status": "In Progress", "dispatch_msg": "run msg",
                            "issue_number": 5}}
        items = [self._item(5, "In Progress")]
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.fetch_runs_by_message",
                   return_value={"run msg": "completed"}), \
             patch("automation.board_poller.issue_has_label",
                   return_value=True), \
             patch("automation.board_poller.find_issue_pr") as find_pr, \
             patch("automation.board_poller.move_to_lane",
                   return_value=True) as move, \
             patch("automation.board_poller.log"), \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        self.assertTrue(any(c.args[5] == "o-blocked"
                            for c in move.call_args_list))
        find_pr.assert_not_called()      # no develop merge while blocked
        self.assertNotIn("dispatch_msg", state["item-5"])

    def test_needs_input_label_lookup_failure_holds_completion(self):
        """A gh failure on the label check must NOT be misread as 'label
        absent': the run may be awaiting human input — keep the marker."""
        state = {"_meta": {"snapshot_done": True},
                 "item-5": {"status": "In Progress", "dispatch_msg": "run msg",
                            "issue_number": 5}}
        items = [self._item(5, "In Progress")]
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.fetch_runs_by_message",
                   return_value={"run msg": "completed"}), \
             patch("automation.board_poller.issue_has_label",
                   return_value=None), \
             patch("automation.board_poller.find_issue_pr") as find_pr, \
             patch("automation.board_poller.move_to_lane") as move, \
             patch("automation.board_poller.log") as log, \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        move.assert_not_called()
        find_pr.assert_not_called()      # no develop merge on uncertainty
        self.assertEqual(state["item-5"]["dispatch_msg"], "run msg")
        self.assertEqual(state["item-5"]["status"], "In Progress")
        self.assertTrue(any("LABEL CHECK UNREADABLE" in c[0][0]
                            for c in log.call_args_list))

    def _review_state(self):
        return {"_meta": {"snapshot_done": True},
                "item-5": {"status": "In Review", "review_msg": "rv msg",
                           "ship_pr": 51, "issue_number": 5}}

    def _ship_pr_gh(self):
        calls = []

        def fake_gh(args, env, timeout=90):
            calls.append(args)
            if args[0:2] == ["pr", "view"]:
                return _cp(stdout=json.dumps(
                    {"number": 51, "state": "OPEN", "baseRefName": "main"}))
            if args[0:2] == ["pr", "merge"]:
                return _cp()
            if args[0:2] == ["issue", "close"]:
                return _cp()
            return _cp(returncode=1)
        return calls, fake_gh

    def test_approving_verdict_merges_ship_pr_and_closes_issue(self):
        state = self._review_state()
        items = [self._item(5, "In Review")]
        calls, fake_gh = self._ship_pr_gh()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.fetch_runs_by_message",
                   return_value={"rv msg": "completed"}), \
             patch("automation.board_poller.fetch_verdict",
                   return_value=("approve", True)), \
             patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.move_to_lane",
                   return_value=True) as move, \
             patch("automation.board_poller.log"), \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        self.assertTrue(any(a[0:2] == ["pr", "merge"] for a in calls))
        self.assertTrue(any(a[0:2] == ["issue", "close"] for a in calls))
        self.assertTrue(any(c.args[5] == "o-done"
                            for c in move.call_args_list))
        self.assertNotIn("review_msg", state["item-5"])
        self.assertNotIn("ship_pr", state["item-5"])

    def test_non_approve_verdict_holds_ship_and_pops_markers(self):
        state = self._review_state()
        items = [self._item(5, "In Review")]
        calls, fake_gh = self._ship_pr_gh()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.fetch_runs_by_message",
                   return_value={"rv msg": "completed"}), \
             patch("automation.board_poller.fetch_verdict",
                   return_value=("none", True)), \
             patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.comment_issue",
                   return_value=True) as comment, \
             patch("automation.board_poller.move_to_lane") as move, \
             patch("automation.board_poller.log") as log, \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        comment.assert_called_once()
        self.assertIn("did not approve", comment.call_args[0][3])
        move.assert_not_called()
        self.assertFalse(any(a[0:2] == ["pr", "merge"] for a in calls))
        self.assertNotIn("review_msg", state["item-5"])
        self.assertTrue(any("SHIP HELD" in c[0][0]
                            for c in log.call_args_list))

    def test_hold_notice_failure_keeps_markers_for_retry(self):
        state = self._review_state()
        items = [self._item(5, "In Review")]
        calls, fake_gh = self._ship_pr_gh()
        with patch("automation.board_poller.fetch_project",
                   return_value=self._project(items)), \
             patch("automation.board_poller.fetch_runs_by_message",
                   return_value={"rv msg": "completed"}), \
             patch("automation.board_poller.fetch_verdict",
                   return_value=("none", True)), \
             patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.comment_issue",
                   return_value=False), \
             patch("automation.board_poller.move_to_lane"), \
             patch("automation.board_poller.log"), \
             patch("automation.board_poller.save_state"):
            bp.poll(self._cfg(), {}, state)
        self.assertEqual(state["item-5"]["review_msg"], "rv msg")
        self.assertEqual(state["item-5"]["ship_pr"], 51)


class ReconcileDeferredWorkCreateTest(unittest.TestCase):
    """The auto-create path: dedupe (link/create-ref) + create + board + the
    linkage comment. A parse or wiring bug here silently creates duplicate or
    unboarded tracking issues."""

    def _cfg(self):
        return {"deferred_work": {}, "default_lane": "Backlog",
                "repo": "o/r", "project_number": 1,
                "project_owner": "o"}

    def _deferred_comment(self):
        return {"body": ("## Deferred work\n"
                          "- **Title:** Add llama.cpp backend\n"
                          "  **Description:** Port the model layer.\n"
                          "  **Reason:** Packaging.\n")}

    def _run(self, open_stdout="[]", closed_stdout="[]", rec=None,
             comments=None):
        rec = {} if rec is None else rec
        calls = []
        comments = [self._deferred_comment()] if comments is None else comments

        def fake_gh(args, env, timeout=90):
            calls.append(args)
            if args[0:2] == ["issue", "view"]:
                return _cp(stdout=json.dumps(
                    {"title": "T", "comments": comments}))
            if args[0:2] == ["issue", "list"]:
                state = args[args.index("--state") + 1]
                return _cp(stdout=open_stdout if state == "open" else closed_stdout)
            if args[0:2] == ["issue", "create"]:
                return _cp(stdout="https://github.com/o/r/issues/99")
            if args[0:2] == ["project", "item-add"]:
                return _cp(stdout='{"itemId": "PVTI_1"}')
            if args[0:2] == ["issue", "comment"]:
                return _cp()
            return _cp(returncode=1)
        with patch("automation.board_poller.gh", side_effect=fake_gh), \
             patch("automation.board_poller.move_to_lane", return_value=True):
            ok = reconcile_deferred_work(
                self._cfg(), {}, 5, None, rec, "run-1",
                "p", "f", {"Backlog": "o1"})
        return ok, rec, calls

    def test_creates_boards_and_links_new_items(self):
        ok, rec, calls = self._run()
        self.assertTrue(ok)
        self.assertEqual(rec["deferred_handled"], "run-1")
        creates = [a for a in calls if a[0:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("Add llama.cpp backend", creates[0])
        adds = [a for a in calls if a[0:2] == ["project", "item-add"]]
        self.assertEqual(len(adds), 1)
        self.assertTrue(any("/issues/99" in a for a in adds[0]))
        comments = [a for a in calls if a[0:2] == ["issue", "comment"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("#99", comments[0][-1])

    def test_open_match_links_without_creating(self):
        existing = json.dumps([{"number": 99,
                               "title": "Add llama.cpp backend"}])
        ok, rec, calls = self._run(open_stdout=existing)
        self.assertTrue(ok)
        self.assertEqual(rec["deferred_handled"], "run-1")
        self.assertFalse(any(a[0:2] == ["issue", "create"] for a in calls))
        self.assertFalse(any(a[0:2] == ["project", "item-add"] for a in calls))
        comments = [a for a in calls if a[0:2] == ["issue", "comment"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("already tracked in #99", comments[0][-1])

    def test_closed_match_creates_ref_superseding_closed(self):
        existing = json.dumps([{"number": 98,
                               "title": "Add llama.cpp backend"}])
        ok, rec, calls = self._run(closed_stdout=existing)
        self.assertTrue(ok)
        creates = [a for a in calls if a[0:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        body = creates[0][creates[0].index("--body") + 1]
        self.assertIn("Supersedes: #98", body)
        comments = [a for a in calls if a[0:2] == ["issue", "comment"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("supersedes closed #98", comments[0][-1])

    def test_explicit_supersedes_is_preserved_on_created_issue(self):
        comments = [{"body": ("## Deferred work\n"
                               "- **Title:** Rebuild deferred adapter\n"
                               "  **Description:** Replace the old adapter.\n"
                               "  **Supersedes:** #42\n")}]
        ok, _rec, calls = self._run(
            comments=comments,
            closed_stdout=json.dumps([{"number": 42, "title": "Old adapter"}]),
        )
        self.assertTrue(ok)
        creates = [a for a in calls if a[0:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 1)
        body = creates[0][creates[0].index("--body") + 1]
        self.assertIn("Supersedes: #42", body)

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
