"""DuckDB-backed run history and cleanup helpers for the news pipeline."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .diagnostics import RunDiagnostics, run_status_from_events


TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
SUMMARY_FILE_RE = re.compile(
    r"^(article_summaries|topic_assigned_article_summaries|story_assigned_article_summaries|final_article_summaries_after_backfill)_"
    r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.json$"
)
URL_LIST_FILE_RE = re.compile(
    r"^(candidate_urls|selected_article_urls)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.txt$"
)
RUN_LOG_FILE_RE = re.compile(r"^run_log_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.log$")
RUN_DETAILS_FILE_RE = re.compile(r"^run_details_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.json$")

SUMMARY_STAGE_BY_PREFIX = {
    "article_summaries": "summarized",
    "topic_assigned_article_summaries": "topic_assigned",
    "story_assigned_article_summaries": "story_assigned",
    "final_article_summaries_after_backfill": "after_backfill",
}
URL_STAGE_BY_PREFIX = {
    "candidate_urls": "candidate",
    "selected_article_urls": "selected",
}
ROLLING_REVIEW_FILENAME = "latest_run.md"
ROLLING_RUN_FILENAMES = {
    ROLLING_REVIEW_FILENAME,
    "latest_run.log",
    "latest_run_details.json",
}


@dataclass
class HistoryCommandResult:
    action: str
    dry_run: bool
    db_path: Path
    run_count: int = 0
    file_count: int = 0
    byte_count: int = 0
    deleted_count: int = 0
    deleted_bytes: int = 0
    details: list[str] = field(default_factory=list)

    def format(self) -> str:
        title = (
            f"History {self.action}"
            if self.action == "export"
            else f"History {self.action} {'dry run' if self.dry_run else 'apply'}"
        )
        lines = [
            title,
            f"- Database: {self.db_path}",
        ]
        if self.run_count:
            lines.append(f"- Runs: {self.run_count}")
        if self.file_count:
            label = "CSV exports" if self.action == "export" else "Visible output files"
            lines.append(f"- {label}: {self.file_count} ({_bytes_label(self.byte_count)})")
        if self.deleted_count:
            lines.append(f"- Deleted: {self.deleted_count} ({_bytes_label(self.deleted_bytes)})")
        if self.details:
            lines.append("")
            lines.extend(self.details)
        return "\n".join(lines)


def _duckdb():
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            "DuckDB is required for the history store. Run `uv sync` after adding "
            "`duckdb>=1.4.4` to project dependencies."
        ) from error
    return duckdb


def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return _duckdb().connect(str(db_path))


def ensure_schema(db_path: Path) -> None:
    with connect(db_path) as con:
        _ensure_schema(con)


def _ensure_schema(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id VARCHAR PRIMARY KEY,
            run_started_at VARCHAR,
            run_completed_at VARCHAR,
            run_date VARCHAR,
            preset_id VARCHAR,
            status VARCHAR,
            abort_reason VARCHAR,
            duration_seconds INTEGER,
            duration_label VARCHAR,
            model VARCHAR,
            model_name VARCHAR,
            model_backend VARCHAR,
            model_default_sampling_json VARCHAR,
            model_task_sampling_json VARCHAR,
            story_cluster_similarity_threshold DOUBLE,
            story_selection_overlap_threshold DOUBLE,
            story_embedding_dedup_threshold DOUBLE,
            min_articles_per_story INTEGER,
            max_stories INTEGER,
            source_count INTEGER,
            feed_item_count INTEGER,
            selected_item_count INTEGER,
            fresh_article_count INTEGER,
            story_count INTEGER,
            story_included_count INTEGER,
            story_dropped_count INTEGER,
            story_draft_count INTEGER,
            story_scale_kept_count INTEGER,
            story_scale_dropped_count INTEGER,
            selected_story_count INTEGER,
            story_selection_candidate_count INTEGER,
            story_coverage_deficit INTEGER,
            article_summary_count INTEGER,
            report_count INTEGER,
            recipient_count INTEGER,
            reports_with_images INTEGER,
            image_warnings INTEGER,
            model_call_count INTEGER,
            model_calls_json VARCHAR,
            model_token_totals_json VARCHAR,
            model_retries INTEGER,
            model_fallbacks INTEGER,
            source_status_counts_json VARCHAR,
            rejection_counts_json VARCHAR,
            settings_json VARCHAR,
            stats_json VARCHAR,
            events_json VARCHAR,
            reports_json VARCHAR,
            artifacts_json VARCHAR,
            imported_from_path VARCHAR,
            imported_at VARCHAR
        )
        """
    )
    _ensure_columns(
        con,
        "runs",
        {
            "preset_id": "VARCHAR",
        },
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS run_sources (
            run_id VARCHAR,
            source_index INTEGER,
            source VARCHAR,
            status VARCHAR,
            reason VARCHAR,
            started_at VARCHAR,
            completed_at VARCHAR,
            elapsed_seconds DOUBLE,
            feed_item_count INTEGER,
            recent_item_count INTEGER,
            selected_item_count INTEGER,
            fresh_article_count INTEGER,
            timeout_count INTEGER,
            slow_source BOOLEAN,
            scrape_status_counts_json VARCHAR,
            rejected_counts_json VARCHAR,
            PRIMARY KEY (run_id, source_index, source)
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS article_summaries (
            run_id VARCHAR,
            stage VARCHAR,
            row_index INTEGER,
            title VARCHAR,
            source VARCHAR,
            published VARCHAR,
            url VARCHAR,
            article_id VARCHAR,
            story VARCHAR,
            summary VARCHAR,
            generated_at VARCHAR,
            imported_from_path VARCHAR,
            PRIMARY KEY (run_id, stage, row_index)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS run_articles (
            run_id VARCHAR,
            stage VARCHAR,
            row_index INTEGER,
            url VARCHAR,
            normalized_url VARCHAR,
            source VARCHAR,
            title VARCHAR,
            published VARCHAR,
            article_id VARCHAR,
            original_rss_url VARCHAR,
            resolved_url VARCHAR,
            scrape_status VARCHAR,
            resolution_status VARCHAR,
            imported_from_path VARCHAR,
            PRIMARY KEY (run_id, stage, row_index)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS url_history (
            normalized_url VARCHAR,
            history_scope VARCHAR,
            url VARCHAR,
            blocks_reuse BOOLEAN,
            first_seen_run_id VARCHAR,
            last_seen_run_id VARCHAR,
            first_seen_at VARCHAR,
            last_seen_at VARCHAR,
            source VARCHAR,
            title VARCHAR,
            published VARCHAR,
            article_id VARCHAR,
            imported_from_path VARCHAR,
            PRIMARY KEY (normalized_url, history_scope)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            run_id VARCHAR,
            name VARCHAR,
            path VARCHAR,
            family VARCHAR,
            metadata_json VARCHAR,
            imported BOOLEAN,
            imported_at VARCHAR,
            PRIMARY KEY (run_id, name, path)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS run_logs (
            run_id VARCHAR PRIMARY KEY,
            path VARCHAR,
            byte_count INTEGER,
            content VARCHAR,
            imported_at VARCHAR
        )
        """
    )


def _ensure_columns(con: Any, table_name: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info('{table_name}')").fetchall()}
    for column_name, column_type in columns.items():
        if column_name not in existing:
            con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def normalize_url_for_history(url: str) -> str:
    clean_url = str(url or "").strip()
    if not clean_url:
        return ""
    return clean_url.rstrip("/")


def blocking_urls(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    ensure_schema(db_path)
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT url, normalized_url FROM url_history WHERE blocks_reuse = TRUE"
        ).fetchall()
    urls: set[str] = set()
    for row in rows:
        for value in row:
            clean_value = str(value or "").strip()
            if clean_value:
                urls.add(clean_value)
    return urls


def upsert_url_history(
    db_path: Path,
    *,
    run_id: str,
    run_started_at: str,
    preset_id: str,
    url_reuse_blocking_enabled: bool,
    urls: Iterable[str],
    articles: Iterable[dict[str, Any]] | None = None,
    imported_from_path: str = "",
) -> int:
    history_label = str(preset_id or "").strip() or "custom"
    blocks_reuse = bool(url_reuse_blocking_enabled)
    article_by_url: dict[str, dict[str, Any]] = {}
    for article in articles or []:
        url = str(article.get("url") or "").strip()
        if url:
            article_by_url[normalize_url_for_history(url)] = article

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in urls:
        clean_url = str(url or "").strip()
        normalized_url = normalize_url_for_history(clean_url)
        if not clean_url or normalized_url in seen:
            continue
        seen.add(normalized_url)
        article = article_by_url.get(normalized_url, {})
        rows.append(
            {
                "normalized_url": normalized_url,
                "history_scope": _history_scope(history_label, blocks_reuse),
                "url": clean_url,
                "blocks_reuse": blocks_reuse,
                "first_seen_run_id": run_id,
                "last_seen_run_id": run_id,
                "first_seen_at": run_started_at,
                "last_seen_at": run_started_at,
                "source": str(article.get("source") or ""),
                "title": str(article.get("title") or ""),
                "published": str(article.get("pub_date") or article.get("published") or ""),
                "article_id": str(article.get("article_id") or ""),
                "imported_from_path": imported_from_path,
            }
        )
    if not rows:
        return 0

    ensure_schema(db_path)
    with connect(db_path) as con:
        for row in rows:
            existing = con.execute(
                """
                SELECT first_seen_run_id, first_seen_at
                FROM url_history
                WHERE normalized_url = ? AND history_scope = ?
                """,
                [row["normalized_url"], row["history_scope"]],
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE url_history
                    SET url = ?, blocks_reuse = ?, last_seen_run_id = ?, last_seen_at = ?,
                        source = COALESCE(NULLIF(?, ''), source),
                        title = COALESCE(NULLIF(?, ''), title),
                        published = COALESCE(NULLIF(?, ''), published),
                        article_id = COALESCE(NULLIF(?, ''), article_id),
                        imported_from_path = COALESCE(NULLIF(?, ''), imported_from_path)
                    WHERE normalized_url = ? AND history_scope = ?
                    """,
                    [
                        row["url"],
                        row["blocks_reuse"],
                        row["last_seen_run_id"],
                        row["last_seen_at"],
                        row["source"],
                        row["title"],
                        row["published"],
                        row["article_id"],
                        row["imported_from_path"],
                        row["normalized_url"],
                        row["history_scope"],
                    ],
                )
            else:
                _insert_dict(con, "url_history", row)
    return len(rows)


def write_run_history(
    db_path: Path,
    *,
    run_id: str,
    diagnostics: RunDiagnostics,
    candidate_articles: list[dict[str, Any]] | None = None,
    summarized_articles: list[dict[str, Any]] | None = None,
    selected_articles: list[dict[str, Any]] | None = None,
    article_summary_records: list[dict[str, Any]] | None = None,
    story_summary_records: list[dict[str, Any]] | None = None,
    run_log_path: str = "",
    export_csv: bool = True,
) -> None:
    ensure_schema(db_path)
    imported_at = datetime.now().isoformat(timespec="seconds")
    run_started_at = str(diagnostics.run_started_at or _run_started_at_from_id(run_id))
    settings = diagnostics.settings or {}
    preset_id = str(settings.get("preset_id") or "")
    url_reuse_blocking = bool(settings.get("url_reuse_blocking_enabled"))
    with connect(db_path) as con:
        con.execute("BEGIN TRANSACTION")
        _delete_run_rows(con, run_id)
        _insert_run(con, run_id, diagnostics, imported_at=imported_at)
        _insert_sources(con, run_id, diagnostics.source_runs)
        _insert_run_articles(con, run_id, "candidate", candidate_articles or [])
        _insert_run_articles(con, run_id, "summarized", summarized_articles or [])
        _insert_run_articles(con, run_id, "selected", selected_articles or [])
        _insert_article_summaries(con, run_id, "summarized", article_summary_records or [])
        _insert_article_summaries(con, run_id, "story_assigned", story_summary_records or [])
        _insert_artifacts(con, run_id, diagnostics.artifacts, imported=False)
        _insert_reports_as_artifacts(con, run_id, diagnostics.reports)
        _insert_run_log(con, run_id, run_log_path)
        con.execute("COMMIT")

    urls = [str(article.get("url") or "") for article in candidate_articles or []]
    upsert_url_history(
        db_path,
        run_id=run_id,
        run_started_at=run_started_at,
        preset_id=preset_id,
        url_reuse_blocking_enabled=url_reuse_blocking,
        urls=urls,
        articles=candidate_articles,
    )
    if export_csv:
        export_history_csvs(db_path)


def export_history_csvs(db_path: Path) -> list[Path]:
    ensure_schema(db_path)
    export_dir = db_path.parent
    exports = [
        ("runs", "SELECT * FROM runs ORDER BY run_started_at DESC, run_id DESC"),
        (
            "run_sources",
            "SELECT s.* FROM run_sources s LEFT JOIN runs r USING (run_id) "
            "ORDER BY r.run_started_at DESC, s.source_index ASC, s.source ASC",
        ),
        (
            "article_summaries",
            "SELECT a.* FROM article_summaries a LEFT JOIN runs r USING (run_id) "
            "ORDER BY r.run_started_at DESC, a.stage ASC, a.row_index ASC",
        ),
        (
            "run_articles",
            "SELECT a.* FROM run_articles a LEFT JOIN runs r USING (run_id) "
            "ORDER BY r.run_started_at DESC, a.stage ASC, a.row_index ASC",
        ),
        (
            "url_history",
            "SELECT * FROM url_history ORDER BY last_seen_at DESC, history_scope ASC, url ASC",
        ),
        (
            "artifacts",
            "SELECT a.* FROM artifacts a LEFT JOIN runs r USING (run_id) "
            "ORDER BY r.run_started_at DESC, a.name ASC",
        ),
    ]
    written: list[Path] = []
    with connect(db_path) as con:
        for table_name, query in exports:
            output_path = export_dir / f"{table_name}.csv"
            columns = [desc[0] for desc in con.execute(query).description]
            rows = con.fetchall()
            with output_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                writer.writerows(rows)
            written.append(output_path)
    return written


def backfill_outputs(
    output_dir: Path,
    db_path: Path,
    *,
    dry_run: bool,
    export_csv: bool = True,
) -> HistoryCommandResult:
    run_ids = _discover_run_ids(output_dir)
    result = HistoryCommandResult(
        action="backfill",
        dry_run=dry_run,
        db_path=db_path,
        run_count=len(run_ids),
    )
    if dry_run:
        result.details = [f"- Would import run {run_id}" for run_id in sorted(run_ids, reverse=True)[:40]]
        if len(run_ids) > 40:
            result.details.append(f"- ...and {len(run_ids) - 40} more run(s)")
        return result

    ensure_schema(db_path)
    for run_id in sorted(run_ids):
        _backfill_run(output_dir, db_path, run_id)
    if export_csv:
        export_history_csvs(db_path)
    result.details = [f"- Imported {len(run_ids)} run(s) from {output_dir}"]
    return result


def cleanup_outputs(
    output_dir: Path,
    db_path: Path,
    *,
    dry_run: bool,
) -> HistoryCommandResult:
    candidates = _visible_output_cleanup_candidates(output_dir)
    result = HistoryCommandResult(
        action="cleanup",
        dry_run=dry_run,
        db_path=db_path,
        file_count=len(candidates),
        byte_count=sum(path.stat().st_size for path in candidates if path.exists()),
    )
    result.details = [
        f"- {'Would delete' if dry_run else 'Deleting'} {path.relative_to(output_dir.parent)}"
        for path in candidates[:80]
    ]
    if len(candidates) > 80:
        result.details.append(f"- ...and {len(candidates) - 80} more file(s)")
    if dry_run:
        return result

    deleted_count = 0
    deleted_bytes = 0
    for path in candidates:
        if not path.exists():
            continue
        size = path.stat().st_size
        path.unlink()
        deleted_count += 1
        deleted_bytes += size
    _remove_empty_output_dirs(output_dir)
    result.deleted_count = deleted_count
    result.deleted_bytes = deleted_bytes
    return result


def cleanup_visible_outputs(
    output_dir: Path,
    *,
    keep_paths: Iterable[Path] | None = None,
) -> tuple[int, int]:
    keep = {
        *(path.resolve() for path in (keep_paths or [])),
        *(output_dir / filename for filename in ROLLING_RUN_FILENAMES),
    }
    keep = {path.resolve() for path in keep}
    candidates = _visible_output_cleanup_candidates(output_dir, keep_paths=keep)
    deleted_count = 0
    deleted_bytes = 0
    for path in candidates:
        if not path.exists():
            continue
        size = path.stat().st_size
        path.unlink()
        deleted_count += 1
        deleted_bytes += size
    _remove_empty_output_dirs(output_dir)
    return deleted_count, deleted_bytes


def parse_history_args(args: list[str], *, output_dir: Path, db_path: Path, export_csv: bool) -> HistoryCommandResult:
    if not args or args[0] in {"-h", "--help", "help"}:
        raise ValueError(_history_usage())
    action = args[0]
    options = set(args[1:])
    dry_run = "--apply" not in options
    if "--dry-run" in options:
        dry_run = True
    if action == "backfill":
        return backfill_outputs(output_dir, db_path, dry_run=dry_run, export_csv=export_csv)
    if action == "cleanup":
        return cleanup_outputs(output_dir, db_path, dry_run=dry_run)
    if action == "export":
        ensure_schema(db_path)
        written = export_history_csvs(db_path)
        return HistoryCommandResult(
            action="export",
            dry_run=False,
            db_path=db_path,
            file_count=len(written),
            details=[f"- Wrote {path}" for path in written],
        )
    raise ValueError(_history_usage())


def _history_usage() -> str:
    return (
        "Usage:\n"
        "  uv run news history backfill [--dry-run|--apply]\n"
        "  uv run news history cleanup [--dry-run|--apply]\n"
        "  uv run news history export\n"
    )


def _insert_run(con: Any, run_id: str, diagnostics: RunDiagnostics, *, imported_at: str, imported_from_path: str = "") -> None:
    settings = diagnostics.settings or {}
    stats = diagnostics.summary_stats()
    completed = _last_event_at(diagnostics.events)
    aborted = _last_event(diagnostics.events, "aborted")
    status = run_status_from_events(diagnostics.events)
    token_totals = stats.get("model_token_totals") or {}
    row = {
        "run_id": run_id,
        "run_started_at": diagnostics.run_started_at,
        "run_completed_at": completed,
        "run_date": run_id[:10],
        "preset_id": settings.get("preset_id") or "custom",
        "status": status,
        "abort_reason": aborted.get("reason", ""),
        "duration_seconds": _duration_seconds(diagnostics.run_started_at, completed),
        "duration_label": stats.get("duration"),
        "model": settings.get("model"),
        "model_name": settings.get("model_name"),
        "model_backend": settings.get("model_backend"),
        "model_default_sampling_json": _json(settings.get("model_default_sampling")),
        "model_task_sampling_json": _json(settings.get("model_task_sampling")),
        "story_cluster_similarity_threshold": _float(settings.get("story_cluster_similarity_threshold")),
        "story_selection_overlap_threshold": _float(settings.get("story_selection_overlap_threshold")),
        "story_embedding_dedup_threshold": _float(settings.get("story_embedding_dedup_threshold")),
        "min_articles_per_story": _int(settings.get("min_articles_per_story")),
        "max_stories": _int(settings.get("max_stories")),
        "source_count": _int(settings.get("source_count")),
        "feed_item_count": _int(stats.get("feed_item_count")),
        "selected_item_count": _int(stats.get("selected_item_count")),
        "fresh_article_count": _int(stats.get("fresh_article_count")),
        "story_count": _int(stats.get("story_count")),
        "story_included_count": _int(stats.get("story_included_count")),
        "story_dropped_count": _int(stats.get("story_dropped_count")),
        "story_draft_count": _int(stats.get("story_draft_count")),
        "story_scale_kept_count": _int(stats.get("story_scale_kept_count")),
        "story_scale_dropped_count": _int(stats.get("story_scale_dropped_count")),
        "selected_story_count": _int(stats.get("selected_story_count")),
        "story_selection_candidate_count": _int(stats.get("story_selection_candidate_count")),
        "story_coverage_deficit": _int(stats.get("story_coverage_deficit")),
        "article_summary_count": _int(stats.get("article_summary_count")),
        "report_count": _int(stats.get("report_count")),
        "recipient_count": _int(stats.get("recipient_count")),
        "reports_with_images": _int(stats.get("reports_with_images")),
        "image_warnings": _int(stats.get("image_warnings")),
        "model_call_count": _int(stats.get("model_call_count")),
        "model_calls_json": _json(stats.get("model_calls")),
        "model_token_totals_json": _json(token_totals),
        "model_retries": _int(stats.get("model_retries")),
        "model_fallbacks": _int(stats.get("model_fallbacks")),
        "source_status_counts_json": _json(stats.get("source_status_counts")),
        "rejection_counts_json": _json(stats.get("rejection_counts")),
        "settings_json": _json(settings),
        "stats_json": _json(stats),
        "events_json": _json(diagnostics.events),
        "reports_json": _json(diagnostics.reports),
        "artifacts_json": _json(diagnostics.artifacts),
        "imported_from_path": imported_from_path,
        "imported_at": imported_at,
    }
    _insert_dict(con, "runs", row)


def _insert_sources(con: Any, run_id: str, source_runs: list[dict[str, Any]]) -> None:
    for fallback_index, source in enumerate(source_runs, start=1):
        row = {
            "run_id": run_id,
            "source_index": _int(source.get("source_index") or fallback_index),
            "source": str(source.get("source") or ""),
            "status": str(source.get("status") or ""),
            "reason": str(source.get("reason") or ""),
            "started_at": str(source.get("started_at") or ""),
            "completed_at": str(source.get("completed_at") or ""),
            "elapsed_seconds": _float(source.get("elapsed_seconds")),
            "feed_item_count": _int(source.get("feed_item_count")),
            "recent_item_count": _int(source.get("recent_item_count")),
            "selected_item_count": _int(source.get("selected_item_count")),
            "fresh_article_count": _int(source.get("fresh_article_count")),
            "timeout_count": _int(source.get("timeout_count")),
            "slow_source": bool(source.get("slow_source")),
            "scrape_status_counts_json": _json(source.get("scrape_status_counts")),
            "rejected_counts_json": _json(source.get("rejected_counts")),
        }
        _insert_dict(con, "run_sources", row)


def _insert_run_articles(con: Any, run_id: str, stage: str, articles: list[dict[str, Any]], *, imported_from_path: str = "") -> None:
    seen: set[tuple[str, str]] = set()
    row_index = 0
    for article in articles:
        url = str(article.get("url") or "").strip()
        article_id = str(article.get("article_id") or "").strip()
        dedupe_key = (normalize_url_for_history(url), article_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        row_index += 1
        row = {
            "run_id": run_id,
            "stage": stage,
            "row_index": row_index,
            "url": url,
            "normalized_url": normalize_url_for_history(url),
            "source": str(article.get("source") or ""),
            "title": str(article.get("title") or ""),
            "published": str(article.get("pub_date") or article.get("published") or ""),
            "article_id": article_id,
            "original_rss_url": str(article.get("original_rss_url") or ""),
            "resolved_url": str(article.get("resolved_url") or ""),
            "scrape_status": str(article.get("scrape_status") or ""),
            "resolution_status": str(article.get("resolution_status") or ""),
            "imported_from_path": imported_from_path,
        }
        _insert_dict(con, "run_articles", row)


def _insert_article_summaries(
    con: Any,
    run_id: str,
    stage: str,
    summaries: list[dict[str, Any]],
    *,
    imported_from_path: str = "",
    generated_at: str = "",
) -> None:
    for fallback_index, summary in enumerate(summaries, start=1):
        row = {
            "run_id": run_id,
            "stage": stage,
            "row_index": _int(summary.get("index") or fallback_index),
            "title": str(summary.get("title") or ""),
            "source": str(summary.get("source") or ""),
            "published": str(summary.get("published") or ""),
            "url": str(summary.get("url") or ""),
            "article_id": str(summary.get("article_id") or ""),
            "story": str(summary.get("story") or ""),
            "summary": str(summary.get("summary") or ""),
            "generated_at": generated_at,
            "imported_from_path": imported_from_path,
        }
        _insert_dict(con, "article_summaries", row)


def _insert_artifacts(con: Any, run_id: str, artifacts: dict[str, Any], *, imported: bool) -> None:
    imported_at = datetime.now().isoformat(timespec="seconds")
    for name, details in sorted((artifacts or {}).items()):
        if isinstance(details, dict):
            path = str(details.get("path") or "")
            metadata = {key: value for key, value in details.items() if key != "path"}
        else:
            path = str(details or "")
            metadata = {}
        if not path:
            continue
        row = {
            "run_id": run_id,
            "name": str(name),
            "path": path,
            "family": _artifact_family(Path(path).name),
            "metadata_json": _json(metadata),
            "imported": imported,
            "imported_at": imported_at,
        }
        _insert_dict(con, "artifacts", row)


def _insert_reports_as_artifacts(con: Any, run_id: str, reports: list[dict[str, Any]]) -> None:
    imported_at = datetime.now().isoformat(timespec="seconds")
    for index, report in enumerate(reports or [], start=1):
        path = str(report.get("path") or "")
        if path:
            _insert_dict(
                con,
                "artifacts",
                {
                    "run_id": run_id,
                    "name": f"report_{index}",
                    "path": path,
                    "family": "final_report",
                    "metadata_json": _json({key: value for key, value in report.items() if key != "path"}),
                    "imported": False,
                    "imported_at": imported_at,
                },
            )
        image_art = report.get("image_art") or {}
        image_path = str(image_art.get("final_image_path") or "")
        if image_path:
            _insert_dict(
                con,
                "artifacts",
                {
                    "run_id": run_id,
                    "name": f"report_{index}_image",
                    "path": image_path,
                    "family": "final_image",
                    "metadata_json": _json(image_art),
                    "imported": False,
                    "imported_at": imported_at,
                },
            )


def _insert_run_log(con: Any, run_id: str, run_log_path: str) -> None:
    if not run_log_path:
        return
    path = Path(run_log_path)
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    row = {
        "run_id": run_id,
        "path": str(path),
        "byte_count": path.stat().st_size,
        "content": content,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
    }
    _insert_dict(con, "run_logs", row)


def _delete_run_rows(con: Any, run_id: str) -> None:
    for table in ("run_logs", "artifacts", "article_summaries", "run_articles", "run_sources", "runs"):
        con.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])


def _insert_dict(con: Any, table: str, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    con.execute(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
        [row[column] for column in columns],
    )


def _backfill_run(output_dir: Path, db_path: Path, run_id: str) -> None:
    details_path = _find_run_file(output_dir, run_id, f"run_details_{run_id}.json")
    diagnostics = _load_diagnostics_for_backfill(run_id, details_path)
    summary_records = _load_summary_files(output_dir, run_id)
    url_articles = _load_url_list_articles(output_dir, run_id)
    candidate_articles = _articles_from_diagnostics(diagnostics)
    if url_articles.get("candidate"):
        candidate_articles = _merge_article_lists(candidate_articles, url_articles["candidate"])
    selected_articles = url_articles.get("selected", [])
    summarized_articles = _summarized_articles(summary_records)
    run_log_path = str(_find_run_file(output_dir, run_id, f"run_log_{run_id}.log") or "")
    ensure_schema(db_path)
    imported_at = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as con:
        con.execute("BEGIN TRANSACTION")
        _delete_run_rows(con, run_id)
        _insert_run(
            con,
            run_id,
            diagnostics,
            imported_at=imported_at,
            imported_from_path=str(details_path or ""),
        )
        _insert_sources(con, run_id, diagnostics.source_runs)
        _insert_run_articles(con, run_id, "candidate", candidate_articles)
        _insert_run_articles(con, run_id, "selected", selected_articles)
        _insert_run_articles(con, run_id, "summarized", summarized_articles)
        for stage, records in summary_records.items():
            _insert_article_summaries(
                con,
                run_id,
                stage,
                records["summaries"],
                imported_from_path=records["path"],
                generated_at=records["generated_at"],
            )
        _insert_artifacts(con, run_id, diagnostics.artifacts, imported=True)
        _insert_reports_as_artifacts(con, run_id, diagnostics.reports)
        _insert_backfill_file_artifacts(con, output_dir, run_id)
        _insert_run_log(con, run_id, run_log_path)
        con.execute("COMMIT")

    settings = diagnostics.settings or {}
    urls = [article.get("url", "") for article in candidate_articles]
    upsert_url_history(
        db_path,
        run_id=run_id,
        run_started_at=diagnostics.run_started_at,
        preset_id=str(settings.get("preset_id") or ""),
        url_reuse_blocking_enabled=bool(settings.get("url_reuse_blocking_enabled")),
        urls=urls,
        articles=candidate_articles,
    )


def _load_diagnostics_for_backfill(run_id: str, details_path: Path | None) -> RunDiagnostics:
    if details_path and details_path.exists():
        payload = json.loads(details_path.read_text(encoding="utf-8"))
        return RunDiagnostics(
            run_started_at=str(payload.get("run_started_at") or _run_started_at_from_id(run_id)),
            settings=payload.get("settings") or {},
            events=payload.get("events") or [],
            top_funnel=payload.get("top_funnel") or {},
            source_runs=payload.get("source_runs") or [],
            article_budget=payload.get("article_budget") or {},
            model_call_stats=payload.get("model_call_stats") or {},
            activity_snapshots=payload.get("activity_snapshots") or [],
            article_summary_count=_int(payload.get("article_summary_count")),
            reports=payload.get("reports") or [],
            artifacts=payload.get("artifacts") or {},
        )
    return RunDiagnostics(
        run_started_at=_run_started_at_from_id(run_id),
        settings={
            "preset_id": "imported",
            "source_scope": "",
            "recipient_scope": "",
            "url_reuse_blocking_enabled": False,
            "output_dir": "",
        },
    )


def _load_summary_files(output_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    run_dir = output_dir / run_id[:10]
    if not run_dir.exists():
        return records
    for path in run_dir.iterdir():
        match = SUMMARY_FILE_RE.match(path.name)
        if not match or match.group(2) != run_id:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        stage = SUMMARY_STAGE_BY_PREFIX.get(match.group(1), match.group(1))
        records[stage] = {
            "path": str(path),
            "generated_at": str(payload.get("generated_at") or ""),
            "summaries": payload.get("summaries") or [],
        }
    return records


def _load_url_list_articles(output_dir: Path, run_id: str) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    run_dir = output_dir / run_id[:10]
    if not run_dir.exists():
        return records
    for path in run_dir.iterdir():
        match = URL_LIST_FILE_RE.match(path.name)
        if not match or match.group(2) != run_id:
            continue
        stage = URL_STAGE_BY_PREFIX.get(match.group(1), match.group(1))
        articles = []
        for index, url in enumerate(_read_url_lines(path), start=1):
            articles.append({"url": url, "article_id": f"{stage}-url-{index}"})
        records[stage] = articles
    return records


def _articles_from_diagnostics(diagnostics: RunDiagnostics) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for source in diagnostics.source_runs:
        source_name = str(source.get("source") or "")
        for article in source.get("fresh_articles") or []:
            if isinstance(article, dict):
                merged = {"source": source_name, **article}
                articles.append(merged)
    return articles


def _summarized_articles(summary_records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records = summary_records.get("summarized") or {}
    articles = []
    for summary in records.get("summaries") or []:
        articles.append(
            {
                "url": summary.get("url"),
                "source": summary.get("source"),
                "title": summary.get("title"),
                "published": summary.get("published"),
                "article_id": summary.get("article_id"),
            }
        )
    return articles


def _merge_article_lists(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url = {
        normalize_url_for_history(str(article.get("url") or "")): article
        for article in primary
        if str(article.get("url") or "").strip()
    }
    merged = list(primary)
    for article in fallback:
        key = normalize_url_for_history(str(article.get("url") or ""))
        if key and key not in by_url:
            merged.append(article)
            by_url[key] = article
    return merged


def _insert_backfill_file_artifacts(con: Any, output_dir: Path, run_id: str) -> None:
    run_dir = output_dir / run_id[:10]
    if not run_dir.exists():
        return
    imported_at = datetime.now().isoformat(timespec="seconds")
    for path in run_dir.iterdir():
        timestamp_match = TIMESTAMP_RE.search(path.name)
        if timestamp_match and timestamp_match.group(1) != run_id:
            continue
        if not timestamp_match:
            continue
        row = {
            "run_id": run_id,
            "name": path.name,
            "path": str(path),
            "family": _artifact_family(path.name),
            "metadata_json": _json({"byte_count": path.stat().st_size}),
            "imported": True,
            "imported_at": imported_at,
        }
        try:
            _insert_dict(con, "artifacts", row)
        except Exception:
            pass


def _discover_run_ids(output_dir: Path) -> set[str]:
    run_ids: set[str] = set()
    if not output_dir.exists():
        return run_ids
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        match = TIMESTAMP_RE.search(path.name)
        if match:
            run_ids.add(match.group(1))
    return run_ids


def _find_run_file(output_dir: Path, run_id: str, filename: str) -> Path | None:
    path = output_dir / run_id[:10] / filename
    return path if path.exists() else None




def _visible_output_cleanup_candidates(
    output_dir: Path,
    *,
    keep_paths: Iterable[Path] | None = None,
) -> list[Path]:
    if not output_dir.exists():
        return []
    keep = {path.resolve() for path in (keep_paths or [])}
    for filename in ROLLING_RUN_FILENAMES:
        keep.add((output_dir / filename).resolve())
    candidates: list[Path] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() in keep:
            continue
        candidates.append(path)
    return candidates


def _remove_empty_output_dirs(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for path in sorted(
        (path for path in output_dir.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            continue








def _artifact_family(filename: str) -> str:
    if filename.startswith("run_details_"):
        return "run_details"
    if filename.startswith("run_summary_"):
        return "run_summary"
    if filename.startswith("run_log_"):
        return "run_log"
    if SUMMARY_FILE_RE.match(filename):
        return "article_summary_json"
    if URL_LIST_FILE_RE.match(filename):
        return "url_list"
    if filename.startswith("topics_"):
        return "topics"
    if filename.endswith("_primary_dataset.txt") or filename.endswith("_primary_dataset.json"):
        return "primary_dataset"
    if filename.endswith("_image_prompt.txt") or filename.endswith("_image_stats.json"):
        return "image_metadata"
    if filename.endswith("_raw.png"):
        return "raw_image"
    if filename.endswith("_image.png"):
        return "final_image"
    if filename.startswith("news_report_") and filename.endswith(".txt"):
        return "final_report"
    return "other"


def _read_url_lines(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def _history_scope(preset_id: str, url_reuse_blocking_enabled: bool) -> str:
    clean_preset = str(preset_id or "").strip() or "unknown"
    if url_reuse_blocking_enabled:
        return "global"
    return clean_preset


def _run_started_at_from_id(run_id: str) -> str:
    if TIMESTAMP_RE.fullmatch(run_id):
        date_part, time_part = run_id.split("_", 1)
        return f"{date_part}T{time_part.replace('-', ':')}"
    match = TIMESTAMP_RE.search(run_id)
    if match:
        return _run_started_at_from_id(match.group(1))
    return ""


def _last_event(events: list[dict[str, Any]], label: str) -> dict[str, Any]:
    for event in reversed(events or []):
        if event.get("label") == label:
            return event
    return {}


def _last_event_at(events: list[dict[str, Any]]) -> str:
    for event in reversed(events or []):
        at = str(event.get("at") or "")
        if at:
            return at
    return ""


def _duration_seconds(started_at: str, completed_at: str) -> int:
    try:
        start = datetime.fromisoformat(str(started_at))
        end = datetime.fromisoformat(str(completed_at))
    except ValueError:
        return 0
    return max(0, int(round((end - start).total_seconds())))


def _json(value: Any) -> str:
    try:
        return json.dumps(value or {}, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bytes_label(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f"{byte_count / 1024 / 1024:.1f} MB"
    if byte_count >= 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count} bytes"
