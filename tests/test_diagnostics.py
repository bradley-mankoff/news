from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from news_pipeline.diagnostics import (
    RunDiagnostics,
    _duration_label,
    _model_assignment_value,
    _model_backend_value,
    _pipeline_budget_value,
    run_status_from_events,
)


class DiagnosticsTests(unittest.TestCase):
    def test_populated_diagnostics_render_and_write_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            diagnostics = self._populated_diagnostics(root)

            stats = diagnostics.summary_stats()
            self.assertEqual(stats["duration"], "15m 30s")
            self.assertEqual(
                stats["source_status_counts"],
                {
                    "ok": 1,
                    "no_recent_items": 1,
                    "no_scraped_recent_items": 1,
                    "source_error": 1,
                },
            )
            self.assertEqual(stats["problem_sources"], ["Delta source (source_error)"])
            self.assertEqual(
                stats["slow_sources"],
                ["Alpha source (7.2s)", "Delta source (2m 5.0s)"],
            )
            self.assertEqual(
                stats["timeout_sources"],
                [
                    "Alpha source (1 timeout(s))",
                    "Beta source (1 timeout(s))",
                    "Delta source (2 timeout(s))",
                ],
            )
            self.assertEqual(stats["feed_item_count"], 11)
            self.assertEqual(stats["selected_item_count"], 6)
            self.assertEqual(stats["fresh_article_count"], 5)
            self.assertEqual(
                stats["rejection_counts"],
                {
                    "duplicate": 2,
                    "paywalled": 1,
                    "too_old": 2,
                    "skipped": 0,
                    "timeout": 3,
                },
            )
            self.assertEqual(stats["story_count"], 14)
            self.assertEqual(stats["story_included_count"], 12)
            self.assertEqual(stats["story_dropped_count"], 2)
            self.assertEqual(stats["story_draft_count"], 14)
            self.assertEqual(stats["story_scale_kept_count"], 3)
            self.assertEqual(stats["story_scale_dropped_count"], 11)
            self.assertEqual(stats["selected_story_count"], 3)
            self.assertEqual(stats["story_selection_candidate_count"], 3)
            self.assertEqual(stats["story_coverage_deficit"], 1)
            self.assertEqual(stats["article_budget_candidate_count"], 11)
            self.assertEqual(stats["article_budget_included_count"], 8)
            self.assertEqual(stats["article_budget_dropped_count"], 3)
            self.assertEqual(stats["article_summary_count"], 7)
            self.assertEqual(stats["model_call_count"], 14)
            self.assertEqual(
                stats["model_calls"],
                {
                    "article_summary": 3,
                    "custom_task": 5,
                    "story_drafting": 2,
                    "story_synthesis": 4,
                },
            )
            self.assertEqual(stats["model_token_totals"]["estimated_input_tokens"], 220)
            self.assertEqual(stats["model_token_totals"]["estimated_output_tokens"], 65)
            self.assertEqual(stats["model_token_totals"]["max_output_tokens_requested"], 275)
            self.assertEqual(stats["model_token_totals"]["actual_input_tokens"], 190)
            self.assertEqual(stats["model_token_totals"]["actual_output_tokens"], 59)
            self.assertEqual(stats["model_token_totals"]["actual_total_tokens"], 249)
            self.assertEqual(stats["model_token_totals"]["actual_usage_calls"], 5)
            self.assertEqual(stats["model_retries"], 2)
            self.assertEqual(stats["model_fallbacks"], 1)
            self.assertEqual(stats["report_count"], 3)
            self.assertEqual(stats["recipient_count"], 3)
            self.assertEqual(stats["reports_with_images"], 1)
            self.assertEqual(stats["image_warnings"], 2)

            payload = diagnostics.to_dict()
            self.assertEqual(payload["top_funnel"]["multi_provider_count"], 1)
            self.assertEqual(payload["top_funnel"]["provider_counts"]["alpha"], 1)
            self.assertEqual(payload["events"][-1]["label"], "failed")

            summary_markdown = diagnostics.to_summary_markdown()
            self.assertIn("# Daily News Run Summary", summary_markdown)
            self.assertIn("## Source Health", summary_markdown)
            self.assertIn("## Story Coverage", summary_markdown)
            self.assertIn("## Model Activity", summary_markdown)
            self.assertIn("## Diagnostic Artifacts", summary_markdown)
            self.assertIn(
                "problem sources: delta source (source_error)",
                summary_markdown.lower(),
            )
            self.assertIn("Alpha source (7.2s)", summary_markdown)
            self.assertIn("Delta source (2m 5.0s)", summary_markdown)
            self.assertIn("duplicate=2", summary_markdown)
            self.assertIn("skipped=0", summary_markdown)
            self.assertIn("summary_json: ", summary_markdown)
            self.assertIn("- Source languages: {'en': 4}", summary_markdown)

            review_markdown = diagnostics.to_run_review_markdown(
                report_body="Daily News Summary\n==================\n\nA useful report."
            )
            self.assertIn("# Latest News Run Review", review_markdown)
            self.assertIn("## Run Settings", review_markdown)
            self.assertIn(
                "| Story Scale Screening model | scale_ref (scale_model) [external] @ https://api.example.com |",
                review_markdown,
            )
            self.assertIn(
                "| Title Generation model | title_ref (title_model) [mlx-vlm] @ http://localhost:9090 |",
                review_markdown,
            )
            self.assertIn(
                "| Image Art Direction model | image_ref (image_model) [external] @ https://api.example.com |",
                review_markdown,
            )
            self.assertIn("| Source languages | {'en': 4} |", review_markdown)
            self.assertIn("## Top-Level KPIs", review_markdown)
            self.assertIn("## Funnel Stats", review_markdown)
            self.assertIn("## Source Health", review_markdown)
            self.assertIn("## Model Activity", review_markdown)
            self.assertIn("## Final Output Stats", review_markdown)
            self.assertIn("## Warnings", review_markdown)
            self.assertIn("Run failed: RuntimeError: synthetic failure", review_markdown)
            self.assertIn("Run aborted: manual stop", review_markdown)
            self.assertIn("A useful report.", review_markdown)

            details_markdown = diagnostics.to_markdown()
            self.assertIn("# Daily News Run Details", details_markdown)
            self.assertIn("- Source languages: {'en': 4}", details_markdown)
            self.assertIn(
                "- Story Scale Screening model: scale_ref (scale_model) [external] @ https://api.example.com",
                details_markdown,
            )
            self.assertIn(
                "- Title Generation model: title_ref (title_model) [mlx-vlm] @ http://localhost:9090",
                details_markdown,
            )
            self.assertIn(
                "- Image Art Direction model: image_ref (image_model) [external] @ https://api.example.com",
                details_markdown,
            )
            self.assertIn(
                "- Image Art Direction is an independent LLM call; Story Discovery has no LLM stage (inherits default).",
                details_markdown,
            )
            self.assertIn(
                "story_scale_screening=external",
                _model_backend_value(diagnostics.settings),
            )
            self.assertIn(
                "title_generation=mlx-vlm",
                _model_backend_value(diagnostics.settings),
            )
            self.assertIn(
                "image_art_direction=external",
                _model_backend_value(diagnostics.settings),
            )
            self.assertIn("## Source Funnel", details_markdown)
            self.assertIn("## Story Clustering", details_markdown)
            self.assertIn("## Story Drafting", details_markdown)
            self.assertIn("## Global Story Scale Screening", details_markdown)
            self.assertIn("... 1 more in raw diagnostics", details_markdown)
            self.assertIn("## Global Story Selection", details_markdown)
            self.assertIn("## Story Coverage Deficit", details_markdown)
            self.assertIn("## Story Backfill", details_markdown)
            self.assertIn("Source-match rejections: 1", details_markdown)
            self.assertIn("Image warning: generation failed", details_markdown)
            self.assertIn("Diagnostic artifacts:", details_markdown)
            self.assertIn("- Activity: latest=memory_pressure, memory_free=18.5%, swapouts=3", details_markdown)

            json_path, markdown_path, summary_path = diagnostics.write(
                root / "batch",
                "2026-06-01_10-15-30",
            )
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertIn("# Daily News Run Details", markdown_path.read_text(encoding="utf-8"))
            self.assertIn("# Daily News Run Summary", summary_path.read_text(encoding="utf-8"))

            details_path = diagnostics.write_details_json(root / "details" / "run.json")
            self.assertTrue(details_path.exists())
            details_payload = json.loads(details_path.read_text(encoding="utf-8"))
            self.assertEqual(details_payload["article_summary_count"], 7)
            self.assertEqual(details_payload["top_funnel"]["merged_count"], 2)

            review_path = diagnostics.write_run_review_markdown(
                root / "review" / "latest_run.md",
                report_body="Daily News Summary\n==================\n\nA useful report.",
            )
            self.assertTrue(review_path.exists())
            review_text = review_path.read_text(encoding="utf-8")
            self.assertIn("# Latest News Run Review", review_text)
            self.assertIn("## Warnings", review_text)
            self.assertIn("A useful report.", review_text)

    def test_empty_diagnostics_cover_fallbacks(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={
                "terminal_output_log": "/tmp/terminal.log",
            },
        )

        self.assertEqual(diagnostics.summary_stats()["duration"], "N/A")
        self.assertEqual(run_status_from_events(diagnostics.events), "unknown")

        summary_markdown = diagnostics.to_summary_markdown()
        self.assertIn("- Preset: custom", summary_markdown)
        self.assertIn("- Source scope: unknown", summary_markdown)
        self.assertIn("- Recipient scope: unknown", summary_markdown)
        self.assertIn("- Run log: /tmp/terminal.log", summary_markdown)
        self.assertIn("Status counts: N/A", summary_markdown)
        self.assertIn("Rejections: none recorded", summary_markdown)
        self.assertIn("Calls by task: none recorded", summary_markdown)

        review_markdown = diagnostics.to_run_review_markdown()
        self.assertIn("| Status | unknown |", review_markdown)
        self.assertIn("| History store | N/A |", review_markdown)
        self.assertIn("## Final Report Preview", review_markdown)
        self.assertIn("_No final prose report was rendered for this run._", review_markdown)

        details_markdown = diagnostics.to_markdown()
        self.assertIn("# Daily News Run Details", details_markdown)
        self.assertIn("## Source Funnel", details_markdown)
        self.assertNotIn("## Story Clustering", details_markdown)
        self.assertNotIn("## Global Story Selection", details_markdown)

    def test_run_status_from_events_precedence(self) -> None:
        self.assertEqual(run_status_from_events([]), "unknown")
        self.assertEqual(run_status_from_events([{"label": "completed"}]), "completed")
        self.assertEqual(
            run_status_from_events([{"label": "completed"}, {"label": "aborted"}]),
            "aborted",
        )
        self.assertEqual(
            run_status_from_events(
                [{"label": "completed"}, {"label": "aborted"}, {"label": "failed"}]
            ),
            "failed",
        )

    def test_event_appends_timestamped_entry(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )

        diagnostics.event("completed", reason="all done")

        self.assertEqual(diagnostics.events[-1]["label"], "completed")
        self.assertEqual(diagnostics.events[-1]["reason"], "all done")
        self.assertIn("at", diagnostics.events[-1])

    def test_private_helper_fallbacks(self) -> None:
        self.assertEqual(_model_assignment_value({"model_assignments": "bad"}, "story"), "N/A")
        self.assertEqual(
            _model_assignment_value(
                {"model_assignments": {"story": "bad"}},
                "story",
            ),
            "N/A",
        )
        self.assertEqual(
            _model_backend_value({"model_backend": "openai", "model_assignments": "bad"}),
            "openai",
        )
        self.assertEqual(_pipeline_budget_value({"pipeline_budget": "bad"}), "N/A")
        self.assertEqual(_duration_label("not-a-date", []), "N/A")
        self.assertEqual(
            _duration_label(
                "2026-06-01T10:00:00",
                [{"at": "not-a-date"}],
            ),
            "N/A",
        )

    def test_record_delivery_serializes_normalized_mapping(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
            events=[{"at": "2026-06-01T10:01:00", "label": "completed"}],
        )
        diagnostics.record_delivery(
            "sent",
            recipients=["reader@example.com"],
        )

        self.assertEqual(
            diagnostics.to_dict()["delivery"],
            {
                "status": "sent",
                "recipients": ["reader@example.com"],
                "reason": "",
                "error_type": "",
                "error_message": "",
                "phase": "",
                "accepted_recipients": [],
                "rejected_recipients": [],
            },
        )
        self.assertEqual(run_status_from_events(diagnostics.events), "completed")

        review_markdown = diagnostics.to_run_review_markdown(report_body="Body")
        self.assertIn("| Delivery | sent |", review_markdown)
        self.assertIn("## Delivery", review_markdown)
        self.assertIn("| Recipients | reader@example.com |", review_markdown)

        summary_markdown = diagnostics.to_summary_markdown()
        self.assertIn("- Delivery: sent", summary_markdown)

    def test_record_delivery_rich_metadata_renders_and_keeps_status(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
            events=[{"at": "2026-06-01T10:01:00", "label": "completed"}],
        )
        diagnostics.record_delivery(
            "failed",
            recipients=["reader@example.com", "editor@example.com"],
            reason="delivery refused for: editor@example.com",
            error_type="SMTPRecipientsRefused",
            error_message="refused recipient",
            phase="send",
            accepted_recipients=["reader@example.com"],
            rejected_recipients=["editor@example.com"],
        )

        recorded = diagnostics.to_dict()["delivery"]
        self.assertEqual(recorded["phase"], "send")
        self.assertEqual(recorded["accepted_recipients"], ["reader@example.com"])
        self.assertEqual(recorded["rejected_recipients"], ["editor@example.com"])

        # Run status remains independent: no failed run event is added.
        self.assertEqual(run_status_from_events(diagnostics.events), "completed")
        self.assertFalse(any(event["label"] == "failed" for event in diagnostics.events))

        review_markdown = diagnostics.to_run_review_markdown()
        self.assertIn("| Delivery | failed |", review_markdown)
        self.assertIn("| Phase | send |", review_markdown)
        self.assertIn("| Accepted | reader@example.com |", review_markdown)
        self.assertIn("| Rejected | editor@example.com |", review_markdown)
        self.assertIn(
            "| Error | SMTPRecipientsRefused: refused recipient |",
            review_markdown,
        )

        # Collections are normalized to lists of strings on re-record.
        self.assertIsNone(
            diagnostics.record_delivery(
                "sent",
                recipients=["a@example.com"],
                phase="send",
                accepted_recipients=None,
            )
        )

        # Empty rich collections stay compact: no rows for unrecorded data.
        plain = RunDiagnostics(run_started_at="2026-06-01T10:00:00", settings={})
        plain.record_delivery("skipped: user_disabled", reason="delivery disabled by profile")
        plain_markdown = plain.to_run_review_markdown()
        self.assertIn("| Status | skipped: user_disabled |", plain_markdown)
        self.assertNotIn("| Phase |", plain_markdown)
        self.assertNotIn("| Accepted |", plain_markdown)
        self.assertNotIn("| Rejected |", plain_markdown)

    def test_record_delivery_failed_keeps_run_status_and_adds_no_event(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
            events=[{"at": "2026-06-01T10:01:00", "label": "completed"}],
        )
        diagnostics.record_delivery(
            "failed",
            recipients=["reader@example.com"],
            reason="delivery failed after report construction",
            error_type="SMTPException",
            error_message="connection refused",
        )

        self.assertEqual(run_status_from_events(diagnostics.events), "completed")
        self.assertFalse(any(event["label"] == "failed" for event in diagnostics.events))
        self.assertEqual(
            diagnostics.to_dict()["delivery"]["error_type"],
            "SMTPException",
        )
        review_markdown = diagnostics.to_run_review_markdown()
        self.assertIn("| Delivery | failed |", review_markdown)
        self.assertIn("| Error | SMTPException: connection refused |", review_markdown)
        details_markdown = diagnostics.to_markdown()
        self.assertIn("- Delivery: failed", details_markdown)

    def test_empty_delivery_remains_backward_compatible(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )
        self.assertEqual(diagnostics.to_dict()["delivery"], {})
        review_markdown = diagnostics.to_run_review_markdown()
        self.assertIn("| Delivery | not recorded |", review_markdown)
        self.assertIn("| Recipients | none |", review_markdown)
        self.assertIn("| Status | not recorded |", review_markdown)
        summary_markdown = diagnostics.to_summary_markdown()
        self.assertIn("- Delivery: not recorded", summary_markdown)

    def test_record_report_drops_unknown_keys(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )
        diagnostics.record_report(
            path="output/daily_outputs/latest_run.md",
            recipient_count=2,
            recipients=["reader@example.com"],
            recipient_counts=2,  # typo'd key must not persist
        )
        self.assertEqual(
            diagnostics.to_dict()["reports"],
            [
                {
                    "path": "output/daily_outputs/latest_run.md",
                    "recipient_count": 2,
                    "recipients": ["reader@example.com"],
                }
            ],
        )

    def test_prompt_snapshot_recording_sequences_and_round_trip(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )
        self.assertEqual(diagnostics.to_dict()["prompt_snapshots"], [])

        first_sequence = diagnostics.record_prompt_snapshot(
            {
                "captured_at": "2026-06-01T10:00:01Z",
                "task": "article_summary",
                "task_name": "analysis for Headline",
                "messages": [
                    {"type": "system", "content": "Summarize."},
                    {"type": "human", "content": [{"text": "Article body"}]},
                ],
                "retry_attempts": 0,
                "used_fallback": False,
            }
        )
        second_sequence = diagnostics.record_prompt_snapshot(
            {
                "captured_at": "2026-06-01T10:00:02Z",
                "task": "story_drafting",
                "task_name": "story synthesis for Story",
                "messages": [{"type": "system", "content": "Draft."}],
            }
        )
        self.assertEqual(first_sequence, 1)
        self.assertEqual(second_sequence, 2)

        payload = diagnostics.to_dict()
        self.assertEqual(len(payload["prompt_snapshots"]), 2)
        self.assertEqual(payload["prompt_snapshots"][0]["sequence"], 1)
        self.assertEqual(payload["prompt_snapshots"][1]["sequence"], 2)
        self.assertEqual(
            payload["prompt_snapshots"][0]["messages"][1]["content"],
            [{"text": "Article body"}],
        )
        # Snapshot recording must not leak into summary stats or Markdown.
        self.assertEqual(diagnostics.summary_stats()["model_call_count"], 0)
        self.assertNotIn("prompt_snapshots", diagnostics.to_summary_markdown())
        self.assertNotIn("Summarize.", diagnostics.to_markdown())

        with tempfile.TemporaryDirectory() as tmpdir:
            details_path = Path(tmpdir) / "latest_run_details.json"
            diagnostics.write_details_json(details_path)
            written = json.loads(details_path.read_text(encoding="utf-8"))
            self.assertEqual(len(written["prompt_snapshots"]), 2)
            self.assertEqual(written["prompt_snapshots"][0]["sequence"], 1)
            self.assertEqual(
                written["prompt_snapshots"][0]["messages"][0],
                {"type": "system", "content": "Summarize."},
            )

    def test_prompt_snapshot_update_metadata_and_isolated_copy(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )
        details = {"task": "article_summary", "messages": []}
        sequence = diagnostics.record_prompt_snapshot(details)
        # Later caller-side mutation must not alter the recorded payload.
        details["messages"] = [{"type": "human", "content": "changed"}]
        self.assertEqual(diagnostics.prompt_snapshots[0]["messages"], [])

        diagnostics.update_prompt_snapshot(
            sequence,
            retry_attempts=2,
            used_fallback=True,
        )
        diagnostics.update_prompt_snapshot(999, retry_attempts=9)
        snapshot = diagnostics.to_dict()["prompt_snapshots"][0]
        self.assertEqual(snapshot["retry_attempts"], 2)
        self.assertTrue(snapshot["used_fallback"])

    def test_prompt_snapshot_concurrent_recording_keeps_unique_sequences(self) -> None:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={},
        )
        errors: list[Exception] = []

        def record(index: int) -> None:
            try:
                diagnostics.record_prompt_snapshot(
                    {
                        "task": "article_summary",
                        "task_name": f"analysis for Article {index}",
                        "messages": [{"type": "human", "content": f"body {index}"}],
                    }
                )
            except Exception as error:  # pragma: no cover - failure path
                errors.append(error)

        threads = [
            threading.Thread(target=record, args=(index,)) for index in range(16)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        snapshots = diagnostics.to_dict()["prompt_snapshots"]
        self.assertEqual(len(snapshots), 16)
        self.assertEqual(
            sorted(snapshot["sequence"] for snapshot in snapshots),
            list(range(1, 17)),
        )

    def _populated_diagnostics(self, root: Path) -> RunDiagnostics:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={
                "preset_id": "daily",
                "source_scope": "all",
                "source_languages": {"en": 4},
                "recipient_scope": "vip",
                "url_reuse_blocking_enabled": True,
                "output_dir": str(root / "daily_outputs"),
                "run_log_path": str(root / "daily_outputs" / "run.log"),
                "run_used_urls_path": str(root / "daily_outputs" / "used_urls.txt"),
                "history_db_path": str(root / "history" / "news_history.duckdb"),
                "latest_run_markdown_path": str(root / "daily_outputs" / "latest_run.md"),
                "run_staging_dir": str(root / "daily_outputs" / ".staging"),
                "model": "gemma-2b",
                "model_name": "default_model",
                "model_backend": "mlx-lm",
                "model_assignments": {
                    "article_summary": {
                        "reference": "summary_ref",
                        "name": "summary_model",
                        "backend": "mlx-lm",
                        "base_url": "http://localhost:8080",
                    },
                    "story_drafting": {
                        "reference": "draft_ref",
                        "name": "draft_model",
                        "backend": "openai",
                        "base_url": "https://api.example.com",
                    },
                    "story_scale_screening": {
                        "reference": "scale_ref",
                        "name": "scale_model",
                        "backend": "external",
                        "base_url": "https://api.example.com",
                    },
                    "title_generation": {
                        "reference": "title_ref",
                        "name": "title_model",
                        "backend": "mlx-vlm",
                        "base_url": "http://localhost:9090",
                    },
                    "image_art_direction": {
                        "reference": "image_ref",
                        "name": "image_model",
                        "backend": "external",
                        "base_url": "https://api.example.com",
                    },
                },
                "model_max_input_tokens": 2048,
                "article_summary_max_tokens": 512,
                "story_drafting_max_tokens": 768,
                "pipeline_budget": {
                    "article_text_token_limit": 900,
                    "total_article_summary_cap": 2200,
                    "recent_window_hours": 48,
                    "max_articles_per_source": 25,
                    "min_articles_per_story": 2,
                    "max_stories": 4,
                },
                "recent_window_hours": 48,
                "max_stories": 4,
                "min_articles_per_story": 2,
                "story_cluster_similarity_threshold": 0.31,
                "story_selection_overlap_threshold": 0.25,
                "story_scale_screening_enabled": True,
                "image_generation_enabled": True,
                "slow_source_warning_seconds": 5.0,
                "model_default_sampling": {"temperature": 0.2},
                "model_reasoning_sampling": {"temperature": 0.1},
                "model_task_sampling": {"summary": {"temperature": 0.2}},
                "source_count": 4,
            },
            article_summary_count=7,
        )

        diagnostics.record_top_funnel(
            providers={
                "alpha": [
                    {
                        "title": "Alpha provider story",
                        "url": "https://alpha.example/1",
                        "provider": "alpha",
                        "providers": ["alpha"],
                        "frames": ["alpha-frame"],
                        "domain": "alpha.example",
                        "score": 2.5,
                        "num_comments": 3,
                        "match_score": 0.9,
                    }
                ],
                "beta": [
                    {
                        "title": "Beta provider story",
                        "url": "https://beta.example/1",
                        "provider": "beta",
                        "providers": ["beta"],
                        "frames": [],
                        "domain": "beta.example",
                        "score": 1.5,
                        "num_comments": 0,
                    }
                ],
            },
            merged=[
                {
                    "title": "Merged alpha beta",
                    "url": "https://news.example/story",
                    "provider": "alpha",
                    "providers": ["alpha", "beta"],
                    "frames": ["f1", "f2"],
                    "domain": "news.example",
                    "score": 4.0,
                    "num_comments": 12,
                    "match_score": 0.75,
                },
                {
                    "title": "Merged gamma",
                    "url": "https://news.example/story2",
                    "provider": "gamma",
                    "providers": ["gamma"],
                    "frames": [],
                    "domain": "news.example",
                    "score": 2.0,
                    "num_comments": 4,
                },
            ],
            seed_merged=[
                {
                    "title": "Seed story",
                    "url": "https://seed.example/story",
                    "provider": "seed",
                    "providers": ["seed"],
                    "frames": [],
                    "domain": "seed.example",
                    "score": 1.0,
                }
            ],
            validation_merged=[
                {
                    "title": "Validation story",
                    "url": "https://validation.example/story",
                    "provider": "validation",
                    "providers": ["validation"],
                    "frames": [],
                    "domain": "validation.example",
                    "score": 0.5,
                }
            ],
            provider_metadata={
                "alpha": {"label": "Alpha"},
                "beta": {"label": "Beta"},
            },
        )

        diagnostics.record_source_run(
            {
                "source_index": 1,
                "source": "Alpha source",
                "status": "ok",
                "feed_item_count": 8,
                "selected_item_count": 5,
                "fresh_article_count": 4,
                "rejected_counts": {"duplicate": 2, "paywalled": 1},
                "elapsed_seconds": 7.25,
                "slow_source": False,
                "timeout_count": 0,
                "scrape_status_counts": {"timeout_connect": 1},
                "feed_rejections": [
                    {
                        "title": "Duplicate feed item",
                        "observed_source_labels": ["Alpha Feed", "Alpha Mirror"],
                    }
                ],
            }
        )
        diagnostics.record_source_run(
            {
                "source_index": 2,
                "source": "Beta source",
                "status": "no_recent_items",
                "feed_item_count": 0,
                "selected_item_count": 0,
                "fresh_article_count": 0,
                "rejected_counts": {"too_old": 2},
                "elapsed_seconds": 1.5,
                "timeout_count": 1,
                "scrape_status_counts": {"timeout_read": 1},
            }
        )
        diagnostics.record_source_run(
            {
                "source_index": 3,
                "source": "Gamma source",
                "status": "no_scraped_recent_items",
                "feed_item_count": "bad",
                "selected_item_count": 1,
                "fresh_article_count": 1,
                "rejected_counts": {"skipped": "oops"},
                "elapsed_seconds": "bad",
                "timeout_count": 0,
            }
        )
        diagnostics.record_source_run(
            {
                "source_index": 4,
                "source": "Delta source",
                "status": "source_error",
                "feed_item_count": 3,
                "selected_item_count": 0,
                "fresh_article_count": 0,
                "rejected_counts": {"timeout": 3},
                "elapsed_seconds": 125.0,
                "slow_source": True,
                "timeout_count": 0,
                "scrape_status_counts": {"timeout_scrape": 2},
            }
        )

        diagnostics.record_article_budget(
            {"candidate_count": 11, "included_count": 8, "dropped_count": 3}
        )
        diagnostics.record_model_call_stats(
            {
                "calls": {
                    "analysis for final synthesis of city council": 2,
                    "analysis for article text": 3,
                    "story synthesis for feature package": 4,
                    "custom task": 5,
                },
                "token_usage": {
                    "analysis for final synthesis of city council": self._token_bucket(
                        calls=2,
                        estimated_input_tokens=100,
                        estimated_output_tokens=30,
                        max_output_tokens_requested=120,
                        actual_input_tokens=90,
                        actual_output_tokens=28,
                        actual_total_tokens=118,
                        actual_usage_calls=1,
                        fallback_calls=0,
                        max_estimated_input_tokens=100,
                        max_estimated_output_tokens=30,
                        max_actual_input_tokens=90,
                        max_actual_output_tokens=28,
                    ),
                    "analysis for article text": self._token_bucket(
                        calls=3,
                        estimated_input_tokens=60,
                        estimated_output_tokens=20,
                        max_output_tokens_requested=80,
                        actual_input_tokens=50,
                        actual_output_tokens=18,
                        actual_total_tokens=68,
                        actual_usage_calls=2,
                        fallback_calls=1,
                        max_estimated_input_tokens=60,
                        max_estimated_output_tokens=20,
                        max_actual_input_tokens=50,
                        max_actual_output_tokens=18,
                    ),
                    "story synthesis for feature package": self._token_bucket(
                        calls=4,
                        estimated_input_tokens=40,
                        estimated_output_tokens=10,
                        max_output_tokens_requested=50,
                        actual_input_tokens=35,
                        actual_output_tokens=9,
                        actual_total_tokens=44,
                        actual_usage_calls=1,
                        fallback_calls=0,
                        max_estimated_input_tokens=40,
                        max_estimated_output_tokens=10,
                        max_actual_input_tokens=35,
                        max_actual_output_tokens=9,
                    ),
                    "custom task": self._token_bucket(
                        calls=5,
                        estimated_input_tokens=20,
                        estimated_output_tokens=5,
                        max_output_tokens_requested=25,
                        actual_input_tokens=15,
                        actual_output_tokens=4,
                        actual_total_tokens=19,
                        actual_usage_calls=1,
                        fallback_calls=1,
                        max_estimated_input_tokens=20,
                        max_estimated_output_tokens=5,
                        max_actual_input_tokens=15,
                        max_actual_output_tokens=4,
                    ),
                    "skip-me": "not a dict",
                },
                "retries": 2,
                "fallbacks": 1,
            }
        )
        diagnostics.record_activity_snapshot(
            {"label": "boot", "memory_free_pct": 72.0, "swapouts": 0}
        )
        diagnostics.record_activity_snapshot(
            {"label": "memory_pressure", "memory_free_pct": 18.5, "swapouts": 3}
        )
        diagnostics.record_report(
            path=str(root / "reports" / "daily_1.md"),
            recipient_count=1,
            image_art={"final_image_path": str(root / "images" / "daily_1.png")},
        )
        diagnostics.record_report(
            path=str(root / "reports" / "daily_2.md"),
            recipient_count=1,
            image_art={"error": "generation failed"},
        )
        diagnostics.record_report(
            path=str(root / "reports" / "daily_3.md"),
            recipient_count=1,
            image_art={"art_prompt_error": "prompt failed"},
        )
        diagnostics.record_artifact("summary_json", str(root / "artifacts" / "summary.json"))
        diagnostics.record_artifact(
            "source_report", str(root / "artifacts" / "source_report.md")
        )
        diagnostics.record_artifact(
            "candidate_urls", str(root / "artifacts" / "candidate_urls.txt"), count=11
        )

        diagnostics.events.extend(
            [
                {
                    "at": "2026-06-01T10:01:00",
                    "label": "story_clustering",
                    "clustering_method": "connected_components",
                    "candidate_count": 40,
                    "included_count": 12,
                    "dropped_count": 2,
                    "story_count": 14,
                    "viable_story_count": 12,
                    "similarity_threshold": 0.31,
                    "component_overlap_suppress_threshold": 0.4,
                    "stories": [
                        {
                            "story_title": "Alpha to beta",
                            "story_key": "alpha-beta",
                            "article_count": 3,
                            "source_count": 2,
                            "story_strength_score": 9.0,
                            "connectedness_score": 0.82,
                            "average_similarity": 0.77,
                            "articles": [
                                {
                                    "source": "Alpha source",
                                    "title": "Alpha headline",
                                    "article_id": "a1",
                                },
                                {
                                    "source": "Beta source",
                                    "title": "Beta headline",
                                    "article_id": "b1",
                                },
                            ],
                        },
                        {
                            "title": "Title fallback story",
                            "story_key": "title-fallback",
                            "article_count": 2,
                            "source_count": 1,
                            "story_strength_score": 4.0,
                            "connectedness_score": 0.51,
                            "average_similarity": 0.45,
                            "articles": [
                                {
                                    "source": "Gamma source",
                                    "title": "Gamma headline",
                                    "article_id": "g1",
                                }
                            ],
                        },
                        {
                            "story_key": "key-fallback",
                            "article_count": 1,
                            "source_count": 1,
                            "story_strength_score": 2.0,
                            "connectedness_score": 0.22,
                            "average_similarity": 0.11,
                            "articles": [
                                {
                                    "source": "Delta source",
                                    "title": "Delta headline",
                                    "article_id": "d1",
                                }
                            ],
                        },
                    ],
                },
                {
                    "at": "2026-06-01T10:02:00",
                    "label": "story_drafting",
                    "story_blocks_requested": 14,
                    "story_drafts_generated": 14,
                    "story_drafts_rejected": 2,
                    "contradiction_analytics": {
                        "stories_checked": 14,
                        "raw_contradiction_count": 6,
                        "validated_contradiction_count": 3,
                        "render_eligible_contradiction_count": 2,
                        "raw_contradictions_rejected_by_citation_validation": 1,
                    },
                },
                {
                    "at": "2026-06-01T10:03:00",
                    "label": "global_story_scale_screening",
                    "enabled": True,
                    "required_scale": "large",
                    "candidate_count": 14,
                    "judged_count": 14,
                    "kept_count": 3,
                    "dropped_count": 11,
                    "scale_counts": {"large": 3, "small": 11},
                    "dropped": [
                        {
                            "story_title": f"Dropped story {index}",
                            "article_count": 1,
                            "source_count": 1,
                            "scale_screening_reason": "too small",
                        }
                        for index in range(1, 12)
                    ],
                },
                {
                    "at": "2026-06-01T10:04:00",
                    "label": "global_story_selection",
                    "story_count": 3,
                    "selected_story_count": 3,
                    "max_stories": 4,
                    "overlap_threshold": 0.25,
                    "selected": [
                        {
                            "global_selection_rank": 1,
                            "story_title": "Alpha to beta",
                            "article_count": 3,
                            "source_count": 2,
                        },
                        {
                            "global_selection_rank": 2,
                            "story_title": "Title fallback story",
                            "article_count": 2,
                            "source_count": 1,
                        },
                        {
                            "global_selection_rank": 3,
                            "story_title": "Key fallback story",
                            "article_count": 1,
                            "source_count": 1,
                        },
                    ],
                },
                {
                    "at": "2026-06-01T10:05:00",
                    "label": "story_coverage_deficit",
                    "selected_story_count": 3,
                    "target_story_count": 4,
                    "deficit": 1,
                },
                {
                    "at": "2026-06-01T10:06:00",
                    "label": "story_backfill",
                    "enabled": True,
                    "reason": "coverage deficit",
                    "iterations": 1,
                    "attempted_article_count": 5,
                    "new_article_summary_count": 2,
                    "new_story_draft_count": 1,
                },
                {
                    "at": "2026-06-01T10:14:00",
                    "label": "aborted",
                    "reason": "manual stop",
                },
                {
                    "at": "2026-06-01T10:15:30",
                    "label": "failed",
                    "error_type": "RuntimeError",
                    "error_message": "synthetic failure",
                },
            ]
        )

        return diagnostics

    def _token_bucket(self, **details: int) -> dict[str, int]:
        return details


if __name__ == "__main__":
    unittest.main()
