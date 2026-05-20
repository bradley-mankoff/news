"""Daily news pipeline implementation.

Common usage:
    uv run news dev
    uv run news local-prod
    uv run news prod

Development vs. real sends:
    uv run news dev
        Default. Sends only to NEWS_DEV_RECIPIENT, writes dev_used_urls.txt,
        and does not add URLs to the long-lived seen_urls.txt history.

    uv run news local-prod
        Production-width run with isolated URL history, but delivery is
        limited to NEWS_DEV_RECIPIENT for review and manual forwarding.

    uv run news prod
        Production run. Uses the configured recipient list, writes used_urls.txt,
        and records seen URLs globally so future runs avoid them.

Model selection:
    NEWS_MODEL=gemma-26b-moe uv run news dev
    NEWS_MODEL=qwen-9b-dense uv run news dev
    NEWS_MODEL=gemma-e2b-tiny uv run news dev

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
import json
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Annotated, Any, TextIO, TypedDict, List
import requests
from bs4 import BeautifulSoup
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, BaseMessage, AIMessage, RemoveMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from ddgs import DDGS
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
)
from .diagnostics import RunDiagnostics

try:
    import tiktoken
except ImportError:
    tiktoken = None


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
CONFIG = load_runtime_config()
MODEL_NAME = CONFIG.model_name
MODEL_REFERENCE = CONFIG.model_reference
MODEL_PROFILE = CONFIG.model_profile
MODEL_PROFILE_KEY = MODEL_PROFILE.key
MODEL_BASE_URL = CONFIG.model_base_url
MODEL_BACKEND = CONFIG.model_backend
MODEL_SERVER_COMMAND = CONFIG.model_server_command
BRADLEY_ONLY_RECIPIENT = CONFIG.bradley_only_recipient
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

LAST_TOP_FUNNEL_PROVIDER_STORIES: dict[str, list[dict]] = {}
LAST_TOP_FUNNEL_PROVIDER_METADATA: dict[str, dict] = {}
TOPIC_FRAME_TARGETS = {"western": 0.75, "us": 0.50, "non_western": 0.25}
TOPIC_FRAME_NUDGE_STRENGTH = 0.75
EXCLUDED_NEWS_SOURCE_LABELS = {"abcnews", "abcnewsgo"}
EXCLUDED_NEWS_SOURCE_DOMAINS = {"abcnews.go.com", "abcnews.com"}

RECENT_WINDOW_HOURS = CONFIG.recent_window_hours
MAX_ARTICLES_PER_SOURCE = CONFIG.max_articles_per_source
NUM_TOP_TOPICS = CONFIG.num_top_topics
TOP_TOPIC_PROBES = CONFIG.top_topic_probes
TOP_OF_FUNNEL_PER_PROVIDER = CONFIG.top_of_funnel_per_provider
PROJECT_SUMMARY_SCOPE_LABEL = CONFIG.summary_scope_label
DEV_SOURCE_LIMIT = max(1, CONFIG.dev_source_limit)
DEV_NUM_TOPICS = max(1, CONFIG.dev_num_topics)

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
BRADLEY_ONLY_DELIVERY = CONFIG.bradley_only_delivery
SHARED_URL_HISTORY_ENABLED = CONFIG.shared_url_history_enabled
RELAXED_FINAL_SYNTHESIS_GUARDS = CONFIG.relaxed_final_synthesis_guards
TOPIC_MODE = CONFIG.topic_mode
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
try:
    MIN_ARTICLES_PER_STORY = max(2, int(os.getenv("NEWS_MIN_ARTICLES_PER_STORY", "2")))
except ValueError:
    MIN_ARTICLES_PER_STORY = 2
try:
    TOPIC_RELEVANCE_MIN_SCORE = max(1, int(os.getenv("NEWS_TOPIC_RELEVANCE_MIN_SCORE", "6")))
except ValueError:
    TOPIC_RELEVANCE_MIN_SCORE = 6
MAX_STORIES_PER_TOPIC = max(1, CONFIG.max_stories_per_topic)
MAX_ARTICLES_PER_STORY = max(MIN_ARTICLES_PER_STORY, CONFIG.max_articles_per_story)
STORY_CLUSTER_SIMILARITY_THRESHOLD = min(
    1.0,
    max(0.0, CONFIG.story_cluster_similarity_threshold),
)
STORY_SIMILARITY_TITLE_WEIGHT = 1
STORY_SIMILARITY_DESCRIPTION_WEIGHT = 1
STORY_SIMILARITY_TEXT_WEIGHT = 4
STORY_MIN_SOURCE_COUNT = 2
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
    "retries": 0,
    "fallbacks": 0,
    "failures": {},
}
MODEL_CALL_STATS_LOCK = Lock()
RUN_ACTIVITY_SNAPSHOTS: list[dict[str, Any]] = []
ACTIVE_RUN_DIAGNOSTICS: RunDiagnostics | None = None


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

SOURCE_FEEDS = load_sources(CONFIG.sources_path)
TOP_FUNNEL_PROVIDERS: dict[str, dict[str, Any]] = {}

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

STORY_PAIR_DEBUG_LIMIT = 250
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
        "model",
        "setup",
        "topics",
        "sources",
        "stories",
        "summaries",
        "report",
        "finalize",
    ]
    STEP_LABELS = {
        "model": "model",
        "setup": "setup",
        "topics": "topics",
        "sources": "sources",
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

    def start_source(self, source_index: int) -> None:
        if self.current_step != "sources":
            self.current_step = "sources"
        self.meter_done = max(0, min(self.meter_total, source_index - 1))
        self._render_meter()

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
            email: {"name": email, "personal_prompt": None, "pause": False}
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
                "personal_prompt": None,
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
        email: {"name": email, "personal_prompt": None, "pause": False}
        for email in EMAIL_RECIPIENTS_FALLBACK
    }


def build_prompt_groups(recipient_config: dict[str, dict]) -> List[dict]:
    default_group = {
        "group_key": "default_prompt",
        "custom_prompt_text": None,
        "uses_default_prompt": True,
        "recipient_emails": [],
        "recipient_names": [],
    }
    custom_groups: List[dict] = []
    custom_group_lookup: dict[str, dict] = {}

    for email, settings in recipient_config.items():
        personal_prompt = settings.get("personal_prompt")
        target_group = default_group
        if personal_prompt is not None:
            target_group = custom_group_lookup.get(personal_prompt)
            if target_group is None:
                target_group = {
                    "group_key": f"custom_prompt_{len(custom_groups) + 1:02d}",
                    "custom_prompt_text": personal_prompt,
                    "uses_default_prompt": False,
                    "recipient_emails": [],
                    "recipient_names": [],
                }
                custom_group_lookup[personal_prompt] = target_group
                custom_groups.append(target_group)

        target_group["recipient_emails"].append(email)
        target_group["recipient_names"].append(settings.get("name") or email)

    groups: List[dict] = []
    if default_group["recipient_emails"]:
        groups.append(default_group)
    groups.extend(custom_groups)
    return groups


def _slugify_report_suffix(value: str) -> str:
    clean_value = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return clean_value or "report"


def build_report_path(group: dict) -> str:
    if group.get("uses_default_prompt"):
        suffix = "default_prompt"
    elif len(group.get("recipient_emails", [])) == 1:
        suffix = _slugify_report_suffix(group["recipient_emails"][0])
    else:
        suffix = group.get("group_key") or "prompt_group"

    base_path = os.path.join(RUN_OUTPUT_DIR, f"news_report_{timestamp}_{MODEL_PROFILE_KEY}_{suffix}")
    return f"{base_path}.txt"

# --- TOOLS ---

def internet_search(query: str) -> str:
    try:
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=4)]
            return json.dumps(results)
    except Exception as e:
        return f"Search failed: {e}"


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


def scrape_article_text(url: str) -> tuple[str, str]:
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            content = trafilatura.extract(downloaded)
            if content:
                clean_content = _clean_content_text(content)
                return (clean_content, "scraped") if clean_content else ("Scraper found no text.", "scraper_no_text")
            return "Scraper found no text.", "scraper_no_text"
        return "Access Denied.", "access_denied"
    except Exception:
        return "Scrape Error.", "scrape_error"


def web_scrape(url: str) -> str:
    return scrape_article_text(url)[0]


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
) -> dict[str, Any]:
    resolution = _resolve_google_news_url_details(original_url)
    resolved_url = resolution.get("resolved_url") or str(original_url or "").strip()
    fallback_text = _build_feed_fallback_text(title, description)

    if not resolved_url:
        return {
            **resolution,
            "text": fallback_text,
            "scrape_status": "missing_url",
            "feed_fallback_used": bool(fallback_text),
        }

    if _is_google_news_url(resolved_url):
        return {
            **resolution,
            "resolved_url": resolved_url,
            "text": fallback_text,
            "scrape_status": "google_news_unresolved" if fallback_text else "google_news_unresolved_no_fallback",
            "feed_fallback_used": bool(fallback_text),
        }

    article_text, scrape_status = scrape_article_text(resolved_url)
    if scrape_status != "scraped":
        if fallback_text:
            return {
                **resolution,
                "resolved_url": resolved_url,
                "text": fallback_text,
                "scrape_status": f"{scrape_status}_feed_fallback",
                "feed_fallback_used": True,
            }
        return {
            **resolution,
            "resolved_url": resolved_url,
            "text": "",
            "scrape_status": scrape_status,
            "feed_fallback_used": False,
        }

    return {
        **resolution,
        "resolved_url": resolved_url,
        "text": article_text,
        "scrape_status": "scraped",
        "feed_fallback_used": False,
    }

def _translate_if_needed(text: str, title: str = "") -> str:
    """Use the local LLM to translate non-English article text."""
    sample = (title + " " + text)[:300].strip()
    if not sample:
        return text
    ascii_letters = sum(1 for c in sample if c.isascii() and c.isalpha())
    total_letters = sum(1 for c in sample if c.isalpha())
    if total_letters == 0 or ascii_letters / total_letters > 0.7:
        return text

    progress_tracker.warning(f"Translating {title[:56]}")
    try:
        llm = build_chat_model(
            max_tokens=TRANSLATION_MAX_TOKENS,
            task="translation",
        )
        response = invoke_with_retries(
            llm,
            [
                SystemMessage(content=(
                    "Translate the following article text into English. "
                    "Output ONLY the translated text, nothing else. "
                    "Preserve paragraph structure."
                )),
                HumanMessage(content=text[:5000]),
            ],
            task_name="translation",
            fallback_content=text,
        )
        translated = strip_model_artifacts(response.content)
        return translated if translated else text
    except Exception:
        return text

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


def _clean_content_text(text: str | None) -> str:
    """Normalize scraped/feed text before it reaches summarization or matching."""
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
            clean_text = BeautifulSoup(clean_text, "html.parser").get_text(" ", strip=True)
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
    clean_text = re.sub(r"\s+", " ", clean_text)
    return clean_text.strip()


def _clean_feed_text(text: str | None) -> str:
    return _clean_content_text(text)


def _clean_feed_url(text: str | None) -> str:
    clean_url = html.unescape(str(text or "")).strip()
    clean_url = re.sub(r"\s+", "", clean_url)
    return clean_url


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
    topic_limit = effective_num_topics if DEV else None
    topics = load_predefined_topics_for_run(topic_limit)
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
    section_candidates: dict[str, list[dict]] = {topic["key"]: [] for topic in topics}
    post_scrape_rejections: list[dict[str, Any]] = []
    scrape_attempts: list[dict[str, Any]] = []
    scrape_status_counts: Counter[str] = Counter()
    scrape_cache: dict[str, dict[str, Any]] = {}
    scraped_text_count = 0

    for item in items:
        original_rss_url = str(item.get("link") or "").strip()
        if not original_rss_url:
            continue
        if not _is_within_recent_window(item.get("published_at"), now_utc):
            continue

        if original_rss_url in scrape_cache:
            scrape_result = scrape_cache[original_rss_url]
        else:
            scrape_result = _resolve_and_scrape_feed_article(
                original_rss_url,
                title=item.get("title"),
                description=item.get("description"),
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
            "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
            "matched_topics": [],
        }
        if not selected_url:
            scrape_attempts.append(attempt)
            continue

        article_text = str(scrape_result.get("text") or "").strip()
        if not article_text:
            scrape_attempts.append(attempt)
            continue

        article_text = _translate_if_needed(article_text, item.get("title", ""))
        clean_article_text = _clean_content_text(article_text)
        if not clean_article_text:
            scrape_attempts.append(attempt)
            continue

        scraped_text_count += 1
        full_relevance_text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("description") or ""),
                clean_article_text,
            ]
        )
        summary_text = truncate_text_to_token_limit(clean_article_text, ARTICLE_TEXT_TOKEN_LIMIT)

        for topic in topics:
            topic_key = str(topic.get("key") or "")
            final_relevance_score = _score_topic_text_relevance(full_relevance_text, topic)
            if final_relevance_score <= 0:
                continue
            attempt["matched_topics"].append(
                {
                    "topic_title": topic.get("title"),
                    "topic_key": topic_key,
                    "relevance_score": final_relevance_score,
                }
            )
            section_candidates[topic_key].append(
                {
                    **item,
                    "url": selected_url,
                    "original_rss_url": original_rss_url,
                    "resolved_url": selected_url,
                    "resolution_status": scrape_result.get("resolution_status"),
                    "scrape_status": scrape_status,
                    "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                    "text": summary_text,
                    "title": item.get("title", ""),
                    "pub_date": item.get("pub_date", ""),
                    "description": item.get("description", ""),
                    "topic_key": topic_key,
                    "topic_title": topic.get("title"),
                    "relevance_score": final_relevance_score,
                }
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
                "description": article.get("description", ""),
                "text": article.get("text", ""),
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
                "scrape_status": article.get("scrape_status"),
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
            scrape_result = _resolve_and_scrape_feed_article(
                original_rss_url,
                title=str(story.get("title") or ""),
                description=str(story.get("description") or ""),
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

            article_text = _translate_if_needed(article_text, str(story.get("title") or ""))
            target_index = len(fallback_targets) + 1
            fallback_targets.append(
                {
                    "article_id": f"top-funnel-{topic_key}-{target_index}",
                    "source": _story_source_label(story),
                    "title": story.get("title", ""),
                    "pub_date": _story_pub_date(story),
                    "url": selected_url,
                    "original_rss_url": original_rss_url,
                    "resolved_url": selected_url,
                    "resolution_status": scrape_result.get("resolution_status"),
                    "scrape_status": scrape_status,
                    "feed_fallback_used": bool(scrape_result.get("feed_fallback_used")),
                    "description": story.get("description", ""),
                    "text": prepare_article_text_for_summary(article_text),
                    "topic_key": topic_key,
                    "topic_title": topic_title,
                    "relevance_score": _score_topic_against_story(topic, story),
                    "coverage_fallback": True,
                }
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


def _story_slug(value: str, fallback: str = "story") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or fallback


def _clean_story_title(value: str, topic: dict, articles: list[dict]) -> str:
    clean_value = _strip_inline_markdown(strip_model_artifacts(value or ""))
    clean_value = re.sub(r"[\r\n]+", " ", clean_value)
    clean_value = re.sub(r"\s+", " ", clean_value).strip(" \"'")
    topic_title = str(topic.get("title") or "").strip()
    if not clean_value or clean_value.lower() == topic_title.lower():
        first_title = str((articles[0] if articles else {}).get("title") or "").strip()
        clean_value = _clean_topic_source_title(first_title) or topic_title or "News update"
    words = clean_value.split()
    if len(words) > 14:
        clean_value = " ".join(words[:14])
    return clean_value[:120].strip() or "News update"


def _story_similarity_stopwords(topic: dict) -> set[str]:
    broad_topic_terms = set(
        _ordered_topic_match_terms(topic.get("key"), topic.get("title"), topic.get("rationale"))
    )
    generic_terms = TOPIC_MATCH_STOPWORDS | {
        "article",
        "briefing",
        "coverage",
        "daily",
        "development",
        "developments",
        "newsletter",
        "official",
        "officials",
        "reported",
        "report",
        "reports",
        "said",
        "says",
        "source",
        "story",
        "update",
        "updates",
    }
    return broad_topic_terms | generic_terms | WEAK_TOPIC_MATCH_TERMS | BOILERPLATE_CONTENT_STOPWORDS


def _normalize_story_similarity_token(token: str) -> str:
    normalized = token.strip("'")
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


def _story_similarity_terms(text: str, stopwords: set[str]) -> list[str]:
    terms: list[str] = []
    clean_text = _clean_content_text(text)
    for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", clean_text.lower()):
        token_variants = [token]
        if "-" in token:
            token_variants.extend(part for part in token.split("-") if part)
        for variant in token_variants:
            normalized = _normalize_story_similarity_token(variant)
            if len(normalized) < 3 or normalized.isdigit() or normalized in stopwords:
                continue
            terms.append(normalized)
    return terms


def _story_weighted_term_counts(article: dict, topic: dict) -> Counter[str]:
    stopwords = _story_similarity_stopwords(topic)
    weighted_parts = [
        (
            _clean_content_text(_clean_topic_source_title(str(article.get("title") or ""))),
            STORY_SIMILARITY_TITLE_WEIGHT,
        ),
        (_clean_content_text(str(article.get("description") or "")), STORY_SIMILARITY_DESCRIPTION_WEIGHT),
        (_clean_content_text(str(article.get("text") or "")), STORY_SIMILARITY_TEXT_WEIGHT),
    ]
    counts: Counter[str] = Counter()
    for text, weight in weighted_parts:
        for term in _story_similarity_terms(text, stopwords):
            counts[term] += weight
    return counts


def _build_story_tfidf_vectors(
    topic: dict,
    articles: list[dict],
) -> tuple[list[dict[str, float]], list[float]]:
    document_counts = [_story_weighted_term_counts(article, topic) for article in articles]
    document_frequency: Counter[str] = Counter()
    for counts in document_counts:
        document_frequency.update(counts.keys())

    total_documents = max(1, len(document_counts))
    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for counts in document_counts:
        vector: dict[str, float] = {}
        for term, count in counts.items():
            tf = 1.0 + math.log(max(1, count))
            idf = math.log((1 + total_documents) / (1 + document_frequency[term])) + 1.0
            vector[term] = tf * idf
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        vectors.append(vector)
        norms.append(norm)
    return vectors, norms


def _cosine_similarity(
    left: dict[str, float],
    left_norm: float,
    right: dict[str, float],
    right_norm: float,
) -> float:
    if not left_norm or not right_norm:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot_product = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    if dot_product <= 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)


def _story_pair_key(left_index: int, right_index: int) -> tuple[int, int]:
    return (left_index, right_index) if left_index < right_index else (right_index, left_index)


def _story_pair_similarity(
    similarities: dict[tuple[int, int], float],
    left_index: int,
    right_index: int,
) -> float:
    if left_index == right_index:
        return 1.0
    return similarities.get(_story_pair_key(left_index, right_index), 0.0)


def _story_component_average_similarity(
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> float:
    if len(component) < 2:
        return 0.0
    total = 0.0
    pair_count = 0
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            total += _story_pair_similarity(similarities, left_index, right_index)
            pair_count += 1
    return total / pair_count if pair_count else 0.0


def _story_component_pair_count(component: list[int]) -> int:
    size = len(component)
    return (size * (size - 1)) // 2


def _story_component_edge_count(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> int:
    edge_count = 0
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            if _story_pair_similarity(similarities, left_index, right_index) >= similarity_threshold:
                edge_count += 1
    return edge_count


def _story_component_best_similarities(
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> list[float]:
    best_scores: list[float] = []
    for index in component:
        other_scores = [
            _story_pair_similarity(similarities, index, other_index)
            for other_index in component
            if other_index != index
        ]
        best_scores.append(max(other_scores, default=0.0))
    return best_scores


def _minimum_story_edge_density(component_size: int) -> float:
    if component_size <= 2:
        return 1.0
    if component_size == 3:
        return 2.0 / 3.0
    if component_size <= 5:
        return 0.50
    return 0.45


def _story_component_connectedness_metrics(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> dict[str, float | int]:
    pair_count = _story_component_pair_count(component)
    edge_count = _story_component_edge_count(component, similarities, similarity_threshold)
    average_similarity = _story_component_average_similarity(component, similarities)
    best_scores = _story_component_best_similarities(component, similarities)
    mean_best_similarity = sum(best_scores) / len(best_scores) if best_scores else 0.0
    min_best_similarity = min(best_scores, default=0.0)
    edge_density = edge_count / pair_count if pair_count else 0.0
    connectedness_score = (
        (mean_best_similarity * 0.40)
        + (average_similarity * 0.25)
        + (edge_density * 0.20)
        + (min_best_similarity * 0.15)
    )
    support_multiplier = math.log1p(max(1, len(component)))
    story_strength_score = connectedness_score * support_multiplier
    return {
        "pair_count": pair_count,
        "edge_count": edge_count,
        "edge_density": edge_density,
        "average_similarity": average_similarity,
        "mean_best_similarity": mean_best_similarity,
        "min_best_similarity": min_best_similarity,
        "connectedness_score": connectedness_score,
        "story_strength_score": story_strength_score,
    }


def _story_component_source_count(component: list[int], source_identities: list[str]) -> int:
    return len({source_identities[index] for index in component})


def _story_component_has_source_diversity(component: list[int], source_identities: list[str]) -> bool:
    return _story_component_source_count(component, source_identities) >= min(
        STORY_MIN_SOURCE_COUNT,
        len(component),
    )


def _story_subcomponents_from_adjacency(
    component: list[int],
    adjacency: dict[int, set[int]],
) -> list[list[int]]:
    component_set = set(component)
    subcomponents: list[list[int]] = []
    visited: set[int] = set()
    for start_index in sorted(component):
        if start_index in visited:
            continue
        stack = [start_index]
        visited.add(start_index)
        subcomponent: list[int] = []
        while stack:
            index = stack.pop()
            subcomponent.append(index)
            for neighbor in adjacency.get(index, set()):
                if neighbor not in component_set or neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)
        subcomponents.append(sorted(subcomponent))
    return subcomponents


def _story_similarity_edges(
    component: list[int],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
) -> list[tuple[float, int, int]]:
    edges: list[tuple[float, int, int]] = []
    for offset, left_index in enumerate(component):
        for right_index in component[offset + 1 :]:
            score = _story_pair_similarity(similarities, left_index, right_index)
            if score >= similarity_threshold:
                edges.append((score, left_index, right_index))
    return edges


def _story_component_meets_connectedness_floor(
    component: list[int],
    source_identities: list[str],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
    *,
    min_articles_per_story: int,
) -> bool:
    if len(component) < min_articles_per_story:
        return False
    if not _story_component_has_source_diversity(component, source_identities):
        return False
    metrics = _story_component_connectedness_metrics(component, similarities, similarity_threshold)
    if float(metrics["min_best_similarity"]) < similarity_threshold:
        return False
    return float(metrics["edge_density"]) >= _minimum_story_edge_density(len(component))


def _split_story_component_by_weak_bridges(
    component: list[int],
    source_identities: list[str],
    similarities: dict[tuple[int, int], float],
    similarity_threshold: float,
    *,
    min_articles_per_story: int,
) -> list[list[int]]:
    if len(component) < min_articles_per_story:
        return []
    edges = _story_similarity_edges(component, similarities, similarity_threshold)
    if not edges:
        return []

    base_metrics = _story_component_connectedness_metrics(component, similarities, similarity_threshold)
    base_strength = float(base_metrics["story_strength_score"])
    for _, left_index, right_index in sorted(edges, key=lambda edge: (edge[0], edge[1], edge[2])):
        adjacency: dict[int, set[int]] = {index: set() for index in component}
        for edge_score, edge_left, edge_right in edges:
            if (edge_left, edge_right) == (left_index, right_index):
                continue
            adjacency[edge_left].add(edge_right)
            adjacency[edge_right].add(edge_left)
        subcomponents = _story_subcomponents_from_adjacency(component, adjacency)
        if len(subcomponents) <= 1:
            continue
        retained = [
            subcomponent
            for subcomponent in subcomponents
            if _story_component_meets_connectedness_floor(
                subcomponent,
                source_identities,
                similarities,
                similarity_threshold,
                min_articles_per_story=min_articles_per_story,
            )
        ]
        if len(retained) < 2:
            continue
        split_strength = sum(
            float(
                _story_component_connectedness_metrics(
                    subcomponent,
                    similarities,
                    similarity_threshold,
                )["story_strength_score"]
            )
            for subcomponent in retained
        )
        if split_strength <= base_strength * 1.05:
            continue
        split_components: list[list[int]] = []
        for subcomponent in retained:
            split_components.extend(
                _split_story_component_by_weak_bridges(
                    subcomponent,
                    source_identities,
                    similarities,
                    similarity_threshold,
                    min_articles_per_story=min_articles_per_story,
                )
            )
        return split_components

    if _story_component_meets_connectedness_floor(
        component,
        source_identities,
        similarities,
        similarity_threshold,
        min_articles_per_story=min_articles_per_story,
    ):
        return [component]
    return []


def _story_index_average_similarity(
    index: int,
    component: list[int],
    similarities: dict[tuple[int, int], float],
) -> float:
    other_indexes = [other_index for other_index in component if other_index != index]
    if not other_indexes:
        return 0.0
    return sum(
        _story_pair_similarity(similarities, index, other_index)
        for other_index in other_indexes
    ) / len(other_indexes)


def _story_component_medoid_index(
    component: list[int],
    articles: list[dict],
    similarities: dict[tuple[int, int], float],
) -> int:
    def rank(index: int) -> tuple:
        article = articles[index]
        return (
            -_story_index_average_similarity(index, component, similarities),
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            index,
        )

    return sorted(component, key=rank)[0]


def _story_cluster_relevance_score(component: list[int], articles: list[dict]) -> float:
    if not component:
        return 0.0
    return sum(float(articles[index].get("relevance_score") or 0.0) for index in component) / len(component)


def _story_cluster_recency_rank(component: list[int], articles: list[dict]) -> tuple[int, int, int]:
    if not component:
        return (0, 0, 0)
    return max(_article_time_rank(articles[index]) for index in component)


def _article_source_identity(article: dict) -> str:
    source = str(article.get("source") or "").strip().lower()
    if source:
        return re.sub(r"\s+", " ", source)
    for key in ("resolved_url", "url", "original_rss_url"):
        parsed = urlparse(str(article.get(key) or ""))
        host = parsed.netloc.lower().removeprefix("www.")
        if host:
            return host
    return "unknown"


def _select_story_article_indexes(
    component: list[int],
    medoid_index: int,
    articles: list[dict],
    similarities: dict[tuple[int, int], float],
    *,
    max_articles_per_story: int,
) -> list[int]:
    limit = max(1, max_articles_per_story)

    def article_rank(index: int) -> tuple:
        article = articles[index]
        return (
            0 if index == medoid_index else 1,
            -_story_index_average_similarity(index, component, similarities),
            -int(article.get("relevance_score") or 0),
            tuple(-value for value in _article_time_rank(article)),
            str(article.get("source") or ""),
            index,
        )

    ranked_indexes = sorted(component, key=article_rank)
    source_count = len({_article_source_identity(articles[index]) for index in component})
    selected: list[int] = []
    selected_sources: set[str] = set()

    for index in ranked_indexes:
        if len(selected) >= limit:
            break
        source = _article_source_identity(articles[index])
        if source in selected_sources and len(selected_sources) < source_count:
            continue
        selected.append(index)
        selected_sources.add(source)

    for index in ranked_indexes:
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)

    return selected


def cluster_topic_stories_by_similarity(
    topic: dict,
    articles: list[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    max_articles_per_story: int = MAX_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> list[dict]:
    if len(articles) < min_articles_per_story:
        return []

    vectors, norms = _build_story_tfidf_vectors(topic, articles)
    source_identities = [_article_source_identity(article) for article in articles]
    similarities: dict[tuple[int, int], float] = {}
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(articles))}
    max_similarity_by_index: dict[int, float] = {index: 0.0 for index in range(len(articles))}
    pair_debug: list[dict[str, Any]] = []
    for left_index in range(len(articles)):
        for right_index in range(left_index + 1, len(articles)):
            score = _cosine_similarity(
                vectors[left_index],
                norms[left_index],
                vectors[right_index],
                norms[right_index],
            )
            if score > 0.0:
                similarities[(left_index, right_index)] = score
            max_similarity_by_index[left_index] = max(max_similarity_by_index[left_index], score)
            max_similarity_by_index[right_index] = max(max_similarity_by_index[right_index], score)
            distinct_source_pair = source_identities[left_index] != source_identities[right_index]
            linked = score >= similarity_threshold
            if linked:
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)
            if score > 0.0 or linked:
                pair_debug.append(
                    {
                        "left_article_id": articles[left_index].get("article_id"),
                        "right_article_id": articles[right_index].get("article_id"),
                        "similarity": round(score, 4),
                        "similarity_threshold": round(similarity_threshold, 4),
                        "linked": linked,
                        "distinct_source_pair": distinct_source_pair,
                    }
                )

    components: list[list[int]] = []
    visited: set[int] = set()
    eligible_indexes = {
        index
        for index, max_similarity in max_similarity_by_index.items()
        if max_similarity >= similarity_threshold
    }
    for start_index in range(len(articles)):
        if start_index not in eligible_indexes:
            continue
        if start_index in visited:
            continue
        stack = [start_index]
        component: list[int] = []
        visited.add(start_index)
        while stack:
            index = stack.pop()
            component.append(index)
            for neighbor in adjacency[index]:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)
        if len(component) >= min_articles_per_story:
            components.extend(
                _split_story_component_by_weak_bridges(
                    sorted(component),
                    source_identities,
                    similarities,
                    similarity_threshold,
                    min_articles_per_story=min_articles_per_story,
                )
            )

    article_id_by_index = {
        index: str(article.get("article_id") or "")
        for index, article in enumerate(articles)
    }
    story_groups: list[dict] = []
    for component in components:
        if not _story_component_meets_connectedness_floor(
            component,
            source_identities,
            similarities,
            similarity_threshold,
            min_articles_per_story=min_articles_per_story,
        ):
            continue
        metrics = _story_component_connectedness_metrics(component, similarities, similarity_threshold)
        source_count = _story_component_source_count(component, source_identities)
        medoid_index = _story_component_medoid_index(component, articles, similarities)
        selected_indexes = _select_story_article_indexes(
            component,
            medoid_index,
            articles,
            similarities,
            max_articles_per_story=max(max_articles_per_story, min_articles_per_story),
        )
        if len(selected_indexes) < min_articles_per_story:
            continue
        story_groups.append(
            {
                "title": _clean_story_title(
                    _clean_topic_source_title(str(articles[medoid_index].get("title") or "")),
                    topic,
                    [articles[medoid_index]],
                ),
                "article_ids": [
                    article_id_by_index[index]
                    for index in selected_indexes
                    if article_id_by_index[index]
                ],
                "cluster_article_ids": [
                    article_id_by_index[index]
                    for index in component
                    if article_id_by_index[index]
                ],
                "article_count": len(component),
                "selected_article_count": len(selected_indexes),
                "average_similarity": round(float(metrics["average_similarity"]), 4),
                "connectedness_score": round(float(metrics["connectedness_score"]), 4),
                "story_strength_score": round(float(metrics["story_strength_score"]), 4),
                "edge_density": round(float(metrics["edge_density"]), 4),
                "edge_count": int(metrics["edge_count"]),
                "mean_best_similarity": round(float(metrics["mean_best_similarity"]), 4),
                "min_best_similarity": round(float(metrics["min_best_similarity"]), 4),
                "source_count": source_count,
                "relevance_score": _story_cluster_relevance_score(component, articles),
                "latest_rank": _story_cluster_recency_rank(component, articles),
                "_medoid_index": medoid_index,
            }
        )

    def story_rank(story: dict) -> tuple:
        return (
            -float(story.get("story_strength_score") or 0.0),
            -float(story.get("connectedness_score") or 0.0),
            -int(story.get("article_count") or 0),
            -int(story.get("source_count") or 0),
            -float(story.get("relevance_score") or 0.0),
            tuple(-value for value in story.get("latest_rank", (0, 0, 0))),
            int(story.get("_medoid_index") or 0),
        )

    ranked_groups = sorted(story_groups, key=story_rank)
    for story in ranked_groups:
        story.pop("_medoid_index", None)
        story.pop("latest_rank", None)
    ranked_groups_debug = sorted(
        pair_debug,
        key=lambda entry: (
            not entry.get("linked"),
            -float(entry.get("similarity") or 0.0),
            str(entry.get("left_article_id") or ""),
            str(entry.get("right_article_id") or ""),
        ),
    )
    for story in ranked_groups:
        story["_pair_debug"] = ranked_groups_debug[:STORY_PAIR_DEBUG_LIMIT]
    return ranked_groups


def organize_article_targets_into_stories(
    article_targets: List[dict],
    topics: List[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
    max_stories_per_topic: int = MAX_STORIES_PER_TOPIC,
    max_articles_per_story: int = MAX_ARTICLES_PER_STORY,
    similarity_threshold: float = STORY_CLUSTER_SIMILARITY_THRESHOLD,
) -> tuple[List[dict], dict[str, Any]]:
    if min_articles_per_story <= 1:
        return article_targets, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "min_articles_per_story": min_articles_per_story,
            "max_stories_per_topic": max_stories_per_topic,
            "max_articles_per_story": max_articles_per_story,
            "similarity_threshold": similarity_threshold,
        }

    topic_by_key = {str(topic.get("key") or ""): topic for topic in topics}
    topic_order = [str(topic.get("key") or "") for topic in topics]
    articles_by_topic: dict[str, list[dict]] = {key: [] for key in topic_order}
    for article in article_targets:
        topic_key = str(article.get("topic_key") or "")
        articles_by_topic.setdefault(topic_key, []).append(article)

    annotated_by_original_id: dict[int, dict] = {}
    stories_by_topic: dict[str, list[dict]] = {}
    pair_debug_by_topic: dict[str, list[dict[str, Any]]] = {}
    dropped_articles_by_topic: dict[str, list[dict[str, Any]]] = {}
    dropped_by_topic: Counter[str] = Counter()

    for topic_key in topic_order + sorted(key for key in articles_by_topic if key not in topic_order):
        articles = articles_by_topic.get(topic_key) or []
        if not articles:
            continue
        topic = topic_by_key.get(
            topic_key,
            {
                "key": topic_key,
                "title": articles[0].get("topic_title") or topic_key or "Unknown topic",
                "keywords": [],
                "boost_phrases": [],
            },
        )
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        all_story_groups = cluster_topic_stories_by_similarity(
            topic,
            articles,
            min_articles_per_story=min_articles_per_story,
            max_articles_per_story=max_articles_per_story,
            similarity_threshold=similarity_threshold,
        )
        story_groups = all_story_groups[:max_stories_per_topic]
        if all_story_groups:
            pair_debug_by_topic[topic_title] = (
                all_story_groups[0].get("_pair_debug", [])[:STORY_PAIR_DEBUG_LIMIT]
            )
        if not story_groups:
            dropped_by_topic[topic_title] += len(articles)
            dropped_articles_by_topic[topic_title] = [
                {
                    "article_id": article.get("article_id"),
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "relevance_score": article.get("relevance_score"),
                    "reason": "no_supported_story_cluster",
                }
                for article in articles
            ]
            continue

        article_lookup = {str(article.get("article_id") or ""): article for article in articles}
        topic_story_records: list[dict] = []
        for story_index, story in enumerate(story_groups, start=1):
            story_title = _clean_story_title(str(story.get("title") or ""), topic, articles)
            article_ids = [
                str(article_id)
                for article_id in story.get("article_ids", [])
                if str(article_id) in article_lookup
            ]
            if len(article_ids) < min_articles_per_story:
                continue
            story_key = (
                f"{_story_slug(topic_key or topic_title, 'topic')}"
                f"-story-{story_index:02d}-{_story_slug(story_title)}"
            )
            topic_story_records.append(
                {
                    "story_key": story_key,
                    "story_title": story_title,
                    "article_count": len(article_ids),
                    "cluster_article_count": int(story.get("article_count") or len(article_ids)),
                    "selected_article_count": len(article_ids),
                    "article_ids": article_ids,
                    "cluster_article_ids": story.get("cluster_article_ids", article_ids),
                    "average_similarity": story.get("average_similarity"),
                    "connectedness_score": story.get("connectedness_score"),
                    "story_strength_score": story.get("story_strength_score"),
                    "edge_density": story.get("edge_density"),
                    "mean_best_similarity": story.get("mean_best_similarity"),
                    "min_best_similarity": story.get("min_best_similarity"),
                    "source_count": story.get("source_count"),
                }
            )
            for article_id in article_ids:
                original_article = article_lookup[article_id]
                annotated_by_original_id[id(original_article)] = {
                    **original_article,
                    "story_key": story_key,
                    "story_title": story_title,
                    "story_article_count": int(story.get("article_count") or len(article_ids)),
                    "story_selected_article_count": len(article_ids),
                    "story_average_similarity": story.get("average_similarity"),
                    "story_connectedness_score": story.get("connectedness_score"),
                    "story_strength_score": story.get("story_strength_score"),
                    "story_edge_density": story.get("edge_density"),
                    "story_source_count": story.get("source_count"),
                }
        included_ids = {
            id(article_lookup[article_id])
            for record in topic_story_records
            for article_id in record["article_ids"]
            if article_id in article_lookup
        }
        dropped_by_topic[topic_title] += len([article for article in articles if id(article) not in included_ids])
        dropped_articles = [article for article in articles if id(article) not in included_ids]
        if dropped_articles:
            dropped_articles_by_topic[topic_title] = [
                {
                    "article_id": article.get("article_id"),
                    "source": article.get("source"),
                    "title": article.get("title"),
                    "relevance_score": article.get("relevance_score"),
                    "reason": "outside_retained_story_or_article_cap",
                }
                for article in dropped_articles
            ]
        if topic_story_records:
            stories_by_topic[topic_title] = topic_story_records

    selected_targets = [
        annotated_by_original_id[id(article)]
        for article in article_targets
        if id(article) in annotated_by_original_id
    ]
    stats = {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected_targets),
        "dropped_count": len(article_targets) - len(selected_targets),
        "min_articles_per_story": min_articles_per_story,
        "max_stories_per_topic": max_stories_per_topic,
        "max_articles_per_story": max_articles_per_story,
        "similarity_threshold": similarity_threshold,
        "clustering_method": "cleaned_body_weighted_tfidf_similarity_graph",
        "story_count": sum(len(stories) for stories in stories_by_topic.values()),
        "stories_by_topic": stories_by_topic,
        "dropped_by_topic": dict(dropped_by_topic),
        "dropped_articles_by_topic": dropped_articles_by_topic,
        "pair_debug_by_topic": pair_debug_by_topic,
    }
    return selected_targets, stats


def filter_budgeted_targets_by_story_floor(
    article_targets: List[dict],
    *,
    min_articles_per_story: int = MIN_ARTICLES_PER_STORY,
) -> tuple[List[dict], dict[str, Any]]:
    if min_articles_per_story <= 1:
        return article_targets, {
            "enabled": False,
            "candidate_count": len(article_targets),
            "included_count": len(article_targets),
            "dropped_count": 0,
            "min_articles_per_story": min_articles_per_story,
        }

    grouped: dict[str, list[dict]] = {}
    for article in article_targets:
        story_key = str(article.get("story_key") or "").strip()
        if not story_key:
            continue
        grouped.setdefault(story_key, []).append(article)

    eligible_story_keys = {
        story_key
        for story_key, story_articles in grouped.items()
        if len(story_articles) >= min_articles_per_story
    }
    selected = [
        article
        for article in article_targets
        if str(article.get("story_key") or "").strip() in eligible_story_keys
    ]
    dropped = [
        article
        for article in article_targets
        if str(article.get("story_key") or "").strip() not in eligible_story_keys
    ]

    return selected, {
        "enabled": True,
        "candidate_count": len(article_targets),
        "included_count": len(selected),
        "dropped_count": len(dropped),
        "min_articles_per_story": min_articles_per_story,
        "dropped_article_ids": [article.get("article_id") for article in dropped],
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


def invoke_with_retries(
    llm,
    messages,
    *,
    task_name: str,
    fallback_content: str,
    attempts: int = MODEL_RETRY_ATTEMPTS,
) -> AIMessage:
    last_error = None
    with MODEL_CALL_STATS_LOCK:
        calls = MODEL_CALL_STATS.setdefault("calls", {})
        calls[task_name] = int(calls.get(task_name, 0)) + 1
    for attempt in range(1, attempts + 1):
        try:
            response = llm.invoke(messages)
            if isinstance(response, AIMessage):
                return response
            return AIMessage(content=str(getattr(response, "content", response)))
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


def prepare_article_text_for_summary(text: str) -> str:
    return truncate_text_to_token_limit(_clean_content_text(text), ARTICLE_TEXT_TOKEN_LIMIT)


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

# --- STATE ---

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    final_reports: List[str]
    articles_remaining: List[dict]
    empty_response_count: int
    final_synthesis_token_stats: dict
    generate_final_synthesis: bool
    final_prompt_text: str | None
    topics: List[dict]


def _is_empty_ai_response(message: BaseMessage) -> bool:
    if not isinstance(message, AIMessage):
        return False
    if getattr(message, "tool_calls", None):
        return False
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    clean_text = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    return not clean_text


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


def _final_synthesis_heading_count(text: str) -> int:
    return len(re.findall(r"(?m)^##+\s+\S", text or ""))


def _normalize_synthesis_heading_label(value: str) -> str:
    clean_value = _strip_inline_markdown(strip_model_artifacts(value or ""))
    clean_value = re.sub(r"[^a-z0-9]+", " ", clean_value.lower()).strip()
    return clean_value


def _final_synthesis_heading_labels(text: str) -> list[str]:
    clean_text = _strip_prompt_echo_lines(strip_model_artifacts(text or ""))
    return [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^##+\s+(.+?)\s*$", clean_text)
    ]


def _missing_required_topic_headings(text: str, required_headings: list[str]) -> list[str]:
    present = {
        _normalize_synthesis_heading_label(label)
        for label in _final_synthesis_heading_labels(text)
    }
    missing: list[str] = []
    for heading in required_headings:
        normalized = _normalize_synthesis_heading_label(heading)
        if normalized and normalized not in present:
            missing.append(heading)
    return missing


def describe_final_synthesis_rejection(
    text: str,
    topics: List[dict],
    *,
    uses_custom_prompt: bool,
    relaxed: bool = False,
    validation: dict[str, Any] | None = None,
) -> str:
    if _contains_disallowed_final_markup(text):
        return "disallowed topic markup"

    clean_text = _strip_prompt_echo_lines(strip_model_artifacts(text or ""))
    if not clean_text:
        return "empty after cleanup"
    if uses_custom_prompt:
        return ""

    heading_count = _final_synthesis_heading_count(clean_text)
    word_count = _final_synthesis_word_count(clean_text)
    required_headings = [
        str(heading)
        for heading in (validation or {}).get("required_topic_headings", [])
        if str(heading).strip()
    ]

    if required_headings:
        missing_headings = _missing_required_topic_headings(clean_text, required_headings)
        if missing_headings:
            return "missing required topic heading(s): " + ", ".join(missing_headings)
    elif heading_count == 0:
        return "missing markdown section heading"

    if relaxed:
        minimum_words = min(40, max(12, max(1, len(required_headings) or len(topics)) * 8))
    else:
        minimum_words = min(120, max(50, max(1, len(required_headings) or len(topics)) * 35))
    if word_count < minimum_words:
        return f"too short ({word_count}/{minimum_words} words)"
    return ""


def is_valid_final_synthesis_response(
    text: str,
    topics: List[dict],
    *,
    uses_custom_prompt: bool,
    relaxed: bool = False,
    validation: dict[str, Any] | None = None,
) -> bool:
    return not describe_final_synthesis_rejection(
        text,
        topics,
        uses_custom_prompt=uses_custom_prompt,
        relaxed=relaxed,
        validation=validation,
    )


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

    section_pattern = re.compile(r"(?m)^##+\s+(.+?)\s*$")
    matches = list(section_pattern.finditer(clean_text))
    if not matches:
        return clean_text.strip() if relaxed else (
            "" if _is_low_coverage_synthesis_section(clean_text) else clean_text.strip()
        )

    prefix = clean_text[: matches[0].start()].strip()
    kept_sections: list[str] = [prefix] if prefix else []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean_text)
        section = clean_text[match.start():end].strip()
        section_body = clean_text[match.end():end].strip()
        if not relaxed and _is_low_coverage_synthesis_section(section_body):
            continue
        kept_sections.append(section)

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


# --- DEFAULT FINAL SYNTHESIS INSTRUCTIONS (DYNAMIC, ONE SECTION PER TOPIC) ---

def _format_topic_section_header(topic_title: str) -> str:
    return topic_title.upper()


def get_default_final_synthesis_instructions(topics: List[dict]) -> str:
    section_lines = "\n".join(
        f"       ## {_format_topic_section_header(topic['title'])}"
        for topic in topics
    )
    return textwrap.dedent(f"""
        TASK:
        1) Output ONLY this exact structure (one section per selected topic, in order):
{section_lines}
        2) Use only claims supported by PRIMARY_DATASET; if support is weak or conflicting, place
           that point in the same section with explicit uncertainty language.
        3) PRIMARY_DATASET is divided into Topic/Story blocks. Within each topic section, write one
           distinct paragraph per Story block. Do not merge different Story blocks into the same
           paragraph, and do not shift to a new Story block mid-paragraph.
        4) Treat a Story block as eligible only when it has at least {MIN_ARTICLES_PER_STORY} source
           summaries. Ignore any singleton, broad background recap, explainer, or generic state-of-play
           material. Do not compensate for omitted singleton stories by writing a broader recap.
        5) Lead each paragraph with today's reported development. Avoid opening sentences that recap a
           longstanding conflict, market cycle, or policy debate unless the recap itself is newly reported.
        6) Each story paragraph should be roughly 70-130 words of cohesive prose
           (no bullets, no label-colon fragments). Read like a compact wire-service roundup.
        7) Focus on concrete reported claims: who acted, what happened, where, when, casualties or
           damage, official statements, deadlines, and what remains unconfirmed.
        8) Do not include a preamble, methodology, or outlet-style commentary.
        9) If a section has no credible updates in the dataset, omit that section entirely. Do not
           explain missing coverage, apologize, or mention empty source material.
        10) Write for newsletter recipients. Do not mention the user, the prompt, AI, PRIMARY_DATASET,
           LOW_CONFIDENCE_DATASET, supplied coverage, or source-material limitations in the final copy.
        11) Do not write XML/HTML-style topic tags, label-only lines, bullets, or headline lists.
        12) Do not invent facts beyond what PRIMARY_DATASET supports. Treat LOW_CONFIDENCE_DATASET
           entries as headline-only material; they may justify uncertainty language but never establish
           a claim on their own.
    """).strip()


def _report_topic_label(entry: str) -> str:
    topic_match = re.search(r"^- Topic:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return topic_match.group(1).strip() if topic_match else ""


def _report_story_label(entry: str) -> str:
    story_match = re.search(r"^- Story:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return story_match.group(1).strip() if story_match else ""


def _report_summary_text(entry: str) -> str:
    summary_match = re.search(r"Summary:\s*(.*)", entry or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", summary_match.group(1).strip()) if summary_match else ""


def _collect_grouped_synthesis_blocks(
    reports: List[str],
    topics: List[dict],
) -> tuple[list[str], dict[str, dict[str, list[str]]], list[str], bool]:
    topic_order = [str(topic.get("title") or "").strip() for topic in topics if topic.get("title")]
    grouped: dict[str, dict[str, list[str]]] = {topic_title: {} for topic_title in topic_order}
    ungrouped: list[str] = []
    explicit_story_mode = any(_report_story_label(report) for report in reports)

    for report in reports:
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

    return topic_order, grouped, ungrouped, explicit_story_mode


def _required_synthesis_structure_for_reports(reports: List[str], topics: List[dict]) -> dict[str, Any]:
    topic_order, grouped, _, explicit_story_mode = _collect_grouped_synthesis_blocks(reports, topics)
    required_topic_titles: list[str] = []
    story_blocks_by_topic: dict[str, list[str]] = {}
    remaining_topics = [topic for topic in grouped.keys() if topic not in topic_order]
    for topic_title in topic_order + sorted(remaining_topics):
        story_map = grouped.get(topic_title) or {}
        story_titles: list[str] = []
        for story_title, summaries in story_map.items():
            if explicit_story_mode and len(summaries) < MIN_ARTICLES_PER_STORY:
                continue
            if summaries:
                story_titles.append(story_title)
        if story_titles:
            required_topic_titles.append(topic_title)
            story_blocks_by_topic[topic_title] = story_titles
    return {
        "required_topic_titles": required_topic_titles,
        "required_topic_headings": [
            _format_topic_section_header(topic_title)
            for topic_title in required_topic_titles
        ],
        "required_story_blocks_by_topic": story_blocks_by_topic,
        "eligible_story_block_count": sum(len(stories) for stories in story_blocks_by_topic.values()),
        "explicit_story_mode": explicit_story_mode,
    }


def _build_grouped_synthesis_dataset(reports: List[str], topics: List[dict]) -> str:
    topic_order, grouped, ungrouped, explicit_story_mode = _collect_grouped_synthesis_blocks(reports, topics)
    sections: list[str] = []
    remaining_topics = [topic for topic in grouped.keys() if topic not in topic_order]
    for topic_title in topic_order + sorted(remaining_topics):
        story_map = grouped.get(topic_title) or {}
        if not story_map:
            continue
        for story_title, summaries in story_map.items():
            if explicit_story_mode and len(summaries) < MIN_ARTICLES_PER_STORY:
                continue
            lines = [f"Topic: {topic_title}", f"Story: {story_title}", "Source summaries:"]
            for index, summary_text in enumerate(summaries, start=1):
                lines.append(f"{index}. {summary_text}")
            sections.append("\n".join(lines))

    if ungrouped and not explicit_story_mode:
        lines = ["Other source summaries:"]
        for index, summary_text in enumerate(ungrouped, start=1):
            lines.append(f"{index}. {summary_text}")
        sections.append("\n".join(lines))

    return "\n\n---\n\n".join(sections)


def _eligible_story_synthesis_blocks(final_reports: List[str], topics: List[dict]) -> list[dict[str, Any]]:
    topic_order, grouped, _, explicit_story_mode = _collect_grouped_synthesis_blocks(final_reports, topics)
    blocks: list[dict[str, Any]] = []
    remaining_topics = [topic for topic in grouped.keys() if topic not in topic_order]
    for topic_index, topic_title in enumerate(topic_order + sorted(remaining_topics)):
        story_map = grouped.get(topic_title) or {}
        for story_index, (story_title, summaries) in enumerate(story_map.items()):
            if explicit_story_mode and len(summaries) < MIN_ARTICLES_PER_STORY:
                continue
            if not summaries:
                continue
            blocks.append(
                {
                    "topic_title": topic_title,
                    "story_title": story_title,
                    "summaries": summaries,
                    "topic_index": topic_index,
                    "story_index": story_index,
                }
            )
    return blocks


def build_story_synthesis_prompt_messages(story_block: dict[str, Any], now_label: str) -> list[BaseMessage]:
    topic_title = str(story_block.get("topic_title") or "Unknown topic")
    story_title = str(story_block.get("story_title") or "Story update")
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    source_summary_lines = "\n".join(
        f"{index}. {summary}"
        for index, summary in enumerate(summaries, start=1)
    )
    system_prompt = SystemMessage(content=textwrap.dedent(f"""
        Today: {now_label}.
        You are synthesizing prewritten article summaries into one newsletter story paragraph.
        Use only the supplied source summaries.
        Write exactly one cohesive prose paragraph, roughly 70-130 words.
        Lead with today's reported development. Include concrete reported claims, named actors,
        places, timing, figures, damage, statements, deadlines, and uncertainty when supported.
        Do not write a heading, bullets, labels, source-material notes, methodology, or preamble.
        Do not merge in background material unless a source summary reports it as part of today's update.
    """).strip())
    user_prompt = HumanMessage(content=textwrap.dedent(f"""
        Topic: {topic_title}
        Story: {story_title}

        Source summaries:
        {source_summary_lines}

        Return only the story paragraph.
    """).strip())
    return [system_prompt, user_prompt]


def _clean_story_synthesis_paragraph(raw_text: str, summaries: list[str]) -> str:
    clean_text = _strip_prompt_echo_lines(strip_model_artifacts(raw_text or ""))
    clean_text = re.sub(r"(?m)^##+\s+.*$", "", clean_text)
    clean_text = re.sub(r"(?mi)^(topic|story|source summaries?|paragraph)\s*:\s*.*$", "", clean_text)
    clean_text = re.sub(r"(?m)^\s*[-*]\s+", "", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    if clean_text and not _is_low_coverage_synthesis_section(clean_text):
        return clean_text
    return _dev_synthesis_paragraph_from_summaries(summaries)


def _run_story_synthesis_block(story_block: dict[str, Any], now_label: str) -> dict[str, Any]:
    summaries = [str(summary or "").strip() for summary in story_block.get("summaries", []) if str(summary or "").strip()]
    fallback_paragraph = _dev_synthesis_paragraph_from_summaries(summaries)
    prompt_messages = build_story_synthesis_prompt_messages(story_block, now_label)
    estimated_input_tokens = sum(estimate_message_token_count(message) for message in prompt_messages)
    response = invoke_with_retries(
        build_chat_model(
            max_tokens=max(300, min(900, FINAL_SYNTHESIS_MAX_TOKENS)),
            task="final_synthesis",
        ),
        prompt_messages,
        task_name=f"story synthesis for {story_block.get('story_title') or 'story'}",
        fallback_content=fallback_paragraph,
    )
    paragraph = _clean_story_synthesis_paragraph(response.content, summaries)
    prompt_tokens = extract_prompt_tokens_from_response(response)
    return {
        **story_block,
        "paragraph": paragraph,
        "estimated_input_tokens": estimated_input_tokens,
        "actual_prompt_tokens": prompt_tokens,
        "word_count": _final_synthesis_word_count(paragraph),
        "valid": bool(paragraph),
        "reason": "accepted" if paragraph else "empty after cleanup",
        "preview": paragraph[:500],
    }


def _run_story_synthesis_blocks(story_blocks: list[dict[str, Any]], now_label: str) -> list[dict[str, Any]]:
    if ARTICLE_SUMMARY_CONCURRENCY > 1 and len(story_blocks) > 1:
        ordered_results: list[tuple[int, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=ARTICLE_SUMMARY_CONCURRENCY) as executor:
            future_map = {
                executor.submit(_run_story_synthesis_block, story_block, now_label): index
                for index, story_block in enumerate(story_blocks)
            }
            for future in as_completed(future_map):
                ordered_results.append((future_map[future], future.result()))
        return [
            result
            for _, result in sorted(ordered_results, key=lambda item: item[0])
        ]
    return [_run_story_synthesis_block(story_block, now_label) for story_block in story_blocks]


def _assemble_story_synthesis(results: list[dict[str, Any]], topics: List[dict]) -> str:
    topic_order = [str(topic.get("title") or "").strip() for topic in topics if topic.get("title")]
    grouped: dict[str, list[str]] = {topic_title: [] for topic_title in topic_order}
    for result in results:
        paragraph = str(result.get("paragraph") or "").strip()
        if not paragraph:
            continue
        topic_title = str(result.get("topic_title") or "").strip()
        grouped.setdefault(topic_title, []).append(paragraph)

    sections: list[str] = []
    remaining_topics = [topic_title for topic_title in grouped.keys() if topic_title not in topic_order]
    for topic_title in topic_order + sorted(remaining_topics):
        paragraphs = grouped.get(topic_title) or []
        if not paragraphs:
            continue
        sections.append(
            f"## {_format_topic_section_header(topic_title)}\n"
            + "\n\n".join(paragraphs)
        )
    return "\n\n".join(sections)


def run_story_synthesis_pass(
    final_reports: List[str],
    topics: List[dict],
) -> tuple[str, dict, dict]:
    now = datetime.now().strftime("%B %d, %Y")
    story_blocks = _eligible_story_synthesis_blocks(final_reports, topics)
    primary_dataset = _build_grouped_synthesis_dataset(final_reports, topics)
    required_structure = _required_synthesis_structure_for_reports(final_reports, topics)
    token_stats: dict[str, Any] = {
        "synthesis_method": "per_story_parallel",
        "story_synthesis_concurrency": ARTICLE_SUMMARY_CONCURRENCY,
        "total_reports": len(final_reports),
        "reports_included_in_synthesis": len(final_reports),
        "reports_omitted_from_synthesis": 0,
        "high_confidence_reports": len([entry for entry in final_reports if not is_low_confidence_report_entry(entry)]),
        "low_confidence_reports": len([entry for entry in final_reports if is_low_confidence_report_entry(entry)]),
        "story_blocks_included": len(story_blocks),
        "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,
        "model_profile": MODEL_PROFILE_KEY,
        "model": MODEL_REFERENCE,
        "model_name": MODEL_NAME,
        "model_backend": MODEL_BACKEND,
        "uses_custom_prompt": False,
        "topic_count": len(topics),
        "primary_dataset": primary_dataset,
        "included_report_keys": [_report_reference_key(entry) for entry in final_reports],
        **required_structure,
    }
    if not story_blocks:
        fallback = build_dev_final_synthesis_preview(final_reports, topics)
        return fallback, token_stats, {
            "attempts": [],
            "relaxed_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
            "dev_fallback_used": bool(fallback),
            "synthesis_method": "per_story_parallel",
        }

    results = _run_story_synthesis_blocks(story_blocks, now)
    final_synthesis = _assemble_story_synthesis(results, topics)
    token_stats["estimated_total_input_tokens"] = sum(
        int(result.get("estimated_input_tokens") or 0)
        for result in results
    )
    actual_prompt_tokens = [
        int(result["actual_prompt_tokens"])
        for result in results
        if result.get("actual_prompt_tokens") is not None
    ]
    if actual_prompt_tokens:
        token_stats["actual_prompt_tokens"] = sum(actual_prompt_tokens)
    attempts = [
        {
            "topic": result.get("topic_title"),
            "story": result.get("story_title"),
            "valid": bool(result.get("valid")),
            "reason": result.get("reason"),
            "word_count": result.get("word_count"),
            "preview": result.get("preview"),
        }
        for result in results
    ]
    debug = {
        "attempts": attempts,
        "relaxed_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
        "dev_fallback_used": False,
        "synthesis_method": "per_story_parallel",
    }
    if not final_synthesis and RELAXED_FINAL_SYNTHESIS_GUARDS:
        final_synthesis = build_dev_final_synthesis_preview(final_reports, topics)
        debug["dev_fallback_used"] = bool(final_synthesis)
    return final_synthesis, token_stats, debug


def _choose_report_index_to_trim(reports: list[str]) -> int:
    topic_counts = Counter(_report_topic_label(report) for report in reports)
    for index in range(len(reports) - 1, -1, -1):
        topic_label = _report_topic_label(reports[index])
        if is_low_confidence_report_entry(reports[index]) and topic_counts[topic_label] > 1:
            return index
    for index in range(len(reports) - 1, -1, -1):
        topic_label = _report_topic_label(reports[index])
        if topic_counts[topic_label] > 1:
            return index
    return len(reports) - 1


def _truncate_report_for_input_budget(report: str, max_summary_chars: int) -> str:
    summary_match = re.search(r"Summary:\s*(.*)", report or "", flags=re.DOTALL)
    if not summary_match:
        return (report or "")[:max_summary_chars]
    summary_text = summary_match.group(1).strip()
    truncated_summary = summary_text[:max_summary_chars].rsplit(" ", 1)[0].strip()
    if len(summary_text) > len(truncated_summary):
        truncated_summary = f"{truncated_summary} ..."
    return report[: summary_match.start(1)] + truncated_summary


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


def build_final_synthesis_payload(
    final_reports: List[str],
    now_label: str,
    topics: List[dict],
    custom_prompt_text: str | None = None,
) -> tuple[list[BaseMessage], dict]:
    def compose(reports_for_prompt: list[str]) -> tuple[list[BaseMessage], dict]:
        high_confidence_reports = [
            entry for entry in reports_for_prompt if not is_low_confidence_report_entry(entry)
        ]
        low_confidence_reports = [
            entry for entry in reports_for_prompt if is_low_confidence_report_entry(entry)
        ]
        primary_reports = high_confidence_reports or reports_for_prompt

        primary_dataset = _build_grouped_synthesis_dataset(primary_reports, topics)
        required_structure = _required_synthesis_structure_for_reports(primary_reports, topics)
        supplemental_dataset = "(Omitted to save context window)"

        uses_custom_prompt = custom_prompt_text is not None
        if uses_custom_prompt:
            instruction_text = textwrap.dedent(
                f"""
                You are synthesizing prewritten article summaries about {PROJECT_SUMMARY_SCOPE_LABEL}.
                Use only the supplied dataset.
                Treat PRIMARY_DATASET as the stronger evidence base and LOW_CONFIDENCE_DATASET as sparse-support material.
                Focus on concrete reported claims and avoid discussion of outlet style unless it changes the factual claim.
                When PRIMARY_DATASET is divided into Topic/Story blocks, keep distinct Story blocks in
                distinct paragraphs and do not use singleton or background-only story material as a basis
                for final copy.
                Do not quote, restate, or paraphrase the prompt instructions themselves.
                Do not include meta labels like 'Title:' or 'Content:' in your final answer unless explicitly required by the user.
                Do not write XML/HTML-style topic tags unless explicitly required by the user.
                Write for newsletter recipients. Do not mention the user, the prompt, AI, PRIMARY_DATASET,
                LOW_CONFIDENCE_DATASET, supplied coverage, or source-material limitations in the final copy.

                USER REQUEST:
                {custom_prompt_text}
                """
            ).strip()
            system_prompt_text = textwrap.dedent(
                f"""
                Today: {now_label}.
                {instruction_text}

                PRIMARY_DATASET:
                {primary_dataset}

                LOW_CONFIDENCE_DATASET:
                {supplemental_dataset}
                """
            ).strip()
            user_prompt_text = (
                "Produce the requested final output now, using only the supplied dataset. "
                "Return only the final analysis content."
            )
        else:
            instruction_text = get_default_final_synthesis_instructions(topics)
            system_prompt_text = textwrap.dedent(
                f"""
                Today: {now_label}.
                You are synthesizing prewritten article summaries covering {PROJECT_SUMMARY_SCOPE_LABEL}.
                {instruction_text}

                PRIMARY_DATASET:
                {primary_dataset}

                LOW_CONFIDENCE_DATASET:
                {supplemental_dataset}
                """
            ).strip()
            user_prompt_text = (
                f"Produce the {len(topics)}-section daily news synthesis now using only the supplied dataset. "
                "One section per selected topic, in the order listed in the TASK. "
                "Return markdown ## headings followed by prose paragraphs only."
            )
        prompt_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt_text),
            HumanMessage(content=user_prompt_text),
        ]

        summary_only_tokens = 0
        for entry in reports_for_prompt:
            summary_match = re.search(r"Summary:\s*(.*)", entry, flags=re.DOTALL)
            summary_only_tokens += estimate_token_count(
                summary_match.group(1).strip() if summary_match else entry
            )
        article_entry_payload_tokens = sum(estimate_token_count(entry) for entry in reports_for_prompt)
        prompt_instruction_tokens = estimate_token_count(instruction_text) + estimate_token_count(user_prompt_text)
        estimated_total_input_tokens = sum(estimate_message_token_count(message) for message in prompt_messages)

        stats = {
            "article_summary_tokens_estimate": summary_only_tokens,
            "article_entry_payload_tokens_estimate": article_entry_payload_tokens,
            "prompt_instruction_tokens_estimate": prompt_instruction_tokens,
            "estimated_total_input_tokens": estimated_total_input_tokens,
            "total_reports": len(final_reports),
            "reports_included_in_synthesis": len(reports_for_prompt),
            "reports_omitted_from_synthesis": len(final_reports) - len(reports_for_prompt),
            "high_confidence_reports": len(high_confidence_reports),
            "low_confidence_reports": len(low_confidence_reports),
            "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,
            "model_profile": MODEL_PROFILE_KEY,
            "model": MODEL_REFERENCE,
            "model_name": MODEL_NAME,
            "model_backend": MODEL_BACKEND,
            "uses_custom_prompt": uses_custom_prompt,
            "topic_count": len(topics),
            **required_structure,
            "primary_dataset": primary_dataset,
            "included_report_keys": [
                _report_reference_key(entry) for entry in reports_for_prompt
            ],
        }
        if MODEL_MAX_INPUT_TOKENS > 0:
            stats["estimated_input_utilization_pct"] = round(
                (estimated_total_input_tokens / MODEL_MAX_INPUT_TOKENS) * 100, 1
            )
        return prompt_messages, stats

    working_reports = list(final_reports)
    prompt_messages, stats = compose(working_reports)
    while (
        MODEL_MAX_INPUT_TOKENS > 0
        and stats["estimated_total_input_tokens"] > MODEL_MAX_INPUT_TOKENS
        and len(working_reports) > 1
    ):
        working_reports.pop(_choose_report_index_to_trim(working_reports))
        prompt_messages, stats = compose(working_reports)

    emergency_summary_chars = max(250, MODEL_MAX_INPUT_TOKENS * 2)
    while (
        MODEL_MAX_INPUT_TOKENS > 0
        and stats["estimated_total_input_tokens"] > MODEL_MAX_INPUT_TOKENS
        and working_reports
        and emergency_summary_chars >= 250
    ):
        working_reports = [
            _truncate_report_for_input_budget(working_reports[0], emergency_summary_chars)
        ]
        prompt_messages, stats = compose(working_reports)
        emergency_summary_chars //= 2

    stats["input_budget_enforced"] = MODEL_MAX_INPUT_TOKENS > 0
    stats["input_budget_satisfied"] = (
        MODEL_MAX_INPUT_TOKENS <= 0
        or stats["estimated_total_input_tokens"] <= MODEL_MAX_INPUT_TOKENS
    )
    return prompt_messages, stats


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

# --- NODES ---

def build_article_summary_prompt_messages(current_article: dict, now_label: str) -> list[BaseMessage]:
    source_name = current_article.get("source", "Unknown source")
    source_config = SOURCE_FEEDS.get(source_name)
    display_name = (
        source_config.get("name", source_name)
        if isinstance(source_config, dict)
        else source_name
    )
    topic_title = current_article.get("topic_title")
    target = _build_article_heading(current_article)
    topic_format_line = f"\n- Topic: {topic_title}" if topic_title else ""
    system_prompt = SystemMessage(content=textwrap.dedent(f"""
        Today: {now_label}.
        Current Task: Summarize one preselected article from the last {RECENT_WINDOW_HOURS} hours
        covering one of today's selected news topics.
        1. Use only the provided article metadata, URL, description, and article text.
        2. Do not call tools in this step.
        3. Ignore outlet style and focus on concrete reported claims.
        4. Include key facts: what reportedly happened, where, timeline, named actors, casualties or damage if reported, and what remains unconfirmed.
        5. If the article text is thin, summarize only what is actually supported by the provided text and metadata.
        6. Do not recap the general history of a longstanding topic or conflict; include background only
           when the article reports a new fact about it or one short clause is needed for orientation.
        7. Start your response with 'DATABASE_ENTRY:' and then exactly the requested Markdown block.
        8. Do not include any text before 'DATABASE_ENTRY:' or after the summary.
    """).strip())
    story_line = f"Story: {current_article.get('story_title')}\n" if current_article.get("story_title") else ""
    article_payload = (
        "Selected article:\n\n"
        f"Title: {current_article.get('title') or 'N/A'}\n"
        f"Source: {display_name}\n"
        f"Published: {current_article.get('pub_date') or 'Unknown publish time'}\n"
        f"URL: {current_article.get('url') or 'N/A'}\n"
        f"Topic: {topic_title or 'general news topic'}\n"
        f"{story_line}"
        f"Description: {current_article.get('description') or 'N/A'}\n"
        f"Article text:\n{current_article.get('text') or 'N/A'}\n\n"
        "Return exactly this block, replacing only the summary text:\n\n"
        "DATABASE_ENTRY:\n"
        f"### {target}\n"
        "Metadata:\n"
        f"- Source: {display_name}\n"
        f"- Published: {current_article.get('pub_date') or 'Unknown publish time'}\n"
        f"- URL: {current_article.get('url') or 'N/A'}{topic_format_line}\n\n"
        "Summary:\n"
        "<4-7 sentence article summary in plain prose, no brackets>"
    )
    return [system_prompt, HumanMessage(content=article_payload)]


def call_model(state: AgentState):
    now = datetime.now().strftime("%B %d, %Y")

    current_article = state["articles_remaining"][0] if state["articles_remaining"] else None
    target = _build_article_heading(current_article) if current_article else "Final Synthesis"

    if not state["articles_remaining"]:
        llm = build_chat_model(
            max_tokens=FINAL_SYNTHESIS_MAX_TOKENS,
            task="final_synthesis",
        )
        if not state.get("generate_final_synthesis", True):
            return {"messages": [AIMessage(content="ARTICLE_SUMMARIES_COMPLETE")], "empty_response_count": 0}

        prompt_messages, token_stats = build_final_synthesis_payload(
            state["final_reports"],
            now,
            state.get("topics") or [],
            custom_prompt_text=state.get("final_prompt_text"),
        )
    else:
        llm = build_chat_model(
            max_tokens=ARTICLE_SUMMARY_MAX_TOKENS,
            task="article_summary",
        )
        prompt_messages = build_article_summary_prompt_messages(current_article, now)

    if state["articles_remaining"]:
        fallback_content = build_article_fallback_entry(current_article)
    else:
        if state.get("final_prompt_text") is None:
            topics = state.get("topics") or []
            if topics:
                fallback_sections = []
                for topic in topics:
                    fallback_sections.append(
                        f"## {_format_topic_section_header(topic['title'])}\n"
                        "Final synthesis unavailable because the model connection failed repeatedly."
                    )
                fallback_content = "\n\n".join(fallback_sections)
            else:
                fallback_content = (
                    "## DAILY NEWS SUMMARY\n"
                    "Final synthesis unavailable because the model connection failed repeatedly."
                )
        else:
            fallback_content = (
                "Final synthesis unavailable because the model connection failed repeatedly "
                "before the custom final prompt could be completed."
            )

    response = invoke_with_retries(
        llm,
        prompt_messages,
        task_name=f"analysis for {target}",
        fallback_content=fallback_content,
    )

    token_stats_update = {}
    if not state["articles_remaining"] and state.get("generate_final_synthesis", True):
        prompt_tokens = extract_prompt_tokens_from_response(response)
        if prompt_tokens is not None:
            token_stats["actual_prompt_tokens"] = prompt_tokens
        token_stats_update["final_synthesis_token_stats"] = token_stats

    is_valid = False
    if hasattr(response, "tool_calls") and response.tool_calls:
        is_valid = True
    elif has_structured_entry(response.content, target):
        is_valid = True
    elif not state["articles_remaining"] and is_valid_final_synthesis_response(
        response.content,
        state.get("topics") or [],
        uses_custom_prompt=state.get("final_prompt_text") is not None,
        relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
        validation=token_stats,
    ):
        is_valid = True

    error_count = state.get("empty_response_count", 0)
    if not is_valid:
        error_count += 1
    else:
        error_count = 0

    return {
        "messages": [response],
        "empty_response_count": error_count,
        **token_stats_update,
    }

def call_tool(state: AgentState):
    last_message = state['messages'][-1]
    tool_results = []
    for tool_call in last_message.tool_calls:
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {"input": str(args)}

        if tool_name == "internet_search":
            query = args.get("query") or args.get("input") or args.get("__arg1")
            if not query:
                res = "Tool call error: internet_search missing required argument 'query'."
            else:
                res = internet_search(str(query))
        elif tool_name == "web_scrape":
            url = args.get("url") or args.get("link") or args.get("input") or args.get("__arg1")
            if not url:
                res = "Tool call error: web_scrape missing required argument 'url'."
            else:
                res = web_scrape(str(url))
        else:
            res = f"Tool call error: unsupported tool '{tool_name}'."
        tool_results.append(ToolMessage(tool_call_id=tool_call["id"], content=res))
    return {"messages": tool_results, "empty_response_count": 0}


def recover_from_empty_response(state: AgentState):
    error_count = state.get("empty_response_count", 0)

    if error_count >= 3:
        if state["articles_remaining"]:
            article = state["articles_remaining"][0]
            fallback_summary = build_article_fallback_entry(article).split("DATABASE_ENTRY:\n", 1)[1]
            wipe_messages = [RemoveMessage(id=m.id) for m in state['messages']]
            progress_tracker.article_completed()
            return {
                "messages": wipe_messages + [HumanMessage(content="Proceed to next target.")],
                "final_reports": state['final_reports'] + [fallback_summary],
                "articles_remaining": state["articles_remaining"][1:],
                "empty_response_count": 0
            }

        return {"empty_response_count": 0}

    if not state["articles_remaining"]:
        validation = state.get("final_synthesis_token_stats") or {}
        required_headings = [
            str(heading)
            for heading in validation.get("required_topic_headings", [])
            if str(heading).strip()
        ]
        rejection_reason = describe_final_synthesis_rejection(
            state["messages"][-1].content if state.get("messages") else "",
            state.get("topics") or [],
            uses_custom_prompt=state.get("final_prompt_text") is not None,
            relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
            validation=validation,
        )
        heading_instruction = ""
        if required_headings:
            heading_instruction = (
                " Include every required heading exactly once, even if the prose must be shorter:\n"
                + "\n".join(f"## {heading}" for heading in required_headings)
                + "\n"
            )
        return {
            "messages": [HumanMessage(content=(
                f"The last response did not match the requested newsletter format ({rejection_reason}). "
                "Provide the requested final synthesis now using only the supplied dataset. "
                f"{heading_instruction}"
                "Use markdown ## section headings, write one cohesive prose paragraph per story block, "
                "shorten paragraphs if needed to preserve the required structure, "
                "and return no tags, bullets, "
                "preamble, methodology, or source-material labels."
            ))]
        }

    return {
        "messages": [HumanMessage(content=(
            "Format Error: respond with exactly one article block only. "
            "Use 'DATABASE_ENTRY:' followed by '### article title', then 'Metadata:' with Source/Published/URL bullets "
            "(plus Topic when provided), then 'Summary:'. "
            "Do not add commentary, correction text, code fences, or trailing notes."
        ))]
    }

def database_save_and_clear(state: AgentState):
    last_message = state['messages'][-1]

    if state["articles_remaining"]:
        current_article = state["articles_remaining"][0]
        heading_name = _build_article_heading(current_article)
    else:
        current_article = None
        heading_name = ""

    if current_article and has_structured_entry(last_message.content, heading_name):
        summary = normalize_report_entry(current_article, last_message.content)
        progress_tracker.article_completed()

        wipe_messages = [RemoveMessage(id=m.id) for m in state['messages']]
        remaining_articles = state["articles_remaining"][1:]
        next_prompt = [HumanMessage(content="Proceed to next target.")] if remaining_articles else []

        return {
            "messages": wipe_messages + next_prompt,
            "final_reports": state['final_reports'] + [summary],
            "articles_remaining": remaining_articles,
            "empty_response_count": 0
        }
    return {}

# --- LOGIC ---

def should_continue(state: AgentState):
    last_message = state['messages'][-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    if state["articles_remaining"] and has_structured_entry(
        last_message.content,
        _build_article_heading(state["articles_remaining"][0]),
    ):
        return "database"
    if not state["articles_remaining"]:
        if not state.get("generate_final_synthesis", True):
            return END
        if is_valid_final_synthesis_response(
            last_message.content,
            state.get("topics") or [],
            uses_custom_prompt=state.get("final_prompt_text") is not None,
            relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
            validation=state.get("final_synthesis_token_stats") or {},
        ):
            return END
        if state.get("empty_response_count", 0) >= 3:
            return END
        return "recover"

    return "recover"

# --- BUILD ---

workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", call_tool)
workflow.add_node("database", database_save_and_clear)
workflow.add_node("recover", recover_from_empty_response)

workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")
workflow.add_edge("database", "agent")
workflow.add_edge("recover", "agent")

app = workflow.compile()

# --- RUN HELPERS ---

def run_article_summary_pass(article_targets: List[dict], topics: List[dict]) -> List[str]:
    if ARTICLE_SUMMARY_CONCURRENCY > 1 and len(article_targets) > 1:
        ordered_results: list[tuple[int, List[str]]] = []
        with ThreadPoolExecutor(max_workers=ARTICLE_SUMMARY_CONCURRENCY) as executor:
            future_map = {
                executor.submit(run_article_summary_pass, [article], topics): index
                for index, article in enumerate(article_targets)
            }
            for future in as_completed(future_map):
                ordered_results.append((future_map[future], future.result()))
        final_reports: List[str] = []
        for _, reports in sorted(ordered_results, key=lambda item: item[0]):
            final_reports.extend(reports)
        return final_reports

    inputs: AgentState = {
        "messages": [HumanMessage(content="Initialize research protocol.")],
        "final_reports": [],
        "articles_remaining": article_targets,
        "empty_response_count": 0,
        "final_synthesis_token_stats": {},
        "generate_final_synthesis": False,
        "final_prompt_text": None,
        "topics": topics,
    }

    recursion_limit = max(80, (len(article_targets) * 12) + 20)
    final_reports: List[str] = []
    for output in app.stream(inputs, stream_mode="values", config={"recursion_limit": recursion_limit}):
        if "final_reports" in output:
            final_reports = output["final_reports"]

    return final_reports


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
        for summaries in story_map.values():
            if explicit_story_mode and len(summaries) < MIN_ARTICLES_PER_STORY:
                continue
            paragraph = _dev_synthesis_paragraph_from_summaries(summaries)
            if paragraph:
                paragraphs.append(paragraph)
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


def run_final_synthesis_pass(
    final_reports: List[str],
    topics: List[dict],
    final_prompt_text: str | None,
) -> tuple[str, dict, dict]:
    if final_prompt_text is None:
        return run_story_synthesis_pass(final_reports, topics)

    inputs: AgentState = {
        "messages": [HumanMessage(content="Produce the final synthesis.")],
        "final_reports": final_reports,
        "articles_remaining": [],
        "empty_response_count": 0,
        "final_synthesis_token_stats": {},
        "generate_final_synthesis": True,
        "final_prompt_text": final_prompt_text,
        "topics": topics,
    }

    final_synthesis = ""
    token_stats: dict = {}
    attempts: list[dict[str, Any]] = []
    seen_ai_messages: set[str] = set()
    for output in app.stream(inputs, stream_mode="values", config={"recursion_limit": 50}):
        if "final_synthesis_token_stats" in output:
            token_stats = output["final_synthesis_token_stats"]
        if "messages" in output and output["messages"]:
            msg = output["messages"][-1]
            if isinstance(msg, AIMessage):
                msg_id = str(getattr(msg, "id", "") or f"{len(attempts)}:{hash(msg.content)}")
                if msg_id not in seen_ai_messages:
                    seen_ai_messages.add(msg_id)
                    clean_content = _strip_prompt_echo_lines(strip_model_artifacts(msg.content or ""))
                    rejection_reason = describe_final_synthesis_rejection(
                        msg.content,
                        topics,
                        uses_custom_prompt=final_prompt_text is not None,
                        relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
                        validation=token_stats,
                    )
                    attempts.append(
                        {
                            "valid": not rejection_reason,
                            "reason": rejection_reason or "accepted",
                            "word_count": _final_synthesis_word_count(clean_content),
                            "heading_count": _final_synthesis_heading_count(clean_content),
                            "preview": clean_content[:500],
                        }
                    )
                if is_valid_final_synthesis_response(
                    msg.content,
                    topics,
                    uses_custom_prompt=final_prompt_text is not None,
                    relaxed=RELAXED_FINAL_SYNTHESIS_GUARDS,
                    validation=token_stats,
                ):
                    final_synthesis = strip_model_artifacts(msg.content)

    debug = {
        "attempts": attempts,
        "relaxed_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
        "dev_fallback_used": False,
    }
    if not final_synthesis and RELAXED_FINAL_SYNTHESIS_GUARDS:
        final_synthesis = build_dev_final_synthesis_preview(final_reports, topics)
        debug["dev_fallback_used"] = bool(final_synthesis)

    return final_synthesis, token_stats, debug


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
        heading_match = re.match(r"^##+\s+(.+)$", stripped)
        if heading_match:
            heading = heading_match.group(1).strip()
            if formatted_lines and formatted_lines[-1] != "":
                formatted_lines.append("")
            formatted_lines.append(heading)
            formatted_lines.append("-" * len(heading))
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
                    headline_label = headline
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
                    headline_label = headline
                    lines.append(f"- {headline_label} ({link})" if link else f"- {headline_label}")
                grouped_sections.append("\n".join(lines))
    else:
        for display_name, source_entries in grouped_headlines.items():
            first_entry = source_entries[0] if source_entries else None
            homepage_url = first_entry[2] if first_entry else None
            lines = [f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:", ""]
            for headline, link, _, _story_title in source_entries:
                headline_label = headline
                lines.append(f"- {headline_label} ({link})" if link else f"- {headline_label}")
            grouped_sections.append("\n".join(lines))

    return "\n\n".join(grouped_sections) if grouped_sections else "No article headlines available."


def _render_html_paragraphs(block_text: str) -> str:
    paragraphs = [segment.strip() for segment in re.split(r"\n\s*\n", block_text.strip()) if segment.strip()]
    return "".join(
        f"<p class=\"email-paragraph\" style=\"margin:0 0 18px; font-size:16px; line-height:1.7; color:#1f2937;\">"
        f"{html.escape(paragraph).replace(chr(10), '<br>')}"
        f"</p>"
        for paragraph in paragraphs
    )


def _build_html_synthesis(synthesis_body: str) -> str:
    cleaned = _strip_inline_markdown(
        _strip_prompt_echo_lines(strip_model_artifacts(synthesis_body))
    ).replace("\r\n", "\n")
    blocks: list[str] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def flush_section() -> None:
        nonlocal current_heading, current_lines
        if current_heading is None and not current_lines:
            return
        if current_heading is not None:
            blocks.append(
                f"<h2 style=\"margin:32px 0 12px; font-size:22px; line-height:1.3; "
                f"font-weight:700; color:#111827; letter-spacing:0.01em; text-transform:uppercase;\">"
                f"{html.escape(current_heading)}</h2>"
            )
        section_text = "\n".join(current_lines).strip()
        if section_text:
            blocks.append(_render_html_paragraphs(section_text))
        current_heading = None
        current_lines = []

    for raw_line in cleaned.splitlines():
        stripped = raw_line.strip()
        heading_match = re.match(r"^##+\s+(.+)$", stripped)
        if heading_match:
            flush_section()
            current_heading = heading_match.group(1).strip()
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
) -> str:
    cleaned_synthesis_body = _format_plain_text_synthesis(synthesis_body)
    article_listing = _build_plain_text_article_listing(final_reports, topics)
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

    return (
        f"{report_title}\n"
        f"{'=' * len(report_title)}\n\n"
        f"{image_section}"
        f"{cleaned_synthesis_body}\n\n"
        "ARTICLES BY SOURCE\n"
        "==================\n\n"
        f"{article_listing}\n"
    )


def build_report_html(
    recipient_email: str,
    recipient_name: str,
    report_title: str,
    synthesis_body: str,
    final_reports: List[str],
    topics: List[dict],
    image_art: dict[str, Any] | None = None,
) -> str:
    first_name = _extract_first_name(recipient_name)
    synthesis_html = _build_html_synthesis(synthesis_body)
    article_listing_html = _build_html_article_listing(final_reports, topics)
    unsubscribe_url = build_unsubscribe_url(recipient_email)
    image_html = ""
    if image_art and image_art.get("content_id"):
        image_alt = image_art.get("overlay_headline") or report_title
        image_html = (
            "<img "
            f"alt=\"{html.escape(str(image_alt), quote=True)}\" "
            f"src=\"cid:{html.escape(str(image_art['content_id']), quote=True)}\" "
            "style=\"display:block; width:100%; height:auto; margin:0 0 30px; border-radius:6px;\">"
        )
    elif image_art and image_art.get("data_uri"):
        image_alt = image_art.get("overlay_headline") or report_title
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
        ".email-title{font-size:28px !important; line-height:1.18 !important;}"
        ".email-paragraph{font-size:16px !important; line-height:1.65 !important;}"
        "}"
        "</style>"
        "</head><body style=\"margin:0; padding:0; background-color:#f3f4f6;\">"
        "<div class=\"email-shell\" style=\"margin:0; padding:20px 0; width:100%;\">"
        "<div class=\"email-card\" style=\"width:100%; max-width:1040px; margin:0 auto; background:#ffffff; border-radius:8px; overflow:hidden; box-sizing:border-box;\">"
        "<div class=\"email-content\" style=\"padding:36px 32px 24px; box-sizing:border-box;\">"
        f"<p style=\"margin:0 0 24px; font-size:18px; line-height:1.6; color:#111827;\">{html.escape(first_name)},</p>"
        "<p style=\"margin:0 0 28px; font-size:17px; line-height:1.7; color:#374151;\">Here is your daily news summary.</p>"
        f"<h1 class=\"email-title\" style=\"margin:0 0 28px; font-size:32px; line-height:1.2; font-weight:800; color:#111827;\">{html.escape(report_title)}</h1>"
        f"{image_html}"
        f"{synthesis_html}"
        "<hr style=\"border:none; border-top:1px solid #e5e7eb; margin:36px 0 28px;\">"
        "<h2 style=\"margin:0 0 18px; font-size:22px; line-height:1.3; font-weight:800; color:#111827; letter-spacing:0.01em;\">Sources</h2>"
        f"{article_listing_html}"
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
    topic_match = re.search(r"^- Topic:\s*(.+)$", entry or "", flags=re.MULTILINE)
    story_match = re.search(r"^- Story:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return {
        "index": index,
        "title": title_match.group(1).strip() if title_match else "",
        "source": source_match.group(1).strip() if source_match else "",
        "published": published_match.group(1).strip() if published_match else "",
        "url": url_match.group(1).strip() if url_match else "",
        "topic": topic_match.group(1).strip() if topic_match else "",
        "story": story_match.group(1).strip() if story_match else "",
        "summary": _report_summary_text(entry),
        "raw_entry": entry,
    }


def _persist_article_summaries_debug(final_reports: List[str]) -> str | None:
    if not final_reports:
        return None
    debug_path = os.path.join(RUN_OUTPUT_DIR, f"article_summaries_{timestamp}.json")
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
            "bradley_only_delivery": BRADLEY_ONLY_DELIVERY,
            "shared_url_history_enabled": SHARED_URL_HISTORY_ENABLED,
            "relaxed_final_synthesis_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
            "source_count": source_count,
            "sources_path": str(CONFIG.sources_path),
            "topic_mode": TOPIC_MODE,
            "client_path": str(CONFIG.client_path),
            "topics_path": str(CONFIG.topics_path),
            "active_topic_ids": [],
            "top_funnel_provider_count": len(TOP_FUNNEL_PROVIDERS),
            "recipients_path": str(CONFIG.recipients_path),
            "output_dir": RUN_OUTPUT_DIR,
            "run_used_urls_path": RUN_USED_URLS_PATH,
            "run_log_path": RUN_LOG_PATH,
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
            "topic_relevance_min_score": TOPIC_RELEVANCE_MIN_SCORE,
            "per_source_topic_article_cap": PER_SOURCE_TOPIC_ARTICLE_CAP,
            "dev_source_limit": DEV_SOURCE_LIMIT,
            "dev_num_topics": DEV_NUM_TOPICS,
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
            "max_articles_per_story": MAX_ARTICLES_PER_STORY,
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


def preflight_model_server() -> dict[str, Any]:
    models_url = f"{MODEL_BASE_URL.rstrip('/')}/models"
    result: dict[str, Any] = {
        "base_url": MODEL_BASE_URL,
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
            expected = {MODEL_NAME, MODEL_REFERENCE, "default_model"}
            result["model_match"] = any(model in expected for model in served_models)
    except Exception as error:
        result["error"] = str(error)
    return result


def probe_model_generation(timeout_seconds: int = MODEL_LOAD_PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    completions_url = f"{MODEL_BASE_URL.rstrip('/')}/chat/completions"
    result: dict[str, Any] = {
        "base_url": MODEL_BASE_URL,
        "completions_url": completions_url,
        "ok": False,
    }
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
    try:
        response = requests.post(completions_url, json=payload, timeout=timeout_seconds)
        result["status_code"] = response.status_code
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            result["content_preview"] = str(content or "")[:80]
        result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
    return result


def _managed_model_server_log_path() -> str:
    return os.path.join(RUN_OUTPUT_DIR, "model_server.log")


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


def _stop_managed_model_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        progress_tracker.detail(f"Managed model server already exited with code {process.returncode}.")
        return

    progress_tracker.detail("Stopping managed model server.")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        progress_tracker.detail("Managed model server did not stop gracefully; killing it.")
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
    ensure_codex_safe_model_reference(MODEL_REFERENCE)
    progress_tracker.step("model", "Checking model server.")
    record_activity_snapshot("before_model_server_preflight")
    existing_preflight = preflight_model_server()
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
        try:
            yield
        finally:
            record_activity_snapshot("after_existing_model_server_run", ACTIVE_RUN_DIAGNOSTICS)
            if ACTIVE_RUN_DIAGNOSTICS is not None:
                ACTIVE_RUN_DIAGNOSTICS.write(CONFIG.run_output_dir, timestamp)
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
        yield
    finally:
        _stop_managed_model_server(process)
        record_activity_snapshot("after_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
        if ACTIVE_RUN_DIAGNOSTICS is not None:
            ACTIVE_RUN_DIAGNOSTICS.write(CONFIG.run_output_dir, timestamp)
        log_file.close()


def _run_pipeline() -> None:
    global ACTIVE_RUN_DIAGNOSTICS
    all_sources = list(SOURCE_FEEDS.keys())

    # In dev mode, cap the source list and topic count to the minimum needed to
    # exercise every code path (multi-source sweep, topic matching, article budget,
    # synthesis, image, email) without a full production-width run.
    if DEV:
        sources = all_sources[:DEV_SOURCE_LIMIT]
        effective_num_topics = DEV_NUM_TOPICS
        effective_total_article_summary_cap = (
            min(TOTAL_ARTICLE_SUMMARY_CAP, effective_num_topics * 2)
            if TOTAL_ARTICLE_SUMMARY_CAP > 0
            else effective_num_topics * 2
        )
        effective_per_topic_article_summary_cap = (
            min(PER_TOPIC_ARTICLE_SUMMARY_CAP, 2)
            if PER_TOPIC_ARTICLE_SUMMARY_CAP > 0
            else 2
        )
    else:
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
        f"Model caps: input {MODEL_MAX_INPUT_TOKENS} tokens, "
        f"article text {ARTICLE_TEXT_TOKEN_LIMIT} tokens, "
        f"summaries {TOTAL_ARTICLE_SUMMARY_CAP} total/{PER_TOPIC_ARTICLE_SUMMARY_CAP} per topic, "
        f"{MAX_STORIES_PER_TOPIC} stories/topic, {MAX_ARTICLES_PER_STORY} articles/story, "
        f"{PER_SOURCE_TOPIC_ARTICLE_CAP} per source/story budget cap, "
        f"summary concurrency {ARTICLE_SUMMARY_CONCURRENCY}."
    )
    progress_tracker.detail(f"Run mode: {RUN_MODE}")
    preflight = preflight_model_server()
    diagnostics.event("model_server_preflight", **preflight)
    if not preflight.get("ok"):
        progress_tracker.warning(
            f"model server preflight failed at {preflight.get('models_url')}: "
            f"{preflight.get('error') or 'unknown error'}"
        )
    elif not preflight.get("model_match"):
        progress_tracker.warning(
            "model server is reachable but did not report the expected model. "
            f"Served: {preflight.get('served_models')}"
        )
    progress_tracker.detail(f"Source pool: {len(sources)} of {len(all_sources)} configured feed(s).")
    if DEV:
        topic_limit_label = f"first {effective_num_topics} predefined topic(s)"
        progress_tracker.detail(
            f"DEV mode active. Source pool limited to {DEV_SOURCE_LIMIT} source(s), "
            f"topics limited to {topic_limit_label} (full pool: {len(all_sources)} sources), "
            f"article summaries capped at "
            f"{effective_total_article_summary_cap} total/{effective_per_topic_article_summary_cap} per topic. "
            f"Sending to one recipient only "
            f"(always {BRADLEY_ONLY_RECIPIENT}) without recording URLs into the shared history."
        )
    elif LOCAL_PROD:
        history_label = (
            "shared production URL history"
            if SHARED_URL_HISTORY_ENABLED
            else "isolated URL history"
        )
        progress_tracker.detail(
            f"LOCAL-PROD mode active. Using the full production source/topic scope "
            f"with {history_label}, but sending only to {BRADLEY_ONLY_RECIPIENT}."
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
    run_seen_urls: set[tuple[str, str]] = set()
    article_targets_for_budget: List[dict] = []
    final_reports: List[str] = []
    matched_feed_item_count = 0
    fresh_article_count = 0
    source_rejection_counts: Counter[str] = Counter()

    # 3) Iterate union of sources, collect articles per topic.
    for source_index, source_name in enumerate(sources, start=1):
        progress_tracker.start_source(source_index)
        article_targets, new_urls, source_run = gather_article_targets_for_source(
            source_name, topics, seen_urls, run_seen_urls
        )
        diagnostics.record_source_run(source_run)
        matched_feed_item_count += int(source_run.get("selected_item_count") or 0)
        fresh_article_count += int(source_run.get("fresh_article_count") or 0)
        source_rejection_counts.update(source_run.get("rejected_counts") or {})
        progress_tracker.set_source_article_total(0)
        if new_urls:
            _record_run_urls(new_urls)
        if article_targets:
            article_targets_for_budget.extend(article_targets)
        progress_tracker.source_completed()

    if matched_feed_item_count or any(source_rejection_counts.values()):
        progress_tracker.detail(
            f"Source funnel: {matched_feed_item_count} matched feed item(s), "
            f"{fresh_article_count} fresh article target(s) after dedupe/history "
            f"(history={source_rejection_counts.get('seen_in_history', 0)}, "
            f"duplicate_this_run={source_rejection_counts.get('duplicate_this_run', 0)}, "
            f"missing_url={source_rejection_counts.get('missing_url', 0)})."
        )

    progress_tracker.step("stories", "Organizing article candidates.")
    article_targets_for_budget, story_cluster_stats = organize_article_targets_into_stories(
        article_targets_for_budget,
        topics,
        min_articles_per_story=MIN_ARTICLES_PER_STORY,
    )
    diagnostics.event("story_clustering", **story_cluster_stats)
    progress_tracker.detail(
        f"Story clustering: {story_cluster_stats.get('included_count', 0)} "
        f"article target(s) retained across {story_cluster_stats.get('story_count', 0)} "
        f"story group(s); {story_cluster_stats.get('dropped_count', 0)} dropped below "
        f"the {MIN_ARTICLES_PER_STORY}-article story floor/caps "
        f"(TF-IDF threshold {STORY_CLUSTER_SIMILARITY_THRESHOLD:.2f})."
    )
    story_coverage_deficits = {
        topic_title: MAX_STORIES_PER_TOPIC - len(stories)
        for topic_title, stories in (story_cluster_stats.get("stories_by_topic") or {}).items()
        if len(stories) < MAX_STORIES_PER_TOPIC
    }
    for topic in topics:
        topic_title = str(topic.get("title") or topic.get("key") or "Unknown topic")
        if topic_title not in (story_cluster_stats.get("stories_by_topic") or {}):
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

    article_targets, article_budget_stats = budget_article_targets(
        article_targets_for_budget,
        topics,
        total_cap=effective_total_article_summary_cap,
        per_topic_cap=effective_per_topic_article_summary_cap,
        per_source_topic_cap=PER_SOURCE_TOPIC_ARTICLE_CAP,
    )
    diagnostics.record_article_budget(article_budget_stats)
    progress_tracker.detail(
        f"Article budget: {article_budget_stats.get('included_count', 0)} selected, "
        f"{article_budget_stats.get('dropped_count', 0)} candidate target(s) not summarized "
        f"(cap {effective_total_article_summary_cap} total/"
        f"{effective_per_topic_article_summary_cap} per topic/"
        f"{PER_SOURCE_TOPIC_ARTICLE_CAP} per source-story)."
    )
    article_targets, post_budget_story_stats = filter_budgeted_targets_by_story_floor(
        article_targets,
        min_articles_per_story=MIN_ARTICLES_PER_STORY,
    )
    if post_budget_story_stats.get("dropped_count", 0):
        diagnostics.event("post_budget_story_floor", **post_budget_story_stats)
        progress_tracker.detail(
            f"Story floor after budget: dropped "
            f"{post_budget_story_stats.get('dropped_count', 0)} target(s) because budget caps "
            f"left their story below {MIN_ARTICLES_PER_STORY} articles."
        )
    progress_tracker.start_article_summary(len(article_targets))
    if article_targets:
        final_reports.extend(run_article_summary_pass(article_targets, topics))

    progress_tracker.set_final_step("reports", 1)
    diagnostics.article_summary_count = len(final_reports)
    article_summaries_path = _persist_article_summaries_debug(final_reports)
    if article_summaries_path:
        diagnostics.record_artifact(
            "final_article_summaries",
            article_summaries_path,
            count=len(final_reports),
        )
    record_activity_snapshot("after_article_summaries", diagnostics)
    progress_tracker.detail(f"Saved {len(final_reports)} article summary record(s).")

    recipient_config = get_active_recipient_config(load_recipient_config())
    prompt_groups = build_prompt_groups(recipient_config)

    if not prompt_groups:
        progress_tracker.step("finalize", "No recipients configured; stopping after summaries.")
        diagnostics.event("completed_without_recipients")
        _write_run_diagnostics(diagnostics)
        return

    for group_index, group in enumerate(prompt_groups, start=1):
        recipient_list = group["recipient_emails"]
        prompt_label = "default prompt" if group["uses_default_prompt"] else "custom prompt"
        progress_tracker.step(
            "report",
            f"Building report {group_index}/{len(prompt_groups)}.",
            log_detail=f"Building {prompt_label} report for: {', '.join(recipient_list)}",
        )

        report_path = build_report_path(group)
        progress_tracker.set_final_step("synthesis", 2)
        final_synthesis, token_stats, synthesis_debug = run_final_synthesis_pass(
            final_reports,
            topics,
            group["custom_prompt_text"],
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
            continue
        if synthesis_debug.get("dev_fallback_used"):
            diagnostics.event(
                "dev_final_synthesis_fallback_used",
                recipients=recipient_list,
                attempts=synthesis_debug.get("attempts") or [],
            )

        report_title = strip_model_artifacts(generate_report_title(synthesis_body, timestamp))

        progress_tracker.set_final_step("art", 3)
        record_activity_snapshot("before_image_generation", diagnostics)
        image_art = generate_report_image_art(
            report_path=report_path,
            synthesis_body=synthesis_body,
            report_title=report_title,
        )
        record_activity_snapshot("after_image_generation", diagnostics)

        progress_tracker.set_final_step("render", 4)
        reference_reports = filter_reports_for_references(final_reports, token_stats)
        report_body = build_report_body(report_title, synthesis_body, reference_reports, topics, image_art)
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
            group["recipient_names"],
            image_art,
        )

        if token_stats:
            progress_tracker.detail(f"Final synthesis token stats for {', '.join(recipient_list)}: {token_stats}")
        progress_tracker.detail(f"Finished report. Saved text report: {report_path}")

    diagnostics.event("completed")
    _write_run_diagnostics(diagnostics)
    sync_assistant_context_latest_output(CONFIG)
