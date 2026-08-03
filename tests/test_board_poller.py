from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, mock_open, patch

from automation import board_poller as bp
from automation.board_poller import (
    conflict_episode_action,
    dedupe_deferred,
    dep_gate,
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
    try_merge_base_into_head,
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
             "label": "feature"},
            {"title": "Extract shared readiness helper",
             "description": "Merge the two readiness loops.",
             "reason": "", "label": ""},
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
             "reason": "", "label": ""}])

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


if __name__ == "__main__":
    unittest.main()
