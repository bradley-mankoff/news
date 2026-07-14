"""Shared feed and Google News mechanics."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlparse


PORTUGUESE_DATE_TOKEN_MAP = {
    "Dom": "Sun",
    "Seg": "Mon",
    "Ter": "Tue",
    "Qua": "Wed",
    "Qui": "Thu",
    "Sex": "Fri",
    "Sab": "Sat",
    "Sáb": "Sat",
    "Jan": "Jan",
    "Fev": "Feb",
    "Mar": "Mar",
    "Abr": "Apr",
    "Mai": "May",
    "Jun": "Jun",
    "Jul": "Jul",
    "Ago": "Aug",
    "Set": "Sep",
    "Out": "Oct",
    "Nov": "Nov",
    "Dez": "Dec",
}


def is_google_news_url(url: str | None) -> bool:
    raw_url = str(url or "").strip()
    if not raw_url:
        return False
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname == "news.google.com" or hostname.endswith(".news.google.com")


def google_news_query_target(url: str) -> str:
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except Exception:
        return ""
    for key in ("url", "u"):
        for value in query.get(key, []):
            candidate = unquote(str(value or "").strip())
            if candidate.startswith(("http://", "https://")) and not is_google_news_url(candidate):
                return candidate
    return ""


def decode_google_news_article_path(url: str) -> str:
    """Decode modern Google News RSS article URLs."""
    try:
        article_id = urlparse(url).path.rstrip("/").split("/")[-1]
        if not article_id:
            return ""

        decoded_bytes = base64.urlsafe_b64decode(article_id + "==")
        decoded_str = decoded_bytes.decode("latin1")

        prefix = b"\x08\x13\x22".decode("latin1")
        if decoded_str.startswith(prefix):
            decoded_str = decoded_str[len(prefix):]
        suffix = b"\xd2\x01\x00".decode("latin1")
        if decoded_str.endswith(suffix):
            decoded_str = decoded_str[: -len(suffix)]

        bytes_array = bytearray(decoded_str, "latin1")
        if not bytes_array:
            return ""
        length = bytes_array[0]
        # Short lengths use a one-byte length prefix, so the URL starts at offset 1.
        candidate = decoded_str[2 : length + 1] if length >= 0x80 else decoded_str[1 : length + 1]

        if candidate.startswith(("http://", "https://")) and not is_google_news_url(candidate):
            return candidate

        if candidate.startswith("AU_yqL"):
            try:
                from googlenewsdecoder import gnewsdecoder

                result = gnewsdecoder(url)
                if result.get("status"):
                    resolved = result.get("decoded_url", "")
                    if resolved and not is_google_news_url(resolved):
                        return resolved
            except Exception:
                pass
    except Exception:
        pass
    return ""


def resolve_google_news_url(url: str) -> str:
    original_url = str(url or "").strip()
    if not is_google_news_url(original_url):
        return original_url
    return (
        google_news_query_target(original_url)
        or decode_google_news_article_path(original_url)
        or original_url
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_feed_date_text(value: str) -> str:
    normalized = value
    for source_token, target_token in PORTUGUESE_DATE_TOKEN_MAP.items():
        normalized = re.sub(
            rf"(?<![A-Za-zÀ-ÿ]){re.escape(source_token)}(?![A-Za-zÀ-ÿ])",
            target_token,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def parse_feed_datetime(raw_value: str | None) -> datetime | None:
    value = str(raw_value or "").strip()
    if not value:
        return None

    for candidate in (value, _normalize_feed_date_text(value)):
        try:
            return _utc_datetime(parsedate_to_datetime(candidate))
        except Exception:
            pass

    iso_value = value.replace("Z", "+00:00")
    try:
        return _utc_datetime(datetime.fromisoformat(iso_value))
    except Exception:
        return None
