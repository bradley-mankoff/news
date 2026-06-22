from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from news_pipeline.story_drafting import (
    StoryDraftingRuntime,
    article_summary_lookup_by_id,
    build_story_synthesis_prompt_messages,
    parse_story_synthesis_output,
    run_story_synthesis_block,
    story_summary_blocks_from_clusters,
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
