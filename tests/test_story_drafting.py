from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from news_pipeline.story_drafting import (
    StoryDraftingRuntime,
    article_summary_lookup_by_id,
    clean_story_synthesis_contradictions,
    clean_story_synthesis_headline,
    clean_story_synthesis_paragraph,
    contradiction_presence_diagnostics,
    draft_story_clusters_from_article_summaries,
    report_article_id,
    run_story_synthesis_blocks,
    build_story_synthesis_prompt_messages,
    parse_story_synthesis_output,
    run_story_synthesis_block,
    story_summary_blocks_from_clusters,
    summarize_contradiction_analytics,
    _article_body_evidence,
    _article_lookup_by_id,
    _distinct_cited_source_ids,
)


def _story_drafting_runtime() -> StoryDraftingRuntime:
    return StoryDraftingRuntime(
        story_synthesis_concurrency=1,
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
    )


def _story_drafting_runtime_with_response(content: str) -> StoryDraftingRuntime:
    return replace(
        _story_drafting_runtime(),
        invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(content=content),
        extract_prompt_tokens_from_response=lambda _response: 7,
    )


def _summary_entry(article_id: str, title: str, summary: str) -> str:
    return (
        f"### {title}\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Sat, 16 May 2026 15:30:00 GMT\n"
        f"- URL: https://example.com/{article_id}\n"
        f"- Article ID: {article_id}\n\n"
        "Summary:\n"
        f"{summary}"
    )


def _source(local_id: str, **overrides):
    record = {
        "local_id": local_id,
        "title": f"Fixture article {local_id}",
        "source": "Fixture Wire",
        "published": "Sat, 16 May 2026 15:30:00 GMT",
        "url": f"https://example.com/{local_id.lower()}",
        "article_id": local_id.lower(),
        "summary": "Officials reported storm damage.",
    }
    record.update(overrides)
    return record


class StoryDraftingTests(unittest.TestCase):
    def test_basic_report_and_lookup_helpers_cover_empty_branches(self) -> None:
        self.assertEqual(report_article_id("### Title\nMetadata:\n- Article ID: a1\nSummary:\nBody"), "a1")
        self.assertEqual(_article_lookup_by_id(None), {})
        self.assertEqual(_article_lookup_by_id([{"title": "Missing id"}]), {})
        self.assertEqual(_article_body_evidence(None), "")

    def test_story_prompt_requests_paraphrased_clean_evidence_with_source_markers(self) -> None:
        messages = build_story_synthesis_prompt_messages(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": ["Officials reported storm damage."],
                "citation_sources": [
                    _source(
                        "S1",
                        body_evidence=(
                            "County logs listed ten damaged homes and two closed roads."
                        ),
                    )
                ],
            },
            "May 30, 2026",
        )

        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertIn("Cleaned article evidence to paraphrase, not quote", prompt_text)
        self.assertIn("Paraphrase those details in your own words", prompt_text)
        self.assertIn("do not quote article text", prompt_text)
        self.assertIn("source markers", prompt_text)
        self.assertIn("[[S1]]", prompt_text)

    def test_story_prompt_includes_citation_precedence_without_reordering_sources(self) -> None:
        messages = build_story_synthesis_prompt_messages(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": [
                    "Associated Press reported levee repairs.",
                    "Yahoo cited AP on levee repairs and added local reaction.",
                ],
                "citation_sources": [
                    _source(
                        "S1",
                        source="Associated Press",
                        summary="Officials approved levee repairs.",
                    ),
                    _source(
                        "S2",
                        source="Yahoo News",
                        summary=(
                            "According to AP, officials approved levee repairs. "
                            "Yahoo reported local reaction."
                        ),
                        body_evidence=(
                            "According to AP, officials approved levee repairs. "
                            "Yahoo reported local reaction."
                        ),
                    ),
                ],
            },
            "May 30, 2026",
        )

        prompt_text = "\n\n".join(str(message.content) for message in messages)

        self.assertLess(prompt_text.index("S1:"), prompt_text.index("S2:"))
        self.assertIn("prefer the listed primary source", prompt_text)
        self.assertIn("S2 appears to cite S1", prompt_text)
        self.assertIn("cite S2 only for unique reporting", prompt_text)

    def test_parse_story_synthesis_output_omits_none_contradictions(self) -> None:
        headline, main_story, contradictions = parse_story_synthesis_output(
            (
                "Headline: Storm Damage Reports Diverge\n"
                "Main story: Officials reported storm damage across the county[[S1,S2]].\n"
                "Contradictions: NONE"
            ),
            ["Officials reported storm damage."],
            "Storm update",
            _story_drafting_runtime(),
        )

        self.assertEqual(headline, "Storm Damage Reports Diverge")
        self.assertEqual(
            main_story,
            "Officials reported storm damage across the county[[S1,S2]].",
        )
        self.assertEqual(contradictions, "")

    def test_story_summary_blocks_attach_capped_clean_body_evidence(self) -> None:
        reports = [
            _summary_entry("a1", "Storm report one", "Officials reported 10 damaged homes."),
            _summary_entry("a2", "Storm report two", "Officials reported 12 damaged homes."),
        ]
        article_lookup = {
            "a1": {
                "article_id": "a1",
                "source": "Fixture Wire",
                "url": "https://example.com/a1",
                "title": "Storm report one",
                "text": "<p>" + ("First article body evidence. " * 140) + "</p>",
            },
            "a2": {
                "article_id": "a2",
                "source": "Fixture Wire",
                "url": "https://example.com/a2",
                "title": "Storm report two",
                "text": "Second article body evidence.",
            },
        }

        blocks = story_summary_blocks_from_clusters(
            [
                {
                    "story_title": "Storm damage",
                    "cluster_article_ids": ["a1", "a2"],
                }
            ],
            article_summary_lookup_by_id(reports),
            article_lookup,
            min_articles_per_story=2,
        )

        self.assertEqual(len(blocks), 1)
        first_evidence = blocks[0]["citation_sources"][0]["body_evidence"]
        self.assertLessEqual(len(first_evidence), 2000)
        self.assertIn("First article body evidence.", first_evidence)
        self.assertNotIn("<p>", first_evidence)

    def test_story_summary_blocks_skip_missing_entries_and_minimums(self) -> None:
        summary_lookup = article_summary_lookup_by_id(
            [
                _summary_entry("a1", "Storm report one", "Officials reported 10 damaged homes."),
                _summary_entry("a2", "Storm report two", ""),
            ]
        )

        self.assertEqual(
            story_summary_blocks_from_clusters(
                [
                    {
                        "story_title": "Storm damage",
                        "article_ids": ["a1", "a2", "missing"],
                    }
                ],
                summary_lookup,
                min_articles_per_story=2,
            ),
            [],
        )

    def test_clean_story_synthesis_helpers_cover_fallback_and_no_contradiction_branches(self) -> None:
        runtime = replace(_story_drafting_runtime(), is_low_coverage_synthesis_section=lambda _text: True)
        long_fallback = " ".join(f"word{i}" for i in range(1, 16))
        headline = clean_story_synthesis_headline("", long_fallback, runtime)
        self.assertLessEqual(len(headline.split()), 12)
        self.assertEqual(
            clean_story_synthesis_paragraph("   ", ["Fallback source summary."], runtime),
            "Fallback source summary.",
        )
        self.assertEqual(clean_story_synthesis_contradictions("None reported", runtime), "")
        self.assertEqual(clean_story_synthesis_contradictions("Potential contradiction", runtime), "")
        self.assertEqual(
            clean_story_synthesis_contradictions("A real contradiction", _story_drafting_runtime()),
            "A real contradiction",
        )

    def test_contradiction_diagnostics_helpers_cover_analytics_branches(self) -> None:
        self.assertEqual(
            _distinct_cited_source_ids(
                [
                    {"source_ids": ["s1", "S2", "s1"]},
                    {"source_ids": ["s3"]},
                ]
            ),
            ["S1", "S2", "S3"],
        )
        presence = contradiction_presence_diagnostics(
            {
                "story_key": "story-1",
                "story_title": "Storm damage",
                "article_count": 2,
                "source_count": 2,
                "marked_contradictions": "Contradiction text",
                "contradictions_paragraph": "Validated contradiction text",
                "contradiction_cited_sentences": [{"source_ids": ["s1", "S2"]}],
                "contradiction_citation_diagnostics": {"foo": 1},
            }
        )
        self.assertTrue(presence["render_eligible"])
        analytics = summarize_contradiction_analytics(
            [
                {
                    "marked_contradictions": "Raw contradiction",
                    "contradictions_paragraph": "",
                    "contradiction_cited_sentences": [],
                },
                {
                    "marked_contradictions": "Raw contradiction",
                    "contradictions_paragraph": "Validated contradiction",
                    "contradiction_cited_sentences": [{"source_ids": ["s1"]}],
                },
            ]
        )
        self.assertEqual(analytics["raw_contradiction_count"], 2)
        self.assertEqual(analytics["validated_contradiction_count"], 1)
        self.assertEqual(analytics["render_eligible_contradiction_count"], 0)
        self.assertEqual(analytics["validated_contradictions_not_render_eligible"], 1)

    def test_run_story_synthesis_blocks_and_draft_story_clusters_cover_sequential_and_rejection_paths(self) -> None:
        runtime = _story_drafting_runtime_with_response(
            "Headline: Storm Cleanup Advances\n"
            "Main story: County logs showed ten homes were damaged after the storm.[[S1]]\n"
            "Contradictions: NONE"
        )
        story_block = {
            "story_title": "Storm damage",
            "summaries": ["County officials reported ten damaged homes."],
            "citation_sources": [
                _source(
                    "S1",
                    summary="County officials reported ten damaged homes.",
                    body_evidence="County logs listed ten damaged homes after the storm.",
                )
            ],
        }
        callbacks: list[tuple[str, dict[str, Any]]] = []
        runtime = replace(runtime, progress_callback=lambda label, payload: callbacks.append((label, payload)))
        results = run_story_synthesis_blocks([story_block], "May 30, 2026", runtime)
        self.assertEqual(len(results), 1)
        self.assertTrue(callbacks)

        short_runtime = replace(
            runtime,
            story_synthesis_concurrency=1,
            invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(
                content="Headline: Storm Cleanup Advances\nMain story: short paragraph[[S1]]\nContradictions: NONE"
            ),
            story_drafting_word_count=lambda text: len(str(text or "").split()),
        )
        drafts, metadata = draft_story_clusters_from_article_summaries(
            [
                {
                    "story_title": "Storm damage",
                    "article_ids": ["a1", "a2"],
                }
            ],
            [
                _summary_entry("a1", "Storm report one", "Officials reported 10 damaged homes."),
                _summary_entry("a2", "Storm report two", "Officials reported 12 damaged homes."),
            ],
            short_runtime,
            article_targets=[
                {
                    "article_id": "a1",
                    "source": "Fixture Wire",
                    "url": "https://example.com/a1",
                    "title": "Storm report one",
                    "text": "County logs listed ten damaged homes after the storm.",
                }
                ,
                {
                    "article_id": "a2",
                    "source": "Fixture Wire",
                    "url": "https://example.com/a2",
                    "title": "Storm report two",
                    "text": "County logs listed twelve damaged homes after the storm.",
                },
            ],
        )
        self.assertEqual(drafts, [])
        self.assertEqual(metadata["story_blocks_requested"], 1)
        self.assertEqual(metadata["story_drafts_generated"], 0)
        self.assertEqual(metadata["story_draft_rejections"][0]["reason"], "below_story_word_count_floor")

        empty_drafts, empty_metadata = draft_story_clusters_from_article_summaries(
            [
                {
                    "story_title": "Singleton story",
                    "article_ids": ["a1"],
                }
            ],
            [
                _summary_entry("a1", "Solo report", "Officials reported 10 damaged homes."),
            ],
            short_runtime,
            article_targets=[
                {
                    "article_id": "a1",
                    "source": "Fixture Wire",
                    "url": "https://example.com/a1",
                    "title": "Solo report",
                    "text": "County logs listed ten damaged homes after the storm.",
                }
            ],
        )
        self.assertEqual(empty_drafts, [])
        self.assertEqual(empty_metadata["story_blocks_requested"], 0)
        self.assertEqual(empty_metadata["missing_or_singleton_story_count"], 1)

        valid_runtime = replace(
            runtime,
            invoke_with_retries=lambda *_args, **_kwargs: SimpleNamespace(
                content=(
                    "Headline: Storm Cleanup Advances\n"
                    "Main story: "
                    + " ".join(f"word{i}" for i in range(60))
                    + " [[S1]]\n"
                    "Contradictions: NONE"
                )
            ),
            story_drafting_word_count=lambda text: len(str(text or "").split()),
        )
        valid_drafts, valid_metadata = draft_story_clusters_from_article_summaries(
            [
                {
                    "story_title": "Storm damage",
                    "article_ids": ["a1", "a2"],
                }
            ],
            [
                _summary_entry("a1", "Storm report one", "Officials reported 10 damaged homes."),
                _summary_entry("a2", "Storm report two", "Officials reported 12 damaged homes."),
            ],
            valid_runtime,
            article_targets=[
                {
                    "article_id": "a1",
                    "source": "Fixture Wire",
                    "url": "https://example.com/a1",
                    "title": "Storm report one",
                    "text": "County logs listed ten damaged homes after the storm.",
                },
                {
                    "article_id": "a2",
                    "source": "Fixture Wire",
                    "url": "https://example.com/a2",
                    "title": "Storm report two",
                    "text": "County logs listed twelve damaged homes after the storm.",
                },
            ],
        )
        self.assertEqual(len(valid_drafts), 1)
        self.assertEqual(valid_drafts[0]["story_text"], valid_drafts[0]["paragraph"].strip())
        self.assertEqual(valid_metadata["story_drafts_generated"], 1)

    def test_run_story_synthesis_block_preserves_validated_evidence_citations(self) -> None:
        result = run_story_synthesis_block(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": [
                    "County officials reported ten damaged homes.",
                    "Transportation officials reopened the highway.",
                ],
                "citation_sources": [
                    _source(
                        "S1",
                        summary="County officials reported ten damaged homes.",
                        body_evidence=(
                            "County logs listed ten damaged homes after the storm."
                        ),
                    ),
                    _source(
                        "S2",
                        summary="Transportation officials reopened the highway.",
                        body_evidence=(
                            "The highway reopened after crews cleared fallen trees."
                        ),
                    ),
                ],
            },
            "May 30, 2026",
            _story_drafting_runtime_with_response(
                "Headline: Storm Cleanup Advances\n"
                "Main story: County logs showed ten homes were damaged after the storm.[[S1]] "
                "Transportation officials said the highway reopened after crews cleared fallen trees.[[S2]]\n"
                "Contradictions: NONE"
            ),
        )

        self.assertEqual(
            result["paragraph"],
            (
                "County logs showed ten homes were damaged after the storm. "
                "Transportation officials said the highway reopened after crews cleared fallen trees."
            ),
        )
        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(result["cited_sentences"][1]["source_ids"], ["S2"])
        self.assertTrue(result["citation_diagnostics"]["has_validated_citation"])
        self.assertEqual(
            result["citation_diagnostics"]["validated_citation_sentence_count"],
            2,
        )

    def test_run_story_synthesis_block_accepts_uncited_story_without_inventing_citations(self) -> None:
        result = run_story_synthesis_block(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": [
                    "County officials reported storm damage.",
                    "Transportation officials reopened the highway.",
                ],
                "citation_sources": [
                    _source("S1", summary="County officials reported storm damage."),
                    _source("S2", summary="Transportation officials reopened the highway."),
                ],
            },
            "May 30, 2026",
            _story_drafting_runtime_with_response(
                "Headline: Storm Cleanup Advances\n"
                "Main story: County officials reported storm damage across several neighborhoods after "
                "overnight winds brought down trees and damaged power lines. Transportation officials "
                "said crews reopened the highway after clearing debris, while emergency managers kept "
                "shelters available for residents whose homes still needed inspection. Local agencies "
                "said the recovery would continue through the weekend as utility repairs and road checks "
                "moved block by block.\n"
                "Contradictions: NONE"
            ),
        )

        self.assertTrue(result["valid"])
        self.assertEqual(
            [sentence["source_ids"] for sentence in result["cited_sentences"]],
            [[], [], []],
        )
        self.assertFalse(result["citation_diagnostics"]["has_validated_citation"])
        self.assertEqual(
            result["citation_diagnostics"]["validated_citation_sentence_count"],
            0,
        )
        self.assertEqual(result["citation_diagnostics"]["uncited_sentence_count"], 3)

    def test_run_story_synthesis_block_rejects_short_nonempty_story(self) -> None:
        result = run_story_synthesis_block(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": [
                    "County officials reported storm damage.",
                    "Transportation officials reopened the highway.",
                ],
                "citation_sources": [
                    _source("S1", summary="County officials reported storm damage."),
                    _source("S2", summary="Transportation officials reopened the highway."),
                ],
            },
            "May 30, 2026",
            _story_drafting_runtime_with_response(
                "Headline: Storm Cleanup Advances\n"
                "Main story: County officials reported storm damage. "
                "Transportation officials reopened the highway.\n"
                "Contradictions: NONE"
            ),
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "below_story_word_count_floor")
        self.assertLess(result["word_count"], result["min_word_count"])

    def test_run_story_synthesis_block_accepts_normal_length_story(self) -> None:
        result = run_story_synthesis_block(
            {
                "topic_title": "Weather",
                "story_title": "Storm damage",
                "summaries": [
                    "County officials reported storm damage.",
                    "Transportation officials reopened the highway.",
                ],
                "citation_sources": [
                    _source("S1", summary="County officials reported storm damage."),
                    _source("S2", summary="Transportation officials reopened the highway."),
                ],
            },
            "May 30, 2026",
            _story_drafting_runtime_with_response(
                "Headline: Storm Cleanup Advances\n"
                "Main story: County officials reported storm damage across several neighborhoods "
                "after overnight winds brought down trees and damaged power lines. Transportation "
                "officials said crews reopened the highway after clearing debris, while emergency "
                "managers kept shelters available for residents whose homes still needed inspection. "
                "Local agencies said the recovery would continue through the weekend as utility "
                "repairs and road checks moved block by block.\n"
                "Contradictions: NONE"
            ),
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], "accepted")


if __name__ == "__main__":
    unittest.main()
