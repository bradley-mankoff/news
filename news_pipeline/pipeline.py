"""Daily news pipeline implementation.

Common usage:
    uv run news dev
    uv run news local-prod
    uv run news loose-local-prod
    uv run news prod

Development vs. real sends:
    uv run news dev
        Default. Sends only to NEWS_DEV_RECIPIENT, writes dev_used_urls.txt,
        uses core English sources allowed for the active topic IDs, and does not
        add URLs to the long-lived seen_urls.txt history.

    uv run news local-prod
        Core plus peripheral English source run with isolated URL history, but
        delivery is limited to NEWS_DEV_RECIPIENT for review and manual forwarding.

    uv run news loose-local-prod
        Local production review with dev-loose topic/story matching thresholds
        and the regular local-prod four-article story floor.

    uv run news prod
        Production run. Uses the configured recipient list, writes used_urls.txt,
        and records seen URLs globally so future runs avoid them.

Model selection:
    NEWS_MODEL=gemma-e2b-tiny uv run news dev
    NEWS_MODEL=gemma-26b-moe uv run news local-prod
    NEWS_MODEL=qwen-9b-dense uv run news local-prod

    NEWS_MODEL accepts either a friendly alias above or a full model repo/name.
    NEWS_MODEL_NAME is still honored as a lower-priority legacy override, and
    NEWS_DEFAULT_MODEL changes the fallback when neither is set.

Local model server:
    NEWS_MODEL=qwen-9b-dense uv run news dev
        Starts the matching local MLX server automatically, waits until it
        is ready, runs the pipeline, then shuts the managed server down even if
        the run errors. Server logs are written beside the report output.

    NEWS_MODEL=qwen-9b-dense uv run news model-server-command
        Prints the matching MLX server command for the selected model and
        inferred runtime profile without starting the pipeline.

    uv run news codex-model-server-command
        Prints the Codex-safe MLX server command for gemma-e2b-tiny. Codex-run
        model invocation is blocked unless this tiny model is selected.

    NEWS_MODEL_BASE_URL=http://127.0.0.1:8080/v1 uv run news dev
        Points the pipeline at a different OpenAI-compatible local endpoint.

Other useful switches:
    uv run news dev --topics sports,science_space_tech
        Overrides config/client.yaml for one run with a comma-separated list of
        configured topic IDs.

    NEWS_IMAGE_ENABLED=0 uv run news dev
        Skips report image generation.

    uv run news serve-unsubscribe
        Runs the local unsubscribe endpoint instead of generating a report.

This module owns orchestration and the heavier pipeline logic. Configuration
lives in ``config/*.yaml``. Predefined topic selection reads ``client.yaml`` and
``topics.yaml`` by default; diagnostic run details are written beside each report
under ``output/daily_outputs/<date>/``.
"""

import importlib.util
import gc
import json
import logging
import math
import re
import os
import shlex
import signal
import sys
import time
import textwrap
import smtplib
import html
import base64
import hashlib
import hmac
import random
import subprocess
import tempfile
import trafilatura
from collections import Counter
from contextlib import contextmanager
from threading import Lock, current_thread, main_thread
from typing import Any, Callable, TextIO, List
import requests
from bs4 import BeautifulSoup
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse
import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .config import (
    ModelSamplingSettings,
    ensure_codex_safe_model_reference,
    load_predefined_topics,
    load_recipients,
    load_runtime_config,
    load_sources,
    sync_assistant_context_latest_output,
    update_recipient_pause_setting,
    write_source_translation_flags,
)
from .diagnostics import RunDiagnostics
from . import article_summarization as article_summarization_stage
from . import citations as citations_stage
from . import embeddings as embeddings_stage
from . import story_clustering as story_clustering_stage
from . import story_drafting as story_drafting_stage
from . import story_topic_assignment as story_topic_assignment_stage
from .text_cleaning import (
    clean_article_text as _clean_article_text,
    clean_content_text as _clean_content_text,
    clean_feed_text as _clean_feed_text,
    clean_feed_url as _clean_feed_url,
)

try:
    import tiktoken
except ImportError:
    tiktoken = None


logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


MODEL_RETRY_ATTEMPTS = 4
MODEL_RETRY_BASE_DELAY_SECONDS = 2
MODEL_REQUEST_TIMEOUT_SECONDS = max(
    10,
    _int_env("NEWS_MODEL_REQUEST_TIMEOUT_SECONDS", 180),
)
MODEL_LOAD_PROBE_TIMEOUT_SECONDS = max(
    10,
    _int_env("NEWS_MODEL_LOAD_PROBE_TIMEOUT_SECONDS", 120),
)
ARTICLE_DOWNLOAD_TIMEOUT_SECONDS = max(
    5,
    _int_env("NEWS_ARTICLE_DOWNLOAD_TIMEOUT_SECONDS", 20),
)
ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS = max(
    ARTICLE_DOWNLOAD_TIMEOUT_SECONDS,
    _int_env("NEWS_ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS", 30),
)
SLOW_SOURCE_WARNING_SECONDS = max(
    5,
    _int_env("NEWS_SLOW_SOURCE_WARNING_SECONDS", 60),
)
CONFIG = load_runtime_config()
MODEL_NAME = CONFIG.model_name
MODEL_REFERENCE = CONFIG.model_reference
MODEL_PROFILE = CONFIG.model_profile
MODEL_PROFILE_KEY = MODEL_PROFILE.key
MODEL_BASE_URL = CONFIG.model_base_url
MODEL_BACKEND = CONFIG.model_backend
MODEL_SERVER_COMMAND = CONFIG.model_server_command
TRANSLATION_MODEL_REFERENCE = CONFIG.translation_model_reference
TRANSLATION_MODEL_NAME = CONFIG.translation_model_name
TRANSLATION_MODEL_BASE_URL = CONFIG.translation_model_base_url
TRANSLATION_MODEL_BACKEND = CONFIG.translation_model_backend
TRANSLATION_MODEL_SERVER_COMMAND = CONFIG.translation_model_server_command
TRANSLATION_TARGET_LANGUAGE = CONFIG.translation_target_language
TRANSLATION_ENABLED = CONFIG.translation_enabled
BRADLEY_ONLY_RECIPIENT = CONFIG.bradley_only_recipient
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
ARTICLE_DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

LAST_TOP_FUNNEL_PROVIDER_STORIES: dict[str, list[dict]] = {}
LAST_TOP_FUNNEL_PROVIDER_METADATA: dict[str, dict] = {}
TOPIC_FRAME_TARGETS = {"western": 0.75, "us": 0.50, "non_western": 0.25}
TOPIC_FRAME_NUDGE_STRENGTH = 0.75
EXCLUDED_NEWS_SOURCE_LABELS = {"abcnews", "abcnewsgo"}
EXCLUDED_NEWS_SOURCE_DOMAINS = {"abcnews.go.com", "abcnews.com"}
EXCLUDED_FEED_ITEM_PATTERNS = (
    (
        "daily_puzzle_answer",
        re.compile(
            r"\b(?:today(?:'|’)?s|daily)\b.{0,80}\b"
            r"(?:nyt\s+)?(?:connections|strands|wordle|mini\s+crossword|crossword)\b"
            r".{0,80}\b(?:hint|hints|answer|answers|clue|clues|help)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "daily_puzzle_answer",
        re.compile(
            r"\b(?:nyt\s+)?(?:connections|strands|wordle|mini\s+crossword|crossword)\b"
            r".{0,80}\b(?:hint|hints|answer|answers|clue|clues|help)\b",
            re.IGNORECASE,
        ),
    ),
)
SOURCE_MATCH_MODE_FEED_LABEL = "feed_label"
SOURCE_MATCH_MODE_WIRE_ATTRIBUTION = "wire_attribution"
WIRE_ATTRIBUTION_EVIDENCE_CHARS = 2500
WIRE_ATTRIBUTION_BEFORE_ALIAS = (
    "by",
    "credited to",
    "distributed by",
    "from",
    "provided by",
    "reporting by",
    "via",
    "written by",
)
WIRE_ATTRIBUTION_AFTER_ALIAS = (
    "contributed",
    "contributed to this report",
    "distributed",
    "provided",
    "reported",
    "wrote",
)

RECENT_WINDOW_HOURS = CONFIG.recent_window_hours
MAX_ARTICLES_PER_SOURCE = CONFIG.max_articles_per_source
NUM_TOP_TOPICS = CONFIG.num_top_topics
TOP_TOPIC_PROBES = CONFIG.top_topic_probes
TOP_OF_FUNNEL_PER_PROVIDER = CONFIG.top_of_funnel_per_provider
PROJECT_SUMMARY_SCOPE_LABEL = CONFIG.summary_scope_label

RUN_STARTED_AT = CONFIG.run_started_at
RUN_DATE = CONFIG.run_date
timestamp = CONFIG.timestamp
OUTPUT_DIR = str(CONFIG.output_dir)
RUN_OUTPUT_DIR = str(CONFIG.run_output_dir)
USED_URLS_FILENAME = CONFIG.used_urls_filename
DEV_USED_URLS_FILENAME = CONFIG.dev_used_urls_filename
LOCAL_PROD_USED_URLS_FILENAME = CONFIG.local_prod_used_urls_filename
LEGACY_SEEN_URLS_PATH = str(CONFIG.legacy_seen_urls_path)
RUN_MODE = CONFIG.run_mode
DEV = CONFIG.dev
LOCAL_PROD = CONFIG.local_prod
LOOSE_LOCAL_PROD = CONFIG.loose_local_prod
BRADLEY_ONLY_DELIVERY = CONFIG.bradley_only_delivery
SHARED_URL_HISTORY_ENABLED = CONFIG.shared_url_history_enabled
RELAXED_FINAL_SYNTHESIS_GUARDS = CONFIG.relaxed_final_synthesis_guards
TOPIC_MODE = CONFIG.topic_mode
ACTIVE_TOPIC_IDS = CONFIG.topic_ids
RUN_USED_URLS_PATH = str(CONFIG.run_used_urls_path)
RUN_LOG_PATH = os.path.join(RUN_OUTPUT_DIR, f"run_log_{timestamp}.log")
EMAIL_RECIPIENTS_FALLBACK = CONFIG.email_recipients_fallback
EMAIL_FROM = CONFIG.email_from
SMTP_HOST = CONFIG.smtp_host
SMTP_PORT = CONFIG.smtp_port
SMTP_USERNAME = CONFIG.smtp_username
SMTP_USE_SSL = CONFIG.smtp_use_ssl
SMTP_PASSWORD = CONFIG.smtp_password
UNSUBSCRIBE_BASE_URL = CONFIG.unsubscribe_base_url
UNSUBSCRIBE_HOST = CONFIG.unsubscribe_host
UNSUBSCRIBE_PORT = CONFIG.unsubscribe_port
UNSUBSCRIBE_SECRET = CONFIG.unsubscribe_secret
MODEL_MAX_INPUT_TOKENS = MODEL_PROFILE.model_max_input_tokens
TOKEN_ENCODING_NAME = CONFIG.token_encoding_name
ARTICLE_SUMMARY_CONCURRENCY = max(1, MODEL_PROFILE.article_summary_concurrency)
ARTICLE_TEXT_TOKEN_LIMIT = max(500, MODEL_PROFILE.article_text_token_limit)
TOTAL_ARTICLE_SUMMARY_CAP = max(0, MODEL_PROFILE.total_article_summary_cap)
PER_TOPIC_ARTICLE_SUMMARY_CAP = max(0, MODEL_PROFILE.per_topic_article_summary_cap)
PER_SOURCE_TOPIC_ARTICLE_CAP = max(0, CONFIG.per_source_topic_article_cap)
TOPIC_CLUSTERING_MAX_TOKENS = max(100, MODEL_PROFILE.topic_clustering_max_tokens)
TRANSLATION_MAX_TOKENS = max(100, MODEL_PROFILE.translation_max_tokens)
ARTICLE_SUMMARY_MAX_TOKENS = max(100, MODEL_PROFILE.article_summary_max_tokens)
FINAL_SYNTHESIS_MAX_TOKENS = max(100, MODEL_PROFILE.final_synthesis_max_tokens)
TITLE_GENERATION_MAX_TOKENS = max(20, MODEL_PROFILE.title_generation_max_tokens)


def _bounded_env_float(name: str, default: float, *, lower: float = 0.0, upper: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(upper, max(lower, value))


MIN_ARTICLES_PER_STORY = max(2, CONFIG.min_articles_per_story)
TOPIC_RELEVANCE_MIN_SCORE = max(1, CONFIG.topic_relevance_min_score)
STORY_TOPIC_FIT_MIN_SCORE = max(1, CONFIG.story_topic_fit_min_score)
STORY_TOPIC_VALIDATION_ENABLED = CONFIG.story_topic_validation_enabled
MAX_STORIES_PER_TOPIC = max(1, CONFIG.max_stories_per_topic)
STORY_CLUSTER_SIMILARITY_THRESHOLD = min(
    1.0,
    max(0.0, CONFIG.story_cluster_similarity_threshold),
)
TOPIC_STORY_DIVERSITY_MIN_DISTANCE = _bounded_env_float(
    "NEWS_TOPIC_STORY_DIVERSITY_MIN_DISTANCE",
    0.50,
)
TOPIC_EMBEDDING_SIMILARITY_THRESHOLD = min(
    1.0,
    max(0.0, CONFIG.topic_embedding_similarity_threshold),
)
STORY_EMBEDDING_DEDUP_THRESHOLD = _bounded_env_float(
    "NEWS_STORY_DEDUP_THRESHOLD",
    0.85,
)
STORY_BACKFILL_BATCH_MULTIPLIER = max(
    1,
    _int_env("NEWS_STORY_BACKFILL_BATCH_MULTIPLIER", 2),
)
IMAGE_GENERATION_ENABLED = CONFIG.image_generation_enabled
IMAGE_GENERATION_FAIL_ON_ERROR = CONFIG.image_generation_fail_on_error
IMAGE_WIDTH = max(256, CONFIG.image_width)
IMAGE_HEIGHT = max(256, CONFIG.image_height)
IMAGE_STEPS = max(1, CONFIG.image_steps)
IMAGE_CROP_BOTTOM_RATIO = min(max(CONFIG.image_crop_bottom_ratio, 0.0), 0.35)
IMAGE_MODEL_ID = CONFIG.image_model_id
IMAGE_BASE_MODEL = CONFIG.image_base_model
IMAGE_MODEL_LABEL = IMAGE_MODEL_ID.split("/")[-1] if "/" in IMAGE_MODEL_ID else IMAGE_MODEL_ID
MODEL_DEFAULT_SAMPLING = MODEL_PROFILE.default_sampling
MODEL_REASONING_SAMPLING = MODEL_PROFILE.reasoning_sampling
MODEL_TASK_SAMPLING = MODEL_PROFILE.task_sampling
MODEL_CALL_STATS: dict[str, Any] = {
    "calls": {},
    "token_usage": {},
    "retries": 0,
    "fallbacks": 0,
    "failures": {},
}
MODEL_CALL_STATS_LOCK = Lock()
RUN_ACTIVITY_SNAPSHOTS: list[dict[str, Any]] = []
ACTIVE_RUN_DIAGNOSTICS: RunDiagnostics | None = None
MANAGED_MODEL_SERVER_ACTIVE = False
MANAGED_MODEL_SERVER_READY = False
MANAGED_MODEL_SERVER_EXTERNAL = False
MANAGED_MODEL_SERVER_PROCESS: subprocess.Popen | None = None
MANAGED_MODEL_SERVER_LOG_FILE: TextIO | None = None
TRANSLATION_MODEL_RESOURCES: tuple[Any, Any, Any] | None = None


def _read_url_file(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def _load_seen_urls() -> set[str]:
    if not SHARED_URL_HISTORY_ENABLED:
        return set()
    seen_urls = _read_url_file(LEGACY_SEEN_URLS_PATH)
    for root, _, files in os.walk(OUTPUT_DIR):
        if USED_URLS_FILENAME in files:
            seen_urls.update(_read_url_file(os.path.join(root, USED_URLS_FILENAME)))
    return seen_urls


def _append_unique_urls(path: str, urls: list[str]) -> None:
    if not urls:
        return

    existing_urls = _read_url_file(path)
    urls_to_write: list[str] = []
    for url in urls:
        clean_url = str(url).strip()
        if not clean_url or clean_url in existing_urls:
            continue
        existing_urls.add(clean_url)
        urls_to_write.append(clean_url)

    if not urls_to_write:
        return

    with open(path, "a", encoding="utf-8") as f:
        for url in urls_to_write:
            f.write(url + "\n")


def _record_run_urls(urls: list[str]) -> None:
    _append_unique_urls(RUN_USED_URLS_PATH, urls)
    if SHARED_URL_HISTORY_ENABLED:
        _append_unique_urls(LEGACY_SEEN_URLS_PATH, urls)


def _ordered_unique_urls(urls: list[str]) -> list[str]:
    seen_urls: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        clean_url = str(url or "").strip()
        if not clean_url or clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)
        unique_urls.append(clean_url)
    return unique_urls


def _persist_url_list_debug(urls: list[str], label: str) -> tuple[str, int] | None:
    unique_urls = _ordered_unique_urls(urls)
    if not unique_urls:
        return None

    safe_label = re.sub(r"[^a-zA-Z0-9_]+", "_", label).strip("_") or "urls"
    debug_path = os.path.join(RUN_OUTPUT_DIR, f"{safe_label}_{timestamp}.txt")
    try:
        with open(debug_path, "w", encoding="utf-8") as debug_file:
            for url in unique_urls:
                debug_file.write(url + "\n")
        return debug_path, len(unique_urls)
    except Exception:
        return None


def _run_article_topic_key(url: str, topic_key: Any) -> tuple[str, str]:
    return (str(url or "").strip(), str(topic_key or "").strip())


def _parse_activity_command_output(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        free_pct_match = re.search(r"memory free percentage:\s*(\d+)%", line, flags=re.IGNORECASE)
        if free_pct_match:
            parsed["memory_free_pct"] = int(free_pct_match.group(1))
            continue
        pages_match = re.match(r'"?([^":]+)"?:\s+([0-9]+)\.?$', line)
        if pages_match:
            key = re.sub(r"[^a-z0-9]+", "_", pages_match.group(1).strip().lower()).strip("_")
            if key:
                parsed[key] = int(pages_match.group(2))
    return parsed


def _run_activity_command(command: list[str], *, timeout: int = 5) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except Exception as error:
        return {
            "ok": False,
            "command": command,
            "error": str(error),
        }

    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "ok": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "parsed": _parse_activity_command_output(output),
        "output_tail": output[-4000:],
    }


def capture_activity_snapshot(label: str) -> dict[str, Any]:
    """Collect macOS memory/activity signals without making the run depend on them."""
    snapshot = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "commands": {
            "memory_pressure": _run_activity_command(["/usr/bin/memory_pressure"]),
            "vm_stat": _run_activity_command(["/usr/bin/vm_stat"]),
        },
    }
    memory_pressure = snapshot["commands"]["memory_pressure"]
    if isinstance(memory_pressure, dict):
        parsed = memory_pressure.get("parsed") or {}
        if "memory_free_pct" in parsed:
            snapshot["memory_free_pct"] = parsed["memory_free_pct"]
    vm_stat = snapshot["commands"]["vm_stat"]
    if isinstance(vm_stat, dict):
        parsed = vm_stat.get("parsed") or {}
        for key in ("swapins", "swapouts", "pages_occupied_by_compressor"):
            if key in parsed:
                snapshot[key] = parsed[key]
    return snapshot


def record_activity_snapshot(label: str, diagnostics: RunDiagnostics | None = None) -> dict[str, Any]:
    snapshot = capture_activity_snapshot(label)
    RUN_ACTIVITY_SNAPSHOTS.append(snapshot)
    if diagnostics is not None:
        diagnostics.record_activity_snapshot(snapshot)
    return snapshot


def _attach_pending_activity_snapshots(diagnostics: RunDiagnostics) -> None:
    seen = {
        (snapshot.get("at"), snapshot.get("label"))
        for snapshot in diagnostics.activity_snapshots
    }
    for snapshot in RUN_ACTIVITY_SNAPSHOTS:
        key = (snapshot.get("at"), snapshot.get("label"))
        if key in seen:
            continue
        diagnostics.record_activity_snapshot(snapshot)
        seen.add(key)


def _normalize_source_label(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _url_has_excluded_source_domain(value: str | None) -> bool:
    raw_value = (value or "").strip()
    if not raw_value:
        return False
    try:
        parsed = urlparse(raw_value)
    except Exception:
        return False
    hostname = (parsed.netloc or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in EXCLUDED_NEWS_SOURCE_DOMAINS
    )


def _is_excluded_news_source(*values: str | None) -> bool:
    for value in values:
        if _normalize_source_label(value) in EXCLUDED_NEWS_SOURCE_LABELS:
            return True
        if _url_has_excluded_source_domain(value):
            return True
    return False


def _is_excluded_feed_item(title: str | None, source: str | None, link: str | None) -> bool:
    if _is_excluded_news_source(source, link):
        return True
    title_text = (title or "").strip()
    if " - " in title_text:
        return _is_excluded_news_source(title_text.rsplit(" - ", 1)[-1])
    return False


def _feed_title_source_suffix(title: str | None) -> str:
    title_text = str(title or "").strip()
    if " - " not in title_text:
        return ""
    suffix = title_text.rsplit(" - ", 1)[-1].strip()
    return suffix


def _source_match_aliases(source_name: str, source_config: dict[str, Any]) -> set[str]:
    aliases = {
        str(source_name or "").strip(),
        str(source_config.get("name") or "").strip(),
    }
    aliases.update(str(alias or "").strip() for alias in source_config.get("source_match_aliases") or [])
    return {_normalize_source_label(alias) for alias in aliases if _normalize_source_label(alias)}


def _source_match_mode(source_config: dict[str, Any]) -> str:
    mode = str(source_config.get("source_match_mode") or SOURCE_MATCH_MODE_FEED_LABEL)
    mode = mode.strip().lower().replace("-", "_")
    if mode == SOURCE_MATCH_MODE_WIRE_ATTRIBUTION:
        return SOURCE_MATCH_MODE_WIRE_ATTRIBUTION
    return SOURCE_MATCH_MODE_FEED_LABEL


def _configured_source_display_name(source_name: str, source_config: dict[str, Any]) -> str:
    return str(source_config.get("name") or source_name or "Unknown source").strip()


def _feed_item_source_labels(item: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    feed_source = str(item.get("source") or "").strip()
    title_suffix = _feed_title_source_suffix(str(item.get("title") or ""))
    for label in (feed_source, title_suffix):
        clean_label = label.strip()
        if clean_label and clean_label not in labels:
            labels.append(clean_label)
    return labels


def _publisher_source_label(item: dict[str, Any], source_display_name: str) -> str:
    labels = _feed_item_source_labels(item)
    return labels[0] if labels else source_display_name


def _source_display_name_for_match(
    *,
    source_display_name: str,
    publisher_source: str,
    wire_source: str,
    source_match_status: str,
) -> str:
    if (
        source_match_status == "wire_attribution_confirmed"
        and publisher_source
        and wire_source
        and _normalize_source_label(publisher_source) != _normalize_source_label(wire_source)
    ):
        return f"{wire_source} via {publisher_source}"
    return source_display_name


def _source_match_public_metadata(match_result: dict[str, Any]) -> dict[str, str]:
    return {
        "source_match_status": str(match_result.get("source_match_status") or ""),
        "publisher_source": str(match_result.get("publisher_source") or ""),
        "wire_source": str(match_result.get("wire_source") or ""),
        "source_display_name": str(match_result.get("source_display_name") or ""),
    }


def _source_match_result_for_feed_item(
    source_name: str,
    source_config: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    source_display_name = _configured_source_display_name(source_name, source_config)
    publisher_source = _publisher_source_label(item, source_display_name)
    mode = _source_match_mode(source_config)

    base_result: dict[str, Any] = {
        "accepted": False,
        "pending_wire_attribution": False,
        "source": source_name,
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "feed_source": item.get("source", ""),
        "title_source_suffix": _feed_title_source_suffix(str(item.get("title") or "")),
        "publisher_source": publisher_source,
        "wire_source": source_display_name if mode == SOURCE_MATCH_MODE_WIRE_ATTRIBUTION else "",
        "source_display_name": source_display_name,
        "source_match_mode": mode,
    }
    if not bool(source_config.get("strict_source_match")):
        return {
            **base_result,
            "accepted": True,
            "source_match_status": "not_required",
            "publisher_source": source_display_name,
            "wire_source": "",
        }

    aliases = _source_match_aliases(source_name, source_config)
    labels = _feed_item_source_labels(item)
    normalized_labels = [_normalize_source_label(label) for label in labels]
    observed_labels = [label for label in normalized_labels if label]
    matched = bool(observed_labels) and all(label in aliases for label in observed_labels)
    if matched:
        return {
            **base_result,
            "accepted": True,
            "source_match_status": "feed_label_confirmed",
            "accepted_aliases": sorted(aliases),
            "observed_source_labels": labels,
            "source_display_name": source_display_name,
        }

    if mode == SOURCE_MATCH_MODE_WIRE_ATTRIBUTION:
        return {
            **base_result,
            "reason": "pending_wire_attribution",
            "pending_wire_attribution": True,
            "source_match_status": "wire_attribution_pending",
            "accepted_aliases": sorted(aliases),
            "observed_source_labels": labels,
        }

    return {
        **base_result,
        "reason": "wrong_feed_source",
        "source_match_status": "wrong_feed_source",
        "accepted_aliases": sorted(aliases),
        "observed_source_labels": labels,
    }


def _feed_item_matches_configured_source(
    source_name: str,
    source_config: dict[str, Any],
    item: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    match_result = _source_match_result_for_feed_item(source_name, source_config, item)
    if match_result.get("accepted"):
        return True, None
    return False, match_result


def _wire_attribution_aliases(source_name: str, source_config: dict[str, Any]) -> list[str]:
    raw_aliases = [
        source_name,
        str(source_config.get("name") or ""),
        *(str(alias or "") for alias in source_config.get("source_match_aliases") or []),
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for alias in raw_aliases:
        clean_alias = re.sub(r"\s+", " ", str(alias or "")).strip()
        if not clean_alias:
            continue
        variants = [clean_alias]
        if clean_alias.lower().startswith("the "):
            variants.append(clean_alias[4:].strip())
        for variant in variants:
            normalized = _normalize_source_label(variant)
            if normalized and normalized not in seen:
                aliases.append(variant)
                seen.add(normalized)
    return aliases


def _wire_attribution_phrase_pattern(phrase: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", phrase)
    return r"\s+".join(re.escape(word) for word in words)


def _wire_attribution_alias_pattern(alias: str) -> str:
    clean_alias = re.sub(r"(?i)^the\s+", "", str(alias or "").strip())
    words = re.findall(r"[A-Za-z0-9]+", clean_alias)
    return r"[\s.\-]+".join(re.escape(word) for word in words)


def _article_confirms_wire_attribution(
    source_name: str,
    source_config: dict[str, Any],
    item: dict[str, Any],
    article_text: str,
) -> tuple[bool, str]:
    evidence_text = "\n".join(
        part
        for part in (
            str(item.get("author") or ""),
            str(item.get("creator") or ""),
            str(item.get("description") or ""),
            str(article_text or "")[:WIRE_ATTRIBUTION_EVIDENCE_CHARS],
        )
        if part
    )
    if not evidence_text.strip():
        return False, ""

    before_pattern = "|".join(
        _wire_attribution_phrase_pattern(phrase)
        for phrase in WIRE_ATTRIBUTION_BEFORE_ALIAS
    )
    after_pattern = "|".join(
        _wire_attribution_phrase_pattern(phrase)
        for phrase in WIRE_ATTRIBUTION_AFTER_ALIAS
    )
    for alias in _wire_attribution_aliases(source_name, source_config):
        alias_pattern = _wire_attribution_alias_pattern(alias)
        if not alias_pattern:
            continue
        alias_expr = rf"(?:the\s+)?{alias_pattern}"
        if re.search(
            rf"(?im)(?:^|[\n\r.;:|•-])\s*(?:{before_pattern})\s+{alias_expr}\b",
            evidence_text,
        ):
            return True, alias
        if re.search(
            rf"(?im)(?:^|[\n\r.;:|•-])\s*{alias_expr}\s+(?:{after_pattern})\b",
            evidence_text,
        ):
            return True, alias
        if re.search(
            rf"(?im)\bcopyright\s+(?:\d{{4}}\s+)?{alias_expr}\b",
            evidence_text,
        ):
            return True, alias
        if len(_normalize_source_label(alias)) > 3 and re.search(
            rf"(?im)^\s*{alias_expr}\s*$",
            evidence_text,
        ):
            return True, alias
    return False, ""


def _confirm_wire_source_match(
    match_result: dict[str, Any],
    *,
    attribution_alias: str,
) -> dict[str, Any]:
    wire_source = str(match_result.get("wire_source") or match_result.get("source_display_name") or "")
    publisher_source = str(match_result.get("publisher_source") or "")
    source_display_name = _source_display_name_for_match(
        source_display_name=str(match_result.get("source_display_name") or wire_source),
        publisher_source=publisher_source,
        wire_source=wire_source,
        source_match_status="wire_attribution_confirmed",
    )
    return {
        **match_result,
        "accepted": True,
        "pending_wire_attribution": False,
        "reason": "",
        "source_match_status": "wire_attribution_confirmed",
        "wire_attribution_alias": attribution_alias,
        "source_display_name": source_display_name,
    }


def _wire_source_unattributed_rejection(
    match_result: dict[str, Any],
    *,
    resolved_url: str = "",
    scrape_status: str = "",
) -> dict[str, Any]:
    return {
        **match_result,
        "accepted": False,
        "pending_wire_attribution": False,
        "reason": "wrong_feed_source_unattributed",
        "source_match_status": "wrong_feed_source_unattributed",
        "resolved_url": resolved_url,
        "scrape_status": scrape_status,
    }


def _record_feed_source_rejection(
    rejected_counts: Counter[str],
    rejections: list[dict[str, Any]],
    rejection: dict[str, Any],
) -> None:
    reason = str(rejection.get("reason") or "wrong_feed_source")
    rejected_counts[reason] += 1
    if len(rejections) < 50:
        rejections.append(rejection)


def _excluded_feed_item_reason(item: dict[str, Any]) -> str:
    text = " ".join(
        str(item.get(field) or "")
        for field in ("title", "description", "summary", "link")
    )
    for reason, pattern in EXCLUDED_FEED_ITEM_PATTERNS:
        if pattern.search(text):
            return reason
    return ""


SOURCE_FEEDS = load_sources(
    CONFIG.sources_path,
    run_mode=CONFIG.run_mode,
    active_topic_ids=CONFIG.topic_ids,
)
TOP_FUNNEL_PROVIDERS: dict[str, dict[str, Any]] = {}


def _source_allowed_topic_ids(source_name: str) -> set[str]:
    source_config = SOURCE_FEEDS.get(source_name) or {}
    return {
        str(topic_id or "").strip()
        for topic_id in source_config.get("allowed_topic_ids", [])
        if str(topic_id or "").strip()
    }


def _filter_articles_by_source_topic_scope(
    articles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retained: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for article in articles:
        source_name = str(article.get("source") or "").strip()
        allowed_topic_ids = _source_allowed_topic_ids(source_name)
        if not allowed_topic_ids:
            retained.append(article)
            continue
        topic_key = str(article.get("topic_key") or "").strip()
        if topic_key in allowed_topic_ids:
            retained.append(article)
            continue
        dropped.append(
            {
                "article_id": article.get("article_id"),
                "source": source_name,
                "title": article.get("title"),
                "url": article.get("url") or article.get("resolved_url"),
                "topic_key": topic_key,
                "topic_title": article.get("topic_title"),
                "allowed_topic_ids": sorted(allowed_topic_ids),
            }
        )
    return retained, dropped

LOW_CONFIDENCE_SUMMARY_PATTERNS = [
    "insufficient to create a substantive summary",
    "contains only the headline",
    "only contains the headline",
    "headline and metadata",
    "metadata wrapper",
    "metadata-only entry",
    "placeholder or metadata-only entry",
    "article text is missing",
    "without any supporting article content",
    "without any substantive reporting content",
    "cannot provide a detailed summary",
    "cannot provide a factual summary",
    "the only concrete information available is the headline",
    "provided article metadata and text only contain a headline",
]

LOW_COVERAGE_SYNTHESIS_PATTERNS = [
    "no high-confidence updates in supplied coverage",
    "primary dataset provided for this section is completely empty",
    "primary dataset is literally empty",
    "dataset appears to have crashed",
    "zero data available",
    "there is no article text, metadata, headlines",
    "impossible to extract",
    "impossible to summarize",
    "making any attempt to summarize",
    "unsupported by the provided source material",
]

TOPIC_MATCH_STOPWORDS = {
    "a",
    "about",
    "after",
    "against",
    "amid",
    "among",
    "an",
    "and",
    "are",
    "around",
    "as",
    "at",
    "auto-generated",
    "auto",
    "be",
    "for",
    "fallback",
    "from",
    "generated",
    "has",
    "have",
    "headline",
    "her",
    "him",
    "his",
    "into",
    "in",
    "is",
    "its",
    "it",
    "being",
    "just",
    "may",
    "me",
    "my",
    "new",
    "no",
    "not",
    "now",
    "of",
    "off",
    "one",
    "on",
    "or",
    "over",
    "provider",
    "providers",
    "reported",
    "report",
    "reports",
    "says",
    "said",
    "say",
    "seed",
    "some",
    "source",
    "sources",
    "support",
    "that",
    "the",
    "them",
    "their",
    "they",
    "this",
    "through",
    "tells",
    "tell",
    "told",
    "to",
    "topic",
    "under",
    "up",
    "was",
    "we",
    "were",
    "who",
    "with",
    # Outlet/aggregator boilerplate that often appears in RSS titles.
    "abc",
    "apnews",
    "associated",
    "bbc",
    "breaking",
    "cnn",
    "com",
    "exclusive",
    "investing",
    "latest",
    "live",
    "news",
    "npr",
    "photos",
    "press",
    "reuters",
    "update",
    "updates",
    "video",
    "watch",
}

SHORT_TOPIC_MATCH_STOPWORDS = TOPIC_MATCH_STOPWORDS | {
    "am",
    "do",
    "go",
    "he",
    "if",
    "so",
}

WEAK_TOPIC_MATCH_TERMS = {
    "attack",
    "bond",
    "business",
    "cash",
    "commodity",
    "crude",
    "currency",
    "drone",
    "energy",
    "gold",
    "market",
    "military",
    "oil",
    "price",
    "rate",
    "report",
    "stock",
    "strike",
    "trade",
    "war",
}

BOILERPLATE_CONTENT_STOPWORDS = {
    "about",
    "amp",
    "aria",
    "april",
    "august",
    "blank",
    "body",
    "class",
    "click",
    "com",
    "content",
    "css",
    "data",
    "december",
    "div",
    "february",
    "font",
    "friday",
    "google",
    "height",
    "href",
    "html",
    "http",
    "https",
    "img",
    "january",
    "july",
    "june",
    "link",
    "march",
    "monday",
    "nbsp",
    "noopener",
    "noreferrer",
    "november",
    "october",
    "px",
    "rel",
    "rss",
    "saturday",
    "script",
    "september",
    "share",
    "span",
    "src",
    "style",
    "sunday",
    "target",
    "text",
    "thursday",
    "tuesday",
    "utm",
    "wednesday",
    "width",
    "www",
}

FEED_DESCRIPTION_RELEVANCE_CHARS = 500


RUN_LOG_FILE: TextIO | None = None


def _clean_progress_message(message: str) -> str:
    clean = re.sub(r"^\[progress\]\s*", "", str(message or "").strip())
    clean = clean.replace("--- [EMAIL]:", "[email]").replace("--- [UNSUBSCRIBE]:", "[unsubscribe]")
    return clean


def _write_run_log(message: str) -> None:
    if RUN_LOG_FILE is None:
        return
    timestamp_label = datetime.now().isoformat(timespec="seconds")
    clean = _clean_progress_message(message).replace("\r", "\n").strip()
    if not clean:
        return
    for line in clean.splitlines():
        RUN_LOG_FILE.write(f"{timestamp_label} {line.rstrip()}\n")
    RUN_LOG_FILE.flush()


class ProgressTracker:
    STEP_ORDER = [
        "setup",
        "topics",
        "sources",
        "translation",
        "stories",
        "summaries",
        "model",
        "report",
        "finalize",
    ]
    STEP_LABELS = {
        "model": "model",
        "setup": "setup",
        "topics": "topics",
        "sources": "sources",
        "translation": "translation",
        "stories": "stories",
        "summaries": "summaries",
        "report": "report",
        "finalize": "finalize",
    }

    def __init__(self) -> None:
        self.current_step = ""
        self.meter_total = 0
        self.meter_done = 0
        self.meter_unit = ""
        self.last_render = ""
        self._line_active = False

    def step(self, step_key: str, message: str, *, log_detail: str | None = None) -> None:
        self.current_step = step_key
        self.meter_total = 0
        self.meter_done = 0
        self.meter_unit = ""
        self._finish_active_line()
        line = f"{self._step_prefix(step_key)} {message}"
        print(line)
        _write_run_log(line)
        if log_detail:
            self.detail(log_detail)

    def detail(self, message: str) -> None:
        _write_run_log(message)

    def log(self, message: str, *, terminal: bool = True) -> None:
        clean = _clean_progress_message(message)
        if terminal:
            self._finish_active_line()
            print(clean)
        _write_run_log(clean)

    def reset(self, *, total_sources: int) -> None:
        self.current_step = "sources"
        self.meter_done = 0
        self.meter_total = max(0, total_sources)
        self.meter_unit = "sources"
        if self.meter_total:
            self._render_meter(force=True)
        else:
            self.step("sources", "No source feeds configured.")

    def start_source(self, source_index: int, source_name: str | None = None) -> None:
        if self.current_step != "sources":
            self.current_step = "sources"
        self.meter_done = max(0, min(self.meter_total, source_index - 1))
        self._render_meter()
        if source_name:
            self.detail(f"Starting source {source_index}/{self.meter_total}: {source_name}")

    def set_source_article_total(self, total_articles: int) -> None:
        del total_articles

    def article_completed(self) -> None:
        if self.current_step == "summaries" and self.meter_total > 0:
            self.meter_done = min(self.meter_total, self.meter_done + 1)
            self._render_meter()

    def source_completed(self) -> None:
        if self.current_step != "sources":
            self.current_step = "sources"
        if self.meter_total > 0:
            self.meter_done = min(self.meter_total, self.meter_done + 1)
            self._render_meter()

    def start_article_summary(self, total_articles: int) -> None:
        self.current_step = "summaries"
        self.meter_done = 0
        self.meter_total = max(0, total_articles)
        self.meter_unit = "articles"
        if self.meter_total:
            self._render_meter(force=True)
        else:
            self.step("summaries", "No article summaries selected.")

    def retrying(self, task_name: str, attempt: int, attempts: int, delay: int) -> None:
        self.detail(
            f"Retrying {task_name}: attempt {attempt}/{attempts} failed; "
            f"sleeping {delay}s before the next attempt."
        )

    def warning(self, label: str) -> None:
        self.detail(f"WARNING: {label}")

    def set_final_step(self, step_name: str, step_index: int) -> None:
        del step_index
        messages = {
            "reports": "Preparing report inputs.",
            "synthesis": "Running final synthesis.",
            "art": "Generating report image.",
            "render": "Rendering report assets.",
            "email": "Sending report.",
        }
        self.step("report", messages.get(step_name, f"Running {step_name}."))

    def finish(self, label: str) -> None:
        del label
        self._finish_active_line()
        self.step("finalize", "Daily news run complete.")

    def _finish_active_line(self) -> None:
        if self._line_active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_active = False
            self.last_render = ""

    def _step_prefix(self, step_key: str | None = None) -> str:
        key = step_key or self.current_step or "setup"
        try:
            index = self.STEP_ORDER.index(key) + 1
        except ValueError:
            index = 0
        label = self.STEP_LABELS.get(key, key)
        if index:
            return f"[{index}/{len(self.STEP_ORDER)} {label}]"
        return f"[{label}]"

    def _render_meter(self, *, force: bool = False) -> None:
        if self.meter_total <= 0:
            return
        fill = round((self.meter_done / self.meter_total) * 20)
        fill = max(0, min(20, fill))
        bar = "#" * fill + "-" * (20 - fill)
        line = (
            f"{self._step_prefix()} [{bar}] "
            f"{self.meter_done}/{self.meter_total} {self.meter_unit}"
        )
        if not force and line == self.last_render:
            return
        if sys.stdout.isatty():
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
            self._line_active = True
        else:
            print(line)
        self.last_render = line
        _write_run_log(line)


progress_tracker = ProgressTracker()


def _article_summarization_runtime() -> article_summarization_stage.ArticleSummarizationRuntime:
    return article_summarization_stage.ArticleSummarizationRuntime(
        source_feeds=SOURCE_FEEDS,
        recent_window_hours=RECENT_WINDOW_HOURS,
        article_summary_concurrency=ARTICLE_SUMMARY_CONCURRENCY,
        article_summary_max_tokens=ARTICLE_SUMMARY_MAX_TOKENS,
        build_article_heading=_build_article_heading,
        format_article_metadata=_format_article_metadata,
        build_article_fallback_entry=build_article_fallback_entry,
        build_chat_model=build_chat_model,
        invoke_with_retries=invoke_with_retries,
        has_structured_entry=has_structured_entry,
        normalize_report_entry=normalize_report_entry,
        article_completed=progress_tracker.article_completed,
    )


def _story_drafting_runtime(
    *, min_articles_per_story: int | None = None
) -> story_drafting_stage.StoryDraftingRuntime:
    return story_drafting_stage.StoryDraftingRuntime(
        article_summary_concurrency=ARTICLE_SUMMARY_CONCURRENCY,
        final_synthesis_max_tokens=FINAL_SYNTHESIS_MAX_TOKENS,
        model_reference=MODEL_REFERENCE,
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        min_articles_per_story=min_articles_per_story if min_articles_per_story is not None else MIN_ARTICLES_PER_STORY,
        build_chat_model=build_chat_model,
        invoke_with_retries=invoke_with_retries,
        estimate_message_token_count=estimate_message_token_count,
        extract_prompt_tokens_from_response=extract_prompt_tokens_from_response,
        strip_prompt_echo_lines=_strip_prompt_echo_lines,
        strip_model_artifacts=strip_model_artifacts,
        is_low_coverage_synthesis_section=_is_low_coverage_synthesis_section,
        dev_synthesis_paragraph_from_summaries=_dev_synthesis_paragraph_from_summaries,
        final_synthesis_word_count=_final_synthesis_word_count,
    )


def _story_topic_runtime() -> story_topic_assignment_stage.StoryTopicRuntime:
    return story_topic_assignment_stage.StoryTopicRuntime(
        max_stories_per_topic=MAX_STORIES_PER_TOPIC,
        min_score=STORY_TOPIC_FIT_MIN_SCORE,
        diversity_min_distance=TOPIC_STORY_DIVERSITY_MIN_DISTANCE,
        model_max_input_tokens=MODEL_MAX_INPUT_TOKENS,
        model_profile_key=MODEL_PROFILE_KEY,
        model_reference=MODEL_REFERENCE,
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        relaxed_final_synthesis_guards=RELAXED_FINAL_SYNTHESIS_GUARDS,
        story_topic_validation_enabled=STORY_TOPIC_VALIDATION_ENABLED,
        build_chat_model=build_chat_model,
        invoke_with_retries=invoke_with_retries,
        build_article_heading=_build_article_heading,
        format_article_metadata=_format_article_metadata,
        format_topic_section_header=_format_topic_section_header,
        final_synthesis_word_count=_final_synthesis_word_count,
        is_low_confidence_report_entry=is_low_confidence_report_entry,
        report_reference_key=_report_reference_key,
    )


def run_article_summary_pass(article_targets: list[dict], topics: list[dict]) -> list[str]:
    return article_summarization_stage.run_article_summary_pass(
        article_targets, topics, _article_summarization_runtime()
    )


def run_per_story_synthesis(
    article_summaries: list[str],
    story_records: list[dict],
    topics: list[dict],
    *,
    min_articles_per_story: int | None = None,
) -> str:
    story_drafts, _ = story_drafting_stage.draft_story_clusters_from_article_summaries(
        story_records, article_summaries, _story_drafting_runtime(min_articles_per_story=min_articles_per_story)
    )
    if not story_drafts:
        return ""
    selected_matches, _ = story_topic_assignment_stage.classify_story_drafts_for_topics(
        story_drafts, topics, _story_topic_runtime()
    )
    final_synthesis, _, _ = story_topic_assignment_stage.build_precomputed_story_synthesis(
        selected_matches, topics, article_summaries, _story_topic_runtime()
    )
    return clean_synthesis_for_publication(final_synthesis, relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS)


@contextmanager
def run_logging():
    global RUN_LOG_FILE
    with open(RUN_LOG_PATH, "w", encoding="utf-8") as log_file:
        RUN_LOG_FILE = log_file
        log_file.write(
            "# Daily news run log\n"
            f"# Started: {RUN_STARTED_AT.isoformat(timespec='seconds')}\n"
            f"# Run mode: {RUN_MODE}\n\n"
        )
        log_file.flush()
        try:
            yield
        finally:
            _write_run_log(f"Run log saved: {RUN_LOG_PATH}")
            RUN_LOG_FILE = None


def load_recipient_config() -> dict[str, dict]:
    """Load active recipient metadata from config/recipients.yaml."""
    recipient_config = load_recipients(CONFIG.recipients_path)
    if not recipient_config:
        return {
            email: {"name": email, "pause": False}
            for email in EMAIL_RECIPIENTS_FALLBACK
        }
    return recipient_config


def _base64url_encode(raw_value: bytes) -> str:
    return base64.urlsafe_b64encode(raw_value).decode("ascii").rstrip("=")


def _base64url_decode(encoded_value: str) -> bytes:
    padding = "=" * (-len(encoded_value) % 4)
    return base64.urlsafe_b64decode((encoded_value + padding).encode("ascii"))


def _unsubscribe_signing_secret() -> bytes:
    secret = UNSUBSCRIBE_SECRET or SMTP_PASSWORD or EMAIL_FROM
    return secret.encode("utf-8")


def build_unsubscribe_token(recipient_email: str) -> str:
    payload = json.dumps(
        {"email": recipient_email.strip().lower()},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_part = _base64url_encode(payload)
    signature = hmac.new(
        _unsubscribe_signing_secret(),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_part}.{_base64url_encode(signature)}"


def parse_unsubscribe_token(token: str) -> str:
    try:
        payload_part, signature_part = (token or "").split(".", 1)
    except ValueError as error:
        raise ValueError("Malformed unsubscribe token.") from error

    expected_signature = hmac.new(
        _unsubscribe_signing_secret(),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    supplied_signature = _base64url_decode(signature_part)
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise ValueError("Invalid unsubscribe token signature.")

    payload = json.loads(_base64url_decode(payload_part).decode("utf-8"))
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Unsubscribe token did not include a valid email address.")
    return email


def build_unsubscribe_url(recipient_email: str) -> str:
    token = build_unsubscribe_token(recipient_email)
    separator = "&" if "?" in UNSUBSCRIBE_BASE_URL else "?"
    return f"{UNSUBSCRIBE_BASE_URL}{separator}{urlencode({'token': token})}"


def update_client_pause_setting(target_email: str, pause: bool = True) -> int:
    return update_recipient_pause_setting(
        target_email,
        pause=pause,
        path=CONFIG.recipients_path,
    )


def serve_unsubscribe_endpoint() -> None:
    class UnsubscribeHandler(BaseHTTPRequestHandler):
        def _send_html(self, status_code: int, body: str) -> None:
            encoded_body = body.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded_body)))
            self.end_headers()
            self.wfile.write(encoded_body)

        def _handle_request(self) -> None:
            parsed_url = urlparse(self.path)
            if parsed_url.path.rstrip("/") != "/unsubscribe":
                self._send_html(404, "<h1>Not found</h1>")
                return

            token = parse_qs(parsed_url.query).get("token", [""])[0]
            try:
                email = parse_unsubscribe_token(token)
                updated_count = update_client_pause_setting(email, pause=True)
            except Exception as error:
                self._send_html(
                    400,
                    "<h1>Unsubscribe failed</h1>"
                    f"<p>{html.escape(str(error))}</p>",
                )
                return

            if updated_count:
                self._send_html(
                    200,
                    "<h1>You are unsubscribed</h1>"
                    "<p>Your daily news email setting has been paused.</p>",
                )
            else:
                self._send_html(
                    404,
                    "<h1>Email not found</h1>"
                    "<p>No matching recipient was found in config/recipients.yaml.</p>",
                )

        def do_GET(self) -> None:
            self._handle_request()

        def do_POST(self) -> None:
            self._handle_request()

        def log_message(self, format: str, *args) -> None:
            progress_tracker.log(f"--- [UNSUBSCRIBE]: {format % args} ---")

    server = HTTPServer((UNSUBSCRIBE_HOST, UNSUBSCRIBE_PORT), UnsubscribeHandler)
    print(f"[unsubscribe] Listening on http://{UNSUBSCRIBE_HOST}:{UNSUBSCRIBE_PORT}/unsubscribe")
    server.serve_forever()


def get_active_recipient_config(recipient_config: dict[str, dict]) -> dict[str, dict]:
    if BRADLEY_ONLY_DELIVERY:
        preferred_config = recipient_config.get(BRADLEY_ONLY_RECIPIENT)
        if preferred_config:
            return {BRADLEY_ONLY_RECIPIENT: preferred_config}
        return {
            BRADLEY_ONLY_RECIPIENT: {
                "name": BRADLEY_ONLY_RECIPIENT,
                "pause": False,
            }
        }

    if recipient_config:
        return {
            email: settings
            for email, settings in recipient_config.items()
            if not settings.get("pause", False)
        }

    return {
        email: {"name": email, "pause": False}
        for email in EMAIL_RECIPIENTS_FALLBACK
    }


def _slugify_report_suffix(value: str) -> str:
    clean_value = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return clean_value or "report"


def _is_google_news_url(url: str | None) -> bool:
    raw_url = str(url or "").strip()
    if not raw_url:
        return False
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname == "news.google.com" or hostname.endswith(".news.google.com")


def _google_news_query_target(url: str) -> str:
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except Exception:
        return ""
    for key in ("url", "u"):
        for value in query.get(key, []):
            candidate = unquote(str(value or "").strip())
            if candidate.startswith(("http://", "https://")) and not _is_google_news_url(candidate):
                return candidate
    return ""


def _decode_google_news_article_path(url: str) -> str:
    """Decode modern Google News RSS article URLs (CBMi... base64 path encoding).

    Two encoding variants are handled:
    - URL directly encoded in the proto payload (older modern format)
    - AU_yqL secondary token (current AP/Reuters format) — resolved via Google's
      batchexecute API using the googlenewsdecoder package.

    HTTP redirects no longer work for these URLs; the path must be decoded.
    """
    try:
        article_id = urlparse(url).path.rstrip("/").split("/")[-1]
        if not article_id:
            return ""

        decoded_bytes = base64.urlsafe_b64decode(article_id + "==")
        decoded_str = decoded_bytes.decode("latin1")

        # Strip known proto header/footer bytes
        prefix = b"\x08\x13\x22".decode("latin1")
        if decoded_str.startswith(prefix):
            decoded_str = decoded_str[len(prefix):]
        suffix = b"\xd2\x01\x00".decode("latin1")
        if decoded_str.endswith(suffix):
            decoded_str = decoded_str[: -len(suffix)]

        # Extract the first length-prefixed string field
        bytes_array = bytearray(decoded_str, "latin1")
        if not bytes_array:
            return ""
        length = bytes_array[0]
        candidate = decoded_str[2 : length + 1] if length >= 0x80 else decoded_str[1 : length + 1]

        # Variant 1: URL is directly embedded
        if candidate.startswith(("http://", "https://")) and not _is_google_news_url(candidate):
            return candidate

        # Variant 2: AU_yqL secondary token — resolve via batchexecute API
        if candidate.startswith("AU_yqL"):
            try:
                from googlenewsdecoder import gnewsdecoder
                result = gnewsdecoder(url)
                if result.get("status"):
                    resolved = result.get("decoded_url", "")
                    if resolved and not _is_google_news_url(resolved):
                        return resolved
            except Exception:
                pass

    except Exception:
        pass
    return ""


def _resolve_google_news_url_details(url: str) -> dict[str, str]:
    """Follow Google News redirect links without treating Google pages as articles."""
    original_url = str(url or "").strip()
    if not original_url:
        return {
            "original_url": "",
            "resolved_url": "",
            "resolution_status": "missing_url",
        }
    if not _is_google_news_url(original_url):
        return {
            "original_url": original_url,
            "resolved_url": original_url,
            "resolution_status": "not_google_news",
        }

    query_target = _google_news_query_target(original_url)
    if query_target:
        return {
            "original_url": original_url,
            "resolved_url": query_target,
            "resolution_status": "google_news_resolved_query",
        }

    # Modern Google News RSS links encode the article URL in the base64 path
    # rather than using HTTP redirects. Decode it directly.
    decoded_target = _decode_google_news_article_path(original_url)
    if decoded_target:
        return {
            "original_url": original_url,
            "resolved_url": decoded_target,
            "resolution_status": "google_news_resolved_decode",
        }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://news.google.com/",
    }
    last_error = ""
    for method in ("GET", "HEAD"):
        try:
            response = requests.request(
                method,
                original_url,
                headers=headers,
                allow_redirects=True,
                timeout=15,
                stream=(method == "GET"),
            )
            resolved_url = response.url or original_url
            response.close()
            if resolved_url and not _is_google_news_url(resolved_url):
                return {
                    "original_url": original_url,
                    "resolved_url": resolved_url,
                    "resolution_status": f"google_news_resolved_{method.lower()}",
                }
        except Exception as error:
            last_error = str(error)

    details = {
        "original_url": original_url,
        "resolved_url": original_url,
        "resolution_status": "google_news_unresolved",
    }
    if last_error:
        details["resolution_error"] = last_error
    return details


def _resolve_google_news_url(url: str) -> str:
    """Follow Google News redirect to get the real article URL when possible."""
    return _resolve_google_news_url_details(url).get("resolved_url") or str(url or "").strip()


class ArticleScrapeTimeoutError(TimeoutError):
    """Raised when one article scrape exceeds the run's hard scrape deadline."""


@contextmanager
def _article_scrape_deadline(seconds: int):
    if seconds <= 0 or current_thread() is not main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _raise_timeout(_signum, _frame):
        raise ArticleScrapeTimeoutError(f"article scrape exceeded {seconds}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _download_article_html(url: str) -> str:
    response = requests.get(
        url,
        headers=ARTICLE_DOWNLOAD_HEADERS,
        timeout=ARTICLE_DOWNLOAD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


def scrape_article_text(
    url: str,
    *,
    source: str | None = None,
    title: str | None = None,
) -> tuple[str, str]:
    try:
        with _article_scrape_deadline(ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS):
            downloaded = _download_article_html(url)
            if downloaded:
                content = trafilatura.extract(downloaded, url=url)
                if content:
                    clean_content = _clean_article_text(content, source=source, url=url, title=title)
                    return (clean_content, "scraped") if clean_content else ("Scraper found no text.", "scraper_no_text")
                return "Scraper found no text.", "scraper_no_text"
            return "Access Denied.", "access_denied"
    except (ArticleScrapeTimeoutError, requests.Timeout):
        return "Scrape timed out.", "scrape_timeout"
    except requests.RequestException:
        return "Access Denied.", "access_denied"
    except Exception:
        return "Scrape Error.", "scrape_error"


def _build_feed_fallback_text(title: str | None, description: str | None) -> str:
    title_text = _clean_feed_text(title)
    description_text = _clean_feed_text(description)
    if description_text and title_text and description_text.lower().startswith(title_text.lower()):
        parts = [description_text]
    else:
        parts = [part for part in (title_text, description_text) if part]
    if not parts:
        return ""
    fallback_text = ". ".join(part.rstrip(".") for part in parts if part).strip()
    return fallback_text + ("." if fallback_text and fallback_text[-1] not in ".!?" else "")


def _resolve_and_scrape_feed_article(
    original_url: str,
    *,
    title: str | None,
    description: str | None,
    source: str | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    resolution = _resolve_google_news_url_details(original_url)
    resolved_url = resolution.get("resolved_url") or str(original_url or "").strip()
    fallback_text = _build_feed_fallback_text(title, description)

    if not resolved_url:
        return {
            **resolution,
            "text": fallback_text,
            "scrape_status": "missing_url",
            "feed_fallback_used": bool(fallback_text),
            "scrape_seconds": round(time.perf_counter() - started_at, 3),
        }

    if _is_google_news_url(resolved_url):
        return {
            **resolution,
            "resolved_url": resolved_url,
            "text": fallback_text,
            "scrape_status": "google_news_unresolved" if fallback_text else "google_news_unresolved_no_fallback",
            "feed_fallback_used": bool(fallback_text),
            "scrape_seconds": round(time.perf_counter() - started_at, 3),
        }

    article_text, scrape_status = scrape_article_text(
        resolved_url,
        source=source,
        title=title,
    )
    scrape_seconds = round(time.perf_counter() - started_at, 3)
    if scrape_status != "scraped":
        if fallback_text:
            return {
                **resolution,
                "resolved_url": resolved_url,
                "text": fallback_text,
                "scrape_status": f"{scrape_status}_feed_fallback",
                "feed_fallback_used": True,
                "scrape_seconds": scrape_seconds,
            }
        return {
            **resolution,
            "resolved_url": resolved_url,
            "text": "",
            "scrape_status": scrape_status,
            "feed_fallback_used": False,
            "scrape_seconds": scrape_seconds,
        }

    return {
        **resolution,
        "resolved_url": resolved_url,
        "text": article_text,
        "scrape_status": "scraped",
        "feed_fallback_used": False,
        "scrape_seconds": scrape_seconds,
    }

def _text_looks_non_english(text: str, title: str = "") -> bool:
    sample = (title + " " + text)[:300].strip()
    if not sample:
        return False
    ascii_letters = sum(1 for c in sample if c.isascii() and c.isalpha())
    total_letters = sum(1 for c in sample if c.isalpha())
    return bool(total_letters and ascii_letters / total_letters <= 0.7)


def _normalize_translation_language(value: str | None) -> str:
    return str(value or "").strip().replace("_", "-").lower()


def _infer_script_translation_language(text: str, title: str = "") -> str:
    sample = (title + " " + text)[:1000]
    if re.search(r"[\u0400-\u04ff]", sample):
        return "ru"
    if re.search(r"[\u0600-\u06ff]", sample):
        return "fa"
    if re.search(r"[\u0900-\u097f]", sample):
        return "hi"
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", sample):
        return "ja"
    return ""


def _article_translation_decision(
    source_name: str,
    *,
    title: str,
    text: str,
) -> dict[str, Any]:
    if not TRANSLATION_ENABLED:
        return {
            "needed": False,
            "reason": "translation_disabled",
            "source_language": None,
        }

    source_config = SOURCE_FEEDS.get(source_name) or {}
    source_language = _normalize_translation_language(source_config.get("language"))
    translation_source_language = _normalize_translation_language(
        source_config.get("translation_source_language") or source_language
    )
    source_requires_translation = bool(source_config.get("requires_translation"))

    if source_requires_translation and translation_source_language and translation_source_language != "en":
        return {
            "needed": True,
            "reason": "source_requires_translation",
            "source_language": translation_source_language,
            "retag_source": not bool(source_config.get("requires_translation_explicit")),
        }

    if _text_looks_non_english(text, title):
        inferred_language = (
            translation_source_language
            if translation_source_language and translation_source_language != "en"
            else _infer_script_translation_language(text, title)
        )
        return {
            "needed": bool(inferred_language),
            "reason": "detected_non_english_text",
            "source_language": inferred_language or None,
            "retag_source": True,
        }

    return {
        "needed": False,
        "reason": "looks_english",
        "source_language": translation_source_language or None,
    }


def _with_translation_metadata(
    record: dict[str, Any],
    *,
    source_name: str,
    title: str,
    text: str,
) -> dict[str, Any]:
    decision = _article_translation_decision(source_name, title=title, text=text)
    status = "pending" if decision.get("needed") else "not_needed"
    return {
        **record,
        "translation_needed": bool(decision.get("needed")),
        "translation_status": status,
        "translation_reason": decision.get("reason"),
        "translation_source_language": decision.get("source_language"),
        "translation_target_language": TRANSLATION_TARGET_LANGUAGE,
        "translation_model": TRANSLATION_MODEL_NAME,
        "translation_retag_source": bool(decision.get("retag_source")),
    }


def _translation_response_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _translation_messages(text: str, source_language: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "source_lang_code": source_language,
                    "target_lang_code": TRANSLATION_TARGET_LANGUAGE,
                    "text": text[:5000],
                }
            ],
        }
    ]


def _translation_payload(text: str, source_language: str) -> dict[str, Any]:
    return {
        "model": TRANSLATION_MODEL_NAME,
        "messages": _translation_messages(text, source_language),
        "max_tokens": TRANSLATION_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
    }


def _format_translation_prompt(processor: Any, text: str, source_language: str) -> str:
    messages = _translation_messages(text, source_language)
    try:
        prompt = processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
        )
    except TypeError:
        prompt = processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
        )
    return str(prompt)


def _load_translation_model_resources() -> tuple[Any, Any, Any]:
    global TRANSLATION_MODEL_RESOURCES
    ensure_codex_safe_model_reference(TRANSLATION_MODEL_REFERENCE)
    if TRANSLATION_MODEL_RESOURCES is not None:
        return TRANSLATION_MODEL_RESOURCES
    if TRANSLATION_MODEL_BACKEND != "mlx-vlm":
        raise RuntimeError(
            "TranslateGemma must use the mlx-vlm backend for direct structured prompting. "
            f"Configured backend: {TRANSLATION_MODEL_BACKEND}"
        )

    try:
        from mlx_vlm import generate as mlx_vlm_generate
        from mlx_vlm import load as mlx_vlm_load
    except Exception as error:
        raise RuntimeError(f"Could not import mlx-vlm for translation: {error}") from error

    progress_tracker.step("translation", "Loading translation model.")
    progress_tracker.detail(f"Translation model: {TRANSLATION_MODEL_REFERENCE} -> {TRANSLATION_MODEL_NAME}")
    model, processor = mlx_vlm_load(TRANSLATION_MODEL_NAME)
    TRANSLATION_MODEL_RESOURCES = (model, processor, mlx_vlm_generate)
    return TRANSLATION_MODEL_RESOURCES


def _unload_translation_model_resources() -> None:
    global TRANSLATION_MODEL_RESOURCES
    if TRANSLATION_MODEL_RESOURCES is None:
        return
    TRANSLATION_MODEL_RESOURCES = None
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass


def _generate_translation_text(
    text: str,
    source_language: str,
    *,
    max_tokens: int | None = None,
) -> str:
    model, processor, generate_fn = _load_translation_model_resources()
    prompt = _format_translation_prompt(processor, text, source_language)
    result = generate_fn(
        model,
        processor,
        prompt,
        max_tokens=max_tokens or TRANSLATION_MAX_TOKENS,
        temperature=0,
        verbose=False,
        skip_special_tokens=True,
    )
    return strip_model_artifacts(str(getattr(result, "text", result))).strip()


def _translate_text_with_translation_model(text: str, source_language: str, title: str) -> str:
    source_language = _normalize_translation_language(source_language)
    if not text.strip() or not source_language:
        return text

    with MODEL_CALL_STATS_LOCK:
        calls = MODEL_CALL_STATS.setdefault("calls", {})
        calls["translation"] = int(calls.get("translation", 0)) + 1

    last_error: Exception | None = None
    for attempt in range(1, MODEL_RETRY_ATTEMPTS + 1):
        try:
            translated = _generate_translation_text(text, source_language)
            return translated if translated else text
        except Exception as error:
            last_error = error
            if attempt >= MODEL_RETRY_ATTEMPTS:
                break
            delay = MODEL_RETRY_BASE_DELAY_SECONDS * attempt
            progress_tracker.retry(
                "translation",
                attempt,
                MODEL_RETRY_ATTEMPTS,
                delay,
                error,
            )
            time.sleep(delay)

    with MODEL_CALL_STATS_LOCK:
        MODEL_CALL_STATS["fallbacks"] = int(MODEL_CALL_STATS.get("fallbacks", 0)) + 1
        failures = MODEL_CALL_STATS.setdefault("failures", {})
        failures["translation"] = str(last_error or "unknown translation error")
    progress_tracker.warning(f"Translation failed; using original text: {title[:56]}")
    return text


def translate_article_candidates(
    articles: list[dict],
    diagnostics: RunDiagnostics | None = None,
) -> list[dict]:
    if not TRANSLATION_ENABLED:
        if diagnostics is not None:
            diagnostics.event(
                "translation",
                candidate_count=len(articles),
                translated_count=0,
                skipped=True,
                reason="translation_disabled",
            )
        return articles

    translation_targets = [
        article
        for article in articles
        if article.get("translation_needed") and article.get("translation_source_language")
    ]
    skipped_unknown_language = [
        article
        for article in articles
        if article.get("translation_needed") and not article.get("translation_source_language")
    ]
    if skipped_unknown_language:
        progress_tracker.warning(
            f"Skipping {len(skipped_unknown_language)} translation candidate(s) with unknown source language."
        )

    if not translation_targets:
        if diagnostics is not None:
            diagnostics.event(
                "translation",
                candidate_count=len(articles),
                translated_count=0,
                skipped_unknown_language=len(skipped_unknown_language),
            )
        return articles

    progress_tracker.step(
        "translation",
        f"Translating {len(translation_targets)} article candidate(s).",
        log_detail=(
            f"Translation model: {TRANSLATION_MODEL_REFERENCE} -> {TRANSLATION_MODEL_NAME}; "
            f"target language: {TRANSLATION_TARGET_LANGUAGE}"
        ),
    )

    translated_by_id: dict[int, dict[str, Any]] = {}
    retag_sources: dict[str, str | None] = {}
    try:
        for index, article in enumerate(translation_targets, start=1):
            title = str(article.get("title") or "")
            source_language = str(article.get("translation_source_language") or "")
            progress_tracker.detail(
                f"  [{index}/{len(translation_targets)}] Translating {title[:80] or article.get('url')}"
            )
            translated_text = _translate_text_with_translation_model(
                str(article.get("text") or ""),
                source_language,
                title,
            )
            translated_article = {
                **article,
                "translation_original_text_preview": str(article.get("text") or "")[:300],
                "text": translated_text,
                "translation_status": "translated" if translated_text != article.get("text") else "unchanged",
            }
            translated_by_id[id(article)] = translated_article
            if article.get("translation_retag_source"):
                retag_sources[str(article.get("source") or "")] = source_language
    finally:
        _unload_translation_model_resources()

    if retag_sources:
        try:
            written = write_source_translation_flags(CONFIG.sources_path, retag_sources)
            if written:
                progress_tracker.detail(
                    f"Retagged {len(retag_sources)} source(s) in {_display_config_path(CONFIG.sources_path)} "
                    "as requiring translation."
                )
        except Exception as error:
            progress_tracker.warning(f"Could not retag translation sources: {error}")

    translated_articles = [
        translated_by_id.get(id(article), article)
        for article in articles
    ]
    if diagnostics is not None:
        diagnostics.event(
            "translation",
            candidate_count=len(articles),
            translated_count=len(translated_by_id),
            skipped_unknown_language=len(skipped_unknown_language),
            retagged_sources=sorted(retag_sources),
            model=TRANSLATION_MODEL_REFERENCE,
            model_name=TRANSLATION_MODEL_NAME,
            target_language=TRANSLATION_TARGET_LANGUAGE,
        )
    return translated_articles


def _extract_sentences(text: str, limit: int = 5) -> List[str]:
    clean_text = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean_text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    return [sentence.strip() for sentence in sentences if sentence.strip()][:limit]


def _build_article_heading(article: dict) -> str:
    title = (article.get("title") or "Untitled article").strip()
    return re.sub(r"\s+", " ", title)


def _format_article_metadata(article: dict) -> str:
    metadata_lines = [
        f"- Source: {article.get('source') or 'Unknown source'}",
        f"- Published: {article.get('pub_date') or 'Unknown publish time'}",
        f"- URL: {article.get('url') or 'N/A'}",
    ]
    if article.get("article_id"):
        metadata_lines.append(f"- Article ID: {article.get('article_id')}")
    if article.get("topic_title"):
        metadata_lines.append(f"- Topic: {article.get('topic_title')}")
    if article.get("story_title"):
        metadata_lines.append(f"- Story: {article.get('story_title')}")
    return "\n".join(metadata_lines)


def build_article_fallback_entry(article: dict) -> str:
    sentences = _extract_sentences(article.get("text", ""), limit=5)
    summary = " ".join(sentences).strip()
    if not summary:
        summary = (
            "No reliable summary generated because the article was retrieved but the model "
            "connection failed before a synthesis could be produced."
        )
    return (
        "DATABASE_ENTRY:\n"
        f"### {_build_article_heading(article)}\n"
        "Metadata:\n"
        f"{_format_article_metadata(article)}\n\n"
        "Summary:\n"
        f"{summary}"
    )


def _parse_feed_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = parsedate_to_datetime(raw_value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _extract_feed_items(feed_xml: str) -> List[dict]:
    soup = BeautifulSoup(feed_xml, "xml")
    items: List[dict] = []

    for item in soup.find_all(["item", "entry"]):
        title = _clean_feed_text(item.title.get_text(" ", strip=True) if item.title else "")
        if item.link and item.link.get("href"):
            link = _clean_feed_url(item.link.get("href", ""))
        elif item.link:
            link = _clean_feed_url(item.link.get_text(" ", strip=True))
        else:
            link = ""
        description_node = item.description or item.summary
        description = ""
        if description_node:
            description = _clean_feed_text(description_node.get_text(" ", strip=True))
        pub_date = ""
        if item.pubDate:
            pub_date = _clean_feed_text(item.pubDate.get_text(" ", strip=True))
        elif item.published:
            pub_date = _clean_feed_text(item.published.get_text(" ", strip=True))
        source = ""
        if item.source:
            source = _clean_feed_text(item.source.get_text(" ", strip=True))

        if _is_excluded_feed_item(title, source, link):
            continue

        if title or link:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "pub_date": pub_date,
                    "published_at": _parse_feed_datetime(pub_date),
                    "source": source,
                }
            )

    return items


def _is_within_recent_window(published_at: datetime | None, now_utc: datetime) -> bool:
    if published_at is None:
        return True
    return published_at >= now_utc - timedelta(hours=RECENT_WINDOW_HOURS)


# --- DYNAMIC TOP-OF-DAY TOPIC DISCOVERY ---

def _strip_reddit_link_flair(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


def _normalize_url_for_dedupe(url: str) -> str:
    """Loose URL canonicalization for cross-provider dedupe of the same article.
    Drops scheme, leading 'www.', trailing slash, fragments, and tracking params."""
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^www\.", "", raw, flags=re.IGNORECASE)
    raw = raw.split("#", 1)[0]
    if "?" in raw:
        base, query = raw.split("?", 1)
        kept = [
            kv for kv in query.split("&")
            if kv and not re.match(r"^(utm_|gclid|fbclid|mc_cid|mc_eid|ref|ref_src|cmpid|cmp|igshid)", kv, flags=re.IGNORECASE)
        ]
        raw = base + ("?" + "&".join(kept) if kept else "")
    raw = raw.rstrip("/")
    return raw.lower()


def _provider_public_metadata(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": provider.get("key"),
        "name": provider.get("name"),
        "region": provider.get("region"),
        "frame": provider.get("frame"),
        "provider_type": provider.get("provider_type"),
        "intended_role": provider.get("intended_role"),
        "weight": provider.get("weight", 1.0),
        "can_seed_topics": bool(provider.get("can_seed_topics")),
        "can_validate_topics": bool(provider.get("can_validate_topics")),
        "can_enrich_coverage": bool(provider.get("can_enrich_coverage")),
    }


def _story_provider_detail(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": provider.get("key"),
        "name": provider.get("name"),
        "region": provider.get("region"),
        "frame": provider.get("frame"),
        "provider_type": provider.get("provider_type"),
        "intended_role": provider.get("intended_role"),
        "weight": float(provider.get("weight") or 1.0),
    }


def _attach_provider_metadata(story: dict, provider: dict[str, Any]) -> dict:
    story_record = dict(story)
    detail = _story_provider_detail(provider)
    story_record["provider"] = provider.get("key")
    story_record["provider_name"] = provider.get("name")
    story_record["provider_type"] = provider.get("provider_type")
    story_record["region"] = provider.get("region")
    story_record["frame"] = provider.get("frame")
    story_record["provider_weight"] = float(provider.get("weight") or 1.0)
    story_record["provider_detail"] = detail
    return story_record


def fetch_reddit_top_stories(
    provider: dict[str, Any],
    limit: int = 10,
) -> List[dict]:
    """Pull r/news top-of-day. Returns a list of {title, url, num_comments, score, created_utc, domain, provider}.
    This is the literal 'first N centerpiece links on the r/news Top/Today page' approach."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        resp = requests.get(str(provider.get("url") or ""), headers=headers, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as error:
        progress_tracker.warning(f"{provider.get('name') or provider.get('key')} top fetch failed: {error}")
        return []

    children = (payload or {}).get("data", {}).get("children", [])
    stories: list[dict] = []
    for child in children:
        data = (child or {}).get("data") or {}
        title = _strip_reddit_link_flair(data.get("title", ""))
        external_url = data.get("url_overridden_by_dest") or data.get("url") or ""
        if not title:
            continue
        # Skip self/text posts that aren't external article links.
        if data.get("is_self"):
            continue
        if _is_excluded_news_source(data.get("domain"), external_url):
            continue
        stories.append(
            _attach_provider_metadata(
                {
                    "title": title,
                    "url": external_url,
                    "num_comments": int(data.get("num_comments") or 0),
                    "score": int(data.get("score") or 0),
                    "created_utc": float(data.get("created_utc") or 0),
                    "domain": str(data.get("domain") or ""),
                },
                provider,
            )
        )
        if len(stories) >= limit:
            break
    return stories


def fetch_rss_top_stories(provider: dict[str, Any], limit: int = 10) -> List[dict]:
    """Pull the first N items from a top-stories RSS feed."""
    headers = {"User-Agent": USER_AGENT}
    warning_label = provider.get("name") or provider.get("key") or "RSS provider"
    try:
        resp = requests.get(str(provider.get("url") or ""), headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as error:
        progress_tracker.warning(f"{warning_label} top fetch failed: {error}")
        return []

    items = _extract_feed_items(resp.text)
    stories: list[dict] = []
    for item in items[:limit]:
        title = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        if not title or not link:
            continue
        published_at = item.get("published_at")
        created_ts = 0.0
        if isinstance(published_at, datetime):
            created_ts = published_at.replace(tzinfo=timezone.utc).timestamp()
        # Google News titles often look like "Some headline - Outlet Name".
        domain = ""
        if " - " in title:
            tail = title.rsplit(" - ", 1)[-1].strip()
            domain = tail.lower().replace(" ", "")
        stories.append(
            _attach_provider_metadata(
                {
                    "title": title,
                    "url": link,
                    "description": item.get("description", ""),
                    "pub_date": item.get("pub_date", ""),
                    "num_comments": 0,
                    "score": 0,
                    "created_utc": created_ts,
                    "domain": domain,
                },
                provider,
            )
        )
    return stories


def fetch_top_funnel_provider_stories(provider: dict[str, Any], limit: int = 10) -> List[dict]:
    fetcher = str(provider.get("fetcher") or "rss").strip().lower()
    if fetcher in {"reddit", "reddit_top", "reddit_top_json"}:
        return fetch_reddit_top_stories(provider, limit=limit)
    return fetch_rss_top_stories(provider, limit=limit)


def _merge_top_funnel_stories(provider_stories: dict[str, list[dict]]) -> list[dict]:
    merged: list[dict] = []
    by_url: dict[str, dict] = {}
    by_title: dict[str, dict] = {}

    interleaved: list[dict] = []
    max_len = max((len(stories) for stories in provider_stories.values()), default=0)
    for i in range(max_len):
        for stories in provider_stories.values():
            if i < len(stories):
                interleaved.append(stories[i])

    for story in interleaved:
        url_key = _normalize_url_for_dedupe(story.get("url", ""))
        title_key = re.sub(r"\s+", " ", (story.get("title") or "").lower()).strip()
        existing = None
        if url_key and url_key in by_url:
            existing = by_url[url_key]
        elif title_key and title_key in by_title:
            existing = by_title[title_key]

        if existing is not None:
            providers = existing.setdefault("providers", [existing.get("provider", "")])
            if story.get("provider") and story["provider"] not in providers:
                providers.append(story["provider"])
            provider_details = existing.setdefault("provider_details", [])
            detail = story.get("provider_detail")
            if detail and not any(d.get("key") == detail.get("key") for d in provider_details):
                provider_details.append(detail)
            frames = existing.setdefault("frames", [])
            if story.get("frame") and story["frame"] not in frames:
                frames.append(story["frame"])
            # Add this provider's score so cross-provider matches naturally rank higher.
            existing["score"] = int(existing.get("score") or 0) + int(story.get("score") or 0)
            existing["num_comments"] = int(existing.get("num_comments") or 0) + int(story.get("num_comments") or 0)
            existing["provider_weight_total"] = float(existing.get("provider_weight_total") or 0.0) + float(story.get("provider_weight") or 1.0)
            continue

        story_record = dict(story)
        story_record["providers"] = [story.get("provider", "")] if story.get("provider") else []
        story_record["provider_details"] = [story.get("provider_detail")] if story.get("provider_detail") else []
        story_record["frames"] = [story.get("frame")] if story.get("frame") else []
        story_record["provider_weight_total"] = float(story.get("provider_weight") or 1.0)
        merged.append(story_record)
        if url_key:
            by_url[url_key] = story_record
        if title_key:
            by_title[title_key] = story_record

    # Re-sort so cross-provider hits float up. Triangulation = headlines surfaced by 2+
    # providers are more likely to be the actual top topics of the day.
    merged.sort(
        key=lambda s: (
            len(s.get("providers", [])),
            float(s.get("provider_weight_total") or 0.0),
            int(s.get("score") or 0),
            int(s.get("num_comments") or 0),
        ),
        reverse=True,
    )

    return merged


def discover_top_stories_of_day(per_provider_limit: int = TOP_OF_FUNNEL_PER_PROVIDER) -> dict[str, Any]:
    """Fetch configured top-of-funnel providers and keep seed, validation, and enrichment roles separate."""
    global LAST_TOP_FUNNEL_PROVIDER_STORIES, LAST_TOP_FUNNEL_PROVIDER_METADATA

    provider_stories: dict[str, list[dict]] = {}
    provider_metadata: dict[str, dict] = {}
    for key, provider in TOP_FUNNEL_PROVIDERS.items():
        stories = fetch_top_funnel_provider_stories(provider, limit=per_provider_limit)
        provider_stories[key] = stories
        provider_metadata[key] = _provider_public_metadata(provider)

    LAST_TOP_FUNNEL_PROVIDER_STORIES = provider_stories
    LAST_TOP_FUNNEL_PROVIDER_METADATA = provider_metadata

    progress_tracker.detail(
        "Top-funnel provider counts: "
        + ", ".join(f"{key}={len(stories)}" for key, stories in provider_stories.items())
    )

    seed_provider_stories = {
        key: stories
        for key, stories in provider_stories.items()
        if TOP_FUNNEL_PROVIDERS.get(key, {}).get("can_seed_topics")
    }
    validation_provider_stories = {
        key: stories
        for key, stories in provider_stories.items()
        if TOP_FUNNEL_PROVIDERS.get(key, {}).get("can_validate_topics")
    }
    return {
        "providers": provider_stories,
        "provider_metadata": provider_metadata,
        "all_stories": _merge_top_funnel_stories(provider_stories),
        "seed_stories": _merge_top_funnel_stories(seed_provider_stories),
        "validation_stories": _merge_top_funnel_stories(validation_provider_stories),
    }


def _topic_payload_for_llm(stories: List[dict], num_topics: int, probes_each: int) -> str:
    # We only feed titles + (truncated) probes; the LLM clusters and labels.
    truncated = stories[: max(num_topics * probes_each * 2, 25)]
    lines: list[str] = []
    for index, story in enumerate(truncated, start=1):
        title = (story.get("title") or "").strip()
        domain = (story.get("domain") or "").strip()
        providers = story.get("providers") or ([story.get("provider")] if story.get("provider") else [])
        providers = [p for p in providers if p]
        frames = [f for f in (story.get("frames") or [story.get("frame")]) if f]
        annotations: list[str] = []
        if domain:
            annotations.append(domain)
        if providers:
            annotations.append("via " + "+".join(providers))
        if frames:
            annotations.append("frame " + "+".join(sorted(set(frames))))
        suffix = f" [{' | '.join(annotations)}]" if annotations else ""
        lines.append(f"{index}. {title}{suffix}")
    return "\n".join(lines)


def _safe_json_extract(text: str) -> str:
    """Pull the first balanced JSON array or object out of LLM output."""
    if not text:
        return ""
    text = strip_model_artifacts(text)
    # Strip markdown code fences if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1)
    # Find first [...] or {...} block, balanced.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return text.strip()


def _normalize_topic_key(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return slug or "topic"


def _clean_topic_source_title(title: str) -> str:
    """Remove common aggregator source suffixes without trying to rewrite the headline."""
    clean_title = re.sub(r"\s+", " ", (title or "").strip())
    if " - " in clean_title:
        clean_title = clean_title.rsplit(" - ", 1)[0].strip()
    return clean_title


def _compact_dotted_acronyms(text: str) -> str:
    return re.sub(
        r"\b(?:[A-Za-z]\.){2,}",
        lambda match: match.group(0).replace(".", ""),
        text,
    )


def _normalize_topic_token(token: str) -> str:
    normalized = token.lower().replace("&", "").strip("'")
    if normalized.endswith("'s"):
        normalized = normalized[:-2]
    if normalized == "hits":
        normalized = "hit"
    elif len(normalized) > 4 and normalized.endswith("ies"):
        normalized = normalized[:-3] + "y"
    elif (
        len(normalized) > 4
        and normalized.endswith("s")
        and not normalized.endswith(("ss", "virus"))
    ):
        normalized = normalized[:-1]
    return normalized


def _ordered_topic_match_terms(
    *values: Any,
    allowed_short_terms: set[str] | None = None,
    collect_short_terms: bool = False,
) -> list[str]:
    text = " ".join(str(value or "") for value in values)
    text = _compact_dotted_acronyms(text)
    allowed_short_terms = allowed_short_terms or set()
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9](?:[a-z0-9'&-]*[a-z0-9])?", text.lower()):
        token_variants = [token]
        if "-" in token:
            token_variants.extend(part for part in token.split("-") if part)
        for variant in token_variants:
            normalized = _normalize_topic_token(variant)
            is_short = len(normalized) < 3
            if is_short and normalized in SHORT_TOPIC_MATCH_STOPWORDS:
                continue
            if (
                (is_short and not collect_short_terms and normalized not in allowed_short_terms)
                or normalized.isdigit()
                or normalized in TOPIC_MATCH_STOPWORDS
            ):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
    return terms


def _topic_match_terms(
    *values: Any,
    allowed_short_terms: set[str] | None = None,
    collect_short_terms: bool = False,
) -> set[str]:
    return set(
        _ordered_topic_match_terms(
            *values,
            allowed_short_terms=allowed_short_terms,
            collect_short_terms=collect_short_terms,
        )
    )


def _topic_vocabulary_values(topic: dict) -> tuple[Any, ...]:
    return (
        topic.get("title"),
        topic.get("rationale"),
        " ".join(str(k) for k in topic.get("keywords", [])),
        " ".join(str(p) for p in topic.get("boost_phrases", [])),
    )


def _topic_allowed_short_match_terms(topic: dict) -> set[str]:
    return {
        term
        for term in _topic_match_terms(*_topic_vocabulary_values(topic), collect_short_terms=True)
        if len(term) < 3
    }


def _provider_keys_for_story(story: dict) -> set[str]:
    providers = set(str(provider or "") for provider in (story.get("providers") or []) if provider)
    provider = str(story.get("provider") or "")
    if provider:
        providers.add(provider)
    for detail in story.get("provider_details") or []:
        if isinstance(detail, dict) and detail.get("key"):
            providers.add(str(detail["key"]))
    if story.get("provider_detail") and isinstance(story.get("provider_detail"), dict):
        detail = story["provider_detail"]
        if detail.get("key"):
            providers.add(str(detail["key"]))
    return {provider for provider in providers if provider}


def _topic_provider_support_count(topic: dict) -> int:
    providers = set(str(provider or "") for provider in (topic.get("seed_providers") or []) if provider)
    providers.update(str(provider or "") for provider in (topic.get("validation_providers") or []) if provider)
    return len({provider for provider in providers if provider})


def _is_fallback_topic(topic: dict) -> bool:
    source = str(topic.get("topic_source") or "").lower()
    rationale = str(topic.get("rationale") or "").lower()
    return source.startswith("fallback") or "fallback topic" in rationale


def _topic_phrase_required_overlap(term_count: int) -> int:
    if term_count <= 2:
        return term_count
    return max(2, (term_count * 3 + 3) // 4)


def _normalize_phrase_match_text(value: str) -> str:
    compact = _compact_dotted_acronyms(str(value or "")).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", compact)).strip()


def _text_has_normalized_phrase(text: str, phrase: str) -> bool:
    normalized_text = _normalize_phrase_match_text(text)
    normalized_phrase = _normalize_phrase_match_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    return f" {normalized_phrase} " in f" {normalized_text} "


def _topic_has_required_context(topic: dict, text: str) -> bool:
    required_terms = [
        str(term or "").strip()
        for term in (topic.get("required_context_terms") or [])
        if str(term or "").strip()
    ]
    if not required_terms:
        return True
    return any(_text_has_normalized_phrase(text, term) for term in required_terms)


def _score_topic_text_match(topic: dict, text: str) -> int:
    allowed_short_terms = _topic_allowed_short_match_terms(topic)
    text_terms = _topic_match_terms(text, allowed_short_terms=allowed_short_terms)
    if not text_terms:
        return 0

    matched_terms: set[str] = set()
    phrase_score = 0
    boost_score = 0

    for keyword in topic.get("keywords", []) or []:
        keyword_terms = _topic_match_terms(keyword, allowed_short_terms=allowed_short_terms)
        if not keyword_terms:
            continue
        overlap = keyword_terms & text_terms
        if len(keyword_terms) == 1:
            if overlap:
                matched_terms.update(overlap)
            continue
        if len(overlap) >= _topic_phrase_required_overlap(len(keyword_terms)):
            matched_terms.update(overlap)
            phrase_score += 2 + len(overlap)

    for phrase in topic.get("boost_phrases", []) or []:
        phrase_terms = _topic_match_terms(phrase, allowed_short_terms=allowed_short_terms)
        if len(phrase_terms) < 2:
            continue
        overlap = phrase_terms & text_terms
        if len(overlap) >= _topic_phrase_required_overlap(len(phrase_terms)):
            matched_terms.update(overlap)
            boost_score += 4

    strict_score = (len(matched_terms) * 2) + phrase_score + boost_score
    lenient_score = _lenient_topic_overlap_score(topic, text)
    score = max(strict_score, lenient_score)
    if score <= 0:
        return 0

    topic_terms = _topic_match_terms(
        *_topic_vocabulary_values(topic),
        allowed_short_terms=allowed_short_terms,
    )
    overlap_terms = topic_terms & text_terms
    strong_overlap = overlap_terms - WEAK_TOPIC_MATCH_TERMS
    if not strong_overlap and phrase_score <= 0 and boost_score <= 0:
        return 0
    if not _topic_has_required_context(topic, text):
        return 0
    return score


def _lenient_topic_overlap_score(topic: dict, text: str) -> int:
    allowed_short_terms = _topic_allowed_short_match_terms(topic)
    topic_terms = _topic_match_terms(
        *_topic_vocabulary_values(topic),
        allowed_short_terms=allowed_short_terms,
    )
    text_terms = _topic_match_terms(text, allowed_short_terms=allowed_short_terms)
    overlap = topic_terms & text_terms
    required_overlap = 4 if _is_fallback_topic(topic) else 3
    if len(overlap) < required_overlap:
        return 0
    return len(overlap)


def _fallback_topics_from_stories(
    stories: List[dict],
    num_topics: int,
    *,
    start_index: int = 1,
) -> list[dict]:
    topics: list[dict] = []
    for offset, story in enumerate(stories[:num_topics], start=0):
        title = _clean_topic_source_title(story.get("title") or "")
        if not title:
            continue
        keywords = _ordered_topic_match_terms(title)[:12]
        if not keywords:
            continue
        index = start_index + offset
        topics.append(
            {
                "key": f"topic_{index:02d}_{_normalize_topic_key(title)}",
                "title": title[:80],
                "rationale": "Auto-generated fallback topic from a top-of-day seed headline.",
                "keywords": keywords,
                "boost_phrases": [title.lower()[:80]],
                "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
                "min_score": 4,
                "topic_source": "fallback_seed_headline",
            }
        )
    return topics


def _fallback_cluster_match_score(anchor_terms: set[str], candidate_terms: set[str]) -> int:
    if not anchor_terms or not candidate_terms:
        return 0
    overlap = anchor_terms & candidate_terms
    if len(overlap) >= 3:
        return len(overlap)
    if (
        len(overlap) >= 2
        and (len(overlap) / max(1, min(len(anchor_terms), len(candidate_terms)))) >= 0.25
        and any(len(term) >= 6 for term in overlap)
    ):
        return len(overlap)
    return 0


def _cluster_supported_fallback_topics(
    stories: list[dict],
    num_topics: int,
    *,
    start_index: int = 1,
    required_provider_count: int = 2,
) -> list[dict]:
    """Build deterministic fallback topics only from headlines echoed by 2+ providers."""
    if not stories or num_topics <= 0:
        return []

    story_terms = [_topic_match_terms(story.get("title"), story.get("description")) for story in stories]
    used_story_indexes: set[int] = set()
    topics: list[dict] = []

    for anchor_index, anchor in enumerate(stories):
        if len(topics) >= num_topics:
            break
        if anchor_index in used_story_indexes:
            continue
        anchor_title = _clean_topic_source_title(anchor.get("title") or "")
        anchor_terms = story_terms[anchor_index]
        if not anchor_title or len(anchor_terms) < 2:
            continue

        cluster: list[tuple[int, int, dict]] = []
        provider_keys: set[str] = set()
        for candidate_index, candidate in enumerate(stories):
            candidate_terms = story_terms[candidate_index]
            score = (
                len(anchor_terms)
                if candidate_index == anchor_index
                else _fallback_cluster_match_score(anchor_terms, candidate_terms)
            )
            if score <= 0:
                continue
            cluster.append((score, candidate_index, candidate))
            provider_keys.update(_provider_keys_for_story(candidate))

        if len(provider_keys) < required_provider_count:
            continue

        cluster.sort(
            key=lambda item: (
                item[0],
                len(_provider_keys_for_story(item[2])),
                -item[1],
            ),
            reverse=True,
        )
        term_counts: Counter[str] = Counter()
        for _, _, story in cluster:
            term_counts.update(_topic_match_terms(story.get("title"), story.get("description")))
        keywords = [
            term
            for term, _ in term_counts.most_common(14)
            if term not in TOPIC_MATCH_STOPWORDS
        ][:12]
        if len(keywords) < 2:
            continue

        boost_phrases = []
        seen_phrases: set[str] = set()
        for _, _, story in cluster:
            phrase = _clean_topic_source_title(story.get("title") or "").lower()[:90]
            if not phrase or phrase in seen_phrases:
                continue
            seen_phrases.add(phrase)
            boost_phrases.append(phrase)
            if len(boost_phrases) >= 3:
                break

        topic_index = start_index + len(topics)
        topics.append(
            {
                "key": f"topic_{topic_index:02d}_{_normalize_topic_key(anchor_title)}",
                "title": anchor_title[:80],
                "rationale": (
                    "Auto-generated fallback topic from a top-of-day seed headline "
                    "with matching support from multiple top-of-funnel providers."
                ),
                "keywords": keywords,
                "boost_phrases": boost_phrases,
                "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
                "min_score": 4,
                "topic_source": "fallback_cross_provider",
                "fallback_provider_support": sorted(provider_keys),
            }
        )
        for _, candidate_index, _ in cluster:
            used_story_indexes.add(candidate_index)

    return topics


def llm_cluster_top_topics(stories: List[dict], num_topics: int, probes_each: int) -> List[dict]:
    """
    Ask the LLM to (a) cluster the day's top headlines into N topics, and
    (b) for each topic, produce a keyword list and boost-phrase list it can
    use to recognize related articles in our union of sources.
    """
    if not stories:
        return []

    headline_block = _topic_payload_for_llm(stories, num_topics, probes_each)

    system_prompt = textwrap.dedent(f"""
        You are an editor clustering today's seed headlines into {num_topics} distinct candidate news topics
        and producing search vocabulary for each topic.

        INPUT: a numbered list of headlines from providers configured as topic-seeding inputs.
        Provider annotations may include the provider key and broad frame metadata. Those frames are
        context for breadth, not facts about the story.

        OUTPUT: a single JSON array, no prose, no markdown, no code fences, with up to {num_topics}
        objects, each shaped like:
        {{
          "title": "<short human-readable topic title, max ~60 chars>",
          "rationale": "<one short sentence on why this is a top story today>",
          "keywords": ["...", "..."],
          "boost_phrases": ["...", "..."]
        }}

        Rules:
        - Pick the most prominent distinct story candidates from the input. Merge near-duplicates.
        - Prefer stories that appeared on more than one provider when ranking.
        - Each topic must be a *real news event*, not a meta category like "politics".
        - Do not force geographic balance. Validation and final selection happen after this step.
        - "keywords" should be 8-15 lowercase strings: names, places, agencies, technical terms,
          verbs, and other concrete words a relevance scorer can use to detect related coverage.
          Include obvious synonyms and lowercase variants (e.g. "ai", "artificial intelligence").
          Use single words OR short multi-word fragments (max 3 words).
        - "boost_phrases" should be 2-5 highly-specific lowercase multi-word phrases (3-6 words)
          that strongly imply this exact story.
        - Do not invent facts. If unsure, keep keywords broad rather than wrong.
        - Output ONLY the JSON array. Do not include any explanation, preamble, or trailing text.
    """).strip()

    user_prompt = f"Today's top headlines:\n\n{headline_block}\n\nReturn the JSON array now."

    llm = build_chat_model(
        max_tokens=TOPIC_CLUSTERING_MAX_TOKENS,
        task="topic_clustering",
    )
    response = invoke_with_retries(
        llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)],
        task_name="topic clustering",
        fallback_content="[]",
    )
    raw = _safe_json_extract(response.content or "")
    try:
        parsed = json.loads(raw)
    except Exception:
        progress_tracker.warning("Topic clustering JSON parse failed; using degraded fallback.")
        parsed = []

    topics: list[dict] = []
    if isinstance(parsed, list):
        for index, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            keywords = []
            for raw_keyword in entry.get("keywords") or []:
                if not isinstance(raw_keyword, (str, int, float)):
                    continue
                keyword = str(raw_keyword).strip().lower()
                if keyword and _topic_match_terms(keyword):
                    keywords.append(keyword)
            boost_phrases = []
            for raw_phrase in entry.get("boost_phrases") or []:
                if not isinstance(raw_phrase, (str, int, float)):
                    continue
                phrase = str(raw_phrase).strip().lower()
                if phrase and len(_topic_match_terms(phrase)) >= 2:
                    boost_phrases.append(phrase)
            rationale = str(entry.get("rationale") or "").strip()
            if not keywords:
                # Keyword-less topics aren't useful to the relevance scorer.
                continue
            topics.append(
                {
                    "key": f"topic_{index + 1:02d}_{_normalize_topic_key(title)}",
                    "title": title,
                    "rationale": rationale,
                    "keywords": keywords,
                    "boost_phrases": boost_phrases,
                    "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
                    "min_score": 3,
                    "topic_source": "llm",
                }
            )

    if not topics:
        # Degraded fallback: prefer deterministic cross-provider headline clusters,
        # then fall back to single seed headlines only if nothing has overlap.
        topics.extend(_cluster_supported_fallback_topics(stories, num_topics))
        if not topics:
            topics.extend(_fallback_topics_from_stories(stories, num_topics))

    return topics[:num_topics]


def _text_for_topic_frame(topic: dict) -> str:
    return " ".join(
        [
            str(topic.get("title") or ""),
            str(topic.get("rationale") or ""),
            " ".join(str(k) for k in topic.get("keywords", [])),
            " ".join(str(p) for p in topic.get("boost_phrases", [])),
        ]
    ).lower()


def _infer_topic_frame_tags(topic: dict, provider_details: list[dict]) -> list[str]:
    text = _text_for_topic_frame(topic)
    tags: set[str] = set()
    us_terms = {
        "united states", "u.s.", "us ", "america", "american", "white house",
        "congress", "senate", "supreme court", "federal", "washington",
        "pentagon", "state department", "doj", "fbi", "trump", "biden",
    }
    western_terms = {
        "europe", "european", "uk", "britain", "france", "germany", "nato",
        "canada", "australia", "g7", "western",
    }
    non_western_terms = {
        "china", "india", "pakistan", "africa", "middle east", "latin america",
        "global south", "qatar", "turkey", "iran", "iraq", "israel", "gaza",
        "russia", "brazil", "mexico", "indonesia", "korea", "japan",
    }
    padded_text = f" {text} "
    if any(term in padded_text for term in us_terms):
        tags.update({"us", "western"})
    if any(term in padded_text for term in western_terms):
        tags.add("western")
    if any(term in padded_text for term in non_western_terms):
        tags.add("non_western")

    for detail in provider_details:
        frame = str(detail.get("frame") or detail.get("region") or "").lower()
        if "us/" in frame or frame.startswith("us") or "american" in frame:
            tags.update({"us", "western"})
        if "western" in frame:
            tags.add("western")
        if "non-western" in frame or "non_western" in frame or "global south" in frame:
            tags.add("non_western")

    return sorted(tags)


def _score_topic_against_story(topic: dict, story: dict) -> int:
    haystack = " ".join(
        [
            str(story.get("title") or ""),
            str(story.get("description") or ""),
            str(story.get("domain") or ""),
        ]
    ).lower()
    return _score_topic_text_match(topic, haystack)


def _provider_details_from_matches(matches: list[dict]) -> list[dict]:
    details: list[dict] = []
    seen: set[str] = set()
    for match in matches:
        for detail in match.get("provider_details", []):
            key = str(detail.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            details.append(detail)
    return details


def _rank_topic_story_matches(topic: dict, stories: list[dict], *, limit: int = 8) -> list[dict]:
    matches: list[dict] = []
    min_score = max(1, int(topic.get("min_score") or 1))
    for story in stories:
        score = _score_topic_against_story(topic, story)
        if score < min_score:
            continue
        matches.append(
            {
                "title": story.get("title"),
                "url": story.get("url"),
                "providers": story.get("providers", []),
                "provider_details": story.get("provider_details", []),
                "frames": story.get("frames", []),
                "match_score": score,
            }
        )
    return sorted(
        matches,
        key=lambda match: (
            match.get("match_score", 0),
            len(match.get("providers", [])),
        ),
        reverse=True,
    )[:limit]


def _topic_signature_terms(topic: dict) -> set[str]:
    return _topic_match_terms(
        topic.get("title"),
        topic.get("rationale"),
        " ".join(str(k) for k in topic.get("keywords", [])),
        " ".join(str(p) for p in topic.get("boost_phrases", [])),
    )


def _topic_seed_match_urls(topic: dict) -> set[str]:
    return {
        str(match.get("url") or "")
        for match in topic.get("seed_matches", [])
        if match.get("url")
    }


def _is_duplicate_topic(topic: dict, accepted_topics: list[dict]) -> bool:
    title_key = _normalize_topic_key(str(topic.get("title") or ""))
    terms = _topic_signature_terms(topic)
    urls = _topic_seed_match_urls(topic)
    for accepted in accepted_topics:
        if title_key and title_key == _normalize_topic_key(str(accepted.get("title") or "")):
            return True
        accepted_terms = _topic_signature_terms(accepted)
        if terms and accepted_terms:
            overlap = terms & accepted_terms
            union = terms | accepted_terms
            if len(overlap) >= 3 and (len(overlap) / max(1, len(union))) >= 0.65:
                return True
        accepted_urls = _topic_seed_match_urls(accepted)
        if urls and accepted_urls and urls & accepted_urls and terms and accepted_terms:
            overlap = terms & accepted_terms
            if len(overlap) / max(1, min(len(terms), len(accepted_terms))) >= 0.35:
                return True
    return False


def _append_supported_unique_topics(
    destination: list[dict],
    candidates: list[dict],
    *,
    target_count: int,
) -> None:
    for topic in candidates:
        if len(destination) >= target_count:
            break
        if not topic.get("seed_providers"):
            continue
        if _is_fallback_topic(topic) and _topic_provider_support_count(topic) < 2:
            continue
        if _is_duplicate_topic(topic, destination):
            continue
        destination.append(topic)


def prepare_candidate_topics_for_selection(
    annotated_candidates: list[dict],
    *,
    seed_stories: list[dict],
    validation_stories: list[dict],
    target_count: int,
) -> list[dict]:
    """Keep only seed-supported, unique LLM topics and backfill from seed headlines if needed."""
    supported: list[dict] = []
    _append_supported_unique_topics(
        supported,
        annotated_candidates,
        target_count=max(target_count, len(annotated_candidates)),
    )

    if len(supported) < target_count:
        fallback_count = max(target_count * 3, target_count + 4)
        fallback_topics = _cluster_supported_fallback_topics(
            seed_stories + validation_stories,
            fallback_count,
            start_index=len(annotated_candidates) + 1,
        )
        if len(fallback_topics) < target_count:
            fallback_topics.extend(
                _fallback_topics_from_stories(
                    seed_stories,
                    fallback_count,
                    start_index=len(annotated_candidates) + len(fallback_topics) + 1,
                )
            )
        fallback_annotated = annotate_topic_discovery_signals(
            fallback_topics,
            seed_stories=seed_stories,
            validation_stories=validation_stories,
        )
        _append_supported_unique_topics(
            supported,
            fallback_annotated,
            target_count=target_count,
        )

    return supported


def count_topic_level_provider_overlaps(topics: list[dict]) -> int:
    count = 0
    for topic in topics:
        providers = set(topic.get("seed_providers") or [])
        providers.update(topic.get("validation_providers") or [])
        if len(providers) >= 2:
            count += 1
    return count


def annotate_topic_discovery_signals(
    topics: list[dict],
    *,
    seed_stories: list[dict],
    validation_stories: list[dict],
) -> list[dict]:
    annotated: list[dict] = []
    total_candidates = max(1, len(topics))
    for index, topic in enumerate(topics, start=1):
        topic_record = dict(topic)
        seed_matches = _rank_topic_story_matches(topic, seed_stories)
        seed_details = _provider_details_from_matches(seed_matches)
        seed_provider_keys = {str(detail.get("key") or "") for detail in seed_details}
        validation_matches = []
        for match in _rank_topic_story_matches(topic, validation_stories):
            provider_details = [
                detail for detail in match.get("provider_details", [])
                if str(detail.get("key") or "") not in seed_provider_keys
            ]
            if not provider_details:
                continue
            validation_match = dict(match)
            validation_match["provider_details"] = provider_details
            validation_match["providers"] = [
                str(detail.get("key") or "")
                for detail in provider_details
                if detail.get("key")
            ]
            validation_matches.append(validation_match)
        validation_details = _provider_details_from_matches(validation_matches)
        all_details = seed_details + [
            detail for detail in validation_details
            if detail.get("key") not in {d.get("key") for d in seed_details}
        ]
        frame_counter: Counter[str] = Counter()
        for detail in all_details:
            frame = str(detail.get("frame") or detail.get("region") or "unknown").strip()
            if frame:
                frame_counter[frame] += 1

        seed_score = sum(match.get("match_score", 0) for match in seed_matches)
        validation_score = sum(
            match.get("match_score", 0)
            * sum(float(detail.get("weight") or 1.0) for detail in match.get("provider_details", []))
            for match in validation_matches
        )
        rank_score = (total_candidates - index + 1) / total_candidates
        base_score = max(0.05, rank_score + (seed_score * 0.08) + (validation_score * 0.10))

        topic_record.update(
            {
                "candidate_rank": index,
                "seed_matches": seed_matches,
                "validation_matches": validation_matches,
                "seed_providers": [detail.get("key") for detail in seed_details],
                "validation_providers": [detail.get("key") for detail in validation_details],
                "frame_counts": dict(frame_counter),
                "frame_tags": _infer_topic_frame_tags(topic, all_details),
                "selection_base_score": round(base_score, 4),
                "selection_validation_score": round(validation_score, 4),
            }
        )
        annotated.append(topic_record)
    return annotated


def _frame_nudge_for_topic(topic: dict, selected_topics: list[dict], target_count: int) -> float:
    if target_count <= 0:
        return 1.0
    tags = set(topic.get("frame_tags") or [])
    selected_counts: Counter[str] = Counter()
    for selected in selected_topics:
        selected_counts.update(selected.get("frame_tags") or [])

    nudge = 1.0
    for tag, target_share in TOPIC_FRAME_TARGETS.items():
        selected_share = selected_counts[tag] / target_count
        if tag not in tags:
            continue
        gap = target_share - selected_share
        if gap >= 0:
            nudge *= 1.0 + (TOPIC_FRAME_NUDGE_STRENGTH * gap)
        else:
            nudge *= max(0.35, 1.0 + (TOPIC_FRAME_NUDGE_STRENGTH * gap))
    return max(0.20, nudge)


def select_topics_soft_weighted(
    candidate_topics: list[dict],
    target_count: int,
    *,
    seed: str | None = None,
) -> list[dict]:
    """Stochastically sample topics without quotas, using validation and frame nudges as weights."""
    if target_count <= 0:
        return []
    rng = random.Random(seed)
    remaining = [dict(topic) for topic in candidate_topics]
    selected: list[dict] = []

    while remaining and len(selected) < target_count:
        weighted_candidates: list[tuple[dict, float, float]] = []
        for topic in remaining:
            base_score = float(topic.get("selection_base_score") or 0.05)
            frame_nudge = _frame_nudge_for_topic(topic, selected, target_count)
            validation_bonus = 1.0 + min(1.5, float(topic.get("selection_validation_score") or 0.0) / 25.0)
            selection_weight = max(0.01, base_score * frame_nudge * validation_bonus)
            weighted_candidates.append((topic, selection_weight, frame_nudge))

        total_weight = sum(weight for _, weight, _ in weighted_candidates)
        draw = rng.random() * total_weight if total_weight > 0 else 0.0
        cumulative = 0.0
        chosen_index = 0
        for index, (_, weight, _) in enumerate(weighted_candidates):
            cumulative += weight
            if draw <= cumulative:
                chosen_index = index
                break

        chosen_topic, chosen_weight, chosen_nudge = weighted_candidates[chosen_index]
        selected_record = dict(chosen_topic)
        selected_record["selection_rank"] = len(selected) + 1
        selected_record["selection_weight"] = round(chosen_weight, 4)
        selected_record["selection_frame_nudge"] = round(chosen_nudge, 4)
        selected_record["selection_reason"] = (
            f"base={selected_record.get('selection_base_score')}, "
            f"validation={selected_record.get('selection_validation_score')}, "
            f"frame_tags={','.join(selected_record.get('frame_tags') or ['none'])}, "
            f"soft_weight={selected_record['selection_weight']}"
        )
        selected.append(selected_record)
        remaining.pop(chosen_index)

    return selected


def _display_config_path(path) -> str:
    try:
        return str(path.relative_to(CONFIG.root_dir))
    except ValueError:
        return str(path)


def load_predefined_topics_for_run(topic_limit: int | None = None) -> list[dict]:
    configured_topics = load_predefined_topics(
        client_path=CONFIG.client_path,
        topics_path=CONFIG.topics_path,
        default_max_articles_per_source=MAX_ARTICLES_PER_SOURCE,
        topic_ids=CONFIG.topic_ids,
    )
    if topic_limit is not None and topic_limit > 0:
        configured_topics = configured_topics[:topic_limit]

    topics: list[dict] = []
    for index, topic in enumerate(configured_topics, start=1):
        topic_record = dict(topic)
        topic_record["key"] = str(topic_record.get("key") or topic_record.get("id") or "").strip()
        topic_record["topic_source"] = "predefined_config"
        topic_record["configured_rank"] = index
        topic_record["selection_reason"] = "Configured predefined topic from client/topic YAML."
        topic_record.setdefault("seed_providers", [])
        topic_record.setdefault("validation_providers", [])
        topic_record.setdefault("seed_matches", [])
        topic_record.setdefault("validation_matches", [])
        topic_record.setdefault("frame_counts", {})
        topic_record.setdefault("frame_tags", [])
        topics.append(topic_record)
    return topics


def select_topics_for_run(
    effective_num_topics: int,
    *,
    diagnostics: RunDiagnostics | None = None,
) -> tuple[list[dict], list[dict]]:
    topics = load_predefined_topics_for_run()
    if diagnostics is not None:
        diagnostics.settings["active_topic_ids"] = [topic.get("key") for topic in topics]
        diagnostics.record_topics(topics)
    progress_tracker.step("topics", f"Loaded {len(topics)} predefined topic(s).")
    progress_tracker.detail(
        f"Using {len(topics)} predefined topic(s) from "
        f"{_display_config_path(CONFIG.client_path)} and "
        f"{_display_config_path(CONFIG.topics_path)}."
    )
    for topic in topics:
        progress_tracker.detail(
            f"Topic loaded: {topic['title']} "
            f"(configured terms: {len(topic.get('keywords') or [])} keyword(s), "
            f"{len(topic.get('boost_phrases') or [])} boost phrase(s))"
        )
    return topics, []


# --- RELEVANCE SCORING (PER DYNAMIC TOPIC) ---

def _compose_feed_haystack(item: dict) -> str:
    title = str(item.get("title") or "")
    description = _clean_content_text(str(item.get("description") or ""))[
        :FEED_DESCRIPTION_RELEVANCE_CHARS
    ]
    return " ".join([title, description]).lower()


def _score_topic_relevance(item: dict, topic: dict) -> int:
    haystack = _compose_feed_haystack(item)
    return _score_topic_text_relevance(haystack, topic)


def _score_topic_text_relevance(text: str, topic: dict) -> int:
    score = _score_topic_text_match(topic, text)

    if score < TOPIC_RELEVANCE_MIN_SCORE:
        return 0
    return score


def _rank_per_topic_candidates(section_candidates: dict[str, list[dict]], topics: List[dict]) -> List[dict]:
    selected: list[dict] = []
    for topic in topics:
        key = topic["key"]
        max_for_section = max(0, int(topic.get("max_articles_per_source", 0)))
        if max_for_section == 0:
            continue
        ranked = sorted(
            section_candidates.get(key, []),
            key=lambda item: (
                item.get("relevance_score", 0),
                item.get("published_at") or datetime.min.replace(tzinfo=None),
            ),
            reverse=True,
        )
        selected.extend(ranked[:max_for_section])

    return selected


def _select_per_topic_feed_items(items: List[dict], topics: List[dict], *, now_utc: datetime) -> List[dict]:
    section_candidates: dict[str, list[dict]] = {topic["key"]: [] for topic in topics}
    seen_item_topics: set[tuple[str, str]] = set()

    for item in items:
        item_link = str(item.get("link") or "").strip()
        if not item_link:
            continue
        if not _is_within_recent_window(item.get("published_at"), now_utc):
            continue

        for topic in topics:
            topic_score = _score_topic_relevance(item, topic)
            if topic_score <= 0:
                continue
            topic_key = str(topic.get("key") or "")
            item_topic_key = _run_article_topic_key(item_link, topic_key)
            if item_topic_key in seen_item_topics:
                continue
            seen_item_topics.add(item_topic_key)

            enriched_item = dict(item)
            enriched_item["link"] = item_link
            enriched_item["relevance_score"] = topic_score
            enriched_item["topic_key"] = topic_key
            enriched_item["topic_title"] = topic["title"]
            section_candidates[topic_key].append(enriched_item)

    return _rank_per_topic_candidates(section_candidates, topics)


# --- PER-SOURCE FETCH ---

def get_direct_source_article_context(source_name: str) -> dict:
    source_config = SOURCE_FEEDS.get(source_name)
    if not source_config or not isinstance(source_config, dict):
        return {
            "articles": [],
            "status": "missing_source_config",
            "feed_item_count": 0,
            "recent_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "scrape_attempts": [],
            "scrape_status_counts": {},
        }
    feed_url = source_config.get("url")
    if not feed_url:
        return {
            "articles": [],
            "status": "missing_feed_url",
            "feed_item_count": 0,
            "recent_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "scrape_attempts": [],
            "scrape_status_counts": {},
        }

    try:
        response = requests.get(
            feed_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as error:
        return {
            "articles": [],
            "status": "feed_error",
            "reason": str(error),
            "feed_item_count": 0,
            "recent_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "scrape_attempts": [],
            "scrape_status_counts": {},
        }

    content_type = response.headers.get("Content-Type", "")
    if (
        "xml" not in content_type
        and "rss" not in content_type
        and "<rss" not in response.text[:500]
        and "<feed" not in response.text[:500]
    ):
        return {
            "articles": [],
            "status": "not_xml",
            "reason": f"Got Content-Type: {content_type}",
            "feed_item_count": 0,
            "recent_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "scrape_attempts": [],
            "scrape_status_counts": {},
        }

    items = _extract_feed_items(response.text)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    articles: list[dict] = []
    selected_items: list[dict] = []
    scrape_attempts: list[dict[str, Any]] = []
    scrape_status_counts: Counter[str] = Counter()
    scrape_cache: dict[str, dict[str, Any]] = {}
    feed_rejected_counts: Counter[str] = Counter()
    feed_rejections: list[dict[str, Any]] = []
    recent_item_count = 0

    for item in items:
        original_rss_url = str(item.get("link") or "").strip()
        if not original_rss_url:
            continue
        if not _is_within_recent_window(item.get("published_at"), now_utc):
            continue
        recent_item_count += 1

        excluded_reason = _excluded_feed_item_reason(item)
        if excluded_reason:
            _record_feed_source_rejection(
                feed_rejected_counts,
                feed_rejections,
                {
                    "reason": excluded_reason,
                    "source": source_name,
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                },
            )
            continue

        source_match_result = _source_match_result_for_feed_item(
            source_name,
            source_config,
            item,
        )
        if (
            not source_match_result.get("accepted")
            and not source_match_result.get("pending_wire_attribution")
        ):
            _record_feed_source_rejection(
                feed_rejected_counts,
                feed_rejections,
                source_match_result,
            )
            continue

        if original_rss_url in scrape_cache:
            scrape_result = scrape_cache[original_rss_url]
        else:
            scrape_result = _resolve_and_scrape_feed_article(
                original_rss_url,
                title=item.get("title"),
                description=item.get("description"),
                source=source_name,
            )
            scrape_cache[original_rss_url] = scrape_result

        selected_url = str(scrape_result.get("resolved_url") or "").strip()
        scrape_status = str(scrape_result.get("scrape_status") or "unknown")
        scrape_status_counts[scrape_status] += 1
        attempt: dict[str, Any] = {
            "title": item.get("title", ""),
            "original_rss_url": original_rss_url,
            "resolved_url": selected_url,
            "feed_source": item.get("source", ""),
            "title_source_suffix": _feed_title_source_suffix(str(item.get("title") or "")),
            "resolution_status": scrape_result.get("resolution_status"),
            "resolution_error": scrape_result.get("resolution_error"),
            "scrape_status": scrape_status,
            "scrape_seconds": scrape_result.get("scrape_seconds"),
            "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
            **_source_match_public_metadata(source_match_result),
        }
        if not selected_url:
            if source_match_result.get("pending_wire_attribution"):
                rejection = _wire_source_unattributed_rejection(
                    source_match_result,
                    resolved_url=selected_url,
                    scrape_status=scrape_status,
                )
                _record_feed_source_rejection(feed_rejected_counts, feed_rejections, rejection)
                attempt.update(_source_match_public_metadata(rejection))
            scrape_attempts.append(attempt)
            continue

        if attempt["feed_fallback_used"]:
            logger.debug("No real body scraped for %s (status: %s) — dropping", selected_url, scrape_status)
            if source_match_result.get("pending_wire_attribution"):
                rejection = _wire_source_unattributed_rejection(
                    source_match_result,
                    resolved_url=selected_url,
                    scrape_status=scrape_status,
                )
                _record_feed_source_rejection(feed_rejected_counts, feed_rejections, rejection)
                attempt.update(_source_match_public_metadata(rejection))
            scrape_attempts.append(attempt)
            continue

        article_text = str(scrape_result.get("text") or "").strip()
        if not article_text:
            if source_match_result.get("pending_wire_attribution"):
                rejection = _wire_source_unattributed_rejection(
                    source_match_result,
                    resolved_url=selected_url,
                    scrape_status=scrape_status,
                )
                _record_feed_source_rejection(feed_rejected_counts, feed_rejections, rejection)
                attempt.update(_source_match_public_metadata(rejection))
            scrape_attempts.append(attempt)
            continue

        clean_article_text = _clean_article_text(
            article_text,
            source=source_name,
            url=selected_url,
            title=item.get("title", ""),
        )
        if not clean_article_text:
            if source_match_result.get("pending_wire_attribution"):
                rejection = _wire_source_unattributed_rejection(
                    source_match_result,
                    resolved_url=selected_url,
                    scrape_status=scrape_status,
                )
                _record_feed_source_rejection(feed_rejected_counts, feed_rejections, rejection)
                attempt.update(_source_match_public_metadata(rejection))
            scrape_attempts.append(attempt)
            continue

        if source_match_result.get("pending_wire_attribution"):
            attribution_confirmed, attribution_alias = _article_confirms_wire_attribution(
                source_name,
                source_config,
                item,
                article_text,
            )
            if not attribution_confirmed:
                rejection = _wire_source_unattributed_rejection(
                    source_match_result,
                    resolved_url=selected_url,
                    scrape_status=scrape_status,
                )
                _record_feed_source_rejection(feed_rejected_counts, feed_rejections, rejection)
                attempt.update(_source_match_public_metadata(rejection))
                scrape_attempts.append(attempt)
                continue
            source_match_result = _confirm_wire_source_match(
                source_match_result,
                attribution_alias=attribution_alias,
            )
            attempt.update(_source_match_public_metadata(source_match_result))

        summary_text = truncate_text_to_token_limit(clean_article_text, ARTICLE_TEXT_TOKEN_LIMIT)
        article_record = _with_translation_metadata(
            {
                **item,
                "url": selected_url,
                "original_rss_url": original_rss_url,
                "resolved_url": selected_url,
                "resolution_status": scrape_result.get("resolution_status"),
                "scrape_status": scrape_status,
                "scrape_seconds": scrape_result.get("scrape_seconds"),
                "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                "feed_source": item.get("source", ""),
                "title_source_suffix": _feed_title_source_suffix(str(item.get("title") or "")),
                **_source_match_public_metadata(source_match_result),
                "text": summary_text,
                "title": item.get("title", ""),
                "pub_date": item.get("pub_date", ""),
                "description": item.get("description", ""),
            },
            source_name=source_name,
            title=str(item.get("title") or ""),
            text=summary_text,
        )
        articles.append(article_record)
        selected_items.append(
            {
                "title": article_record.get("title", ""),
                "link": article_record.get("original_rss_url", ""),
                "original_rss_url": article_record.get("original_rss_url", ""),
                "resolved_url": article_record.get("resolved_url", ""),
                "pub_date": article_record.get("pub_date", ""),
                "feed_source": article_record.get("feed_source", ""),
                "title_source_suffix": article_record.get("title_source_suffix", ""),
                "source_match_status": article_record.get("source_match_status", ""),
                "publisher_source": article_record.get("publisher_source", ""),
                "wire_source": article_record.get("wire_source", ""),
                "source_display_name": article_record.get("source_display_name", ""),
                "scrape_status": article_record.get("scrape_status"),
                "scrape_seconds": article_record.get("scrape_seconds"),
            }
        )
        scrape_attempts.append(attempt)

    if not articles:
        empty_status = "no_recent_items" if recent_item_count == 0 else "no_scraped_recent_items"
        if scrape_status_counts and all(
            str(status).startswith("google_news_unresolved")
            for status in scrape_status_counts
        ):
            empty_status = "google_news_unresolved"
        return {
            "articles": [],
            "status": empty_status,
            "feed_item_count": len(items),
            "recent_item_count": recent_item_count,
            "selected_item_count": 0,
            "selected_items": selected_items,
            "scrape_attempts": scrape_attempts,
            "scrape_status_counts": dict(scrape_status_counts),
            "feed_rejected_counts": dict(feed_rejected_counts),
            "feed_rejections": feed_rejections,
        }

    return {
        "articles": articles,
        "status": "ok",
        "feed_item_count": len(items),
        "recent_item_count": recent_item_count,
        "selected_item_count": len(articles),
        "selected_items": selected_items,
        "scrape_attempts": scrape_attempts,
        "scrape_status_counts": dict(scrape_status_counts),
        "feed_rejected_counts": dict(feed_rejected_counts),
        "feed_rejections": feed_rejections,
    }


def gather_article_candidates_for_source(
    source_name: str,
    seen_urls: set[str],
    run_seen_urls: set[str],
) -> tuple[List[dict], List[str], dict]:
    direct_context = get_direct_source_article_context(source_name)
    articles = direct_context.get("articles", []) if direct_context else []
    feed_rejected_counts = dict((direct_context or {}).get("feed_rejected_counts") or {})
    source_run = {
        "source": source_name,
        "status": (direct_context or {}).get("status", "missing_source_config"),
        "reason": (direct_context or {}).get("reason"),
        "feed_item_count": (direct_context or {}).get("feed_item_count", 0),
        "recent_item_count": (direct_context or {}).get("recent_item_count", 0),
        "selected_item_count": (direct_context or {}).get("selected_item_count", 0),
        "selected_items": (direct_context or {}).get("selected_items", []),
        "selected_by_topic": {},
        "post_scrape_rejections": [],
        "feed_rejections": (direct_context or {}).get("feed_rejections", []),
        "scrape_attempts": (direct_context or {}).get("scrape_attempts", []),
        "scrape_status_counts": (direct_context or {}).get("scrape_status_counts", {}),
        "fresh_article_count": 0,
        "fresh_articles": [],
        "rejected_counts": {
            "duplicate_this_run": 0,
            "seen_in_history": 0,
            "missing_url": 0,
            **feed_rejected_counts,
        },
    }
    if not articles:
        return [], [], source_run

    fresh_articles = []
    new_urls: List[str] = []
    for article in articles:
        url = str(article.get("url") or "").strip()
        run_dedupe_key = _normalize_url_for_dedupe(url) or url
        if not url:
            source_run["rejected_counts"]["missing_url"] += 1
            continue
        if run_dedupe_key in run_seen_urls:
            source_run["rejected_counts"]["duplicate_this_run"] += 1
            continue
        if SHARED_URL_HISTORY_ENABLED and url in seen_urls:
            source_run["rejected_counts"]["seen_in_history"] += 1
            continue

        fresh_articles.append(article)
        new_urls.append(url)
        run_seen_urls.add(run_dedupe_key)

    if not fresh_articles:
        return [], [], source_run

    article_targets: List[dict] = []
    for index, article in enumerate(fresh_articles, start=1):
        article_targets.append(
            {
                "article_id": f"{source_name}-article-{index}",
                "source": source_name,
                "title": article.get("title", ""),
                "pub_date": article.get("pub_date", ""),
                "url": article.get("url", ""),
                "original_rss_url": article.get("original_rss_url", ""),
                "resolved_url": article.get("resolved_url") or article.get("url", ""),
                "resolution_status": article.get("resolution_status"),
                "scrape_status": article.get("scrape_status"),
                "feed_fallback_used": article.get("feed_fallback_used"),
                "feed_source": article.get("feed_source", ""),
                "title_source_suffix": article.get("title_source_suffix", ""),
                "source_match_status": article.get("source_match_status", ""),
                "publisher_source": article.get("publisher_source", ""),
                "wire_source": article.get("wire_source", ""),
                "source_display_name": article.get("source_display_name", ""),
                "description": article.get("description", ""),
                "text": article.get("text", ""),
                "translation_needed": article.get("translation_needed", False),
                "translation_status": article.get("translation_status"),
                "translation_reason": article.get("translation_reason"),
                "translation_source_language": article.get("translation_source_language"),
                "translation_target_language": article.get("translation_target_language"),
                "translation_model": article.get("translation_model"),
                "translation_retag_source": article.get("translation_retag_source", False),
                "topic_key": "",
                "topic_title": "",
                "relevance_score": 0,
            }
        )
    source_run["fresh_article_count"] = len(article_targets)
    source_run["fresh_articles"] = [
        {
            "article_id": article.get("article_id"),
            "title": article.get("title"),
            "url": article.get("url"),
            "original_rss_url": article.get("original_rss_url"),
            "resolved_url": article.get("resolved_url"),
            "topic_title": "",
            "relevance_score": 0,
            "feed_source": article.get("feed_source", ""),
            "title_source_suffix": article.get("title_source_suffix", ""),
            "source_match_status": article.get("source_match_status", ""),
            "publisher_source": article.get("publisher_source", ""),
            "wire_source": article.get("wire_source", ""),
            "source_display_name": article.get("source_display_name", ""),
            "scrape_status": article.get("scrape_status"),
            "translation_needed": article.get("translation_needed", False),
            "translation_status": article.get("translation_status"),
        }
        for article in article_targets
    ]
    return article_targets, new_urls, source_run


def get_direct_source_context(source_name: str, topics: List[dict]) -> dict | None:
    source_config = SOURCE_FEEDS.get(source_name)
    if not source_config or not isinstance(source_config, dict):
        return None
    feed_url = source_config.get("url")
    if not feed_url:
        return None

    try:
        response = requests.get(
            feed_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as error:
        return {
            "articles": [],
            "status": "feed_error",
            "reason": str(error),
            "feed_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "selected_by_topic": {},
        }

    content_type = response.headers.get("Content-Type", "")
    if (
        "xml" not in content_type
        and "rss" not in content_type
        and "<rss" not in response.text[:500]
        and "<feed" not in response.text[:500]
    ):
        return {
            "articles": [],
            "status": "not_xml",
            "reason": f"Got Content-Type: {content_type}",
            "feed_item_count": 0,
            "selected_item_count": 0,
            "selected_items": [],
            "selected_by_topic": {},
        }

    items = _extract_feed_items(response.text)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    selected_feed_items = _select_per_topic_feed_items(items, topics, now_utc=now_utc)
    section_candidates: dict[str, list[dict]] = {topic["key"]: [] for topic in topics}
    post_scrape_rejections: list[dict[str, Any]] = []
    scrape_attempts: list[dict[str, Any]] = []
    scrape_status_counts: Counter[str] = Counter()
    scrape_cache: dict[str, dict[str, Any]] = {}
    scraped_text_count = 0

    for item in selected_feed_items:
        original_rss_url = str(item.get("link") or "").strip()
        if not original_rss_url:
            continue

        if original_rss_url in scrape_cache:
            scrape_result = scrape_cache[original_rss_url]
        else:
            scrape_result = _resolve_and_scrape_feed_article(
                original_rss_url,
                title=item.get("title"),
                description=item.get("description"),
                source=source_name,
            )
            scrape_cache[original_rss_url] = scrape_result
        selected_url = str(scrape_result.get("resolved_url") or "").strip()
        scrape_status = str(scrape_result.get("scrape_status") or "unknown")
        scrape_status_counts[scrape_status] += 1
        attempt: dict[str, Any] = {
            "title": item.get("title", ""),
            "original_rss_url": original_rss_url,
            "resolved_url": selected_url,
            "resolution_status": scrape_result.get("resolution_status"),
            "resolution_error": scrape_result.get("resolution_error"),
            "scrape_status": scrape_status,
            "scrape_seconds": scrape_result.get("scrape_seconds"),
            "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
            "matched_topics": [
                {
                    "topic_title": item.get("topic_title"),
                    "topic_key": item.get("topic_key"),
                    "relevance_score": item.get("relevance_score", 0),
                }
            ],
        }
        if not selected_url:
            scrape_attempts.append(attempt)
            continue

        if attempt["feed_fallback_used"]:
            logger.debug("No real body scraped for %s (status: %s) — dropping", selected_url, scrape_status)
            scrape_attempts.append(attempt)
            continue

        article_text = str(scrape_result.get("text") or "").strip()
        if not article_text:
            scrape_attempts.append(attempt)
            continue

        clean_article_text = _clean_article_text(
            article_text,
            source=source_name,
            url=selected_url,
            title=item.get("title", ""),
        )
        if not clean_article_text:
            scrape_attempts.append(attempt)
            continue

        scraped_text_count += 1
        summary_text = truncate_text_to_token_limit(clean_article_text, ARTICLE_TEXT_TOKEN_LIMIT)
        topic_key = str(item.get("topic_key") or "")
        if topic_key:
            section_candidates[topic_key].append(
                _with_translation_metadata(
                    {
                        **item,
                        "url": selected_url,
                        "original_rss_url": original_rss_url,
                        "resolved_url": selected_url,
                        "resolution_status": scrape_result.get("resolution_status"),
                        "scrape_status": scrape_status,
                        "scrape_seconds": scrape_result.get("scrape_seconds"),
                        "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                        "text": summary_text,
                        "title": item.get("title", ""),
                        "pub_date": item.get("pub_date", ""),
                        "description": item.get("description", ""),
                        "topic_key": topic_key,
                        "topic_title": item.get("topic_title"),
                        "relevance_score": item.get("relevance_score", 0),
                    },
                    source_name=source_name,
                    title=str(item.get("title") or ""),
                    text=summary_text,
                )
            )
        scrape_attempts.append(attempt)

    selected_articles = _rank_per_topic_candidates(section_candidates, topics)
    selected_by_topic: dict[str, int] = {}
    selected_item_details: list[dict] = []
    for article in selected_articles:
        topic_title = article.get("topic_title") or "Unknown topic"
        selected_by_topic[topic_title] = selected_by_topic.get(topic_title, 0) + 1
        selected_item_details.append(
            {
                "title": article.get("title", ""),
                "link": article.get("original_rss_url", ""),
                "original_rss_url": article.get("original_rss_url", ""),
                "resolved_url": article.get("resolved_url", ""),
                "pub_date": article.get("pub_date", ""),
                "topic_title": article.get("topic_title"),
                "topic_key": article.get("topic_key"),
                "relevance_score": article.get("relevance_score", 0),
                "scrape_status": article.get("scrape_status"),
                "scrape_seconds": article.get("scrape_seconds"),
            }
        )

    if not selected_articles:
        empty_status = "no_relevant_items"
        if scrape_attempts and not scraped_text_count:
            empty_status = "scrape_failed"
        if scrape_status_counts and all(
            str(status).startswith("google_news_unresolved")
            for status in scrape_status_counts
        ):
            empty_status = "google_news_unresolved"
        return {
            "articles": [],
            "status": empty_status,
            "feed_item_count": len(items),
            "selected_item_count": 0,
            "selected_items": selected_item_details,
            "selected_by_topic": selected_by_topic,
            "post_scrape_rejections": post_scrape_rejections,
            "scrape_attempts": scrape_attempts,
            "scrape_status_counts": dict(scrape_status_counts),
        }
    return {
        "articles": selected_articles,
        "status": "ok",
        "feed_item_count": len(items),
        "selected_item_count": len(selected_articles),
        "selected_items": selected_item_details,
        "selected_by_topic": selected_by_topic,
        "post_scrape_rejections": post_scrape_rejections,
        "scrape_attempts": scrape_attempts,
        "scrape_status_counts": dict(scrape_status_counts),
    }


def gather_article_targets_for_source(
    source_name: str,
    topics: List[dict],
    seen_urls: set[str],
    run_seen_urls: set[tuple[str, str]],
) -> tuple[List[dict], List[str], dict]:
    direct_context = get_direct_source_context(source_name, topics)
    articles = direct_context.get("articles", []) if direct_context else []
    source_run = {
        "source": source_name,
        "status": (direct_context or {}).get("status", "missing_source_config"),
        "reason": (direct_context or {}).get("reason"),
        "feed_item_count": (direct_context or {}).get("feed_item_count", 0),
        "selected_item_count": (direct_context or {}).get("selected_item_count", 0),
        "selected_items": (direct_context or {}).get("selected_items", []),
        "selected_by_topic": (direct_context or {}).get("selected_by_topic", {}),
        "post_scrape_rejections": (direct_context or {}).get("post_scrape_rejections", []),
        "scrape_attempts": (direct_context or {}).get("scrape_attempts", []),
        "scrape_status_counts": (direct_context or {}).get("scrape_status_counts", {}),
        "fresh_article_count": 0,
        "fresh_articles": [],
        "rejected_counts": {"duplicate_this_run": 0, "seen_in_history": 0, "missing_url": 0},
    }
    if not articles:
        return [], [], source_run

    fresh_articles = []
    new_urls: List[str] = []
    for article in articles:
        url = article.get("url", "").strip()
        topic_key = str(article.get("topic_key") or "")
        run_dedupe_key = _run_article_topic_key(url, topic_key)
        if not url:
            source_run["rejected_counts"]["missing_url"] += 1
            continue
        if run_dedupe_key in run_seen_urls:
            source_run["rejected_counts"]["duplicate_this_run"] += 1
            continue
        if SHARED_URL_HISTORY_ENABLED and url in seen_urls:
            source_run["rejected_counts"]["seen_in_history"] += 1
            continue

        fresh_articles.append(article)
        new_urls.append(url)
        run_seen_urls.add(run_dedupe_key)

    if not fresh_articles:
        return [], [], source_run

    article_targets: List[dict] = []
    for index, article in enumerate(fresh_articles, start=1):
        topic_key = article.get("topic_key")
        article_targets.append(
            {
                "article_id": f"{source_name}-{topic_key or 'general'}-{index}",
                "source": source_name,
                "title": article.get("title", ""),
                "pub_date": article.get("pub_date", ""),
                "url": article.get("url", ""),
                "original_rss_url": article.get("original_rss_url", ""),
                "resolved_url": article.get("resolved_url") or article.get("url", ""),
                "resolution_status": article.get("resolution_status"),
                "scrape_status": article.get("scrape_status"),
                "feed_fallback_used": article.get("feed_fallback_used"),
                "feed_source": article.get("feed_source", ""),
                "title_source_suffix": article.get("title_source_suffix", ""),
                "source_match_status": article.get("source_match_status", ""),
                "publisher_source": article.get("publisher_source", ""),
                "wire_source": article.get("wire_source", ""),
                "source_display_name": article.get("source_display_name", ""),
                "description": article.get("description", ""),
                "text": article.get("text", ""),
                "translation_needed": article.get("translation_needed", False),
                "translation_status": article.get("translation_status"),
                "translation_reason": article.get("translation_reason"),
                "translation_source_language": article.get("translation_source_language"),
                "translation_target_language": article.get("translation_target_language"),
                "translation_model": article.get("translation_model"),
                "translation_retag_source": article.get("translation_retag_source", False),
                "topic_key": topic_key,
                "topic_title": article.get("topic_title"),
                "relevance_score": article.get("relevance_score", 0),
            }
        )
    source_run["fresh_article_count"] = len(article_targets)
    source_run["fresh_articles"] = [
        {
            "article_id": article.get("article_id"),
            "title": article.get("title"),
            "url": article.get("url"),
            "original_rss_url": article.get("original_rss_url"),
            "resolved_url": article.get("resolved_url"),
            "topic_title": article.get("topic_title"),
            "relevance_score": article.get("relevance_score", 0),
            "feed_source": article.get("feed_source", ""),
            "title_source_suffix": article.get("title_source_suffix", ""),
            "source_match_status": article.get("source_match_status", ""),
            "publisher_source": article.get("publisher_source", ""),
            "wire_source": article.get("wire_source", ""),
            "source_display_name": article.get("source_display_name", ""),
            "scrape_status": article.get("scrape_status"),
            "translation_needed": article.get("translation_needed", False),
            "translation_status": article.get("translation_status"),
        }
        for article in article_targets
    ]
    return article_targets, new_urls, source_run


def _story_source_label(story: dict) -> str:
    provider_names = [
        str(detail.get("name") or detail.get("key") or "").strip()
        for detail in story.get("provider_details", [])
        if isinstance(detail, dict)
    ]
    provider_names = [name for name in provider_names if name]
    if provider_names:
        return provider_names[0]
    return str(story.get("provider_name") or story.get("provider") or "Top-of-funnel source")


def _story_pub_date(story: dict) -> str:
    pub_date = str(story.get("pub_date") or "").strip()
    if pub_date:
        return pub_date
    created_utc = story.get("created_utc")
    if isinstance(created_utc, (int, float)) and created_utc > 0:
        try:
            return datetime.fromtimestamp(created_utc, tz=timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )
        except Exception:
            return ""
    return ""


def _rank_top_funnel_coverage_candidates(topic: dict, stories: list[dict], limit: int = 3) -> list[dict]:
    candidates: list[tuple[int, int, dict]] = []
    for index, story in enumerate(stories):
        score = _score_topic_against_story(topic, story)
        if score <= 0:
            continue
        candidates.append((score, len(story.get("providers", [])), story))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [story for _, _, story in candidates[:limit]]


def build_top_funnel_article_targets_for_coverage_gaps(
    topics: List[dict],
    top_stories: list[dict],
    existing_targets: List[dict],
    seen_urls: set[str],
    run_seen_urls: set[tuple[str, str]],
) -> tuple[List[dict], List[str], dict[str, Any]]:
    existing_topic_keys = {
        str(article.get("topic_key") or "")
        for article in existing_targets
        if article.get("topic_key")
    }
    fallback_targets: list[dict] = []
    new_urls: list[str] = []
    filled_topics: dict[str, int] = {}
    skipped_topics: list[str] = []
    scrape_attempts: list[dict[str, Any]] = []
    scrape_status_counts: Counter[str] = Counter()

    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        if topic_key in existing_topic_keys:
            continue

        candidates = _rank_top_funnel_coverage_candidates(topic, top_stories)
        topic_added = 0
        for story in candidates:
            original_rss_url = str(story.get("url") or "").strip()
            story_source = _story_source_label(story)
            scrape_result = _resolve_and_scrape_feed_article(
                original_rss_url,
                title=str(story.get("title") or ""),
                description=str(story.get("description") or ""),
                source=story_source,
            )
            selected_url = str(scrape_result.get("resolved_url") or "").strip()
            scrape_status = str(scrape_result.get("scrape_status") or "unknown")
            scrape_status_counts[scrape_status] += 1
            scrape_attempts.append(
                {
                    "title": story.get("title", ""),
                    "original_rss_url": original_rss_url,
                    "resolved_url": selected_url,
                    "resolution_status": scrape_result.get("resolution_status"),
                    "resolution_error": scrape_result.get("resolution_error"),
                    "scrape_status": scrape_status,
                    "scrape_seconds": scrape_result.get("scrape_seconds"),
                    "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                    "topic_title": topic_title,
                    "topic_key": topic_key,
                }
            )
            if not selected_url:
                continue
            run_dedupe_key = _run_article_topic_key(selected_url, topic_key)
            if run_dedupe_key in run_seen_urls:
                continue
            if SHARED_URL_HISTORY_ENABLED and selected_url in seen_urls:
                continue

            article_text = str(scrape_result.get("text") or "").strip()
            if not article_text:
                continue

            target_index = len(fallback_targets) + 1
            prepared_text = prepare_article_text_for_summary(
                article_text,
                source=story_source,
                url=selected_url,
                title=str(story.get("title") or ""),
            )
            fallback_targets.append(
                _with_translation_metadata(
                    {
                        "article_id": f"top-funnel-{topic_key}-{target_index}",
                        "source": story_source,
                        "title": story.get("title", ""),
                        "pub_date": _story_pub_date(story),
                        "url": selected_url,
                        "original_rss_url": original_rss_url,
                        "resolved_url": selected_url,
                        "resolution_status": scrape_result.get("resolution_status"),
                        "scrape_status": scrape_status,
                        "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                        "description": story.get("description", ""),
                        "text": prepared_text,
                        "topic_key": topic_key,
                        "topic_title": topic_title,
                        "relevance_score": _score_topic_against_story(topic, story),
                        "coverage_fallback": True,
                    },
                    source_name=story_source,
                    title=str(story.get("title") or ""),
                    text=prepared_text,
                )
            )
            new_urls.append(selected_url)
            run_seen_urls.add(run_dedupe_key)
            topic_added += 1
            break

        if topic_added:
            filled_topics[topic_title] = topic_added
        else:
            skipped_topics.append(topic_title)

    return fallback_targets, new_urls, {
        "filled_topics": filled_topics,
        "skipped_topics": skipped_topics,
        "added_count": len(fallback_targets),
        "scrape_attempts": scrape_attempts,
        "scrape_status_counts": dict(scrape_status_counts),
    }

# --- TITLE GEN / SYNTHESIS ---

def generate_report_title(summary_text: str, fallback_timestamp: str) -> str:
    """Generate a concise markdown title from final synthesis without tools/internet calls."""
    try:
        clean_summary_text = _strip_prompt_echo_lines(summary_text)
        llm = build_chat_model(
            max_tokens=TITLE_GENERATION_MAX_TOKENS,
            task="title_generation",
        )
        title_prompt = [
            SystemMessage(content=(
                "You generate concise report titles. "
                "Return ONLY one title line, no markdown, no quotes, max 12 words. "
                "Base the title on the dominant theme across today's selected topics, "
                "not on media coverage language."
            )),
            HumanMessage(content=f"Create a title based on this final summary:\n\n{clean_summary_text}")
        ]
        response = invoke_with_retries(
            llm,
            title_prompt,
            task_name="title generation",
            fallback_content=f"Daily News Summary - {fallback_timestamp}",
        )
        title = strip_model_artifacts(response.content or "").strip()
        title = re.sub(r"^#+\s*", "", title).strip()
        title = title.splitlines()[0].strip() if title else ""
        title = title[:120].strip()
        return title if title else f"Daily News Summary - {fallback_timestamp}"
    except Exception:
        return f"Daily News Summary - {fallback_timestamp}"


def _strip_prompt_echo_lines(text: str) -> str:
    clean = (text or "").replace("\r\n", "\n")
    filtered_lines: list[str] = []
    for line in clean.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+\)\s*Title\s*:", stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"^\d+\)\s*Content\s*:", stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"^Title\s*:", stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"^Content\s*:", stripped, flags=re.IGNORECASE):
            continue
        if stripped.startswith("The user wants to construct a report that contains a summary of news articles."):
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines).strip()


VISIBLE_CONTENT_CHAT_TEMPLATE_KWARGS: dict[str, Any] = {"enable_thinking": False}


def _sampling_to_extra_body(settings: ModelSamplingSettings) -> dict[str, Any]:
    return {
        "top_p": settings.top_p,
        "top_k": settings.top_k,
        "presence_penalty": settings.presence_penalty,
        "repetition_penalty": settings.repetition_penalty,
        "min_p": settings.min_p,
    }


def _sampling_to_dict(settings: ModelSamplingSettings) -> dict[str, float | int]:
    return {
        "temperature": settings.temperature,
        **_sampling_to_extra_body(settings),
    }


def _task_sampling_to_dict() -> dict[str, dict[str, float | int]]:
    return {
        task: _sampling_to_dict(settings)
        for task, settings in MODEL_TASK_SAMPLING.items()
    }


def _model_extra_body(settings: ModelSamplingSettings) -> dict[str, Any]:
    extra_body = _sampling_to_extra_body(settings)
    # MLX reports template-level hidden reasoning as `message.reasoning`, while
    # LangChain reads the user-visible answer from `message.content`.
    extra_body["chat_template_kwargs"] = dict(VISIBLE_CONTENT_CHAT_TEMPLATE_KWARGS)
    return extra_body


def build_chat_model(max_tokens: int, *, task: str = "default") -> ChatOpenAI:
    ensure_codex_safe_model_reference(MODEL_REFERENCE)
    if MANAGED_MODEL_SERVER_ACTIVE:
        _ensure_main_model_server_ready()
    sampling = MODEL_TASK_SAMPLING.get(task, MODEL_DEFAULT_SAMPLING)
    return ChatOpenAI(
        base_url=MODEL_BASE_URL,
        api_key="not-needed",
        temperature=sampling.temperature,
        model=MODEL_NAME,
        max_tokens=max_tokens,
        max_retries=0,
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        extra_body=_model_extra_body(sampling),
    )


def _is_transient_model_error(error: Exception) -> bool:
    return isinstance(
        error,
        (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
        ),
    )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _model_call_bucket(task_name: str) -> str:
    if task_name.startswith("analysis for "):
        return "article_summary"
    if task_name.startswith("story synthesis for "):
        return "story_synthesis"
    return task_name


def _model_token_usage_entry_locked(task_name: str) -> dict[str, Any]:
    bucket = _model_call_bucket(task_name)
    token_usage = MODEL_CALL_STATS.setdefault("token_usage", {})
    entry = token_usage.setdefault(
        bucket,
        {
            "calls": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "max_output_tokens_requested": 0,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "actual_total_tokens": 0,
            "actual_usage_calls": 0,
            "fallback_calls": 0,
            "max_estimated_input_tokens": 0,
            "max_estimated_output_tokens": 0,
            "max_actual_input_tokens": 0,
            "max_actual_output_tokens": 0,
        },
    )
    return entry


def _record_model_token_usage_locked(
    task_name: str,
    *,
    estimated_input_tokens: int,
    max_output_tokens: int | None,
) -> None:
    entry = _model_token_usage_entry_locked(task_name)
    entry["calls"] = int(entry.get("calls", 0)) + 1
    entry["estimated_input_tokens"] = int(entry.get("estimated_input_tokens", 0)) + estimated_input_tokens
    entry["max_estimated_input_tokens"] = max(
        int(entry.get("max_estimated_input_tokens", 0)),
        estimated_input_tokens,
    )
    if max_output_tokens is not None:
        entry["max_output_tokens_requested"] = (
            int(entry.get("max_output_tokens_requested", 0)) + max_output_tokens
        )


def _extract_token_usage_from_response(message: AIMessage) -> dict[str, int]:
    usage: dict[str, int] = {}
    candidates = [
        getattr(message, "usage_metadata", None),
        getattr(message, "response_metadata", None),
    ]
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        candidates.extend(
            [
                response_metadata.get("token_usage"),
                response_metadata.get("usage"),
            ]
        )

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("token_usage")
        if isinstance(nested, dict):
            candidates.append(nested)
        input_tokens = _coerce_int(candidate.get("input_tokens") or candidate.get("prompt_tokens"))
        output_tokens = _coerce_int(
            candidate.get("output_tokens")
            or candidate.get("completion_tokens")
            or candidate.get("completion")
        )
        total_tokens = _coerce_int(candidate.get("total_tokens"))
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        if total_tokens is not None:
            usage["total_tokens"] = total_tokens
        if usage:
            return usage
    return usage


def _record_response_token_usage(task_name: str, response: AIMessage) -> None:
    usage = _extract_token_usage_from_response(response)
    estimated_output_tokens = estimate_token_count(
        response.content if isinstance(response.content, str) else str(response.content or "")
    )
    with MODEL_CALL_STATS_LOCK:
        entry = _model_token_usage_entry_locked(task_name)
        entry["estimated_output_tokens"] = (
            int(entry.get("estimated_output_tokens", 0)) + estimated_output_tokens
        )
        entry["max_estimated_output_tokens"] = max(
            int(entry.get("max_estimated_output_tokens", 0)),
            estimated_output_tokens,
        )
        if not usage:
            return
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if input_tokens is not None:
            entry["actual_input_tokens"] = int(entry.get("actual_input_tokens", 0)) + input_tokens
            entry["max_actual_input_tokens"] = max(
                int(entry.get("max_actual_input_tokens", 0)),
                input_tokens,
            )
        if output_tokens is not None:
            entry["actual_output_tokens"] = int(entry.get("actual_output_tokens", 0)) + output_tokens
            entry["max_actual_output_tokens"] = max(
                int(entry.get("max_actual_output_tokens", 0)),
                output_tokens,
            )
        if total_tokens is None and (input_tokens is not None or output_tokens is not None):
            total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        if total_tokens is not None:
            entry["actual_total_tokens"] = int(entry.get("actual_total_tokens", 0)) + total_tokens
        entry["actual_usage_calls"] = int(entry.get("actual_usage_calls", 0)) + 1


def invoke_with_retries(
    llm,
    messages,
    *,
    task_name: str,
    fallback_content: str,
    attempts: int = MODEL_RETRY_ATTEMPTS,
) -> AIMessage:
    last_error = None
    estimated_input_tokens = sum(estimate_message_token_count(message) for message in messages)
    max_output_tokens = _coerce_int(getattr(llm, "max_tokens", None))
    with MODEL_CALL_STATS_LOCK:
        calls = MODEL_CALL_STATS.setdefault("calls", {})
        calls[task_name] = int(calls.get(task_name, 0)) + 1
        _record_model_token_usage_locked(
            task_name,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=max_output_tokens,
        )
    for attempt in range(1, attempts + 1):
        try:
            response = llm.invoke(messages)
            if isinstance(response, AIMessage):
                _record_response_token_usage(task_name, response)
                return response
            response_message = AIMessage(content=str(getattr(response, "content", response)))
            _record_response_token_usage(task_name, response_message)
            return response_message
        except Exception as error:
            last_error = error
            if not _is_transient_model_error(error) or attempt == attempts:
                break
            delay = MODEL_RETRY_BASE_DELAY_SECONDS * attempt
            with MODEL_CALL_STATS_LOCK:
                MODEL_CALL_STATS["retries"] = int(MODEL_CALL_STATS.get("retries", 0)) + 1
            progress_tracker.retrying(task_name, attempt, attempts, delay)
            time.sleep(delay)

    progress_tracker.warning(f"{task_name[:40]} failed after {attempts} attempts")
    with MODEL_CALL_STATS_LOCK:
        MODEL_CALL_STATS["fallbacks"] = int(MODEL_CALL_STATS.get("fallbacks", 0)) + 1
        failures = MODEL_CALL_STATS.setdefault("failures", {})
        failures[task_name] = str(last_error) if last_error else "unknown error"
        usage = _model_token_usage_entry_locked(task_name)
        usage["fallback_calls"] = int(usage.get("fallback_calls", 0)) + 1
    return AIMessage(content=fallback_content)


def _get_token_encoder():
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding(TOKEN_ENCODING_NAME)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def estimate_token_count(text: str) -> int:
    clean_text = text or ""
    encoder = _get_token_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(clean_text))
        except Exception:
            pass
    return max(0, round(len(clean_text) / 4))


def estimate_message_token_count(message: BaseMessage) -> int:
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    return estimate_token_count(content)


def truncate_text_to_token_limit(text: str, token_limit: int) -> str:
    clean_text = text or ""
    if token_limit <= 0 or estimate_token_count(clean_text) <= token_limit:
        return clean_text

    encoder = _get_token_encoder()
    if encoder is not None:
        try:
            token_ids = encoder.encode(clean_text)
            truncated = encoder.decode(token_ids[:token_limit]).strip()
            return truncated.rsplit(" ", 1)[0].strip() + " ..."
        except Exception:
            pass

    max_chars = max(500, token_limit * 4)
    truncated = clean_text[:max_chars].rsplit(" ", 1)[0].strip()
    return truncated + " ..."


def prepare_article_text_for_summary(
    text: str,
    *,
    source: str | None = None,
    url: str | None = None,
    title: str | None = None,
) -> str:
    return truncate_text_to_token_limit(
        _clean_article_text(text, source=source, url=url, title=title),
        ARTICLE_TEXT_TOKEN_LIMIT,
    )


def extract_prompt_tokens_from_response(message: AIMessage) -> int | None:
    candidates = [
        getattr(message, "usage_metadata", None),
        getattr(message, "response_metadata", None),
    ]
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        candidates.extend(
            [
                response_metadata.get("token_usage"),
                response_metadata.get("usage"),
            ]
        )

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("input_tokens", "prompt_tokens"):
            value = candidate.get(key)
            if isinstance(value, int):
                return value
        token_usage = candidate.get("token_usage")
        if isinstance(token_usage, dict):
            for key in ("input_tokens", "prompt_tokens"):
                value = token_usage.get(key)
                if isinstance(value, int):
                    return value
    return None

def strip_model_artifacts(text: str) -> str:
    text = text or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|im_(?:start|end)\|>", "", text)
    text = re.sub(r"&lt;/?(?:analysis|content)&gt;", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:analysis|content)>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"&lt;/?topic&gt;", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?topic>", "", text, flags=re.IGNORECASE)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _contains_disallowed_final_markup(text: str) -> bool:
    return bool(re.search(r"(&lt;/?topic\b|</?topic\b)", text or "", flags=re.IGNORECASE))


def _final_synthesis_word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _is_low_coverage_synthesis_section(section_text: str) -> bool:
    clean_text = re.sub(r"\s+", " ", strip_model_artifacts(section_text or "")).strip().lower()
    if not clean_text:
        return True
    return any(pattern in clean_text for pattern in LOW_COVERAGE_SYNTHESIS_PATTERNS)


def clean_synthesis_for_publication(text: str, *, relaxed: bool = False) -> str:
    if _contains_disallowed_final_markup(text):
        return ""
    clean_text = _strip_prompt_echo_lines(strip_model_artifacts(text or ""))
    if not clean_text:
        return ""

    section_pattern = re.compile(r"(?m)^##\s+(.+?)\s*$")
    matches = list(section_pattern.finditer(clean_text))
    if not matches:
        return clean_text.strip() if relaxed else (
            "" if _is_low_coverage_synthesis_section(clean_text) else clean_text.strip()
        )

    prefix = clean_text[: matches[0].start()].strip()
    kept_sections: list[str] = [prefix] if prefix else []
    story_pattern = re.compile(r"(?m)^#{3,4}\s+(.+?)\s*$")
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean_text)
        section_header = clean_text[match.start():match.end()].strip()
        section_body = clean_text[match.end():end].strip()
        if relaxed:
            kept_sections.append(clean_text[match.start():end].strip())
            continue

        story_matches = list(story_pattern.finditer(section_body))
        if not story_matches:
            if _is_low_coverage_synthesis_section(section_body):
                continue
            kept_sections.append(clean_text[match.start():end].strip())
            continue

        section_prefix = section_body[: story_matches[0].start()].strip()
        kept_story_parts: list[str] = []
        for story_index, story_match in enumerate(story_matches):
            story_end = (
                story_matches[story_index + 1].start()
                if story_index + 1 < len(story_matches)
                else len(section_body)
            )
            story = section_body[story_match.start():story_end].strip()
            story_body = section_body[story_match.end():story_end].strip()
            if _is_low_coverage_synthesis_section(story_body):
                continue
            kept_story_parts.append(story)
        if not kept_story_parts:
            continue

        section_parts = [section_header]
        if section_prefix:
            section_parts.append(section_prefix)
        section_parts.extend(kept_story_parts)
        kept_sections.append("\n\n".join(section_parts))

    return "\n\n".join(part for part in kept_sections if part.strip()).strip()


def has_structured_entry(text: str, heading_name: str) -> bool:
    clean_text = strip_model_artifacts(text)
    if "DATABASE_ENTRY:" in clean_text:
        return True
    return all(
        marker in clean_text
        for marker in (f"### {heading_name}", "Metadata:", "Summary:")
    )


def normalize_report_entry(article: dict, raw_text: str) -> str:
    clean_text = strip_model_artifacts(raw_text)
    if "DATABASE_ENTRY:" in clean_text:
        clean_text = clean_text.split("DATABASE_ENTRY:", 1)[1].strip()

    heading_name = _build_article_heading(article)
    heading_match = re.search(rf"###\s+{re.escape(heading_name)}\b", clean_text)
    if heading_match:
        clean_text = clean_text[heading_match.start():]

    metadata_block = _format_article_metadata(article)

    summary = ""
    summary_match = re.search(r"Summary:\s*(.*)", clean_text, flags=re.DOTALL)
    if summary_match:
        summary = summary_match.group(1)

    summary = re.split(r"\n(?:---+|###\s+)", summary, maxsplit=1)[0]
    summary = strip_model_artifacts(summary)

    filtered_lines = []
    for line in summary.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^\*+\s*", "", stripped)
        if re.match(r'^[`"\']?\s*prefix\b', stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"^(let me provide|the correct format|header and proper markdown structure)", stripped, flags=re.IGNORECASE):
            continue
        if stripped.startswith(f"{heading_name} -"):
            continue
        if stripped in {"---", "```", "`"}:
            continue
        filtered_lines.append(stripped)

    summary = re.sub(r"\s+", " ", " ".join(filtered_lines)).strip()
    if not summary:
        summary = "No reliable summary generated because the model failed to format its response."

    return (
        f"### {heading_name}\n"
        "Metadata:\n"
        f"{metadata_block}\n\n"
        "Summary:\n"
        f"{summary}"
    )


def is_low_confidence_report_entry(entry: str) -> bool:
    summary_match = re.search(r"Summary:\s*(.*)", entry or "", flags=re.DOTALL)
    summary_text = summary_match.group(1).lower() if summary_match else (entry or "").lower()
    return any(pattern in summary_text for pattern in LOW_CONFIDENCE_SUMMARY_PATTERNS)


def _format_topic_section_header(topic_title: str) -> str:
    return topic_title.upper()


def _report_topic_label(entry: str) -> str:
    topic_match = re.search(r"^- Topic:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return topic_match.group(1).strip() if topic_match else ""


def _report_story_label(entry: str) -> str:
    story_match = re.search(r"^- Story:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return story_match.group(1).strip() if story_match else ""


def _report_summary_text(entry: str) -> str:
    summary_match = re.search(r"Summary:\s*(.*)", entry or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", summary_match.group(1).strip()) if summary_match else ""


def _report_reference_key(entry: str) -> str:
    return hashlib.sha1((entry or "").encode("utf-8")).hexdigest()


def filter_reports_for_references(final_reports: List[str], token_stats: dict[str, Any]) -> List[str]:
    included_keys = {
        str(key)
        for key in (token_stats or {}).get("included_report_keys", [])
        if str(key)
    }
    if not included_keys:
        return final_reports
    return [entry for entry in final_reports if _report_reference_key(entry) in included_keys]


def _extract_first_name(name_or_email: str) -> str:
    clean_value = (name_or_email or "").strip()
    if not clean_value:
        return "there"

    if "@" in clean_value and " " not in clean_value:
        local_part = clean_value.split("@", 1)[0]
        tokens = re.split(r"[._+-]+", local_part)
    else:
        tokens = clean_value.split()

    first_name = next((token for token in tokens if token), "")
    return first_name or "there"


def build_email_subject(run_datetime: datetime | None = None) -> str:
    subject_date = (run_datetime or RUN_STARTED_AT).strftime("%m/%d/%y")
    return f"Daily LLM News, {subject_date}"


def maybe_email_report(
    report_title: str,
    report_body: str,
    synthesis_body: str,
    final_reports: List[str],
    topics: List[dict],
    recipients: List[str],
    recipient_names: List[str],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
) -> None:
    missing = []
    if not recipients:
        missing.append("recipient list")
    if not EMAIL_FROM:
        missing.append("NEWS_EMAIL_FROM")
    if not SMTP_HOST:
        missing.append("NEWS_SMTP_HOST")
    if not SMTP_USERNAME:
        missing.append("NEWS_SMTP_USERNAME")
    if not SMTP_PASSWORD:
        missing.append("NEWS_SMTP_PASSWORD")

    if missing:
        progress_tracker.detail(f"[email] Skipping email. Missing configuration: {', '.join(missing)}")
        return

    def build_message(recipient_email: str, recipient_name: str) -> EmailMessage:
        first_name = _extract_first_name(recipient_name or recipient_email)
        unsubscribe_url = build_unsubscribe_url(recipient_email)
        html_image_art = image_art
        related_image_bytes = None
        related_image_cid = None
        if image_art and image_art.get("final_image_path"):
            try:
                with open(str(image_art["final_image_path"]), "rb") as image_file:
                    related_image_bytes = image_file.read()
                related_image_cid = make_msgid(domain="news-pipeline.local")
                html_image_art = {
                    **image_art,
                    "content_id": related_image_cid[1:-1],
                }
            except Exception:
                html_image_art = image_art
        message = EmailMessage()
        message["Subject"] = build_email_subject()
        message["From"] = EMAIL_FROM
        message["To"] = recipient_email
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        message.set_content(
            f"{first_name},\n\nHere is your daily news summary.\n\n{report_body}\n\n"
            f"Unsubscribe: {unsubscribe_url}"
        )
        message.add_alternative(
            build_report_html(
                recipient_email,
                recipient_name or recipient_email,
                report_title,
                synthesis_body,
                final_reports,
                topics,
                html_image_art,
                citation_sources,
            ),
            subtype="html",
        )
        if related_image_bytes and related_image_cid:
            html_part = message.get_payload()[-1]
            html_part.add_related(
                related_image_bytes,
                maintype="image",
                subtype="png",
                cid=related_image_cid,
                filename=os.path.basename(str(image_art.get("final_image_path"))),
            )
        return message

    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            for index, recipient_email in enumerate(recipients):
                recipient_name = recipient_names[index] if index < len(recipient_names) else recipient_email
                smtp.send_message(build_message(recipient_email, recipient_name))
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            for index, recipient_email in enumerate(recipients):
                recipient_name = recipient_names[index] if index < len(recipient_names) else recipient_email
                smtp.send_message(build_message(recipient_email, recipient_name))

    progress_tracker.detail(f"[email] Sent report to {', '.join(recipients)}")

def _first_sentences(text: str, max_sentences: int = 2, max_chars: int = 520) -> str:
    clean_text = re.sub(r"\s+", " ", strip_model_artifacts(text or "")).strip()
    if not clean_text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    selected = " ".join(sentence for sentence in sentences[:max_sentences] if sentence).strip()
    if not selected:
        selected = clean_text
    if len(selected) <= max_chars:
        return selected
    return selected[:max_chars].rsplit(" ", 1)[0].strip() + "..."


def _dev_synthesis_paragraph_from_summaries(summaries: list[str]) -> str:
    snippets: list[str] = []
    for summary in summaries[:4]:
        snippet = _first_sentences(summary, max_sentences=2, max_chars=700)
        if snippet:
            snippets.append(snippet)
    paragraph = " ".join(snippets)
    return _first_sentences(paragraph, max_sentences=8, max_chars=1800)


def build_dev_final_synthesis_preview(final_reports: List[str], topics: List[dict]) -> str:
    """Create grouped dev-only prose when final synthesis repeatedly returns empty."""
    topic_order = [str(topic.get("title") or "").strip() for topic in topics if topic.get("title")]
    grouped: dict[str, dict[str, list[str]]] = {topic_title: {} for topic_title in topic_order}
    ungrouped: list[str] = []
    explicit_story_mode = any(_report_story_label(report) for report in final_reports)

    for report in final_reports:
        summary_text = _report_summary_text(report)
        if not summary_text:
            continue
        topic_label = _report_topic_label(report)
        story_label = _report_story_label(report)
        if explicit_story_mode and not story_label:
            continue
        story_label = story_label or topic_label or "General update"
        if topic_label in grouped:
            grouped[topic_label].setdefault(story_label, []).append(summary_text)
        elif topic_label:
            grouped.setdefault(topic_label, {}).setdefault(story_label, []).append(summary_text)
        else:
            ungrouped.append(summary_text)

    sections: list[str] = []
    for topic_title in topic_order:
        story_map = grouped.get(topic_title) or {}
        if not story_map:
            continue
        paragraphs: list[str] = []
        for story_label, summaries in story_map.items():
            if explicit_story_mode and len(summaries) < MIN_ARTICLES_PER_STORY:
                continue
            paragraph = _dev_synthesis_paragraph_from_summaries(summaries)
            if paragraph:
                paragraphs.append(f"### {story_label}\n\n{paragraph}")
        if paragraphs:
            section_body = "\n\n".join(paragraphs)
            sections.append(
                f"## {_format_topic_section_header(topic_title)}\n"
                f"{section_body}"
            )

    if not sections and ungrouped and not explicit_story_mode:
        paragraph = _dev_synthesis_paragraph_from_summaries(ungrouped)
        if paragraph:
            sections.append(
                "## DAILY NEWS SUMMARY\n"
                f"{paragraph}"
            )

    return "\n\n".join(sections)


def _truncate_for_art_prompt(text: str, max_chars: int = 3800) -> str:
    compact_text = re.sub(r"\s+", " ", _strip_prompt_echo_lines(text or "")).strip()
    if len(compact_text) <= max_chars:
        return compact_text
    return compact_text[:max_chars].rsplit(" ", 1)[0].strip() + "..."


def _sanitize_overlay_headline(value: str, fallback: str) -> str:
    clean_value = _strip_inline_markdown(strip_model_artifacts(value or ""))
    clean_value = re.sub(r"[\r\n]+", " ", clean_value)
    clean_value = re.sub(r"^headline\s*:\s*", "", clean_value, flags=re.IGNORECASE).strip()
    clean_value = clean_value.strip(" \"'")
    clean_value = re.sub(r"\s+", " ", clean_value)
    if not clean_value:
        clean_value = fallback
    words = clean_value.split()
    if len(words) > 11:
        clean_value = " ".join(words[:11])
    return clean_value[:90].strip() or fallback


def _fallback_image_prompt(summary_text: str) -> str:
    compact_summary = _truncate_for_art_prompt(summary_text, max_chars=900)
    return textwrap.dedent(
        f"""
        Create one plain text-free documentary photograph that visually suggests this
        news synthesis without depicting an article, poster, flyer, magazine
        spread, web page, screen, infographic, report, newspaper, presentation
        slide, broadcast graphic, captioned image, meme, or social media card:
        {compact_summary}

        Visual target: one coherent real-world scene, photographed as if for a
        wire-service photo archive. Prefer physical places, people, infrastructure,
        and environmental context over symbols or graphic design.

        Important: the image must contain no typography of any kind. Do not create
        signs, banners, labels, captions, posters, headlines, subtitles, watermarks,
        UI panels, lower thirds, footer panels, paragraph blocks, title cards,
        placards, screens, documents, newspapers, maps, charts, or shapes that
        resemble letters or writing.

        Composition: natural camera perspective, no graphic design layout, no
        border bands, no poster framing, no text boxes, no blank caption area, no
        dark strip at the bottom, no empty panel. Fill the frame with the
        photographed scene only.
        """
    ).strip()


def _enforce_text_free_image_prompt(prompt: str) -> str:
    clean_prompt = strip_model_artifacts(prompt or "").strip()
    if not clean_prompt:
        clean_prompt = "A realistic documentary wire-service photograph of today's major news."
    guardrails = (
        "\n\nHard constraints: no text, no letters, no words, no signs, no labels, "
        "no logos, no watermarks, no captions, no screens, no newspapers, no maps, "
        "no charts, no title cards, no lower thirds, no footer bands, and no graphic "
        "layout. The generated image must be the photographed scene only; the "
        "readable headline will be rendered later by code."
    )
    if "readable headline will be rendered later by code" in clean_prompt:
        return clean_prompt
    return clean_prompt + guardrails


def generate_image_art_brief(synthesis_body: str, report_title: str) -> dict[str, str]:
    """Ask the text model for the FLUX prompt plus a separate overlay headline."""
    fallback_headline = _sanitize_overlay_headline(report_title, "Daily News Brief")
    fallback_prompt = _fallback_image_prompt(synthesis_body)
    try:
        llm = build_chat_model(max_tokens=700, task="title_generation")
        response = invoke_with_retries(
            llm,
            [
                SystemMessage(content=(
                    "You are preparing art direction for a text-to-image news illustration. "
                    "Return ONLY valid JSON with keys image_prompt and overlay_headline. "
                    "The image_prompt is for FLUX and must request a realistic documentary "
                    "photograph with absolutely no text or typography in the image. "
                    "The overlay_headline is readable text that will be rendered later by code, "
                    "not by the image model. Keep overlay_headline punchy, factual, and <= 11 words."
                )),
                HumanMessage(content=(
                    "Use the final news output below to create the image prompt and the separate "
                    "footer headline.\n\n"
                    f"Report title: {report_title}\n\n"
                    f"Final output:\n{_truncate_for_art_prompt(synthesis_body)}"
                )),
            ],
            task_name="image art prompt generation",
            fallback_content=json.dumps(
                {
                    "image_prompt": fallback_prompt,
                    "overlay_headline": fallback_headline,
                }
            ),
        )
        payload = json.loads(_safe_json_extract(response.content or ""))
        if not isinstance(payload, dict):
            raise ValueError("Image art prompt response was not a JSON object.")
        image_prompt = _enforce_text_free_image_prompt(str(payload.get("image_prompt") or fallback_prompt))
        overlay_headline = _sanitize_overlay_headline(
            str(payload.get("overlay_headline") or ""),
            fallback_headline,
        )
        return {
            "image_prompt": image_prompt,
            "overlay_headline": overlay_headline,
        }
    except Exception as error:
        progress_tracker.warning("image art prompt fallback")
        return {
            "image_prompt": _enforce_text_free_image_prompt(fallback_prompt),
            "overlay_headline": fallback_headline,
            "error": str(error),
        }


def _load_overlay_font(size: int) -> object:
    from PIL import ImageFont

    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for font_path in font_candidates:
        if os.path.exists(font_path):
            return ImageFont.truetype(font_path, size=size)
    return ImageFont.load_default()


def _wrap_text_to_width(draw: object, text: str, font: object, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current_line = words[0]
    for word in words[1:]:
        candidate = f"{current_line} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current_line = candidate
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return lines


def add_headline_overlay(image: object, headline: str, *, crop_bottom_ratio: float) -> object:
    from PIL import Image, ImageDraw

    working = image.convert("RGB")
    width, height = working.size
    crop_bottom_ratio = min(max(crop_bottom_ratio, 0.0), 0.35)
    cropped_height = max(1, int(height * (1.0 - crop_bottom_ratio)))
    cropped_image = working.crop((0, 0, width, cropped_height))

    font_size = max(34, width // 22)
    footer_padding_x = max(28, width // 30)
    footer_padding_y = max(22, width // 34)
    font = _load_overlay_font(font_size)
    scratch = Image.new("RGB", (width, 1))
    draw = ImageDraw.Draw(scratch)
    max_text_width = width - (footer_padding_x * 2)
    lines = _wrap_text_to_width(draw, headline, font, max_text_width)
    line_height = int(font_size * 1.22)
    footer_height = (line_height * max(1, len(lines))) + (footer_padding_y * 2)

    final_image = Image.new("RGB", (width, cropped_height + footer_height), (7, 10, 16))
    final_image.paste(cropped_image, (0, 0))
    draw = ImageDraw.Draw(final_image)

    text_y = cropped_height + footer_padding_y
    for line in lines:
        draw.text(
            (footer_padding_x, text_y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )
        text_y += line_height

    return final_image


def generate_image_with_mflux(prompt: str, *, output_path: str, seed: int) -> object:
    if importlib.util.find_spec("mflux.models.flux2.cli.flux2_generate") is None:
        raise RuntimeError(
            "mflux FLUX.2 support is not importable in the current Python environment. "
            "Install/sync mflux or run with `uv run --with \"mflux>=0.16.0\" news dev`."
        )

    with tempfile.TemporaryDirectory(prefix="news-art-mflux-") as temp_dir:
        prompt_path = os.path.join(temp_dir, "prompt.txt")
        raw_output_path = os.path.join(temp_dir, "mflux-output.png")
        with open(prompt_path, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(prompt + "\n")

        command = [
            sys.executable,
            "-m",
            "mflux.models.flux2.cli.flux2_generate",
            "--model",
            IMAGE_MODEL_ID,
            "--base-model",
            IMAGE_BASE_MODEL,
            "--prompt-file",
            prompt_path,
            "--seed",
            str(seed),
            "--height",
            str(IMAGE_HEIGHT),
            "--width",
            str(IMAGE_WIDTH),
            "--steps",
            str(IMAGE_STEPS),
            "--output",
            raw_output_path,
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(CONFIG.root_dir),
            stdout=RUN_LOG_FILE or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        if not os.path.exists(raw_output_path):
            raise RuntimeError(f"mflux finished but did not create {raw_output_path}.")

        from PIL import Image

        with Image.open(raw_output_path) as image:
            raw_image = image.copy()
        raw_image.save(output_path)
        return raw_image


def generate_report_image_art(
    *,
    report_path: str,
    synthesis_body: str,
    report_title: str,
) -> dict[str, Any] | None:
    if not IMAGE_GENERATION_ENABLED:
        return None

    art_brief = generate_image_art_brief(synthesis_body, report_title)
    image_prompt = art_brief["image_prompt"]
    overlay_headline = art_brief["overlay_headline"]
    base_path = os.path.splitext(report_path)[0]
    raw_image_path = f"{base_path}_raw.png"
    final_image_path = f"{base_path}_image.png"
    prompt_path = f"{base_path}_image_prompt.txt"
    stats_path = f"{base_path}_image_stats.json"
    seed = random.randint(1, 2**31 - 1)

    started_at = time.perf_counter()
    try:
        progress_tracker.detail(
            f"Generating report image with {IMAGE_MODEL_LABEL} "
            f"({IMAGE_WIDTH}x{IMAGE_HEIGHT}, {IMAGE_STEPS} step(s))."
        )
        raw_image = generate_image_with_mflux(
            image_prompt,
            output_path=raw_image_path,
            seed=seed,
        )
        final_image = add_headline_overlay(
            raw_image,
            overlay_headline,
            crop_bottom_ratio=IMAGE_CROP_BOTTOM_RATIO,
        )
        final_image.save(final_image_path)
        with open(final_image_path, "rb") as image_file:
            image_bytes = image_file.read()
        encoded_image = base64.b64encode(image_bytes).decode("ascii")
        generation_seconds = time.perf_counter() - started_at
        stats = {
            "backend": "mflux",
            "model": IMAGE_MODEL_LABEL,
            "model_id": IMAGE_MODEL_ID,
            "base_model": IMAGE_BASE_MODEL,
            "width": IMAGE_WIDTH,
            "height": IMAGE_HEIGHT,
            "steps": IMAGE_STEPS,
            "seed": seed,
            "generation_seconds": round(generation_seconds, 2),
            "overlay_headline": overlay_headline,
            "crop_bottom_ratio": IMAGE_CROP_BOTTOM_RATIO,
            "raw_image_path": raw_image_path,
            "final_image_path": final_image_path,
        }
        with open(prompt_path, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(image_prompt + "\n")
        with open(stats_path, "w", encoding="utf-8") as stats_file:
            json.dump(stats, stats_file, indent=2)
            stats_file.write("\n")
        return {
            **stats,
            "prompt_path": prompt_path,
            "stats_path": stats_path,
            "image_prompt": image_prompt,
            "data_uri": f"data:image/png;base64,{encoded_image}",
            "art_prompt_error": art_brief.get("error"),
        }
    except Exception as error:
        message = f"Image generation failed: {error}"
        progress_tracker.warning(message)
        if IMAGE_GENERATION_FAIL_ON_ERROR:
            raise
        try:
            with open(prompt_path, "w", encoding="utf-8") as prompt_file:
                prompt_file.write(image_prompt + "\n")
        except Exception:
            pass
        return {
            "error": message,
            "backend": "mflux",
            "model": IMAGE_MODEL_LABEL,
            "model_id": IMAGE_MODEL_ID,
            "base_model": IMAGE_BASE_MODEL,
            "overlay_headline": overlay_headline,
            "prompt_path": prompt_path,
            "image_prompt": image_prompt,
        }


def _strip_inline_markdown(text: str) -> str:
    clean_text = str(text or "")
    clean_text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", clean_text)
    clean_text = re.sub(r"`([^`]*)`", r"\1", clean_text)
    clean_text = re.sub(r"\*\*(.*?)\*\*", r"\1", clean_text)
    clean_text = re.sub(r"\*(.*?)\*", r"\1", clean_text)
    clean_text = re.sub(r"__(.*?)__", r"\1", clean_text)
    clean_text = re.sub(r"_(.*?)_", r"\1", clean_text)
    return clean_text


def _format_plain_text_synthesis(synthesis_body: str) -> str:
    cleaned = _strip_inline_markdown(
        _strip_prompt_echo_lines(strip_model_artifacts(synthesis_body))
    ).replace("\r\n", "\n")
    formatted_lines: list[str] = []

    for raw_line in cleaned.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if formatted_lines and formatted_lines[-1] != "":
                formatted_lines.append("")
            continue
        heading_match = re.match(r"^(#{2,6})\s+(.+)$", stripped)
        if heading_match:
            heading_level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()
            if formatted_lines and formatted_lines[-1] != "":
                formatted_lines.append("")
            formatted_lines.append(heading)
            formatted_lines.append(("-" if heading_level == 2 else "~") * len(heading))
            formatted_lines.append("")
            continue
        formatted_lines.append(stripped)

    return "\n".join(formatted_lines).strip()


def _collect_grouped_headlines(
    final_reports: List[str],
) -> tuple[
    dict[str, List[tuple[str, str | None, str | None, str | None]]],
    dict[str, dict[str, List[tuple[str, str | None, str | None, str | None]]]],
]:
    grouped_headlines: dict[str, List[tuple[str, str | None, str | None, str | None]]] = {}
    grouped_by_topic: dict[str, dict[str, List[tuple[str, str | None, str | None, str | None]]]] = {}
    seen_pairs: set[tuple[str, str, str, str]] = set()
    for entry in final_reports:
        source_match = re.search(r"^- Source:\s*(.+)$", entry, flags=re.MULTILINE)
        source_name = source_match.group(1).strip() if source_match else "Unknown source"
        source_config = SOURCE_FEEDS.get(source_name)
        display_name = (
            source_config.get("name", source_name)
            if isinstance(source_config, dict)
            else source_name
        )
        homepage_url = (
            source_config.get("homepage")
            if isinstance(source_config, dict)
            else None
        )
        title_match = re.search(r"^###\s+(.+)$", entry, flags=re.MULTILINE)
        title_text = title_match.group(1).strip() if title_match else "Untitled article"
        url_match = re.search(r"^- URL:\s*(.+)$", entry, flags=re.MULTILINE)
        url_text = url_match.group(1).strip() if url_match else ""
        topic_match = re.search(r"^- Topic:\s*(.+)$", entry, flags=re.MULTILINE)
        topic_title = topic_match.group(1).strip() if topic_match else ""
        story_match = re.search(r"^- Story:\s*(.+)$", entry, flags=re.MULTILINE)
        story_title = story_match.group(1).strip() if story_match else ""
        normalized_url = url_text if url_text and url_text != "N/A" else ""
        dedupe_key = (topic_title, source_name, title_text, normalized_url)
        if dedupe_key in seen_pairs:
            continue
        seen_pairs.add(dedupe_key)

        if topic_title:
            topic_sources = grouped_by_topic.setdefault(topic_title, {})
            topic_sources.setdefault(display_name, []).append(
                (title_text, normalized_url or None, homepage_url, story_title or None)
            )
        else:
            grouped_headlines.setdefault(display_name, []).append(
                (title_text, normalized_url or None, homepage_url, story_title or None)
            )

    return grouped_headlines, grouped_by_topic


def _build_plain_text_article_listing(final_reports: List[str], topics: List[dict]) -> str:
    grouped_headlines, grouped_by_topic = _collect_grouped_headlines(final_reports)
    grouped_sections: list[str] = []
    topic_order = [topic["title"] for topic in topics]

    if grouped_by_topic:
        remaining_topics = [t for t in grouped_by_topic.keys() if t not in topic_order]
        for topic_title in topic_order + sorted(remaining_topics):
            source_map = grouped_by_topic.get(topic_title)
            if not source_map:
                continue
            topic_lines = [topic_title, "-" * len(topic_title), ""]
            for display_name, source_entries in source_map.items():
                first_entry = source_entries[0] if source_entries else None
                homepage_url = first_entry[2] if first_entry else None
                topic_lines.append(f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:")
                topic_lines.append("")
                for headline, link, _, _story_title in source_entries:
                    headline_label = f"[{_story_title}] {headline}" if _story_title else headline
                    topic_lines.append(
                        f"- {headline_label} ({link})" if link else f"- {headline_label}"
                    )
                topic_lines.append("")
            grouped_sections.append("\n".join(topic_lines).strip())
        if grouped_headlines:
            for display_name, source_entries in grouped_headlines.items():
                first_entry = source_entries[0] if source_entries else None
                homepage_url = first_entry[2] if first_entry else None
                lines = [f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:", ""]
                for headline, link, _, _story_title in source_entries:
                    headline_label = f"[{_story_title}] {headline}" if _story_title else headline
                    lines.append(f"- {headline_label} ({link})" if link else f"- {headline_label}")
                grouped_sections.append("\n".join(lines))
    else:
        for display_name, source_entries in grouped_headlines.items():
            first_entry = source_entries[0] if source_entries else None
            homepage_url = first_entry[2] if first_entry else None
            lines = [f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:", ""]
            for headline, link, _, _story_title in source_entries:
                headline_label = f"[{_story_title}] {headline}" if _story_title else headline
                lines.append(f"- {headline_label} ({link})" if link else f"- {headline_label}")
            grouped_sections.append("\n".join(lines))

    return "\n\n".join(grouped_sections) if grouped_sections else "No article headlines available."


def _render_html_paragraphs(
    block_text: str,
    citation_sources: list[dict[str, Any]] | None = None,
) -> str:
    paragraphs = [segment.strip() for segment in re.split(r"\n\s*\n", block_text.strip()) if segment.strip()]
    return "".join(
        f"<p class=\"email-paragraph\" style=\"margin:0 0 18px; font-size:16px; line-height:1.7; color:#1f2937;\">"
        f"{citations_stage.render_html_text_with_citations(paragraph, citation_sources or [])}"
        f"</p>"
        for paragraph in paragraphs
    )


def _build_html_synthesis(
    synthesis_body: str,
    citation_sources: list[dict[str, Any]] | None = None,
) -> str:
    cleaned = _strip_inline_markdown(
        _strip_prompt_echo_lines(strip_model_artifacts(synthesis_body))
    ).replace("\r\n", "\n")
    blocks: list[str] = []
    current_heading: str | None = None
    current_heading_level: int | None = None
    current_lines: list[str] = []

    def flush_section() -> None:
        nonlocal current_heading, current_heading_level, current_lines
        if current_heading is None and not current_lines:
            return
        if current_heading is not None:
            current_heading_html = citations_stage.render_html_text_with_citations(
                current_heading,
                citation_sources or [],
            )
            if current_heading_level == 2:
                blocks.append(
                    f"<h2 style=\"margin:32px 0 12px; font-size:22px; line-height:1.3; "
                    f"font-weight:700; color:#111827; letter-spacing:0.01em; text-transform:uppercase;\">"
                    f"{current_heading_html}</h2>"
                )
            else:
                blocks.append(
                    f"<h3 style=\"margin:22px 0 10px; font-size:18px; line-height:1.35; "
                    f"font-weight:800; color:#111827;\">"
                    f"{current_heading_html}</h3>"
                )
        section_text = "\n".join(current_lines).strip()
        if section_text:
            blocks.append(_render_html_paragraphs(section_text, citation_sources))
        current_heading = None
        current_heading_level = None
        current_lines = []

    for raw_line in cleaned.splitlines():
        stripped = raw_line.strip()
        heading_match = re.match(r"^(#{2,6})\s+(.+)$", stripped)
        if heading_match:
            flush_section()
            current_heading_level = len(heading_match.group(1))
            current_heading = heading_match.group(2).strip()
            continue
        if not stripped:
            current_lines.append("")
            continue
        current_lines.append(stripped)

    flush_section()
    return "".join(blocks)


def _build_html_article_listing(final_reports: List[str], topics: List[dict]) -> str:
    grouped_headlines, grouped_by_topic = _collect_grouped_headlines(final_reports)
    topic_order = [topic["title"] for topic in topics]

    def render_source_entries(
        source_map: dict[str, List[tuple[str, str | None, str | None, str | None]]],
    ) -> str:
        sections: list[str] = []
        for display_name, source_entries in source_map.items():
            first_entry = source_entries[0] if source_entries else None
            homepage_url = first_entry[2] if first_entry else None
            source_label = html.escape(display_name)
            source_heading = (
                f"<a href=\"{html.escape(homepage_url, quote=True)}\" "
                f"style=\"color:#0f172a; text-decoration:none;\">{source_label}</a>"
                if homepage_url
                else source_label
            )
            items = []
            for headline, link, _, _story_title in source_entries:
                headline_label = headline
                headline_text = html.escape(headline_label)
                if link:
                    item_text = (
                        f"<a href=\"{html.escape(link, quote=True)}\" "
                        f"style=\"color:#2563eb; text-decoration:none;\">{headline_text}</a>"
                    )
                else:
                    item_text = headline_text
                items.append(
                    f"<li style=\"margin:0 0 8px; font-size:15px; line-height:1.6; color:#374151;\">"
                    f"{item_text}</li>"
                )
            sections.append(
                "<div style=\"margin:0 0 22px;\">"
                f"<h3 style=\"margin:0 0 10px; font-size:17px; line-height:1.4; font-weight:700; color:#0f172a;\">{source_heading}</h3>"
                f"<ul style=\"margin:0; padding-left:20px;\">{''.join(items)}</ul>"
                "</div>"
            )
        return "".join(sections)

    html_sections: list[str] = []
    if grouped_by_topic:
        remaining_topics = [t for t in grouped_by_topic.keys() if t not in topic_order]
        for topic_title in topic_order + sorted(remaining_topics):
            source_map = grouped_by_topic.get(topic_title)
            if not source_map:
                continue
            html_sections.append(
                "<section style=\"margin:0 0 28px;\">"
                f"<h3 style=\"margin:0 0 14px; font-size:18px; line-height:1.4; font-weight:700; color:#111827;\">{html.escape(topic_title)}</h3>"
                f"{render_source_entries(source_map)}"
                "</section>"
            )
        if grouped_headlines:
            html_sections.append(render_source_entries(grouped_headlines))
    else:
        html_sections.append(render_source_entries(grouped_headlines))

    if not any(section.strip() for section in html_sections):
        return "<p style=\"margin:0; font-size:15px; line-height:1.6; color:#4b5563;\">No article headlines available.</p>"

    return "".join(html_sections)


def build_report_body(
    report_title: str,
    synthesis_body: str,
    final_reports: List[str],
    topics: List[dict],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
) -> str:
    cleaned_synthesis_body = _format_plain_text_synthesis(synthesis_body)
    clean_citation_sources = citation_sources or []
    article_listing = _build_plain_text_article_listing(final_reports, topics)
    citation_listing = citations_stage.render_plain_text_sources(clean_citation_sources)
    image_section = ""
    if image_art:
        image_lines = ["IMAGE", "=====", ""]
        if image_art.get("final_image_path"):
            image_lines.append(f"Generated image: {image_art.get('final_image_path')}")
        if image_art.get("overlay_headline"):
            image_lines.append(f"Overlay headline: {image_art.get('overlay_headline')}")
        if image_art.get("prompt_path"):
            image_lines.append(f"Image prompt: {image_art.get('prompt_path')}")
        if image_art.get("error"):
            image_lines.append(f"Image generation warning: {image_art.get('error')}")
        image_section = "\n".join(image_lines).strip() + "\n\n"

    title_text = re.sub(r"\s+", " ", str(report_title or "")).strip()
    title_section = ""
    if title_text:
        title_section = f"{title_text}\n{'=' * len(title_text)}\n\n"

    source_heading = "SOURCES" if citation_listing else "ARTICLES BY SOURCE"
    source_rule = "=" * len(source_heading)
    source_body = citation_listing or article_listing

    return (
        f"{title_section}"
        f"{image_section}"
        f"{cleaned_synthesis_body}\n\n"
        f"{source_heading}\n"
        f"{source_rule}\n\n"
        f"{source_body}\n"
    )


def build_report_html(
    recipient_email: str,
    recipient_name: str,
    report_title: str,
    synthesis_body: str,
    final_reports: List[str],
    topics: List[dict],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
) -> str:
    first_name = _extract_first_name(recipient_name)
    clean_citation_sources = citation_sources or []
    synthesis_html = _build_html_synthesis(synthesis_body, clean_citation_sources)
    article_listing_html = _build_html_article_listing(final_reports, topics)
    source_listing_html = (
        citations_stage.render_html_sources(clean_citation_sources)
        if clean_citation_sources
        else article_listing_html
    )
    unsubscribe_url = build_unsubscribe_url(recipient_email)
    title_text = re.sub(r"\s+", " ", str(report_title or "")).strip()
    title_html = (
        "<h1 style=\"margin:0 0 24px; font-size:30px; line-height:1.2; "
        "font-weight:800; color:#111827; letter-spacing:0;\">"
        f"{html.escape(title_text)}</h1>"
        if title_text
        else ""
    )
    image_html = ""
    if image_art and image_art.get("content_id"):
        image_alt = image_art.get("overlay_headline") or report_title or "Daily News Summary"
        image_html = (
            "<img "
            f"alt=\"{html.escape(str(image_alt), quote=True)}\" "
            f"src=\"cid:{html.escape(str(image_art['content_id']), quote=True)}\" "
            "style=\"display:block; width:100%; height:auto; margin:0 0 30px; border-radius:6px;\">"
        )
    elif image_art and image_art.get("data_uri"):
        image_alt = image_art.get("overlay_headline") or report_title or "Daily News Summary"
        image_html = (
            "<img "
            f"alt=\"{html.escape(str(image_alt), quote=True)}\" "
            f"src=\"{html.escape(str(image_art['data_uri']), quote=True)}\" "
            "style=\"display:block; width:100%; height:auto; margin:0 0 30px; border-radius:6px;\">"
        )

    return (
        "<!doctype html>"
        "<html><head>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
        "<style>"
        "@media only screen and (max-width:600px){"
        ".email-shell{padding:12px 0 !important;}"
        ".email-card{width:100% !important; max-width:none !important; border-radius:0 !important;}"
        ".email-content{padding:30px 18px 22px !important;}"
        ".email-paragraph{font-size:16px !important; line-height:1.65 !important;}"
        "}"
        "</style>"
        "</head><body style=\"margin:0; padding:0; background-color:#f3f4f6;\">"
        "<div class=\"email-shell\" style=\"margin:0; padding:20px 0; width:100%;\">"
        "<div class=\"email-card\" style=\"width:100%; max-width:1040px; margin:0 auto; background:#ffffff; border-radius:8px; overflow:hidden; box-sizing:border-box;\">"
        "<div class=\"email-content\" style=\"padding:36px 32px 24px; box-sizing:border-box;\">"
        f"<p style=\"margin:0 0 24px; font-size:18px; line-height:1.6; color:#111827;\">{html.escape(first_name)},</p>"
        "<p style=\"margin:0 0 28px; font-size:17px; line-height:1.7; color:#374151;\">Here is your daily news summary.</p>"
        f"{title_html}"
        f"{image_html}"
        f"{synthesis_html}"
        "<hr style=\"border:none; border-top:1px solid #e5e7eb; margin:36px 0 28px;\">"
        "<h2 style=\"margin:0 0 18px; font-size:22px; line-height:1.3; font-weight:800; color:#111827; letter-spacing:0.01em;\">Sources</h2>"
        f"{source_listing_html}"
        "<div style=\"margin:34px 0 0; padding-top:22px; border-top:1px solid #e5e7eb; text-align:center;\">"
        f"<a href=\"{html.escape(unsubscribe_url, quote=True)}\" "
        "style=\"display:inline-block; padding:10px 16px; border-radius:6px; background:#f3f4f6; "
        "color:#374151; font-size:14px; line-height:1.3; text-decoration:none; font-weight:700;\">"
        "Unsubscribe</a>"
        "</div>"
        "</div></div></div></body></html>"
    )


def write_report_asset(report_path: str, report_body: str) -> None:
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(report_body)


def _report_entry_debug_record(entry: str, index: int) -> dict[str, Any]:
    title_match = re.search(r"^###\s+(.+)$", entry or "", flags=re.MULTILINE)
    source_match = re.search(r"^- Source:\s*(.+)$", entry or "", flags=re.MULTILINE)
    published_match = re.search(r"^- Published:\s*(.+)$", entry or "", flags=re.MULTILINE)
    url_match = re.search(r"^- URL:\s*(.+)$", entry or "", flags=re.MULTILINE)
    article_id_match = re.search(r"^- Article ID:\s*(.+)$", entry or "", flags=re.MULTILINE)
    topic_match = re.search(r"^- Topic:\s*(.+)$", entry or "", flags=re.MULTILINE)
    story_match = re.search(r"^- Story:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return {
        "index": index,
        "title": title_match.group(1).strip() if title_match else "",
        "source": source_match.group(1).strip() if source_match else "",
        "published": published_match.group(1).strip() if published_match else "",
        "url": url_match.group(1).strip() if url_match else "",
        "article_id": article_id_match.group(1).strip() if article_id_match else "",
        "topic": topic_match.group(1).strip() if topic_match else "",
        "story": story_match.group(1).strip() if story_match else "",
        "summary": _report_summary_text(entry),
        "raw_entry": entry,
    }


def _topic_display_title(topic: dict) -> str:
    return str(topic.get("title") or topic.get("key") or "Unknown topic")


def _flatten_topic_story_records(
    story_cluster_stats: dict,
    *,
    pool_key: str = "stories_by_topic",
) -> list[dict]:
    """Flatten a story pool dict into a list, preserving topic_title."""
    records: list[dict] = []
    for topic_title, story_list in (story_cluster_stats.get(pool_key) or {}).items():
        for record in story_list:
            records.append({**record, "topic_title": str(topic_title)})
    return records


def _story_coverage_deficits(
    selected_by_topic: dict,
    topics: list[dict],
    *,
    max_stories_per_topic: int = MAX_STORIES_PER_TOPIC,
) -> dict[str, int]:
    deficits: dict[str, int] = {}
    for topic in topics:
        topic_title = _topic_display_title(topic)
        selected_count = int((selected_by_topic or {}).get(topic_title) or 0)
        deficit = max_stories_per_topic - selected_count
        if deficit > 0:
            deficits[topic_title] = deficit
    return deficits


def _select_reserve_story_batch(
    reserve_stories_by_topic: dict[str, list[dict]],
    deficits_by_topic: dict[str, int],
    topics: list[dict],
    attempted_story_keys: set[str],
    *,
    batch_multiplier: int = STORY_BACKFILL_BATCH_MULTIPLIER,
) -> list[dict]:
    batch: list[dict] = []
    topic_order = [_topic_display_title(topic) for topic in topics]
    extra_topic_titles = sorted(
        topic_title
        for topic_title in reserve_stories_by_topic
        if topic_title not in set(topic_order)
    )
    for topic_title in topic_order + extra_topic_titles:
        deficit = int(deficits_by_topic.get(topic_title) or 0)
        if deficit <= 0:
            continue
        limit = max(1, deficit * max(1, batch_multiplier))
        selected_for_topic = 0
        for story in reserve_stories_by_topic.get(topic_title) or []:
            story_key = str(story.get("story_key") or "").strip()
            if not story_key or story_key in attempted_story_keys:
                continue
            batch.append({**story, "topic_title": topic_title})
            selected_for_topic += 1
            if selected_for_topic >= limit:
                break
    return batch


def _article_targets_for_story_records(
    story_records: list[dict],
    article_lookup: dict[str, dict],
    *,
    existing_article_ids: set[str] | None = None,
) -> list[dict]:
    seen_article_ids: set[str] = set(existing_article_ids or set())
    article_targets: list[dict] = []
    for story in story_records:
        article_ids = story.get("cluster_article_ids") or story.get("article_ids") or []
        for article_id in article_ids:
            clean_article_id = str(article_id or "").strip()
            if not clean_article_id or clean_article_id in seen_article_ids:
                continue
            article = article_lookup.get(clean_article_id)
            if not article:
                continue
            seen_article_ids.add(clean_article_id)
            article_targets.append(
                {
                    **article,
                    "topic_key": story.get("topic_key") or article.get("topic_key"),
                    "topic_title": story.get("topic_title") or article.get("topic_title"),
                    "story_key": story.get("story_key"),
                    "story_title": story.get("story_title"),
                    "story_rank": story.get("story_rank"),
                    "story_article_count": story.get("cluster_article_count") or story.get("article_count"),
                    "story_selected_article_count": story.get("selected_article_count") or story.get("article_count"),
                    "story_average_similarity": story.get("average_similarity"),
                    "story_connectedness_score": story.get("connectedness_score"),
                    "story_strength_score": story.get("story_strength_score"),
                    "story_edge_density": story.get("edge_density"),
                    "story_min_member_average_similarity": story.get("min_member_average_similarity"),
                    "story_min_member_edge_degree": story.get("min_member_edge_degree"),
                    "story_member_cohesion_floor": story.get("member_cohesion_floor"),
                    "story_member_edge_degree_floor": story.get("member_edge_degree_floor"),
                    "story_pruned_article_ids": story.get("pruned_article_ids") or [],
                    "story_prune_reason": story.get("prune_reason") or "",
                    "story_source_count": story.get("source_count"),
                }
            )
    return article_targets


def _dedupe_story_drafts_for_topic_selection(
    story_drafts: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    drafts_before = len(story_drafts)
    try:
        drafts_by_topic: dict[str, list[dict]] = {}
        for draft in story_drafts:
            topic_key = str(draft.get("topic_title") or draft.get("topic_key") or "unassigned")
            drafts_by_topic.setdefault(topic_key, []).append(draft)
        deduped_story_drafts: list[dict] = []
        for _topic_group, drafts in drafts_by_topic.items():
            deduped_story_drafts.extend(
                embeddings_stage.dedup_story_drafts_within_topic(
                    drafts,
                    threshold=STORY_EMBEDDING_DEDUP_THRESHOLD,
                )
            )
        drafts_dropped = drafts_before - len(deduped_story_drafts)
        return deduped_story_drafts, {
            "before": drafts_before,
            "after": len(deduped_story_drafts),
            "dropped": drafts_dropped,
            "threshold": STORY_EMBEDDING_DEDUP_THRESHOLD,
        }
    except Exception as error:
        return story_drafts, {
            "before": drafts_before,
            "after": drafts_before,
            "dropped": 0,
            "threshold": STORY_EMBEDDING_DEDUP_THRESHOLD,
            "error": str(error),
            "fallback": "no_dedup",
        }


def _run_story_reserve_backfill(
    *,
    topics: list[dict],
    reserve_stories_by_topic: dict[str, list[dict]],
    article_lookup: dict[str, dict],
    article_summary_reports: list[str],
    clustered_article_targets: list[dict],
    story_drafts: list[dict],
    selected_story_topic_matches: list[dict],
    story_topic_stats: dict[str, Any],
    summarize_article_targets: Callable[[list[dict]], list[str]],
    draft_story_records: Callable[[list[dict], list[str], list[dict]], tuple[list[dict], dict[str, Any]]],
    classify_story_drafts: Callable[[list[dict]], tuple[list[dict], dict[str, Any]]],
    dedupe_story_drafts: Callable[[list[dict]], tuple[list[dict], dict[str, Any]]],
    max_stories_per_topic: int = MAX_STORIES_PER_TOPIC,
    batch_multiplier: int = STORY_BACKFILL_BATCH_MULTIPLIER,
) -> dict[str, Any]:
    article_summary_reports = list(article_summary_reports)
    clustered_article_targets = list(clustered_article_targets)
    story_drafts = list(story_drafts)
    selected_story_topic_matches = list(selected_story_topic_matches)
    story_topic_stats = dict(story_topic_stats or {})

    initial_selected_by_topic = dict(story_topic_stats.get("selected_by_topic") or {})
    deficits = _story_coverage_deficits(
        initial_selected_by_topic,
        topics,
        max_stories_per_topic=max_stories_per_topic,
    )
    deficits_before = dict(deficits)
    reserve_story_total = sum(len(stories) for stories in (reserve_stories_by_topic or {}).values())
    attempted_story_keys = {
        str(story.get("story_key") or "").strip()
        for story in story_drafts
        if str(story.get("story_key") or "").strip()
    }
    summarized_article_ids = set(story_drafting_stage.article_summary_lookup_by_id(article_summary_reports))
    clustered_article_ids = {
        str(article.get("article_id") or "").strip()
        for article in clustered_article_targets
        if str(article.get("article_id") or "").strip()
    }
    attempted_story_count_by_topic: Counter[str] = Counter()
    attempted_article_ids: set[str] = set()
    new_article_summary_count = 0
    new_story_draft_count = 0
    iterations = 0

    if deficits and reserve_story_total:
        while deficits:
            batch = _select_reserve_story_batch(
                reserve_stories_by_topic,
                deficits,
                topics,
                attempted_story_keys,
                batch_multiplier=batch_multiplier,
            )
            if not batch:
                break

            iterations += 1
            for story in batch:
                story_key = str(story.get("story_key") or "").strip()
                if story_key:
                    attempted_story_keys.add(story_key)
                attempted_story_count_by_topic[str(story.get("topic_title") or "Unknown topic")] += 1
                for article_id in story.get("cluster_article_ids") or story.get("article_ids") or []:
                    clean_article_id = str(article_id or "").strip()
                    if clean_article_id:
                        attempted_article_ids.add(clean_article_id)

            batch_article_targets = _article_targets_for_story_records(
                batch,
                article_lookup,
                existing_article_ids=clustered_article_ids,
            )
            for article in batch_article_targets:
                article_id = str(article.get("article_id") or "").strip()
                if article_id:
                    clustered_article_ids.add(article_id)
            clustered_article_targets.extend(batch_article_targets)

            new_article_targets = [
                article
                for article in batch_article_targets
                if str(article.get("article_id") or "").strip() not in summarized_article_ids
            ]
            if new_article_targets:
                new_reports = summarize_article_targets(new_article_targets)
                article_summary_reports.extend(new_reports)
                summarized_article_ids.update(
                    story_drafting_stage.article_summary_lookup_by_id(new_reports)
                )
                new_article_summary_count += len(new_reports)

            new_story_drafts, _draft_stats = draft_story_records(
                batch,
                article_summary_reports,
                clustered_article_targets,
            )
            if new_story_drafts:
                new_story_draft_count += len(new_story_drafts)
                story_drafts.extend(new_story_drafts)
                story_drafts, _dedup_stats = dedupe_story_drafts(story_drafts)
                selected_story_topic_matches, story_topic_stats = classify_story_drafts(story_drafts)

            deficits = _story_coverage_deficits(
                story_topic_stats.get("selected_by_topic") or {},
                topics,
                max_stories_per_topic=max_stories_per_topic,
            )

    final_selected_by_topic = dict(story_topic_stats.get("selected_by_topic") or {})
    deficits_after = _story_coverage_deficits(
        final_selected_by_topic,
        topics,
        max_stories_per_topic=max_stories_per_topic,
    )
    exhausted_topics: list[str] = []
    for topic_title in deficits_after:
        has_unattempted_reserve = any(
            str(story.get("story_key") or "").strip()
            and str(story.get("story_key") or "").strip() not in attempted_story_keys
            for story in (reserve_stories_by_topic or {}).get(topic_title) or []
        )
        if not has_unattempted_reserve:
            exhausted_topics.append(topic_title)

    return {
        "article_summary_reports": article_summary_reports,
        "clustered_article_targets": clustered_article_targets,
        "story_drafts": story_drafts,
        "selected_story_topic_matches": selected_story_topic_matches,
        "story_topic_stats": story_topic_stats,
        "stats": {
            "enabled": bool(deficits_before and reserve_story_total),
            "iterations": iterations,
            "initial_selected_by_topic": initial_selected_by_topic,
            "final_selected_by_topic": final_selected_by_topic,
            "deficits_before": deficits_before,
            "deficits_after": deficits_after,
            "attempted_story_count_by_topic": dict(attempted_story_count_by_topic),
            "attempted_article_count": len(attempted_article_ids),
            "new_article_summary_count": new_article_summary_count,
            "new_story_draft_count": new_story_draft_count,
            "exhausted_topics": sorted(exhausted_topics),
            "reserve_story_count": reserve_story_total,
            "batch_multiplier": max(1, batch_multiplier),
        },
    }


def _persist_article_summaries_debug(
    final_reports: List[str],
    *,
    label: str = "article_summaries",
) -> str | None:
    if not final_reports:
        return None
    safe_label = re.sub(r"[^a-zA-Z0-9_]+", "_", label).strip("_") or "article_summaries"
    debug_path = os.path.join(RUN_OUTPUT_DIR, f"{safe_label}_{timestamp}.json")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(final_reports),
        "summaries": [
            _report_entry_debug_record(entry, index)
            for index, entry in enumerate(final_reports, start=1)
        ],
    }
    try:
        with open(debug_path, "w", encoding="utf-8") as debug_file:
            json.dump(payload, debug_file, indent=2)
            debug_file.write("\n")
        return debug_path
    except Exception:
        return None


def _persist_grouped_synthesis_dataset_debug(
    report_path: str,
    token_stats: dict[str, Any],
) -> dict[str, str]:
    primary_dataset = str((token_stats or {}).get("primary_dataset") or "").strip()
    if not primary_dataset:
        return {}
    base_path = os.path.splitext(report_path)[0]
    dataset_path = f"{base_path}_primary_dataset.txt"
    metadata_path = f"{base_path}_primary_dataset.json"
    metadata = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required_topic_titles": token_stats.get("required_topic_titles", []),
        "required_topic_headings": token_stats.get("required_topic_headings", []),
        "required_story_blocks_by_topic": token_stats.get("required_story_blocks_by_topic", {}),
        "eligible_story_block_count": token_stats.get("eligible_story_block_count", 0),
        "reports_included_in_synthesis": token_stats.get("reports_included_in_synthesis", 0),
        "reports_omitted_from_synthesis": token_stats.get("reports_omitted_from_synthesis", 0),
        "high_confidence_reports": token_stats.get("high_confidence_reports", 0),
        "low_confidence_reports": token_stats.get("low_confidence_reports", 0),
    }
    try:
        with open(dataset_path, "w", encoding="utf-8") as dataset_file:
            dataset_file.write(primary_dataset)
            dataset_file.write("\n")
        with open(metadata_path, "w", encoding="utf-8") as metadata_file:
            json.dump(metadata, metadata_file, indent=2)
            metadata_file.write("\n")
        token_stats["primary_dataset_path"] = dataset_path
        token_stats["primary_dataset_metadata_path"] = metadata_path
        token_stats.pop("primary_dataset", None)
        return {
            "primary_dataset_path": dataset_path,
            "primary_dataset_metadata_path": metadata_path,
        }
    except Exception:
        return {}


def _persist_topics_debug(topics: List[dict], stories: List[dict]) -> None:
    debug_path = os.path.join(RUN_OUTPUT_DIR, f"topics_{timestamp}.json")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "discovered_at": timestamp,
                    "raw_top_stories": stories,
                    "provider_metadata": LAST_TOP_FUNNEL_PROVIDER_METADATA,
                    "topics": topics,
                },
                f,
                indent=2,
            )
    except Exception:
        pass


def _article_sort_datetime(article: dict) -> datetime:
    parsed = _parse_feed_datetime(article.get("pub_date"))
    return parsed or datetime.min.replace(tzinfo=None)


def _article_time_rank(article: dict) -> tuple[int, int, int]:
    parsed = _article_sort_datetime(article)
    return (parsed.toordinal(), parsed.hour * 3600 + parsed.minute * 60 + parsed.second, parsed.microsecond)


def _budget_story_article_targets(
    article_targets: List[dict],
    topics: List[dict],
    *,
    total_cap: int,
    per_topic_cap: int,
    per_source_topic_cap: int,
    min_articles_per_story: int,
) -> tuple[List[dict], dict[str, Any]]:
    topic_order = {
        str(topic.get("key") or ""): index
        for index, topic in enumerate(topics)
    }
    topic_titles_by_key = {
        str(topic.get("key") or ""): str(topic.get("title") or topic.get("key") or "Unknown")
        for topic in topics
    }
    indexed_articles = list(enumerate(article_targets))
    story_order: dict[str, int] = {}
    articles_by_story: dict[str, list[tuple[int, dict]]] = {}
    for index, article in indexed_articles:
        story_key = str(article.get("story_key") or "").strip()
        if not story_key:
            continue
        story_order.setdefault(story_key, len(story_order))
        articles_by_story.setdefault(story_key, []).append((index, article))

    def topic_title_for(article: dict) -> str:
        topic_key = str(article.get("topic_key") or "")
        return topic_titles_by_key.get(topic_key, article.get("topic_title") or "Unknown")

    def story_rank(item: tuple[str, list[tuple[int, dict]]]) -> tuple:
        story_key, story_articles = item
        first_article = story_articles[0][1]
        topic_key = str(first_article.get("topic_key") or "")
        article_count = max(
            int(article.get("story_article_count") or len(story_articles))
            for _, article in story_articles
        )
        average_similarity = max(
            float(article.get("story_average_similarity") or 0.0)
            for _, article in story_articles
        )
        story_strength = max(
            float(article.get("story_strength_score") or 0.0)
            for _, article in story_articles
        )
        connectedness = max(
            float(article.get("story_connectedness_score") or 0.0)
            for _, article in story_articles
        )
        source_count = len({str(article.get("source") or "") for _, article in story_articles})
        relevance_score = sum(
            float(article.get("relevance_score") or 0.0)
            for _, article in story_articles
        ) / max(1, len(story_articles))
        recency_rank = max(_article_time_rank(article) for _, article in story_articles)
        return (
            topic_order.get(topic_key, len(topic_order)),
            -story_strength,
            -connectedness,
            -article_count,
            -source_count,
            -average_similarity,
            -relevance_score,
            tuple(-value for value in recency_rank),
            story_order[story_key],
        )

    def article_rank(item: tuple[int, dict]) -> tuple:
        index, article = item
        return (
            -float(article.get("story_strength_score") or 0.0),
            -float(article.get("story_connectedness_score") or 0.0),
            -float(article.get("story_average_similarity") or 0.0),
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            str(article.get("source") or ""),
            index,
        )

    selected: list[tuple[int, dict]] = []
    selected_indices: set[int] = set()
    selected_story_keys: set[str] = set()
    selected_by_topic_key: Counter[str] = Counter()
    selected_by_source: Counter[str] = Counter()
    selected_by_source_story: Counter[tuple[str, str]] = Counter()

    def remaining_total_capacity() -> int | None:
        return None if total_cap <= 0 else total_cap - len(selected)

    def remaining_topic_capacity(topic_key: str) -> int | None:
        return None if per_topic_cap <= 0 else per_topic_cap - selected_by_topic_key[topic_key]

    def can_fit_floor(story_articles: list[tuple[int, dict]]) -> bool:
        if len(story_articles) < min_articles_per_story:
            return False
        topic_key = str(story_articles[0][1].get("topic_key") or "")
        total_remaining = remaining_total_capacity()
        topic_remaining = remaining_topic_capacity(topic_key)
        if total_remaining is not None and total_remaining < min_articles_per_story:
            return False
        if topic_remaining is not None and topic_remaining < min_articles_per_story:
            return False
        return True

    def mark_selected(item: tuple[int, dict]) -> None:
        _, article = item
        topic_key = str(article.get("topic_key") or "")
        source = str(article.get("source") or "")
        story_key = str(article.get("story_key") or "")
        selected_by_topic_key[topic_key] += 1
        selected_by_source[source] += 1
        selected_by_source_story[(story_key, source)] += 1

    def can_select_extra(item: tuple[int, dict]) -> bool:
        _, article = item
        topic_key = str(article.get("topic_key") or "")
        source = str(article.get("source") or "")
        story_key = str(article.get("story_key") or "")
        if total_cap > 0 and len(selected) >= total_cap:
            return False
        if per_topic_cap > 0 and selected_by_topic_key[topic_key] >= per_topic_cap:
            return False
        if (
            per_source_topic_cap > 0
            and selected_by_source_story[(story_key, source)] >= per_source_topic_cap
        ):
            return False
        return True

    def add_selected(item: tuple[int, dict]) -> None:
        selected.append(item)
        selected_indices.add(item[0])
        selected_story_keys.add(str(item[1].get("story_key") or ""))
        mark_selected(item)

    for story_key, story_articles in sorted(articles_by_story.items(), key=story_rank):
        if not can_fit_floor(story_articles):
            continue
        for item in sorted(story_articles, key=article_rank)[:min_articles_per_story]:
            add_selected(item)

    remaining = [
        item
        for item in indexed_articles
        if item[0] not in selected_indices
        and str(item[1].get("story_key") or "") in selected_story_keys
    ]
    while remaining and (total_cap <= 0 or len(selected) < total_cap):
        selectable = [item for item in remaining if can_select_extra(item)]
        if not selectable:
            break

        def extra_rank(item: tuple[int, dict]) -> tuple:
            index, article = item
            topic_key = str(article.get("topic_key") or "")
            source = str(article.get("source") or "")
            story_key = str(article.get("story_key") or "")
            return (
                story_order.get(story_key, len(story_order)),
                selected_by_source_story[(story_key, source)],
                selected_by_source[source],
                topic_order.get(topic_key, len(topic_order)),
                -float(article.get("story_strength_score") or 0.0),
                -float(article.get("story_connectedness_score") or 0.0),
                -float(article.get("story_average_similarity") or 0.0),
                -int(article.get("relevance_score") or 0),
                tuple(-value for value in _article_time_rank(article)),
                source,
                index,
            )

        chosen = sorted(selectable, key=extra_rank)[0]
        add_selected(chosen)
        remaining = [item for item in remaining if item[0] != chosen[0]]

    selected_sorted = [
        article
        for _, article in sorted(selected, key=lambda candidate: candidate[0])
    ]
    included_ids = {id(article) for article in selected_sorted}
    dropped_targets = [article for article in article_targets if id(article) not in included_ids]

    def count_by_topic(articles: list[dict]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for article in articles:
            counts[topic_title_for(article)] += 1
        return dict(counts)

    def count_by_story(articles: list[dict]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for article in articles:
            story_title = str(article.get("story_title") or article.get("story_key") or "Unknown")
            counts[f"{topic_title_for(article)} | {story_title}"] += 1
        return dict(counts)

    story_labels_by_key: dict[str, str] = {}
    for article in selected_sorted:
        story_key = str(article.get("story_key") or "")
        story_title = str(article.get("story_title") or story_key or "Unknown")
        story_labels_by_key.setdefault(story_key, f"{topic_title_for(article)} | {story_title}")

    return selected_sorted, {
        "enabled": True,
        "story_aware": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected_sorted),
        "dropped_count": len(dropped_targets),
        "total_cap": total_cap,
        "per_topic_cap": per_topic_cap,
        "per_source_topic_cap": per_source_topic_cap,
        "min_articles_per_story": min_articles_per_story,
        "included_by_topic": count_by_topic(selected_sorted),
        "dropped_by_topic": count_by_topic(dropped_targets),
        "included_by_story": count_by_story(selected_sorted),
        "dropped_by_story": count_by_story(dropped_targets),
        "included_by_source": dict(Counter(str(article.get("source") or "") for article in selected_sorted)),
        "included_by_source_topic": {
            f"{story_labels_by_key.get(story_key, story_key or 'Unknown')} | {source}": count
            for (story_key, source), count in selected_by_source_story.items()
        },
        "selected_story_count": len(selected_story_keys),
        "dropped_article_ids": [article.get("article_id") for article in dropped_targets],
    }


def budget_article_targets(
    article_targets: List[dict],
    topics: List[dict],
    *,
    total_cap: int = TOTAL_ARTICLE_SUMMARY_CAP,
    per_topic_cap: int = PER_TOPIC_ARTICLE_SUMMARY_CAP,
    per_source_topic_cap: int = PER_SOURCE_TOPIC_ARTICLE_CAP,
) -> tuple[List[dict], dict[str, Any]]:
    if total_cap <= 0 and per_topic_cap <= 0 and per_source_topic_cap <= 0:
        return article_targets, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "per_source_topic_cap": per_source_topic_cap,
        }

    if any(str(article.get("story_key") or "").strip() for article in article_targets):
        return _budget_story_article_targets(
            article_targets,
            topics,
            total_cap=total_cap,
            per_topic_cap=per_topic_cap,
            per_source_topic_cap=per_source_topic_cap,
            min_articles_per_story=MIN_ARTICLES_PER_STORY,
        )

    topic_order = {
        str(topic.get("key") or ""): index
        for index, topic in enumerate(topics)
    }
    candidates = [
        (index, article)
        for index, article in enumerate(article_targets)
    ]

    def base_rank(candidate: tuple[int, dict]) -> tuple:
        index, article = candidate
        topic_index = topic_order.get(str(article.get("topic_key") or ""), len(topic_order))
        return (
            topic_index,
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            str(article.get("source") or ""),
            index,
        )

    selected: list[tuple[int, dict]] = []
    selected_indices: set[int] = set()
    selected_by_topic_key: Counter[str] = Counter()
    selected_by_source: Counter[str] = Counter()
    selected_by_source_topic: Counter[tuple[str, str]] = Counter()

    def can_select(article: dict) -> bool:
        topic_key = str(article.get("topic_key") or "")
        source = str(article.get("source") or "")
        if per_topic_cap > 0 and selected_by_topic_key[topic_key] >= per_topic_cap:
            return False
        if (
            per_source_topic_cap > 0
            and selected_by_source_topic[(topic_key, source)] >= per_source_topic_cap
        ):
            return False
        if total_cap > 0 and len(selected) >= total_cap:
            return False
        return True

    def mark_selected(article: dict) -> None:
        topic_key = str(article.get("topic_key") or "")
        source = str(article.get("source") or "")
        selected_by_topic_key[topic_key] += 1
        selected_by_source[source] += 1
        selected_by_source_topic[(topic_key, source)] += 1

    # First pass: keep at least one strong article for each selected topic when possible.
    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_candidates = [
            candidate
            for candidate in candidates
            if candidate[0] not in selected_indices
            and str(candidate[1].get("topic_key") or "") == topic_key
        ]
        if not topic_candidates:
            continue
        best = sorted(topic_candidates, key=base_rank)[0]
        if can_select(best[1]):
            selected.append(best)
            selected_indices.add(best[0])
            mark_selected(best[1])

    remaining = [candidate for candidate in candidates if candidate[0] not in selected_indices]
    while remaining and (total_cap <= 0 or len(selected) < total_cap):
        selectable = [
            candidate for candidate in remaining
            if can_select(candidate[1])
        ]
        if not selectable:
            break

        def diversity_rank(candidate: tuple[int, dict]) -> tuple:
            index, article = candidate
            source = str(article.get("source") or "")
            topic_key = str(article.get("topic_key") or "")
            topic_index = topic_order.get(topic_key, len(topic_order))
            return (
                selected_by_source_topic[(topic_key, source)],
                selected_by_source[source],
                selected_by_topic_key[topic_key],
                topic_index,
                -int(article.get("relevance_score") or 0),
                tuple(-value for value in _article_time_rank(article)),
                source,
                index,
            )

        chosen = sorted(selectable, key=diversity_rank)[0]
        selected.append(chosen)
        selected_indices.add(chosen[0])
        mark_selected(chosen[1])
        remaining = [candidate for candidate in remaining if candidate[0] != chosen[0]]

    selected_sorted = [
        article
        for _, article in sorted(selected, key=lambda candidate: candidate[0])
    ]
    included_ids = {id(article) for article in selected_sorted}
    dropped_targets = [article for article in article_targets if id(article) not in included_ids]
    topic_titles_by_key = {
        str(topic.get("key") or ""): str(topic.get("title") or topic.get("key") or "Unknown")
        for topic in topics
    }

    def count_by_topic(articles: list[dict]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for article in articles:
            topic_key = str(article.get("topic_key") or "")
            counts[topic_titles_by_key.get(topic_key, article.get("topic_title") or "Unknown")] += 1
        return dict(counts)

    stats = {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected_sorted),
        "dropped_count": len(dropped_targets),
        "total_cap": total_cap,
        "per_topic_cap": per_topic_cap,
        "per_source_topic_cap": per_source_topic_cap,
        "included_by_topic": count_by_topic(selected_sorted),
        "dropped_by_topic": count_by_topic(dropped_targets),
        "included_by_source": dict(Counter(str(article.get("source") or "") for article in selected_sorted)),
        "included_by_source_topic": {
            f"{topic_titles_by_key.get(topic_key, topic_key or 'Unknown')} | {source}": count
            for (topic_key, source), count in selected_by_source_topic.items()
        },
        "dropped_article_ids": [article.get("article_id") for article in dropped_targets],
    }
    return selected_sorted, stats


def _new_run_diagnostics(source_count: int) -> RunDiagnostics:
    diagnostics = RunDiagnostics(
        run_started_at=RUN_STARTED_AT.isoformat(timespec="seconds"),
        settings={
            "run_mode": RUN_MODE,
            "dev": DEV,
            "local_prod": LOCAL_PROD,
            "loose_local_prod": LOOSE_LOCAL_PROD,
            "bradley_only_delivery": BRADLEY_ONLY_DELIVERY,
            "shared_url_history_enabled": SHARED_URL_HISTORY_ENABLED,
            "relaxed_final_synthesis_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
            "source_count": source_count,
            "sources_path": str(CONFIG.sources_path),
            "topic_mode": TOPIC_MODE,
            "client_path": str(CONFIG.client_path),
            "topics_path": str(CONFIG.topics_path),
            "active_topic_ids": list(ACTIVE_TOPIC_IDS),
            "top_funnel_provider_count": len(TOP_FUNNEL_PROVIDERS),
            "recipients_path": str(CONFIG.recipients_path),
            "output_dir": RUN_OUTPUT_DIR,
            "run_used_urls_path": RUN_USED_URLS_PATH,
            "run_log_path": RUN_LOG_PATH,
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "article_download_timeout_seconds": ARTICLE_DOWNLOAD_TIMEOUT_SECONDS,
            "article_scrape_total_timeout_seconds": ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS,
            "slow_source_warning_seconds": SLOW_SOURCE_WARNING_SECONDS,
            "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
            "topic_relevance_min_score": TOPIC_RELEVANCE_MIN_SCORE,
            "story_topic_fit_min_score": STORY_TOPIC_FIT_MIN_SCORE,
            "story_topic_validation_enabled": STORY_TOPIC_VALIDATION_ENABLED,
            "per_source_topic_article_cap": PER_SOURCE_TOPIC_ARTICLE_CAP,
            "num_top_topics": NUM_TOP_TOPICS,
            "top_topic_probes": TOP_TOPIC_PROBES,
            "top_of_funnel_per_provider": TOP_OF_FUNNEL_PER_PROVIDER,
            "topic_frame_targets": TOPIC_FRAME_TARGETS,
            "topic_frame_nudge_strength": TOPIC_FRAME_NUDGE_STRENGTH,
            "summary_scope_label": PROJECT_SUMMARY_SCOPE_LABEL,
            "model": MODEL_REFERENCE,
            "model_name": MODEL_NAME,
            "model_profile": MODEL_PROFILE_KEY,
            "model_base_url": MODEL_BASE_URL,
            "model_backend": MODEL_BACKEND,
            "model_server_command": MODEL_SERVER_COMMAND,
            "translation_model": TRANSLATION_MODEL_REFERENCE,
            "translation_model_name": TRANSLATION_MODEL_NAME,
            "translation_model_base_url": TRANSLATION_MODEL_BASE_URL,
            "translation_model_backend": TRANSLATION_MODEL_BACKEND,
            "translation_model_server_command": TRANSLATION_MODEL_SERVER_COMMAND,
            "translation_target_language": TRANSLATION_TARGET_LANGUAGE,
            "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,
            "model_default_sampling": _sampling_to_dict(MODEL_DEFAULT_SAMPLING),
            "model_reasoning_sampling": _sampling_to_dict(MODEL_REASONING_SAMPLING),
            "model_task_sampling": _task_sampling_to_dict(),
            "article_summary_concurrency": ARTICLE_SUMMARY_CONCURRENCY,
            "article_text_token_limit": ARTICLE_TEXT_TOKEN_LIMIT,
            "total_article_summary_cap": TOTAL_ARTICLE_SUMMARY_CAP,
            "per_topic_article_summary_cap": PER_TOPIC_ARTICLE_SUMMARY_CAP,
            "min_articles_per_story": MIN_ARTICLES_PER_STORY,
            "max_stories_per_topic": MAX_STORIES_PER_TOPIC,
            "story_backfill_batch_multiplier": STORY_BACKFILL_BATCH_MULTIPLIER,
            "story_cluster_similarity_threshold": STORY_CLUSTER_SIMILARITY_THRESHOLD,
            "topic_clustering_max_tokens": TOPIC_CLUSTERING_MAX_TOKENS,
            "translation_max_tokens": TRANSLATION_MAX_TOKENS,
            "article_summary_max_tokens": ARTICLE_SUMMARY_MAX_TOKENS,
            "final_synthesis_max_tokens": FINAL_SYNTHESIS_MAX_TOKENS,
            "title_generation_max_tokens": TITLE_GENERATION_MAX_TOKENS,
            "image_generation_enabled": IMAGE_GENERATION_ENABLED,
            "image_generation_fail_on_error": IMAGE_GENERATION_FAIL_ON_ERROR,
            "image_model": IMAGE_MODEL_ID,
            "image_base_model": IMAGE_BASE_MODEL,
            "image_width": IMAGE_WIDTH,
            "image_height": IMAGE_HEIGHT,
            "image_steps": IMAGE_STEPS,
            "image_crop_bottom_ratio": IMAGE_CROP_BOTTOM_RATIO,
        },
    )
    _attach_pending_activity_snapshots(diagnostics)
    return diagnostics


def _write_run_diagnostics(diagnostics: RunDiagnostics) -> None:
    with MODEL_CALL_STATS_LOCK:
        diagnostics.record_model_call_stats(json.loads(json.dumps(MODEL_CALL_STATS)))
    json_path, markdown_path, summary_path = diagnostics.write(CONFIG.run_output_dir, timestamp)
    progress_tracker.step("finalize", "Saved run diagnostics.")
    progress_tracker.detail(f"Run details saved: {json_path}")
    progress_tracker.detail(f"Detailed markdown run details saved: {markdown_path}")
    progress_tracker.detail(f"Human-readable run summary saved: {summary_path}")


def _preflight_openai_model_server(
    *,
    base_url: str,
    model_name: str,
    model_reference: str,
) -> dict[str, Any]:
    models_url = f"{base_url.rstrip('/')}/models"
    result: dict[str, Any] = {
        "base_url": base_url,
        "models_url": models_url,
        "ok": False,
        "served_models": [],
        "model_match": False,
    }
    try:
        response = requests.get(models_url, timeout=5)
        result["status_code"] = response.status_code
        response.raise_for_status()
        payload = response.json()
        served_models = [
            str(item.get("id") or item.get("model") or "")
            for item in payload.get("data", [])
            if isinstance(item, dict)
        ]
        result["served_models"] = [model for model in served_models if model]
        result["ok"] = True
        if not served_models:
            result["model_match"] = True
        else:
            expected = {model_name, model_reference, "default_model"}
            result["model_match"] = any(model in expected for model in served_models)
    except Exception as error:
        result["error"] = str(error)
    return result


def preflight_model_server() -> dict[str, Any]:
    return _preflight_openai_model_server(
        base_url=MODEL_BASE_URL,
        model_name=MODEL_NAME,
        model_reference=MODEL_REFERENCE,
    )


def preflight_translation_model_server() -> dict[str, Any]:
    return _preflight_openai_model_server(
        base_url=TRANSLATION_MODEL_BASE_URL,
        model_name=TRANSLATION_MODEL_NAME,
        model_reference=TRANSLATION_MODEL_REFERENCE,
    )


def _probe_chat_completion(
    *,
    base_url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    completions_url = f"{base_url.rstrip('/')}/chat/completions"
    result: dict[str, Any] = {
        "base_url": base_url,
        "completions_url": completions_url,
        "ok": False,
    }
    payload = {**payload, "stream": False}
    try:
        response = requests.post(completions_url, json=payload, timeout=timeout_seconds)
        result["status_code"] = response.status_code
        response.raise_for_status()
        response_payload = response.json()
        result["content_preview"] = _translation_response_content(response_payload)[:80]
        result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
    return result


def probe_model_generation(timeout_seconds: int = MODEL_LOAD_PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "Reply with exactly: ok"},
            {"role": "user", "content": "Health check."},
        ],
        "max_tokens": 2,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": dict(VISIBLE_CONTENT_CHAT_TEMPLATE_KWARGS),
    }
    return _probe_chat_completion(
        base_url=MODEL_BASE_URL,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


def probe_translation_model_generation(
    timeout_seconds: int = MODEL_LOAD_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload = _translation_payload("Hola.", "es")
    payload["max_tokens"] = 8
    return _probe_chat_completion(
        base_url=TRANSLATION_MODEL_BASE_URL,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


def _managed_model_server_log_path() -> str:
    return os.path.join(RUN_OUTPUT_DIR, "model_server.log")


def _managed_translation_model_server_log_path() -> str:
    return os.path.join(RUN_OUTPUT_DIR, "translation_model_server.log")


def _wait_for_managed_model_server(
    process: subprocess.Popen,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_preflight: dict[str, Any] = {}
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"Managed model server exited before it was ready "
                f"(exit code {exit_code}). See {_managed_model_server_log_path()}."
            )
        last_preflight = preflight_model_server()
        if last_preflight.get("ok") and last_preflight.get("model_match"):
            return last_preflight
        time.sleep(2)

    detail = last_preflight.get("error") or last_preflight.get("served_models") or "no response"
    raise TimeoutError(
        f"Managed model server did not become ready within {timeout_seconds} seconds "
        f"at {MODEL_BASE_URL}: {detail}. See {_managed_model_server_log_path()}."
    )


def _wait_for_managed_translation_model_server(
    process: subprocess.Popen,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_preflight: dict[str, Any] = {}
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"Managed translation model server exited before it was ready "
                f"(exit code {exit_code}). See {_managed_translation_model_server_log_path()}."
            )
        last_preflight = preflight_translation_model_server()
        if last_preflight.get("ok") and last_preflight.get("model_match"):
            return last_preflight
        time.sleep(2)

    detail = last_preflight.get("error") or last_preflight.get("served_models") or "no response"
    raise TimeoutError(
        f"Managed translation model server did not become ready within {timeout_seconds} seconds "
        f"at {TRANSLATION_MODEL_BASE_URL}: {detail}. See {_managed_translation_model_server_log_path()}."
    )


def _stop_managed_server_process(process: subprocess.Popen, *, server_label: str) -> None:
    """Stop a managed server spawned with ``start_new_session=True``."""
    if process.poll() is not None:
        progress_tracker.detail(f"Managed {server_label} already exited with code {process.returncode}.")
        return

    progress_tracker.detail(f"Stopping managed {server_label}.")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        progress_tracker.detail(f"Managed {server_label} did not stop gracefully; killing it.")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()
        process.wait(timeout=10)


def run_pipeline() -> None:
    with run_logging():
        try:
            with managed_model_server():
                _run_pipeline()
        except Exception as error:
            progress_tracker.step("finalize", "Daily news run failed. See the run log for details.")
            progress_tracker.detail(f"Run failed: {type(error).__name__}: {error}")
            raise
        else:
            progress_tracker.finish("done")


@contextmanager
def managed_model_server():
    global MANAGED_MODEL_SERVER_ACTIVE
    global MANAGED_MODEL_SERVER_READY
    global MANAGED_MODEL_SERVER_EXTERNAL
    global MANAGED_MODEL_SERVER_PROCESS
    global MANAGED_MODEL_SERVER_LOG_FILE
    previous_active = MANAGED_MODEL_SERVER_ACTIVE
    if previous_active:
        yield
        return

    MANAGED_MODEL_SERVER_ACTIVE = True
    MANAGED_MODEL_SERVER_READY = False
    MANAGED_MODEL_SERVER_EXTERNAL = False
    MANAGED_MODEL_SERVER_PROCESS = None
    MANAGED_MODEL_SERVER_LOG_FILE = None
    try:
        yield
    finally:
        if MANAGED_MODEL_SERVER_PROCESS is not None:
            _stop_managed_server_process(MANAGED_MODEL_SERVER_PROCESS, server_label="model server")
            record_activity_snapshot("after_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
        elif MANAGED_MODEL_SERVER_EXTERNAL and MANAGED_MODEL_SERVER_READY:
            record_activity_snapshot("after_existing_model_server_run", ACTIVE_RUN_DIAGNOSTICS)
        if ACTIVE_RUN_DIAGNOSTICS is not None and MANAGED_MODEL_SERVER_READY:
            ACTIVE_RUN_DIAGNOSTICS.write(CONFIG.run_output_dir, CONFIG.timestamp)
        if MANAGED_MODEL_SERVER_LOG_FILE is not None:
            MANAGED_MODEL_SERVER_LOG_FILE.close()
        MANAGED_MODEL_SERVER_ACTIVE = False
        MANAGED_MODEL_SERVER_READY = False
        MANAGED_MODEL_SERVER_EXTERNAL = False
        MANAGED_MODEL_SERVER_PROCESS = None
        MANAGED_MODEL_SERVER_LOG_FILE = None


def _ensure_main_model_server_ready() -> None:
    global MANAGED_MODEL_SERVER_READY
    global MANAGED_MODEL_SERVER_EXTERNAL
    global MANAGED_MODEL_SERVER_PROCESS
    global MANAGED_MODEL_SERVER_LOG_FILE
    if MANAGED_MODEL_SERVER_READY:
        return

    ensure_codex_safe_model_reference(MODEL_REFERENCE)
    progress_tracker.step("model", "Checking model server.")
    record_activity_snapshot("before_model_server_preflight")
    existing_preflight = preflight_model_server()
    if ACTIVE_RUN_DIAGNOSTICS is not None:
        ACTIVE_RUN_DIAGNOSTICS.event("model_server_preflight", **existing_preflight)
    if existing_preflight.get("ok") and existing_preflight.get("model_match"):
        generation_probe = probe_model_generation()
        if not generation_probe.get("ok"):
            raise RuntimeError(
                "Model server endpoint answered /models but failed a tiny generation probe. "
                f"{generation_probe.get('error') or generation_probe}. "
                f"See {_managed_model_server_log_path()}."
            )
        progress_tracker.step("model", "Model server ready.")
        progress_tracker.detail(
            "Model server already running for the selected model; "
            "using it without managing its lifecycle."
        )
        progress_tracker.detail("Existing model server passed a tiny generation probe.")
        record_activity_snapshot("existing_model_server_ready", ACTIVE_RUN_DIAGNOSTICS)
        MANAGED_MODEL_SERVER_EXTERNAL = True
        MANAGED_MODEL_SERVER_READY = True
        return

    if existing_preflight.get("ok"):
        raise RuntimeError(
            "Model server endpoint is already in use, but it did not report the expected model. "
            f"Expected {MODEL_REFERENCE} / {MODEL_NAME}; served "
            f"{existing_preflight.get('served_models')}. Stop that server or change NEWS_MODEL_BASE_URL."
        )

    log_path = _managed_model_server_log_path()
    command = shlex.split(MODEL_SERVER_COMMAND)
    record_activity_snapshot("before_model_server_start", ACTIVE_RUN_DIAGNOSTICS)
    progress_tracker.step("model", "Starting managed model server.")
    progress_tracker.detail(f"Managed model server command: {MODEL_SERVER_COMMAND}")
    progress_tracker.detail(f"Managed model server log: {log_path}")
    log_file = open(log_path, "w", encoding="utf-8")
    MANAGED_MODEL_SERVER_LOG_FILE = log_file
    try:
        process = subprocess.Popen(
            command,
            cwd=str(CONFIG.root_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except Exception:
        log_file.close()
        MANAGED_MODEL_SERVER_LOG_FILE = None
        raise
    MANAGED_MODEL_SERVER_PROCESS = process
    try:
        ready_preflight = _wait_for_managed_model_server(process)
        record_activity_snapshot("after_model_server_ready", ACTIVE_RUN_DIAGNOSTICS)
        progress_tracker.detail(
            "Managed model server answered /models. "
            f"Served models: {ready_preflight.get('served_models') or ['n/a']}"
        )
        progress_tracker.step("model", "Checking model generation.")
        generation_probe = probe_model_generation()
        if not generation_probe.get("ok"):
            raise RuntimeError(
                "Managed model server answered /models but failed a tiny generation probe. "
                f"{generation_probe.get('error') or generation_probe}. "
                f"See {_managed_model_server_log_path()}."
            )
        progress_tracker.step("model", "Model server ready.")
        progress_tracker.detail("Managed model server passed a tiny generation probe.")
        MANAGED_MODEL_SERVER_READY = True
    except Exception:
        _stop_managed_server_process(process, server_label="model server")
        record_activity_snapshot("after_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
        log_file.close()
        MANAGED_MODEL_SERVER_PROCESS = None
        MANAGED_MODEL_SERVER_LOG_FILE = None
        raise


@contextmanager
def managed_translation_model_server():
    ensure_codex_safe_model_reference(TRANSLATION_MODEL_REFERENCE)
    progress_tracker.step("translation", "Checking translation model server.")
    record_activity_snapshot("before_translation_model_server_preflight", ACTIVE_RUN_DIAGNOSTICS)
    existing_preflight = preflight_translation_model_server()
    if existing_preflight.get("ok") and existing_preflight.get("model_match"):
        generation_probe = probe_translation_model_generation()
        if not generation_probe.get("ok"):
            raise RuntimeError(
                "Translation model server endpoint answered /models but failed a tiny generation probe. "
                f"{generation_probe.get('error') or generation_probe}. "
                f"See {_managed_translation_model_server_log_path()}."
            )
        progress_tracker.step("translation", "Translation model server ready.")
        progress_tracker.detail(
            "Translation model server already running for the selected translation model; "
            "using it without managing its lifecycle."
        )
        record_activity_snapshot("existing_translation_model_server_ready", ACTIVE_RUN_DIAGNOSTICS)
        try:
            yield
        finally:
            record_activity_snapshot("after_existing_translation_model_server_run", ACTIVE_RUN_DIAGNOSTICS)
        return

    if existing_preflight.get("ok"):
        raise RuntimeError(
            "Translation model endpoint is already in use, but it did not report the expected model. "
            f"Expected {TRANSLATION_MODEL_REFERENCE} / {TRANSLATION_MODEL_NAME}; served "
            f"{existing_preflight.get('served_models')}. Stop that server or change "
            "NEWS_TRANSLATION_MODEL_BASE_URL."
        )

    log_path = _managed_translation_model_server_log_path()
    command = shlex.split(TRANSLATION_MODEL_SERVER_COMMAND)
    record_activity_snapshot("before_translation_model_server_start", ACTIVE_RUN_DIAGNOSTICS)
    progress_tracker.step("translation", "Starting translation model server.")
    progress_tracker.detail(f"Managed translation model server command: {TRANSLATION_MODEL_SERVER_COMMAND}")
    progress_tracker.detail(f"Managed translation model server log: {log_path}")
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(CONFIG.root_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except Exception:
        log_file.close()
        raise
    try:
        ready_preflight = _wait_for_managed_translation_model_server(process)
        record_activity_snapshot("after_translation_model_server_ready", ACTIVE_RUN_DIAGNOSTICS)
        progress_tracker.detail(
            "Managed translation model server answered /models. "
            f"Served models: {ready_preflight.get('served_models') or ['n/a']}"
        )
        progress_tracker.step("translation", "Checking translation model generation.")
        generation_probe = probe_translation_model_generation()
        if not generation_probe.get("ok"):
            raise RuntimeError(
                "Managed translation model server answered /models but failed a tiny generation probe. "
                f"{generation_probe.get('error') or generation_probe}. "
                f"See {_managed_translation_model_server_log_path()}."
            )
        progress_tracker.step("translation", "Translation model server ready.")
        progress_tracker.detail("Managed translation model server passed a tiny generation probe.")
        yield
    finally:
        _stop_managed_server_process(process, server_label="translation model server")
        record_activity_snapshot("after_translation_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
        log_file.close()


def run_translation_model_smoke_test() -> int:
    print("Translation model smoke test")
    print(f"Reference: {TRANSLATION_MODEL_REFERENCE}")
    print(f"Resolved model: {TRANSLATION_MODEL_NAME}")
    print(f"Backend: {TRANSLATION_MODEL_BACKEND}")
    try:
        translated = _generate_translation_text("Hola.", "es", max_tokens=16)
    except Exception as error:
        print(f"FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        _unload_translation_model_resources()

    if translated:
        print("OK: translated a tiny Spanish probe with TranslateGemma.")
        print(f"Preview: {translated}")
        return 0

    print("FAILED: translation returned an empty response.", file=sys.stderr)
    return 1


def _run_pipeline() -> None:
    global ACTIVE_RUN_DIAGNOSTICS
    all_sources = list(SOURCE_FEEDS.keys())
    sources = all_sources
    effective_num_topics = NUM_TOP_TOPICS
    effective_total_article_summary_cap = TOTAL_ARTICLE_SUMMARY_CAP
    effective_per_topic_article_summary_cap = PER_TOPIC_ARTICLE_SUMMARY_CAP

    diagnostics = _new_run_diagnostics(len(sources))
    ACTIVE_RUN_DIAGNOSTICS = diagnostics
    progress_tracker.step("setup", "Starting daily news run.")
    progress_tracker.detail(
        f"Model profile: {MODEL_PROFILE_KEY} | backend: {MODEL_BACKEND} | "
        f"model: {MODEL_REFERENCE} -> {MODEL_NAME}"
    )
    progress_tracker.detail(
        (
            f"Translation model: {TRANSLATION_MODEL_REFERENCE} -> {TRANSLATION_MODEL_NAME} "
            f"({TRANSLATION_MODEL_BACKEND}, target={TRANSLATION_TARGET_LANGUAGE})."
            if TRANSLATION_ENABLED
            else "Translation disabled; source selection is English-only."
        )
    )
    progress_tracker.detail(
        f"Model caps: input {MODEL_MAX_INPUT_TOKENS} tokens, "
        f"article text {ARTICLE_TEXT_TOKEN_LIMIT} tokens, "
        f"summaries {TOTAL_ARTICLE_SUMMARY_CAP} total/{PER_TOPIC_ARTICLE_SUMMARY_CAP} per topic, "
        f"{MAX_STORIES_PER_TOPIC} stories/topic, "
        f"{PER_SOURCE_TOPIC_ARTICLE_CAP} per source/story budget cap, "
        f"summary concurrency {ARTICLE_SUMMARY_CONCURRENCY}."
    )
    progress_tracker.detail(
        f"Source scrape guardrails: article download timeout {ARTICLE_DOWNLOAD_TIMEOUT_SECONDS}s, "
        f"article scrape deadline {ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS}s, "
        f"slow source warning {SLOW_SOURCE_WARNING_SECONDS}s."
    )
    progress_tracker.detail(f"Run mode: {RUN_MODE}")
    progress_tracker.detail(
        f"Source pool: {len(sources)} active feed(s) after tier/topic filters."
    )
    active_source_tiers = Counter(
        str(source.get("tier") or "unknown") for source in SOURCE_FEEDS.values()
    )
    if active_source_tiers:
        tier_summary = ", ".join(
            f"{tier}: {count}" for tier, count in sorted(active_source_tiers.items())
        )
        progress_tracker.detail(f"Active source tiers: {tier_summary}.")
    progress_tracker.detail(f"Active topic IDs: {', '.join(ACTIVE_TOPIC_IDS)}")
    topic_scoped_source_count = sum(
        1 for source in SOURCE_FEEDS.values() if source.get("allowed_topic_ids")
    )
    if topic_scoped_source_count:
        progress_tracker.detail(
            f"Topic-scoped sources active: {topic_scoped_source_count} "
            "source(s) with allowed_topic_ids."
        )
    if DEV:
        progress_tracker.detail(
            f"DEV mode active. Using core English sources allowed for active topics "
            f"({len(sources)} source(s)). "
            f"Sending to one recipient only "
            f"(always {BRADLEY_ONLY_RECIPIENT}) without recording URLs into the shared history."
        )
    elif LOCAL_PROD:
        mode_label = "LOOSE-LOCAL-PROD" if LOOSE_LOCAL_PROD else "LOCAL-PROD"
        history_label = (
            "shared production URL history"
            if SHARED_URL_HISTORY_ENABLED
            else "isolated URL history"
        )
        progress_tracker.detail(
            f"{mode_label} mode active. Using core and peripheral English sources "
            f"allowed for active topics with {history_label}, but sending only to "
            f"{BRADLEY_ONLY_RECIPIENT}."
        )
        if LOOSE_LOCAL_PROD:
            progress_tracker.detail(
                "Loose local-prod matching active: dev topic/story thresholds "
                f"with a {MIN_ARTICLES_PER_STORY}-article story floor."
            )
    progress_tracker.detail(f"Run output folder: {RUN_OUTPUT_DIR}")
    progress_tracker.detail(f"Run used URL log: {RUN_USED_URLS_PATH}")
    progress_tracker.detail(f"Run log: {RUN_LOG_PATH}")
    if SHARED_URL_HISTORY_ENABLED:
        progress_tracker.detail(f"Global seen URL log: {LEGACY_SEEN_URLS_PATH}")
    else:
        progress_tracker.detail("Shared URL history: disabled for this run.")

    progress_tracker.detail(f"Topic mode: {TOPIC_MODE}")
    topics, top_stories = select_topics_for_run(
        effective_num_topics,
        diagnostics=diagnostics,
    )
    if not topics:
        progress_tracker.step("finalize", "No topics available; stopping run.")
        if not any(event.get("label") == "aborted" for event in diagnostics.events):
            diagnostics.event("aborted", reason="no_topics")
        _write_run_diagnostics(diagnostics)
        return
    _persist_topics_debug(topics, top_stories)

    progress_tracker.reset(total_sources=len(sources))

    seen_urls = _load_seen_urls()
    run_seen_urls: set[str] = set()
    article_candidates: List[dict] = []
    candidate_urls: List[str] = []
    final_reports: List[str] = []
    matched_feed_item_count = 0
    fresh_article_count = 0
    source_rejection_counts: Counter[str] = Counter()

    # 3) Iterate union of sources, collect recent articles without topic labels.
    for source_index, source_name in enumerate(sources, start=1):
        progress_tracker.start_source(source_index, source_name)
        source_started_at = datetime.now(timezone.utc)
        source_started_perf = time.perf_counter()
        try:
            article_targets, new_urls, source_run = gather_article_candidates_for_source(
                source_name,
                seen_urls,
                run_seen_urls,
            )
        except Exception as error:
            article_targets = []
            new_urls = []
            source_run = {
                "source": source_name,
                "status": "source_error",
                "reason": f"{type(error).__name__}: {error}",
                "feed_item_count": 0,
                "recent_item_count": 0,
                "selected_item_count": 0,
                "selected_items": [],
                "selected_by_topic": {},
                "post_scrape_rejections": [],
                "feed_rejections": [],
                "scrape_attempts": [],
                "scrape_status_counts": {},
                "fresh_article_count": 0,
                "fresh_articles": [],
                "rejected_counts": {},
            }
            progress_tracker.warning(f"Source failed: {source_name}: {type(error).__name__}: {error}")
        source_elapsed_seconds = round(time.perf_counter() - source_started_perf, 3)
        source_run["source_index"] = source_index
        source_run["started_at"] = source_started_at.isoformat(timespec="seconds")
        source_run["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_run["elapsed_seconds"] = source_elapsed_seconds
        scrape_status_counts = source_run.get("scrape_status_counts") or {}
        timeout_count = sum(
            int(count or 0)
            for status, count in scrape_status_counts.items()
            if "timeout" in str(status)
        )
        if timeout_count:
            source_run["timeout_count"] = timeout_count
            diagnostics.event(
                "source_scrape_timeout",
                source=source_name,
                source_index=source_index,
                timeout_count=timeout_count,
                elapsed_seconds=source_elapsed_seconds,
            )
            progress_tracker.warning(
                f"Source had {timeout_count} timed-out scrape(s): {source_name}"
            )
        if source_elapsed_seconds >= SLOW_SOURCE_WARNING_SECONDS:
            source_run["slow_source"] = True
            diagnostics.event(
                "slow_source",
                source=source_name,
                source_index=source_index,
                elapsed_seconds=source_elapsed_seconds,
            )
            progress_tracker.warning(
                f"Slow source: {source_name} took {source_elapsed_seconds:.1f}s"
            )
        diagnostics.record_source_run(source_run)
        matched_feed_item_count += int(source_run.get("selected_item_count") or 0)
        fresh_article_count += int(source_run.get("fresh_article_count") or 0)
        source_rejection_counts.update(source_run.get("rejected_counts") or {})
        progress_tracker.set_source_article_total(0)
        if new_urls:
            candidate_urls.extend(new_urls)
        if article_targets:
            article_candidates.extend(article_targets)
        progress_tracker.source_completed()

    if matched_feed_item_count or any(source_rejection_counts.values()):
        progress_tracker.detail(
            f"Source funnel: {matched_feed_item_count} recent scraped article candidate(s), "
            f"{fresh_article_count} fresh article target(s) after dedupe/history "
            f"(history={source_rejection_counts.get('seen_in_history', 0)}, "
            f"duplicate_this_run={source_rejection_counts.get('duplicate_this_run', 0)}, "
            f"missing_url={source_rejection_counts.get('missing_url', 0)}, "
            f"wrong_feed_source={source_rejection_counts.get('wrong_feed_source', 0)}, "
            "wrong_feed_source_unattributed="
            f"{source_rejection_counts.get('wrong_feed_source_unattributed', 0)})."
        )
    diagnostics.event(
        "article_collection",
        candidate_count=len(article_candidates),
        candidate_url_count=len(candidate_urls),
        matched_feed_item_count=matched_feed_item_count,
        fresh_article_count=fresh_article_count,
        rejected_counts=dict(source_rejection_counts),
    )
    candidate_url_artifact = _persist_url_list_debug(candidate_urls, "candidate_urls")
    if candidate_url_artifact:
        candidate_url_path, candidate_url_count = candidate_url_artifact
        diagnostics.record_artifact(
            "candidate_urls",
            candidate_url_path,
            count=candidate_url_count,
            run_used_urls_path=RUN_USED_URLS_PATH,
        )
    _record_run_urls(candidate_urls)
    if not article_candidates:
        progress_tracker.step("finalize", "No recent article candidates available; stopping run.")
        diagnostics.event("aborted", reason="no_article_candidates")
        _write_run_diagnostics(diagnostics)
        return

    diagnostics.event(
        "translation",
        candidate_count=len(article_candidates),
        translated_count=0,
        skipped=True,
        reason=(
            "translation_disabled"
            if not TRANSLATION_ENABLED
            else "pre_classification_translation_pass_disabled"
        ),
    )
    progress_tracker.detail("Translation pass skipped before topic classification.")

    # Classify all collected articles into topics via embedding cosine similarity.
    # This replaces the earlier keyword-based topic_key assignment with a semantic one,
    # then deduplicates articles that converge to the same (url, topic_key) pair.
    progress_tracker.step("stories", "Classifying articles by topic (semantic embeddings).")
    topic_titles_map = {
        str(t.get("key") or ""): str(t.get("title") or "")
        for t in topics
    }
    progress_tracker.detail(
        f"Embedding model: {embeddings_stage.EMBEDDING_MODEL_NAME} | "
        f"threshold: {TOPIC_EMBEDDING_SIMILARITY_THRESHOLD:.2f}"
    )
    topic_counts_by_key: Counter[str] = Counter()
    embedding_classification_error = ""
    try:
        topic_classified_candidates = embeddings_stage.classify_articles_by_topic(
            article_candidates,
            topics,
            threshold=TOPIC_EMBEDDING_SIMILARITY_THRESHOLD,
        )
    except Exception as _emb_error:
        logger.exception("Embedding classification failed; falling back to keyword-assigned topic keys.")
        progress_tracker.warning(
            f"Embedding classification failed ({_emb_error}); "
            "falling back to keyword-assigned topic keys."
        )
        embedding_classification_error = str(_emb_error)
        topic_classified_candidates = article_candidates

    topic_classified_candidates, source_topic_scope_drops = _filter_articles_by_source_topic_scope(
        topic_classified_candidates
    )
    topic_counts_by_key = Counter(
        str(a.get("topic_key") or "unassigned") for a in topic_classified_candidates
    )
    for topic in topics:
        tkey = str(topic.get("key") or "")
        progress_tracker.detail(
            f"  Topic '{topic_titles_map.get(tkey, tkey)}': "
            f"{topic_counts_by_key.get(tkey, 0)} articles after embedding classification."
        )
    if source_topic_scope_drops:
        progress_tracker.detail(
            "Topic-scoped source filter: dropped "
            f"{len(source_topic_scope_drops)} article-topic match(es) outside allowed_topic_ids."
        )
    embedding_event = {
        "threshold": TOPIC_EMBEDDING_SIMILARITY_THRESHOLD,
        "candidate_count": len(article_candidates),
        "classified_count": len(topic_classified_candidates),
        "counts_by_topic": {
            topic_titles_map.get(k, k): v
            for k, v in topic_counts_by_key.items()
        },
        "source_topic_scope_dropped_count": len(source_topic_scope_drops),
        "source_topic_scope_dropped_by_source": dict(
            Counter(str(item.get("source") or "") for item in source_topic_scope_drops)
        ),
        "source_topic_scope_dropped_by_topic": dict(
            Counter(str(item.get("topic_key") or "unassigned") for item in source_topic_scope_drops)
        ),
    }
    if embedding_classification_error:
        embedding_event["error"] = embedding_classification_error
        embedding_event["fallback"] = "keyword_assigned_topic_keys"
    diagnostics.event("embedding_topic_classification", **embedding_event)

    # Cluster articles into stories *within* each topic using TF-IDF similarity.
    # This produces tighter, more on-topic story clusters than the previous global approach.
    progress_tracker.step("stories", "Clustering stories within topics.")
    clustered_article_targets, story_cluster_stats = (
        story_clustering_stage.organize_article_targets_into_stories(
            topic_classified_candidates,
            topics,
            min_articles_per_story=MIN_ARTICLES_PER_STORY,
            max_stories_per_topic=MAX_STORIES_PER_TOPIC,
            similarity_threshold=STORY_CLUSTER_SIMILARITY_THRESHOLD,
        )
    )
    story_records = _flatten_topic_story_records(story_cluster_stats)
    diagnostics.event("story_clustering", **{
        k: v for k, v in story_cluster_stats.items()
        if k not in (
            "stories_by_topic",
            "reserve_stories_by_topic",
            "dropped_articles_by_topic",
            "pair_debug_by_topic",
        )
    })
    stories_by_topic_debug = story_cluster_stats.get("stories_by_topic") or {}
    for topic in topics:
        tkey = str(topic.get("key") or "")
        ttitle = str(topic.get("title") or "")
        topic_stories = stories_by_topic_debug.get(ttitle, [])
        dropped_count = (story_cluster_stats.get("dropped_by_topic") or {}).get(ttitle, 0)
        progress_tracker.detail(
            f"  {ttitle}: "
            f"{topic_counts_by_key.get(tkey, 0)} articles "
            f"→ {len(topic_stories)} viable story cluster(s) "
            f"(+{dropped_count} true noise/below-floor dropped)"
        )
    progress_tracker.detail(
        f"Story clustering: {story_cluster_stats.get('included_count', 0)} "
        f"article target(s) retained across {story_cluster_stats.get('viable_story_count', 0)} "
        f"viable story group(s); {story_cluster_stats.get('dropped_count', 0)} dropped below "
        f"the {MIN_ARTICLES_PER_STORY}-article story floor "
        f"(TF-IDF threshold {STORY_CLUSTER_SIMILARITY_THRESHOLD:.2f}, per-topic)."
    )
    diagnostics.record_article_budget(
        {
            "enabled": False,
            "reason": "story-first pipeline summarizes every viable story cluster before final topic selection",
            "candidate_count": len(clustered_article_targets),
            "included_count": len(clustered_article_targets),
            "dropped_count": 0,
            "total_cap": effective_total_article_summary_cap,
            "per_topic_cap": effective_per_topic_article_summary_cap,
            "per_source_topic_cap": PER_SOURCE_TOPIC_ARTICLE_CAP,
        }
    )
    if not clustered_article_targets:
        progress_tracker.step("finalize", "No multi-article story clusters available; stopping run.")
        diagnostics.event("aborted", reason="no_supported_story_clusters")
        _write_run_diagnostics(diagnostics)
        return

    progress_tracker.start_article_summary(len(clustered_article_targets))
    article_summary_reports: List[str] = []
    article_summary_reports.extend(
        article_summarization_stage.run_article_summary_pass(
            clustered_article_targets,
            topics,
            _article_summarization_runtime(),
        )
    )

    progress_tracker.set_final_step("reports", 1)
    diagnostics.article_summary_count = len(article_summary_reports)
    article_summaries_path = _persist_article_summaries_debug(article_summary_reports)
    if article_summaries_path:
        diagnostics.record_artifact(
            "final_article_summaries",
            article_summaries_path,
            count=len(article_summary_reports),
        )
    record_activity_snapshot("after_article_summaries", diagnostics)
    progress_tracker.detail(f"Saved {len(article_summary_reports)} article summary record(s).")

    progress_tracker.step("stories", "Drafting clustered stories from article summaries.")
    story_drafts, story_draft_stats = story_drafting_stage.draft_story_clusters_from_article_summaries(
        story_records,
        article_summary_reports,
        _story_drafting_runtime(),
        article_targets=clustered_article_targets,
    )
    diagnostics.event("story_drafting", **story_draft_stats)
    progress_tracker.detail(
        f"Story drafting: {story_draft_stats.get('story_drafts_generated', 0)} "
        f"drafted story paragraph(s) from {story_draft_stats.get('story_blocks_requested', 0)} "
        "eligible cluster(s)."
    )

    # Dedup near-duplicate story drafts within each topic using embedding cosine similarity.
    if story_drafts:
        story_drafts, story_dedup_stats = _dedupe_story_drafts_for_topic_selection(story_drafts)
        if story_dedup_stats.get("dropped"):
            progress_tracker.detail(
                f"Story dedup: removed {story_dedup_stats.get('dropped')} near-duplicate story draft(s) "
                f"(cosine threshold {STORY_EMBEDDING_DEDUP_THRESHOLD:.2f})."
            )
        diagnostics.event("story_dedup", **story_dedup_stats)
        if story_dedup_stats.get("error"):
            progress_tracker.warning(
                f"Story dedup failed ({story_dedup_stats.get('error')}); "
                f"keeping all {story_dedup_stats.get('before', len(story_drafts))} drafts."
            )

        selected_story_topic_matches, story_topic_stats = (
            story_topic_assignment_stage.classify_story_drafts_for_topics(
                story_drafts,
                topics,
                _story_topic_runtime(),
            )
        )
        diagnostics.event("story_topic_classification", **story_topic_stats)
    else:
        story_dedup_stats = {
            "before": 0,
            "after": 0,
            "dropped": 0,
            "threshold": STORY_EMBEDDING_DEDUP_THRESHOLD,
            "skipped": True,
            "reason": "no_initial_story_drafts",
        }
        diagnostics.event("story_dedup", **story_dedup_stats)
        selected_story_topic_matches = []
        story_topic_stats = {
            "enabled": True,
            "story_count": 0,
            "selected_story_topic_count": 0,
            "max_stories_per_topic": MAX_STORIES_PER_TOPIC,
            "min_score": None,
            "keyword_fit_gate_enabled": False,
            "topic_story_diversity_min_distance": TOPIC_STORY_DIVERSITY_MIN_DISTANCE,
            "selected_by_topic": {},
            "story_topic_screening": {
                "enabled": bool(STORY_TOPIC_VALIDATION_ENABLED),
                "us_focus_topic_ids": ["us_economy", "us_politics"],
                "topic_ids": ["us_economy", "us_politics"],
                "candidate_count": 0,
                "judged_count": 0,
                "preferred_count": 0,
                "obvious_exclusion_count": 0,
                "fallback_kept_count": 0,
                "parse_failed_count": 0,
                "topicality_counts": {},
                "scale_counts": {},
                "topics": {},
            },
            "story_topic_validation": {
                "enabled": bool(STORY_TOPIC_VALIDATION_ENABLED),
                "us_focus_topic_ids": ["us_economy", "us_politics"],
                "topic_ids": ["us_economy", "us_politics"],
                "candidate_count": 0,
                "judged_count": 0,
                "kept_count": 0,
                "dropped_count": 0,
                "fallback_kept_count": 0,
                "parse_failed_count": 0,
                "verdict_counts": {},
                "topics": {},
            },
            "article_overlap_dedup": {
                "enabled": True,
                "threshold": story_topic_assignment_stage.STORY_TOPIC_OVERLAP_SUPPRESS_THRESHOLD,
                "conflicts_resolved": 0,
                "banned_story_count": 0,
                "events": [],
            },
            "topics": {},
        }
        diagnostics.event("story_topic_classification", **story_topic_stats)
        progress_tracker.detail(
            "No story drafts generated from viable clusters."
        )
    selected_by_topic = story_topic_stats.get("selected_by_topic") or {}
    progress_tracker.detail(
        "Story-topic assignment: "
        + ", ".join(
            f"{topic_title}={count}"
            for topic_title, count in selected_by_topic.items()
        )
        + " (LLM screening + article-overlap ownership)."
    )
    topic_screening_stats = story_topic_stats.get("story_topic_screening") or {}
    if topic_screening_stats.get("enabled") and topic_screening_stats.get("candidate_count"):
        progress_tracker.detail(
            "Story-topic screening: "
            f"judged {topic_screening_stats.get('judged_count', 0)} story candidate(s), "
            f"{topic_screening_stats.get('preferred_count', 0)} not obviously bad, "
            f"{topic_screening_stats.get('obvious_exclusion_count', 0)} obvious topicality/scale exclusion(s)."
        )
    overlap_stats = story_topic_stats.get("article_overlap_dedup") or {}
    if overlap_stats.get("conflicts_resolved"):
        progress_tracker.detail(
            "Story overlap ownership: "
            f"resolved {overlap_stats.get('conflicts_resolved')} >=50% article-overlap conflict(s)."
        )

    story_backfill_stats = {
        "enabled": False,
        "reason": "all_viable_story_clusters_drafted_up_front",
        "iterations": 0,
        "initial_selected_by_topic": selected_by_topic,
        "final_selected_by_topic": selected_by_topic,
        "deficits_before": {},
        "deficits_after": {},
        "attempted_story_count_by_topic": {},
        "attempted_article_count": 0,
        "new_article_summary_count": 0,
        "new_story_draft_count": 0,
        "exhausted_topics": [],
        "reserve_story_count": 0,
        "batch_multiplier": max(1, STORY_BACKFILL_BATCH_MULTIPLIER),
    }
    diagnostics.event("story_backfill", **story_backfill_stats)
    diagnostics.article_summary_count = len(article_summary_reports)

    story_coverage_deficits = {
        topic_title: MAX_STORIES_PER_TOPIC - int(count or 0)
        for topic_title, count in selected_by_topic.items()
        if int(count or 0) < MAX_STORIES_PER_TOPIC
    }
    for topic in topics:
        topic_title = str(topic.get("title") or topic.get("key") or "Unknown topic")
        if topic_title not in selected_by_topic:
            story_coverage_deficits[topic_title] = MAX_STORIES_PER_TOPIC
    if story_coverage_deficits:
        diagnostics.event("story_coverage_deficit", deficits=story_coverage_deficits)
        progress_tracker.detail(
            "Story coverage deficit: "
            + ", ".join(
                f"{topic_title} short {deficit}"
                for topic_title, deficit in story_coverage_deficits.items()
            )
        )

    final_reports, topic_assignment_stats = (
        story_topic_assignment_stage.build_topic_assigned_article_reports(
            selected_story_topic_matches,
            article_summary_reports,
            clustered_article_targets,
            topics,
            _story_topic_runtime(),
        )
    )
    diagnostics.event("story_topic_report_assignment", **topic_assignment_stats)
    selected_article_ids = {
        story_drafting_stage.report_article_id(entry)
        for entry in final_reports
        if story_drafting_stage.report_article_id(entry)
    }
    selected_urls = [
        str(article.get("url") or "").strip()
        for article in clustered_article_targets
        if str(article.get("article_id") or "") in selected_article_ids and article.get("url")
    ]
    selected_url_artifact = _persist_url_list_debug(selected_urls, "selected_article_urls")
    if selected_url_artifact:
        selected_url_path, selected_url_count = selected_url_artifact
        diagnostics.record_artifact(
            "selected_article_urls",
            selected_url_path,
            count=selected_url_count,
        )
    progress_tracker.detail(
        f"Topic assignment: {len(final_reports)} topic/story article summary record(s) "
        f"from {topic_assignment_stats.get('selected_unique_article_count', 0)} unique article(s)."
    )
    if not final_reports:
        progress_tracker.step("finalize", "No stories met topic-fit thresholds; stopping run.")
        diagnostics.event("aborted", reason="no_story_topic_matches")
        _write_run_diagnostics(diagnostics)
        return
    topic_assigned_summaries_path = _persist_article_summaries_debug(
        final_reports,
        label="topic_assigned_article_summaries",
    )
    if topic_assigned_summaries_path:
        diagnostics.record_artifact(
            "topic_assigned_article_summaries",
            topic_assigned_summaries_path,
            count=len(final_reports),
        )

    recipient_config = get_active_recipient_config(load_recipient_config())
    recipient_list = list(recipient_config.keys())
    recipient_names = [
        recipient_config[email].get("name") or email
        for email in recipient_list
    ]

    if not recipient_list:
        progress_tracker.step("finalize", "No recipients configured; stopping after summaries.")
        diagnostics.event("completed_without_recipients")
        _write_run_diagnostics(diagnostics)
        return

    prompt_label = "default prompt"
    progress_tracker.step(
        "report",
        "Building report.",
        log_detail=f"Building {prompt_label} report for: {', '.join(recipient_list)}",
    )

    report_path = os.path.join(
        RUN_OUTPUT_DIR,
        f"news_report_{timestamp}_{MODEL_PROFILE_KEY}_default_prompt.txt",
    )
    progress_tracker.set_final_step("synthesis", 2)
    final_synthesis, token_stats, synthesis_debug = (
        story_topic_assignment_stage.build_precomputed_story_synthesis(
            selected_story_topic_matches,
            topics,
            final_reports,
            _story_topic_runtime(),
        )
    )
    synthesis_dataset_artifacts = _persist_grouped_synthesis_dataset_debug(report_path, token_stats)
    artifact_prefix = _slugify_report_suffix(os.path.splitext(os.path.basename(report_path))[0])
    for artifact_name, artifact_path in synthesis_dataset_artifacts.items():
        diagnostics.record_artifact(
            f"{artifact_prefix}_{artifact_name}",
            artifact_path,
            recipients=recipient_list,
        )
    synthesis_body = clean_synthesis_for_publication(
        final_synthesis,
        relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
    )
    citation_sources = list((token_stats or {}).get("citation_sources") or [])
    if not synthesis_body:
        last_attempt = (synthesis_debug.get("attempts") or [{}])[-1]
        skip_reason = (
            last_attempt.get("reason")
            if not final_synthesis
            else "publication cleaner removed all synthesis sections"
        )
        progress_tracker.detail(
            f"No synthesis generated for {', '.join(recipient_list)} "
            f"({skip_reason}). Skipping report."
        )
        diagnostics.event(
            "final_synthesis_skipped",
            recipients=recipient_list,
            reason=skip_reason,
            token_stats=token_stats,
            attempts=synthesis_debug.get("attempts") or [],
            relaxed_guards=RELAXED_FINAL_SYNTHESIS_GUARDS,
        )
    else:
        if synthesis_debug.get("dev_fallback_used"):
            diagnostics.event(
                "dev_final_synthesis_fallback_used",
                recipients=recipient_list,
                attempts=synthesis_debug.get("attempts") or [],
            )

        synthesis_body_without_citations = citations_stage.strip_citation_markers(synthesis_body)
        report_title = "Daily News Summary"

        progress_tracker.set_final_step("art", 3)
        record_activity_snapshot("before_image_generation", diagnostics)
        image_art = generate_report_image_art(
            report_path=report_path,
            synthesis_body=synthesis_body_without_citations,
            report_title=report_title,
        )
        record_activity_snapshot("after_image_generation", diagnostics)

        progress_tracker.set_final_step("render", 4)
        reference_reports = filter_reports_for_references(final_reports, token_stats)
        report_body = build_report_body(
            report_title,
            synthesis_body,
            reference_reports,
            topics,
            image_art,
            citation_sources,
        )
        write_report_asset(report_path, report_body)
        image_art_diagnostics = None
        if image_art:
            image_art_diagnostics = {
                key: value
                for key, value in image_art.items()
                if key not in {"data_uri", "image_prompt"}
            }
        diagnostics.record_report(
            path=report_path,
            prompt_label=prompt_label,
            recipient_count=len(recipient_list),
            recipients=recipient_list,
            token_stats=token_stats,
            reference_report_count=len(reference_reports),
            citation_source_count=len(citation_sources),
            synthesis_dataset_artifacts=synthesis_dataset_artifacts,
            image_art=image_art_diagnostics,
        )

        progress_tracker.set_final_step("email", 5)
        maybe_email_report(
            report_title,
            report_body,
            synthesis_body,
            reference_reports,
            topics,
            recipient_list,
            recipient_names,
            image_art,
            citation_sources,
        )

        if token_stats:
            progress_tracker.detail(f"Final synthesis token stats for {', '.join(recipient_list)}: {token_stats}")
        progress_tracker.detail(f"Finished report. Saved text report: {report_path}")

    diagnostics.event("completed")
    _write_run_diagnostics(diagnostics)
    sync_assistant_context_latest_output(CONFIG)
