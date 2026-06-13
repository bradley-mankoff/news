"""Text cleaning helpers for scraped articles and feed metadata."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


XML_DOCUMENT_RE = re.compile(
    r"^[\s\ufeff]*(?:<\?xml\b|<rss\b|<feed\b|<rdf(?::RDF)?\b|<channel\b|<item\b|<entry\b)",
    flags=re.IGNORECASE,
)

YONHAP_SOURCE_PREFIXES = ("yonhap",)
YONHAP_DOMAIN_SUFFIX = "yna.co.kr"

YONHAP_SECTION_NAMES = (
    "All News",
    "National",
    "Politics",
    "Diplomacy",
    "Defense",
    "North Korea",
    "Economy",
    "Economy/Finance",
    "Finance",
    "Health",
    "BIZ",
    "Latest News",
    "Culture",
    "Culture/K-pop",
    "Sports",
    "Images",
    "Videos",
    "Top News",
)
YONHAP_SECTION_PATTERN = "|".join(re.escape(section) for section in YONHAP_SECTION_NAMES)
YONHAP_LEADING_META_RE = re.compile(
    rf"^\s*(?:Yonhap News Agency\s+)?(?:[A-Z][A-Za-z .'-]{{2,60}}\s+)?"
    rf"(?:{YONHAP_SECTION_PATTERN})\s+"
    r"\d{1,2}:\d{2}\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s+",
    flags=re.IGNORECASE,
)
YONHAP_TAIL_PATTERNS = (
    r"\bKeywords\b",
    r"\bArticles with issue keywords\b",
    r"\bMost Liked\b",
    r"\bMost Saved\b",
    r"\bMost Viewed\b",
    r"\bMost Viewed Photos\b",
    r"\bMain Article Right Now\b",
    r"\bHOME\s+All News\b",
    r"\bCopyright\s+\(c\)\s+Yonhap News Agency\b",
    r"\bSend Feedback\s+Close\b",
)


def clean_content_text(text: str | None) -> str:
    """Normalize generic scraped/feed text before matching or summarization."""
    return _collapse_text(_strip_markup_and_web_noise(text, html_separator=" "))


def clean_article_text(
    text: str | None,
    *,
    source: str | None = None,
    url: str | None = None,
    title: str | None = None,
) -> str:
    """Normalize article text, including source-specific page furniture removal."""
    clean_text = _strip_markup_and_web_noise(text, html_separator="\n")
    if _is_yonhap_article(source=source, url=url):
        clean_text = _clean_yonhap_article_text(clean_text, title=title)
    return _collapse_text(clean_text)


def clean_feed_text(text: str | None) -> str:
    return clean_content_text(text)


def clean_feed_url(text: str | None) -> str:
    clean_url = html.unescape(str(text or "")).strip()
    clean_url = re.sub(r"\s+", "", clean_url)
    return clean_url


def _markup_parser_for_text(text: str) -> str:
    return "xml" if XML_DOCUMENT_RE.search(text) else "html.parser"


def _strip_markup_and_web_noise(text: str | None, *, html_separator: str) -> str:
    clean_text = html.unescape(str(text or ""))
    if not clean_text:
        return ""

    clean_text = re.sub(
        r"(?is)<(?:script|style|noscript)\b.*?</(?:script|style|noscript)>",
        " ",
        clean_text,
    )
    if "<" in clean_text and ">" in clean_text:
        try:
            soup = BeautifulSoup(clean_text, _markup_parser_for_text(clean_text))
            clean_text = soup.get_text(
                html_separator,
                strip=True,
            )
        except Exception:
            clean_text = re.sub(r"(?s)<[^>]+>", " ", clean_text)
    else:
        clean_text = re.sub(r"(?s)<[^>]+>", " ", clean_text)

    clean_text = re.sub(r"https?://[^\s<>)\"']+", " ", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"\bwww\.[^\s<>)\"']+", " ", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"\{[^{}]{0,800}\}", " ", clean_text)
    clean_text = re.sub(
        r"\b(?:background(?:-color)?|border(?:-[a-z]+)?|box-sizing|color|display|"
        r"font(?:-[a-z]+)?|height|letter-spacing|line-height|margin(?:-[a-z]+)?|"
        r"max-width|min-width|padding(?:-[a-z]+)?|text-align|text-decoration|"
        r"vertical-align|width)\s*:\s*[^;{}\n]+;?",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"\b(?:aria-[a-z-]+|class|data-[a-z-]+|href|rel|src|style|target)\s*=\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = clean_text.replace("_blank", " ")
    clean_text = re.sub(r"&[a-zA-Z0-9#]+;", " ", clean_text)
    clean_text = re.sub(r"\b\d+(?:px|em|rem|pt|vh|vw|%)\b", " ", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(
        r"\b(?:href|https?|rss|nbsp|font|target|blank|noopener|noreferrer)\b",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    return clean_text


def _clean_yonhap_article_text(text: str, *, title: str | None) -> str:
    clean_text = _collapse_text(text)
    clean_text = _trim_yonhap_prefix(clean_text, title=title)
    clean_text = _strip_yonhap_inline_furniture(clean_text)
    clean_text = _strip_yonhap_tail(clean_text)
    return clean_text


def _trim_yonhap_prefix(text: str, *, title: str | None) -> str:
    clean_text = text
    clean_title_candidates = _yonhap_title_candidates(title)
    lower_text = clean_text.lower()
    for candidate in clean_title_candidates:
        index = lower_text.find(candidate.lower())
        if index < 0:
            continue
        clean_text = clean_text[index + len(candidate) :]
        break

    previous = None
    while previous != clean_text:
        previous = clean_text
        clean_text = YONHAP_LEADING_META_RE.sub(" ", clean_text, count=1)
        clean_text = re.sub(
            r"^\s*(?:Yonhap News Agency|SHARE|LIKE SAVE PRINT|FONT SIZE|SIZE)\b",
            " ",
            clean_text,
            count=1,
            flags=re.IGNORECASE,
        )
        clean_text = _collapse_text(clean_text)
    return clean_text


def _strip_yonhap_inline_furniture(text: str) -> str:
    clean_text = text
    clean_text = re.sub(
        r"\bSHARE\s+Facebook X Pinterest Linked in Tumblr Reddit Facebook Messenger "
        r"Copy URL\s+URL is copied\.?\s*(?:OK)?",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"\bFacebook X Pinterest Linked in Tumblr Reddit Facebook Messenger "
        r"Copy URL\s+URL is copied\.?\s*(?:OK)?",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"\bCopy URL\s+URL is copied\.?\s*(?:OK)?",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(r"\bLIKE SAVE PRINT\b", " ", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(
        r"\b(?:FONT SIZE|SIZE)\b(?:\s+ABCDEFG){0,8}",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"(?:^|\s)(?:ABCDEFG\s+){2,}ABCDEFG(?=\s|$)",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    return _collapse_text(clean_text)


def _strip_yonhap_tail(text: str) -> str:
    clean_text = text
    end_match = re.search(r"\s+\(END\)(?:\s|$)", clean_text, flags=re.IGNORECASE)
    if end_match and _word_count(clean_text[: end_match.start()]) >= 3:
        return clean_text[: end_match.start()].strip()

    tail_start: int | None = None
    for pattern in YONHAP_TAIL_PATTERNS:
        match = re.search(pattern, clean_text, flags=re.IGNORECASE)
        if not match:
            continue
        if _word_count(clean_text[: match.start()]) < 8:
            continue
        tail_start = match.start() if tail_start is None else min(tail_start, match.start())
    if tail_start is not None:
        clean_text = clean_text[:tail_start]
    return clean_text.strip()


def _is_yonhap_article(*, source: str | None, url: str | None) -> bool:
    clean_source = _collapse_text(source).lower()
    if any(clean_source.startswith(prefix) for prefix in YONHAP_SOURCE_PREFIXES):
        return True

    try:
        hostname = (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        hostname = ""
    return hostname == YONHAP_DOMAIN_SUFFIX or hostname.endswith("." + YONHAP_DOMAIN_SUFFIX)


def _yonhap_title_candidates(title: str | None) -> list[str]:
    clean_title = _collapse_text(title)
    if not clean_title:
        return []

    candidates = [clean_title]
    without_source = re.sub(
        r"\s*(?:[-|]\s*)?Yonhap News Agency\s*$",
        "",
        clean_title,
        flags=re.IGNORECASE,
    ).strip()
    if without_source and without_source not in candidates:
        candidates.append(without_source)

    without_update_label = re.sub(
        r"^\((?:URGENT|LEAD|\d+(?:st|nd|rd|th)\s+LD)\)\s+",
        "",
        without_source or clean_title,
        flags=re.IGNORECASE,
    ).strip()
    if without_update_label and without_update_label not in candidates:
        candidates.append(without_update_label)

    return candidates


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _collapse_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
