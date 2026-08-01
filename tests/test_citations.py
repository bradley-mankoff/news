from __future__ import annotations

import unittest

from news_pipeline import citations as citations_stage
from news_pipeline.pipeline import build_report_body, build_report_html
from unittest.mock import patch


def _source(local_id: str, **overrides):
    record = {
        "local_id": local_id,
        "title": f"Fixture article {local_id}",
        "source": "Fixture Wire",
        "published": "Sat, 16 May 2026 15:30:00 GMT",
        "url": f"https://example.com/{local_id.lower()}",
        "article_id": local_id.lower(),
        "summary": "Officials approved levee repairs, backup pumps, and cooling centers.",
    }
    record.update(overrides)
    return record


class CitationHelperTests(unittest.TestCase):

    def test_validate_cited_story_text_flags_unknown_markers_and_leaves_uncited_sentences(self) -> None:
        sources = [
            _source("S1", summary="U.S. officials approved levee repairs and backup pumps."),
            _source("S2", summary="The city opened cooling centers before the summer heat season."),
        ]
        marked_text = (
            "U.S. officials approved levee repairs and backup pumps[[S1]]. "
            "The city opened cooling centers[[S9]]. "
            "Road closures may still happen during extreme storms."
        )

        result = citations_stage.validate_cited_story_text(marked_text, sources)

        self.assertEqual(len(result["cited_sentences"]), 3)
        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(result["cited_sentences"][1]["source_ids"], [])
        self.assertEqual(result["cited_sentences"][2]["source_ids"], [])
        self.assertEqual(result["diagnostics"]["unknown_source_ids"], ["S9"])
        self.assertEqual(result["diagnostics"]["uncited_sentence_count"], 2)
        self.assertNotIn("[[", result["paragraph"])

        skipped = citations_stage.validate_cited_story_text("Alpha[[S1]]. [[S2]]", sources)
        self.assertEqual(len(skipped["cited_sentences"]), 1)

    def test_derivative_wire_citation_uses_primary_for_overlapping_fact(self) -> None:
        sources = [
            _source(
                "S1",
                title="Levee repairs approved",
                source="Associated Press",
                summary="Officials approved levee repairs and backup pumps.",
            ),
            _source(
                "S2",
                title="Yahoo republishes levee plan",
                source="Yahoo News",
                summary=(
                    "The Associated Press reported officials approved levee repairs "
                    "and backup pumps."
                ),
                body_evidence=(
                    "The Associated Press reported that officials approved levee "
                    "repairs and backup pumps."
                ),
            ),
        ]

        result = citations_stage.validate_cited_story_text(
            "Officials approved levee repairs and backup pumps[[S2]].",
            sources,
        )

        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(
            result["diagnostics"]["citation_precedence_dependencies"],
            [
                {
                    "source_id": "S2",
                    "derives_from": ["S1"],
                    "reason": "wire_attribution:associated press",
                }
            ],
        )
        self.assertEqual(result["diagnostics"]["citation_precedence_replacement_count"], 1)

    def test_derivative_citation_is_suppressed_when_primary_already_cited(self) -> None:
        sources = [
            _source(
                "S1",
                source="Associated Press",
                summary="Officials approved levee repairs and backup pumps.",
            ),
            _source(
                "S2",
                source="Yahoo News",
                summary="According to AP, officials approved levee repairs and backup pumps.",
                body_evidence="According to AP, officials approved levee repairs and backup pumps.",
            ),
        ]

        result = citations_stage.validate_cited_story_text(
            "Officials approved levee repairs and backup pumps[[S2,S1]].",
            sources,
        )

        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(result["diagnostics"]["citation_precedence_suppression_count"], 1)

    def test_derivative_unique_claim_remains_cited(self) -> None:
        sources = [
            _source(
                "S1",
                source="Associated Press",
                summary="Officials approved levee repairs and backup pumps.",
            ),
            _source(
                "S2",
                source="Yahoo Finance",
                summary=(
                    "Reuters reported officials approved levee repairs. Yahoo Finance "
                    "said analysts expected a small share-price move."
                ),
                body_evidence=(
                    "Reuters reported the levee decision. Analysts told Yahoo Finance "
                    "they expected a small share-price move."
                ),
            ),
            _source(
                "S3",
                source="Reuters",
                summary="Officials approved levee repairs and backup pumps.",
            ),
        ]

        result = citations_stage.validate_cited_story_text(
            (
                "Officials approved levee repairs, and analysts expected "
                "a small share-price move[[S2]]."
            ),
            sources,
        )

        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S3", "S2"])
        self.assertEqual(result["diagnostics"]["citation_precedence_retained_derivative_count"], 1)

    def test_derivative_source_remains_when_primary_is_absent(self) -> None:
        sources = [
            _source(
                "S1",
                source="Yahoo News",
                summary="The Associated Press reported officials approved levee repairs.",
                body_evidence="The Associated Press reported officials approved levee repairs.",
            )
        ]

        result = citations_stage.validate_cited_story_text(
            "Officials approved levee repairs[[S1]].",
            sources,
        )

        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(result["diagnostics"]["citation_precedence_dependencies"], [])

    def test_wire_self_attribution_is_not_marked_derivative(self) -> None:
        sources = [
            _source(
                "S1",
                source="Associated Press",
                summary="The Associated Press reported officials approved levee repairs.",
                body_evidence="By The Associated Press. Officials approved levee repairs.",
            )
        ]

        annotated = citations_stage.annotate_citation_precedence(sources)

        self.assertEqual(annotated[0]["citation_precedence_derives_from"], [])
        self.assertEqual(annotated[0]["citation_precedence_role"], "neutral")

    def test_same_source_previous_report_uses_earlier_source(self) -> None:
        sources = [
            _source(
                "S1",
                source="Fixture Wire",
                published="Sat, 16 May 2026 14:30:00 GMT",
                summary="Officials approved levee repairs and backup pumps.",
            ),
            _source(
                "S2",
                source="Fixture Wire",
                published="Sat, 16 May 2026 15:30:00 GMT",
                summary=(
                    "As Fixture Wire previously reported, officials approved levee "
                    "repairs and backup pumps."
                ),
                body_evidence=(
                    "As Fixture Wire previously reported, officials approved levee "
                    "repairs and backup pumps."
                ),
            ),
        ]

        result = citations_stage.validate_cited_story_text(
            "Officials approved levee repairs and backup pumps[[S2]].",
            sources,
        )

        self.assertEqual(result["cited_sentences"][0]["source_ids"], ["S1"])
        self.assertEqual(
            result["diagnostics"]["citation_precedence_dependencies"][0]["reason"],
            "same_org_previous_report",
        )


    def test_html_citation_rendering_links_to_escaped_bottom_sources(self) -> None:
        citation_sources = [
            {
                "number": 1,
                "title": "Flood <plan>",
                "source": "Wire & Co",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/flood?a=1&b=2",
            }
        ]

        inline_html = citations_stage.render_html_text_with_citations(
            "Officials approved repairs.[1]",
            citation_sources,
        )
        sources_html = citations_stage.render_html_sources(citation_sources)

        self.assertIn("<sup", inline_html)
        self.assertIn('href="https://example.com/flood?a=1&amp;b=2"', inline_html)
        self.assertIn('[<a href="https://example.com/flood?a=1&amp;b=2"', inline_html)
        self.assertIn(">1</a>]", inline_html)
        self.assertNotIn(">[1]</a>", inline_html)
        self.assertIn('id="source-1"', sources_html)
        self.assertIn("Flood &lt;plan&gt;", sources_html)
        self.assertIn("Wire &amp; Co", sources_html)
        self.assertIn("a=1&amp;b=2", sources_html)

    def test_html_citation_rendering_falls_back_to_internal_anchor_without_url(self) -> None:
        inline_html = citations_stage.render_html_text_with_citations(
            "Officials approved repairs.[1]",
            [{"number": 1, "title": "Flood plan", "source": "Wire", "url": ""}],
        )

        self.assertIn('[<a href="#source-1"', inline_html)


class CitationPrivateHelperTests(unittest.TestCase):
    def test_org_and_precedence_helpers_cover_edge_branches(self) -> None:
        self.assertEqual(citations_stage._fold_label("  Plenary AP & Co  "), "ap and co")
        self.assertEqual(citations_stage._normalize_org_label(""), "")
        self.assertEqual(citations_stage._normalize_org_label("AP News wire"), "associated press")
        self.assertEqual(citations_stage._normalize_org_label("Yahoo News flash"), "yahoo news")
        self.assertEqual(citations_stage._normalize_org_label("Yahoo Finance desk"), "yahoo finance")
        self.assertTrue(
            citations_stage._contains_attribution_to_org(
                citations_stage._fold_label("According to the Associated Press, officials said"),
                "associated press",
            )
        )
        self.assertFalse(citations_stage._contains_attribution_to_org("Officials said", ""))
        self.assertEqual(
            citations_stage._attributed_wire_orgs(
                {
                    "title": "Wire digest",
                    "source": "Yahoo News",
                    "summary": "According to the Associated Press, officials approved repairs.",
                    "body_evidence": "By Reuters. Officials approved repairs.",
                }
            ),
            ["associated press", "reuters"],
        )
        self.assertTrue(
            citations_stage._has_same_org_reference(
                {"source": "Fixture Wire", "summary": "As Fixture Wire previously reported, officials said"}
            )
        )
        self.assertEqual(citations_stage._source_match_score("", "shared text"), 0.0)
        self.assertEqual(citations_stage._source_order({"citation_precedence_order": "bad"}, fallback=7), 7)
        self.assertEqual(citations_stage._source_rank({"citation_precedence_rank": "bad"}), citations_stage.NEUTRAL_CITATION_RANK)
        self.assertEqual(
            citations_stage._same_org_primary_id(
                {
                    "local_id": "S1",
                    "title": "Fixture story",
                    "source": "Fixture Wire",
                    "published": "Mon, 01 Jun 2026 12:00:00 GMT",
                },
                [
                    {
                        "local_id": "S1",
                        "title": "Fixture story",
                        "source": "Fixture Wire",
                        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
                    }
                ],
            ),
            "",
        )
        self.assertEqual(
            citations_stage._same_org_primary_id(
                {
                    "local_id": "S2",
                    "title": "Fixture story",
                    "source": "Fixture Wire",
                    "published": "Mon, 01 Jun 2026 12:00:00 GMT",
                },
                [
                    {
                        "local_id": "S2",
                        "title": "Fixture story",
                        "source": "Fixture Wire",
                        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
                    },
                    {
                        "local_id": "S1",
                        "title": "Fixture story",
                        "source": "Fixture Wire",
                        "published": "Sun, 31 May 2026 12:00:00 GMT",
                    },
                ],
            ),
            "S1",
        )
        annotated = citations_stage.annotate_citation_precedence([{"title": "No local id"}])
        self.assertEqual(annotated[0]["citation_precedence_role"], "neutral")
        self.assertEqual(
            citations_stage._citation_dependency_map(
                [
                    {"title": "No local id"},
                    {
                        "local_id": "S1",
                        "citation_precedence_derives_from": ["S2", "S2", "S3"],
                    },
                    {"local_id": "S2"},
                ]
            ),
            {"S1": ["S2"]},
        )
        self.assertEqual(citations_stage._citation_group_title({"title": " My Story "}, 0), "My Story")
        self.assertEqual(
            citations_stage.split_cited_sentences("Trailing fragment without punctuation"),
            ["Trailing fragment without punctuation"],
        )
        self.assertEqual(
            citations_stage.format_source_published_timestamp("2026-01-01T12:00:00"),
            "01/01/26, 07:00 AM EST",
        )
        with patch("news_pipeline.citations.split_cited_sentences", return_value=["[[S2]]"]):
            validated = citations_stage.validate_cited_story_text(
                "ignored",
                [{"local_id": "S2", "title": "Fixture", "source": "Wire", "published": ""}],
            )
        self.assertEqual(validated["cited_sentences"], [])
        self.assertEqual(citations_stage.render_html_text_with_citations("See [99].", []), "See [99].")

        citation_sources = [
            {
                "number": 1,
                "title": "One",
                "source": "Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/1",
            },
            {
                "number": 2,
                "title": "Two",
                "source": "Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "",
            },
            {
                "number": 3,
                "title": "Three",
                "source": "Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "",
            },
        ]
        citation_groups = [{"title": " Group One ", "citation_numbers": [1, 2]}]
        plain_sources = citations_stage.render_plain_text_sources(citation_sources, citation_groups)
        self.assertIn("Group One", plain_sources)
        self.assertIn("Additional Sources", plain_sources)
        html_sources = citations_stage.render_html_sources(citation_sources, citation_groups)
        self.assertIn("Group One", html_sources)
        self.assertIn("Additional Sources", html_sources)
        self.assertIn('id="source-1"', html_sources)

    def test_marker_and_sentence_helpers_cover_edge_branches(self) -> None:
        with patch("news_pipeline.citations.re.findall", return_value=[]):
            self.assertEqual(
                citations_stage.normalize_temporary_citation_markers("[[S1], [S2]]"),
                "[[S1], [S2]]",
            )
        self.assertEqual(
            citations_stage.normalize_temporary_citation_markers("[[s1], [S2]]"),
            "[[S1,S2]]",
        )
        self.assertEqual(
            citations_stage.normalize_temporary_citation_markers("[[1], [2]]"),
            "[[1], [2]]",
        )
        self.assertEqual(
            citations_stage._marker_source_ids("First [[s1, s2]] second [[s1]]"),
            ["S1", "S2"],
        )
        self.assertEqual(citations_stage._remove_temporary_markers("Alpha [[S1]] beta"), "Alpha beta")
        self.assertTrue(citations_stage._looks_like_abbreviation("U.S."))
        self.assertEqual(citations_stage.split_cited_sentences(""), [])
        self.assertEqual(
            citations_stage.split_cited_sentences("Dr. Smith went home. Then left."),
            ["Dr. Smith went home.", "Then left."],
        )
        self.assertFalse(
            citations_stage._sentence_overlaps_source(
                "",
                {"title": "Alpha", "source": "Example", "summary": "Shared"},
            )
        )

    def test_apply_and_render_story_paths_cover_missing_and_duplicate_ids(self) -> None:
        sources = [
            _source(
                "S1",
                title="Levee repairs approved",
                source="Associated Press",
                summary="Officials approved levee repairs and backup pumps.",
            ),
            _source(
                "S2",
                title="Yahoo market reaction",
                source="Yahoo Finance",
                summary=(
                    "According to AP, officials approved levee repairs. "
                    "Analysts expected a small share-price move."
                ),
                body_evidence=(
                    "According to AP, officials approved levee repairs. "
                    "Analysts expected a small share-price move."
                ),
            ),
        ]
        with patch(
            "news_pipeline.citations._citation_dependency_map",
            return_value={"S2": ["MISSING"]},
        ):
            precedence = citations_stage.apply_citation_precedence(
                [{"text": "Officials approved levee repairs and backup pumps.", "source_ids": ["S2"]}],
                sources,
            )
        self.assertEqual(precedence["cited_sentences"][0]["source_ids"], ["S2"])

        call_count = {"value": 0}

        def ordered_ids(source_ids, source_by_local_id, *, first_seen_order=None):
            del source_by_local_id, first_seen_order
            call_count["value"] += 1
            if call_count["value"] == 1:
                return ["MISSING", "S1", "S1"]
            if call_count["value"] == 2:
                return ["S1", "S2"]
            return list(source_ids)

        registry = citations_stage.CitationRegistry()
        with patch("news_pipeline.citations._precedence_ordered_source_ids", side_effect=ordered_ids):
            rendered = citations_stage.render_cited_story(
                [
                    {
                        "text": "Officials approved levee repairs and backup pumps.",
                        "source_ids": ["S1", "S2", "S3"],
                    }
                ],
                sources,
                registry,
                story_level_citation_sentence_threshold=0,
                apply_precedence=False,
            )

        self.assertIn("Officials approved levee repairs and backup pumps.[2]", rendered["paragraph"])
        self.assertEqual(rendered["story_level_source_ids"], ["MISSING", "S1", "S1"])
        self.assertEqual([source["number"] for source in registry.sources()], [1, 2])

    def test_html_and_plain_text_helpers_cover_fallback_branches(self) -> None:
        registry = citations_stage.CitationRegistry()
        self.assertEqual(
            registry.register(
                {
                    "url": "https://example.com/story?a=1#frag",
                    "title": "URL story",
                }
            ),
            1,
        )
        self.assertEqual(
            registry.register(
                {
                    "article_id": "Article-1",
                    "title": "ID story",
                }
            ),
            2,
        )
        self.assertEqual(
            registry.register(
                {
                    "source": "Wire",
                    "title": "Meta story",
                    "published": "Mon, 01 Jun 2026 12:00:00 GMT",
                }
            ),
            3,
        )
        self.assertEqual(
            [source["number"] for source in registry.sources()],
            [1, 2, 3],
        )
        self.assertEqual(citations_stage._citation_source_number({"number": "bad"}), 0)
        self.assertEqual(citations_stage._citation_group_title({}, 0), "Story 1")
        self.assertEqual(
            citations_stage._citation_group_numbers(
                {"numbers": [{"number": "2"}, {"citation_number": "3"}, "bad", 0, 2]}
            ),
            [2, 3],
        )
        self.assertEqual(citations_stage._render_plain_text_source({"number": "bad"}), "")
        self.assertEqual(citations_stage._render_plain_text_source_section("Title", [{"number": 0}]), "")
        grouped_sections, additional_sources = citations_stage._grouped_citation_sources(
            [
                {"number": 1, "title": "One"},
                {"number": 2, "title": "Two"},
            ],
            [{"citation_numbers": [1, 9]}],
        )
        self.assertEqual(len(grouped_sections), 1)
        self.assertEqual(len(additional_sources), 1)
        self.assertEqual(
            citations_stage._valid_citation_numbers(
                [{"number": "bad"}, {"number": 0}, {"number": 3}]
            ),
            {3},
        )
        self.assertEqual(
            citations_stage._citation_source_url_by_number(
                [
                    {"number": "bad"},
                    {"number": 0},
                    {"number": 3, "url": ""},
                    {"number": 4, "url": "https://example.com"},
                ]
            ),
            {4: "https://example.com"},
        )
        self.assertIn(
            "[99]",
            citations_stage.render_html_text_with_citations(
                "See [99].",
                [{"number": 1, "url": "https://example.com"}],
            ),
        )
        self.assertIn(
            "No URL",
            citations_stage._render_html_source_item(
                {"number": 1, "title": "No URL", "source": "Wire", "published": "bad", "url": ""}
            ),
        )
        self.assertEqual(
            citations_stage._render_html_source_section("Sources", [{"number": 0}], set()),
            "",
        )
        self.assertIn(
            "No citation sources available.",
            citations_stage.render_html_sources([{"number": 0}], []),
        )
        self.assertIsNone(citations_stage._parse_published_datetime(""))
        self.assertIsNotNone(citations_stage._parse_published_datetime("2026-06-01T12:00:00Z"))
        self.assertIsNone(citations_stage._parse_published_datetime("not-a-date"))
        self.assertEqual(citations_stage.format_source_published_timestamp("not-a-date"), "not-a-date")


class CitationIntegrationTests(unittest.TestCase):
    def test_report_renderers_use_sources_section_when_citations_exist(self) -> None:
        citation_sources = [
            {
                "number": 1,
                "title": "Levee repairs approved",
                "source": "Fixture Wire",
                "published": "Sat, 16 May 2026 15:30:00 GMT",
                "url": "https://example.com/levee",
            }
        ]
        synthesis_body = "## Climate Resilience\nOfficials approved levee repairs.[1]"

        report_body = build_report_body(
            "Fixture Daily Brief",
            synthesis_body,
            [],
            [],
            citation_sources=citation_sources,
        )
        report_html = build_report_html(
            "reader@example.com",
            "Reader Example",
            "Fixture Daily Brief",
            synthesis_body,
            [],
            [],
            citation_sources=citation_sources,
        )

        self.assertIn("SOURCES", report_body)
        self.assertNotIn("ARTICLES BY SOURCE", report_body)
        self.assertIn("[1] Levee repairs approved", report_body)
        self.assertIn('href="https://example.com/levee"', report_html)
        self.assertIn('id="source-1"', report_html)


if __name__ == "__main__":
    unittest.main()
