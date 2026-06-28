from __future__ import annotations

import unittest

from news_pipeline.text_cleaning import (
    clean_article_text,
    clean_content_text,
    clean_feed_url,
    strip_model_artifacts,
)
from news_pipeline.text_matching import (
    clean_source_title,
    compact_dotted_acronyms,
    match_terms,
    normalize_match_token,
    normalize_text_key,
    ordered_match_terms,
)


class TextCleaningTests(unittest.TestCase):
    def test_clean_content_text_removes_markup_noise_and_urls(self) -> None:
        raw = '<script>bad()</script><p class="lead">Hello&nbsp;world https://example.com</p>{junk}'

        self.assertEqual(clean_content_text(raw), "Hello world")

    def test_clean_article_text_strips_yonhap_furniture(self) -> None:
        raw = (
            "Yonhap News Agency Politics 12:30 June 1, 2026 "
            "SHARE Facebook X Pinterest Linked in Tumblr Reddit Facebook Messenger Copy URL URL is copied. OK "
            "Officials announced the plan today. (END) Keywords ignored"
        )

        self.assertEqual(
            clean_article_text(raw, source="Yonhap"),
            "Officials announced the plan today.",
        )

    def test_clean_feed_url_and_model_artifacts(self) -> None:
        self.assertEqual(clean_feed_url(" https://example.com/a &amp; b "), "https://example.com/a&b")
        self.assertEqual(
            strip_model_artifacts("<think>hidden</think><|im_start|>&lt;analysis&gt;Visible&lt;/analysis&gt;"),
            "Visible",
        )


class TextMatchingTests(unittest.TestCase):
    def test_title_key_acronym_and_token_normalization(self) -> None:
        self.assertEqual(normalize_text_key("  Big Story!  "), "big_story")
        self.assertEqual(normalize_text_key("!!!"), "story")
        self.assertEqual(clean_source_title("Headline - Reuters"), "Headline")
        self.assertEqual(compact_dotted_acronyms("U.S. and E.U."), "US and EU")
        self.assertEqual(normalize_match_token("company's"), "company")
        self.assertEqual(normalize_match_token("parties"), "party")
        self.assertEqual(normalize_match_token("hits"), "hit")
        self.assertEqual(normalize_match_token("virus"), "virus")

    def test_ordered_match_terms_filters_noise_and_splits_hyphens(self) -> None:
        self.assertEqual(
            ordered_match_terms("The U.S. AI-chip reports hit markets", allowed_short_terms={"ai"}),
            ["ai-chip", "ai", "chip", "hit", "market"],
        )
        self.assertEqual(ordered_match_terms("go ai", allowed_short_terms={"ai"}), ["ai"])
        self.assertEqual(ordered_match_terms("AI", collect_short_terms=True), ["ai"])
        self.assertEqual(match_terms("Markets markets 123"), {"market"})


if __name__ == "__main__":
    unittest.main()
