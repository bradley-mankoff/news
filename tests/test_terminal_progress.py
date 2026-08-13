from __future__ import annotations

from io import StringIO
from unittest.mock import patch
import unittest

from news_pipeline.pipeline import ProgressTracker
from news_pipeline.story_clustering import organize_article_targets_into_global_stories
from news_pipeline.story_drafting import StoryDraftingRuntime, run_story_synthesis_blocks


class FakeTTY(StringIO):
    def isatty(self) -> bool:
        return True


class FakePipe(StringIO):
    def isatty(self) -> bool:
        return False


def _story_runtime(progress_events: list[tuple[str, dict]]) -> StoryDraftingRuntime:
    return StoryDraftingRuntime(
        story_synthesis_concurrency=3,
        story_drafting_max_tokens=1000,
        model_reference="test",
        model_name="test",
        model_backend="test",
        min_articles_per_story=2,
        build_chat_model=lambda **_kwargs: object(),
        invoke_with_retries=lambda *_args, **_kwargs: object(),
        estimate_message_token_count=lambda _message: 1,
        extract_prompt_tokens_from_response=lambda _response: None,
        strip_prompt_echo_lines=lambda text: text,
        strip_model_artifacts=lambda text: text,
        is_low_coverage_synthesis_section=lambda text: not str(text or "").strip(),
        fallback_synthesis_paragraph_from_summaries=lambda summaries: " ".join(summaries),
        story_drafting_word_count=lambda text: len(str(text or "").split()),
        progress_callback=lambda event, payload: progress_events.append((event, payload)),
    )


class TerminalProgressTests(unittest.TestCase):
    def test_compact_tty_source_progress_suppresses_intermediate_items(self) -> None:
        stream = FakeTTY()
        tracker = ProgressTracker(stream=stream)

        tracker.reset(total_sources=3)
        tracker.source_completed("Reuters", candidate_articles=2, worker_count=2)
        tracker.source_completed("AP", candidate_articles=1, worker_count=2)
        tracker.source_completed("BBC", candidate_articles=3, worker_count=2)
        tracker.finish_meter(detail="4 fresh articles")

        output = stream.getvalue()
        self.assertIn("\r", output)
        self.assertIn("0/3 sources", output)
        self.assertIn("3/3 sources", output)
        self.assertNotIn("fresh articles", output)
        self.assertNotIn("Reuters", output)

    def test_live_tty_source_progress_uses_one_mutable_line(self) -> None:
        stream = FakeTTY()
        tracker = ProgressTracker(stream=stream)

        tracker.reset(total_sources=3)
        tracker.source_completed("Reuters", candidate_articles=2, worker_count=2)
        tracker.source_completed("AP", candidate_articles=1, worker_count=2)
        tracker.source_completed("BBC", candidate_articles=3, worker_count=2)
        tracker.finish_meter(detail="4 fresh articles")

        output = stream.getvalue()
        self.assertIn("\r", output)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("3/3 sources", output)
        self.assertNotIn("fresh articles", output)
        self.assertNotIn("Reuters", output)

    def test_non_tty_source_progress_suppresses_intermediate_items(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream)

        tracker.reset(total_sources=3)
        tracker.source_completed("Reuters", candidate_articles=2, worker_count=2)
        tracker.source_completed("AP", candidate_articles=1, worker_count=2)
        tracker.source_completed("BBC", candidate_articles=3, worker_count=2)
        tracker.finish_meter(detail="4 fresh articles")

        output = stream.getvalue()
        self.assertIn("0/3 sources", output)
        self.assertIn("3/3 sources", output)
        self.assertNotIn("Reuters", output)
        self.assertNotIn("AP", output)

    def test_compact_full_run_sized_updates_stay_under_line_budget(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream)

        tracker.step("setup", "preset custom | model large | image on | 57 sources | send Primary only")

        tracker.reset(total_sources=57)
        for index in range(1, 58):
            tracker.source_completed(f"Source {index}", candidate_articles=index % 5, worker_count=8)
        tracker.finish_meter(detail="139 fresh articles")

        tracker.start_story_clustering(200_000, detail="Clustering 139 candidate articles.")
        for done in range(1_000, 200_001, 1_000):
            tracker.story_clustering_progress(
                "similarity_pair",
                {
                    "phase": "pairwise similarity",
                    "done": done,
                    "total": 200_000,
                    "linked_pairs": done // 10_000,
                },
            )
        tracker.finish_meter(detail="47 story groups")

        tracker.start_meter("model", total=3, unit="steps", detail="Checking model server.")
        tracker.update_meter(done=1, detail="Starting managed model server.")
        tracker.update_meter(done=2, detail="Checking model generation.")
        tracker.finish_meter(detail="Model server ready.")

        tracker.start_article_summary(139)
        for index in range(1, 140):
            tracker.article_completed(
                {
                    "source": f"Source {index}",
                    "title": f"Article headline {index}",
                }
            )
        tracker.finish_meter(detail="139 article summaries")

        tracker.start_story_drafting(47)
        for index in range(1, 48):
            tracker.story_draft_completed(
                {
                    "story_title": f"Story {index}",
                    "valid": index % 6 != 0,
                }
            )
        tracker.finish_meter(detail="39 valid | 8 rejected")

        tracker.start_meter("story_selection", total=46, unit="stories", detail="Evaluating story quality.")
        tracker.story_selection_progress(
            "scale_screening_started",
            {
                "total": 46,
                "candidate_count": 46,
            },
        )
        for done in range(1, 47):
            tracker.story_selection_progress(
                "scale_screening_batch_completed",
                {
                    "done": done,
                    "total": 46,
                    "kept_count": min(done, 8),
                    "fallback_count": done // 4,
                },
            )
        tracker.finish_meter(detail="8 eligible | 38 ineligible")

        tracker.start_meter("report", total=5, unit="steps", detail="Building report.")
        tracker.set_final_step("synthesis", 2)
        tracker.set_final_step("art", 3)
        tracker.set_final_step("render", 4)
        tracker.set_final_step("email", 5)

        tracker.finish("done")

        output = stream.getvalue()
        lines = output.split("\n")
        self.assertLessEqual(len(lines), 20, output)
        self.assertIn("\r", output)
        self.assertNotIn("Source 57", output)
        self.assertNotIn("Article headline 139", output)

    def test_pipe_output_contains_no_ansi_clear_line_sequence(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream)

        tracker.start_story_clustering(200_000, detail="Clustering 139 candidate articles.")
        for done in range(1_000, 200_001, 1_000):
            tracker.story_clustering_progress(
                "similarity_pair",
                {
                    "phase": "pairwise similarity",
                    "done": done,
                    "total": 200_000,
                    "linked_pairs": done // 10_000,
                },
            )
        tracker.finish_meter(detail="47 story groups")

        output = stream.getvalue()
        self.assertNotIn("\033[K", output)
        self.assertNotIn("\x1b", output)
        self.assertIn("\r", output)
        self.assertIn("200000/200000 steps", output)

    def test_tty_output_keeps_clear_line_for_in_place_updates(self) -> None:
        stream = FakeTTY()
        tracker = ProgressTracker(stream=stream)

        tracker.start_story_clustering(200_000, detail="Clustering.")
        tracker.story_clustering_progress(
            "similarity_pair",
            {
                "phase": "pairwise similarity",
                "done": 1_000,
                "total": 200_000,
                "linked_pairs": 1,
            },
        )
        tracker.finish_meter(detail="47 story groups")

        output = stream.getvalue()
        self.assertIn("\033[K", output)
        self.assertIn("200000/200000 steps", output)
        self.assertEqual(output.count("\n"), 1)

    def test_high_frequency_clustering_renders_initial_and_final_only(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream)

        tracker.start_story_clustering(200_000, detail="Clustering 139 candidate articles.")
        for done in range(1, 200_001, 500):
            tracker.story_clustering_progress(
                "similarity_pair",
                {
                    "phase": "pairwise similarity",
                    "done": done,
                    "total": 200_000,
                    "linked_pairs": done // 10_000,
                },
            )
        tracker.finish_meter(detail="47 story groups")

        output = stream.getvalue()
        self.assertIn("0/200000 steps", output)
        self.assertIn("200000/200000 steps", output)
        self.assertLessEqual(len(output.split("\n")), 3, output)

    def test_story_drafting_progress_reports_concurrent_completions(self) -> None:
        events: list[tuple[str, dict]] = []
        blocks = [
            {"story_title": "Story A"},
            {"story_title": "Story B"},
            {"story_title": "Story C"},
        ]

        def fake_story_synthesis_block(story_block, _now_label, _runtime):
            return {
                **story_block,
                "valid": story_block["story_title"] != "Story B",
                "paragraph": "Accepted story paragraph.",
            }

        with patch(
            "news_pipeline.story_drafting.run_story_synthesis_block",
            side_effect=fake_story_synthesis_block,
        ):
            results = run_story_synthesis_blocks(
                blocks,
                "June 6, 2026",
                _story_runtime(events),
            )

        completed = [event for event in events if event[0] == "story_draft_completed"]
        self.assertEqual(len(results), 3)
        self.assertEqual(len(completed), 3)
        self.assertEqual(events[0][0], "story_drafting_started")
        self.assertEqual(events[0][1]["total"], 3)
        self.assertEqual(
            sum(1 for _event, payload in completed if payload["story"].get("valid")),
            2,
        )

    def test_story_clustering_progress_reaches_final_total(self) -> None:
        events: list[tuple[str, dict]] = []
        articles = [
            {
                "article_id": f"a{index}",
                "source": f"Source {index}",
                "title": "Major port strike disrupts regional cargo routes",
                "description": "Dockworkers halted cargo handling at the port.",
                "text": (
                    "Dockworkers halted cargo handling at the central port after contract "
                    "talks failed, delaying container ships and trucking routes."
                ),
            }
            for index in range(1, 5)
        ]

        organize_article_targets_into_global_stories(
            articles,
            min_articles_per_story=2,
            similarity_threshold=0.05,
            progress_callback=lambda event, payload: events.append((event, payload)),
        )

        self.assertTrue(any(event == "vectorized_article" for event, _payload in events))
        self.assertTrue(any(event == "similarity_pair" for event, _payload in events))
        self.assertEqual(events[-1][0], "components_ranked")
        self.assertEqual(events[-1][1]["done"], events[-1][1]["total"])

    def test_progress_tracker_helper_branches(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream, show_meter_detail=True)

        tracker.step("custom", "message", log_detail="detail message")
        tracker.log("--- [EMAIL]: sent ---", terminal=True)
        tracker.log("--- [UNSUBSCRIBE]: done ---", terminal=False)
        tracker.start_meter("zero", total=0, unit="steps", detail="ignored")
        tracker.advance_meter()
        tracker.finish_meter()
        tracker.start_meter("report", total=3, unit="steps", detail="long detail that should show", done=1)
        tracker._render_meter(force=True)
        tracker.update_meter(done=2, detail="updated detail", force=True)
        tracker.advance_meter(detail="advanced detail")
        tracker._finish_active_line()
        tracker.finish_meter(detail="complete detail")
        tracker.reset(total_sources=2)
        tracker.start_source(1, "Reuters")
        tracker.set_source_article_total(5)
        tracker.source_completed("Reuters", candidate_articles=2, worker_count=4)
        tracker.update_source_fresh_articles(7, latest_source="Reuters")
        tracker.start_article_summary(1)
        tracker.article_completed({"source_display_name": "Reuters", "title": "Headline"})
        tracker.start_story_clustering(4, detail="cluster")
        tracker.story_clustering_progress(
            "similarity_pair",
            {
                "phase": "phase",
                "done": 1,
                "total": 4,
                "linked_pairs": 2,
                "candidate_components": 3,
            },
        )
        tracker.start_story_drafting(2)
        tracker.story_draft_completed({"story_title": "Story A", "valid": True})
        tracker.story_selection_progress("scale_screening_started", {"total": 2, "candidate_count": 2})
        tracker.story_selection_progress(
            "scale_screening_batch_completed",
            {"done": 1, "total": 2, "kept_count": 1, "fallback_count": 0},
        )
        tracker.retrying("task", 1, 3, 2, ValueError("bad"))
        tracker.retry("task", 2, 3, 4)
        tracker.warning("label")
        tracker.set_final_step("unknown", 9)
        self.assertEqual(tracker._step_prefix("custom"), "[custom]")
        self.assertEqual(tracker._compact_detail("x" * 200), ("x" * 120).rsplit(" ", 1)[0].rstrip(" |") + "...")
        tracker.finish("done")

        output = stream.getvalue()
        self.assertIn("[email] sent", output)
        self.assertIn("No steps selected.", output)
        self.assertIn("long detail that should show", output)
        self.assertIn("updated detail", output)
        self.assertIn("advanced detail", output)
        self.assertIn("complete detail", output)
        self.assertIn("5 fresh articles", output)
        self.assertIn("phase | 2 linked pairs | 3 candidate components", output)
        self.assertIn("latest: Story A | valid 1 | rejected 0", output)
        self.assertIn("scale screening", output)
        self.assertIn("Running unknown.", output)

    def test_retries_and_warnings_reach_pipe_stream_but_details_stay_hidden(self) -> None:
        stream = FakePipe()
        tracker = ProgressTracker(stream=stream)

        tracker.start_story_clustering(200_000, detail="Clustering 139 candidate articles.")
        tracker.story_clustering_progress(
            "similarity_pair",
            {
                "phase": "pairwise similarity",
                "done": 1_000,
                "total": 200_000,
                "linked_pairs": 1,
            },
        )
        tracker.detail("detail marker: /private/tmp/model-server.log")
        tracker.retrying("story drafting", 1, 3, 5, TimeoutError("model timed out"))
        tracker.warning("low coverage for topic")
        tracker.retry("title generation", 2, 3, 8)
        tracker.finish_meter(detail="47 story groups")

        output = stream.getvalue()
        lines = output.split("\n")
        # The active CR meter is closed by the first notice, so every notice
        # lands on its own readable line while the detail line stays hidden.
        self.assertIn(
            "Retrying story drafting: attempt 1/3 failed (TimeoutError: model timed out); "
            "sleeping 5s before the next attempt.",
            lines,
        )
        self.assertIn("WARNING: low coverage for topic", lines)
        # The public retry() alias emits exactly one message through retrying().
        self.assertIn(
            "Retrying title generation: attempt 2/3 failed; sleeping 8s before the next attempt.",
            lines,
        )
        self.assertEqual(output.count("Retrying "), 2)
        # An ordinary detail() call stays file-only and never reaches the stream.
        self.assertNotIn("detail marker", output)
        # The meter line that was active when the first notice arrived is closed
        # exactly once, so the final snapshot begins on a fresh line.
        self.assertTrue(lines[0].startswith("\r["))
        self.assertIn("200000/200000 steps", lines[-2])


if __name__ == "__main__":
    unittest.main()
