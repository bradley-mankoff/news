from __future__ import annotations

import unittest
from unittest.mock import patch

from news_pipeline.text_cleaning import (
    clean_article_text,
    clean_content_text,
    clean_feed_url,
    _collapse_text,
    _is_yonhap_article,
    _strip_markup_and_web_noise,
    _strip_yonhap_tail,
    _trim_yonhap_prefix,
    _yonhap_title_candidates,
    strip_model_artifacts,
)
from news_pipeline.text_matching import (
    clean_source_title,
    compact_dotted_acronyms,
    normalize_match_token,
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

    def test_internal_yonhap_and_markup_helpers_cover_fallback_branches(self) -> None:
        with patch("news_pipeline.text_cleaning.BeautifulSoup", side_effect=RuntimeError("boom")):
            self.assertEqual(
                _strip_markup_and_web_noise("<p>visible</p>", html_separator=" ").strip(),
                "visible",
            )

        self.assertEqual(
            _trim_yonhap_prefix(
                "Yonhap News Agency Politics 12:30 June 1, 2026 Officials announced the plan followed by more details.",
                title="Officials announced the plan",
            ),
            "followed by more details.",
        )
        self.assertEqual(
            _trim_yonhap_prefix(
                "Yonhap News Agency Politics 12:30 June 1, 2026 Officials announced the plan followed by more details.",
                title="Unrelated title",
            ),
            "Officials announced the plan followed by more details.",
        )
        self.assertEqual(
            _strip_yonhap_tail("This article has enough words for the tail Keywords ignored"),
            "This article has enough words for the tail",
        )
        self.assertEqual(
            _strip_yonhap_tail("One two three four five Keywords stay put"),
            "One two three four five Keywords stay put",
        )
        self.assertEqual(
            _strip_yonhap_tail("One two three four (END) trailing text"),
            "One two three four",
        )
        with patch("news_pipeline.text_cleaning.urlparse", side_effect=ValueError("boom")):
            self.assertFalse(_is_yonhap_article(source="Fixture Wire", url="https://example.com"))
        self.assertEqual(
            _yonhap_title_candidates("(URGENT) Flood update - Yonhap News Agency"),
            ["(URGENT) Flood update - Yonhap News Agency", "(URGENT) Flood update", "Flood update"],
        )
        self.assertEqual(_collapse_text("  spaced   text  "), "spaced text")


class TextMatchingTests(unittest.TestCase):
    def test_title_acronym_and_token_normalization(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
