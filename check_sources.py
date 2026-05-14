#!/usr/bin/env python3
"""check_sources.py — Connectivity diagnostics for all configured news sources.

Tests every feed in config/sources.yaml (both top_funnel_providers and sources)
by fetching each URL and attempting to parse it as RSS/Atom or JSON. Prints a
summary table and exits with a non-zero status if any source fails.

Usage:
    uv run check_sources.py
    uv run check_sources.py --sources-yaml config/sources.yaml --timeout 15
    uv run check_sources.py --only-failures
    uv run check_sources.py --section top_funnel
    uv run check_sources.py --section sources
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import urllib.request
import urllib.error


ROOT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# YAML loading (minimal, avoids importing the full pipeline package)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        sys.exit("PyYAML not found. Run: pip install pyyaml --break-system-packages")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_sources(sources_yaml: Path) -> list[dict[str, Any]]:
    payload = _load_yaml(sources_yaml)
    results: list[dict[str, Any]] = []

    for provider in payload.get("top_funnel_providers", []):
        if not isinstance(provider, dict):
            continue
        key = str(provider.get("key") or provider.get("name") or "").strip()
        url = str(provider.get("url") or "").strip()
        if not key or not url:
            continue
        results.append({
            "section": "top_funnel",
            "key": key,
            "name": str(provider.get("name") or key),
            "url": url,
            "fetcher": str(provider.get("fetcher") or "rss"),
        })

    for source in payload.get("sources", []):
        if not isinstance(source, dict):
            continue
        key = str(source.get("key") or source.get("name") or "").strip()
        url = str(source.get("url") or "").strip()
        if not key or not url:
            continue
        results.append({
            "section": "sources",
            "key": key,
            "name": str(source.get("name") or key),
            "url": url,
            "fetcher": str(source.get("fetcher") or "rss"),
        })

    return results


# ---------------------------------------------------------------------------
# Feed probing
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*",
}

# RSS/Atom namespaces
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss": "",
}

_RSS_ITEM_TAGS = {
    "item",  # RSS 2.0
    "{http://www.w3.org/2005/Atom}entry",  # Atom
}


def _count_feed_items(content: bytes, fetcher: str) -> tuple[int, str]:
    """Return (item_count, format_string). Raises ValueError on parse failure."""
    if fetcher == "reddit_top_json":
        try:
            data = json.loads(content)
            posts = data.get("data", {}).get("children", [])
            return len(posts), "json"
        except Exception as exc:
            raise ValueError(f"JSON parse failed: {exc}") from exc

    # Try XML (RSS / Atom)
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        # Maybe it's JSON anyway
        try:
            data = json.loads(content)
            posts = data.get("data", {}).get("children", [])
            return len(posts), "json"
        except Exception:
            raise ValueError(f"XML parse failed: {exc}") from exc

    tag = root.tag.lower()
    if "rss" in tag or root.tag == "rss":
        items = root.findall(".//item")
        return len(items), "rss"
    if "feed" in tag or "atom" in tag.lower():
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        return len(items), "atom"
    # Generic fallback: count <item> or <entry> anywhere
    items = root.findall(".//item") + root.findall(".//entry")
    fmt = "xml"
    return len(items), fmt


def probe_source(source: dict[str, Any], timeout: int) -> dict[str, Any]:
    url = source["url"]
    fetcher = source["fetcher"]
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "key": source["key"],
        "name": source["name"],
        "section": source["section"],
        "url": url,
        "ok": False,
        "http_status": None,
        "latency_ms": None,
        "format": None,
        "item_count": None,
        "error": None,
    }

    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            result["http_status"] = resp.status
            result["latency_ms"] = latency_ms
            content = resp.read()

        item_count, fmt = _count_feed_items(content, fetcher)
        result["ok"] = True
        result["format"] = fmt
        result["item_count"] = item_count

    except urllib.error.HTTPError as exc:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["http_status"] = exc.code
        result["error"] = f"HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        reason = str(exc.reason)
        result["error"] = f"URLError: {reason}"
    except TimeoutError:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["error"] = f"Timed out after {timeout}s"
    except Exception as exc:
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _color(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{_RESET}" if use_color else text


def _status_cell(result: dict[str, Any], use_color: bool) -> str:
    if result["ok"]:
        return _color("OK", _GREEN, use_color)
    if result["http_status"] and result["http_status"] >= 400:
        return _color(f"HTTP {result['http_status']}", _RED, use_color)
    return _color("FAIL", _RED, use_color)


def print_table(results: list[dict[str, Any]], use_color: bool) -> None:
    col_widths = {
        "status": 10,
        "section": 10,
        "key": max(len(r["key"]) for r in results),
        "name": min(max(len(r["name"]) for r in results), 32),
        "latency": 9,
        "items": 7,
        "format": 6,
        "error": 40,
    }

    def row(status: str, section: str, key: str, name: str, latency: str,
            items: str, fmt: str, error: str) -> str:
        return (
            f"  {status:<{col_widths['status']}}"
            f"  {section:<{col_widths['section']}}"
            f"  {key:<{col_widths['key']}}"
            f"  {name:<{col_widths['name']}}"
            f"  {latency:>{col_widths['latency']}}"
            f"  {items:>{col_widths['items']}}"
            f"  {fmt:<{col_widths['format']}}"
            f"  {error}"
        )

    header = row("STATUS", "SECTION", "KEY", "NAME", "LATENCY", "ITEMS", "FORMAT", "ERROR/NOTE")
    sep = "  " + "-" * (len(header) - 2)

    print()
    print(_color(header, _BOLD, use_color))
    print(sep)

    prev_section = None
    for r in results:
        if r["section"] != prev_section and prev_section is not None:
            print()
        prev_section = r["section"]

        status_str = _status_cell(r, use_color)
        latency_str = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "-"
        items_str = str(r["item_count"]) if r["item_count"] is not None else "-"
        fmt_str = r["format"] or "-"
        error_str = r["error"] or ""
        name_str = r["name"][:col_widths["name"]]

        line = row(status_str, r["section"], r["key"], name_str,
                   latency_str, items_str, fmt_str, error_str)
        if not r["ok"] and use_color:
            line = _color(line, _RED, use_color)
        elif r["item_count"] == 0 and use_color:
            line = _color(line, _YELLOW, use_color)
        print(line)

    print(sep)


def print_summary(results: list[dict[str, Any]], use_color: bool) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    failed = total - passed
    zero_items = sum(1 for r in results if r["ok"] and (r["item_count"] or 0) == 0)

    print()
    summary = f"  {passed}/{total} sources OK"
    if failed:
        summary += f", {failed} FAILED"
    if zero_items:
        summary += f", {zero_items} returned 0 items (check feed URL)"
    if failed or zero_items:
        print(_color(summary, _RED, use_color))
    else:
        print(_color(summary, _GREEN, use_color))
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check connectivity to all configured news sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sources-yaml",
        type=Path,
        default=ROOT_DIR / "config" / "sources.yaml",
        help="Path to sources.yaml (default: config/sources.yaml)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout per source in seconds (default: 20)",
    )
    parser.add_argument(
        "--only-failures",
        action="store_true",
        help="Print only sources that failed",
    )
    parser.add_argument(
        "--section",
        choices=["top_funnel", "sources", "all"],
        default="all",
        help="Which section to check: top_funnel, sources, or all (default: all)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print results as JSON instead of a table",
    )
    args = parser.parse_args()

    use_color = not args.no_color and sys.stdout.isatty()

    sources_yaml: Path = args.sources_yaml
    if not sources_yaml.exists():
        print(f"ERROR: {sources_yaml} not found.", file=sys.stderr)
        return 2

    all_sources = _load_sources(sources_yaml)
    if args.section != "all":
        all_sources = [s for s in all_sources if s["section"] == args.section]

    if not all_sources:
        print("No sources to check.", file=sys.stderr)
        return 2

    print(f"Checking {len(all_sources)} source(s) from {sources_yaml} "
          f"(timeout={args.timeout}s)…")

    results: list[dict[str, Any]] = []
    for i, source in enumerate(all_sources, 1):
        label = source["name"]
        print(f"  [{i:2d}/{len(all_sources)}] {label[:50]}…", end="\r", flush=True)
        results.append(probe_source(source, timeout=args.timeout))
    # Clear the progress line
    print(" " * 70, end="\r", flush=True)

    if args.json_output:
        print(json.dumps(results, indent=2))
        failures = [r for r in results if not r["ok"]]
        return 1 if failures else 0

    display = [r for r in results if not args.only_failures or not r["ok"]]
    if display:
        print_table(display, use_color)
    else:
        print()
        print(_color("  All sources passed.", _GREEN, use_color))

    print_summary(results, use_color)

    failures = [r for r in results if not r["ok"]]
    zero_item_warnings = [r for r in results if r["ok"] and (r["item_count"] or 0) == 0]

    if failures:
        print(_color("Failed sources:", _RED, use_color))
        for r in failures:
            print(f"  - {r['key']}: {r['error']}")
        print()

    if zero_item_warnings:
        print(_color("Warning — sources with 0 items (feed may be empty or mis-configured):", _YELLOW, use_color))
        for r in zero_item_warnings:
            print(f"  - {r['key']}: {r['url']}")
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
