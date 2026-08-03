from __future__ import annotations

import unittest

from automation.board_poller import (
    conflict_episode_action,
    dedupe_deferred,
    dep_gate,
    duplicate_candidates,
    find_unchecked_criteria,
    has_deferral_language,
    match_issue_pr,
    normalize_title,
    parse_dep_refs,
    parse_deferred_work,
    parse_verdict,
    shared_keywords,
    title_keywords,
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
        self.assertEqual(parse_deferred_work("## What shipped\nNo deferrals."), [])
        self.assertEqual(parse_deferred_work(""), [])

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


class TitleKeywordsTest(unittest.TestCase):
    def test_significant_tokens_kept(self):
        self.assertEqual(
            title_keywords("Add llama.cpp/GGUF backend support"),
            {"llama.cpp", "gguf", "backend"})

    def test_stopwords_dropped(self):
        self.assertEqual(title_keywords("Add support for the new via"), set())

    def test_short_tokens_dropped(self):
        self.assertEqual(title_keywords("Fix UI go bug"), set())
        self.assertEqual(title_keywords("Parse CSV output"), {"parse", "output"})

    def test_embedded_punctuation_kept(self):
        self.assertEqual(
            title_keywords("Execute gated history scrub and force-push develop/main"),
            {"execute", "gated", "history", "scrub", "force-push", "develop", "main"})


class DuplicateCandidatesTest(unittest.TestCase):
    def test_gguf_phrasings_match(self):
        # The three real duplicates on the board (one manual, two guard-created).
        issues = [
            {"number": 75, "title": "Add managed cross-platform GGUF via a llama.cpp adapter"},
            {"number": 97, "title": "Add managed GGUF/llama.cpp backend support for external_only models"},
        ]
        self.assertEqual(
            duplicate_candidates("Add llama.cpp/GGUF backend support", issues),
            [97, 75])

    def test_scrub_phrasings_match(self):
        self.assertEqual(
            duplicate_candidates(
                "Execute gated history scrub and force-push develop/main",
                [{"number": 74, "title": "Scrub personal data from git history before open-sourcing"}]),
            [74])

    def test_catalog_phrasings_match(self):
        self.assertEqual(
            duplicate_candidates(
                "Add curated Model Catalog plus Hugging Face metadata integration",
                [{"number": 30, "title": "Add curated Model Catalog plus Hugging Face search/metadata integration"}]),
            [30])

    def test_translation_phrasings_match(self):
        self.assertEqual(
            duplicate_candidates(
                "Reintroduce the translation pipeline stage",
                [{"number": 33, "title": "Reintroduce explicit translation without content-based language detection"}]),
            [33])

    def test_single_shared_keyword_does_not_match(self):
        # Both mention "pipeline" but are different work.
        self.assertEqual(
            duplicate_candidates(
                "Add CI pipeline running pytest",
                [{"number": 60, "title": "Add README installation instructions for news-pipeline at publish time"}]),
            [])

    def test_unrelated_does_not_match(self):
        self.assertEqual(
            duplicate_candidates(
                "Add copyright line to LICENSE",
                [{"number": 58, "title": "Add llama.cpp/GGUF backend support"}]),
            [])

    def test_shared_keywords_reported(self):
        self.assertEqual(
            shared_keywords("Add llama.cpp/GGUF backend support",
                            "Add managed cross-platform GGUF via a llama.cpp adapter"),
            ["gguf", "llama.cpp"])

    def test_source_issue_shared_vocabulary_found(self):
        # duplicate_candidates DOES flag the source issue ("Replace personal
        # data..." vs its own deferral "Scrub personal data...") — the
        # reconcile loop excludes the source issue before dedupe, which is why
        # this behavior is acceptable.
        self.assertEqual(
            duplicate_candidates(
                "Scrub personal data from git history",
                [{"number": 23, "title": "Replace personal data with safe examples"}]),
            [23])
        # ...but an unrelated issue sharing "data" alone is also not a match.
        self.assertEqual(
            duplicate_candidates(
                "Scrub personal data from git history",
                [{"number": 5, "title": "Add safe data examples"}]),
            [])


class DeferredDedupeWarnTest(unittest.TestCase):
    def test_near_match_warns_instead_of_create(self):
        item = {"title": "Add llama.cpp/GGUF backend support"}
        open_issues = [{"number": 75, "title": "Add managed cross-platform GGUF via a llama.cpp adapter"}]
        self.assertEqual(dedupe_deferred(item, open_issues, []), ("warn", 75))

    def test_exact_open_wins_over_near(self):
        item = {"title": "Same title"}
        open_issues = [{"number": 1, "title": "Same title"},
                       {"number": 2, "title": "Same title phrased differently"}]
        self.assertEqual(dedupe_deferred(item, open_issues, []), ("link", 1))

    def test_near_open_wins_over_exact_closed(self):
        item = {"title": "Add llama.cpp/GGUF backend support"}
        open_issues = [{"number": 75, "title": "Add managed cross-platform GGUF via a llama.cpp adapter"}]
        closed = [{"number": 9, "title": "Add llama.cpp/GGUF backend support"}]
        self.assertEqual(dedupe_deferred(item, open_issues, closed), ("warn", 75))


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


if __name__ == "__main__":
    unittest.main()
