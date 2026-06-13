"""Article Collection Funnel for fresh article candidates."""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from . import history_store
from .config import RuntimeConfig
from .diagnostics import RunDiagnostics
from .run_finalizer import RunFinalizer


logger = logging.getLogger(__name__)


class ProgressLike(Protocol):
    def detail(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def source_completed(
        self,
        source_name: str | None = None,
        *,
        worker_count: int | None = None,
        candidate_articles: int = 0,
    ) -> None: ...

    def update_source_fresh_articles(
        self,
        total_articles: int,
        *,
        latest_source: str | None = None,
    ) -> None: ...

    def finish_meter(self, *, detail: str | None = None) -> None: ...


@dataclass(frozen=True)
class ArticleCollectionRequest:
    sources: list[str]
    source_feeds: dict[str, dict[str, Any]]
    config: RuntimeConfig
    run_id: str
    run_started_at: str
    preset_id: str
    run_used_urls_path: str
    slow_source_warning_seconds: float
    source_collection_concurrency: int
    url_reuse_blocking_enabled: bool
    write_legacy_diagnostics: bool


@dataclass(frozen=True)
class ArticleCollectionStats:
    matched_feed_item_count: int = 0
    fresh_article_count: int = 0
    candidate_url_count: int = 0
    rejected_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ArticleCollectionResult:
    article_candidates: list[dict[str, Any]]
    stats: ArticleCollectionStats


@dataclass(frozen=True)
class ArticleCollectionAdapters:
    fetch_source_context: Callable[[int, str], dict[str, Any]]
    blocking_urls: Callable[[Path], set[str]] = history_store.blocking_urls
    normalize_history_url: Callable[[str], str] = history_store.normalize_url_for_history
    upsert_url_history: Callable[..., None] = history_store.upsert_url_history
    persist_url_list_debug: Callable[[list[str], str], tuple[str, int] | None] = lambda _urls, _label: None
    append_unique_urls: Callable[[str, list[str]], None] = lambda _path, _urls: None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def collect_article_candidates(
    request: ArticleCollectionRequest,
    diagnostics: RunDiagnostics,
    finalizer: RunFinalizer,
    progress: ProgressLike,
    adapters: ArticleCollectionAdapters,
) -> ArticleCollectionResult:
    seen_urls = _load_seen_urls(request, adapters)
    run_seen_urls: set[str] = set()
    article_candidates: list[dict[str, Any]] = []
    candidate_urls: list[str] = []
    matched_feed_item_count = 0
    fresh_article_count = 0
    source_rejection_counts: Counter[str] = Counter()

    for source_result in _collect_source_contexts(request, progress, adapters):
        article_targets, new_urls, source_run = _source_run_from_context(
            request,
            source_result,
            seen_urls,
            run_seen_urls,
            diagnostics,
            progress,
            adapters,
        )
        diagnostics.record_source_run(source_run)
        matched_feed_item_count += int(source_run.get("selected_item_count") or 0)
        fresh_article_count += int(source_run.get("fresh_article_count") or 0)
        source_rejection_counts.update(source_run.get("rejected_counts") or {})
        progress.update_source_fresh_articles(
            fresh_article_count,
            latest_source=str(source_run.get("source") or ""),
        )
        candidate_urls.extend(new_urls)
        article_candidates.extend(article_targets)

    progress.finish_meter(detail=f"{fresh_article_count} fresh articles")
    stats = ArticleCollectionStats(
        matched_feed_item_count=matched_feed_item_count,
        fresh_article_count=fresh_article_count,
        candidate_url_count=len(candidate_urls),
        rejected_counts=dict(source_rejection_counts),
    )
    _record_collection_summary(progress, stats)
    diagnostics.event(
        "article_collection",
        candidate_count=len(article_candidates),
        candidate_url_count=len(candidate_urls),
        matched_feed_item_count=matched_feed_item_count,
        fresh_article_count=fresh_article_count,
        rejected_counts=dict(source_rejection_counts),
    )
    _record_candidate_url_artifact(request, diagnostics, candidate_urls, adapters)
    finalizer.record_candidate_articles(article_candidates)
    _record_run_urls(request, candidate_urls, article_candidates, adapters)
    return ArticleCollectionResult(article_candidates=article_candidates, stats=stats)


def _collect_source_contexts(
    request: ArticleCollectionRequest,
    progress: ProgressLike,
    adapters: ArticleCollectionAdapters,
) -> list[dict[str, Any]]:
    if not request.sources:
        return []

    worker_count = max(1, min(request.source_collection_concurrency, len(request.sources)))
    progress.detail(f"Source collection concurrency: {worker_count}.")
    collected: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(adapters.fetch_source_context, source_index, source_name): (
                source_index,
                source_name,
            )
            for source_index, source_name in enumerate(request.sources, start=1)
        }
        for future in as_completed(future_map):
            source_index, source_name = future_map[future]
            try:
                result = future.result()
            except Exception as error:
                error_reason = f"{type(error).__name__}: {error}"
                now = adapters.now().isoformat(timespec="seconds")
                result = {
                    "source_index": source_index,
                    "source": source_name,
                    "started_at": now,
                    "completed_at": now,
                    "elapsed_seconds": 0.0,
                    "direct_context": None,
                    "error_reason": error_reason,
                }
            collected[source_index] = result
            direct_context = result.get("direct_context") or {}
            candidate_count = int(
                direct_context.get("selected_item_count")
                or len(direct_context.get("articles") or [])
                or 0
            )
            progress.source_completed(
                source_name,
                candidate_articles=candidate_count,
                worker_count=worker_count,
            )

    return [
        collected[source_index]
        for source_index in range(1, len(request.sources) + 1)
        if source_index in collected
    ]


def _source_run_from_context(
    request: ArticleCollectionRequest,
    source_result: dict[str, Any],
    seen_urls: set[str],
    run_seen_urls: set[str],
    diagnostics: RunDiagnostics,
    progress: ProgressLike,
    adapters: ArticleCollectionAdapters,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    source_index = int(source_result.get("source_index") or 0)
    source_name = str(source_result.get("source") or "")
    elapsed_seconds = float(source_result.get("elapsed_seconds") or 0.0)
    error_reason = str(source_result.get("error_reason") or "")
    if error_reason:
        article_targets: list[dict[str, Any]] = []
        new_urls: list[str] = []
        source_run = _source_context_error_run(source_name, error_reason)
        progress.warning(f"Source failed: {source_name}: {error_reason}")
    else:
        article_targets, new_urls, source_run = _article_candidates_from_source_context(
            request,
            source_name,
            source_result.get("direct_context"),
            seen_urls,
            run_seen_urls,
            adapters,
        )

    source_run["source_index"] = source_index
    source_run["started_at"] = source_result.get("started_at")
    source_run["completed_at"] = source_result.get("completed_at")
    source_run["elapsed_seconds"] = elapsed_seconds
    _record_source_timing(
        request,
        diagnostics,
        progress,
        source_name,
        source_index,
        elapsed_seconds,
        source_run,
    )
    return article_targets, new_urls, source_run


def _record_source_timing(
    request: ArticleCollectionRequest,
    diagnostics: RunDiagnostics,
    progress: ProgressLike,
    source_name: str,
    source_index: int,
    elapsed_seconds: float,
    source_run: dict[str, Any],
) -> None:
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
            elapsed_seconds=elapsed_seconds,
        )
        progress.warning(f"Source had {timeout_count} timed-out scrape(s): {source_name}")
    if elapsed_seconds >= request.slow_source_warning_seconds:
        source_run["slow_source"] = True
        diagnostics.event(
            "slow_source",
            source=source_name,
            source_index=source_index,
            elapsed_seconds=elapsed_seconds,
        )
        progress.warning(f"Slow source: {source_name} took {elapsed_seconds:.1f}s")


def _article_candidates_from_source_context(
    request: ArticleCollectionRequest,
    source_name: str,
    direct_context: dict[str, Any] | None,
    seen_urls: set[str],
    run_seen_urls: set[str],
    adapters: ArticleCollectionAdapters,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
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
        "selected_by_story": {},
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
    new_urls: list[str] = []
    for article in articles:
        url = str(article.get("url") or "").strip()
        run_dedupe_key = _normalize_url_for_dedupe(url) or url
        if not url:
            source_run["rejected_counts"]["missing_url"] += 1
            continue
        if run_dedupe_key in run_seen_urls:
            source_run["rejected_counts"]["duplicate_this_run"] += 1
            continue
        if request.url_reuse_blocking_enabled and (
            url in seen_urls or adapters.normalize_history_url(url) in seen_urls or run_dedupe_key in seen_urls
        ):
            source_run["rejected_counts"]["seen_in_history"] += 1
            continue

        fresh_articles.append(article)
        new_urls.append(url)
        run_seen_urls.add(run_dedupe_key)

    if not fresh_articles:
        return [], [], source_run

    article_targets: list[dict[str, Any]] = []
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


def _source_context_error_run(source_name: str, reason: str) -> dict[str, Any]:
    return {
        "source": source_name,
        "status": "source_error",
        "reason": reason,
        "feed_item_count": 0,
        "recent_item_count": 0,
        "selected_item_count": 0,
        "selected_items": [],
        "selected_by_story": {},
        "post_scrape_rejections": [],
        "feed_rejections": [],
        "scrape_attempts": [],
        "scrape_status_counts": {},
        "fresh_article_count": 0,
        "fresh_articles": [],
        "rejected_counts": {},
    }


def _record_collection_summary(progress: ProgressLike, stats: ArticleCollectionStats) -> None:
    rejected_counts = stats.rejected_counts
    if stats.matched_feed_item_count or any(rejected_counts.values()):
        progress.detail(
            f"Source funnel: {stats.matched_feed_item_count} recent scraped article candidate(s), "
            f"{stats.fresh_article_count} fresh article target(s) after dedupe/history "
            f"(history={rejected_counts.get('seen_in_history', 0)}, "
            f"duplicate_this_run={rejected_counts.get('duplicate_this_run', 0)}, "
            f"missing_url={rejected_counts.get('missing_url', 0)}, "
            f"wrong_feed_source={rejected_counts.get('wrong_feed_source', 0)}, "
            "wrong_feed_source_unattributed="
            f"{rejected_counts.get('wrong_feed_source_unattributed', 0)})."
        )


def _record_candidate_url_artifact(
    request: ArticleCollectionRequest,
    diagnostics: RunDiagnostics,
    candidate_urls: list[str],
    adapters: ArticleCollectionAdapters,
) -> None:
    candidate_url_artifact = adapters.persist_url_list_debug(candidate_urls, "candidate_urls")
    if candidate_url_artifact:
        candidate_url_path, candidate_url_count = candidate_url_artifact
        diagnostics.record_artifact(
            "candidate_urls",
            candidate_url_path,
            count=candidate_url_count,
            run_used_urls_path=request.run_used_urls_path,
        )


def _load_seen_urls(request: ArticleCollectionRequest, adapters: ArticleCollectionAdapters) -> set[str]:
    if not request.url_reuse_blocking_enabled:
        return set()
    try:
        seen_urls = set(adapters.blocking_urls(request.config.history_db_path))
        return seen_urls | {
            key
            for url in seen_urls
            for key in (adapters.normalize_history_url(url), _normalize_url_for_dedupe(url))
            if key
        }
    except Exception as error:
        logger.warning("Could not read DuckDB URL history: %s", error)
        return set()


def _record_run_urls(
    request: ArticleCollectionRequest,
    urls: list[str],
    articles: list[dict[str, Any]],
    adapters: ArticleCollectionAdapters,
) -> None:
    try:
        adapters.upsert_url_history(
            request.config.history_db_path,
            run_id=request.run_id,
            run_started_at=request.run_started_at,
            preset_id=request.preset_id,
            url_reuse_blocking_enabled=request.url_reuse_blocking_enabled,
            urls=urls,
            articles=articles,
        )
    except Exception as error:
        logger.warning("Could not write DuckDB URL history: %s", error)
        adapters.append_unique_urls(request.run_used_urls_path, urls)
        return

    if request.write_legacy_diagnostics:
        adapters.append_unique_urls(request.run_used_urls_path, urls)


def _normalize_url_for_dedupe(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    if raw.lower().startswith("www."):
        raw = raw[4:]
    raw = raw.split("#", 1)[0]
    if "?" in raw:
        base, query = raw.split("?", 1)
        kept = [
            kv for kv in query.split("&")
            if kv
            and not kv.lower().startswith(
                ("utm_", "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "cmp", "igshid")
            )
        ]
        raw = base + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/").lower()
