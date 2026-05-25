"""Source diagnostics and metadata helpers for configured news sources."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from .config import ROOT_DIR


LANGUAGE_DETECTION_MODEL = "papluca/xlm-roberta-base-language-detection"
LANGUAGE_MIN_CONFIDENCE = 0.35
LANGUAGE_SAMPLE_LIMIT = 12
TEXT_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
SOURCE_FIELD_RE = re.compile(r"^    ([A-Za-z_][A-Za-z0-9_-]*):")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*",
    "Accept-Encoding": "gzip, deflate",
}


def _default_sources_yaml() -> Path:
    return ROOT_DIR / (os.environ.get("NEWS_SOURCES_YAML") or "config/sources.yaml")


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
                "language": str(record.get("language") or "").strip(),
            }
        )
    return rows


def _decompress_response_body(content: bytes, content_encoding: str) -> bytes:
    clean_encoding = (content_encoding or "").lower()
    if "gzip" in clean_encoding or content.startswith(b"\x1f\x8b"):
        return gzip.decompress(content)
    if "deflate" in clean_encoding:
        return zlib.decompress(content)
    return content


def _fetch_url_once(url: str, timeout: int) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read()
        content_encoding = response.headers.get("Content-Encoding", "")
        return _decompress_response_body(content, content_encoding), int(response.status)


def _fetch_url(url: str, timeout: int, *, retries: int = 1) -> tuple[bytes, int]:
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return _fetch_url_once(url, timeout)
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError(str(last_error or "fetch failed"))


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
        content, status = _fetch_url(source["url"], timeout)
        result["http_status"] = status
        result["latency_ms"] = int((time.monotonic() - started_at) * 1000)
        item_count, feed_format = _count_items(content, source["fetcher"])
        result.update(format=feed_format, item_count=item_count)
        if item_count <= 0:
            result["error"] = "Feed parsed but returned 0 items."
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

    return result


def _clean_sample_text(value: str) -> str:
    text = TEXT_TAG_RE.sub(" ", html.unescape(value or ""))
    return WHITESPACE_RE.sub(" ", text).strip()


def _local_xml_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _direct_child_text(node: ElementTree.Element, field_names: set[str]) -> str:
    parts: list[str] = []
    for child in list(node):
        if _local_xml_name(child.tag) in field_names:
            parts.append(" ".join(child.itertext()))
    return _clean_sample_text(" ".join(parts))


def _json_language_samples(content: bytes, max_items: int) -> list[str]:
    data = json.loads(content)
    samples: list[str] = []

    if isinstance(data, dict):
        children = data.get("data", {}).get("children", [])
        if isinstance(children, list):
            for child in children:
                record = child.get("data") if isinstance(child, dict) else None
                if not isinstance(record, dict):
                    continue
                sample = _clean_sample_text(
                    " ".join(
                        str(record.get(field) or "")
                        for field in ("title", "description", "selftext")
                    )
                )
                if sample:
                    samples.append(sample)
                if len(samples) >= max_items:
                    return samples

        items = data.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sample = _clean_sample_text(
                    " ".join(
                        str(item.get(field) or "")
                        for field in ("title", "summary", "content_text", "content_html")
                    )
                )
                if sample:
                    samples.append(sample)
                if len(samples) >= max_items:
                    return samples

    return samples


def _xml_language_samples(content: bytes, max_items: int) -> list[str]:
    root = ElementTree.fromstring(content)
    samples: list[str] = []
    text_fields = {"title", "description", "summary", "content", "encoded"}

    for node in root.iter():
        if _local_xml_name(node.tag) not in {"item", "entry"}:
            continue
        sample = _direct_child_text(node, text_fields)
        if sample:
            samples.append(sample)
        if len(samples) >= max_items:
            return samples

    fallback = _direct_child_text(root, {"title", "description", "subtitle"})
    if fallback:
        samples.append(fallback)
    return samples[:max_items]


def extract_language_samples(content: bytes, fetcher: str, max_items: int) -> list[str]:
    if fetcher in {"reddit", "reddit_top", "reddit_top_json"}:
        return _json_language_samples(content, max_items)

    try:
        return _xml_language_samples(content, max_items)
    except ElementTree.ParseError:
        return _json_language_samples(content, max_items)


def _load_language_detector(model_name: str) -> Any:
    try:
        from transformers import pipeline
    except ImportError as error:
        raise RuntimeError(
            "Language detection requires the transformers package. "
            "Install it in the uv environment or use the existing Darwin lock dependencies."
        ) from error

    try:
        return pipeline("text-classification", model=model_name, tokenizer=model_name)
    except Exception as error:
        raise RuntimeError(
            f"Could not load language detection model {model_name!r}. "
            "If this is the first run, the model may need to be downloaded."
        ) from error


def _best_language_label(output: Any) -> tuple[str, float]:
    if isinstance(output, list):
        candidates = [item for item in output if isinstance(item, dict)]
        if not candidates:
            return "", 0.0
        output = max(candidates, key=lambda item: float(item.get("score") or 0.0))
    if not isinstance(output, dict):
        return "", 0.0
    label = str(output.get("label") or "").strip().lower()
    try:
        score = float(output.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return label, score


def detect_language_from_samples(
    samples: list[str],
    detector: Any,
    *,
    min_confidence: float = LANGUAGE_MIN_CONFIDENCE,
) -> dict[str, Any]:
    if not samples:
        return {"language": None, "confidence": None, "scores": {}}

    try:
        raw_outputs = detector(samples, truncation=True, max_length=256)
    except TypeError:
        raw_outputs = detector(samples)

    outputs = raw_outputs if isinstance(raw_outputs, list) else [raw_outputs]
    scores: dict[str, float] = {}
    for output in outputs:
        label, score = _best_language_label(output)
        if not label:
            continue
        scores[label] = scores.get(label, 0.0) + score

    if not scores:
        return {"language": None, "confidence": None, "scores": {}}

    language = max(scores, key=scores.get)
    confidence = scores[language] / max(1, len(outputs))
    if confidence < min_confidence:
        return {"language": None, "confidence": confidence, "scores": scores}
    return {"language": language, "confidence": confidence, "scores": scores}


def detect_source_language(
    source: dict[str, Any],
    timeout: int,
    detector: Any,
    *,
    max_items: int = LANGUAGE_SAMPLE_LIMIT,
    min_confidence: float = LANGUAGE_MIN_CONFIDENCE,
) -> dict[str, Any]:
    started_at = time.monotonic()
    result: dict[str, Any] = {
        "key": source["key"],
        "name": source["name"],
        "section": source["section"],
        "url": source["url"],
        "ok": False,
        "skipped": False,
        "language": None,
        "confidence": None,
        "sample_count": 0,
        "latency_ms": None,
        "error": None,
    }

    try:
        content, _status_code = _fetch_url(source["url"], timeout)
        samples = extract_language_samples(content, source["fetcher"], max_items)
        result["sample_count"] = len(samples)
        if not samples:
            result["error"] = "No feed item text found."
            return result
        detection = detect_language_from_samples(samples, detector, min_confidence=min_confidence)
        result["language"] = detection["language"]
        result["confidence"] = detection["confidence"]
        if result["language"]:
            result["ok"] = True
        else:
            confidence = result["confidence"]
            if confidence is None:
                result["error"] = "Detector returned no language labels."
            else:
                result["error"] = f"Language confidence below {min_confidence:.2f}."
    except urllib.error.HTTPError as error:
        result["error"] = f"HTTP {error.code} {error.reason}"
    except urllib.error.URLError as error:
        result["error"] = f"URLError: {error.reason}"
    except TimeoutError:
        result["error"] = f"Timed out after {timeout}s"
    except Exception as error:
        result["error"] = str(error)
    finally:
        result["latency_ms"] = int((time.monotonic() - started_at) * 1000)

    return result


def _source_block_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    in_sources = False
    start: int | None = None

    for index, line in enumerate(lines):
        if not in_sources:
            if line.startswith("sources:"):
                in_sources = True
            continue
        if line.startswith("  - "):
            if start is not None:
                ranges.append((start, index))
            start = index
            continue
        if start is not None and line.strip() and not line.startswith((" ", "#")):
            ranges.append((start, index))
            start = None
            break

    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def _source_block_key(lines: list[str], start: int, end: int) -> str:
    try:
        payload = yaml.safe_load("sources:\n" + "".join(lines[start:end])) or {}
    except yaml.YAMLError:
        return ""
    records = payload.get("sources", []) if isinstance(payload, dict) else []
    if not records or not isinstance(records[0], dict):
        return ""
    return str(records[0].get("key") or records[0].get("name") or "").strip()


def _direct_field_line(lines: list[str], start: int, end: int, field: str) -> int | None:
    for index in range(start, end):
        match = SOURCE_FIELD_RE.match(lines[index])
        if match and match.group(1) == field:
            return index
    return None


def _preferred_language_insert_line(lines: list[str], start: int, end: int) -> int:
    for field in ("url", "region", "name"):
        field_line = _direct_field_line(lines, start, end, field)
        if field_line is not None:
            return field_line + 1
    return start + 1


def write_source_languages(path: Path, results: list[dict[str, Any]], *, overwrite: bool = False) -> int:
    detected = {
        str(result["key"]): str(result["language"])
        for result in results
        if result.get("ok") and result.get("language") and not result.get("skipped")
    }
    if not detected:
        return 0

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    newline = "\n"
    for line in lines:
        if line.endswith("\r\n"):
            newline = "\r\n"
            break
    edits: list[tuple[str, int, str]] = []

    for start, end in _source_block_ranges(lines):
        key = _source_block_key(lines, start, end)
        if key not in detected:
            continue
        language_line = f"    language: {detected[key]}{newline}"
        existing_line = _direct_field_line(lines, start, end, "language")
        if existing_line is not None:
            if overwrite:
                edits.append(("replace", existing_line, language_line))
            continue
        edits.append(("insert", _preferred_language_insert_line(lines, start, end), language_line))

    for action, index, line in reversed(edits):
        if action == "replace":
            lines[index] = line
        else:
            lines.insert(index, line)

    if edits:
        path.write_text("".join(lines), encoding="utf-8")
    return len(edits)


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


def print_language_table(results: list[dict[str, Any]]) -> None:
    if not results:
        return
    widths = {
        "key": min(max(len(row["key"]) for row in results), 28),
        "name": min(max(len(row["name"]) for row in results), 30),
    }
    print()
    print(
        f"  {'STATUS':<8}  {'KEY':<{widths['key']}}  {'NAME':<{widths['name']}}  "
        f"{'LANG':<5}  {'CONF':>5}  {'SAMPLES':>7}  ERROR"
    )
    print("  " + "-" * 92)
    for row in results:
        status = "SKIP" if row.get("skipped") else ("OK" if row.get("ok") else "FAIL")
        confidence = row.get("confidence")
        confidence_text = f"{confidence:.2f}" if isinstance(confidence, float) else "-"
        name = row["name"][: widths["name"]]
        key = row["key"][: widths["key"]]
        print(
            f"  {status:<8}  {key:<{widths['key']}}  {name:<{widths['name']}}  "
            f"{row.get('language') or '-':<5}  {confidence_text:>5}  "
            f"{row.get('sample_count') or 0:>7}  {row.get('error') or ''}"
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
    parser.add_argument("--only-failures", action="store_true", help="Print only failed sources.")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--detect-languages",
        action="store_true",
        help="Detect feed languages instead of running connectivity checks.",
    )
    parser.add_argument(
        "--write-languages",
        action="store_true",
        help="Write detected language values back to the source YAML.",
    )
    parser.add_argument(
        "--overwrite-languages",
        action="store_true",
        help="Re-detect sources that already have a language field and replace it when writing.",
    )
    parser.add_argument(
        "--language-model",
        default=LANGUAGE_DETECTION_MODEL,
        help=f"Hugging Face text-classification model for language detection. Defaults to {LANGUAGE_DETECTION_MODEL}.",
    )
    parser.add_argument(
        "--language-samples",
        type=int,
        default=LANGUAGE_SAMPLE_LIMIT,
        help="Maximum feed items to sample per source when detecting language.",
    )
    parser.add_argument(
        "--min-language-confidence",
        type=float,
        default=LANGUAGE_MIN_CONFIDENCE,
        help="Minimum aggregate confidence required before writing a language.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of sources checked.")
    parser.add_argument(
        "--section",
        choices=["sources", "all"],
        default="all",
        help="Which source section to check.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON.")
    return parser


def _run_language_detection(args: argparse.Namespace, sources: list[dict[str, Any]]) -> int:
    if args.limit is not None:
        sources = sources[: max(0, args.limit)]
    if not sources:
        print("No sources to check.", file=sys.stderr)
        return 2

    pending = [
        source
        for source in sources
        if args.overwrite_languages or not str(source.get("language") or "").strip()
    ]
    detector = None
    if pending:
        if not args.json_output:
            print(f"Loading language detector {args.language_model}...")
        try:
            detector = _load_language_detector(args.language_model)
        except RuntimeError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

    if not args.json_output:
        print(
            f"Detecting languages for {len(sources)} source(s) from {args.sources_yaml} "
            f"(timeout={args.timeout}s, samples={args.language_samples})..."
        )

    results: list[dict[str, Any]] = []
    for source in sources:
        existing_language = str(source.get("language") or "").strip()
        if existing_language and not args.overwrite_languages:
            results.append(
                {
                    "key": source["key"],
                    "name": source["name"],
                    "section": source["section"],
                    "url": source["url"],
                    "ok": True,
                    "skipped": True,
                    "language": existing_language,
                    "confidence": None,
                    "sample_count": 0,
                    "latency_ms": 0,
                    "error": "language already set",
                }
            )
            continue
        results.append(
            detect_source_language(
                source,
                args.timeout,
                detector,
                max_items=max(1, args.language_samples),
                min_confidence=args.min_language_confidence,
            )
        )

    written = 0
    if args.write_languages:
        written = write_source_languages(args.sources_yaml, results, overwrite=args.overwrite_languages)

    if args.json_output:
        print(json.dumps({"results": results, "written": written}, indent=2))
    else:
        print_language_table(results)
        detected = sum(1 for result in results if result.get("ok") and not result.get("skipped"))
        skipped = sum(1 for result in results if result.get("skipped"))
        failed = sum(1 for result in results if not result.get("ok"))
        print()
        summary = f"  {detected} detected"
        if skipped:
            summary += f", {skipped} skipped"
        if failed:
            summary += f", {failed} failed"
        if args.write_languages:
            summary += f", {written} YAML update(s) written"
        print(summary)

    return 1 if any(not result.get("ok") for result in results) else 0


def _probe_sources(
    sources: list[dict[str, Any]],
    *,
    timeout: int,
    concurrency: int,
) -> list[dict[str, Any]]:
    worker_count = max(1, min(max(1, concurrency), len(sources)))
    if worker_count == 1:
        return [probe_source(source, timeout) for source in sources]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(probe_source, source, timeout) for source in sources]
        return [future.result() for future in futures]


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

    if args.detect_languages:
        return _run_language_detection(args, sources)

    if args.limit is not None:
        sources = sources[: max(0, args.limit)]
        if not sources:
            print("No sources to check.", file=sys.stderr)
            return 2

    if not args.json_output:
        print(
            f"Checking {len(sources)} source(s) from {args.sources_yaml} "
            f"(timeout={args.timeout}s, concurrency={args.concurrency})..."
        )
    results = _probe_sources(sources, timeout=args.timeout, concurrency=args.concurrency)

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
