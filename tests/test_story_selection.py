from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from news_pipeline.article_summary_records import render_markdown_entry
from news_pipeline.story_records import StoryRecord
import news_pipeline.story_selection as ss


def _citation_source(number: int, title: str, local_id: str) -> dict[str, str]:
    slug = title.lower().replace(" ", "-")
    return {
        "number": number,
        "title": title,
        "source": "Fixture Wire",
        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
        "url": f"https://example.com/{slug}",
        "article_id": local_id,
    }


def _summary(article_id: str) -> str:
    return (
        f"### Article {article_id}\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Mon, 01 Jun 2026 12:00:00 GMT\n"
        f"- URL: https://example.com/{article_id}\n"
        f"- Article ID: {article_id}\n\n"
        "Summary:\n"
        f"Reported facts for article {article_id}."
    )


def _runtime(
    *,
    enabled: bool = True,
    progress=None,
    invoke_with_retries=None,
    low_confidence=None,
) -> ss.StorySelectionRuntime:
    return ss.StorySelectionRuntime(
        story_scale_screening_enabled=enabled,
        model_max_input_tokens=1000,
        model_label="test-model",
        model_reference="test/reference",
        model_name="Test Model",
        model_backend="mlx-lm",
        relaxed_story_drafting_guards=True,
        build_chat_model=lambda **kwargs: {"kwargs": kwargs},
        invoke_with_retries=invoke_with_retries
        or (lambda _model, _messages, **_kwargs: SimpleNamespace(content="[]")),
        build_article_heading=lambda article: str(article.get("title") or ""),
        format_article_metadata=lambda _article: "",
        story_drafting_word_count=lambda text: len(str(text or "").split()),
        is_low_confidence_report_entry=low_confidence or (lambda _entry: False),
        report_reference_key=lambda entry: entry,
        progress_callback=progress,
    )


class StorySelectionTests(unittest.TestCase):
    def test_parsing_and_prompt_helpers(self) -> None:
        self.assertEqual(ss._compact_gate_text("  a   b c  ", 20), "a b c")
        self.assertEqual(ss._compact_gate_text("one two three four", 9), "one two")
        self.assertEqual(ss._validation_story_key({"story_key": "  alpha  "}, 0), "alpha")
        self.assertEqual(ss._validation_story_key({}, 2), "story-3")
        self.assertEqual(ss._json_block_from_text(""), "")
        self.assertEqual(ss._json_block_from_text("prefix ```json [1, 2] ``` suffix"), "[1, 2]")
        self.assertEqual(ss._json_block_from_text("noise {\"a\": {\"b\": 1}} tail"), "{\"a\": {\"b\": 1}}")
        self.assertEqual(ss._normalize_story_scale_verdict("major"), ss.STORY_SCALE_OBVIOUSLY_LARGE)
        self.assertEqual(ss._normalize_story_scale_verdict("small-scale"), ss.STORY_SCALE_OBVIOUSLY_SMALL)
        self.assertIsNone(ss._normalize_story_scale_verdict("mystery"))

        parsed, stats = ss.parse_story_scale_screening_response(
            json.dumps(
                {
                    "stories": {
                        "alpha": {"scale": "major", "scale_reason": "  broad scope  "},
                        "beta": {"scale": "mystery", "reason": "  unclear  "},
                    }
                }
            )
        )
        self.assertEqual(parsed["alpha"]["scale"], ss.STORY_SCALE_OBVIOUSLY_LARGE)
        self.assertEqual(parsed["beta"]["scale"], ss.STORY_SCALE_DEFAULT_VERDICT)
        self.assertEqual(stats["entry_count"], 2)
        self.assertEqual(stats["unknown_scale_count"], 1)

        parsed, stats = ss.parse_story_scale_screening_response(
            json.dumps({"story_key": "solo", "scale": "small", "scale_reason": " local "})
        )
        self.assertEqual(parsed["solo"]["scale"], ss.STORY_SCALE_OBVIOUSLY_SMALL)
        self.assertEqual(stats["entry_count"], 1)

        parsed, stats = ss.parse_story_scale_screening_response(
            json.dumps(
                {
                    "alpha": {"scale": "big", "reason": "  huge  "},
                    "beta": {"scale": "obviously_large_scale", "scale_reason": " broad "},
                }
            )
        )
        self.assertEqual(parsed["alpha"]["scale"], ss.STORY_SCALE_OBVIOUSLY_LARGE)
        self.assertEqual(parsed["beta"]["scale"], ss.STORY_SCALE_OBVIOUSLY_LARGE)
        self.assertEqual(stats["entry_count"], 2)

        parsed, stats = ss.parse_story_scale_screening_response("not valid json")
        self.assertEqual(parsed, {})
        self.assertTrue(stats["parse_failed"])

        parsed, stats = ss.parse_story_scale_screening_response(json.dumps("not a list"))
        self.assertEqual(parsed, {})
        self.assertTrue(stats["parse_failed"])
        self.assertEqual(stats["parse_error"], "story-scale screening response was not a list")

        parsed, stats = ss.parse_story_scale_screening_response(json.dumps([1, {"id": " ", "scale": "big"}]))
        self.assertEqual(parsed, {})
        self.assertEqual(stats["invalid_entry_count"], 2)
        self.assertEqual(stats["entry_count"], 0)

        candidate = {
            "story_title": "Headliner",
            "paragraph": "The city announced new flood barriers.",
            "summaries": ["First summary", "Second summary"],
        }
        self.assertIn("Headliner", ss._story_screening_text(candidate))
        fallback = json.loads(ss._scale_screening_fallback_content([candidate, {"story_key": "beta"}]))
        self.assertEqual([entry["story_key"] for entry in fallback], ["story-1", "beta"])
        self.assertTrue(all(entry["scale"] == ss.STORY_SCALE_DEFAULT_VERDICT for entry in fallback))

        prompt_messages = ss._global_scale_screening_prompt_messages([candidate])
        self.assertEqual(len(prompt_messages), 2)
        self.assertIn("Story key: story-1", prompt_messages[1].content)
        self.assertIn("obviously_large_scale", prompt_messages[0].content)

        injected_messages = ss._global_scale_screening_prompt_messages(
            [candidate],
            prompt_instructions="Judge scale only from the supplied facts.",
        )
        self.assertIn("Judge scale only from the supplied facts.", injected_messages[0].content)
        self.assertIn("Return only valid JSON", injected_messages[0].content)
        self.assertIn("obviously_large_scale", injected_messages[0].content)

        self.assertEqual(
            ss._deterministic_global_scale_record({"story_title": "Wordle hints and answers"}),
            {
                "scale": ss.STORY_SCALE_OBVIOUSLY_SMALL,
                "scale_reason": "Daily puzzle hints or answers are evergreen service content, not a major news event.",
            },
        )
        self.assertEqual(
            ss._deterministic_global_scale_record({"story_title": "PSG fans riot after match"}),
            {
                "scale": ss.STORY_SCALE_OBVIOUSLY_SMALL,
                "scale_reason": "The story is local public disorder around a sporting event.",
            },
        )
        self.assertEqual(
            ss._deterministic_global_scale_record({"story_title": "A normal global story"}),
            {
                "scale": ss.STORY_SCALE_DEFAULT_VERDICT,
                "scale_reason": "No deterministic obvious scale exclusion.",
            },
        )
        self.assertTrue(ss._global_scale_screening_eligible({"scale_screening_scale": "obviously_large_scale"}))
        self.assertFalse(ss._global_scale_screening_eligible({"scale_screening_scale": "not_obvious"}))
        debug = ss._selected_global_story_debug_record(
            {
                "story_key": "alpha",
                "story_title": "Alpha",
                "story_strength_score": 3.5,
                "average_similarity": 0.75,
                "article_ids": ["a1"],
                "cluster_article_ids": ["a1"],
            }
        )
        self.assertEqual(debug["story_key"], "alpha")
        self.assertEqual(debug["article_ids"], ["a1"])

    def test_apply_global_story_scale_screening_variants(self) -> None:
        disabled_runtime = _runtime(enabled=False)
        kept, stats = ss.apply_global_story_scale_screening(
            [{"story_key": "alpha", "story_title": "Alpha"}],
            disabled_runtime,
        )
        self.assertEqual(kept, [])
        self.assertEqual(stats["skipped_reason"], "disabled")
        self.assertEqual(stats["dropped_count"], 1)

        empty_kept, empty_stats = ss.apply_global_story_scale_screening([], _runtime(enabled=True))
        self.assertEqual(empty_kept, [])
        self.assertEqual(empty_stats["skipped_reason"], "no_candidates")

        progress_events: list[tuple[str, dict]] = []
        responses = iter(
            [
                SimpleNamespace(
                    content=json.dumps(
                        [
                            {
                                "story_key": "story-1",
                                "scale": "obviously_large_scale",
                                "scale_reason": "broad stakes",
                            }
                        ]
                    )
                ),
                RuntimeError("boom"),
            ]
        )

        def invoke_with_retries(_model, _messages, **_kwargs):
            outcome = next(responses)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        story_drafts = [
            {
                "story_key": f"story-{index}",
                "story_title": f"Story {index}",
                "paragraph": f"Paragraph {index}",
                "summaries": [f"Summary {index}"],
            }
            for index in range(1, 10)
        ]

        runtime = _runtime(
            enabled=True,
            progress=lambda event, payload: progress_events.append((event, payload)),
            invoke_with_retries=invoke_with_retries,
            low_confidence=lambda entry: "story-2" in str(entry),
        )
        kept, stats = ss.apply_global_story_scale_screening(story_drafts, runtime)

        self.assertEqual([story["story_key"] for story in kept], ["story-1"])
        self.assertEqual(stats["judged_count"], 1)
        self.assertEqual(stats["fallback_count"], 8)
        self.assertEqual(stats["missing_verdict_count"], 8)
        self.assertEqual(stats["kept_count"], 1)
        self.assertEqual(stats["dropped_count"], 8)
        self.assertTrue(stats["parse_failed"])
        self.assertIn("RuntimeError", stats["model_error"])
        self.assertEqual(stats["scale_counts"]["obviously_large_scale"], 1)
        self.assertEqual(stats["scale_counts"][ss.STORY_SCALE_DEFAULT_VERDICT], 8)
        self.assertEqual(progress_events[0][0], "scale_screening_started")
        self.assertEqual(progress_events[-1][0], "scale_screening_batch_completed")

        fallback_runtime = _runtime(
            enabled=True,
            invoke_with_retries=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with patch.object(
            ss,
            "_deterministic_global_scale_record",
            return_value={
                "scale": ss.STORY_SCALE_OBVIOUSLY_LARGE,
                "scale_reason": "Forced large-scale fallback for coverage.",
            },
        ):
            kept, stats = ss.apply_global_story_scale_screening(
                [
                    {
                        "story_key": "forced",
                        "story_title": "Forced large-scale fallback",
                        "paragraph": "A global supply chain story.",
                        "summaries": ["Global stakes"],
                    }
                ],
                fallback_runtime,
            )
        self.assertEqual([story["story_key"] for story in kept], ["forced"])
        self.assertEqual(stats["fallback_kept_count"], 1)
        self.assertEqual(stats["kept_count"], 1)

    def test_select_and_report_assembly_paths(self) -> None:
        draft_a = {
            "story_key": "a",
            "story_title": "Story A",
            "story_strength_score": 4.0,
            "connectedness_score": 2.5,
            "article_count": 2,
            "source_count": 2,
            "average_similarity": 0.9,
            "article_ids": ["a1", "a2"],
            "cluster_article_ids": ["a1", "a2"],
        }
        draft_b = {
            "story_key": "b",
            "story_title": "Story B",
            "story_strength_score": 3.0,
            "connectedness_score": 2.0,
            "article_count": 2,
            "source_count": 2,
            "average_similarity": 0.8,
            "article_ids": ["a2", "b2"],
            "cluster_article_ids": ["a2", "b2"],
        }
        draft_c = {
            "story_key": "c",
            "story_title": "Story C",
            "story_strength_score": 2.0,
            "connectedness_score": 1.5,
            "article_count": 2,
            "source_count": 2,
            "average_similarity": 0.7,
            "article_ids": ["c1", "c2"],
            "cluster_article_ids": ["c1", "c2"],
        }
        draft_d = {
            "story_key": "d",
            "story_title": "Story D",
            "story_strength_score": 1.0,
            "connectedness_score": 1.0,
            "article_count": 2,
            "source_count": 2,
            "average_similarity": 0.6,
            "article_ids": ["d1", "d2"],
            "cluster_article_ids": ["d1", "d2"],
        }

        selected, stats = ss.select_global_story_drafts(
            [draft_d, draft_b, draft_a, draft_c],
            max_stories=2,
            overlap_threshold=0.25,
        )

        self.assertEqual([story["story_key"] for story in selected], ["a", "c"])
        self.assertEqual(stats["selected_story_count"], 2)
        self.assertEqual(stats["article_overlap_dedup"]["conflicts_resolved"], 1)
        self.assertEqual(stats["rejected"][0]["reason"], "article_overlap_above_global_threshold")
        self.assertEqual(stats["rejected"][1]["reason"], "global_story_limit_reached")

        duplicate_story = StoryRecord(
            story_key="ports",
            story_title="Port Talks Resume",
            article_ids=("a1", "", "a1", "missing"),
            cluster_article_ids=("a1", "", "a1", "missing"),
            article_count=4,
            cluster_article_count=4,
            selected_article_count=1,
        )
        duplicate_reports, duplicate_stats = ss.build_story_assigned_article_reports(
            [duplicate_story],
            [_summary("a1"), _summary("a2")],
            [
                {
                    "article_id": "a1",
                    "title": "Article a1",
                    "source": "Fixture Wire",
                    "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                    "url": "https://example.com/a1",
                },
                {
                    "article_id": "a2",
                    "title": "Article a2",
                    "source": "Fixture Wire",
                    "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                    "url": "https://example.com/a2",
                },
            ],
            _runtime(),
        )
        self.assertEqual(duplicate_stats["included_report_count"], 1)
        self.assertEqual(duplicate_stats["missing_summary_article_ids"], ["missing"])
        self.assertEqual(len(duplicate_reports), 1)

        story_match = {
            "story_key": "ports",
            "story_title": "Port Talks Resume",
            "story_headline": "## Port Talks Resume [99] with enough extra words to be trimmed cleanly for display",
            "paragraph": "Negotiators resumed talks.",
            "main_story_paragraph": "Negotiators resumed talks.",
            "article_ids": ["a1", "", "a2", "a2", "missing"],
            "cluster_article_ids": ["a1", "a2", "missing"],
            "cited_sentences": [
                {"text": "Negotiators resumed talks.", "source_ids": ["S1"]},
                {"text": "Union leaders opened a vote.", "source_ids": ["S2"]},
            ],
            "citation_sources": [
                _citation_source(1, "Port talks article", "S1"),
                _citation_source(2, "Union vote article", "S2"),
                _citation_source(3, "Grid repairs article", "S3"),
            ],
            "contradictions_paragraph": "Some outlets said negotiations stalled.",
            "contradiction_cited_sentences": [
                {"text": "Some outlets said negotiations stalled.", "source_ids": ["S2", "S3", "S2"]},
            ],
        }
        skip_story = {
            "story_key": "skip",
            "story_title": "Skip story",
            "paragraph": "",
            "article_ids": [],
            "cluster_article_ids": [],
            "citation_sources": [],
        }
        article_reports = [_summary("a1"), _summary("a2")]
        article_targets = [
            {
                "article_id": "a1",
                "title": "Article a1",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/a1",
            },
            {
                "article_id": "a2",
                "title": "Article a2",
                "source": "Fixture Wire",
                "pub_date": "Mon, 01 Jun 2026 12:00:00 GMT",
                "url": "https://example.com/a2",
            },
        ]

        reports, report_stats = ss.build_story_assigned_article_reports(
            [story_match, skip_story],
            article_reports,
            article_targets,
            _runtime(),
        )
        rendered_reports = "\n".join(render_markdown_entry(report) for report in reports)
        self.assertEqual(report_stats["included_report_count"], 2)
        self.assertEqual(report_stats["missing_summary_article_ids"], ["missing"])
        self.assertIn("- Story: Port Talks Resume", rendered_reports)
        self.assertNotIn("- Topic:", rendered_reports)

        def fake_render_cited_story(cited_sentences, citation_sources, registry, **kwargs):
            numbers = [registry.register(source) for source in citation_sources]
            if kwargs.get("apply_precedence") is False:
                return {
                    "paragraph": "Counterpoint paragraph [3].",
                    "story_citation_numbers": [3, "bad"],
                    "story_level_source_numbers": [],
                    "story_level_source_sentence_counts": {},
                    "citation_precedence_diagnostics": {"apply_precedence": False},
                    "headline_citation_text": "",
                }
            return {
                "paragraph": " ".join(sentence.get("text") or "" for sentence in cited_sentences),
                "headline_citation_text": f"[{numbers[0]}]",
                "story_level_source_numbers": numbers[:2],
                "story_citation_numbers": [1, "x", 2, 0, 2],
                "story_level_source_sentence_counts": {"S1": 1, "S2": 1},
                "citation_precedence_diagnostics": {"apply_precedence": True},
            }

        runtime = _runtime(
            low_confidence=lambda entry: "a2" in str(entry),
            invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content="[]"),
        )
        with patch("news_pipeline.story_selection.citations_stage.render_cited_story", side_effect=fake_render_cited_story):
            synthesis, token_stats, debug = ss.build_precomputed_global_story_synthesis(
                [story_match, skip_story],
                [render_markdown_entry(report) for report in reports],
                runtime,
            )

        self.assertIn("### Port Talks Resume", synthesis)
        self.assertIn("Contradictions: Counterpoint paragraph [3].", synthesis)
        self.assertEqual(token_stats["citation_source_count"], 3)
        self.assertEqual(token_stats["citation_group_count"], 1)
        self.assertEqual(token_stats["high_confidence_reports"], 1)
        self.assertEqual(token_stats["low_confidence_reports"], 1)
        self.assertEqual(len(token_stats["required_story_headlines"]), 1)
        self.assertNotIn("##", token_stats["required_story_headlines"][0])
        self.assertNotIn("[99]", token_stats["required_story_headlines"][0])
        self.assertLessEqual(len(token_stats["required_story_headlines"][0].split()), 13)
        self.assertEqual(debug["attempts"][0]["story_citation_numbers"], [1, 2, 3])
        self.assertTrue(debug["attempts"][0]["contradiction_rendered"])
        self.assertTrue(debug["attempts"][0]["display_story_headline"].endswith("[1]"))
        self.assertEqual(ss._distinct_cited_source_ids(story_match["contradiction_cited_sentences"]), ["S2", "S3"])


if __name__ == "__main__":
    unittest.main()
