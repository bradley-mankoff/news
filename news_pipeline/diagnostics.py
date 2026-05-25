"""Run diagnostics for the daily news funnel.

This module records the run in two formats:

- JSON for inspection or later automation.
- Markdown for a quick human-readable backend audit trail.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class RunDiagnostics:
    run_started_at: str
    settings: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    top_funnel: dict[str, Any] = field(default_factory=dict)
    topics: list[dict[str, Any]] = field(default_factory=list)
    source_runs: list[dict[str, Any]] = field(default_factory=list)
    article_budget: dict[str, Any] = field(default_factory=dict)
    model_call_stats: dict[str, Any] = field(default_factory=dict)
    activity_snapshots: list[dict[str, Any]] = field(default_factory=list)
    article_summary_count: int = 0
    reports: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

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

    def record_topics(self, topics: list[dict[str, Any]]) -> None:
        self.topics = [
            {
                "key": topic.get("key"),
                "title": topic.get("title"),
                "rationale": topic.get("rationale"),
                "topic_source": topic.get("topic_source"),
                "fallback_provider_support": topic.get("fallback_provider_support", []),
                "required_context_terms": topic.get("required_context_terms", []),
                "keywords": topic.get("keywords", []),
                "boost_phrases": topic.get("boost_phrases", []),
                "max_articles_per_source": topic.get("max_articles_per_source"),
                "configured_rank": topic.get("configured_rank"),
                "candidate_rank": topic.get("candidate_rank"),
                "selection_rank": topic.get("selection_rank"),
                "selection_reason": topic.get("selection_reason"),
                "seed_providers": topic.get("seed_providers", []),
                "validation_providers": topic.get("validation_providers", []),
                "frame_counts": topic.get("frame_counts", {}),
                "frame_tags": topic.get("frame_tags", []),
                "seed_matches": [
                    _story_digest(match, rank=index)
                    for index, match in enumerate(topic.get("seed_matches", []), start=1)
                ],
                "validation_matches": [
                    _story_digest(match, rank=index)
                    for index, match in enumerate(topic.get("validation_matches", []), start=1)
                ],
            }
            for topic in topics
        ]

    def record_source_run(self, source_run: dict[str, Any]) -> None:
        self.source_runs.append(source_run)

    def record_article_budget(self, details: dict[str, Any]) -> None:
        self.article_budget = details

    def record_model_call_stats(self, details: dict[str, Any]) -> None:
        self.model_call_stats = details

    def record_activity_snapshot(self, details: dict[str, Any]) -> None:
        self.activity_snapshots.append(details)

    def record_report(self, **details: Any) -> None:
        self.reports.append(details)

    def record_artifact(self, name: str, path: str, **details: Any) -> None:
        self.artifacts[name] = {"path": path, **details}

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_started_at": self.run_started_at,
            "settings": self.settings,
            "top_funnel": self.top_funnel,
            "topics": self.topics,
            "source_runs": self.source_runs,
            "article_budget": self.article_budget,
            "model_call_stats": self.model_call_stats,
            "activity_snapshots": self.activity_snapshots,
            "article_summary_count": self.article_summary_count,
            "reports": self.reports,
            "artifacts": self.artifacts,
            "events": self.events,
        }

    def write(self, output_dir: Path, timestamp: str) -> tuple[Path, Path, Path]:
        json_path = output_dir / f"run_details_{timestamp}.json"
        markdown_path = output_dir / f"run_details_{timestamp}.md"
        summary_path = output_dir / f"run_summary_{timestamp}.md"
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        summary_path.write_text(self.to_summary_markdown(), encoding="utf-8")
        return json_path, markdown_path, summary_path

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
            if status not in {"ok", "no_relevant_items", "no_recent_items"}:
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
        story_topic_classification = _last_event(self.events, "story_topic_classification")
        coverage_deficit = _last_event(self.events, "story_coverage_deficit")
        coverage_deficits = coverage_deficit.get("deficits") or {}

        model_calls: Counter[str] = Counter()
        for task_name, count in (self.model_call_stats.get("calls") or {}).items():
            model_calls[_model_call_bucket(str(task_name))] += _safe_int(count)
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
            "topic_count": len(self.topics),
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
            "story_topic_selected_count": _safe_int(
                story_topic_classification.get("selected_story_topic_count")
            ),
            "story_topic_selected_by_topic": story_topic_classification.get("selected_by_topic") or {},
            "coverage_deficit_topic_count": len(coverage_deficits),
            "coverage_deficit_total": sum(_safe_int(value) for value in coverage_deficits.values()),
            "article_budget_candidate_count": _safe_int(self.article_budget.get("candidate_count")),
            "article_budget_included_count": _safe_int(self.article_budget.get("included_count")),
            "article_budget_dropped_count": _safe_int(self.article_budget.get("dropped_count")),
            "article_summary_count": self.article_summary_count,
            "model_call_count": sum(_safe_int(value) for value in model_calls.values()),
            "model_calls": dict(model_calls),
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
            f"- Run mode: {self.settings.get('run_mode') or ('dev' if self.settings.get('dev') else 'prod')}",
            f"- Output folder: {output_dir}",
            f"- Run log: {run_log_path}",
            "",
            "## At a Glance",
            "",
            f"- Topics selected: {stats['topic_count']}",
            f"- Sources checked: {self.settings.get('source_count')}",
            f"- Feed items parsed: {stats['feed_item_count']}",
            f"- Candidate feed items selected: {stats['selected_item_count']}",
            f"- Fresh article targets after history/dedupe: {stats['fresh_article_count']}",
            f"- Story groups retained: {stats['story_count']}",
            f"- Article targets retained after story grouping: {stats['story_included_count']}",
            f"- Article targets dropped before budget: {stats['story_dropped_count']}",
            f"- Story-topic matches selected: {stats['story_topic_selected_count']}",
            f"- Article summaries generated: {stats['article_summary_count']}",
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
        if stats["problem_sources"]:
            lines.append("- Problem sources: " + ", ".join(stats["problem_sources"]))
        else:
            lines.append("- Problem sources: none")
        if stats["timeout_sources"]:
            lines.append("- Sources with scrape timeouts: " + ", ".join(stats["timeout_sources"]))
        else:
            lines.append("- Sources with scrape timeouts: none")
        if stats["slow_sources"]:
            lines.append("- Slow sources: " + ", ".join(stats["slow_sources"]))
        else:
            lines.append("- Slow sources: none")
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
                "## Budget And Coverage",
                "",
                f"- Budget candidates: {stats['article_budget_candidate_count']}",
                f"- Budget included: {stats['article_budget_included_count']}",
                f"- Budget dropped: {stats['article_budget_dropped_count']}",
                f"- Coverage deficits: {stats['coverage_deficit_topic_count']} topic(s), total shortfall {stats['coverage_deficit_total']}",
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

    def to_markdown(self) -> str:
        lines = [
            "# Daily News Run Details",
            "",
            f"- Started: {self.run_started_at}",
            f"- Run mode: {self.settings.get('run_mode') or ('dev' if self.settings.get('dev') else 'prod')}",
            f"- DEV mode: {self.settings.get('dev')}",
            f"- Bradley-only delivery: {self.settings.get('bradley_only_delivery', self.settings.get('dev'))}",
            f"- Shared URL history: {self.settings.get('shared_url_history_enabled')}",
            f"- Source feeds: {self.settings.get('source_count')}",
            f"- Topic mode: {self.settings.get('topic_mode') or 'predefined'}",
            f"- Top topics requested: {self.settings.get('num_top_topics')}",
            f"- Recent window hours: {self.settings.get('recent_window_hours')}",
            f"- Article download timeout: {self.settings.get('article_download_timeout_seconds')}s",
            f"- Article scrape deadline: {self.settings.get('article_scrape_total_timeout_seconds')}s",
            f"- Slow source warning threshold: {self.settings.get('slow_source_warning_seconds')}s",
            f"- Topic relevance min score: {self.settings.get('topic_relevance_min_score')}",
            f"- Model: {self.settings.get('model')} ({self.settings.get('model_profile')})",
            f"- Model backend: {self.settings.get('model_backend') or 'mlx-lm'}",
            f"- Model input cap: {self.settings.get('model_max_input_tokens')}",
            f"- Model default sampling: {self.settings.get('model_default_sampling')}",
            f"- Model reasoning sampling: {self.settings.get('model_reasoning_sampling')}",
            f"- Model task sampling: {self.settings.get('model_task_sampling')}",
            f"- Run log: {_run_log_path(self.settings)}",
            f"- Run URL log: {self.settings.get('run_used_urls_path')}",
            f"- Activity snapshots: {len(self.activity_snapshots)}",
        ]
        if self.settings.get("topic_mode") == "predefined":
            active_topic_ids = ", ".join(self.settings.get("active_topic_ids") or []) or "N/A"
            lines.extend(
                [
                    "",
                    "## Predefined Topics",
                    "",
                    f"- Client config: {self.settings.get('client_path')}",
                    f"- Topic definitions: {self.settings.get('topics_path')}",
                    f"- Active topic IDs: {active_topic_ids}",
                    "",
                    "## Topics and Search Vocabulary",
                    "",
                ]
            )
        else:
            lines.extend(["", "## Top-of-Funnel Discovery", ""])
            provider_counts = self.top_funnel.get("provider_counts", {})
            if provider_counts:
                for provider, count in provider_counts.items():
                    lines.append(f"- {provider}: {count} headline(s)")
            lines.extend(
                [
                    f"- Unique merged headlines: {self.top_funnel.get('merged_count', 0)}",
                    f"- Seed-capable merged headlines: {self.top_funnel.get('seed_merged_count', 0)}",
                    f"- Validation-capable merged headlines: {self.top_funnel.get('validation_merged_count', 0)}",
                    f"- Exact URL/title duplicates across providers: {self.top_funnel.get('multi_provider_count', 0)}",
                    "",
                    "## Topics and Search Vocabulary",
                    "",
                ]
            )
        for topic in self.topics:
            lines.extend(
                [
                    f"### {topic.get('title')}",
                    "",
                    f"- Rationale: {topic.get('rationale') or 'N/A'}",
                    f"- Topic source: {topic.get('topic_source') or 'N/A'}",
                    f"- Selection: {topic.get('selection_reason') or 'N/A'}",
                    f"- Seeded by: {', '.join(topic.get('seed_providers') or []) or 'N/A'}",
                    f"- Validated by: {', '.join(topic.get('validation_providers') or []) or 'N/A'}",
                    f"- Frame tags: {', '.join(topic.get('frame_tags') or []) or 'N/A'}",
                    f"- Required context terms: {', '.join(topic.get('required_context_terms') or [])}",
                    f"- Keywords: {', '.join(topic.get('keywords') or [])}",
                    f"- Boost phrases: {', '.join(topic.get('boost_phrases') or [])}",
                    "",
                ]
            )
        lines.extend(["## Source Funnel", ""])
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
            by_topic = source.get("selected_by_topic") or {}
            if by_topic:
                lines.append("- Selected by topic:")
                for topic_title, count in by_topic.items():
                    lines.append(f"  - {topic_title}: {count}")
            rejected = source.get("rejected_counts") or {}
            if rejected:
                lines.append(
                    "- Rejections: "
                    + ", ".join(f"{reason}={count}" for reason, count in rejected.items())
                )
            post_scrape_rejections = source.get("post_scrape_rejections") or []
            if post_scrape_rejections:
                lines.append(
                    f"- Post-scrape relevance rejections: {len(post_scrape_rejections)}"
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
                    f"- Story groups: {story_clustering.get('story_count', 0)}",
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
            pair_debug = story_clustering.get("pair_debug") or []
            if pair_debug:
                lines.extend(["### Strongest Article Links", ""])
                for pair in pair_debug[:25]:
                    lines.append(
                        f"- {pair.get('similarity')}: "
                        f"{pair.get('left_source')} / {pair.get('left_title')} <-> "
                        f"{pair.get('right_source')} / {pair.get('right_title')}"
                    )
                lines.append("")
            dropped_articles = story_clustering.get("dropped_articles") or []
            if dropped_articles:
                lines.extend(["### Dropped Article Candidates", ""])
                for article in dropped_articles[:40]:
                    lines.append(
                        f"- {article.get('source')}: {article.get('title')} "
                        f"[{article.get('article_id')}] ({article.get('reason')})"
                    )
                lines.append("")

        story_topic_classification = _last_event(self.events, "story_topic_classification")
        if story_topic_classification:
            lines.extend(["## Story Topic Fit", ""])
            lines.extend(
                [
                    f"- Story drafts scored: {story_topic_classification.get('story_count', 0)}",
                    f"- Selected story-topic matches: {story_topic_classification.get('selected_story_topic_count', 0)}",
                    f"- Max stories per topic: {story_topic_classification.get('max_stories_per_topic')}",
                    f"- Minimum fit score: {story_topic_classification.get('min_score')}",
                    "",
                ]
            )
            for topic_title, details in (story_topic_classification.get("topics") or {}).items():
                lines.extend(
                    [
                        f"### {topic_title}",
                        "",
                        f"- Candidates above threshold: {details.get('candidate_count', 0)}",
                        f"- Owned candidates: {details.get('owned_candidate_count', 0)}",
                        f"- Selected: {details.get('selected_count', 0)}",
                        f"- Diversity min distance: {details.get('diversity_min_distance')}",
                    ]
                )
                selected = details.get("selected") or []
                if selected:
                    lines.append("- Selected stories:")
                    for story in selected:
                        distance_text = ""
                        if story.get("min_distance_to_selected") is not None:
                            distance_text = f", min distance {story.get('min_distance_to_selected')}"
                        lines.append(
                            f"  - score {story.get('topic_fit_score')}: "
                            f"{story.get('story_title')} "
                            f"({story.get('article_count')} article(s), {story.get('source_count')} source(s)"
                            f"{distance_text})"
                        )
                rejected = details.get("rejected") or []
                if rejected:
                    lines.append("- Best rejected stories:")
                    for story in rejected[:10]:
                        owner_text = ""
                        if story.get("owned_topic_title"):
                            owner_text = f"; owner={story.get('owned_topic_title')}"
                        lines.append(
                            f"  - score {story.get('topic_fit_score')}: "
                            f"{story.get('story_title')} ({story.get('reason')}{owner_text})"
                        )
                lines.append("")
        lines.extend(
            [
                "## Outputs",
                "",
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


def _model_call_bucket(task_name: str) -> str:
    normalized = task_name.strip().lower()
    if normalized.startswith("analysis for final synthesis"):
        return "final_synthesis"
    if normalized.startswith("analysis for "):
        return "article_summary"
    if normalized.startswith("story synthesis for "):
        return "story_synthesis"
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
