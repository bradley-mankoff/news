"""Daily news pipeline implementation.

Common usage:
    uv run news run --preset NAME
    NEWS_SOURCE_SCOPE=peripheral NEWS_RECIPIENT_SCOPE=primary uv run news run

Common Run Settings:
    NEWS_SOURCE_SCOPE=core|peripheral
    NEWS_RECIPIENT_SCOPE=primary|all
    NEWS_BLOCK_REUSED_URLS=0|1
    NEWS_IMAGE_ENABLED=0|1

Model selection:
    NEWS_MODEL=gemma-e2b-tiny uv run news run --preset NAME
    NEWS_MODEL=qwythos-9b-8bit uv run news run --preset NAME
    NEWS_MODEL=qwythos-9b-4bit uv run news run --preset NAME

    NEWS_MODEL accepts either a friendly alias above or a full model repo/name.
    Task-specific model assignments inherit it unless overridden.

Local model server:
    NEWS_MODEL=https://huggingface.co/EgorKodin/Huihui-gemma-4-12B-it-abliterated-mlx-4bit uv run news run --preset NAME
        Starts the matching local MLX server automatically, waits until it
        is ready, runs the pipeline, then shuts the managed server down even if
        the run errors. Server logs are written beside the report output.

    NEWS_MODEL=... uv run news model-server-command
        Prints the matching MLX server command for managed backends; for the
        external backend, prints a notice instead (no managed command exists).

    NEWS_MODEL_BACKEND=external NEWS_MODEL_BASE_URL=https://api.example.com/v1 uv run news run --preset NAME
        Uses any OpenAI-compatible endpoint for the default model: no managed
        server is started; the pipeline waits for and probes the endpoint.
        Authenticated endpoints can pass NEWS_MODEL_API_KEY (sent as a Bearer
        token on /models and /chat/completions).

    uv run news codex-model-server-command
        Prints the Codex-safe MLX server command for gemma-e2b-tiny. Codex-run
        model invocation is blocked unless this tiny model is selected.

    NEWS_MODEL_BASE_URL=http://127.0.0.1:8080/v1 uv run news run --preset NAME
        Points the pipeline at a different OpenAI-compatible local endpoint.

Other useful switches:
    NEWS_IMAGE_ENABLED=0 uv run news run --preset NAME
        Skips report image generation.

    uv run news serve-unsubscribe
        Runs the local unsubscribe endpoint instead of generating a report.

This module owns orchestration and the heavier pipeline logic. Configuration
lives in ``config/*.yaml``. Run history is written to the DuckDB history store
under ``output/history/`` by default; the human-readable review is published to
``output/daily_outputs/latest_run.md``.
"""

import importlib.util
import dataclasses
import json
import logging
import re
import os
import shlex
import signal
import sys
import time
import textwrap
import traceback
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
from threading import Lock, RLock, current_thread, main_thread
from typing import Any, Callable, TextIO, List
import requests
from bs4 import BeautifulSoup
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .config import (
    ModelSamplingSettings,
    RuntimeConfig,
    DEFAULT_TITLE_GENERATION_MAX_TOKENS,
    MODEL_BACKEND_EXTERNAL,
    MODEL_TASK_ARTICLE_SUMMARY,
    MODEL_TASK_IMAGE_ART_DIRECTION,
    MODEL_TASK_STORY_DRAFTING,
    MODEL_TASK_STORY_SCALE_SCREENING,
    MODEL_TASK_TITLE_GENERATION,
    configured_model_api_key,
    ensure_codex_safe_model_reference,
    is_gemma_4_model_reference,
    load_recipients,
    load_runtime_config,
    load_sources,
    sync_assistant_context_latest_output,
    update_recipient_pause_setting,

)
from .diagnostics import RunDiagnostics
from .run_finalizer import RunFinalizer, RunFinalizerAdapters, RunFinalizerConfig
from .article_collection import (
    ArticleCollectionAdapters,
    ArticleCollectionRequest,
    collect_article_candidates,
)
from . import article_summarization as article_summarization_stage
from .prompt_contracts import IMAGE_ART_JSON_CONTRACT, IMAGE_ART_OVERLAY_PROTOCOL
from . import article_summary_records as article_summary_records_stage
from . import citations as citations_stage
from . import embeddings as embeddings_stage
from . import story_clustering as story_clustering_stage
from . import story_drafting as story_drafting_stage
from . import story_records as story_records_stage
from . import story_selection as story_selection_stage
from .prompt_catalog import DEFAULT_PROMPT_INSTRUCTIONS, resolve_prompt_instructions
from .feed_utils import (
    decode_google_news_article_path as _decode_google_news_article_path,
    google_news_query_target as _google_news_query_target,
    is_google_news_url as _is_google_news_url,
    parse_feed_datetime as _parse_feed_datetime_utc,
)
from .text_cleaning import (
    clean_article_text as _clean_article_text,
    clean_feed_text as _clean_feed_text,
    clean_feed_url as _clean_feed_url,
    strip_model_artifacts,
)

try:
    import tiktoken
except ImportError:
    tiktoken = None


logger = logging.getLogger(__name__)


def _filesystem_safe_model_label(value: str) -> str:
    clean_value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    clean_value = clean_value.strip("._-")
    return clean_value or "model"


def _json_ready(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _json_ready(val) for key, val in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_ready(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


MODEL_RETRY_ATTEMPTS = 4
MODEL_RETRY_BASE_DELAY_SECONDS = 2
MODEL_REQUEST_TIMEOUT_SECONDS = 180
MODEL_LOAD_PROBE_TIMEOUT_SECONDS = 120
# External endpoints get the same readiness budget as the managed-server wait
# (_wait_for_managed_model_server defaults to 300 s) so both paths behave alike.
EXTERNAL_SERVER_READY_TIMEOUT_SECONDS = 300
ARTICLE_DOWNLOAD_TIMEOUT_SECONDS = 20
ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS = max(
    ARTICLE_DOWNLOAD_TIMEOUT_SECONDS,
    30,
)
SLOW_SOURCE_WARNING_SECONDS = 60
CONFIG = load_runtime_config()
MODEL_ASSIGNMENTS = CONFIG.model_assignments
MODEL_TUNING = CONFIG.model_tuning
PIPELINE_BUDGET = CONFIG.pipeline_budget
MODEL_SERVER_SETTINGS = CONFIG.model_server_settings
MODEL_NAME = CONFIG.model_name
MODEL_REFERENCE = CONFIG.model_reference
MODEL_BASE_URL = CONFIG.model_base_url
MODEL_BACKEND = CONFIG.model_backend
MODEL_SERVER_COMMAND = CONFIG.model_server_command
MODEL_API_KEY = configured_model_api_key()
MODEL_REPORT_LABEL = _filesystem_safe_model_label(MODEL_REFERENCE)

PRIMARY_RECIPIENT = CONFIG.primary_recipient
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
ARTICLE_DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

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

RUN_STARTED_AT = CONFIG.run_started_at
RUN_DATE = CONFIG.run_date
timestamp = CONFIG.timestamp
OUTPUT_DIR = str(CONFIG.output_dir)
RUN_OUTPUT_DIR = str(CONFIG.run_output_dir)
RUN_STAGING_DIR = str(CONFIG.run_staging_dir)
LATEST_RUN_MARKDOWN_PATH = str(CONFIG.latest_run_markdown_path)
LATEST_RUN_LOG_PATH = str(CONFIG.latest_run_log_path)
LATEST_RUN_DETAILS_PATH = str(CONFIG.latest_run_details_path)
HISTORY_DB_PATH = str(CONFIG.history_db_path)
HISTORY_EXPORT_CSV = CONFIG.history_export_csv
PRESET_ID = CONFIG.preset_id
PROMPT_PROFILE_ID = CONFIG.prompt_profile_id
# Resolved once at import time: the profile is frozen for the process lifetime
# and validated eagerly (fail-fast on unknown ids) before any LLM work starts.
PROMPT_INSTRUCTIONS = resolve_prompt_instructions(
    PROMPT_PROFILE_ID, overrides=CONFIG.prompt_instruction_overrides
)
SOURCE_SCOPE = CONFIG.source_scope
RECIPIENT_SCOPE = CONFIG.recipient_scope
URL_REUSE_BLOCKING_ENABLED = CONFIG.url_reuse_blocking_enabled
RELAXED_STORY_DRAFTING_GUARDS = CONFIG.relaxed_story_drafting_guards
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
MODEL_MAX_INPUT_TOKENS = CONFIG.model_max_input_tokens
TOKEN_ENCODING_NAME = CONFIG.token_encoding_name
MODEL_CONCURRENCY = max(1, CONFIG.model_concurrency)
ARTICLE_SUMMARY_CONCURRENCY = max(1, CONFIG.article_summary_concurrency)
STORY_SYNTHESIS_CONCURRENCY = max(1, CONFIG.story_synthesis_concurrency)
SOURCE_COLLECTION_CONCURRENCY = max(1, CONFIG.source_collection_concurrency)
ARTICLE_TEXT_TOKEN_LIMIT = max(500, CONFIG.article_text_token_limit)
TOTAL_ARTICLE_SUMMARY_CAP = max(0, CONFIG.total_article_summary_cap)
MODEL_IS_GEMMA_4 = is_gemma_4_model_reference(MODEL_REFERENCE)
TOTAL_ARTICLE_SUMMARY_CAP_GEMMA_4_DERIVED = CONFIG.total_article_summary_cap_gemma_4_derived
ARTICLE_SUMMARY_MAX_TOKENS = max(100, CONFIG.article_summary_max_tokens)
STORY_DRAFTING_MAX_TOKENS = max(100, CONFIG.story_drafting_max_tokens)

MIN_ARTICLES_PER_STORY = max(2, CONFIG.min_articles_per_story)
STORY_SCALE_SCREENING_ENABLED = CONFIG.story_scale_screening_enabled
MAX_STORIES = max(1, CONFIG.max_stories)
STORY_CLUSTER_SIMILARITY_THRESHOLD = min(
    1.0,
    max(0.0, CONFIG.story_cluster_similarity_threshold),
)
STORY_SELECTION_OVERLAP_THRESHOLD = CONFIG.story_selection_overlap_threshold
STORY_EMBEDDING_DEDUP_THRESHOLD = CONFIG.story_embedding_dedup_threshold
STORY_BACKFILL_BATCH_MULTIPLIER = max(1, CONFIG.story_backfill_batch_multiplier)
IMAGE_GENERATION_ENABLED = CONFIG.image_generation_enabled
IMAGE_GENERATION_FAIL_ON_ERROR = CONFIG.image_generation_fail_on_error
IMAGE_WIDTH = max(256, CONFIG.image_width)
IMAGE_HEIGHT = max(256, CONFIG.image_height)
IMAGE_STEPS = max(1, CONFIG.image_steps)
IMAGE_CROP_BOTTOM_RATIO = min(max(CONFIG.image_crop_bottom_ratio, 0.0), 0.35)
IMAGE_MODEL_ID = CONFIG.image_model_id
IMAGE_BASE_MODEL = CONFIG.image_base_model
IMAGE_MODEL_LABEL = IMAGE_MODEL_ID.split("/")[-1] if "/" in IMAGE_MODEL_ID else IMAGE_MODEL_ID
MODEL_DEFAULT_SAMPLING = MODEL_TUNING.task_sampling.get("default", ModelSamplingSettings())
MODEL_REASONING_SAMPLING = MODEL_TUNING.task_sampling.get("reasoning", ModelSamplingSettings())
MODEL_TASK_SAMPLING = MODEL_TUNING.task_sampling
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
ACTIVE_RUN_FINALIZER: RunFinalizer | None = None
MANAGED_MODEL_SERVER_ACTIVE = False
MANAGED_MODEL_SERVER_READY = False
MANAGED_MODEL_SERVER_READY_LOCK = Lock()
MANAGED_MODEL_SERVER_EXTERNAL = False
MANAGED_MODEL_SERVER_PROCESS: subprocess.Popen | None = None
MANAGED_MODEL_SERVER_LOG_FILE: TextIO | None = None
MANAGED_MODEL_SERVER_EXIT_RECORDED = False


class ManagedModelServerExited(RuntimeError):
    """Raised when a managed local model server dies during a run."""


def _text_file_tail(path: str, *, max_chars: int = 3000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[-max_chars:].strip()
    except FileNotFoundError:
        return ""
    except Exception as error:
        return f"Could not read {path}: {error}"


def _managed_model_server_exit_message(exit_code: int, log_tail: str) -> str:
    message = (
        f"Managed model server exited unexpectedly with code {exit_code}. "
        f"See {_managed_model_server_log_path()}."
    )
    if "OutOfMemory" in log_tail or "Insufficient Memory" in log_tail:
        message += " The server log indicates Metal/GPU insufficient memory."
    if log_tail:
        message += f"\n\nManaged model server log tail:\n{log_tail}"
    return message


def _raise_if_managed_model_server_exited() -> None:
    global MANAGED_MODEL_SERVER_EXIT_RECORDED
    global MANAGED_MODEL_SERVER_READY
    if (
        not MANAGED_MODEL_SERVER_ACTIVE
        or MANAGED_MODEL_SERVER_EXTERNAL
        or MANAGED_MODEL_SERVER_PROCESS is None
    ):
        return

    exit_code = MANAGED_MODEL_SERVER_PROCESS.poll()
    if exit_code is None:
        return

    MANAGED_MODEL_SERVER_READY = False
    log_path = _managed_model_server_log_path()
    log_tail = _text_file_tail(log_path)
    if not MANAGED_MODEL_SERVER_EXIT_RECORDED:
        MANAGED_MODEL_SERVER_EXIT_RECORDED = True
        if ACTIVE_RUN_DIAGNOSTICS is not None:
            ACTIVE_RUN_DIAGNOSTICS.event(
                "managed_model_server_exited",
                exit_code=exit_code,
                log_path=log_path,
                log_tail=log_tail,
            )
        record_activity_snapshot("after_model_server_unexpected_exit", ACTIVE_RUN_DIAGNOSTICS)
    raise ManagedModelServerExited(_managed_model_server_exit_message(exit_code, log_tail))


def _read_url_file(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


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




def _budget_article_targets_for_summary(
    article_targets: list[dict],
    story_records: list[dict],
    *,
    total_cap: int,
    gemma_4_derived: bool,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    candidate_count = len(article_targets)
    cap = max(0, int(total_cap or 0))
    stats: dict[str, Any] = {
        "enabled": cap > 0,
        "reason": (
            "Gemma 4 summary cap applied after global story clustering"
            if cap > 0 and gemma_4_derived
            else (
                "story-first summary cap applied after global story clustering"
                if cap > 0
                else "story-first pipeline summarizes every viable global story cluster before final selection"
            )
        ),
        "candidate_count": candidate_count,
        "included_count": candidate_count,
        "dropped_count": 0,
        "total_cap": cap,
        "gemma_4_derived": bool(gemma_4_derived),
    }
    if cap <= 0 or candidate_count <= cap:
        return article_targets, story_records, stats

    article_lookup = {
        str(article.get("article_id") or "").strip(): article
        for article in article_targets
        if str(article.get("article_id") or "").strip()
    }
    selected_ids: list[str] = []
    selected_id_set: set[str] = set()
    budgeted_story_records: list[dict] = []
    skipped_story_keys: list[str] = []
    remaining = cap

    for story in story_records:
        story_ids = [
            article_id
            for article_id in story_records_stage.story_article_ids(story)
            if article_id in article_lookup
        ]
        if not story_ids:
            continue

        new_ids = [
            article_id
            for article_id in story_ids
            if article_id not in selected_id_set
        ]
        if not new_ids:
            budgeted_story_records.append(
                story_records_stage.to_story_dict(
                    story_records_stage.with_budgeted_article_ids(story, story_ids)
                )
            )
            continue

        if len(new_ids) <= remaining:
            selected_ids.extend(new_ids)
            selected_id_set.update(new_ids)
            remaining -= len(new_ids)
            budgeted_story_records.append(
                story_records_stage.to_story_dict(
                    story_records_stage.with_budgeted_article_ids(story, story_ids)
                )
            )
            continue

        if not selected_ids and len(story_ids) > cap:
            selected_ids.extend(new_ids[:cap])
            selected_id_set.update(selected_ids)
            budgeted_story_records.append(
                story_records_stage.to_story_dict(
                    story_records_stage.with_budgeted_article_ids(story, story_ids[:cap])
                )
            )
            remaining = 0
            continue

        skipped_story_keys.append(str(story.get("story_key") or story.get("story_title") or ""))

    if not selected_ids:
        selected_ids = [
            str(article.get("article_id") or "").strip()
            for article in article_targets[:cap]
            if str(article.get("article_id") or "").strip()
        ]
        selected_id_set = set(selected_ids)

    budgeted_targets = [
        article_lookup[article_id]
        for article_id in selected_ids
        if article_id in article_lookup
    ]
    dropped_ids = [
        str(article.get("article_id") or "").strip()
        for article in article_targets
        if str(article.get("article_id") or "").strip() not in selected_id_set
    ]
    stats.update(
        {
            "included_count": len(budgeted_targets),
            "dropped_count": max(0, candidate_count - len(budgeted_targets)),
            "included_story_count": len(budgeted_story_records),
            "skipped_story_keys": [key for key in skipped_story_keys if key],
            "dropped_article_ids": [article_id for article_id in dropped_ids if article_id],
        }
    )
    return budgeted_targets, budgeted_story_records or story_records, stats




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


SOURCE_FEEDS = load_sources(CONFIG.sources_path, source_scope=CONFIG.source_scope)

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


RUN_LOG_FILE: TextIO | None = None
RUN_LOG_FILES: list[TextIO] = []
ACTIVE_RUN_SESSION: "RunSession | None" = None
_RUN_SESSION_LOCK = RLock()


def _compat_runtime_values(config: RuntimeConfig) -> dict[str, Any]:
    return {
        "CONFIG": config,
        "MODEL_NAME": config.model_name,
        "MODEL_REFERENCE": config.model_reference,
        "MODEL_ASSIGNMENTS": config.model_assignments,
        "MODEL_TUNING": config.model_tuning,
        "PIPELINE_BUDGET": config.pipeline_budget,
        "MODEL_SERVER_SETTINGS": config.model_server_settings,
        "MODEL_BASE_URL": config.model_base_url,
        "MODEL_BACKEND": config.model_backend,
        "MODEL_SERVER_COMMAND": config.model_server_command,
        "PRIMARY_RECIPIENT": config.primary_recipient,
        "RECENT_WINDOW_HOURS": config.recent_window_hours,
        "MAX_ARTICLES_PER_SOURCE": config.max_articles_per_source,
        "RUN_STARTED_AT": config.run_started_at,
        "RUN_DATE": config.run_date,
        "timestamp": config.timestamp,
        "OUTPUT_DIR": str(config.output_dir),
        "RUN_OUTPUT_DIR": str(config.run_output_dir),
        "RUN_STAGING_DIR": str(config.run_staging_dir),
        "LATEST_RUN_MARKDOWN_PATH": str(config.latest_run_markdown_path),
        "LATEST_RUN_LOG_PATH": str(config.latest_run_log_path),
        "LATEST_RUN_DETAILS_PATH": str(config.latest_run_details_path),
        "HISTORY_DB_PATH": str(config.history_db_path),
        "HISTORY_EXPORT_CSV": config.history_export_csv,
        "PRESET_ID": config.preset_id,
        "SOURCE_SCOPE": config.source_scope,
        "RECIPIENT_SCOPE": config.recipient_scope,
        "URL_REUSE_BLOCKING_ENABLED": config.url_reuse_blocking_enabled,
        "RELAXED_STORY_DRAFTING_GUARDS": config.relaxed_story_drafting_guards,
        "RUN_USED_URLS_PATH": str(config.run_used_urls_path),
        "RUN_LOG_PATH": os.path.join(str(config.run_output_dir), f"run_log_{config.timestamp}.log"),
        "EMAIL_RECIPIENTS_FALLBACK": config.email_recipients_fallback,
        "EMAIL_FROM": config.email_from,
        "SMTP_HOST": config.smtp_host,
        "SMTP_PORT": config.smtp_port,
        "SMTP_USERNAME": config.smtp_username,
        "SMTP_USE_SSL": config.smtp_use_ssl,
        "SMTP_PASSWORD": config.smtp_password,
        "UNSUBSCRIBE_BASE_URL": config.unsubscribe_base_url,
        "UNSUBSCRIBE_HOST": config.unsubscribe_host,
        "UNSUBSCRIBE_PORT": config.unsubscribe_port,
        "UNSUBSCRIBE_SECRET": config.unsubscribe_secret,
        "MODEL_MAX_INPUT_TOKENS": config.model_max_input_tokens,
        "TOKEN_ENCODING_NAME": config.token_encoding_name,
        "MODEL_CONCURRENCY": max(1, config.model_concurrency),
        "ARTICLE_SUMMARY_CONCURRENCY": max(1, config.article_summary_concurrency),
        "STORY_SYNTHESIS_CONCURRENCY": max(1, config.story_synthesis_concurrency),
        "SOURCE_COLLECTION_CONCURRENCY": max(1, config.source_collection_concurrency),
        "ARTICLE_TEXT_TOKEN_LIMIT": max(500, config.article_text_token_limit),
        "TOTAL_ARTICLE_SUMMARY_CAP": max(0, config.total_article_summary_cap),
        "MODEL_IS_GEMMA_4": is_gemma_4_model_reference(config.model_reference),
        "TOTAL_ARTICLE_SUMMARY_CAP_GEMMA_4_DERIVED": config.total_article_summary_cap_gemma_4_derived,
        "ARTICLE_SUMMARY_MAX_TOKENS": max(100, config.article_summary_max_tokens),
        "STORY_DRAFTING_MAX_TOKENS": max(100, config.story_drafting_max_tokens),
        "MIN_ARTICLES_PER_STORY": max(2, config.min_articles_per_story),
        "STORY_SCALE_SCREENING_ENABLED": config.story_scale_screening_enabled,
        "MAX_STORIES": max(1, config.max_stories),
        "STORY_CLUSTER_SIMILARITY_THRESHOLD": min(
            1.0,
            max(0.0, config.story_cluster_similarity_threshold),
        ),
        "STORY_SELECTION_OVERLAP_THRESHOLD": config.story_selection_overlap_threshold,
        "STORY_EMBEDDING_DEDUP_THRESHOLD": config.story_embedding_dedup_threshold,
        "STORY_BACKFILL_BATCH_MULTIPLIER": max(1, config.story_backfill_batch_multiplier),
        "IMAGE_GENERATION_ENABLED": config.image_generation_enabled,
        "IMAGE_GENERATION_FAIL_ON_ERROR": config.image_generation_fail_on_error,
        "IMAGE_WIDTH": max(256, config.image_width),
        "IMAGE_HEIGHT": max(256, config.image_height),
        "IMAGE_STEPS": max(1, config.image_steps),
        "IMAGE_CROP_BOTTOM_RATIO": min(max(config.image_crop_bottom_ratio, 0.0), 0.35),
        "IMAGE_MODEL_ID": config.image_model_id,
        "IMAGE_BASE_MODEL": config.image_base_model,
        "IMAGE_MODEL_LABEL": (
            config.image_model_id.split("/")[-1]
            if "/" in config.image_model_id
            else config.image_model_id
        ),
        "MODEL_DEFAULT_SAMPLING": config.model_tuning.task_sampling.get("default", ModelSamplingSettings()),
        "MODEL_REASONING_SAMPLING": config.model_tuning.task_sampling.get("reasoning", ModelSamplingSettings()),
        "MODEL_TASK_SAMPLING": config.model_tuning.task_sampling,
        "SOURCE_FEEDS": load_sources(config.sources_path, source_scope=config.source_scope),
    }


class RunSession:
    """Owns the state and lifecycle for one daily news run."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        progress: Any | None = None,
    ) -> None:
        self.config = config
        self.progress = progress or progress_tracker
        self.diagnostics: RunDiagnostics | None = None
        self.finalizer: RunFinalizer | None = None
        self.model_call_stats: dict[str, Any] = {
            "calls": {},
            "token_usage": {},
            "retries": 0,
            "fallbacks": 0,
            "failures": {},
        }
        self.activity_snapshots: list[dict[str, Any]] = []
        self.run_log_file: TextIO | None = None
        self.run_log_files: list[TextIO] = []
        self.managed_model_server_active = False
        self.managed_model_server_ready = False
        self.managed_model_server_external = False
        self.managed_model_server_process: subprocess.Popen | None = None
        self.managed_model_server_log_file: TextIO | None = None
        self.managed_model_server_exit_recorded = False

    def run(self, run_impl: Callable[[], None] | None = None) -> None:
        implementation = run_impl or _run_pipeline
        with self._activate():
            with run_logging():
                try:
                    with managed_model_server():
                        implementation()
                except Exception as error:
                    traceback_text = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                    progress_tracker.step("finalize", "Daily news run failed. See the run log for details.")
                    progress_tracker.detail(f"Run failed: {type(error).__name__}: {error}")
                    _write_run_log(traceback_text)
                    _finalize_failed_run(error, traceback_text, self.config)
                    raise
                else:
                    progress_tracker.finish("done")

    @contextmanager
    def _activate(self):
        global ACTIVE_RUN_SESSION
        global progress_tracker
        with _RUN_SESSION_LOCK:
            if ACTIVE_RUN_SESSION is not None:
                raise RuntimeError("Another daily news run session is already active in this process.")
            runtime_values = _compat_runtime_values(self.config)
            names = {
                *list(runtime_values),
                "ACTIVE_RUN_DIAGNOSTICS",
                "ACTIVE_RUN_FINALIZER",
                "MODEL_CALL_STATS",
                "RUN_ACTIVITY_SNAPSHOTS",
                "RUN_LOG_FILE",
                "RUN_LOG_FILES",
                "MANAGED_MODEL_SERVER_ACTIVE",
                "MANAGED_MODEL_SERVER_READY",
                "MANAGED_MODEL_SERVER_EXTERNAL",
                "MANAGED_MODEL_SERVER_PROCESS",
                "MANAGED_MODEL_SERVER_LOG_FILE",
                "MANAGED_MODEL_SERVER_EXIT_RECORDED",
                "progress_tracker",
            }
            previous = {name: globals().get(name) for name in names}
            globals().update(runtime_values)
            progress_tracker = self.progress
            self._sync_to_legacy_globals()
            ACTIVE_RUN_SESSION = self
            try:
                yield
            finally:
                self._capture_from_legacy_globals()
                ACTIVE_RUN_SESSION = None
                globals().update(previous)

    def _sync_to_legacy_globals(self) -> None:
        # ADR 0002 keeps run state on the session; ACTIVE_RUN_FINALIZER is the
        # compatibility handle used by normal and failed-run finalization helpers.
        globals().update(
            {
                "ACTIVE_RUN_DIAGNOSTICS": self.diagnostics,
                "ACTIVE_RUN_FINALIZER": self.finalizer,
                "MODEL_CALL_STATS": self.model_call_stats,
                "RUN_ACTIVITY_SNAPSHOTS": self.activity_snapshots,
                "RUN_LOG_FILE": self.run_log_file,
                "RUN_LOG_FILES": self.run_log_files,
                "MANAGED_MODEL_SERVER_ACTIVE": self.managed_model_server_active,
                "MANAGED_MODEL_SERVER_READY": self.managed_model_server_ready,
                "MANAGED_MODEL_SERVER_EXTERNAL": self.managed_model_server_external,
                "MANAGED_MODEL_SERVER_PROCESS": self.managed_model_server_process,
                "MANAGED_MODEL_SERVER_LOG_FILE": self.managed_model_server_log_file,
                "MANAGED_MODEL_SERVER_EXIT_RECORDED": self.managed_model_server_exit_recorded,
            }
        )

    def _capture_from_legacy_globals(self) -> None:
        self.diagnostics = ACTIVE_RUN_DIAGNOSTICS
        self.finalizer = ACTIVE_RUN_FINALIZER
        self.model_call_stats = MODEL_CALL_STATS
        self.activity_snapshots = RUN_ACTIVITY_SNAPSHOTS
        self.run_log_file = RUN_LOG_FILE
        self.run_log_files = RUN_LOG_FILES
        self.managed_model_server_active = MANAGED_MODEL_SERVER_ACTIVE
        self.managed_model_server_ready = MANAGED_MODEL_SERVER_READY
        self.managed_model_server_external = MANAGED_MODEL_SERVER_EXTERNAL
        self.managed_model_server_process = MANAGED_MODEL_SERVER_PROCESS
        self.managed_model_server_log_file = MANAGED_MODEL_SERVER_LOG_FILE
        self.managed_model_server_exit_recorded = MANAGED_MODEL_SERVER_EXIT_RECORDED


def _clean_progress_message(message: str) -> str:
    clean = re.sub(r"^\[progress\]\s*", "", str(message or "").strip())
    clean = clean.replace("--- [EMAIL]:", "[email]").replace("--- [UNSUBSCRIBE]:", "[unsubscribe]")
    return clean


def _write_run_log(message: str) -> None:
    if not RUN_LOG_FILES:
        return
    timestamp_label = datetime.now().isoformat(timespec="seconds")
    clean = _clean_progress_message(message).replace("\r", "\n").strip()
    if not clean:
        return
    for log_file in RUN_LOG_FILES:
        for line in clean.splitlines():
            log_file.write(f"{timestamp_label} {line.rstrip()}\n")
        log_file.flush()


class ProgressTracker:
    STEP_ORDER = [
        "setup",
        "sources",
        "clustering",
        "model",
        "summaries",
        "story_drafting",
        "story_selection",
        "report",
        "finalize",
    ]
    STEP_LABELS = {
        "model": "model",
        "setup": "setup",
        "sources": "sources",
        "stories": "stories",
        "clustering": "clustering",
        "summaries": "article summaries",
        "story_drafting": "story drafting",
        "story_selection": "story selection",
        "report": "report",
        "finalize": "finalize",
    }

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        show_meter_detail: bool = False,
    ) -> None:
        self.stream = stream
        self.show_meter_detail = show_meter_detail
        self._lock = RLock()
        self.current_step = ""
        self.meter_total = 0
        self.meter_done = 0
        self.meter_unit = ""
        self.meter_detail = ""
        self.last_render = ""
        self._line_active = False
        self._source_worker_count = 0
        self._source_candidate_articles = 0
        self._source_fresh_articles: int | None = None
        self._story_drafts_valid = 0
        self._story_drafts_rejected = 0

    def step(self, step_key: str, message: str, *, log_detail: str | None = None) -> None:
        with self._lock:
            self.current_step = step_key
            self.meter_total = 0
            self.meter_done = 0
            self.meter_unit = ""
            self.meter_detail = ""
            self._finish_active_line_locked()
            line = f"{self._step_prefix(step_key)} {message}"
            self._print_terminal_line_locked(line)
            _write_run_log(line)
        if log_detail:
            self.detail(log_detail)

    def detail(self, message: str) -> None:
        _write_run_log(message)

    def log(self, message: str, *, terminal: bool = True) -> None:
        clean = _clean_progress_message(message)
        if terminal:
            with self._lock:
                self._finish_active_line_locked()
                self._print_terminal_line_locked(clean)
        _write_run_log(clean)

    def start_meter(
        self,
        step_key: str,
        *,
        total: int,
        unit: str,
        detail: str = "",
        done: int = 0,
    ) -> None:
        with self._lock:
            self.current_step = step_key
            self.meter_total = max(0, total)
            self.meter_done = max(0, min(self.meter_total, done))
            self.meter_unit = unit
            self.meter_detail = self._compact_detail(detail)
            self.last_render = ""
            if self.meter_total:
                self._render_meter_locked(force=True)
            else:
                self.step(step_key, f"No {unit} selected.")

    def update_meter(
        self,
        *,
        done: int | None = None,
        total: int | None = None,
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            if total is not None:
                self.meter_total = max(0, total)
            if done is not None:
                upper_bound = self.meter_total if self.meter_total > 0 else done
                self.meter_done = max(0, min(upper_bound, done))
            if detail is not None:
                self.meter_detail = self._compact_detail(detail)
            self._render_meter_locked(force=force)

    def advance_meter(self, count: int = 1, *, detail: str | None = None, force: bool = False) -> None:
        with self._lock:
            if self.meter_total <= 0:
                return
            self.meter_done = min(self.meter_total, self.meter_done + max(0, count))
            if detail is not None:
                self.meter_detail = self._compact_detail(detail)
            self._render_meter_locked(force=force)

    def finish_meter(self, *, detail: str | None = None) -> None:
        with self._lock:
            if self.meter_total <= 0:
                self._finish_active_line_locked()
                return
            self.meter_done = self.meter_total
            if detail is not None:
                self.meter_detail = self._compact_detail(detail)
            self._render_meter_locked(force=True, final=True)

    def reset(self, *, total_sources: int) -> None:
        self._source_worker_count = 0
        self._source_candidate_articles = 0
        self._source_fresh_articles = None
        self.start_meter("sources", total=total_sources, unit="sources")

    def start_source(self, source_index: int, source_name: str | None = None) -> None:
        if self.current_step != "sources":
            self.current_step = "sources"
        self.update_meter(done=max(0, min(self.meter_total, source_index - 1)))
        if source_name:
            self.detail(f"Starting source {source_index}/{self.meter_total}: {source_name}")

    def set_source_article_total(self, total_articles: int) -> None:
        self._source_fresh_articles = max(0, total_articles)
        self.update_meter(detail=self._source_detail())

    def source_completed(
        self,
        source_name: str | None = None,
        *,
        candidate_articles: int = 0,
        worker_count: int | None = None,
    ) -> None:
        if worker_count is not None:
            self._source_worker_count = max(0, worker_count)
        self._source_candidate_articles += max(0, candidate_articles)
        if self.current_step != "sources":
            self.current_step = "sources"
        if self.meter_total > 0:
            self.advance_meter(detail=self._source_detail(latest_source=source_name))

    def update_source_fresh_articles(self, total_articles: int, *, latest_source: str | None = None) -> None:
        self._source_fresh_articles = max(0, total_articles)
        self.update_meter(detail=self._source_detail(latest_source=latest_source))

    def article_completed(self, article: dict | None = None) -> None:
        title = str((article or {}).get("title") or "").strip()
        source = str((article or {}).get("source_display_name") or (article or {}).get("source") or "").strip()
        latest = " - ".join(part for part in (source, title) if part)
        detail = f"latest: {latest}" if latest else None
        if self.current_step == "summaries" and self.meter_total > 0:
            self.advance_meter(detail=detail)

    def start_article_summary(self, total_articles: int) -> None:
        self.start_meter("summaries", total=total_articles, unit="articles")

    def start_story_clustering(self, total_work: int, *, detail: str = "") -> None:
        self.start_meter("clustering", total=total_work, unit="steps", detail=detail)

    def story_clustering_progress(self, event: str, payload: dict[str, Any]) -> None:
        del event
        phase = str(payload.get("phase") or "").strip()
        done = int(payload.get("done") or 0)
        total = int(payload.get("total") or 0)
        detail_parts = []
        if phase:
            detail_parts.append(phase)
        if payload.get("linked_pairs") is not None:
            detail_parts.append(f"{int(payload.get('linked_pairs') or 0)} linked pairs")
        if payload.get("candidate_components") is not None:
            detail_parts.append(f"{int(payload.get('candidate_components') or 0)} candidate components")
        self.update_meter(done=done, total=total, detail=" | ".join(detail_parts))

    def start_story_drafting(self, total_stories: int) -> None:
        self._story_drafts_valid = 0
        self._story_drafts_rejected = 0
        self.start_meter("story_drafting", total=total_stories, unit="stories")

    def story_draft_completed(self, story: dict[str, Any]) -> None:
        if story.get("valid"):
            self._story_drafts_valid += 1
        else:
            self._story_drafts_rejected += 1
        title = str(story.get("story_title") or story.get("story_headline") or "story").strip()
        detail = (
            f"latest: {title} | valid {self._story_drafts_valid} | "
            f"rejected {self._story_drafts_rejected}"
        )
        self.advance_meter(detail=detail)

    def story_selection_progress(self, event: str, payload: dict[str, Any]) -> None:
        total = int(payload.get("total") or payload.get("candidate_count") or self.meter_total or 0)
        done = int(payload.get("done") or 0)
        if event == "scale_screening_started":
            if self.current_step == "story_selection" and self.meter_total > 0:
                self.update_meter(done=0, total=total, detail="scale screening")
            else:
                self.start_meter("story_selection", total=total, unit="stories", detail="scale screening")
            return
        if event == "scale_screening_batch_completed":
            detail = (
                f"scale screening | kept {int(payload.get('kept_count') or 0)} | "
                f"fallback {int(payload.get('fallback_count') or 0)}"
            )
            self.update_meter(done=done, total=total, detail=detail)

    def retrying(
        self,
        task_name: str,
        attempt: int,
        attempts: int,
        delay: int,
        error: Exception | None = None,
    ) -> None:
        error_detail = f" ({type(error).__name__}: {error})" if error else ""
        self.detail(
            f"Retrying {task_name}: attempt {attempt}/{attempts} failed{error_detail}; "
            f"sleeping {delay}s before the next attempt."
        )

    def retry(
        self,
        task_name: str,
        attempt: int,
        attempts: int,
        delay: int,
        error: Exception | None = None,
    ) -> None:
        self.retrying(task_name, attempt, attempts, delay, error)

    def warning(self, label: str) -> None:
        self.detail(f"WARNING: {label}")

    def set_final_step(self, step_name: str, step_index: int) -> None:
        messages = {
            "reports": "Preparing report inputs.",
            "synthesis": "Running final synthesis.",
            "art": "Generating report image.",
            "render": "Rendering report assets.",
            "email": "Sending report.",
        }
        detail = messages.get(step_name, f"Running {step_name}.")
        done = max(0, min(5, step_index))
        if self.current_step != "report" or self.meter_total <= 0:
            self.start_meter(
                "report",
                total=5,
                unit="steps",
                detail=detail,
                done=max(0, done - 1),
            )
        self.update_meter(done=done, detail=detail)

    def finish(self, label: str) -> None:
        del label
        with self._lock:
            self._finish_active_line_locked()
        self.step("finalize", "Daily news run complete.")

    def _finish_active_line(self) -> None:
        with self._lock:
            self._finish_active_line_locked()

    def _finish_active_line_locked(self) -> None:
        if self._line_active:
            self._stream().write("\n")
            self._stream().flush()
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
        with self._lock:
            self._render_meter_locked(force=force)

    def _render_meter_locked(self, *, force: bool = False, final: bool = False) -> None:
        if self.meter_total <= 0:
            return
        fill = round((self.meter_done / self.meter_total) * 20)
        fill = max(0, min(20, fill))
        bar = "#" * fill + "-" * (20 - fill)
        line = (
            f"{self._step_prefix()} [{bar}] "
            f"{self.meter_done}/{self.meter_total} {self.meter_unit}"
        )
        if self.show_meter_detail and self.meter_detail:
            line += f" | {self.meter_detail}"
        if line == self.last_render:
            if final and self._line_active:
                self._stream().write("\n")
                self._stream().flush()
                self._line_active = False
            return
        stream = self._stream()
        effective_final = final or self.meter_done >= self.meter_total
        stream.write("\r" + line + "\033[K")
        if final:
            stream.write("\n")
            self._line_active = False
        else:
            self._line_active = True
        stream.flush()
        self.last_render = line
        if force or final or effective_final:
            _write_run_log(line)

    def _source_detail(self, *, latest_source: str | None = None) -> str:
        parts = []
        if self._source_worker_count:
            parts.append(f"workers {self._source_worker_count}")
        if latest_source:
            parts.append(f"latest: {latest_source}")
        if self._source_fresh_articles is not None:
            parts.append(f"{self._source_fresh_articles} fresh articles")
        else:
            parts.append(f"{self._source_candidate_articles} candidates")
        return " | ".join(parts)

    def _stream(self) -> TextIO:
        return self.stream or sys.stdout

    def _print_terminal_line_locked(self, line: str) -> None:
        print(line, file=self._stream())
        self._stream().flush()

    @staticmethod
    def _compact_detail(detail: str | None, *, max_chars: int = 120) -> str:
        clean = re.sub(r"\s+", " ", str(detail or "")).strip()
        if len(clean) <= max_chars:
            return clean
        return clean[:max_chars].rsplit(" ", 1)[0].rstrip(" |") + "..."


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
        prompt_instructions=PROMPT_INSTRUCTIONS["article_summary"],
    )


def _story_drafting_progress(event: str, payload: dict[str, Any]) -> None:
    if event == "story_drafting_started":
        progress_tracker.start_story_drafting(int(payload.get("total") or 0))
    elif event == "story_draft_completed":
        progress_tracker.story_draft_completed(dict(payload.get("story") or {}))


def _story_drafting_runtime(
    *, min_articles_per_story: int | None = None
) -> story_drafting_stage.StoryDraftingRuntime:
    return story_drafting_stage.StoryDraftingRuntime(
        story_synthesis_concurrency=STORY_SYNTHESIS_CONCURRENCY,
        story_drafting_max_tokens=STORY_DRAFTING_MAX_TOKENS,
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
        fallback_synthesis_paragraph_from_summaries=_fallback_synthesis_paragraph_from_summaries,
        story_drafting_word_count=_story_drafting_word_count,
        progress_callback=_story_drafting_progress,
        prompt_instructions=PROMPT_INSTRUCTIONS["story_drafting"],
    )


def _story_selection_runtime() -> story_selection_stage.StorySelectionRuntime:
    return story_selection_stage.StorySelectionRuntime(
        story_scale_screening_enabled=STORY_SCALE_SCREENING_ENABLED,
        model_max_input_tokens=MODEL_MAX_INPUT_TOKENS,
        model_label=MODEL_REPORT_LABEL,
        model_reference=MODEL_REFERENCE,
        model_name=MODEL_NAME,
        model_backend=MODEL_BACKEND,
        relaxed_story_drafting_guards=RELAXED_STORY_DRAFTING_GUARDS,
        build_chat_model=build_chat_model,
        invoke_with_retries=invoke_with_retries,
        build_article_heading=_build_article_heading,
        format_article_metadata=_format_article_metadata,
        story_drafting_word_count=_story_drafting_word_count,
        is_low_confidence_report_entry=is_low_confidence_report_entry,
        report_reference_key=_report_reference_key,
        progress_callback=progress_tracker.story_selection_progress,
        prompt_instructions=PROMPT_INSTRUCTIONS["story_scale_screening"],
        story_scale_screening_max_tokens=(
            MODEL_ASSIGNMENTS[MODEL_TASK_STORY_SCALE_SCREENING].tuning.story_scale_screening_max_tokens
            or story_selection_stage.STORY_SCALE_VALIDATION_MAX_TOKENS
        ),
    )






@contextmanager
def run_logging():
    global RUN_LOG_FILE
    global RUN_LOG_FILES
    for log_path in (RUN_LOG_PATH, LATEST_RUN_LOG_PATH):
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
    with open(RUN_LOG_PATH, "w", encoding="utf-8") as run_log_file, open(
        LATEST_RUN_LOG_PATH,
        "w",
        encoding="utf-8",
    ) as latest_log_file:
        RUN_LOG_FILE = run_log_file
        RUN_LOG_FILES = [run_log_file, latest_log_file]
        header = (
            "# Daily news run log\n"
            f"# Started: {RUN_STARTED_AT.isoformat(timespec='seconds')}\n"
            f"# Preset: {PRESET_ID or 'custom'}\n"
            f"# Timestamped log: {RUN_LOG_PATH}\n"
            f"# Rolling log: {LATEST_RUN_LOG_PATH}\n\n"
        )
        for log_file in RUN_LOG_FILES:
            log_file.write(header)
            log_file.flush()
        try:
            yield
        finally:
            _write_run_log(f"Run log saved: {RUN_LOG_PATH}")
            _write_run_log(f"Rolling run log saved: {LATEST_RUN_LOG_PATH}")
            RUN_LOG_FILES = []
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
    if RECIPIENT_SCOPE == "primary":
        preferred_config = recipient_config.get(PRIMARY_RECIPIENT)
        if preferred_config:
            return {PRIMARY_RECIPIENT: preferred_config}
        return {
            PRIMARY_RECIPIENT: {
                "name": PRIMARY_RECIPIENT,
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

def _build_article_heading(article: dict) -> str:
    return article_summary_records_stage.build_article_heading(article)


def _format_article_metadata(article: dict) -> str:
    return article_summary_records_stage.format_article_metadata(article)


def build_article_fallback_entry(article: dict) -> str:
    return article_summary_records_stage.fallback_entry(article)


def _parse_feed_datetime(raw_value: str | None) -> datetime | None:
    parsed = _parse_feed_datetime_utc(raw_value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


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
        article_record = {
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
        }
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


def _fetch_source_context_for_collection(source_index: int, source_name: str) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    direct_context: dict[str, Any] | None = None
    error_reason = ""
    try:
        direct_context = get_direct_source_article_context(source_name)
    except Exception as error:
        error_reason = f"{type(error).__name__}: {error}"
    return {
        "source_index": source_index,
        "source": source_name,
        "started_at": started_at.isoformat(timespec="seconds"),
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started_perf, 3),
        "direct_context": direct_context,
        "error_reason": error_reason,
    }
# --- SYNTHESIS ---



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
        key: value
        for key, value in {
            "top_p": settings.top_p,
            "top_k": settings.top_k,
            "presence_penalty": settings.presence_penalty,
            "repetition_penalty": settings.repetition_penalty,
            "min_p": settings.min_p,
        }.items()
        if value is not None
    }


def _sampling_to_dict(settings: ModelSamplingSettings) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    if settings.temperature is not None:
        result["temperature"] = settings.temperature
    result.update(_sampling_to_extra_body(settings))
    return result


def _model_sampling_kwargs(settings: ModelSamplingSettings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature
    return kwargs


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


def _normalized_model_task(task: str) -> str:
    clean_task = str(task or "default").strip().lower().replace("-", "_")
    return clean_task or "default"


def _task_model_assignment(task: str):
    normalized_task = _normalized_model_task(task)
    if normalized_task == MODEL_TASK_ARTICLE_SUMMARY:
        return MODEL_ASSIGNMENTS[MODEL_TASK_ARTICLE_SUMMARY]
    if normalized_task == MODEL_TASK_STORY_DRAFTING:
        return MODEL_ASSIGNMENTS[MODEL_TASK_STORY_DRAFTING]
    if normalized_task == MODEL_TASK_STORY_SCALE_SCREENING:
        return MODEL_ASSIGNMENTS[MODEL_TASK_STORY_SCALE_SCREENING]
    if normalized_task == MODEL_TASK_TITLE_GENERATION:
        return MODEL_ASSIGNMENTS[MODEL_TASK_TITLE_GENERATION]
    # image_art_direction is produced by the same LLM call as title_generation
    # (generate_image_art_brief); it inherits that assignment by design.
    if normalized_task == MODEL_TASK_IMAGE_ART_DIRECTION:
        return MODEL_ASSIGNMENTS[MODEL_TASK_TITLE_GENERATION]
    # story_discovery has no LLM stage (TF-IDF/embedding clustering); it inherits default.
    return MODEL_ASSIGNMENTS["default"]


def build_chat_model(max_tokens: int, *, task: str = "default") -> ChatOpenAI:
    ensure_codex_safe_model_reference(MODEL_REFERENCE)
    normalized_task = _normalized_model_task(task)
    assignment = _task_model_assignment(normalized_task)
    if MANAGED_MODEL_SERVER_ACTIVE:
        if assignment.base_url == MODEL_BASE_URL:
            if assignment.name != MODEL_NAME:
                raise RuntimeError(
                    "Managed model server cannot serve multiple different models from the same base URL. "
                    f"Task {normalized_task!r} wants {assignment.reference!r} ({assignment.name!r}) "
                    f"but the managed main model is {MODEL_REFERENCE!r} ({MODEL_NAME!r}) at {MODEL_BASE_URL}. "
                    "Set a per-task base URL or run that task against an external server."
                )
            _ensure_main_model_server_ready()
            _raise_if_managed_model_server_exited()
    sampling = assignment.tuning.task_sampling.get(
        normalized_task,
        assignment.tuning.task_sampling.get("default", ModelSamplingSettings()),
    )
    return ChatOpenAI(
        base_url=assignment.base_url,
        api_key=MODEL_API_KEY,  # pragma: allowlist secret
        model=assignment.name,
        max_tokens=max_tokens,
        max_retries=0,
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        **_model_sampling_kwargs(sampling),
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
            _raise_if_managed_model_server_exited()
            response = llm.invoke(messages)
            if isinstance(response, AIMessage):
                _record_response_token_usage(task_name, response)
                return response
            response_message = AIMessage(content=str(getattr(response, "content", response)))
            _record_response_token_usage(task_name, response_message)
            return response_message
        except ManagedModelServerExited:
            raise
        except Exception as error:
            last_error = error
            _raise_if_managed_model_server_exited()
            if not _is_transient_model_error(error) or attempt == attempts:
                break
            delay = MODEL_RETRY_BASE_DELAY_SECONDS * attempt
            with MODEL_CALL_STATS_LOCK:
                MODEL_CALL_STATS["retries"] = int(MODEL_CALL_STATS.get("retries", 0)) + 1
            progress_tracker.retrying(task_name, attempt, attempts, delay, error)
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


def _contains_disallowed_final_markup(text: str) -> bool:
    return False


def _story_drafting_word_count(text: str) -> int:
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
    return article_summary_records_stage.has_structured_entry(text, heading_name)


def normalize_report_entry(article: dict, raw_text: str) -> str:
    return article_summary_records_stage.normalize_model_response(article, raw_text)


def is_low_confidence_report_entry(entry: str) -> bool:
    return article_summary_records_stage.is_low_confidence(entry)






def _report_reference_key(entry: str) -> str:
    return article_summary_records_stage.reference_key(entry)


def filter_reports_for_references(
    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str],
    token_stats: dict[str, Any],
) -> List[article_summary_records_stage.ArticleSummaryRecord | str]:
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
    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str],
    recipients: List[str],
    recipient_names: List[str],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
    citation_groups: list[dict[str, Any]] | None = None,
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
                html_image_art,
                citation_sources,
                citation_groups,
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


def _fallback_synthesis_paragraph_from_summaries(summaries: list[str]) -> str:
    snippets: list[str] = []
    for summary in summaries[:4]:
        snippet = _first_sentences(summary, max_sentences=2, max_chars=700)
        if snippet:
            snippets.append(snippet)
    paragraph = " ".join(snippets)
    return _first_sentences(paragraph, max_sentences=8, max_chars=1800)


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


def _build_image_art_system_prompt(image_art_direction: str, title_guidance: str) -> str:
    """Compose the image-art system prompt from the pipeline-owned JSON contract
    and the profile's editorial art-direction / title-generation sentences.
    """
    return (
        "You are preparing art direction for a text-to-image news illustration. "
        f"{IMAGE_ART_JSON_CONTRACT} "
        f"{image_art_direction} "
        f"{IMAGE_ART_OVERLAY_PROTOCOL} "
        f"{title_guidance}"
    )


def generate_image_art_brief(
    synthesis_body: str,
    report_title: str,
    *,
    prompt_instructions: dict[str, str] | None = None,
) -> dict[str, str]:
    """Ask the text model for the FLUX prompt plus a separate overlay headline.

    Consumes two prompt-catalog task slots from ``prompt_instructions``
    (``image_art_direction`` and ``title_generation``). A missing/falsy slot
    falls back to the ``balanced`` instructions for that task; a missing slot
    in an explicitly provided dict is surfaced as a progress warning.
    """
    fallback_headline = _sanitize_overlay_headline(report_title, "Daily News Brief")
    fallback_prompt = _fallback_image_prompt(synthesis_body)
    instructions = prompt_instructions or {}
    if prompt_instructions is not None and "image_art_direction" not in prompt_instructions:
        progress_tracker.warning("prompt profile missing image_art_direction; using balanced default")
    if prompt_instructions is not None and "title_generation" not in prompt_instructions:
        progress_tracker.warning("prompt profile missing title_generation; using balanced default")
    image_art_direction = instructions.get("image_art_direction") or DEFAULT_PROMPT_INSTRUCTIONS["image_art_direction"]
    title_guidance = instructions.get("title_generation") or DEFAULT_PROMPT_INSTRUCTIONS["title_generation"]
    try:
        llm = build_chat_model(
            # Defensive fallback: tuning is always seeded, but a 0-valued env
            # override would otherwise produce a zero-token cap. The fallback
            # references the config constant so a default change propagates.
            max_tokens=(
                MODEL_ASSIGNMENTS[MODEL_TASK_TITLE_GENERATION].tuning.title_generation_max_tokens
                or DEFAULT_TITLE_GENERATION_MAX_TOKENS
            ),
            task=MODEL_TASK_TITLE_GENERATION,
        )
        response = invoke_with_retries(
            llm,
            [
                SystemMessage(content=_build_image_art_system_prompt(image_art_direction, title_guidance)),
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
            "Install/sync mflux or run with `uv run --with \"mflux>=0.16.0\" news run --preset NAME`."
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

    art_brief = generate_image_art_brief(
        synthesis_body,
        report_title,
        prompt_instructions=PROMPT_INSTRUCTIONS,
    )
    image_prompt = art_brief["image_prompt"]
    overlay_headline = art_brief["overlay_headline"]
    base_path = os.path.splitext(report_path)[0]
    raw_temp_dir = tempfile.TemporaryDirectory(prefix="news-raw-image-")
    raw_image_path = os.path.join(raw_temp_dir.name, "raw.png")
    final_image_path = f"{base_path}_image.png"
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
            "final_image_path": final_image_path,
        }
        return {
            **stats,
            "image_prompt": image_prompt,
            "data_uri": f"data:image/png;base64,{encoded_image}",
            "art_prompt_error": art_brief.get("error"),
        }
    except Exception as error:
        message = f"Image generation failed: {error}"
        progress_tracker.warning(message)
        if IMAGE_GENERATION_FAIL_ON_ERROR:
            raise
        return {
            "error": message,
            "backend": "mflux",
            "model": IMAGE_MODEL_LABEL,
            "model_id": IMAGE_MODEL_ID,
            "base_model": IMAGE_BASE_MODEL,
            "overlay_headline": overlay_headline,
            "image_prompt": image_prompt,
        }
    finally:
        raw_temp_dir.cleanup()


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
    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str],
) -> dict[str, List[tuple[str, str | None, str | None, str | None]]]:
    grouped_headlines: dict[str, List[tuple[str, str | None, str | None, str | None]]] = {}
    seen_pairs: set[tuple[str, str, str]] = set()
    for entry in final_reports:
        record = article_summary_records_stage.ensure_record(entry)
        source_name = record.source or "Unknown source"
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
        title_text = record.title or "Untitled article"
        url_text = record.url
        story_title = record.story
        normalized_url = url_text if url_text and url_text != "N/A" else ""
        dedupe_key = (source_name, title_text, normalized_url)
        if dedupe_key in seen_pairs:
            continue
        seen_pairs.add(dedupe_key)

        grouped_headlines.setdefault(display_name, []).append(
            (title_text, normalized_url or None, homepage_url, story_title or None)
        )

    return grouped_headlines


def _build_plain_text_article_listing(final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str]) -> str:
    grouped_headlines = _collect_grouped_headlines(final_reports)
    grouped_sections: list[str] = []

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


def _build_html_article_listing(final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str]) -> str:
    grouped_headlines = _collect_grouped_headlines(final_reports)

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
    html_sections.append(render_source_entries(grouped_headlines))

    if not any(section.strip() for section in html_sections):
        return "<p style=\"margin:0; font-size:15px; line-height:1.6; color:#4b5563;\">No article headlines available.</p>"

    return "".join(html_sections)


def build_report_body(
    report_title: str,
    synthesis_body: str,
    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
    citation_groups: list[dict[str, Any]] | None = None,
) -> str:
    cleaned_synthesis_body = _format_plain_text_synthesis(synthesis_body)
    clean_citation_sources = citation_sources or []
    clean_citation_groups = citation_groups or []
    article_listing = _build_plain_text_article_listing(final_reports)
    citation_listing = citations_stage.render_plain_text_sources(
        clean_citation_sources,
        clean_citation_groups,
    )
    image_section = ""
    if image_art:
        image_lines = ["IMAGE", "=====", ""]
        if image_art.get("final_image_path"):
            image_lines.append(f"Generated image: {image_art.get('final_image_path')}")
        if image_art.get("overlay_headline"):
            image_lines.append(f"Overlay headline: {image_art.get('overlay_headline')}")
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
    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str],
    image_art: dict[str, Any] | None = None,
    citation_sources: list[dict[str, Any]] | None = None,
    citation_groups: list[dict[str, Any]] | None = None,
) -> str:
    first_name = _extract_first_name(recipient_name)
    clean_citation_sources = citation_sources or []
    clean_citation_groups = citation_groups or []
    synthesis_html = _build_html_synthesis(synthesis_body, clean_citation_sources)
    article_listing_html = _build_html_article_listing(final_reports)
    source_listing_html = (
        citations_stage.render_html_sources(clean_citation_sources, clean_citation_groups)
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


def _report_entry_debug_record(entry: article_summary_records_stage.ArticleSummaryRecord | str, index: int) -> dict[str, Any]:
    return article_summary_records_stage.to_history_record(
        article_summary_records_stage.ensure_record(entry),
        index,
    )


def _report_entry_debug_records(entries: List[article_summary_records_stage.ArticleSummaryRecord | str]) -> list[dict[str, Any]]:
    return [
        _report_entry_debug_record(entry, index)
        for index, entry in enumerate(entries, start=1)
    ]


def _dedupe_story_drafts_for_global_selection(
    story_drafts: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    drafts_before = len(story_drafts)
    try:
        deduped_story_drafts = embeddings_stage.dedup_story_drafts(
            story_drafts,
            threshold=STORY_EMBEDDING_DEDUP_THRESHOLD,
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






def _new_run_diagnostics(source_count: int) -> RunDiagnostics:
    diagnostics = RunDiagnostics(
        run_started_at=RUN_STARTED_AT.isoformat(timespec="seconds"),
        settings={
            "preset_id": PRESET_ID or "custom",
            "source_scope": SOURCE_SCOPE,
            "recipient_scope": RECIPIENT_SCOPE,
            "url_reuse_blocking_enabled": URL_REUSE_BLOCKING_ENABLED,
            "relaxed_story_drafting_guards": RELAXED_STORY_DRAFTING_GUARDS,
            "source_count": source_count,
            "sources_path": str(CONFIG.sources_path),
            "recipients_path": str(CONFIG.recipients_path),
            "output_dir": OUTPUT_DIR,
            "run_staging_dir": RUN_STAGING_DIR,
            "latest_run_markdown_path": LATEST_RUN_MARKDOWN_PATH,
            "latest_run_log_path": LATEST_RUN_LOG_PATH,
            "latest_run_details_path": LATEST_RUN_DETAILS_PATH,
            "history_db_path": HISTORY_DB_PATH,
            "history_export_csv": HISTORY_EXPORT_CSV,
            "run_used_urls_path": RUN_USED_URLS_PATH,
            "run_log_path": RUN_LOG_PATH,
            "recent_window_hours": RECENT_WINDOW_HOURS,
            "article_download_timeout_seconds": ARTICLE_DOWNLOAD_TIMEOUT_SECONDS,
            "article_scrape_total_timeout_seconds": ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS,
            "slow_source_warning_seconds": SLOW_SOURCE_WARNING_SECONDS,
            "source_collection_concurrency": SOURCE_COLLECTION_CONCURRENCY,
            "max_articles_per_source": MAX_ARTICLES_PER_SOURCE,
            "max_stories": MAX_STORIES,
            "story_selection_overlap_threshold": STORY_SELECTION_OVERLAP_THRESHOLD,
            "story_embedding_dedup_threshold": STORY_EMBEDDING_DEDUP_THRESHOLD,
            "story_scale_screening_enabled": STORY_SCALE_SCREENING_ENABLED,
            "model": MODEL_REFERENCE,
            "model_name": MODEL_NAME,
            "model_is_gemma_4": MODEL_IS_GEMMA_4,
            "model_base_url": MODEL_BASE_URL,
            "model_backend": MODEL_BACKEND,
            "model_concurrency": MODEL_CONCURRENCY,
            "model_concurrency_source": "derived_from_model_stage_concurrency",
            "model_server_command": MODEL_SERVER_COMMAND,
            "model_assignments": _json_ready(MODEL_ASSIGNMENTS),
            "model_tuning": _json_ready(MODEL_TUNING),
            "pipeline_budget": _json_ready(PIPELINE_BUDGET),
            "model_server_settings": _json_ready(MODEL_SERVER_SETTINGS),
            "model_max_input_tokens": MODEL_MAX_INPUT_TOKENS,
            "model_default_sampling": _sampling_to_dict(MODEL_DEFAULT_SAMPLING),
            "model_reasoning_sampling": _sampling_to_dict(MODEL_REASONING_SAMPLING),
            "model_task_sampling": _task_sampling_to_dict(),
            "pipeline_concurrency": MODEL_CONCURRENCY,
            "article_summary_concurrency": ARTICLE_SUMMARY_CONCURRENCY,
            "story_synthesis_concurrency": STORY_SYNTHESIS_CONCURRENCY,
            "article_text_token_limit": ARTICLE_TEXT_TOKEN_LIMIT,
            "total_article_summary_cap": TOTAL_ARTICLE_SUMMARY_CAP,
            "total_article_summary_cap_gemma_4_derived": TOTAL_ARTICLE_SUMMARY_CAP_GEMMA_4_DERIVED,
            "min_articles_per_story": MIN_ARTICLES_PER_STORY,
            "story_backfill_batch_multiplier": STORY_BACKFILL_BATCH_MULTIPLIER,
            "story_cluster_similarity_threshold": STORY_CLUSTER_SIMILARITY_THRESHOLD,
            "article_summary_max_tokens": ARTICLE_SUMMARY_MAX_TOKENS,
            "story_drafting_max_tokens": STORY_DRAFTING_MAX_TOKENS,
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


def _model_call_stats_snapshot() -> dict[str, Any]:
    with MODEL_CALL_STATS_LOCK:
        return json.loads(json.dumps(MODEL_CALL_STATS))


def _new_run_finalizer(diagnostics: RunDiagnostics, config: RuntimeConfig) -> RunFinalizer:
    return RunFinalizer(
        diagnostics=diagnostics,
        config=RunFinalizerConfig(
            run_id=config.timestamp,
            latest_run_details_path=config.latest_run_details_path,
            latest_run_markdown_path=config.latest_run_markdown_path,
            latest_run_log_path=config.latest_run_log_path,
            history_db_path=config.history_db_path,
            beehiiv_paste_dir=config.output_dir.parent / "beehiiv",
            output_dir=config.output_dir,
            run_log_path=os.path.join(str(config.run_output_dir), f"run_log_{config.timestamp}.log"),
            history_export_csv=config.history_export_csv,
        ),
        adapters=RunFinalizerAdapters(
            attach_pending_activity_snapshots=_attach_pending_activity_snapshots,
            model_call_stats_snapshot=_model_call_stats_snapshot,
            progress=progress_tracker,
        ),
    )


def _active_run_finalizer(diagnostics: RunDiagnostics, config: RuntimeConfig) -> RunFinalizer:
    global ACTIVE_RUN_FINALIZER
    global ACTIVE_RUN_DIAGNOSTICS
    if ACTIVE_RUN_SESSION is not None:
        ACTIVE_RUN_DIAGNOSTICS = diagnostics
        ACTIVE_RUN_SESSION.diagnostics = diagnostics
        if (
            ACTIVE_RUN_SESSION.finalizer is None
            or ACTIVE_RUN_SESSION.finalizer.diagnostics is not diagnostics
        ):
            ACTIVE_RUN_SESSION.finalizer = _new_run_finalizer(diagnostics, config)
        ACTIVE_RUN_FINALIZER = ACTIVE_RUN_SESSION.finalizer
        return ACTIVE_RUN_SESSION.finalizer
    if ACTIVE_RUN_FINALIZER is None or ACTIVE_RUN_FINALIZER.diagnostics is not diagnostics:
        ACTIVE_RUN_FINALIZER = _new_run_finalizer(diagnostics, config)
    return ACTIVE_RUN_FINALIZER


def _finish_run_diagnostics(diagnostics: RunDiagnostics, config: RuntimeConfig) -> None:
    _active_run_finalizer(diagnostics, config).finish()


def _model_auth_headers() -> dict[str, str]:
    """Authorization headers for OpenAI-compatible endpoint requests.

    Only sent when a real NEWS_MODEL_API_KEY is configured; the default
    "not-needed" sentinel keeps unauthenticated local servers working.
    """
    if MODEL_API_KEY and MODEL_API_KEY != "not-needed":
        return {"Authorization": f"Bearer {MODEL_API_KEY}"}
    return {}


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
    headers = _model_auth_headers()
    try:
        response = requests.get(models_url, timeout=5, headers=headers)
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
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def preflight_model_server() -> dict[str, Any]:
    return _preflight_openai_model_server(
        base_url=MODEL_BASE_URL,
        model_name=MODEL_NAME,
        model_reference=MODEL_REFERENCE,
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
    headers = _model_auth_headers()
    try:
        response = requests.post(completions_url, json=payload, timeout=timeout_seconds, headers=headers)
        result["status_code"] = response.status_code
        response.raise_for_status()
        response_payload = response.json()
        result["content_preview"] = str(response_payload)[:80]
        result["ok"] = True
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
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


def _finalize_failed_run(error: Exception, traceback_text: str, config: RuntimeConfig) -> None:
    global ACTIVE_RUN_DIAGNOSTICS
    diagnostics = ACTIVE_RUN_DIAGNOSTICS
    if diagnostics is None:
        diagnostics = _new_run_diagnostics(len(SOURCE_FEEDS))
        ACTIVE_RUN_DIAGNOSTICS = diagnostics
    try:
        _active_run_finalizer(diagnostics, config).finish_failed(error, traceback_text)
    except Exception as finalizer_error:
        progress_tracker.warning(f"Failed-run diagnostics finalization failed: {finalizer_error}")


def run_pipeline() -> None:
    RunSession(CONFIG).run()


@contextmanager
def managed_model_server():
    global MANAGED_MODEL_SERVER_ACTIVE
    global MANAGED_MODEL_SERVER_READY
    global MANAGED_MODEL_SERVER_EXTERNAL
    global MANAGED_MODEL_SERVER_PROCESS
    global MANAGED_MODEL_SERVER_LOG_FILE
    global MANAGED_MODEL_SERVER_EXIT_RECORDED
    previous_active = MANAGED_MODEL_SERVER_ACTIVE
    if previous_active:
        yield
        return

    MANAGED_MODEL_SERVER_ACTIVE = True
    MANAGED_MODEL_SERVER_READY = False
    MANAGED_MODEL_SERVER_EXTERNAL = False
    MANAGED_MODEL_SERVER_PROCESS = None
    MANAGED_MODEL_SERVER_LOG_FILE = None
    MANAGED_MODEL_SERVER_EXIT_RECORDED = False
    try:
        yield
    finally:
        if MANAGED_MODEL_SERVER_PROCESS is not None:
            _stop_managed_server_process(MANAGED_MODEL_SERVER_PROCESS, server_label="model server")
            record_activity_snapshot("after_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
        if MANAGED_MODEL_SERVER_LOG_FILE is not None:
            MANAGED_MODEL_SERVER_LOG_FILE.close()
        MANAGED_MODEL_SERVER_ACTIVE = False
        MANAGED_MODEL_SERVER_READY = False
        MANAGED_MODEL_SERVER_EXTERNAL = False
        MANAGED_MODEL_SERVER_PROCESS = None
        MANAGED_MODEL_SERVER_LOG_FILE = None
        MANAGED_MODEL_SERVER_EXIT_RECORDED = False


def _ensure_external_model_server_ready() -> None:
    """Wait for the external endpoint to answer /models, then probe generation.

    Unlike the managed path below, an already-live endpoint is the goal (not an
    error), so there is no conflict check; the generation probe is the real gate.
    """
    record_activity_snapshot("before_external_server_wait", ACTIVE_RUN_DIAGNOSTICS)
    deadline = time.monotonic() + EXTERNAL_SERVER_READY_TIMEOUT_SECONDS
    last_preflight: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_preflight = preflight_model_server()
        if last_preflight.get("ok"):
            break
        status_code = last_preflight.get("status_code")
        if status_code in (401, 403):
            raise RuntimeError(
                f"External model server at {MODEL_BASE_URL} rejected the request "
                f"with HTTP {status_code}. The endpoint requires authentication: "
                "set NEWS_MODEL_API_KEY to a valid key for this endpoint."
            )
        time.sleep(2)

    detail = last_preflight.get("error") or last_preflight.get("served_models") or "no response"
    if not last_preflight.get("ok"):
        raise TimeoutError(
            f"External model server did not become ready within "
            f"{EXTERNAL_SERVER_READY_TIMEOUT_SECONDS} seconds at {MODEL_BASE_URL}: {detail}."
        )
    progress_tracker.update_meter(done=2, detail="Checking model generation.")
    generation_probe = probe_model_generation()
    if not generation_probe.get("ok"):
        probe_status = generation_probe.get("status_code")
        auth_hint = (
            f" The endpoint rejected the probe with HTTP {probe_status}; set "
            "NEWS_MODEL_API_KEY if it requires authentication."
            if probe_status in (401, 403)
            else ""
        )
        raise RuntimeError(
            "External model server answered /models but failed a tiny generation probe. "
            f"{generation_probe.get('error') or generation_probe}. "
            "Verify the endpoint supports POST /chat/completions and that NEWS_MODEL "
            f"({MODEL_REFERENCE}) matches a served model id.{auth_hint}"
        )
    record_activity_snapshot("after_external_server_ready", ACTIVE_RUN_DIAGNOSTICS)


def _ensure_main_model_server_ready() -> None:
    global MANAGED_MODEL_SERVER_READY
    global MANAGED_MODEL_SERVER_PROCESS
    global MANAGED_MODEL_SERVER_LOG_FILE
    global MANAGED_MODEL_SERVER_EXTERNAL
    if MANAGED_MODEL_SERVER_READY:
        return

    with MANAGED_MODEL_SERVER_READY_LOCK:
        if MANAGED_MODEL_SERVER_READY:
            return

        MANAGED_MODEL_SERVER_EXTERNAL = False
        ensure_codex_safe_model_reference(MODEL_REFERENCE)
        progress_tracker.start_meter("model", total=3, unit="steps", detail="Checking model server.")
        record_activity_snapshot("before_model_server_preflight")
        if MODEL_BACKEND == MODEL_BACKEND_EXTERNAL:
            _ensure_external_model_server_ready()
            MANAGED_MODEL_SERVER_EXTERNAL = True
            MANAGED_MODEL_SERVER_READY = True
            progress_tracker.finish_meter(detail="External model server ready.")
            return
        existing_preflight = preflight_model_server()
        if ACTIVE_RUN_DIAGNOSTICS is not None:
            ACTIVE_RUN_DIAGNOSTICS.event("model_server_preflight", **existing_preflight)
        if existing_preflight.get("ok"):
            served_models = existing_preflight.get("served_models") or ["n/a"]
            raise RuntimeError(
                "Model server endpoint is already in use before this run could start "
                "a managed server. "
                f"Base URL: {MODEL_BASE_URL}. "
                f"Expected {MODEL_REFERENCE} / {MODEL_NAME}; served {served_models}. "
                "Stop the existing server or choose another NEWS_MODEL_BASE_URL."
            )

        log_path = _managed_model_server_log_path()
        command = shlex.split(MODEL_SERVER_COMMAND)
        record_activity_snapshot("before_model_server_start", ACTIVE_RUN_DIAGNOSTICS)
        progress_tracker.update_meter(done=1, detail="Starting managed model server.")
        progress_tracker.detail(f"Managed model server command: {MODEL_SERVER_COMMAND}")
        progress_tracker.detail(f"Managed model server log: {log_path}")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
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
            progress_tracker.update_meter(done=2, detail="Checking model generation.")
            generation_probe = probe_model_generation()
            if not generation_probe.get("ok"):
                raise RuntimeError(
                    "Managed model server answered /models but failed a tiny generation probe. "
                    f"{generation_probe.get('error') or generation_probe}. "
                    f"See {_managed_model_server_log_path()}."
                )
            time.sleep(0.5)
            _raise_if_managed_model_server_exited()
            progress_tracker.detail("Managed model server passed a tiny generation probe.")
            MANAGED_MODEL_SERVER_READY = True
            progress_tracker.finish_meter(detail="Model server ready.")
        except Exception:
            _stop_managed_server_process(process, server_label="model server")
            record_activity_snapshot("after_model_server_stop", ACTIVE_RUN_DIAGNOSTICS)
            log_file.close()
            MANAGED_MODEL_SERVER_PROCESS = None
            MANAGED_MODEL_SERVER_LOG_FILE = None
            raise


def _managed_model_server_log_path() -> str:
    return os.path.join(RUN_OUTPUT_DIR, "model_server.log")


def _run_pipeline() -> None:
    global ACTIVE_RUN_DIAGNOSTICS
    global ACTIVE_RUN_FINALIZER
    all_sources = list(SOURCE_FEEDS.keys())
    sources = all_sources
    effective_total_article_summary_cap = TOTAL_ARTICLE_SUMMARY_CAP

    diagnostics = _new_run_diagnostics(len(sources))
    ACTIVE_RUN_DIAGNOSTICS = diagnostics
    ACTIVE_RUN_FINALIZER = _active_run_finalizer(diagnostics, CONFIG)
    image_status = "image on" if IMAGE_GENERATION_ENABLED else "image off"
    send_target = "Primary only" if RECIPIENT_SCOPE == "primary" else "active recipients"
    preset_label = PRESET_ID or "custom"
    progress_tracker.step(
        "setup",
        (
            f"{preset_label} | model {MODEL_REFERENCE} | {image_status} | "
            f"{len(sources)} sources | send {send_target}"
        ),
    )
    progress_tracker.detail(
        f"Default model: {MODEL_NAME} | backend: {MODEL_BACKEND} | "
        f"model: {MODEL_REFERENCE} -> {MODEL_NAME}"
    )
    progress_tracker.detail(
    f"Model caps: input {MODEL_MAX_INPUT_TOKENS} tokens, "
    f"article text {ARTICLE_TEXT_TOKEN_LIMIT} tokens, "
    f"summaries {TOTAL_ARTICLE_SUMMARY_CAP} total, "
    f"{MAX_STORIES} stories overall, "
    f"story overlap threshold {STORY_SELECTION_OVERLAP_THRESHOLD:.2f}, "
    f"article summary concurrency {ARTICLE_SUMMARY_CONCURRENCY}, "
    f"story synthesis concurrency {STORY_SYNTHESIS_CONCURRENCY}, "
    f"derived model concurrency {MODEL_CONCURRENCY}."
    )
    progress_tracker.detail(
        f"Source scrape guardrails: article download timeout {ARTICLE_DOWNLOAD_TIMEOUT_SECONDS}s, "
        f"article scrape deadline {ARTICLE_SCRAPE_TOTAL_TIMEOUT_SECONDS}s, "
        f"slow source warning {SLOW_SOURCE_WARNING_SECONDS}s, "
        f"source concurrency {SOURCE_COLLECTION_CONCURRENCY}."
    )
    progress_tracker.detail(f"Preset: {preset_label}")
    progress_tracker.detail(f"Source scope: {SOURCE_SCOPE}")
    progress_tracker.detail(f"Recipient scope: {RECIPIENT_SCOPE}")
    progress_tracker.detail(f"URL reuse blocking: {URL_REUSE_BLOCKING_ENABLED}")
    progress_tracker.detail(
        f"Source pool: {len(sources)} active feed(s) after tier/language filters."
    )
    active_source_tiers = Counter(
        str(source.get("tier") or "unknown") for source in SOURCE_FEEDS.values()
    )
    if active_source_tiers:
        tier_summary = ", ".join(
            f"{tier}: {count}" for tier, count in sorted(active_source_tiers.items())
        )
        progress_tracker.detail(f"Active source tiers: {tier_summary}.")
    if RECIPIENT_SCOPE == "primary":
        progress_tracker.detail(f"Delivery limited to {PRIMARY_RECIPIENT}.")
    progress_tracker.detail(f"Run staging folder: {RUN_OUTPUT_DIR}")
    progress_tracker.detail(f"Latest readable run review: {LATEST_RUN_MARKDOWN_PATH}")
    progress_tracker.detail(f"Rolling run log: {LATEST_RUN_LOG_PATH}")
    progress_tracker.detail(f"Rolling run details: {LATEST_RUN_DETAILS_PATH}")
    progress_tracker.detail(f"Run used URL log: {RUN_USED_URLS_PATH}")
    progress_tracker.detail(f"Run log: {RUN_LOG_PATH}")
    if not URL_REUSE_BLOCKING_ENABLED:
        progress_tracker.detail("URL reuse blocking: disabled for this run.")

    progress_tracker.detail("Story mode: global story-first clustering.")

    progress_tracker.reset(total_sources=len(sources))

    final_reports: List[article_summary_records_stage.ArticleSummaryRecord | str] = []

    # 3) Collect source contexts, dedupe, record source diagnostics, and persist URL history.
    article_collection = collect_article_candidates(
        ArticleCollectionRequest(
            sources=sources,
            source_feeds=SOURCE_FEEDS,
            config=CONFIG,
            run_id=timestamp,
            run_started_at=RUN_STARTED_AT.isoformat(timespec="seconds"),
            preset_id=PRESET_ID or "custom",
            run_used_urls_path=RUN_USED_URLS_PATH,
            slow_source_warning_seconds=SLOW_SOURCE_WARNING_SECONDS,
            source_collection_concurrency=SOURCE_COLLECTION_CONCURRENCY,
            url_reuse_blocking_enabled=URL_REUSE_BLOCKING_ENABLED,
        ),
        diagnostics,
        _active_run_finalizer(diagnostics, CONFIG),
        progress_tracker,
        ArticleCollectionAdapters(
            fetch_source_context=_fetch_source_context_for_collection,
            append_unique_urls=_append_unique_urls,
        ),
    )
    article_candidates = article_collection.article_candidates
    if not article_candidates:
        progress_tracker.step("finalize", "No recent article candidates available; stopping run.")
        diagnostics.event("aborted", reason="no_article_candidates")
        _finish_run_diagnostics(diagnostics, CONFIG)
        return


    story_cluster_work = max(
        1,
        len(article_candidates)
        + ((len(article_candidates) * (len(article_candidates) - 1)) // 2)
        + len(article_candidates),
    )
    progress_tracker.start_story_clustering(
        story_cluster_work,
        detail=f"Clustering {len(article_candidates)} candidate articles.",
    )
    clustered_article_targets, story_records, story_cluster_stats = (
        story_clustering_stage.organize_article_targets_into_global_stories(
            article_candidates,
            min_articles_per_story=MIN_ARTICLES_PER_STORY,
            similarity_threshold=STORY_CLUSTER_SIMILARITY_THRESHOLD,
            max_articles_per_source=MAX_ARTICLES_PER_SOURCE,
            progress_callback=progress_tracker.story_clustering_progress,
        )
    )
    _active_run_finalizer(diagnostics, CONFIG).record_story_records(story_records)
    progress_tracker.finish_meter(
        detail=(
            f"{story_cluster_stats.get('story_count', 0)} story groups | "
            f"{story_cluster_stats.get('included_count', 0)} retained articles"
        )
    )
    diagnostics.event("story_clustering", **{
        k: v for k, v in story_cluster_stats.items()
        if k not in (
            "stories",
            "dropped_articles",
            "pair_debug",
            "article_story_memberships",
        )
    })
    progress_tracker.detail(
        f"Story clustering: {story_cluster_stats.get('included_count', 0)} "
        f"article target(s) retained across {story_cluster_stats.get('story_count', 0)} "
        f"viable story group(s); {story_cluster_stats.get('dropped_count', 0)} dropped below "
        f"the {MIN_ARTICLES_PER_STORY}-article story floor "
        f"(TF-IDF threshold {STORY_CLUSTER_SIMILARITY_THRESHOLD:.2f}, global)."
    )
    all_clustered_article_targets = list(clustered_article_targets)
    all_story_records = list(story_records)
    clustered_article_targets, story_records, article_budget_stats = (
        _budget_article_targets_for_summary(
            clustered_article_targets,
            story_records,
            total_cap=effective_total_article_summary_cap,
            gemma_4_derived=TOTAL_ARTICLE_SUMMARY_CAP_GEMMA_4_DERIVED,
        )
    )
    diagnostics.record_article_budget(article_budget_stats)
    if article_budget_stats.get("dropped_count"):
        progress_tracker.detail(
            "Article summary budget: "
            f"{article_budget_stats.get('included_count', 0)} of "
            f"{article_budget_stats.get('candidate_count', 0)} clustered article target(s) "
            f"kept for summarization (cap {article_budget_stats.get('total_cap', 0)})."
        )
    if not clustered_article_targets:
        progress_tracker.step("finalize", "No multi-article story clusters available; stopping run.")
        diagnostics.event("aborted", reason="no_supported_story_clusters")
        _finish_run_diagnostics(diagnostics, CONFIG)
        return

    if MANAGED_MODEL_SERVER_ACTIVE and not MANAGED_MODEL_SERVER_READY:
        _ensure_main_model_server_ready()

    progress_tracker.start_article_summary(len(clustered_article_targets))
    article_summary_reports: List[article_summary_records_stage.ArticleSummaryRecord] = []
    article_summary_reports.extend(
        article_summarization_stage.run_article_summary_pass(
            clustered_article_targets,
            _article_summarization_runtime(),
        )
    )

    progress_tracker.finish_meter(detail=f"{len(article_summary_reports)} article summaries")
    diagnostics.article_summary_count = len(article_summary_reports)
    article_summary_records = _report_entry_debug_records(article_summary_reports)
    _active_run_finalizer(diagnostics, CONFIG).record_summarized_articles(clustered_article_targets)
    _active_run_finalizer(diagnostics, CONFIG).record_article_summary_records(article_summary_records)
    record_activity_snapshot("after_article_summaries", diagnostics)
    progress_tracker.detail(f"Saved {len(article_summary_reports)} article summary record(s).")

    progress_tracker.detail("Drafting clustered stories from article summaries.")
    story_drafts, story_draft_stats = story_drafting_stage.draft_story_clusters_from_article_summaries(
        story_records,
        article_summary_reports,
        _story_drafting_runtime(),
        article_targets=clustered_article_targets,
    )
    progress_tracker.finish_meter(
        detail=(
            f"{story_draft_stats.get('story_drafts_generated', 0)} valid | "
            f"{story_draft_stats.get('story_drafts_rejected', 0)} rejected"
        )
    )
    diagnostics.event("story_drafting", **story_draft_stats)
    progress_tracker.detail(
        f"Story drafting: {story_draft_stats.get('story_drafts_generated', 0)} "
        f"drafted story paragraph(s) from {story_draft_stats.get('story_blocks_requested', 0)} "
        "eligible cluster(s)."
    )

    # Dedup near-duplicate story drafts using embedding cosine similarity.
    if story_drafts:
        story_drafts, story_dedup_stats = _dedupe_story_drafts_for_global_selection(story_drafts)
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

        progress_tracker.start_meter(
            "story_selection",
            total=len(story_drafts),
            unit="stories",
            detail="Evaluating story quality.",
        )
        story_drafts, global_scale_stats = (
            story_selection_stage.apply_global_story_scale_screening(
                story_drafts,
                _story_selection_runtime(),
            )
        )
        progress_tracker.finish_meter(
            detail=(
                f"{global_scale_stats.get('kept_count', 0)} eligible | "
                f"{global_scale_stats.get('dropped_count', 0)} ineligible"
            )
        )
        diagnostics.event("global_story_scale_screening", **global_scale_stats)
        progress_tracker.detail(
            "Global story scale screening: "
            f"{global_scale_stats.get('kept_count', 0)} eligible for final output, "
            f"{global_scale_stats.get('dropped_count', 0)} ineligible "
            f"(enabled={bool(global_scale_stats.get('enabled'))})."
        )

        selected_story_matches, story_selection_stats = (
            story_selection_stage.select_global_story_drafts(
                story_drafts,
                max_stories=MAX_STORIES,
                overlap_threshold=STORY_SELECTION_OVERLAP_THRESHOLD,
            )
        )
        diagnostics.event("global_story_selection", **story_selection_stats)
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
        selected_story_matches = []
        story_selection_stats = {
            "enabled": True,
            "story_count": 0,
            "selected_story_count": 0,
            "max_stories": MAX_STORIES,
            "overlap_threshold": STORY_SELECTION_OVERLAP_THRESHOLD,
            "selected": [],
            "rejected": [],
            "article_overlap_dedup": {
                "enabled": True,
                "threshold": STORY_SELECTION_OVERLAP_THRESHOLD,
                "conflicts_resolved": 0,
                "banned_story_count": 0,
                "events": [],
            },
        }
        diagnostics.event("global_story_selection", **story_selection_stats)
        progress_tracker.detail(
            "No story drafts generated from viable clusters."
        )
    _active_run_finalizer(diagnostics, CONFIG).record_story_records(selected_story_matches)
    progress_tracker.detail(
        f"Global story selection: {story_selection_stats.get('selected_story_count', 0)} "
        f"of {story_selection_stats.get('story_count', 0)} drafted story candidate(s) selected "
        f"(cap {MAX_STORIES}, overlap threshold {STORY_SELECTION_OVERLAP_THRESHOLD:.2f})."
    )
    overlap_stats = story_selection_stats.get("article_overlap_dedup") or {}
    if overlap_stats.get("conflicts_resolved"):
        progress_tracker.detail(
            "Story overlap filter: "
            f"rejected {overlap_stats.get('conflicts_resolved')} story candidate(s) "
            f"above {STORY_SELECTION_OVERLAP_THRESHOLD:.2f} article overlap."
        )

    story_backfill_stats = {
        "enabled": True,
        "iterations": 0,
        "initial_selected_story_count": 0,
        "final_selected_story_count": 0,
        "deficits_before": {},
        "deficits_after": {},
        "attempted_story_count_by_story": {},
        "attempted_article_count": 0,
        "new_article_summary_count": 0,
        "new_story_draft_count": 0,
        "exhausted_stories": [],
        "reserve_story_count": 0,
        "batch_multiplier": max(1, STORY_BACKFILL_BATCH_MULTIPLIER),
    }
    diagnostics.article_summary_count = len(article_summary_reports)
    # Accumulate backfill contributions into first-pass stats
    accumulated_draft_stats = dict(story_draft_stats)
    accumulated_scale_stats = dict(global_scale_stats) if story_drafts else {"kept_count": 0, "dropped_count": 0, "enabled": False}
    accumulated_selection_stats = dict(story_selection_stats)

    selected_story_count = int(story_selection_stats.get("selected_story_count") or 0)
    story_backfill_stats["initial_selected_story_count"] = selected_story_count
    summarized_article_ids: set[str] = {
        str(a.get("article_id") or "") for a in clustered_article_targets
    }
    attempted_story_keys: set[str] = set()
    all_selected_stories: list[dict] = list(selected_story_matches)
    backfill_budgeted_stories: list[dict] = []
    backfill_iteration = 0
    max_backfill_iterations = 3
    backfill_batch_cap = max(
        effective_total_article_summary_cap,
        STORY_BACKFILL_BATCH_MULTIPLIER * MAX_STORIES * MIN_ARTICLES_PER_STORY,
    )

    while (
        len(all_selected_stories) < MAX_STORIES
        and backfill_iteration < max_backfill_iterations
    ):
        # Find stories not yet attempted
        unbudgeted_stories = [
            s for s in all_story_records
            if s.get("story_key") not in attempted_story_keys
        ]
        if not unbudgeted_stories:
            break
        # Find articles not yet summarized
        unsummarized_articles = [
            a for a in all_clustered_article_targets
            if str(a.get("article_id") or "") not in summarized_article_ids
        ]
        backfill_iteration += 1
        backfill_budgeted, backfill_filtered_stories, backfill_budget_stats = (
            _budget_article_targets_for_summary(
                unsummarized_articles,
                unbudgeted_stories,
                total_cap=backfill_batch_cap,
                gemma_4_derived=False,
            )
        )
        # Track story keys that were actually budgeted this round
        for s in backfill_filtered_stories:
            attempted_story_keys.add(str(s.get("story_key") or ""))

        if not backfill_budgeted:
            break

        progress_tracker.detail(
            f"Story backfill #{backfill_iteration}: "
            f"summarizing {len(backfill_budgeted)} more article(s) "
            f"from {len(backfill_filtered_stories)} reserve story group(s)."
        )
        backfill_summaries = list(
            article_summarization_stage.run_article_summary_pass(
                backfill_budgeted,
                _article_summarization_runtime(),
            )
        )
        if not backfill_summaries:
            break

        article_summary_reports.extend(backfill_summaries)
        for a in backfill_budgeted:
            summarized_article_ids.add(str(a.get("article_id") or ""))

        backfill_drafts, backfill_draft_stats = (
            story_drafting_stage.draft_story_clusters_from_article_summaries(
                backfill_filtered_stories,
                article_summary_reports,
                _story_drafting_runtime(),
                article_targets=all_clustered_article_targets,
            )
        )
        if not backfill_drafts:
            continue
        backfill_drafts, backfill_scale_stats = (
            story_selection_stage.apply_global_story_scale_screening(
                backfill_drafts,
                _story_selection_runtime(),
            )
        )
        backfill_selected, backfill_selection_stats = (
            story_selection_stage.select_global_story_drafts(
                backfill_drafts,
                max_stories=MAX_STORIES - len(all_selected_stories),
                overlap_threshold=STORY_SELECTION_OVERLAP_THRESHOLD,
            )
        )
        all_selected_stories.extend(backfill_selected)
        story_backfill_stats["new_article_summary_count"] += len(backfill_summaries)
        story_backfill_stats["new_story_draft_count"] += len(backfill_drafts)
        # Accumulate into first-pass stats
        for key in ("story_drafts_generated", "story_drafts_rejected", "story_blocks_requested"):
            accumulated_draft_stats[key] = accumulated_draft_stats.get(key, 0) + backfill_draft_stats.get(key, 0)
        accumulated_scale_stats["kept_count"] = accumulated_scale_stats.get("kept_count", 0) + backfill_scale_stats.get("kept_count", 0)
        accumulated_scale_stats["dropped_count"] = accumulated_scale_stats.get("dropped_count", 0) + backfill_scale_stats.get("dropped_count", 0)
    # Merge backfill results into first-pass stats
    if backfill_iteration > 0:
        selected_story_matches = all_selected_stories
        selected_story_count = len(all_selected_stories)
        # Merge accumulated into original dicts
        for key in ("story_drafts_generated", "story_drafts_rejected", "story_blocks_requested"):
            story_draft_stats[key] = accumulated_draft_stats.get(key, story_draft_stats.get(key, 0))
        for key in ("kept_count", "dropped_count", "enabled"):
            if key in accumulated_scale_stats:
                global_scale_stats[key] = accumulated_scale_stats[key]
        for key in ("story_count", "selected_story_count"):
            story_selection_stats[key] = accumulated_selection_stats.get(key, story_selection_stats.get(key, 0))
        story_selection_stats["selected_story_count"] = selected_story_count
        # Re-emit diagnostics with accumulated values
        diagnostics.event("story_drafting", **story_draft_stats)
        diagnostics.event("global_story_scale_screening", **global_scale_stats)
        diagnostics.event("global_story_selection", **story_selection_stats)
        progress_tracker.detail(
            f"Story backfill: {backfill_iteration} iteration(s), "
            f"+{story_backfill_stats['new_article_summary_count']} article summaries, "
            f"final tally {selected_story_count} story candidate(s)."
        )

    story_backfill_stats.update({
        "iterations": backfill_iteration,
        "final_selected_story_count": selected_story_count,
        "attempted_article_count": story_backfill_stats["new_article_summary_count"],
        "reserve_story_count": len(backfill_budgeted_stories),
    })
    if selected_story_count < MAX_STORIES:
        diagnostics.event(
            "story_coverage_deficit",
            selected_story_count=selected_story_count,
            target_story_count=MAX_STORIES,
            deficit=MAX_STORIES - selected_story_count,
        )
        progress_tracker.detail(
            f"Story coverage deficit: selected {selected_story_count} of {MAX_STORIES} target story slot(s)."
        )

    final_reports, story_assignment_stats = (
        story_selection_stage.build_story_assigned_article_reports(
            selected_story_matches,
            article_summary_reports,
            clustered_article_targets,
            _story_selection_runtime(),
        )
    )
    diagnostics.event("story_report_assignment", **story_assignment_stats)
    selected_article_ids = {
        story_drafting_stage.report_article_id(entry)
        for entry in final_reports
        if story_drafting_stage.report_article_id(entry)
    }
    selected_articles = [
        article
        for article in clustered_article_targets
        if str(article.get("article_id") or "") in selected_article_ids
    ]
    progress_tracker.detail(
        f"Story assignment: {len(final_reports)} story article summary record(s) "
        f"from {story_assignment_stats.get('selected_unique_article_count', 0)} unique article(s)."
    )
    if not final_reports:
        progress_tracker.step("finalize", "No global stories selected; stopping run.")
        diagnostics.event("aborted", reason="no_global_story_matches")
        _finish_run_diagnostics(diagnostics, CONFIG)
        return
    story_summary_records = _report_entry_debug_records(final_reports)
    _active_run_finalizer(diagnostics, CONFIG).record_selected_articles(selected_articles)
    _active_run_finalizer(diagnostics, CONFIG).record_story_summary_records(story_summary_records)

    recipient_config = get_active_recipient_config(load_recipient_config())
    recipient_list = list(recipient_config.keys())
    recipient_names = [
        recipient_config[email].get("name") or email
        for email in recipient_list
    ]

    if not recipient_list:
        progress_tracker.step("finalize", "No recipients configured; stopping after summaries.")
        diagnostics.event("completed_without_recipients")
        _finish_run_diagnostics(diagnostics, CONFIG)
        return

    prompt_label = "default prompt"
    progress_tracker.start_meter(
        "report",
        total=5,
        unit="steps",
        detail="Building report.",
    )
    progress_tracker.detail(
        f"Building {prompt_label} report for: {', '.join(recipient_list)}"
    )

    report_asset_path = os.path.join(
        RUN_OUTPUT_DIR,
        f"news_report_{timestamp}_{MODEL_REPORT_LABEL}_default_prompt.txt",
    )
    report_body = ""
    progress_tracker.set_final_step("synthesis", 2)
    story_drafting, token_stats, synthesis_debug = (
        story_selection_stage.build_precomputed_global_story_synthesis(
            selected_story_matches,
            final_reports,
            _story_selection_runtime(),
        )
    )
    synthesis_body = clean_synthesis_for_publication(
        story_drafting,
        relaxed=RELAXED_STORY_DRAFTING_GUARDS,
    )
    citation_sources = list((token_stats or {}).get("citation_sources") or [])
    citation_groups = list((token_stats or {}).get("citation_groups") or [])
    if not synthesis_body:
        last_attempt = (synthesis_debug.get("attempts") or [{}])[-1]
        skip_reason = (
            last_attempt.get("reason")
            if not story_drafting
            else "publication cleaner removed all synthesis sections"
        )
        progress_tracker.detail(
            f"No synthesis generated for {', '.join(recipient_list)} "
            f"({skip_reason}). Skipping report."
        )
        diagnostics.event(
            "story_drafting_skipped",
            recipients=recipient_list,
            reason=skip_reason,
            token_stats=token_stats,
            attempts=synthesis_debug.get("attempts") or [],
            relaxed_guards=RELAXED_STORY_DRAFTING_GUARDS,
        )
    else:
        if synthesis_debug.get("fallback_synthesis_used"):
            diagnostics.event(
                "story_drafting_fallback_used",
                recipients=recipient_list,
                attempts=synthesis_debug.get("attempts") or [],
            )

        synthesis_body_without_citations = citations_stage.strip_citation_markers(synthesis_body)
        report_title = "Daily News Summary"

        progress_tracker.set_final_step("art", 3)
        record_activity_snapshot("before_image_generation", diagnostics)
        image_art = generate_report_image_art(
            report_path=report_asset_path,
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
            image_art,
            citation_sources,
            citation_groups,
        )
        image_art_diagnostics = None
        if image_art:
            image_art_diagnostics = {
                key: value
                for key, value in image_art.items()
                if key not in {"data_uri", "image_prompt"}
            }
        diagnostics.record_report(
            path=LATEST_RUN_MARKDOWN_PATH,
            prompt_label=prompt_label,
            recipient_count=len(recipient_list),
            recipients=recipient_list,
            token_stats=token_stats,
            reference_report_count=len(reference_reports),
            citation_source_count=len(citation_sources),
            citation_group_count=len(citation_groups),
            synthesis_dataset_artifacts=synthesis_dataset_artifacts,
            image_art=image_art_diagnostics,
        )

        progress_tracker.set_final_step("email", 5)
        maybe_email_report(
            report_title,
            report_body,
            synthesis_body,
            reference_reports,
            recipient_list,
            recipient_names,
            image_art,
            citation_sources,
            citation_groups,
        )

        if token_stats:
            progress_tracker.detail(f"Final synthesis token stats for {', '.join(recipient_list)}: {token_stats}")
        progress_tracker.detail("Finished report. Final prose will be embedded in latest_run.md.")

    diagnostics.event("completed")
    _active_run_finalizer(diagnostics, CONFIG).record_report_body(report_body)
    _finish_run_diagnostics(diagnostics, CONFIG)
    sync_assistant_context_latest_output(CONFIG)
