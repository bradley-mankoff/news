"""Run diagnostics for the daily news funnel.

This module records the run in two formats:

- JSON for inspection or later automation.
- Markdown for a quick human-readable backend audit trail.
"""

from __future__ import annotations

import json
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
    article_summary_count: int = 0
    reports: list[dict[str, Any]] = field(default_factory=list)

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
                "keywords": topic.get("keywords", []),
                "boost_phrases": topic.get("boost_phrases", []),
                "min_score": topic.get("min_score"),
                "max_articles_per_source": topic.get("max_articles_per_source"),
                "candidate_rank": topic.get("candidate_rank"),
                "selection_rank": topic.get("selection_rank"),
                "selection_reason": topic.get("selection_reason"),
                "selection_base_score": topic.get("selection_base_score"),
                "selection_validation_score": topic.get("selection_validation_score"),
                "selection_weight": topic.get("selection_weight"),
                "selection_frame_nudge": topic.get("selection_frame_nudge"),
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

    def record_report(self, **details: Any) -> None:
        self.reports.append(details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_started_at": self.run_started_at,
            "settings": self.settings,
            "top_funnel": self.top_funnel,
            "topics": self.topics,
            "source_runs": self.source_runs,
            "article_budget": self.article_budget,
            "model_call_stats": self.model_call_stats,
            "article_summary_count": self.article_summary_count,
            "reports": self.reports,
            "events": self.events,
        }

    def write(self, output_dir: Path, timestamp: str) -> tuple[Path, Path]:
        json_path = output_dir / f"run_details_{timestamp}.json"
        markdown_path = output_dir / f"run_details_{timestamp}.md"
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, markdown_path

    def to_markdown(self) -> str:
        lines = [
            "# Daily News Run Details",
            "",
            f"- Started: {self.run_started_at}",
            f"- Run mode: {self.settings.get('run_mode') or ('dev' if self.settings.get('dev') else 'prod')}",
            f"- DEV mode: {self.settings.get('dev')}",
            f"- Bradley-only delivery: {self.settings.get('bradley_only_delivery', self.settings.get('dev'))}",
            f"- Source feeds: {self.settings.get('source_count')}",
            f"- Top topics requested: {self.settings.get('num_top_topics')}",
            f"- Recent window hours: {self.settings.get('recent_window_hours')}",
            f"- Model: {self.settings.get('model')} ({self.settings.get('model_profile')})",
            f"- Model input cap: {self.settings.get('model_max_input_tokens')}",
            f"- Model default sampling: {self.settings.get('model_default_sampling')}",
            f"- Model reasoning sampling: {self.settings.get('model_reasoning_sampling')}",
            f"- Model task sampling: {self.settings.get('model_task_sampling')}",
            "",
            "## Top-of-Funnel Discovery",
            "",
        ]
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
                    f"- Status: {source.get('status')}",
                    f"- Feed items parsed: {source.get('feed_item_count', 0)}",
                    f"- Candidate items selected: {source.get('selected_item_count', 0)}",
                    f"- Article targets after dedupe/history: {source.get('fresh_article_count', 0)}",
                ]
            )
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
        for report in self.reports:
            lines.append(f"- Report: {report.get('path')} ({report.get('recipient_count')} recipient(s))")
            image_art = report.get("image_art") or {}
            if image_art.get("final_image_path"):
                lines.append(f"  - Image: {image_art.get('final_image_path')}")
            elif image_art.get("error"):
                lines.append(f"  - Image warning: {image_art.get('error')}")
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
