"""Daily news pipeline implementation.

Common usage:
    uv run todays_news.py

Development vs. real sends:
    NEWS_DEV=1 uv run todays_news.py
        Default. Sends only to NEWS_DEV_RECIPIENT, writes dev_used_urls.txt,
        and does not add URLs to the long-lived seen_urls.txt history.

    NEWS_RUN_MODE=local-prod uv run todays_news.py
        Production-width run with production URL history, but delivery is
        limited to NEWS_DEV_RECIPIENT for review and manual forwarding.

    NEWS_DEV=0 uv run todays_news.py
        Production run. Uses the configured recipient list, writes used_urls.txt,
        and records seen URLs globally so future runs avoid them.

Model selection:
    NEWS_MODEL=gemma-26b-moe uv run todays_news.py
    NEWS_MODEL=qwen-9b-dense uv run todays_news.py

    NEWS_MODEL accepts either a friendly alias above or a full model repo/name.
    NEWS_MODEL_NAME is still honored as a lower-priority legacy override, and
    NEWS_DEFAULT_MODEL changes the fallback when neither is set.

Local model server:
    NEWS_MODEL=qwen-9b-dense uv run todays_news.py
        Starts the matching local mlx_lm.server automatically, waits until it
        is ready, runs the pipeline, then shuts the managed server down even if
        the run errors. Server logs are written beside the report output.

    NEWS_MODEL=qwen-9b-dense uv run todays_news.py --model-server-command
        Prints the matching mlx_lm.server command for the selected model and
        inferred runtime profile without starting the pipeline.

    NEWS_MODEL_BASE_URL=http://127.0.0.1:8080/v1 uv run todays_news.py
        Points the pipeline at a different OpenAI-compatible local endpoint.

Other useful switches:
    NEWS_IMAGE_ENABLED=0 uv run todays_news.py
        Skips report image generation.

    uv run todays_news.py --serve-unsubscribe
        Runs the local unsubscribe endpoint instead of generating a report.

This module owns orchestration and the heavier pipeline logic. Configuration
lives in ``config/*.yaml``; diagnostic run details are written beside each
report under ``output/daily_outputs/<date>/``.
"""

import json
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
import shutil
import subprocess
import tempfile
import trafilatura
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Annotated, Any, TypedDict, List
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
from urllib.parse import parse_qs, urlencode, urlparse
import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .config import (
    ModelSamplingSettings,
    load_recipients,
    load_runtime_config,
    load_sources,
    load_top_funnel_providers,
    update_recipient_pause_setting,
)
from .diagnostics import RunDiagnostics

try:
    import tiktoken
except ImportError:
    tiktoken = None

MODEL_RETRY_ATTEMPTS = 4
MODEL_RETRY_BASE_DELAY_SECONDS = 2
CONFIG = load_runtime_config()
MODEL_NAME = CONFIG.model_name
MODEL_REFERENCE = CONFIG.model_reference
MODEL_PROFILE = CONFIG.model_profile
MODEL_PROFILE_KEY = MODEL_PROFILE.key
MODEL_BASE_URL = CONFIG.model_base_url
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
LEGACY_SEEN_URLS_PATH = str(CONFIG.legacy_seen_urls_path)
RUN_MODE = CONFIG.run_mode
DEV = CONFIG.dev
LOCAL_PROD = CONFIG.local_prod
BRADLEY_ONLY_DELIVERY = CONFIG.bradley_only_delivery
RELAXED_FINAL_SYNTHESIS_GUARDS = CONFIG.relaxed_final_synthesis_guards
RUN_USED_URLS_PATH = str(CONFIG.run_used_urls_path)
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


def _read_url_file(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def _load_seen_urls() -> set[str]:
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
    if not DEV:
        _append_unique_urls(LEGACY_SEEN_URLS_PATH, urls)


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
TOP_FUNNEL_PROVIDERS = load_top_funnel_providers(CONFIG.sources_path)

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
    "about",
    "after",
    "against",
    "amid",
    "among",
    "and",
    "are",
    "around",
    "auto-generated",
    "auto",
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
    "its",
    "being",
    "just",
    "may",
    "new",
    "not",
    "now",
    "off",
    "one",
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
    "topic",
    "under",
    "was",
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


class ProgressTracker:
    SOURCE_WEIGHT = 0.6
    ARTICLE_WEIGHT = 0.3
    FINAL_WEIGHT = 0.1

    def __init__(self) -> None:
        self.total_sources = 0
        self.current_source_index = 0
        self.completed_sources = 0
        self.current_source_total_articles = 0
        self.current_source_completed_articles = 0
        self.article_summary_started = False
        self.total_summary_articles = 0
        self.completed_summary_articles = 0
        self.final_step = 0
        self.final_step_total = 5
        self.last_render = ""
        self._line_active = False

    def reset(self, *, total_sources: int) -> None:
        self.total_sources = total_sources
        self.current_source_index = 0
        self.completed_sources = 0
        self.current_source_total_articles = 0
        self.current_source_completed_articles = 0
        self.article_summary_started = False
        self.total_summary_articles = 0
        self.completed_summary_articles = 0
        self.final_step = 0
        self._render()

    def start_source(self, source_index: int) -> None:
        self.current_source_index = source_index
        self.current_source_total_articles = 0
        self.current_source_completed_articles = 0
        self._render()

    def set_source_article_total(self, total_articles: int) -> None:
        self.current_source_total_articles = max(0, total_articles)
        self.current_source_completed_articles = 0
        self._render()

    def article_completed(self) -> None:
        if self.article_summary_started:
            if self.total_summary_articles > 0:
                self.completed_summary_articles = min(
                    self.total_summary_articles,
                    self.completed_summary_articles + 1,
                )
        elif self.current_source_total_articles > 0:
            self.current_source_completed_articles = min(
                self.current_source_total_articles,
                self.current_source_completed_articles + 1,
            )
        self._render()

    def source_completed(self) -> None:
        self.completed_sources = min(self.total_sources, self.completed_sources + 1)
        self.current_source_total_articles = 0
        self.current_source_completed_articles = 0
        self._render()

    def start_article_summary(self, total_articles: int) -> None:
        self.article_summary_started = True
        self.total_summary_articles = max(0, total_articles)
        self.completed_summary_articles = 0
        self.current_source_total_articles = 0
        self.current_source_completed_articles = 0
        self._render()

    def retrying(self, task_name: str, attempt: int, attempts: int, delay: int) -> None:
        self._render()

    def warning(self, label: str) -> None:
        self._render()

    def set_final_step(self, step_name: str, step_index: int) -> None:
        self.final_step = step_index
        self._render()

    def finish(self, label: str) -> None:
        self.final_step = self.final_step_total
        self._render(force=True)
        if self._line_active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_active = False

    def log(self, message: str) -> None:
        if self._line_active:
            sys.stdout.write("\n")
            self._line_active = False
            self.last_render = ""
        print(message)

    def _percent(self) -> int:
        source_progress = self.completed_sources / self.total_sources if self.total_sources else 0.0
        if (
            self.total_sources
            and self.current_source_index > self.completed_sources
            and self.current_source_total_articles > 0
        ):
            source_progress = (
                self.completed_sources
                + (self.current_source_completed_articles / self.current_source_total_articles)
            ) / self.total_sources
        if not self.article_summary_started:
            article_progress = 0.0
        elif self.total_summary_articles == 0:
            article_progress = 1.0
        else:
            article_progress = self.completed_summary_articles / self.total_summary_articles
        final_progress = (
            self.final_step / self.final_step_total if self.final_step_total else 0.0
        )
        overall = (
            (source_progress * self.SOURCE_WEIGHT)
            + (article_progress * self.ARTICLE_WEIGHT)
            + (final_progress * self.FINAL_WEIGHT)
        )
        return max(0, min(100, int(round(overall * 100))))

    def _render(self, *, force: bool = False) -> None:
        percent = self._percent()
        bar_fill = max(0, min(20, round(percent / 5)))
        bar = "#" * bar_fill + "-" * (20 - bar_fill)
        shown_source = self.current_source_index if self.current_source_index else min(self.completed_sources, self.total_sources)
        if self.completed_sources >= self.total_sources and self.total_sources > 0:
            shown_source = self.total_sources
        if self.article_summary_started:
            shown_article_total = self.total_summary_articles
            shown_article_done = min(self.completed_summary_articles, shown_article_total)
            article_label = f"{shown_article_done}/{shown_article_total}"
        elif self.current_source_total_articles > 0:
            shown_article_total = self.current_source_total_articles
            shown_article_done = min(self.current_source_completed_articles, shown_article_total)
            article_label = f"{shown_article_done}/{shown_article_total}"
        else:
            article_label = "-/-"
        line = (
            f"\r[{bar}] {percent:>3}% | source {shown_source}/{self.total_sources} "
            f"| article {article_label}"
        )
        if force or line != self.last_render:
            sys.stdout.write(line)
            sys.stdout.flush()
            self.last_render = line
            self._line_active = True


progress_tracker = ProgressTracker()


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

def web_scrape(url: str) -> str:
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            content = trafilatura.extract(downloaded)
            return content if content else "Scraper found no text."
        return "Access Denied."
    except Exception:
        return "Scrape Error."

def _resolve_google_news_url(url: str) -> str:
    """Follow Google News redirect to get the real article URL."""
    if "news.google.com" not in url:
        return url
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
            timeout=10,
        )
        return resp.url if resp.url else url
    except Exception:
        return url

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


def _clean_feed_text(text: str | None) -> str:
    raw_text = text or ""
    return re.sub(r"\s+", " ", raw_text).strip()


def _extract_feed_items(feed_xml: str) -> List[dict]:
    soup = BeautifulSoup(feed_xml, "xml")
    items: List[dict] = []

    for item in soup.find_all(["item", "entry"]):
        title = _clean_feed_text(item.title.get_text(" ", strip=True) if item.title else "")
        if item.link and item.link.get("href"):
            link = item.link.get("href", "").strip()
        elif item.link:
            link = _clean_feed_text(item.link.get_text(" ", strip=True))
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

    print(
        "[progress]   "
        + " | ".join(f"{key}: {len(stories)}" for key, stories in provider_stories.items())
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


def _ordered_topic_match_terms(*values: Any) -> list[str]:
    text = " ".join(str(value or "") for value in values)
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text.lower()):
        token_variants = [token]
        if "-" in token:
            token_variants.extend(part for part in token.split("-") if part)
        for variant in token_variants:
            normalized = variant.strip("'")
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
            if len(normalized) < 3 or normalized in TOPIC_MATCH_STOPWORDS:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
    return terms


def _topic_match_terms(*values: Any) -> set[str]:
    return set(_ordered_topic_match_terms(*values))


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


def _score_topic_text_match(topic: dict, text: str) -> int:
    text_terms = _topic_match_terms(text)
    if not text_terms:
        return 0

    matched_terms: set[str] = set()
    phrase_score = 0
    boost_score = 0

    for keyword in topic.get("keywords", []) or []:
        keyword_terms = _topic_match_terms(keyword)
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
        phrase_terms = _topic_match_terms(phrase)
        if len(phrase_terms) < 2:
            continue
        overlap = phrase_terms & text_terms
        if len(overlap) >= _topic_phrase_required_overlap(len(phrase_terms)):
            matched_terms.update(overlap)
            boost_score += 4

    strict_score = (len(matched_terms) * 2) + phrase_score + boost_score
    return max(strict_score, _lenient_topic_overlap_score(topic, text))


def _lenient_topic_overlap_score(topic: dict, text: str) -> int:
    topic_terms = _topic_match_terms(
        topic.get("title"),
        topic.get("rationale"),
        " ".join(str(k) for k in topic.get("keywords", [])),
        " ".join(str(p) for p in topic.get("boost_phrases", [])),
    )
    text_terms = _topic_match_terms(text)
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


# --- RELEVANCE SCORING (PER DYNAMIC TOPIC) ---

def _compose_feed_haystack(item: dict) -> str:
    return " ".join([item.get("title", ""), item.get("description", "")]).lower()


def _score_topic_relevance(item: dict, topic: dict) -> int:
    haystack = _compose_feed_haystack(item)
    score = _score_topic_text_match(topic, haystack)

    if score < int(topic.get("min_score", 1)):
        return 0
    return score


def _select_per_topic_feed_items(items: List[dict], topics: List[dict], *, now_utc: datetime) -> List[dict]:
    section_candidates: dict[str, list[dict]] = {topic["key"]: [] for topic in topics}

    for item in items:
        if not item.get("link"):
            continue
        if not _is_within_recent_window(item.get("published_at"), now_utc):
            continue

        best_match: tuple[dict, int] | None = None
        for topic in topics:
            topic_score = _score_topic_relevance(item, topic)
            if topic_score <= 0:
                continue
            if best_match is None or topic_score > best_match[1]:
                best_match = (topic, topic_score)

        if best_match is None:
            continue

        topic, score = best_match
        enriched_item = dict(item)
        enriched_item["relevance_score"] = score
        enriched_item["topic_key"] = topic["key"]
        enriched_item["topic_title"] = topic["title"]
        section_candidates[topic["key"]].append(enriched_item)

    selected: list[dict] = []
    for topic in topics:
        key = topic["key"]
        max_for_section = max(0, int(topic.get("max_articles_per_source", 0)))
        if PER_SOURCE_TOPIC_ARTICLE_CAP > 0:
            max_for_section = min(max_for_section, PER_SOURCE_TOPIC_ARTICLE_CAP)
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
    selected_items = _select_per_topic_feed_items(
        items,
        topics,
        now_utc=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    selected_by_topic: dict[str, int] = {}
    selected_item_details: list[dict] = []
    for item in selected_items:
        topic_title = item.get("topic_title") or "Unknown topic"
        selected_by_topic[topic_title] = selected_by_topic.get(topic_title, 0) + 1
        selected_item_details.append(
            {
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "pub_date": item.get("pub_date", ""),
                "topic_title": item.get("topic_title"),
                "topic_key": item.get("topic_key"),
                "relevance_score": item.get("relevance_score", 0),
            }
        )
    if not selected_items:
        return {
            "articles": [],
            "status": "no_relevant_items",
            "feed_item_count": len(items),
            "selected_item_count": 0,
            "selected_items": [],
            "selected_by_topic": {},
        }
    selected_articles = []
    for item in selected_items:
        selected_url = _resolve_google_news_url(item.get("link", "").strip())
        if not selected_url:
            continue

        article_text = web_scrape(selected_url)
        if not article_text or article_text in {"Access Denied.", "Scrape Error.", "Scraper found no text."}:
            summary_parts = [part for part in [item.get("title"), item.get("description")] if part]
            if not summary_parts:
                continue
            article_text = ". ".join(summary_parts)

        article_text = _translate_if_needed(article_text, item.get("title", ""))

        selected_articles.append(
            {
                "url": selected_url,
                "text": prepare_article_text_for_summary(article_text),
                "title": item.get("title", ""),
                "pub_date": item.get("pub_date", ""),
                "description": item.get("description", ""),
                "topic_key": item.get("topic_key"),
                "topic_title": item.get("topic_title"),
                "relevance_score": item.get("relevance_score", 0),
            }
        )

    if not selected_articles:
        return {
            "articles": [],
            "status": "scrape_failed",
            "feed_item_count": len(items),
            "selected_item_count": len(selected_items),
            "selected_items": selected_item_details,
            "selected_by_topic": selected_by_topic,
        }
    return {
        "articles": selected_articles,
        "status": "ok",
        "feed_item_count": len(items),
        "selected_item_count": len(selected_items),
        "selected_items": selected_item_details,
        "selected_by_topic": selected_by_topic,
    }


def gather_article_targets_for_source(
    source_name: str,
    topics: List[dict],
    seen_urls: set[str],
    run_seen_urls: set[str],
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
        if not url:
            source_run["rejected_counts"]["missing_url"] += 1
            continue
        if url in run_seen_urls:
            source_run["rejected_counts"]["duplicate_this_run"] += 1
            continue
        if not DEV and url in seen_urls:
            source_run["rejected_counts"]["seen_in_history"] += 1
            continue

        fresh_articles.append(article)
        new_urls.append(url)
        run_seen_urls.add(url)
        seen_urls.add(url)

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
            "topic_title": article.get("topic_title"),
            "relevance_score": article.get("relevance_score", 0),
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
    run_seen_urls: set[str],
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

    for topic in topics:
        topic_key = str(topic.get("key") or "")
        topic_title = str(topic.get("title") or topic_key or "Unknown topic")
        if topic_key in existing_topic_keys:
            continue

        candidates = _rank_top_funnel_coverage_candidates(topic, top_stories)
        topic_added = 0
        for story in candidates:
            selected_url = _resolve_google_news_url(str(story.get("url") or "").strip())
            if not selected_url or selected_url in run_seen_urls:
                continue
            if not DEV and selected_url in seen_urls:
                continue

            article_text = web_scrape(selected_url)
            if not article_text or article_text in {"Access Denied.", "Scrape Error.", "Scraper found no text."}:
                summary_parts = [
                    str(part).strip()
                    for part in [story.get("title"), story.get("description")]
                    if str(part or "").strip()
                ]
                if not summary_parts:
                    continue
                article_text = ". ".join(summary_parts)

            article_text = _translate_if_needed(article_text, str(story.get("title") or ""))
            target_index = len(fallback_targets) + 1
            fallback_targets.append(
                {
                    "article_id": f"top-funnel-{topic_key}-{target_index}",
                    "source": _story_source_label(story),
                    "title": story.get("title", ""),
                    "pub_date": _story_pub_date(story),
                    "url": selected_url,
                    "description": story.get("description", ""),
                    "text": prepare_article_text_for_summary(article_text),
                    "topic_key": topic_key,
                    "topic_title": topic_title,
                    "relevance_score": _score_topic_against_story(topic, story),
                    "coverage_fallback": True,
                }
            )
            new_urls.append(selected_url)
            run_seen_urls.add(selected_url)
            seen_urls.add(selected_url)
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
                "Base the title on the dominant theme across today's top stories, "
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


def _sampling_to_extra_body(settings: ModelSamplingSettings) -> dict[str, float | int]:
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


def build_chat_model(max_tokens: int, *, task: str = "default") -> ChatOpenAI:
    sampling = MODEL_TASK_SAMPLING.get(task, MODEL_DEFAULT_SAMPLING)
    return ChatOpenAI(
        base_url=MODEL_BASE_URL,
        api_key="not-needed",
        temperature=sampling.temperature,
        model=MODEL_NAME,
        max_tokens=max_tokens,
        max_retries=0,
        extra_body=_sampling_to_extra_body(sampling),
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
    return truncate_text_to_token_limit(text or "", ARTICLE_TEXT_TOKEN_LIMIT)


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


def describe_final_synthesis_rejection(
    text: str,
    topics: List[dict],
    *,
    uses_custom_prompt: bool,
    relaxed: bool = False,
) -> str:
    if _contains_disallowed_final_markup(text) and not relaxed:
        return "disallowed topic markup"

    clean_text = _strip_prompt_echo_lines(strip_model_artifacts(text or ""))
    if not clean_text:
        return "empty after cleanup"
    if uses_custom_prompt:
        return ""

    heading_count = _final_synthesis_heading_count(clean_text)
    word_count = _final_synthesis_word_count(clean_text)

    if relaxed:
        minimum_words = min(30, max(8, len(topics) * 8))
        if word_count < minimum_words:
            return f"too short ({word_count}/{minimum_words} words)"
        return ""

    if heading_count == 0:
        return "missing markdown section heading"
    minimum_words = min(120, max(50, len(topics) * 35))
    if word_count < minimum_words:
        return f"too short ({word_count}/{minimum_words} words)"
    return ""


def is_valid_final_synthesis_response(
    text: str,
    topics: List[dict],
    *,
    uses_custom_prompt: bool,
    relaxed: bool = False,
) -> bool:
    return not describe_final_synthesis_rejection(
        text,
        topics,
        uses_custom_prompt=uses_custom_prompt,
        relaxed=relaxed,
    )


def _is_low_coverage_synthesis_section(section_text: str) -> bool:
    clean_text = re.sub(r"\s+", " ", strip_model_artifacts(section_text or "")).strip().lower()
    if not clean_text:
        return True
    return any(pattern in clean_text for pattern in LOW_COVERAGE_SYNTHESIS_PATTERNS)


def clean_synthesis_for_publication(text: str, *, relaxed: bool = False) -> str:
    if _contains_disallowed_final_markup(text) and not relaxed:
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
    section_count = len(topics)
    return textwrap.dedent(f"""
        TASK:
        1) Output ONLY this exact structure (one section per top story of the day, in order):
{section_lines}
        2) Use only claims supported by PRIMARY_DATASET; if support is weak or conflicting, place
           that point in the same section with explicit uncertainty language.
        3) Each of the {section_count} sections should be roughly 130-220 words of cohesive prose
           (no bullets, no label-colon fragments). Read like a compact wire-service roundup.
        4) Focus on concrete reported claims: who acted, what happened, where, when, casualties or
           damage, official statements, deadlines, and what remains unconfirmed.
        5) Do not include a preamble, methodology, or outlet-style commentary.
        6) If a section has no credible updates in the dataset, omit that section entirely. Do not
           explain missing coverage, apologize, or mention empty source material.
        7) Write for newsletter recipients. Do not mention the user, the prompt, AI, PRIMARY_DATASET,
           LOW_CONFIDENCE_DATASET, supplied coverage, or source-material limitations in the final copy.
        8) Do not write XML/HTML-style topic tags, label-only lines, bullets, or headline lists.
        9) Do not invent facts beyond what PRIMARY_DATASET supports. Treat LOW_CONFIDENCE_DATASET
           entries as headline-only material; they may justify uncertainty language but never establish
           a claim on their own.
    """).strip()


def _report_topic_label(entry: str) -> str:
    topic_match = re.search(r"^- Topic:\s*(.+)$", entry or "", flags=re.MULTILINE)
    return topic_match.group(1).strip() if topic_match else ""


def _report_summary_text(entry: str) -> str:
    summary_match = re.search(r"Summary:\s*(.*)", entry or "", flags=re.DOTALL)
    return re.sub(r"\s+", " ", summary_match.group(1).strip()) if summary_match else ""


def _build_grouped_synthesis_dataset(reports: List[str], topics: List[dict]) -> str:
    topic_order = [str(topic.get("title") or "").strip() for topic in topics if topic.get("title")]
    grouped: dict[str, list[str]] = {topic_title: [] for topic_title in topic_order}
    ungrouped: list[str] = []

    for report in reports:
        summary_text = _report_summary_text(report)
        if not summary_text:
            continue
        topic_label = _report_topic_label(report)
        if topic_label in grouped:
            grouped[topic_label].append(summary_text)
        elif topic_label:
            grouped.setdefault(topic_label, []).append(summary_text)
        else:
            ungrouped.append(summary_text)

    sections: list[str] = []
    remaining_topics = [topic for topic in grouped.keys() if topic not in topic_order]
    for topic_title in topic_order + sorted(remaining_topics):
        summaries = grouped.get(topic_title) or []
        if not summaries:
            continue
        lines = [f"Story: {topic_title}", "Source summaries:"]
        for index, summary_text in enumerate(summaries, start=1):
            lines.append(f"{index}. {summary_text}")
        sections.append("\n".join(lines))

    if ungrouped:
        lines = ["Other source summaries:"]
        for index, summary_text in enumerate(ungrouped, start=1):
            lines.append(f"{index}. {summary_text}")
        sections.append("\n".join(lines))

    return "\n\n---\n\n".join(sections)


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
        supplemental_dataset = "(Omitted to save context window)"

        uses_custom_prompt = custom_prompt_text is not None
        if uses_custom_prompt:
            instruction_text = textwrap.dedent(
                f"""
                You are synthesizing prewritten article summaries about {PROJECT_SUMMARY_SCOPE_LABEL}.
                Use only the supplied dataset.
                Treat PRIMARY_DATASET as the stronger evidence base and LOW_CONFIDENCE_DATASET as sparse-support material.
                Focus on concrete reported claims and avoid discussion of outlet style unless it changes the factual claim.
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
                "One section per top story of the day, in the order listed in the TASK. "
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
            "uses_custom_prompt": uses_custom_prompt,
            "topic_count": len(topics),
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
        progress_tracker.log(f"--- [EMAIL]: Skipping email. Missing configuration: {', '.join(missing)} ---")
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
        message["Subject"] = report_title
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

    progress_tracker.log(f"--- [EMAIL]: Sent report to {', '.join(recipients)} ---")

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
        covering one of today's top news stories.
        1. Use only the provided article metadata, URL, description, and article text.
        2. Do not call tools in this step.
        3. Ignore outlet style and focus on concrete reported claims.
        4. Include key facts: what reportedly happened, where, timeline, named actors, casualties or damage if reported, and what remains unconfirmed.
        5. If the article text is thin, summarize only what is actually supported by the provided text and metadata.
        6. Start your response with 'DATABASE_ENTRY:' and then exactly the requested Markdown block.
        7. Do not include any text before 'DATABASE_ENTRY:' or after the summary.
    """).strip())
    article_payload = (
        "Selected article:\n\n"
        f"Title: {current_article.get('title') or 'N/A'}\n"
        f"Source: {display_name}\n"
        f"Published: {current_article.get('pub_date') or 'Unknown publish time'}\n"
        f"URL: {current_article.get('url') or 'N/A'}\n"
        f"Topic: {topic_title or 'general top story'}\n"
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
        return {
            "messages": [HumanMessage(content=(
                "The last response did not match the requested newsletter format. "
                "Provide the requested final synthesis now using only the supplied dataset. "
                "Use markdown ## section headings, write cohesive prose paragraphs, and return no tags, bullets, "
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
    grouped: dict[str, list[str]] = {topic_title: [] for topic_title in topic_order}
    ungrouped: list[str] = []

    for report in final_reports:
        summary_text = _report_summary_text(report)
        if not summary_text:
            continue
        topic_label = _report_topic_label(report)
        if topic_label in grouped:
            grouped[topic_label].append(summary_text)
        elif topic_label:
            grouped.setdefault(topic_label, []).append(summary_text)
        else:
            ungrouped.append(summary_text)

    sections: list[str] = []
    for topic_title in topic_order:
        summaries = grouped.get(topic_title) or []
        if not summaries:
            continue
        paragraph = _dev_synthesis_paragraph_from_summaries(summaries)
        if paragraph:
            sections.append(
                f"## {_format_topic_section_header(topic_title)}\n"
                f"{paragraph}"
            )

    if not sections and ungrouped:
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
    if shutil.which("mflux-generate-flux2") is None:
        raise RuntimeError(
            "mflux-generate-flux2 is not on PATH. Install/sync mflux or run with "
            "`uv run --with \"mflux>=0.16.0\" todays_news.py`."
        )

    with tempfile.TemporaryDirectory(prefix="news-art-mflux-") as temp_dir:
        prompt_path = os.path.join(temp_dir, "prompt.txt")
        raw_output_path = os.path.join(temp_dir, "mflux-output.png")
        with open(prompt_path, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(prompt + "\n")

        command = [
            "mflux-generate-flux2",
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
        subprocess.run(command, check=True)
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
        progress_tracker.log(
            f"[progress] Generating report image with {IMAGE_MODEL_LABEL} "
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
        progress_tracker.log(f"[progress] WARNING: {message}")
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
    dict[str, List[tuple[str, str | None, str | None]]],
    dict[str, dict[str, List[tuple[str, str | None, str | None]]]],
]:
    grouped_headlines: dict[str, List[tuple[str, str | None, str | None]]] = {}
    grouped_by_topic: dict[str, dict[str, List[tuple[str, str | None, str | None]]]] = {}
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
        normalized_url = url_text if url_text and url_text != "N/A" else ""
        dedupe_key = (topic_title, source_name, title_text, normalized_url)
        if dedupe_key in seen_pairs:
            continue
        seen_pairs.add(dedupe_key)

        if topic_title:
            topic_sources = grouped_by_topic.setdefault(topic_title, {})
            topic_sources.setdefault(display_name, []).append(
                (title_text, normalized_url or None, homepage_url)
            )
        else:
            grouped_headlines.setdefault(display_name, []).append(
                (title_text, normalized_url or None, homepage_url)
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
                for headline, link, _ in source_entries:
                    topic_lines.append(f"- {headline} ({link})" if link else f"- {headline}")
                topic_lines.append("")
            grouped_sections.append("\n".join(topic_lines).strip())
        if grouped_headlines:
            for display_name, source_entries in grouped_headlines.items():
                first_entry = source_entries[0] if source_entries else None
                homepage_url = first_entry[2] if first_entry else None
                lines = [f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:", ""]
                for headline, link, _ in source_entries:
                    lines.append(f"- {headline} ({link})" if link else f"- {headline}")
                grouped_sections.append("\n".join(lines))
    else:
        for display_name, source_entries in grouped_headlines.items():
            first_entry = source_entries[0] if source_entries else None
            homepage_url = first_entry[2] if first_entry else None
            lines = [f"{display_name}: {homepage_url}" if homepage_url else f"{display_name}:", ""]
            for headline, link, _ in source_entries:
                lines.append(f"- {headline} ({link})" if link else f"- {headline}")
            grouped_sections.append("\n".join(lines))

    return "\n\n".join(grouped_sections) if grouped_sections else "No article headlines available."


def _render_html_paragraphs(block_text: str) -> str:
    paragraphs = [segment.strip() for segment in re.split(r"\n\s*\n", block_text.strip()) if segment.strip()]
    return "".join(
        f"<p style=\"margin:0 0 18px; font-size:16px; line-height:1.7; color:#1f2937;\">"
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

    def render_source_entries(source_map: dict[str, List[tuple[str, str | None, str | None]]]) -> str:
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
            for headline, link, _ in source_entries:
                headline_text = html.escape(headline)
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
        "<html><body style=\"margin:0; padding:0; background-color:#f3f4f6;\">"
        "<div style=\"margin:0; padding:24px 8px;\">"
        "<div style=\"max-width:980px; margin:0 auto; background:#ffffff; border-radius:8px; overflow:hidden;\">"
        "<div style=\"padding:38px 48px 24px;\">"
        f"<p style=\"margin:0 0 24px; font-size:18px; line-height:1.6; color:#111827;\">{html.escape(first_name)},</p>"
        "<p style=\"margin:0 0 28px; font-size:17px; line-height:1.7; color:#374151;\">Here is your daily news summary.</p>"
        f"<h1 style=\"margin:0 0 28px; font-size:32px; line-height:1.2; font-weight:800; color:#111827;\">{html.escape(report_title)}</h1>"
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
    return RunDiagnostics(
        run_started_at=RUN_STARTED_AT.isoformat(timespec="seconds"),
        settings={
            "run_mode": RUN_MODE,
            "dev": DEV,
            "local_prod": LOCAL_PROD,
            "bradley_only_delivery": BRADLEY_ONLY_DELIVERY,
            "relaxed_final_synthesis_guards": RELAXED_FINAL_SYNTHESIS_GUARDS,
            "source_count": source_count,
            "sources_path": str(CONFIG.sources_path),
            "top_funnel_provider_count": len(TOP_FUNNEL_PROVIDERS),
            "recipients_path": str(CONFIG.recipients_path),
            "output_dir": RUN_OUTPUT_DIR,
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
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
            "model_server_command": MODEL_SERVER_COMMAND,
            "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,
            "model_default_sampling": _sampling_to_dict(MODEL_DEFAULT_SAMPLING),
            "model_reasoning_sampling": _sampling_to_dict(MODEL_REASONING_SAMPLING),
            "model_task_sampling": _task_sampling_to_dict(),
            "article_summary_concurrency": ARTICLE_SUMMARY_CONCURRENCY,
            "article_text_token_limit": ARTICLE_TEXT_TOKEN_LIMIT,
            "total_article_summary_cap": TOTAL_ARTICLE_SUMMARY_CAP,
            "per_topic_article_summary_cap": PER_TOPIC_ARTICLE_SUMMARY_CAP,
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


def _write_run_diagnostics(diagnostics: RunDiagnostics) -> None:
    with MODEL_CALL_STATS_LOCK:
        diagnostics.record_model_call_stats(json.loads(json.dumps(MODEL_CALL_STATS)))
    json_path, markdown_path = diagnostics.write(CONFIG.run_output_dir, timestamp)
    print(f"[progress] Run details saved: {json_path}")
    print(f"[progress] Human-readable run details saved: {markdown_path}")


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
        print(f"[progress] Managed model server already exited with code {process.returncode}.")
        return

    print("[progress] Stopping managed model server...")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        print("[progress] Managed model server did not stop gracefully; killing it.")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()
        process.wait(timeout=10)


def run_pipeline() -> None:
    with managed_model_server():
        _run_pipeline()


@contextmanager
def managed_model_server():
    existing_preflight = preflight_model_server()
    if existing_preflight.get("ok") and existing_preflight.get("model_match"):
        print(
            "[progress] Model server already running for the selected model; "
            "using it without managing its lifecycle."
        )
        yield
        return

    if existing_preflight.get("ok"):
        raise RuntimeError(
            "Model server endpoint is already in use, but it did not report the expected model. "
            f"Expected {MODEL_REFERENCE} / {MODEL_NAME}; served "
            f"{existing_preflight.get('served_models')}. Stop that server or change NEWS_MODEL_BASE_URL."
        )

    log_path = _managed_model_server_log_path()
    command = shlex.split(MODEL_SERVER_COMMAND)
    print(f"[progress] Starting managed model server: {MODEL_SERVER_COMMAND}")
    print(f"[progress] Managed model server log: {log_path}")
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
        print(
            "[progress] Managed model server is ready. "
            f"Served models: {ready_preflight.get('served_models') or ['n/a']}"
        )
        yield
    finally:
        _stop_managed_model_server(process)
        log_file.close()


def _run_pipeline() -> None:
    all_sources = list(SOURCE_FEEDS.keys())

    # In dev mode, cap the source list and topic count to the minimum needed to
    # exercise every code path (multi-source sweep, topic matching, article budget,
    # synthesis, image, email) without a full production-width run.
    if DEV:
        sources = all_sources[:DEV_SOURCE_LIMIT]
        effective_num_topics = min(NUM_TOP_TOPICS, DEV_NUM_TOPICS)
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
    print("[progress] Starting daily news run...")
    print(
        f"[progress] Model profile: {MODEL_PROFILE_KEY} | model: {MODEL_REFERENCE} -> {MODEL_NAME}"
    )
    print(
        f"[progress] Model caps: input {MODEL_MAX_INPUT_TOKENS} tokens, "
        f"article text {ARTICLE_TEXT_TOKEN_LIMIT} tokens, "
        f"summaries {TOTAL_ARTICLE_SUMMARY_CAP} total/{PER_TOPIC_ARTICLE_SUMMARY_CAP} per topic, "
        f"{PER_SOURCE_TOPIC_ARTICLE_CAP} per source/topic, "
        f"summary concurrency {ARTICLE_SUMMARY_CONCURRENCY}."
    )
    print(f"[progress] Run mode: {RUN_MODE}")
    preflight = preflight_model_server()
    diagnostics.event("model_server_preflight", **preflight)
    if not preflight.get("ok"):
        print(
            f"[progress] WARNING: model server preflight failed at {preflight.get('models_url')}: "
            f"{preflight.get('error') or 'unknown error'}"
        )
    elif not preflight.get("model_match"):
        print(
            "[progress] WARNING: model server is reachable but did not report the expected model. "
            f"Served: {preflight.get('served_models')}"
        )
    print(f"[progress] Source pool: {len(sources)} of {len(all_sources)} configured feed(s).")
    if DEV:
        print(
            f"[progress] DEV mode active. Source pool limited to {DEV_SOURCE_LIMIT} source(s), "
            f"topics limited to {effective_num_topics} (full pool: {len(all_sources)} sources, "
            f"{NUM_TOP_TOPICS} topics), article summaries capped at "
            f"{effective_total_article_summary_cap} total/{effective_per_topic_article_summary_cap} per topic. "
            f"Sending to one recipient only "
            f"(always {BRADLEY_ONLY_RECIPIENT}) without recording URLs into the shared history."
        )
    elif LOCAL_PROD:
        print(
            f"[progress] LOCAL-PROD mode active. Using the full production source/topic scope "
            f"and shared URL history, but sending only to {BRADLEY_ONLY_RECIPIENT}."
        )
    print(f"[progress] Run output folder: {RUN_OUTPUT_DIR}")
    print(f"[progress] Run used URL log: {RUN_USED_URLS_PATH}")
    if not DEV:
        print(f"[progress] Global seen URL log: {LEGACY_SEEN_URLS_PATH}")

    # 1) Discover today's top stories from configured top-of-funnel providers.
    #    Seed-capable providers generate candidate topics; validation-capable
    #    providers test those candidates before soft selection.
    seed_provider_count = sum(1 for p in TOP_FUNNEL_PROVIDERS.values() if p.get("can_seed_topics"))
    validation_provider_count = sum(1 for p in TOP_FUNNEL_PROVIDERS.values() if p.get("can_validate_topics"))
    print(
        f"[progress] Discovering today's top stories from {len(TOP_FUNNEL_PROVIDERS)} configured provider(s): "
        f"{seed_provider_count} seed-capable, {validation_provider_count} validation-capable "
        f"(top {TOP_OF_FUNNEL_PER_PROVIDER} per provider)."
    )
    top_funnel = discover_top_stories_of_day(per_provider_limit=TOP_OF_FUNNEL_PER_PROVIDER)
    top_stories = top_funnel["all_stories"]
    seed_stories = top_funnel["seed_stories"]
    validation_stories = top_funnel["validation_stories"]
    diagnostics.record_top_funnel(
        providers=LAST_TOP_FUNNEL_PROVIDER_STORIES,
        merged=top_stories,
        seed_merged=seed_stories,
        validation_merged=validation_stories,
        provider_metadata=LAST_TOP_FUNNEL_PROVIDER_METADATA,
    )
    if not seed_stories:
        print("[progress] No seed-capable top stories discovered. Aborting.")
        diagnostics.event("aborted", reason="no_seed_stories")
        _write_run_diagnostics(diagnostics)
        return
    multi_provider_count = sum(1 for s in top_stories if len(s.get("providers", [])) >= 2)
    print(
        f"[progress] Pulled {len(top_stories)} unique top-of-day headlines "
        f"({multi_provider_count} exact URL/title duplicate(s) across providers; "
        "topic-level overlap is assessed after clustering)."
    )

    # 2) Have the LLM cluster seed headlines into a larger candidate set, validate
    #    candidates separately, then soft-select N final topics without hard quotas.
    candidate_topic_count = max(effective_num_topics, min(effective_num_topics * 3, effective_num_topics + 8))
    print(
        f"[progress] Clustering into up to {candidate_topic_count} candidate topics "
        f"(probing ~{TOP_TOPIC_PROBES} headlines per cluster)."
    )
    candidate_topics = llm_cluster_top_topics(seed_stories, candidate_topic_count, TOP_TOPIC_PROBES)
    if not candidate_topics:
        print("[progress] Topic clustering produced nothing. Aborting.")
        diagnostics.event("aborted", reason="no_topics")
        _write_run_diagnostics(diagnostics)
        return
    annotated_candidates = annotate_topic_discovery_signals(
        candidate_topics,
        seed_stories=seed_stories,
        validation_stories=validation_stories,
    )
    topic_overlap_count = count_topic_level_provider_overlaps(annotated_candidates)
    print(
        f"[progress] Topic-level provider overlap: {topic_overlap_count}/{len(annotated_candidates)} "
        "candidate topic(s) matched 2+ providers by story, not exact headline."
    )
    selection_candidates = prepare_candidate_topics_for_selection(
        annotated_candidates,
        seed_stories=seed_stories,
        validation_stories=validation_stories,
        target_count=effective_num_topics,
    )
    if not selection_candidates:
        print("[progress] No seed-supported topic candidates remained after validation. Aborting.")
        diagnostics.event("aborted", reason="no_seed_supported_topics")
        _write_run_diagnostics(diagnostics)
        return
    topics = select_topics_soft_weighted(
        selection_candidates,
        effective_num_topics,
        seed=f"{RUN_DATE}:{timestamp}",
    )
    diagnostics.record_topics(topics)
    for topic in topics:
        print(
            f"[progress]   - {topic['title']} "
            f"(seeded: {','.join(topic.get('seed_providers') or ['n/a'])}; "
            f"validated: {','.join(topic.get('validation_providers') or ['n/a'])}; "
            f"frames: {','.join(topic.get('frame_tags') or ['n/a'])})"
        )
    _persist_topics_debug(topics, top_stories)

    progress_tracker.reset(total_sources=len(sources))

    seen_urls = _load_seen_urls()
    run_seen_urls: set[str] = set()
    article_targets_for_budget: List[dict] = []
    final_reports: List[str] = []

    # 3) Iterate union of sources, collect articles per topic.
    for source_index, source_name in enumerate(sources, start=1):
        progress_tracker.start_source(source_index)
        article_targets, new_urls, source_run = gather_article_targets_for_source(
            source_name, topics, seen_urls, run_seen_urls
        )
        diagnostics.record_source_run(source_run)
        progress_tracker.set_source_article_total(0)
        if new_urls:
            _record_run_urls(new_urls)
        if article_targets:
            article_targets_for_budget.extend(article_targets)
        progress_tracker.source_completed()

    fallback_targets, fallback_urls, fallback_stats = build_top_funnel_article_targets_for_coverage_gaps(
        topics,
        top_stories,
        article_targets_for_budget,
        seen_urls,
        run_seen_urls,
    )
    if fallback_urls:
        _record_run_urls(fallback_urls)
    if fallback_targets:
        article_targets_for_budget.extend(fallback_targets)
        progress_tracker.log(
            "[progress] Filled top-story coverage gaps from discovery links: "
            + ", ".join(fallback_stats["filled_topics"].keys())
        )
    elif fallback_stats.get("skipped_topics"):
        progress_tracker.log(
            "[progress] No discovery-link fallback coverage found for: "
            + ", ".join(fallback_stats["skipped_topics"])
        )

    article_targets, article_budget_stats = budget_article_targets(
        article_targets_for_budget,
        topics,
        total_cap=effective_total_article_summary_cap,
        per_topic_cap=effective_per_topic_article_summary_cap,
        per_source_topic_cap=PER_SOURCE_TOPIC_ARTICLE_CAP,
    )
    diagnostics.record_article_budget(article_budget_stats)
    progress_tracker.log(
        f"[progress] Article budget: {article_budget_stats.get('included_count', 0)} selected, "
        f"{article_budget_stats.get('dropped_count', 0)} candidate target(s) not summarized "
        f"(cap {effective_total_article_summary_cap} total/"
        f"{effective_per_topic_article_summary_cap} per topic/"
        f"{PER_SOURCE_TOPIC_ARTICLE_CAP} per source-topic)."
    )
    progress_tracker.start_article_summary(len(article_targets))
    if article_targets:
        final_reports.extend(run_article_summary_pass(article_targets, topics))

    progress_tracker.set_final_step("reports", 1)
    diagnostics.article_summary_count = len(final_reports)
    progress_tracker.log(f"[progress] Saved {len(final_reports)} article summary record(s).")

    recipient_config = get_active_recipient_config(load_recipient_config())
    prompt_groups = build_prompt_groups(recipient_config)

    if not prompt_groups:
        progress_tracker.log("[progress] No recipients configured. Exiting after article summary generation.")
        diagnostics.event("completed_without_recipients")
        _write_run_diagnostics(diagnostics)
        return

    for group in prompt_groups:
        recipient_list = group["recipient_emails"]
        prompt_label = "default prompt" if group["uses_default_prompt"] else "custom prompt"
        progress_tracker.log(f"[progress] Building {prompt_label} report for: {', '.join(recipient_list)}")

        progress_tracker.set_final_step("synthesis", 2)
        final_synthesis, token_stats, synthesis_debug = run_final_synthesis_pass(
            final_reports,
            topics,
            group["custom_prompt_text"],
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
            progress_tracker.log(
                f"[progress] No synthesis generated for {', '.join(recipient_list)} "
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
        report_path = build_report_path(group)

        progress_tracker.set_final_step("art", 3)
        image_art = generate_report_image_art(
            report_path=report_path,
            synthesis_body=synthesis_body,
            report_title=report_title,
        )

        progress_tracker.set_final_step("render", 4)
        report_body = build_report_body(report_title, synthesis_body, final_reports, topics, image_art)
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
            image_art=image_art_diagnostics,
        )

        progress_tracker.set_final_step("email", 5)
        maybe_email_report(
            report_title,
            report_body,
            synthesis_body,
            final_reports,
            topics,
            recipient_list,
            group["recipient_names"],
            image_art,
        )

        if token_stats:
            progress_tracker.log(f"[progress] Final synthesis token stats for {', '.join(recipient_list)}: {token_stats}")
        progress_tracker.log(f"[progress] Finished report. Saved text report: {report_path}")

    progress_tracker.finish("done")
    diagnostics.event("completed")
    _write_run_diagnostics(diagnostics)
