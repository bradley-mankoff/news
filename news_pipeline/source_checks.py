"""Connectivity diagnostics for configured news sources."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from .config import CONFIG_DIR


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _source_rows(path: Path) -> list[dict[str, Any]]:
    payload = _load_yaml(path)
    rows: list[dict[str, Any]] = []

    records = payload.get("sources", [])
    if not isinstance(records, list):
        return rows
    for record in records:
        if not isinstance(record, dict):
            continue
        key = str(record.get("key") or record.get("name") or "").strip()
        url = str(record.get("url") or "").strip()
        if not key or not url:
            continue
        rows.append(
            {
                "section": "sources",
                "key": key,
                "name": str(record.get("name") or key),
                "url": url,
                "fetcher": str(record.get("fetcher") or "rss").strip().lower(),
            }
        )
    return rows


def _count_items(content: bytes, fetcher: str) -> tuple[int, str]:
    if fetcher in {"reddit", "reddit_top", "reddit_top_json"}:
        data = json.loads(content)
        return len(data.get("data", {}).get("children", [])), "json"

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        data = json.loads(content)
        return len(data.get("data", {}).get("children", [])), "json"

    root_tag = root.tag.lower()
    if "rss" in root_tag or root.tag == "rss":
        return len(root.findall(".//item")), "rss"
    if "feed" in root_tag or "atom" in root_tag:
        return len(root.findall(".//{http://www.w3.org/2005/Atom}entry")), "atom"
    return len(root.findall(".//item") + root.findall(".//entry")), "xml"


def probe_source(source: dict[str, Any], timeout: int) -> dict[str, Any]:
    started_at = time.monotonic()
    result: dict[str, Any] = {
        "key": source["key"],
        "name": source["name"],
        "section": source["section"],
        "url": source["url"],
        "ok": False,
        "http_status": None,
        "latency_ms": None,
        "format": None,
        "item_count": None,
        "error": None,
    }

    try:
        request = urllib.request.Request(source["url"], headers=HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
            result["http_status"] = response.status
        result["latency_ms"] = int((time.monotonic() - started_at) * 1000)
        item_count, feed_format = _count_items(content, source["fetcher"])
        result.update(ok=True, format=feed_format, item_count=item_count)
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

    return result


def _status(result: dict[str, Any]) -> str:
    if result["ok"]:
        return "OK"
    if result["http_status"]:
        return f"HTTP {result['http_status']}"
    return "FAIL"


def print_table(results: list[dict[str, Any]]) -> None:
    if not results:
        return
    widths = {
        "section": 10,
        "key": min(max(len(row["key"]) for row in results), 28),
        "name": min(max(len(row["name"]) for row in results), 30),
    }
    print()
    print(
        f"  {'STATUS':<8}  {'SECTION':<10}  {'KEY':<{widths['key']}}  "
        f"{'NAME':<{widths['name']}}  {'LATENCY':>8}  {'ITEMS':>5}  FORMAT  ERROR"
    )
    print("  " + "-" * 108)
    for row in results:
        latency = f"{row['latency_ms']}ms" if row["latency_ms"] is not None else "-"
        items = str(row["item_count"]) if row["item_count"] is not None else "-"
        name = row["name"][: widths["name"]]
        key = row["key"][: widths["key"]]
        print(
            f"  {_status(row):<8}  {row['section']:<10}  {key:<{widths['key']}}  "
            f"{name:<{widths['name']}}  {latency:>8}  {items:>5}  "
            f"{row['format'] or '-':<6}  {row['error'] or ''}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check configured news source connectivity.")
    parser.add_argument(
        "--sources-yaml",
        type=Path,
        default=CONFIG_DIR / "sources.yaml",
        help="Path to sources.yaml.",
    )
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout per source.")
    parser.add_argument("--only-failures", action="store_true", help="Print only failed sources.")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--section",
        choices=["sources", "all"],
        default="all",
        help="Which source section to check.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.sources_yaml.exists():
        print(f"ERROR: {args.sources_yaml} not found.", file=sys.stderr)
        return 2

    sources = _source_rows(args.sources_yaml)
    section = args.section.replace("-", "_")
    if section != "all":
        sources = [source for source in sources if source["section"] == section]
    if not sources:
        print("No sources to check.", file=sys.stderr)
        return 2

    if not args.json_output:
        print(f"Checking {len(sources)} source(s) from {args.sources_yaml} (timeout={args.timeout}s)...")
    results = [probe_source(source, args.timeout) for source in sources]

    if args.json_output:
        print(json.dumps(results, indent=2))
    else:
        display = [result for result in results if not args.only_failures or not result["ok"]]
        if display:
            print_table(display)
        else:
            print()
            print("  All sources passed.")
        passed = sum(1 for result in results if result["ok"])
        zero_items = sum(1 for result in results if result["ok"] and (result["item_count"] or 0) == 0)
        summary = f"  {passed}/{len(results)} sources OK"
        if zero_items:
            summary += f", {zero_items} returned 0 items"
        print()
        print(summary)

    return 1 if any(not result["ok"] for result in results) else 0
