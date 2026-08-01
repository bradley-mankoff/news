"""Open Knowledge Format v0.2 projections for Daily News runs.

The OKF bundle is an output adapter. It consumes structured article and story
records already produced by the pipeline; it does not parse rendered reports or
query the DuckDB history store.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .article_summary_records import ArticleSummaryRecord, ensure_record
from .diagnostics import RunDiagnostics, run_status_from_events
from .story_records import StoryRecord, ensure_story_record


OKF_VERSION = "0.2"
OKF_PROCESS_ACTOR = "process:daily-news-pipeline"
_RESERVED_FILENAMES = {"index.md", "log.md"}
_STORY_METRIC_FIELDS = (
    "article_count",
    "cluster_article_count",
    "selected_article_count",
    "source_count",
    "average_similarity",
    "connectedness_score",
    "story_strength_score",
    "edge_density",
    "mean_best_similarity",
    "min_best_similarity",
    "min_member_average_similarity",
    "min_member_edge_degree",
    "member_cohesion_floor",
    "member_edge_degree_floor",
    "pruned_article_ids",
    "prune_reason",
    "story_rank",
    "global_selection_rank",
)


@dataclass
class _ArticleConcept:
    record: ArticleSummaryRecord
    identity: str
    filename: str = ""


@dataclass
class _StoryConcept:
    record: StoryRecord
    identity: str
    filename: str = ""


class OKFRunBundleSerializer:
    """Write one deterministic, replace-on-success OKF Run Bundle."""

    def __init__(
        self,
        history_db_path: Path,
        *,
        run_id: str,
        diagnostics: RunDiagnostics,
        report_body: str = "",
        article_summary_records: Sequence[ArticleSummaryRecord | Mapping[str, Any]] | None = None,
        story_summary_records: Sequence[ArticleSummaryRecord | Mapping[str, Any]] | None = None,
        story_records: Sequence[StoryRecord | Mapping[str, Any]] | None = None,
        candidate_articles: Sequence[Mapping[str, Any]] | None = None,
        summarized_articles: Sequence[Mapping[str, Any]] | None = None,
        selected_articles: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.history_db_path = Path(history_db_path)
        self.run_id = str(run_id or "").strip() or "unknown-run"
        self.diagnostics = diagnostics
        self.report_body = str(report_body or "")
        self.article_summary_records = article_summary_records
        self.story_summary_records = story_summary_records
        self.story_records = story_records
        self.candidate_articles = candidate_articles
        self.summarized_articles = summarized_articles
        self.selected_articles = selected_articles

    @property
    def status(self) -> str:
        """Return OKF lifecycle status from the diagnostic terminal event."""
        return "stable" if run_status_from_events(self.diagnostics.events) == "completed" else "draft"

    @property
    def generated_at(self) -> str:
        raw_started_at = str(self.diagnostics.run_started_at or "").strip()
        if raw_started_at:
            try:
                parsed = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
                return parsed.isoformat(timespec="seconds")
            except ValueError:
                pass
        match = re.match(
            r"^(\d{4}-\d{2}-\d{2})(?:[_T](\d{2})[-:](\d{2})[-:](\d{2}))?",
            self.run_id,
        )
        if match:
            date_part, hour, minute, second = match.groups()
            return f"{date_part}T{hour or '00'}:{minute or '00'}:{second or '00'}"
        return "1970-01-01T00:00:00"

    @property
    def run_date(self) -> str:
        return self.generated_at[:10]

    @property
    def bundle_path(self) -> Path:
        run_component = _safe_run_component(self.run_id)
        return self.history_db_path.parent / "okf" / run_component

    def write(self) -> Path:
        """Build in a sibling staging directory, then replace the run bundle."""
        target = self.bundle_path
        okf_root = target.parent
        okf_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=okf_root))
        try:
            self._write_tree(staging)
            self._replace_tree(staging, target)
        except Exception:
            _remove_tree(staging)
            raise
        return target

    def _write_tree(self, root: Path) -> None:
        articles_dir = root / "articles"
        stories_dir = root / "stories"
        articles_dir.mkdir(parents=True, exist_ok=True)
        stories_dir.mkdir(parents=True, exist_ok=True)

        article_concepts = self._article_concepts()
        story_concepts = self._story_concepts(article_concepts)
        _allocate_filenames(article_concepts, "article")
        _allocate_filenames(story_concepts, "story")

        article_paths = {
            entry.identity: f"articles/{entry.filename}" for entry in article_concepts
        }
        story_paths = {
            entry.identity: f"stories/{entry.filename}" for entry in story_concepts
        }
        article_id_paths = {
            entry.record.article_id.strip(): article_paths[entry.identity]
            for entry in article_concepts
            if entry.record.article_id.strip()
        }
        story_key_paths = {
            entry.record.story_key.strip(): story_paths[entry.identity]
            for entry in story_concepts
            if entry.record.story_key.strip()
        }
        story_title_paths = {
            entry.record.story_title.strip(): story_paths[entry.identity]
            for entry in story_concepts
            if entry.record.story_title.strip()
        }

        for entry in article_concepts:
            body = self._article_body(entry.record, story_key_paths, story_title_paths)
            frontmatter = self._article_frontmatter(entry.record)
            _write_document(articles_dir / entry.filename, frontmatter, body)

        for entry in story_concepts:
            body = self._story_body(entry.record, article_id_paths)
            frontmatter = self._story_frontmatter(entry.record, article_id_paths)
            _write_document(stories_dir / entry.filename, frontmatter, body)

        report_body = self._report_body(story_concepts, story_paths)
        report_sources = [
            _source_entry(story_paths[entry.identity], entry.record.story_title or "News story")
            for entry in story_concepts
        ]
        _write_document(
            root / "report.md",
            self._base_frontmatter(
                "Daily News Report",
                "The rendered report for this OKF Run Bundle.",
                concept_type="Daily News Report",
                tags=["daily-news", "report"],
                sources=report_sources,
            ),
            report_body,
        )

        _write_text(root / "index.md", self._root_index(story_concepts, article_concepts))
        _write_text(root / "log.md", self._log(story_concepts))
        _write_text(articles_dir / "index.md", self._group_index("Articles", article_concepts))
        _write_text(stories_dir / "index.md", self._group_index("Stories", story_concepts))

    def _article_concepts(self) -> list[_ArticleConcept]:
        records_by_identity: dict[str, ArticleSummaryRecord] = {}
        ordered_values: list[Any] = []
        for values in (
            self.candidate_articles,
            self.summarized_articles,
            self.selected_articles,
            self.article_summary_records,
            self.story_summary_records,
        ):
            ordered_values.extend(values or [])
        for value in ordered_values:
            record = _coerce_article_record(value)
            if record is None:
                continue
            records_by_identity[_article_identity(record)] = record
        concepts = [
            _ArticleConcept(record=record, identity=identity)
            for identity, record in records_by_identity.items()
        ]
        return sorted(concepts, key=lambda entry: (entry.record.title.casefold(), entry.identity))

    def _story_concepts(self, article_concepts: list[_ArticleConcept]) -> list[_StoryConcept]:
        if self.story_records is None:
            records = _derived_story_records(article_concepts)
        else:
            records = []
            for index, value in enumerate(self.story_records):
                record = _coerce_story_record(value, index=index)
                if record is not None:
                    records.append(record)
        unique: dict[str, StoryRecord] = {}
        for record in records:
            unique[_story_identity(record)] = record
        concepts = [
            _StoryConcept(record=record, identity=identity)
            for identity, record in unique.items()
        ]
        return sorted(
            concepts,
            key=lambda entry: (
                entry.record.global_selection_rank is None,
                entry.record.global_selection_rank or 0,
                entry.record.story_rank or 0,
                entry.record.story_title.casefold(),
                entry.identity,
            ),
        )

    def _base_frontmatter(
        self,
        title: str,
        description: str,
        *,
        concept_type: str,
        tags: list[str],
        resource: str | None = None,
        sources: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        frontmatter: dict[str, Any] = {
            "type": concept_type,
            "title": title,
            "description": _single_line(description),
            "tags": tags,
            "status": self.status,
            "generated": {"by": OKF_PROCESS_ACTOR, "at": self.generated_at},
        }
        if resource:
            frontmatter["resource"] = resource
        if sources:
            frontmatter["sources"] = sources
        return frontmatter

    def _article_frontmatter(self, record: ArticleSummaryRecord) -> dict[str, Any]:
        title = record.title.strip() or "Untitled article"
        url = record.url.strip()
        source_resource = url or f"article:{_article_identity(record)}"
        frontmatter = self._base_frontmatter(
            title,
            _description_from_text(
                record.summary,
                fallback=f"Article summary from {record.source or 'an unknown source'}.",
            ),
            concept_type="Article Summary",
            tags=["daily-news", "article"],
            resource=url or None,
            sources=[_source_entry(source_resource, record.source or "Original article")],
        )
        if record.article_id.strip():
            frontmatter["article_id"] = record.article_id.strip()
        return frontmatter

    def _story_frontmatter(
        self,
        record: StoryRecord,
        article_id_paths: dict[str, str],
    ) -> dict[str, Any]:
        source_entries = [
            _source_entry(f"../{article_id_paths[article_id]}", f"Article {article_id}")
            for article_id in record.article_ids
            if article_id in article_id_paths
        ]
        frontmatter = self._base_frontmatter(
            record.story_title.strip() or "News update",
            _description_from_text(
                _story_prose(record),
                fallback="A grouped Daily News story assembled from article summaries.",
            ),
            concept_type="News Story",
            tags=["daily-news", "story"],
            sources=source_entries,
        )
        if record.story_key.strip():
            frontmatter["story_key"] = record.story_key.strip()
        return frontmatter

    def _article_body(
        self,
        record: ArticleSummaryRecord,
        story_key_paths: dict[str, str],
        story_title_paths: dict[str, str],
    ) -> str:
        title = record.title.strip() or "Untitled article"
        summary = record.summary.strip() or "No article summary was generated for this record."
        lines = [f"# {title}", "", "## Summary", "", summary]
        if record.source.strip() or record.published.strip() or record.url.strip():
            lines.extend(["", "## Source details", ""])
            if record.source.strip():
                lines.append(f"- Source: {record.source.strip()}")
            if record.published.strip():
                lines.append(f"- Published: {record.published.strip()}")
            if record.url.strip():
                lines.append(f"- Original URL: <{record.url.strip()}>")
        story_path = story_key_paths.get(record.story.strip()) or story_title_paths.get(record.story.strip())
        if story_path:
            lines.extend(["", "## Story", "", f"- [{record.story.strip()}](../{story_path})"])
        elif record.story.strip():
            lines.extend(["", "## Story", "", f"- Story assignment: {record.story.strip()}"])
        return "\n".join(lines)

    def _story_body(self, record: StoryRecord, article_id_paths: dict[str, str]) -> str:
        title = record.story_title.strip() or "News update"
        lines = [f"# {title}"]
        if record.story_key.strip():
            lines.extend(["", f"- Story key: `{record.story_key.strip()}`"])
        prose = _story_prose(record)
        if prose:
            lines.extend(["", "## Story", "", prose])
        lines.extend(["", "## Articles", ""])
        linked_article_ids = [
            article_id for article_id in record.article_ids if article_id in article_id_paths
        ]
        if linked_article_ids:
            lines.extend(
                f"- [{article_id}](../{article_id_paths[article_id]})"
                for article_id in linked_article_ids
            )
        else:
            lines.append("- No article concepts were recorded for this story.")
        metrics = _story_metrics(record)
        if metrics:
            lines.extend(["", "## Metrics", ""])
            lines.extend(f"- `{key}`: {_metric_text(value)}" for key, value in metrics.items())
        return "\n".join(lines)

    def _report_body(self, story_concepts: list[_StoryConcept], story_paths: dict[str, str]) -> str:
        body = self.report_body
        if not body.strip():
            body = "# Daily News Report\n\nNo report body was generated for this run."
        if not body.endswith("\n"):
            body += "\n"
        if story_concepts:
            body += "\n## OKF Story Concepts\n\n"
            body += "\n".join(
                f"- [{entry.record.story_title or 'News update'}]({story_paths[entry.identity]})"
                for entry in story_concepts
            )
        else:
            body += "\n## OKF Story Concepts\n\nNo story concepts were recorded for this run."
        return body

    def _root_index(
        self,
        story_concepts: list[_StoryConcept],
        article_concepts: list[_ArticleConcept],
    ) -> str:
        lines = [
            "---",
            f'okf_version: "{OKF_VERSION}"',
            "---",
            "",
            "# OKF Run Bundle",
            "",
            f"Generated by `{OKF_PROCESS_ACTOR}` for run `{self.run_id}`.",
            f"Lifecycle status: **{self.status}**.",
            "",
            "# Run Concepts",
            "",
            "* [Daily News Report](report.md) - Rendered report body and story links.",
            "* [Articles](articles/) - Article summary concepts sourced from original URLs.",
            "* [Stories](stories/) - Story concepts linked to their article concepts.",
        ]
        if not article_concepts and not story_concepts:
            lines.extend(["", "No article or story concepts were recorded for this run."])
        return "\n".join(lines) + "\n"

    def _group_index(self, title: str, concepts: Sequence[_ArticleConcept | _StoryConcept]) -> str:
        lines = [f"# {title}", ""]
        if not concepts:
            lines.append("No concepts were recorded.")
        else:
            for entry in concepts:
                record = entry.record
                display_title = (
                    record.title if isinstance(record, ArticleSummaryRecord) else record.story_title
                ) or "Untitled concept"
                if isinstance(record, ArticleSummaryRecord):
                    description = _description_from_text(record.summary, fallback="Article summary concept.")
                else:
                    description = _description_from_text(
                        _story_prose(record), fallback="Grouped news story concept."
                    )
                lines.append(f"* [{display_title}]({entry.filename}) - {_single_line(description)}")
        return "\n".join(lines) + "\n"

    def _log(self, story_concepts: Sequence[_StoryConcept]) -> str:
        lines = ["# Directory Update Log", "", f"## {self.run_date}"]
        if story_concepts:
            lines.append(
                "* **Creation**: Generated the [Daily News Report](report.md) and "
                f"{len(story_concepts)} story concept(s) for run `{self.run_id}`."
            )
        else:
            lines.append(
                "* **Creation**: Generated the [Daily News Report](report.md) with no story concepts "
                f"for run `{self.run_id}`."
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _replace_tree(staging: Path, target: Path) -> None:
        backup: Path | None = None
        if target.exists() or target.is_symlink():
            backup = target.with_name(f".{target.name}.old-{next(tempfile._get_candidate_names())}")
            target.replace(backup)
        try:
            staging.replace(target)
        except Exception:
            if backup is not None and backup.exists():
                backup.replace(target)
            raise
        finally:
            if backup is not None:
                _remove_tree(backup)


def write_okf_run_bundle(
    history_db_path: Path,
    *,
    run_id: str,
    diagnostics: RunDiagnostics,
    report_body: str = "",
    article_summary_records: Sequence[ArticleSummaryRecord | Mapping[str, Any]] | None = None,
    story_summary_records: Sequence[ArticleSummaryRecord | Mapping[str, Any]] | None = None,
    story_records: Sequence[StoryRecord | Mapping[str, Any]] | None = None,
    candidate_articles: Sequence[Mapping[str, Any]] | None = None,
    summarized_articles: Sequence[Mapping[str, Any]] | None = None,
    selected_articles: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write and return ``output/history/okf/<run_id>/`` for one run."""
    return OKFRunBundleSerializer(
        history_db_path,
        run_id=run_id,
        diagnostics=diagnostics,
        report_body=report_body,
        article_summary_records=article_summary_records,
        story_summary_records=story_summary_records,
        story_records=story_records,
        candidate_articles=candidate_articles,
        summarized_articles=summarized_articles,
        selected_articles=selected_articles,
    ).write()


def source_id_for_resource(resource: str) -> str:
    """Return a stable source key for an OKF ``sources`` resource."""
    return "source-" + hashlib.sha256(str(resource).encode("utf-8")).hexdigest()[:16]


def _write_document(path: Path, frontmatter: Mapping[str, Any], body: str) -> None:
    yaml_text = yaml.safe_dump(dict(frontmatter), sort_keys=False, allow_unicode=True).rstrip()
    text = f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"
    _write_text(path, text)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text), encoding="utf-8")


def _source_entry(resource: str, title: str) -> dict[str, str]:
    clean_resource = str(resource or "").strip() or "unknown:source"
    return {
        "id": source_id_for_resource(clean_resource),
        "resource": clean_resource,
        "title": _single_line(title) or clean_resource,
    }


def _coerce_article_record(value: Any) -> ArticleSummaryRecord | None:
    if isinstance(value, ArticleSummaryRecord):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        if not data.get("story") and data.get("story_title"):
            data["story"] = data["story_title"]
        if not data.get("published") and data.get("pub_date"):
            data["published"] = data["pub_date"]
        return ensure_record(data)
    # Rendered Markdown is intentionally not an input adapter for OKF.
    return None


def _coerce_story_record(value: Any, *, index: int) -> StoryRecord | None:
    if isinstance(value, StoryRecord):
        return value
    if isinstance(value, Mapping):
        return ensure_story_record(dict(value), index=index)
    return None


def _article_identity(record: ArticleSummaryRecord) -> str:
    preferred = record.url.strip() or record.article_id.strip()
    if preferred:
        return preferred
    payload = "\x1f".join((record.title, record.source, record.published, record.summary))
    return "generated-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _story_identity(record: StoryRecord) -> str:
    if record.story_key.strip():
        return record.story_key.strip()
    payload = "\x1f".join((record.story_title, *record.article_ids))
    return "generated-story-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _derived_story_records(article_concepts: Sequence[_ArticleConcept]) -> list[StoryRecord]:
    grouped: dict[str, list[str]] = {}
    for entry in article_concepts:
        story_title = entry.record.story.strip()
        article_id = entry.record.article_id.strip()
        if story_title and article_id:
            grouped.setdefault(story_title, []).append(article_id)
    return [
        StoryRecord(
            story_key="derived-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16],
            story_title=title,
            article_ids=tuple(article_ids),
            cluster_article_ids=tuple(article_ids),
            article_count=len(article_ids),
            cluster_article_count=len(article_ids),
            selected_article_count=len(article_ids),
        )
        for title, article_ids in sorted(grouped.items(), key=lambda item: item[0].casefold())
    ]


def _allocate_filenames(concepts: Sequence[_ArticleConcept | _StoryConcept], default_prefix: str) -> None:
    grouped: dict[str, list[_ArticleConcept | _StoryConcept]] = {}
    for concept in concepts:
        if isinstance(concept.record, ArticleSummaryRecord):
            label = concept.record.title
        else:
            label = concept.record.story_title
        base = _safe_component(label, fallback=default_prefix)
        grouped.setdefault(base, []).append(concept)
    for base, entries in grouped.items():
        for index, entry in enumerate(sorted(entries, key=lambda item: item.identity), start=1):
            suffix = "" if index == 1 else f"--{index}"
            entry.filename = f"{base}{suffix}.md"


def _safe_component(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-.").lower()
    if clean in {"index", "log"} or not clean:
        return fallback
    return clean[:99]

def _safe_run_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return clean or "unknown-run"


def _description_from_text(text: str, *, fallback: str) -> str:
    clean = _single_line(text)
    if not clean:
        return _single_line(fallback)
    return clean[:200]


def _single_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()


def _story_prose(record: StoryRecord) -> str:
    extras = record.extras or {}
    for key in ("main_story_paragraph", "paragraph", "story_text", "preview"):
        value = extras.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _story_metrics(record: StoryRecord) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_name in _STORY_METRIC_FIELDS:
        value = getattr(record, field_name, None)
        if value not in (None, "", (), []):
            safe = _safe_metric(value)
            if safe is not None:
                result[field_name] = safe
    for key, value in sorted((record.extras or {}).items(), key=lambda item: str(item[0])):
        clean_key = str(key).strip()
        if not clean_key or clean_key.startswith("_") or clean_key in _STORY_METRIC_FIELDS:
            continue
        safe = _safe_metric(value)
        if safe is not None:
            result[clean_key] = safe
    return result


def _safe_metric(value: Any, *, depth: int = 0) -> Any:
    if depth > 2:
        return None
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return _single_line(value)[:500] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        if len(value) > 20:
            return None
        values = [_safe_metric(item, depth=depth + 1) for item in value]
        return values if all(item is not None for item in values) else None
    if isinstance(value, Mapping):
        if len(value) > 20:
            return None
        result: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            safe = _safe_metric(item, depth=depth + 1)
            if safe is None:
                return None
            result[_single_line(key)[:80]] = safe
        return result
    return None


def _metric_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


OKFSerializer = OKFRunBundleSerializer
