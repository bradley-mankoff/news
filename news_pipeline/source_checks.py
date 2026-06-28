"""Source diagnostics and metadata helpers for configured news sources."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .config import ROOT_DIR
from .source_catalog import (
    DeleteSources,
    apply_source_catalog_patch,
    load_source_rows,
)
from .feed_utils import (
    decode_google_news_article_path as _decode_google_news_article_path,
    google_news_query_target as _google_news_query_target,
    is_google_news_url as _is_google_news_url,
    parse_feed_datetime as _parse_feed_datetime,
    resolve_google_news_url as _resolve_google_news_url,
)

RECENT_SOURCE_WINDOW_DAYS = 7
ARTICLE_PROBE_SAMPLE_SIZE = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*",
    "Accept-Encoding": "gzip, deflate",
}
ARTICLE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
}


def _default_sources_yaml() -> Path:
    return ROOT_DIR / (os.environ.get("NEWS_SOURCES_YAML") or "config/sources.yaml")


def _source_rows(path: Path) -> list[dict[str, Any]]:
    return load_source_rows(path)


def _decompress_response_body(content: bytes, content_encoding: str) -> bytes:
    clean_encoding = (content_encoding or "").lower()
    if "gzip" in clean_encoding or content.startswith(b"\x1f\x8b"):
        return gzip.decompress(content)
    if "deflate" in clean_encoding:
        return zlib.decompress(content)
    return content


def _fetch_url_once(
    url: str,
    timeout: int,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read()
        content_encoding = response.headers.get("Content-Encoding", "")
        return _decompress_response_body(content, content_encoding), int(response.status)


def _fetch_url(
    url: str,
    timeout: int,
    *,
    retries: int = 1,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, int]:
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return _fetch_url_once(url, timeout, headers=headers)
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError(str(last_error or "fetch failed"))


def _recent_probe_url(url: str, recent_days: int) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.lower() != "news.google.com" or not parts.path.startswith("/rss/search"):
        return url

    query_items = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    updated_items: list[tuple[str, str]] = []
    changed = False
    for key, value in query_items:
        if key != "q":
            updated_items.append((key, value))
            continue
        updated_value = re.sub(
            r"\bwhen:\S+",
            f"when:{recent_days}d",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        if updated_value == value:
            updated_value = f"when:{recent_days}d {value}".strip()
        updated_items.append((key, updated_value))
        changed = True

    if not changed:
        updated_items.append(("q", f"when:{recent_days}d"))

    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(updated_items))
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_unix_datetime(raw_value: Any) -> datetime | None:
    if raw_value is None or isinstance(raw_value, bool):
        return None
    try:
        timestamp = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 1_000_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _format_feed_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc_datetime(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_record_datetime(record: dict[str, Any]) -> datetime | None:
    timestamp_fields = ("created_utc", "created", "created_at_utc")
    date_fields = (
        "date_published",
        "date_modified",
        "published",
        "published_at",
        "updated",
        "updated_at",
        "pubDate",
        "pub_date",
    )
    for field in timestamp_fields:
        parsed = _parse_unix_datetime(record.get(field))
        if parsed is not None:
            return parsed
    for field in date_fields:
        parsed = _parse_feed_datetime(record.get(field))
        if parsed is not None:
            return parsed
    return None


def _json_item_datetimes(content: bytes) -> list[datetime | None]:
    data = json.loads(content)
    dates: list[datetime | None] = []

    def add_records(records: Any) -> None:
        if not isinstance(records, list):
            return
        for item in records:
            if not isinstance(item, dict):
                continue
            record = item.get("data") if isinstance(item.get("data"), dict) else item
            dates.append(_json_record_datetime(record))

    if isinstance(data, dict):
        nested_data = data.get("data")
        reddit_children = nested_data.get("children", []) if isinstance(nested_data, dict) else []
        add_records(reddit_children)
        add_records(data.get("items", []))
        if not dates:
            for field in ("entries", "articles", "results"):
                add_records(data.get(field, []))
        if not dates and isinstance(nested_data, list):
            add_records(nested_data)
    elif isinstance(data, list):
        add_records(data)

    return dates


def _xml_feed_format(root: ElementTree.Element) -> str:
    root_tag = _local_xml_name(root.tag)
    if "rss" in root_tag or root_tag == "rss":
        return "rss"
    if "feed" in root_tag or "atom" in root_tag:
        return "atom"
    return "xml"


def _xml_item_datetime(node: ElementTree.Element) -> datetime | None:
    date_fields = ("pubdate", "published", "updated", "date", "created", "modified", "issued")
    values_by_field: dict[str, str] = {}
    for child in list(node):
        field = _local_xml_name(child.tag)
        if field in date_fields:
            values_by_field.setdefault(field, " ".join(child.itertext()).strip())
    for field in date_fields:
        parsed = _parse_feed_datetime(values_by_field.get(field))
        if parsed is not None:
            return parsed
    return None


def _xml_item_datetimes(content: bytes) -> tuple[list[datetime | None], str]:
    root = _xml_root_from_content(content)
    dates = [
        _xml_item_datetime(node)
        for node in root.iter()
        if _local_xml_name(node.tag) in {"item", "entry"}
    ]
    return dates, _xml_feed_format(root)


def _item_datetimes(content: bytes, fetcher: str) -> tuple[list[datetime | None], str]:
    if fetcher in {"reddit", "reddit_top", "reddit_top_json"}:
        return _json_item_datetimes(content), "json"

    try:
        return _xml_item_datetimes(content)
    except ElementTree.ParseError:
        return _json_item_datetimes(content), "json"


def _summarize_items(
    content: bytes,
    fetcher: str,
    *,
    recent_days: int,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    item_datetimes, feed_format = _item_datetimes(content, fetcher)
    dated_items = [value for value in item_datetimes if value is not None]
    cutoff = (now_utc or datetime.now(timezone.utc)) - timedelta(days=recent_days)
    recent_items = [value for value in dated_items if value >= cutoff]
    newest_item = max(dated_items) if dated_items else None
    return {
        "format": feed_format,
        "item_count": len(item_datetimes),
        "recent_item_count": len(recent_items),
        "undated_item_count": len(item_datetimes) - len(dated_items),
        "newest_item_at": _format_feed_datetime(newest_item),
    }

def probe_source(
    source: dict[str, Any],
    timeout: int,
    *,
    recent_days: int = RECENT_SOURCE_WINDOW_DAYS,
    probe_articles: int = 0,
) -> dict[str, Any]:
    started_at = time.monotonic()
    result: dict[str, Any] = {
        "key": source["key"],
        "name": source["name"],
        "section": source["section"],
        "url": source["url"],
        "probe_url": None,
        "ok": False,
        "http_status": None,
        "latency_ms": None,
        "format": None,
        "item_count": None,
        "recent_item_count": None,
        "undated_item_count": None,
        "newest_item_at": None,
        "recent_days": recent_days,
        "stale": False,
        "error": None,
        "article_probe_count": None,
        "article_probe_successes": None,
    }

    content: bytes | None = None
    try:
        probe_url = _recent_probe_url(source["url"], recent_days)
        if probe_url != source["url"]:
            result["probe_url"] = probe_url
        content, status = _fetch_url(probe_url, timeout)
        result["http_status"] = status
        result["latency_ms"] = int((time.monotonic() - started_at) * 1000)
        item_summary = _summarize_items(content, source["fetcher"], recent_days=recent_days)
        result.update(item_summary)
        item_count = int(item_summary["item_count"])
        recent_item_count = int(item_summary["recent_item_count"])
        if item_count <= 0:
            result["stale"] = True
            result["error"] = "Feed parsed but returned 0 items."
        elif recent_item_count <= 0:
            result["stale"] = True
            newest_item_at = item_summary.get("newest_item_at")
            undated_item_count = int(item_summary.get("undated_item_count") or 0)
            if newest_item_at:
                result["error"] = (
                    f"No feed items dated within last {recent_days} day(s); "
                    f"newest is {newest_item_at}."
                )
            elif undated_item_count:
                result["error"] = "Feed items had no parseable publish/update dates."
            else:
                result["error"] = f"No feed items dated within last {recent_days} day(s)."
        else:
            result["ok"] = True
    except urllib.error.HTTPError as error:
        result["http_status"] = error.code
        result["error"] = f"HTTP {error.code} {error.reason}"
    except urllib.error.URLError as error:
        result["error"] = f"URLError: {error.reason}"
    except TimeoutError:
        result["error"] = f"Timed out after {timeout}s"
    except Exception as error:
        result["error"] = str(error)
    finally:
        if result["latency_ms"] is None:
            result["latency_ms"] = int((time.monotonic() - started_at) * 1000)

    if probe_articles > 0 and content is not None and not result.get("stale"):
        urls = _extract_feed_article_urls(content, source["fetcher"], probe_articles)
        successes = 0
        for url in urls:
            has_body, _status = _probe_article_body(url, timeout)
            if has_body:
                successes += 1
        result["article_probe_count"] = len(urls)
        result["article_probe_successes"] = successes

    return result


def _local_xml_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _xml_root_from_content(content: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as first_error:
        for encoding in ("utf-8-sig", "iso-8859-1", "windows-1252"):
            try:
                return ElementTree.fromstring(content.decode(encoding))
            except (ElementTree.ParseError, UnicodeDecodeError):
                continue
        raise first_error


def _xml_feed_article_urls(content: bytes, max_urls: int) -> list[str]:
    root = _xml_root_from_content(content)
    urls: list[str] = []
    for node in root.iter():
        if _local_xml_name(node.tag) not in {"item", "entry"}:
            continue
        url = ""
        for child in list(node):
            name = _local_xml_name(child.tag)
            if name == "link":
                href = child.get("href", "").strip()
                url = href or (child.text or "").strip()
                if url:
                    break
        if not url:
            for child in list(node):
                if _local_xml_name(child.tag) == "guid":
                    candidate = (child.text or "").strip()
                    if candidate.startswith("http"):
                        url = candidate
                    break
        if url:
            urls.append(url)
        if len(urls) >= max_urls:
            break
    return urls


def _json_feed_article_urls(content: bytes, max_urls: int) -> list[str]:
    data = json.loads(content)
    urls: list[str] = []
    children = []
    if isinstance(data, dict):
        children = data.get("data", {}).get("children", [])
        if not isinstance(children, list):
            children = []
    for child in children:
        record = child.get("data") if isinstance(child, dict) else None
        if not isinstance(record, dict):
            continue
        url = str(record.get("url") or "").strip()
        if url and url.startswith("http"):
            urls.append(url)
        if len(urls) >= max_urls:
            break
    return urls


def _extract_feed_article_urls(content: bytes, fetcher: str, max_urls: int) -> list[str]:
    if fetcher in {"reddit", "reddit_top", "reddit_top_json"}:
        return _json_feed_article_urls(content, max_urls)
    try:
        return _xml_feed_article_urls(content, max_urls)
    except Exception:
        return _json_feed_article_urls(content, max_urls)


def _probe_article_body(url: str, timeout: int) -> tuple[bool, str]:
    """Returns (has_real_body, status_label)."""
    try:
        article_url = _resolve_google_news_url(url)
        content, _status = _fetch_url(article_url, timeout, headers=ARTICLE_HEADERS)
        try:
            import trafilatura  # type: ignore
            text = trafilatura.extract(
                content.decode("utf-8", errors="replace"),
                url=article_url,
            )
        except ImportError:
            return False, "trafilatura_not_installed"
        except Exception:
            return False, "trafilatura_error"
        return bool(text and text.strip()), "scraped" if (text and text.strip()) else "no_text"
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return False, f"url_error: {exc.reason}"
    except TimeoutError:
        return False, f"timeout_{timeout}s"
    except Exception as exc:
        return False, f"error: {exc!s}"


def remove_source_blocks(path: Path, keys: set[str]) -> int:
    result = apply_source_catalog_patch(path, [DeleteSources(keys)])
    return result.edit_count


def _status(result: dict[str, Any]) -> str:
    if result.get("stale"):
        return "STALE"
    if result["ok"]:
        return "OK"
    if result["http_status"]:
        return f"HTTP {result['http_status']}"
    return "FAIL"


def print_table(results: list[dict[str, Any]]) -> None:
    if not results:
        return
    show_scrape = any(row.get("article_probe_count") is not None for row in results)
    widths = {
        "section": 10,
        "key": min(max(len(row["key"]) for row in results), 28),
        "name": min(max(len(row["name"]) for row in results), 30),
    }
    scrape_header = f"  {'SCRAPE':>6}" if show_scrape else ""
    print()
    print(
        f"  {'STATUS':<8}  {'SECTION':<10}  {'KEY':<{widths['key']}}  "
        f"{'NAME':<{widths['name']}}  {'LATENCY':>8}  {'ITEMS':>5}  "
        f"{'RECENT':>6}  {'NEWEST':<20}  FORMAT{scrape_header}  ERROR"
    )
    print("  " + "-" * (136 + (9 if show_scrape else 0)))
    for row in results:
        latency = f"{row['latency_ms']}ms" if row["latency_ms"] is not None else "-"
        items = str(row["item_count"]) if row["item_count"] is not None else "-"
        recent = str(row.get("recent_item_count")) if row.get("recent_item_count") is not None else "-"
        newest = str(row.get("newest_item_at") or "-")[:20]
        name = row["name"][: widths["name"]]
        key = row["key"][: widths["key"]]
        scrape_col = ""
        if show_scrape:
            probe_count = row.get("article_probe_count")
            probe_ok = row.get("article_probe_successes")
            scrape_col = f"  {f'{probe_ok}/{probe_count}' if probe_count is not None else '-':>6}"
        print(
            f"  {_status(row):<8}  {row['section']:<10}  {key:<{widths['key']}}  "
            f"{name:<{widths['name']}}  {latency:>8}  {items:>5}  "
            f"{recent:>6}  {newest:<20}  "
            f"{row['format'] or '-':<6}{scrape_col}  {row['error'] or ''}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check configured news sources.")
    parser.add_argument(
        "--sources-yaml",
        type=Path,
        default=_default_sources_yaml(),
        help=(
            "Path to a sources YAML file. Defaults to NEWS_SOURCES_YAML, "
            "then config/sources.yaml."
        ),
    )
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout per source.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=12,
        help="Maximum concurrent source probes for connectivity checks.",
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=RECENT_SOURCE_WINDOW_DAYS,
        help=(
            "Require at least one feed item dated within this many days. "
            f"Defaults to {RECENT_SOURCE_WINDOW_DAYS}."
        ),
    )
    parser.add_argument(
        "--prune-inactive",
        "--prune-stale",
        action="store_true",
        help=(
            "Remove source entries from the YAML when the feed parses but has "
            "0 articles in the recent-days window."
        ),
    )
    parser.add_argument(
        "--probe-articles",
        action="store_true",
        help=(
            f"For each active source, fetch up to {ARTICLE_PROBE_SAMPLE_SIZE} article URLs from "
            "the feed and verify that trafilatura can extract real body text. "
            "Adds a SCRAPE column to the output table."
        ),
    )
    parser.add_argument(
        "--prune-unscrapable",
        action="store_true",
        help=(
            "Remove source entries from the YAML when ALL probed article bodies were empty "
            "(requires --probe-articles). Use this for monthly source hygiene."
        ),
    )
    parser.add_argument("--only-failures", action="store_true", help="Print only failed sources.")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of sources checked.")
    parser.add_argument(
        "--section",
        choices=["sources", "all"],
        default="all",
        help="Which source section to check.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON.")
    return parser


def _probe_sources(
    sources: list[dict[str, Any]],
    *,
    timeout: int,
    concurrency: int,
    recent_days: int,
    probe_articles: int = 0,
) -> list[dict[str, Any]]:
    worker_count = max(1, min(max(1, concurrency), len(sources)))
    if worker_count == 1:
        return [
            probe_source(source, timeout, recent_days=recent_days, probe_articles=probe_articles)
            for source in sources
        ]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                probe_source, source, timeout, recent_days=recent_days, probe_articles=probe_articles
            )
            for source in sources
        ]
        return [future.result() for future in futures]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.sources_yaml.exists():
        print(f"ERROR: {args.sources_yaml} not found.", file=sys.stderr)
        return 2
    if args.recent_days < 1:
        print("ERROR: --recent-days must be at least 1.", file=sys.stderr)
        return 2

    sources = _source_rows(args.sources_yaml)
    section = args.section.replace("-", "_")
    if section != "all":
        sources = [source for source in sources if source["section"] == section]
    if not sources:
        print("No sources to check.", file=sys.stderr)
        return 2

    if args.limit is not None:
        sources = sources[: max(0, args.limit)]
        if not sources:
            print("No sources to check.", file=sys.stderr)
            return 2

    probe_articles_count = ARTICLE_PROBE_SAMPLE_SIZE if args.probe_articles else 0

    if args.prune_unscrapable and not args.probe_articles:
        print("ERROR: --prune-unscrapable requires --probe-articles.", file=sys.stderr)
        return 2

    if not args.json_output:
        probe_note = f", probe-articles={probe_articles_count}" if probe_articles_count else ""
        print(
            f"Checking {len(sources)} source(s) from {args.sources_yaml} "
            f"(timeout={args.timeout}s, concurrency={args.concurrency}, "
            f"recent_days={args.recent_days}{probe_note})..."
        )
    results = _probe_sources(
        sources,
        timeout=args.timeout,
        concurrency=args.concurrency,
        recent_days=args.recent_days,
        probe_articles=probe_articles_count,
    )

    stale_keys = {str(result["key"]) for result in results if result.get("stale")}
    unscrapable_keys = {
        str(result["key"])
        for result in results
        if result.get("article_probe_count") and result.get("article_probe_successes") == 0
    }

    pruned = 0
    pruned_unscrapable = 0
    if args.prune_inactive:
        pruned = remove_source_blocks(args.sources_yaml, stale_keys)
    if args.prune_unscrapable:
        pruned_unscrapable = remove_source_blocks(args.sources_yaml, unscrapable_keys)

    if args.json_output:
        payload: dict[str, Any] = {"results": results}
        if args.prune_inactive:
            payload["pruned_inactive"] = pruned
            payload["pruned_inactive_keys"] = sorted(stale_keys)
        if args.prune_unscrapable:
            payload["pruned_unscrapable"] = pruned_unscrapable
            payload["pruned_unscrapable_keys"] = sorted(unscrapable_keys)
        print(json.dumps(payload, indent=2))
    else:
        display = [result for result in results if not args.only_failures or not result["ok"]]
        if display:
            print_table(display)
        else:
            print()
            print("  All sources passed.")
        active = sum(1 for result in results if result["ok"])
        stale = sum(1 for result in results if result.get("stale"))
        failed = sum(1 for result in results if not result["ok"] and not result.get("stale"))
        summary = f"  {active}/{len(results)} sources active"
        if stale:
            summary += f", {stale} inactive (0 articles in last {args.recent_days} days)"
        if failed:
            summary += f", {failed} failed"
        if probe_articles_count:
            unscrapable_count = len(unscrapable_keys)
            summary += f", {unscrapable_count} unscrapable (0/{probe_articles_count} bodies)"
        if args.prune_inactive:
            summary += f", {pruned} pruned (inactive)"
        if args.prune_unscrapable:
            summary += f", {pruned_unscrapable} pruned (unscrapable)"
        print()
        print(summary)

    if args.prune_inactive:
        return 1 if any(not result["ok"] and not result.get("stale") for result in results) else 0
    return 1 if any(not result["ok"] for result in results) else 0
