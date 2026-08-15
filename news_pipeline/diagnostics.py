"""Run diagnostics for the global story-first news funnel."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass
class RunDiagnostics:
    run_started_at: str
    settings: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    top_funnel: dict[str, Any] = field(default_factory=dict)
    source_runs: list[dict[str, Any]] = field(default_factory=list)
    article_budget: dict[str, Any] = field(default_factory=dict)
    model_call_stats: dict[str, Any] = field(default_factory=dict)
    activity_snapshots: list[dict[str, Any]] = field(default_factory=list)
    article_summary_count: int = 0
    reports: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)
    prompt_snapshots: list[dict[str, Any]] = field(default_factory=list)
    # Private: guards snapshot append/sequence assignment so concurrent
    # article-summary/story-drafting workers cannot corrupt ordering. Kept out
    # of repr/equality and never serialized by ``to_dict()``.
    _prompt_snapshots_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def event(self, label: str, **details: Any) -> None:
        self.events.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "label": label,
                **details,
            }
        )

    def record_top_funnel(
        self,
        *,
        providers: dict[str, list[dict[str, Any]]],
        merged: list[dict[str, Any]],
        seed_merged: list[dict[str, Any]] | None = None,
        validation_merged: list[dict[str, Any]] | None = None,
        provider_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.top_funnel = {
            "provider_metadata": provider_metadata or {},
            "providers": {
                provider: [
                    _story_digest(story, rank=index)
                    for index, story in enumerate(stories, start=1)
                ]
                for provider, stories in providers.items()
            },
            "merged": [
                _story_digest(story, rank=index)
                for index, story in enumerate(merged, start=1)
            ],
            "seed_merged": [
                _story_digest(story, rank=index)
                for index, story in enumerate(seed_merged or [], start=1)
            ],
            "validation_merged": [
                _story_digest(story, rank=index)
                for index, story in enumerate(validation_merged or [], start=1)
            ],
            "provider_counts": {
                provider: len(stories) for provider, stories in providers.items()
            },
            "merged_count": len(merged),
            "seed_merged_count": len(seed_merged or []),
            "validation_merged_count": len(validation_merged or []),
            "multi_provider_count": sum(
                1 for story in merged if len(story.get("providers", [])) >= 2
            ),
        }

    def record_source_run(self, source_run: dict[str, Any]) -> None:
        self.source_runs.append(source_run)

    def record_article_budget(self, details: dict[str, Any]) -> None:
        self.article_budget = details

    def record_model_call_stats(self, details: dict[str, Any]) -> None:
        self.model_call_stats = details

    def record_activity_snapshot(self, details: dict[str, Any]) -> None:
        self.activity_snapshots.append(details)

    def record_prompt_snapshot(self, details: dict[str, Any]) -> int:
        """Append one JSON-ready prompt snapshot under a private lock.

        The sequence is assigned atomically (capture order, not pipeline
        semantic order); the caller receives it back so retry/fallback
        metadata can be updated later on the same logical call. A shallow
        copy is stored so later caller-side metadata updates cannot mutate the
        recorded payload.
        """
        snapshot = dict(details or {})
        with self._prompt_snapshots_lock:
            sequence = len(self.prompt_snapshots) + 1
            snapshot["sequence"] = sequence
            self.prompt_snapshots.append(snapshot)
        return sequence

    def update_prompt_snapshot(self, sequence: int, **updates: Any) -> None:
        """Apply retry/fallback metadata to an already recorded snapshot."""
        with self._prompt_snapshots_lock:
            for snapshot in self.prompt_snapshots:
                if snapshot.get("sequence") == sequence:
                    snapshot.update(updates)
                    return

    def record_report(self, **details: Any) -> None:
        self.reports.append(details)

    def record_artifact(self, name: str, path: str, **details: Any) -> None:
        self.artifacts[name] = {"path": path, **details}

    def record_delivery(
        self,
        status: str,
        *,
        recipients: list[str] | None = None,
        reason: str = "",
        error_type: str = "",
        error_message: str = "",
        phase: str = "",
        accepted_recipients: list[str] | None = None,
        rejected_recipients: list[str] | None = None,
    ) -> None:
        """Record a normalized delivery outcome independently from run status.

        Callers must pass redacted, address-only delivery metadata. This method
        normalizes supplied values but does not sanitize arbitrary exception or
        SMTP payload text. ``status`` is one of ``sent``,
        ``skipped: not_configured``, ``skipped: user_disabled``, or ``failed``.
        Recording a delivery outcome never adds a run event:
        ``run_status_from_events`` keeps describing report/run generation only.
        """
        self.delivery = {
            "status": str(status or "").strip(),
            "recipients": [str(recipient) for recipient in (recipients or [])],
            "reason": str(reason or ""),
            "error_type": str(error_type or ""),
            "error_message": str(error_message or ""),
            "phase": str(phase or ""),
            "accepted_recipients": [
                str(recipient) for recipient in (accepted_recipients or [])
            ],
            "rejected_recipients": [
                str(recipient) for recipient in (rejected_recipients or [])
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_started_at": self.run_started_at,
            "settings": self.settings,
            "top_funnel": self.top_funnel,
            "source_runs": self.source_runs,
            "article_budget": self.article_budget,
            "model_call_stats": self.model_call_stats,
            "activity_snapshots": self.activity_snapshots,
            "article_summary_count": self.article_summary_count,
            "report_count": len(self.reports),
            "report_generated": bool(self.reports),
            "reports": self.reports,
            "artifacts": self.artifacts,
            "delivery": self.delivery,
            "prompt_snapshots": self.prompt_snapshots,
            "events": self.events,
        }

    def write(self, output_dir: Path, timestamp: str) -> tuple[Path, Path, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"run_details_{timestamp}.json"
        markdown_path = output_dir / f"run_details_{timestamp}.md"
        summary_path = output_dir / f"run_summary_{timestamp}.md"
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        summary_path.write_text(self.to_summary_markdown(), encoding="utf-8")
        return json_path, markdown_path, summary_path

    def write_details_json(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f"{output_path.name}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")
        temp_path.replace(output_path)
        return output_path

    def write_run_review_markdown(
        self,
        output_path: Path,
        *,
        report_body: str = "",
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f"{output_path.name}.tmp")
        temp_path.write_text(
            self.to_run_review_markdown(report_body=report_body),
            encoding="utf-8",
        )
        temp_path.replace(output_path)
        return output_path

    def summary_stats(self) -> dict[str, Any]:
        source_status_counts: Counter[str] = Counter()
        rejection_counts: Counter[str] = Counter()
        feed_item_count = 0
        selected_item_count = 0
        fresh_article_count = 0
        problem_sources: list[str] = []
        slow_sources: list[str] = []
        timeout_sources: list[str] = []

        for source in self.source_runs:
            status = str(source.get("status") or "unknown")
            source_status_counts[status] += 1
            feed_item_count += _safe_int(source.get("feed_item_count"))
            selected_item_count += _safe_int(source.get("selected_item_count"))
            fresh_article_count += _safe_int(source.get("fresh_article_count"))
            rejection_counts.update(
                {
                    str(reason): _safe_int(count)
                    for reason, count in (source.get("rejected_counts") or {}).items()
                }
            )
            if status not in {"ok", "no_recent_items", "no_scraped_recent_items"}:
                problem_sources.append(f"{source.get('source') or 'Unknown source'} ({status})")
            elapsed_seconds = _safe_float(source.get("elapsed_seconds"))
            slow_threshold = _safe_float(self.settings.get("slow_source_warning_seconds"))
            if source.get("slow_source") or (
                elapsed_seconds
                and slow_threshold
                and elapsed_seconds >= slow_threshold
            ):
                slow_sources.append(
                    f"{source.get('source') or 'Unknown source'} ({_seconds_label(elapsed_seconds)})"
                )
            timeout_count = _safe_int(source.get("timeout_count"))
            if not timeout_count:
                timeout_count = sum(
                    _safe_int(count)
                    for status_label, count in (source.get("scrape_status_counts") or {}).items()
                    if "timeout" in str(status_label)
                )
            if timeout_count:
                timeout_sources.append(
                    f"{source.get('source') or 'Unknown source'} ({timeout_count} timeout(s))"
                )

        story_clustering = _last_event(self.events, "story_clustering")
        story_drafting = _last_event(self.events, "story_drafting")
        story_selection = _last_event(self.events, "global_story_selection")
        scale_screening = _last_event(self.events, "global_story_scale_screening")
        coverage_deficit = _last_event(self.events, "story_coverage_deficit")
        translation = _last_event(self.events, "translation")
        contradiction_analytics = story_drafting.get("contradiction_analytics") or {}

        model_calls: Counter[str] = Counter()
        for task_name, count in (self.model_call_stats.get("calls") or {}).items():
            model_calls[_model_call_bucket(str(task_name))] += _safe_int(count)
        token_usage = self.model_call_stats.get("token_usage") or {}
        model_token_totals = _model_token_totals(token_usage)

        reports_with_images = 0
        image_warnings = 0
        recipient_count = 0
        for report in self.reports:
            recipient_count += _safe_int(report.get("recipient_count"))
            image_art = report.get("image_art") or {}
            if image_art.get("final_image_path"):
                reports_with_images += 1
            if image_art.get("error") or image_art.get("art_prompt_error"):
                image_warnings += 1

        return {
            "duration": _duration_label(self.run_started_at, self.events),
            "source_status_counts": dict(source_status_counts),
            "problem_sources": problem_sources,
            "slow_sources": slow_sources,
            "timeout_sources": timeout_sources,
            "feed_item_count": feed_item_count,
            "selected_item_count": selected_item_count,
            "fresh_article_count": fresh_article_count,
            "rejection_counts": dict(rejection_counts),
            "story_count": _safe_int(story_clustering.get("story_count")),
            "story_included_count": _safe_int(story_clustering.get("included_count")),
            "story_dropped_count": _safe_int(story_clustering.get("dropped_count")),
            "story_draft_count": _safe_int(story_drafting.get("story_drafts_generated")),
            "story_scale_kept_count": _safe_int(scale_screening.get("kept_count")),
            "story_scale_dropped_count": _safe_int(scale_screening.get("dropped_count")),
            "selected_story_count": _safe_int(story_selection.get("selected_story_count")),
            "story_selection_candidate_count": _safe_int(story_selection.get("story_count")),
            "story_coverage_deficit": _safe_int(coverage_deficit.get("deficit")),
            "contradiction_stories_checked": _safe_int(
                contradiction_analytics.get("stories_checked")
            ),
            "raw_contradiction_count": _safe_int(
                contradiction_analytics.get("raw_contradiction_count")
            ),
            "validated_contradiction_count": _safe_int(
                contradiction_analytics.get("validated_contradiction_count")
            ),
            "render_eligible_contradiction_count": _safe_int(
                contradiction_analytics.get("render_eligible_contradiction_count")
            ),
            "raw_contradictions_rejected_by_citation_validation": _safe_int(
                contradiction_analytics.get(
                    "raw_contradictions_rejected_by_citation_validation"
                )
            ),
            "article_budget_candidate_count": _safe_int(self.article_budget.get("candidate_count")),
            "article_budget_included_count": _safe_int(self.article_budget.get("included_count")),
            "article_budget_dropped_count": _safe_int(self.article_budget.get("dropped_count")),
            "translated_count": _safe_int(translation.get("translated_count")),
            "translation_unchanged_count": _safe_int(translation.get("unchanged_count")),
            "translation_skipped_count": _safe_int(translation.get("skipped_unknown_language")),
            "article_summary_count": self.article_summary_count,
            "model_call_count": sum(_safe_int(value) for value in model_calls.values()),
            "model_calls": dict(model_calls),
            "model_token_usage": token_usage,
            "model_token_totals": model_token_totals,
            "model_retries": _safe_int(self.model_call_stats.get("retries")),
            "model_fallbacks": _safe_int(self.model_call_stats.get("fallbacks")),
            "report_count": len(self.reports),
            "recipient_count": recipient_count,
            "reports_with_images": reports_with_images,
            "image_warnings": image_warnings,
        }

    def to_summary_markdown(self) -> str:
        stats = self.summary_stats()
        source_status = stats["source_status_counts"]
        rejection_counts = stats["rejection_counts"]
        model_calls = stats["model_calls"]
        run_log_path = _run_log_path(self.settings)
        output_dir = self.settings.get("output_dir") or "N/A"

        lines = [
            "# Daily News Run Summary",
            "",
            f"- Started: {self.run_started_at}",
            f"- Duration: {stats['duration']}",
            f"- Preset: {self.settings.get('preset_id') or 'custom'}",
            f"- Source scope: {self.settings.get('source_scope') or 'unknown'}",
            f"- Translation enabled: {self.settings.get('translation_enabled')}",
            f"- Translation target: {self.settings.get('translation_target_language') or 'unknown'}",
            f"- Source languages: {self.settings.get('source_languages') or 'unknown'}",
            f"- Recipient scope: {self.settings.get('recipient_scope') or 'unknown'}",
            f"- URL reuse blocking: {self.settings.get('url_reuse_blocking_enabled')}",
            f"- Output folder: {output_dir}",
            f"- Run log: {run_log_path}",
            "",
            "## At a Glance",
            "",
            f"- Sources checked: {self.settings.get('source_count')}",
            f"- Feed items parsed: {stats['feed_item_count']}",
            f"- Candidate feed items selected: {stats['selected_item_count']}",
            f"- Fresh article targets after history/dedupe: {stats['fresh_article_count']}",
            f"- Article targets translated: {stats['translated_count']}",
            f"- Article targets unchanged after translation: {stats['translation_unchanged_count']}",
            f"- Translation candidates skipped (unknown language): {stats['translation_skipped_count']}",
            f"- Story groups retained: {stats['story_count']}",
            f"- Article targets retained after story grouping: {stats['story_included_count']}",
            f"- Article targets dropped before summary: {stats['story_dropped_count']}",
            f"- Story drafts generated: {stats['story_draft_count']}",
            "- Story scale screening eligible/ineligible: "
            f"{stats['story_scale_kept_count']}/{stats['story_scale_dropped_count']}",
            f"- Global stories selected: {stats['selected_story_count']} of {self.settings.get('max_stories')}",
            f"- Story coverage deficit: {stats['story_coverage_deficit']}",
            f"- Article summaries generated: {stats['article_summary_count']}",
            f"- Contradictions: {stats['validated_contradiction_count']} validated "
            f"from {stats['contradiction_stories_checked']} story draft(s) checked "
            f"({stats['raw_contradiction_count']} raw)",
            f"- Reports written: {stats['report_count']}",
        ]

        lines.extend(["", "## Source Health", ""])
        if source_status:
            lines.append(
                "- Status counts: "
                + ", ".join(f"{status}={count}" for status, count in sorted(source_status.items()))
            )
        else:
            lines.append("- Status counts: N/A")
        lines.append(
            "- Problem sources: " + (", ".join(stats["problem_sources"]) if stats["problem_sources"] else "none")
        )
        lines.append(
            "- Sources with scrape timeouts: "
            + (", ".join(stats["timeout_sources"]) if stats["timeout_sources"] else "none")
        )
        lines.append(
            "- Slow sources: " + (", ".join(stats["slow_sources"]) if stats["slow_sources"] else "none")
        )
        if rejection_counts:
            lines.append(
                "- Rejections: "
                + ", ".join(f"{reason}={count}" for reason, count in sorted(rejection_counts.items()))
            )
        else:
            lines.append("- Rejections: none recorded")

        lines.extend(
            [
                "",
                "## Story Coverage",
                "",
                f"- Global story candidates: {stats['story_selection_candidate_count']}",
                f"- Global stories selected: {stats['selected_story_count']}",
                f"- Target global stories: {self.settings.get('max_stories')}",
                f"- Coverage deficit: {stats['story_coverage_deficit']}",
            ]
        )

        lines.extend(["", "## Model Activity", ""])
        if model_calls:
            lines.append(
                "- Calls by task: "
                + ", ".join(f"{task}={count}" for task, count in sorted(model_calls.items()))
            )
        else:
            lines.append("- Calls by task: none recorded")
        lines.extend(
            [
                f"- Total model calls: {stats['model_call_count']}",
                f"- Estimated input tokens: {stats['model_token_totals']['estimated_input_tokens']}",
                f"- Estimated output tokens: {stats['model_token_totals']['estimated_output_tokens']}",
                f"- Max output tokens requested: {stats['model_token_totals']['max_output_tokens_requested']}",
                f"- Actual input tokens: {stats['model_token_totals']['actual_input_tokens'] or 'N/A'}",
                f"- Actual output tokens: {stats['model_token_totals']['actual_output_tokens'] or 'N/A'}",
                f"- Actual total tokens: {stats['model_token_totals']['actual_total_tokens'] or 'N/A'}",
                f"- Calls with provider usage: {stats['model_token_totals']['actual_usage_calls']}",
                f"- Retries: {stats['model_retries']}",
                f"- Fallbacks: {stats['model_fallbacks']}",
            ]
        )

        lines.extend(
            [
                "",
                "## Outputs",
                "",
                f"- Reports: {stats['report_count']}",
                f"- Recipients covered: {stats['recipient_count']}",
                f"- Reports with generated images: {stats['reports_with_images']}",
                f"- Image warnings: {stats['image_warnings']}",
                f"- Delivery: {_delivery_status_label(self.delivery)}",
                f"- Run URL log: {self.settings.get('run_used_urls_path')}",
            ]
        )
        if self.artifacts:
            lines.extend(["", "## Diagnostic Artifacts", ""])
            for name, details in sorted(self.artifacts.items()):
                path = details.get("path") if isinstance(details, dict) else details
                lines.append(f"- {name}: {path}")
        lines.append("")
        return "\n".join(lines)

    def to_run_review_markdown(self, *, report_body: str = "") -> str:
        stats = self.summary_stats()
        settings = self.settings or {}
        preset_id = settings.get("preset_id") or "custom"
        source_status = stats["source_status_counts"]
        rejection_counts = stats["rejection_counts"]
        model_calls = stats["model_calls"]
        token_totals = stats["model_token_totals"]
        aborted_event = _last_event(self.events, "aborted")
        failed_event = _last_event(self.events, "failed")
        status = run_status_from_events(self.events)
        history_path = settings.get("history_db_path") or "N/A"
        output_path = settings.get("latest_run_markdown_path") or "N/A"
        staging_dir = settings.get("run_staging_dir") or settings.get("output_dir") or "N/A"
        selected_story_label = f"{stats['selected_story_count']} of {settings.get('max_stories')}"

        delivery_status = _delivery_status_label(self.delivery)

        lines = [
            "# Latest News Run Review",
            "",
            "| Run | Value |",
            "| --- | --- |",
            f"| Status | {_table_value(status)} |",
            f"| Delivery | {_table_value(delivery_status)} |",
            f"| Started | {_table_value(self.run_started_at)} |",
            f"| Duration | {_table_value(stats['duration'])} |",
            f"| Preset | {_table_value(preset_id)} |",
            f"| Output review | {_table_value(output_path)} |",
            f"| History store | {_table_value(history_path)} |",
            f"| Staging directory | {_table_value(staging_dir)} |",
            "",
            "## Run Settings",
            "",
            "| Setting | Value |",
            "| --- | --- |",
            f"| Default model | {_table_value(str(settings.get('model')) + ' -> ' + str(settings.get('model_name')))} |",
            f"| Article Summarization model | {_table_value(_model_assignment_value(settings, 'article_summary'))} |",
            f"| Story Drafting model | {_table_value(_model_assignment_value(settings, 'story_drafting'))} |",
            f"| Story Scale Screening model | {_table_value(_model_assignment_value(settings, 'story_scale_screening'))} |",
            f"| Title Generation model | {_table_value(_model_assignment_value(settings, 'title_generation'))} |",
            f"| Image Art Direction model | {_table_value(_model_assignment_value(settings, 'image_art_direction'))} |",
            f"| Translation model | {_table_value(_model_assignment_value(settings, 'translation'))} |",
            f"| Model backend(s) | {_table_value(_model_backend_value(settings))} |",
            f"| Model input cap | {_table_value(settings.get('model_max_input_tokens'))} |",
            f"| Article summary cap | {_table_value(settings.get('article_summary_max_tokens'))} |",
            f"| Story drafting cap | {_table_value(settings.get('story_drafting_max_tokens'))} |",
            f"| Pipeline budget | {_table_value(_pipeline_budget_value(settings))} |",
            f"| Sources configured | {_table_value(settings.get('source_count'))} |",
            f"| Recent window | {_table_value(_hours_label(settings.get('recent_window_hours')))} |",
            f"| Max global stories | {_table_value(settings.get('max_stories'))} |",
            f"| Min articles per story | {_table_value(settings.get('min_articles_per_story'))} |",
            f"| Story similarity threshold | {_table_value(settings.get('story_cluster_similarity_threshold'))} |",
            f"| Story overlap threshold | {_table_value(settings.get('story_selection_overlap_threshold'))} |",
            f"| Source scope | {_table_value(settings.get('source_scope'))} |",
            f"| Translation enabled | {_table_value(settings.get('translation_enabled'))} |",
            f"| Translation target | {_table_value(settings.get('translation_target_language'))} |",
            f"| Source languages | {_table_value(settings.get('source_languages'))} |",
            f"| Recipient scope | {_table_value(settings.get('recipient_scope'))} |",
            f"| URL reuse blocking | {_table_value(settings.get('url_reuse_blocking_enabled'))} |",
            f"| Image generation | {_table_value(settings.get('image_generation_enabled'))} |",
            "",
            "## Top-Level KPIs",
            "",
            "| KPI | Value |",
            "| --- | --- |",
            f"| Sources checked | {_table_value(settings.get('source_count'))} |",
            f"| Feed items parsed | {_table_value(stats['feed_item_count'])} |",
            f"| Candidate feed items selected | {_table_value(stats['selected_item_count'])} |",
            f"| Fresh article targets | {_table_value(stats['fresh_article_count'])} |",
            f"| Story groups retained | {_table_value(stats['story_count'])} |",
            f"| Story drafts generated | {_table_value(stats['story_draft_count'])} |",
            f"| Global stories selected | {_table_value(selected_story_label)} |",
            f"| Story coverage deficit | {_table_value(stats['story_coverage_deficit'])} |",
            f"| Article summaries generated | {_table_value(stats['article_summary_count'])} |",
            f"| Reports prepared | {_table_value(stats['report_count'])} |",
            f"| Recipients covered | {_table_value(stats['recipient_count'])} |",
            "",
            "## Funnel Stats",
            "",
            "| Stage | Count |",
            "| --- | ---: |",
            f"| Feed items parsed | {_table_value(stats['feed_item_count'])} |",
            f"| Candidate items selected | {_table_value(stats['selected_item_count'])} |",
            f"| Fresh after history/dedupe | {_table_value(stats['fresh_article_count'])} |",
            f"| Article targets translated | {_table_value(stats['translated_count'])} |",
            f"| Article targets unchanged after translation | {_table_value(stats['translation_unchanged_count'])} |",
            f"| Translation candidates skipped (unknown language) | {_table_value(stats['translation_skipped_count'])} |",
            f"| Article targets retained after story grouping | {_table_value(stats['story_included_count'])} |",
            f"| Article targets dropped before summary | {_table_value(stats['story_dropped_count'])} |",
            f"| Story drafts generated | {_table_value(stats['story_draft_count'])} |",
            f"| Story scale screening eligible | {_table_value(stats['story_scale_kept_count'])} |",
            "| Story scale screening ineligible | "
            f"{_table_value(stats['story_scale_dropped_count'])} |",
            f"| Global story candidates | {_table_value(stats['story_selection_candidate_count'])} |",
            f"| Global stories selected | {_table_value(stats['selected_story_count'])} |",
            "",
            "## Source Health",
            "",
            "| Signal | Value |",
            "| --- | --- |",
            f"| Status counts | {_table_value(_format_count_map(source_status))} |",
            f"| Rejections | {_table_value(_format_count_map(rejection_counts) or 'none recorded')} |",
            f"| Problem sources | {_table_value(_join_or_none(stats['problem_sources']))} |",
            f"| Sources with scrape timeouts | {_table_value(_join_or_none(stats['timeout_sources']))} |",
            f"| Slow sources | {_table_value(_join_or_none(stats['slow_sources']))} |",
            "",
            "## Model Activity",
            "",
            "| Signal | Value |",
            "| --- | ---: |",
            f"| Calls by task | {_table_value(_format_count_map(model_calls) or 'none recorded')} |",
            f"| Total model calls | {_table_value(stats['model_call_count'])} |",
            f"| Estimated input tokens | {_table_value(token_totals['estimated_input_tokens'])} |",
            f"| Estimated output tokens | {_table_value(token_totals['estimated_output_tokens'])} |",
            f"| Max output tokens requested | {_table_value(token_totals['max_output_tokens_requested'])} |",
            f"| Actual input tokens | {_table_value(token_totals['actual_input_tokens'] or 'N/A')} |",
            f"| Actual output tokens | {_table_value(token_totals['actual_output_tokens'] or 'N/A')} |",
            f"| Actual total tokens | {_table_value(token_totals['actual_total_tokens'] or 'N/A')} |",
            f"| Calls with provider usage | {_table_value(token_totals['actual_usage_calls'])} |",
            f"| Retries | {_table_value(stats['model_retries'])} |",
            f"| Fallbacks | {_table_value(stats['model_fallbacks'])} |",
            "",
            "## Final Output Stats",
            "",
            "| Signal | Value |",
            "| --- | --- |",
            f"| Reports prepared | {_table_value(stats['report_count'])} |",
            f"| Recipients covered | {_table_value(stats['recipient_count'])} |",
            f"| Reports with generated images | {_table_value(stats['reports_with_images'])} |",
            f"| Image warnings | {_table_value(stats['image_warnings'])} |",
            f"| Run log imported into history | {_table_value(_run_log_path(settings))} |",
        ]

        warnings = _run_warning_lines(stats, aborted_event, failed_event)
        if warnings:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)

        lines.extend(["", "## Delivery", ""])
        delivery = self.delivery or {}
        lines.extend(
            [
                "| Field | Value |",
                "| --- | --- |",
                f"| Status | {_table_value(delivery_status)} |",
                "| Recipients | "
                f"{_table_value(', '.join(delivery.get('recipients') or []) or 'none')} |",
                f"| Reason | {_table_value(delivery.get('reason') or '')} |",
            ]
        )
        if delivery.get("phase"):
            lines.append(f"| Phase | {_table_value(delivery.get('phase'))} |")
        if delivery.get("accepted_recipients"):
            lines.append(
                "| Accepted | "
                f"{_table_value(', '.join(delivery.get('accepted_recipients') or []))} |"
            )
        if delivery.get("rejected_recipients"):
            lines.append(
                "| Rejected | "
                f"{_table_value(', '.join(delivery.get('rejected_recipients') or []))} |"
            )
        if delivery.get("error_type") or delivery.get("error_message"):
            lines.append(
                "| Error | "
                f"{_table_value(delivery.get('error_type') or 'error')}: "
                f"{_table_value(delivery.get('error_message') or '')} |"
            )

        clean_report_body = str(report_body or "").strip()
        lines.extend(["", "## Final Report Preview", ""])
        if clean_report_body:
            lines.append(clean_report_body)
        else:
            lines.append("_No final prose report was rendered for this run._")
        lines.append("")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            "# Daily News Run Details",
            "",
            f"- Started: {self.run_started_at}",
            f"- Preset: {self.settings.get('preset_id') or 'custom'}",
            f"- Source scope: {self.settings.get('source_scope')}",
            f"- Source languages: {self.settings.get('source_languages') or 'unknown'}",
            f"- Recipient scope: {self.settings.get('recipient_scope')}",
            f"- URL reuse blocking: {self.settings.get('url_reuse_blocking_enabled')}",
            f"- Source feeds: {self.settings.get('source_count')}",
            f"- Recent window hours: {self.settings.get('recent_window_hours')}",
            f"- Article download timeout: {self.settings.get('article_download_timeout_seconds')}s",
            f"- Article scrape deadline: {self.settings.get('article_scrape_total_timeout_seconds')}s",
            f"- Slow source warning threshold: {self.settings.get('slow_source_warning_seconds')}s",
            f"- Max global stories: {self.settings.get('max_stories')}",
            f"- Story scale screening enabled: {self.settings.get('story_scale_screening_enabled')}",
            f"- Default model: {self.settings.get('model')} -> {self.settings.get('model_name')}",
            f"- Article Summarization model: {_model_assignment_value(self.settings, 'article_summary')}",
            f"- Story Drafting model: {_model_assignment_value(self.settings, 'story_drafting')}",
            f"- Story Scale Screening model: {_model_assignment_value(self.settings, 'story_scale_screening')}",
            f"- Title Generation model: {_model_assignment_value(self.settings, 'title_generation')}",
            f"- Image Art Direction model: {_model_assignment_value(self.settings, 'image_art_direction')}",
            f"- Image Art Direction is an independent LLM call; Story Discovery has no LLM stage (inherits default).",
            f"- Model backends: {_model_backend_value(self.settings)}",
            f"- Model input cap: {self.settings.get('model_max_input_tokens')}",
            f"- Article summary cap: {self.settings.get('article_summary_max_tokens')}",
            f"- Story drafting cap: {self.settings.get('story_drafting_max_tokens')}",
            f"- Pipeline budget: {_pipeline_budget_value(self.settings)}",
            f"- Model default sampling: {self.settings.get('model_default_sampling')}",
            f"- Model reasoning sampling: {self.settings.get('model_reasoning_sampling')}",
            f"- Model task sampling: {self.settings.get('model_task_sampling')}",
            f"- Run log: {_run_log_path(self.settings)}",
            f"- Run URL log: {self.settings.get('run_used_urls_path')}",
            f"- Activity snapshots: {len(self.activity_snapshots)}",
        ]

        lines.extend(["", "## Source Funnel", ""])
        for source in self.source_runs:
            lines.extend(
                [
                    f"### {source.get('source')}",
                    "",
                    f"- Source index: {source.get('source_index') or 'N/A'}",
                    f"- Status: {source.get('status')}",
                    f"- Elapsed: {_seconds_label(source.get('elapsed_seconds'))}",
                    f"- Feed items parsed: {source.get('feed_item_count', 0)}",
                    f"- Candidate items selected: {source.get('selected_item_count', 0)}",
                    f"- Article targets after dedupe/history: {source.get('fresh_article_count', 0)}",
                ]
            )
            if source.get("timeout_count"):
                lines.append(f"- Scrape timeouts: {source.get('timeout_count')}")
            rejected = source.get("rejected_counts") or {}
            if rejected:
                lines.append(
                    "- Rejections: "
                    + ", ".join(f"{reason}={count}" for reason, count in rejected.items())
                )
            feed_rejections = source.get("feed_rejections") or []
            if feed_rejections:
                lines.append(f"- Source-match rejections: {len(feed_rejections)}")
                for rejection in feed_rejections[:10]:
                    labels = ", ".join(rejection.get("observed_source_labels") or []) or "N/A"
                    lines.append(
                        f"  - {rejection.get('title') or 'Untitled feed item'} "
                        f"(observed: {labels})"
                    )
            scrape_status_counts = source.get("scrape_status_counts") or {}
            if scrape_status_counts:
                lines.append(
                    "- Scrape statuses: "
                    + ", ".join(
                        f"{status}={count}"
                        for status, count in sorted(scrape_status_counts.items())
                    )
                )
            lines.append("")

        story_clustering = _last_event(self.events, "story_clustering")
        if story_clustering:
            lines.extend(["## Story Clustering", ""])
            lines.extend(
                [
                    f"- Method: {story_clustering.get('clustering_method') or 'N/A'}",
                    f"- Candidate articles: {story_clustering.get('candidate_count', 0)}",
                    f"- Retained articles: {story_clustering.get('included_count', 0)}",
                    f"- Dropped articles: {story_clustering.get('dropped_count', 0)}",
                    f"- Viable story groups: {story_clustering.get('viable_story_count', story_clustering.get('story_count', 0))}",
                    f"- Similarity threshold: {story_clustering.get('similarity_threshold')}",
                    f"- Component overlap suppression threshold: {story_clustering.get('component_overlap_suppress_threshold')}",
                    "",
                ]
            )
            for story in (story_clustering.get("stories") or [])[:40]:
                lines.extend(
                    [
                        f"### {story.get('story_title') or story.get('title') or story.get('story_key')}",
                        "",
                        f"- Story key: {story.get('story_key')}",
                        f"- Articles: {story.get('article_count')} ({story.get('source_count')} source(s))",
                        f"- Strength: {story.get('story_strength_score')} | connectedness: {story.get('connectedness_score')} | avg similarity: {story.get('average_similarity')}",
                        "- Member articles:",
                    ]
                )
                for article in story.get("articles") or []:
                    lines.append(
                        f"  - {article.get('source')}: {article.get('title')} [{article.get('article_id')}]"
                    )
                lines.append("")

        story_drafting = _last_event(self.events, "story_drafting")
        if story_drafting:
            contradiction_analytics = story_drafting.get("contradiction_analytics") or {}
            lines.extend(["## Story Drafting", ""])
            lines.extend(
                [
                    f"- Story blocks requested: {story_drafting.get('story_blocks_requested', 0)}",
                    f"- Story drafts generated: {story_drafting.get('story_drafts_generated', 0)}",
                    f"- Story drafts rejected: {story_drafting.get('story_drafts_rejected', 0)}",
                    f"- Contradiction checks: {contradiction_analytics.get('stories_checked', 0)} story draft(s)",
                    f"- Raw contradiction outputs: {contradiction_analytics.get('raw_contradiction_count', 0)}",
                    f"- Validated contradictions: {contradiction_analytics.get('validated_contradiction_count', 0)}",
                    f"- Render-eligible contradictions: {contradiction_analytics.get('render_eligible_contradiction_count', 0)}",
                    f"- Raw contradictions rejected by citation validation: {contradiction_analytics.get('raw_contradictions_rejected_by_citation_validation', 0)}",
                    "",
                ]
            )

        scale_screening = _last_event(self.events, "global_story_scale_screening")
        if scale_screening:
            lines.extend(["## Global Story Scale Screening", ""])
            lines.extend(
                [
                    f"- Enabled: {scale_screening.get('enabled')}",
                    "- Required final-output scale: "
                    f"{scale_screening.get('required_scale') or 'N/A'}",
                    f"- Story drafts evaluated: {scale_screening.get('candidate_count', 0)}",
                    f"- Screening judged: {scale_screening.get('judged_count', 0)}",
                    f"- Eligible for final output: {scale_screening.get('kept_count', 0)}",
                    "- Ineligible because not obviously large-scale: "
                    f"{scale_screening.get('dropped_count', 0)}",
                    f"- Scale counts: {scale_screening.get('scale_counts') or {}}",
                    "",
                ]
            )
            dropped = scale_screening.get("dropped") or []
            if dropped:
                lines.append("- Ineligible stories:")
                for story in dropped[:10]:
                    reason = story.get("scale_screening_reason") or "N/A"
                    lines.append(
                        f"  - {story.get('story_title')} "
                        f"({story.get('article_count')} article(s), "
                        f"{story.get('source_count')} source(s); {reason})"
                    )
                if len(dropped) > 10:
                    lines.append(
                        f"  - ... {len(dropped) - 10} more in raw diagnostics"
                    )
                lines.append("")

        story_selection = _last_event(self.events, "global_story_selection")
        if story_selection:
            lines.extend(["## Global Story Selection", ""])
            lines.extend(
                [
                    f"- Drafted story candidates: {story_selection.get('story_count', 0)}",
                    f"- Selected global stories: {story_selection.get('selected_story_count', 0)}",
                    f"- Max global stories: {story_selection.get('max_stories')}",
                    f"- Overlap threshold: {story_selection.get('overlap_threshold')}",
                    "",
                ]
            )
            selected = story_selection.get("selected") or []
            if selected:
                lines.append("- Selected stories:")
                for story in selected:
                    lines.append(
                        f"  - {story.get('global_selection_rank')}. {story.get('story_title')} "
                        f"({story.get('article_count')} article(s), {story.get('source_count')} source(s))"
                    )
                lines.append("")

        coverage_deficit = _last_event(self.events, "story_coverage_deficit")
        if coverage_deficit:
            lines.extend(["## Story Coverage Deficit", ""])
            lines.extend(
                [
                    f"- Selected global stories: {coverage_deficit.get('selected_story_count')}",
                    f"- Target global stories: {coverage_deficit.get('target_story_count')}",
                    f"- Deficit: {coverage_deficit.get('deficit')}",
                    "",
                ]
            )

        story_backfill = _last_event(self.events, "story_backfill")
        if story_backfill:
            lines.extend(["## Story Backfill", ""])
            lines.extend(
                [
                    f"- Enabled: {story_backfill.get('enabled')}",
                    f"- Reason: {story_backfill.get('reason') or 'N/A'}",
                    f"- Iterations: {story_backfill.get('iterations', 0)}",
                    f"- Attempted reserve articles: {story_backfill.get('attempted_article_count', 0)}",
                    f"- New article summaries: {story_backfill.get('new_article_summary_count', 0)}",
                    f"- New story drafts: {story_backfill.get('new_story_draft_count', 0)}",
                    "",
                ]
            )

        lines.extend(
            [
                "## Outputs",
                "",
                f"- Delivery: {_delivery_status_label(self.delivery)}",
                f"- Article budget included: {self.article_budget.get('included_count', 0)}",
                f"- Article budget dropped: {self.article_budget.get('dropped_count', 0)}",
                f"- Article summaries generated: {self.article_summary_count}",
                f"- Model fallbacks: {self.model_call_stats.get('fallbacks', 0)}",
                f"- Model retries: {self.model_call_stats.get('retries', 0)}",
            ]
        )
        if self.activity_snapshots:
            latest_activity = self.activity_snapshots[-1]
            activity_parts = [f"latest={latest_activity.get('label')}"]
            if latest_activity.get("memory_free_pct") is not None:
                activity_parts.append(f"memory_free={latest_activity.get('memory_free_pct')}%")
            if latest_activity.get("swapouts") is not None:
                activity_parts.append(f"swapouts={latest_activity.get('swapouts')}")
            lines.append("- Activity: " + ", ".join(activity_parts))
        for report in self.reports:
            lines.append(f"- Report: {report.get('path')} ({report.get('recipient_count')} recipient(s))")
            image_art = report.get("image_art") or {}
            if image_art.get("final_image_path"):
                lines.append(f"  - Image: {image_art.get('final_image_path')}")
            elif image_art.get("error"):
                lines.append(f"  - Image warning: {image_art.get('error')}")
        if self.artifacts:
            lines.append("- Diagnostic artifacts:")
            for name, details in sorted(self.artifacts.items()):
                path = details.get("path") if isinstance(details, dict) else details
                lines.append(f"  - {name}: {path}")
        lines.append("")
        return "\n".join(lines)


def _story_digest(story: dict[str, Any], *, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "title": story.get("title"),
        "url": story.get("url"),
        "provider": story.get("provider"),
        "providers": story.get("providers", []),
        "frames": story.get("frames", []),
        "domain": story.get("domain"),
        "score": story.get("score", 0),
        "num_comments": story.get("num_comments", 0),
        "match_score": story.get("match_score"),
    }


def _model_assignment_value(settings: dict[str, Any], task: str) -> str:
    assignments = settings.get("model_assignments") or {}
    if not isinstance(assignments, dict):
        return "N/A"
    record = assignments.get(task) or {}
    if not isinstance(record, dict):
        return "N/A"
    reference = str(record.get("reference") or record.get("name") or "N/A")
    name = str(record.get("name") or "")
    backend = str(record.get("backend") or "")
    base_url = str(record.get("base_url") or "")
    parts = [reference]
    if name and name != reference:
        parts.append(f"({name})")
    if backend:
        parts.append(f"[{backend}]")
    if base_url:
        parts.append(f"@ {base_url}")
    return " ".join(parts)


def _model_backend_value(settings: dict[str, Any]) -> str:
    default_backend = str(settings.get("model_backend") or "mlx-lm")
    assignments = settings.get("model_assignments") or {}
    if not isinstance(assignments, dict):
        return default_backend
    parts = [f"default={default_backend}"]
    for task in (
        "article_summary",
        "story_drafting",
        "story_scale_screening",
        "title_generation",
        "image_art_direction",
        "translation",
    ):
        record = assignments.get(task)
        if not isinstance(record, dict):
            continue
        backend = str(record.get("backend") or default_backend)
        if backend != default_backend:
            parts.append(f"{task}={backend}")
    return ", ".join(parts)


def _pipeline_budget_value(settings: dict[str, Any]) -> str:
    budget = settings.get("pipeline_budget") or {}
    if not isinstance(budget, dict):
        return "N/A"
    parts = []
    for label, key in (
        ("article_text", "article_text_token_limit"),
        ("article_summary_cap", "total_article_summary_cap"),
        ("recent_window", "recent_window_hours"),
        ("max_articles_per_source", "max_articles_per_source"),
        ("min_articles_per_story", "min_articles_per_story"),
        ("max_stories", "max_stories"),
    ):
        value = budget.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    return ", ".join(parts) if parts else "N/A"


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _table_value(value: Any) -> str:
    text = str(value if value is not None else "N/A")
    text = text.replace("\n", "<br>")
    text = text.replace("|", "\\|")
    return text or "N/A"


def _format_count_map(values: dict[str, Any]) -> str:
    if not values:
        return ""
    return ", ".join(
        f"{key}={_safe_int(count)}"
        for key, count in sorted(values.items())
    )


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _hours_label(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{value}h"


def _run_warning_lines(
    stats: dict[str, Any],
    aborted_event: dict[str, Any],
    failed_event: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if failed_event:
        error_type = failed_event.get("error_type") or "unknown error"
        error_message = failed_event.get("error_message") or failed_event.get("reason") or ""
        warnings.append(f"Run failed: {error_type}: {error_message}".rstrip(": "))
    if aborted_event:
        warnings.append(f"Run aborted: {aborted_event.get('reason') or 'unknown reason'}")
    if stats.get("problem_sources"):
        warnings.append("Problem sources: " + _join_or_none(stats["problem_sources"]))
    if stats.get("timeout_sources"):
        warnings.append("Scrape timeouts: " + _join_or_none(stats["timeout_sources"]))
    if stats.get("slow_sources"):
        warnings.append("Slow sources: " + _join_or_none(stats["slow_sources"]))
    if _safe_int(stats.get("image_warnings")):
        warnings.append(f"Image warnings: {stats.get('image_warnings')}")
    if _safe_int(stats.get("model_retries")):
        warnings.append(f"Model retries: {stats.get('model_retries')}")
    if _safe_int(stats.get("model_fallbacks")):
        warnings.append(f"Model fallbacks: {stats.get('model_fallbacks')}")
    return warnings


def _model_token_totals(token_usage: dict[str, Any]) -> dict[str, int]:
    totals = {
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
    }
    for details in token_usage.values():
        if not isinstance(details, dict):
            continue
        for key in (
            "calls",
            "estimated_input_tokens",
            "estimated_output_tokens",
            "max_output_tokens_requested",
            "actual_input_tokens",
            "actual_output_tokens",
            "actual_total_tokens",
            "actual_usage_calls",
            "fallback_calls",
        ):
            totals[key] += _safe_int(details.get(key))
        totals["max_estimated_input_tokens"] = max(
            totals["max_estimated_input_tokens"],
            _safe_int(details.get("max_estimated_input_tokens")),
        )
        totals["max_estimated_output_tokens"] = max(
            totals["max_estimated_output_tokens"],
            _safe_int(details.get("max_estimated_output_tokens")),
        )
        totals["max_actual_input_tokens"] = max(
            totals["max_actual_input_tokens"],
            _safe_int(details.get("max_actual_input_tokens")),
        )
        totals["max_actual_output_tokens"] = max(
            totals["max_actual_output_tokens"],
            _safe_int(details.get("max_actual_output_tokens")),
        )
    return totals


def _seconds_label(value: Any) -> str:
    seconds = _safe_float(value)
    if seconds >= 60:
        minutes, remainder = divmod(seconds, 60)
        return f"{int(minutes)}m {remainder:.1f}s"
    return f"{seconds:.1f}s"


def _last_event(events: list[dict[str, Any]], label: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("label") == label:
            return event
    return {}


def run_status_from_events(events: list[dict[str, Any]]) -> str:
    if _last_event(events, "failed"):
        return "failed"
    if _last_event(events, "aborted"):
        return "aborted"
    if _last_event(events, "completed"):
        return "completed"
    return "unknown"


def _delivery_status_label(delivery: dict[str, Any]) -> str:
    """Return the normalized delivery status or ``not recorded`` when empty."""
    status = str((delivery or {}).get("status") or "").strip()
    return status or "not recorded"


def _model_call_bucket(task_name: str) -> str:
    normalized = task_name.strip().lower()
    if normalized.startswith("analysis for final synthesis"):
        return "story_drafting"
    if normalized.startswith("analysis for "):
        return "article_summary"
    if normalized.startswith("story synthesis for "):
        return "story_synthesis"
    if normalized.startswith("translation for "):
        return "translation"
    clean = normalized.replace(" ", "_")
    return clean or "unknown"


def _duration_label(run_started_at: str, events: list[dict[str, Any]]) -> str:
    try:
        started_at = datetime.fromisoformat(run_started_at)
    except ValueError:
        return "N/A"

    ended_at: datetime | None = None
    for event in reversed(events):
        if event.get("at"):
            try:
                ended_at = datetime.fromisoformat(str(event["at"]))
                break
            except ValueError:
                continue
    if ended_at is None:
        return "N/A"

    elapsed_seconds = max(0, int(round((ended_at - started_at).total_seconds())))
    minutes, seconds = divmod(elapsed_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _run_log_path(settings: dict[str, Any]) -> Any:
    return settings.get("run_log_path") or settings.get("terminal_output_log") or "N/A"
