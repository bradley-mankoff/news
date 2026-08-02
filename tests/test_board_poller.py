from __future__ import annotations

import unittest

from automation.board_poller import (
    conflict_episode_action,
    dep_gate,
    match_issue_pr,
    parse_dep_refs,
    parse_verdict,
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

    def test_multiline_body(self):
        self.assertEqual(
            parse_verdict(["line one\nVERDICT: approve\nline three"]), "approve")


if __name__ == "__main__":
    unittest.main()
