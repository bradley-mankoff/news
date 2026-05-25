from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml
from langchain_core.messages import AIMessage
from langgraph.graph import END

from news_pipeline.config import (
    _configured_run_mode,
    _configured_model_reference,
    _sync_cursorignore_latest_output,
    build_model_server_command,
    configured_max_articles_per_story,
    configured_max_stories_per_topic,
    configured_story_cluster_similarity_threshold,
    configured_topic_mode,
    configured_model_profile,
    infer_model_profile_key,
    load_client_config,
    load_predefined_topics,
    load_sources,
    load_topic_definitions,
    load_top_funnel_providers,
    resolve_model_name,
)
from news_pipeline import cli
from news_pipeline.pipeline import (
    ProgressTracker,
    _cluster_supported_fallback_topics,
    _enforce_text_free_image_prompt,
    _sanitize_overlay_headline,
    _select_per_topic_feed_items,
    _report_reference_key,
    _score_topic_against_story,
    _score_topic_relevance,
    annotate_topic_discovery_signals,
    budget_article_targets,
    build_article_summary_prompt_messages,
    build_chat_model,
    build_dev_final_synthesis_preview,
    build_final_synthesis_payload,
    build_report_body,
    build_top_funnel_article_targets_for_coverage_gaps,
    capture_activity_snapshot,
    clean_synthesis_for_publication,
    describe_final_synthesis_rejection,
    estimate_message_token_count,
    filter_reports_for_references,
    get_default_final_synthesis_instructions,
    is_valid_final_synthesis_response,
    organize_article_targets_into_stories,
    prepare_candidate_topics_for_selection,
    load_predefined_topics_for_run,
    select_topics_for_run,
    select_topics_soft_weighted,
    should_continue,
    strip_model_artifacts,
    truncate_text_to_token_limit,
)


class ConfigAndTopicSelectionTests(unittest.TestCase):
    def test_build_chat_model_disables_hidden_template_thinking(self) -> None:
        with patch("news_pipeline.pipeline.ChatOpenAI") as chat_model_class:
            build_chat_model(max_tokens=123, task="final_synthesis")

        kwargs = chat_model_class.call_args.kwargs
        self.assertEqual(kwargs["max_tokens"], 123)
        self.assertEqual(
            kwargs["extra_body"]["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertIn("top_p", kwargs["extra_body"])

    def test_progress_tracker_allocates_article_summary_progress(self) -> None:
        tracker = ProgressTracker()
        with redirect_stdout(StringIO()):
            tracker.reset(total_sources=20)
            for source_index in range(1, 21):
                tracker.start_source(source_index)
                tracker.source_completed()

            self.assertEqual(tracker.current_step, "sources")
            self.assertEqual(tracker.meter_done, 20)
            self.assertEqual(tracker.meter_total, 20)
            self.assertEqual(tracker.meter_unit, "sources")

            tracker.start_article_summary(12)
            self.assertEqual(tracker.current_step, "summaries")
            self.assertEqual(tracker.meter_done, 0)
            self.assertEqual(tracker.meter_total, 12)
            self.assertEqual(tracker.meter_unit, "articles")

            for _ in range(6):
                tracker.article_completed()
            self.assertEqual(tracker.meter_done, 6)

            for _ in range(6):
                tracker.article_completed()
            self.assertEqual(tracker.meter_done, 12)

            tracker.set_final_step("reports", 1)
            self.assertEqual(tracker.current_step, "report")

            tracker.finish("done")
            self.assertEqual(tracker.current_step, "finalize")

    def test_progress_tracker_keeps_article_count_after_summary_phase(self) -> None:
        tracker = ProgressTracker()
        output = StringIO()
        with redirect_stdout(output):
            tracker.reset(total_sources=1)
            tracker.start_source(1)
            tracker.source_completed()
            tracker.start_article_summary(2)
            tracker.article_completed()
            tracker.article_completed()
            tracker.set_final_step("reports", 1)

        self.assertIn("2/2 articles", output.getvalue())
        self.assertNotIn("0/0 articles", output.getvalue())

    def _write_config(self, payload: dict) -> Path:
        return self._write_yaml(payload, "sources.yaml")

    def _write_yaml(self, payload: dict, filename: str) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / filename
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def test_assistant_ignore_manager_keeps_core_and_current_output_visible(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root_dir = Path(temp_dir.name)
        output_dir = root_dir / "output" / "daily_outputs"
        run_output_dir = output_dir / "2026-05-02"
        run_output_dir.mkdir(parents=True)
        (run_output_dir / "run_details_2026-05-02_09-00-00.json").write_text("{}", encoding="utf-8")
        (run_output_dir / "news_report_2026-05-02_10-30-00_big.txt").write_text("latest", encoding="utf-8")
        (run_output_dir / "topics_2026-05-02_10-30-00.json").write_text("{}", encoding="utf-8")
        cursorignore_path = root_dir / ".cursorignore"
        cursorignore_path.write_text(
            "custom-rule\n\n"
            "# >>> news-pipeline latest output >>>\n"
            "output/daily_outputs/*\n"
            "!output/daily_outputs/2026-05-01/\n"
            "!output/daily_outputs/2026-05-01/**\n"
            "# <<< news-pipeline latest output <<<\n",
            encoding="utf-8",
        )

        _sync_cursorignore_latest_output(root_dir, output_dir, run_output_dir)

        for ignore_name in (".codexignore", ".cursorignore", ".continueignore"):
            with self.subTest(ignore_name=ignore_name):
                assistant_ignore = (root_dir / ignore_name).read_text(encoding="utf-8")
                if ignore_name == ".cursorignore":
                    self.assertIn("custom-rule", assistant_ignore)
                self.assertIn("!news_pipeline/**", assistant_ignore)
                self.assertIn("!config/sources.yaml", assistant_ignore)
                self.assertIn("!todays_news.py", assistant_ignore)
                self.assertIn("output/daily_outputs/*", assistant_ignore)
                self.assertIn("!output/daily_outputs/2026-05-02/", assistant_ignore)
                self.assertIn("!output/daily_outputs/2026-05-02/news_report_2026-05-02_10-30-00_big.txt", assistant_ignore)
                self.assertIn("!output/daily_outputs/2026-05-02/topics_2026-05-02_10-30-00.json", assistant_ignore)
                self.assertNotIn("2026-05-02_09-00-00", assistant_ignore)
                self.assertNotIn("!output/daily_outputs/2026-05-02/**", assistant_ignore)
                self.assertNotIn("2026-05-01", assistant_ignore)

    def test_model_aliases_resolve_to_latest_repo_ids(self) -> None:
        self.assertEqual(
            resolve_model_name("qwen-9b-dense"),
            "TheCluster/Qwen3.5-9B-Heretic-MLX-mxfp4",
        )
        self.assertEqual(
            resolve_model_name("gemma-26b-moe"),
            "mlx-community/gemma-4-26B-A4B-it-heretic-4bit",
        )

    def test_removed_model_references_are_rejected(self) -> None:
        removed_references = (
            "qwen-9b-medium",
            "TheCluster/Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8",
        )
        for model_reference in removed_references:
            with self.subTest(model_reference=model_reference):
                with self.assertRaises(ValueError):
                    resolve_model_name(model_reference)
                with self.assertRaises(ValueError):
                    infer_model_profile_key(model_reference)

    def test_unknown_model_reference_passes_through_as_raw_repo_id(self) -> None:
        self.assertEqual(
            resolve_model_name("some-org/custom-model"),
            "some-org/custom-model",
        )

    def test_model_reference_uses_configurable_default(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEWS_DEFAULT_MODEL": "qwen-9b-dense"},
            clear=True,
        ):
            self.assertEqual(_configured_model_reference(), "qwen-9b-dense")

    def test_run_mode_uses_news_dev_for_backward_compatibility(self) -> None:
        with patch.dict("os.environ", {"NEWS_DEV": "0"}, clear=True):
            self.assertEqual(_configured_run_mode(), "prod")

        with patch.dict("os.environ", {"NEWS_DEV": "1"}, clear=True):
            self.assertEqual(_configured_run_mode(), "dev")

    def test_explicit_run_mode_supports_local_prod(self) -> None:
        with patch.dict(
            "os.environ",
            {"NEWS_RUN_MODE": "local_prod", "NEWS_DEV": "1"},
            clear=True,
        ):
            self.assertEqual(_configured_run_mode(), "local-prod")

    def test_cli_run_mode_commands_set_environment(self) -> None:
        with patch.dict("os.environ", {}, clear=True) as env:
            with patch("news_pipeline.cli._run_pipeline_command", return_value=0) as run_pipeline:
                self.assertEqual(cli.main(["local-prod", "--dynamic-topics"]), 0)

            run_pipeline.assert_called_once_with()
            self.assertEqual(env["NEWS_RUN_MODE"], "local-prod")
            self.assertEqual(env["NEWS_TOPIC_MODE"], "dynamic")

    def test_cli_source_check_command_delegates_options(self) -> None:
        with patch("news_pipeline.source_checks.main", return_value=0) as source_checks:
            self.assertEqual(cli.main(["check-sources", "--section", "sources"]), 0)

        source_checks.assert_called_once_with(["--section", "sources"])

    def test_story_cap_defaults_follow_run_mode_and_model_profile(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(configured_max_stories_per_topic("dev"), 2)
            self.assertEqual(configured_max_stories_per_topic("local-prod"), 4)
            self.assertEqual(configured_max_stories_per_topic("prod"), 4)
            self.assertEqual(configured_max_articles_per_story("big_conservative"), 4)
            self.assertEqual(configured_max_articles_per_story("small_aggressive"), 5)
            self.assertEqual(configured_story_cluster_similarity_threshold(), 0.22)

        with patch.dict(
            "os.environ",
            {
                "NEWS_MAX_STORIES_PER_TOPIC": "6",
                "NEWS_MAX_ARTICLES_PER_STORY": "7",
                "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD": "0.30",
            },
            clear=True,
        ):
            self.assertEqual(configured_max_stories_per_topic("dev"), 6)
            self.assertEqual(configured_max_articles_per_story("big_conservative"), 7)
            self.assertEqual(configured_story_cluster_similarity_threshold(), 0.31)

    def test_topic_mode_defaults_to_predefined(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(configured_topic_mode(), "predefined")

        with patch.dict("os.environ", {"NEWS_TOPIC_MODE": "dynamic"}, clear=True):
            self.assertEqual(configured_topic_mode(), "dynamic")

    def test_loads_predefined_topics_in_client_order_with_defaults(self) -> None:
        client_path = self._write_yaml(
            {"topic_ids": ["iran_war", "us_economy"]},
            "client.yaml",
        )
        topics_path = self._write_yaml(
            {
                "topics": [
                    {
                        "id": "us_economy",
                        "title": "US Economy",
                        "keywords": ["Fed", "stocks", "stocks"],
                        "boost_phrases": ["jobs report"],
                    },
                    {
                        "id": "iran_war",
                        "title": "Iran War",
                        "keywords": ["Iran", "Israel"],
                        "boost_phrases": ["iran israel war"],
                        "frame_tags": ["US", "non_western"],
                    },
                ]
            },
            "topics.yaml",
        )

        self.assertEqual(load_client_config(client_path)["topic_ids"], ["iran_war", "us_economy"])
        definitions = load_topic_definitions(topics_path)
        self.assertEqual(definitions["us_economy"]["keywords"], ["fed", "stocks"])

        topics = load_predefined_topics(
            client_path=client_path,
            topics_path=topics_path,
            default_max_articles_per_source=5,
        )

        self.assertEqual([topic["key"] for topic in topics], ["iran_war", "us_economy"])
        self.assertEqual(topics[0]["min_score"], 2)
        self.assertEqual(topics[0]["max_articles_per_source"], 5)
        self.assertEqual(topics[0]["frame_tags"], ["us", "non_western"])

    def test_predefined_topic_config_validation_errors(self) -> None:
        duplicate_topics_path = self._write_yaml(
            {
                "topics": [
                    {"id": "topic_a", "title": "Topic A", "keywords": ["alpha"]},
                    {"id": "topic_a", "title": "Topic A Again", "keywords": ["beta"]},
                ]
            },
            "topics.yaml",
        )
        with self.assertRaisesRegex(ValueError, "duplicate topic ids"):
            load_topic_definitions(duplicate_topics_path)

        missing_title_path = self._write_yaml(
            {"topics": [{"id": "topic_a", "keywords": ["alpha"]}]},
            "topics.yaml",
        )
        with self.assertRaisesRegex(ValueError, "must define a title"):
            load_topic_definitions(missing_title_path)

        missing_terms_path = self._write_yaml(
            {"topics": [{"id": "topic_a", "title": "Topic A"}]},
            "topics.yaml",
        )
        with self.assertRaisesRegex(ValueError, "keywords or boost_phrases"):
            load_topic_definitions(missing_terms_path)

        duplicate_client_path = self._write_yaml(
            {"topic_ids": ["topic_a", "topic_a"]},
            "client.yaml",
        )
        with self.assertRaisesRegex(ValueError, "duplicate topic_ids"):
            load_client_config(duplicate_client_path)

        client_path = self._write_yaml({"topic_ids": ["missing"]}, "client.yaml")
        topics_path = self._write_yaml(
            {"topics": [{"id": "topic_a", "title": "Topic A", "keywords": ["alpha"]}]},
            "topics.yaml",
        )
        with self.assertRaisesRegex(ValueError, "unknown topic_id"):
            load_predefined_topics(client_path=client_path, topics_path=topics_path)

    def test_selected_model_overrides_default_model(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEWS_DEFAULT_MODEL": "qwen-9b-dense",
                "NEWS_MODEL": "gemma-26b-moe",
            },
            clear=True,
        ):
            self.assertEqual(_configured_model_reference(), "gemma-26b-moe")

    def test_model_profile_infers_from_alias(self) -> None:
        self.assertEqual(infer_model_profile_key("gemma-26b-moe"), "big_conservative")
        self.assertEqual(infer_model_profile_key("qwen-9b-dense"), "small_aggressive")
        self.assertEqual(infer_model_profile_key("some-org/custom-14b"), "small_aggressive")

    def test_explicit_model_profile_and_overrides_win(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEWS_MODEL_PROFILE": "small_aggressive",
                "NEWS_MODEL_MAX_INPUT_TOKENS": "12345",
                "NEWS_ARTICLE_SUMMARY_CONCURRENCY": "3",
            },
            clear=True,
        ):
            profile = configured_model_profile("gemma-26b-moe")
        self.assertEqual(profile.key, "small_aggressive")
        self.assertEqual(profile.model_max_input_tokens, 12345)
        self.assertEqual(profile.article_summary_concurrency, 3)

    def test_small_qwen_profile_is_memory_bounded_for_local_runs(self) -> None:
        profile = configured_model_profile("qwen-9b-dense")
        self.assertEqual(profile.key, "small_aggressive")
        self.assertEqual(profile.model_max_input_tokens, 12000)
        self.assertEqual(profile.article_summary_concurrency, 2)
        self.assertEqual(profile.article_text_token_limit, 8000)
        self.assertEqual(profile.total_article_summary_cap, 32)
        self.assertEqual(profile.per_topic_article_summary_cap, 8)
        self.assertEqual(profile.article_summary_max_tokens, 1800)
        self.assertEqual(profile.server_decode_concurrency, 2)
        self.assertEqual(profile.server_prompt_concurrency, 2)
        self.assertEqual(profile.server_prompt_cache_bytes, "3GB")

    def test_model_server_command_uses_module_entrypoint(self) -> None:
        profile = configured_model_profile("gemma-26b-moe")
        command = build_model_server_command(
            "mlx-community/gemma-4-26B-A4B-it-heretic-4bit",
            profile,
        )
        self.assertTrue(command.startswith("uv run python -m mlx_lm server "))
        self.assertIn("--model mlx-community/gemma-4-26B-A4B-it-heretic-4bit", command)
        self.assertIn("--prompt-cache-bytes 512MB", command)

    def test_qwen_profiles_use_hf_card_sampling_presets(self) -> None:
        profile = configured_model_profile("qwen-9b-dense")
        self.assertEqual(profile.default_sampling.temperature, 0.7)
        self.assertEqual(profile.default_sampling.top_p, 0.8)
        self.assertEqual(profile.default_sampling.top_k, 20)
        self.assertEqual(profile.default_sampling.min_p, 0.0)
        self.assertEqual(profile.default_sampling.presence_penalty, 1.5)
        self.assertEqual(profile.default_sampling.repetition_penalty, 1.0)
        self.assertEqual(profile.reasoning_sampling.temperature, 1.0)
        self.assertEqual(profile.reasoning_sampling.top_p, 1.0)
        self.assertEqual(profile.reasoning_sampling.top_k, 40)
        self.assertEqual(profile.reasoning_sampling.presence_penalty, 2.0)
        self.assertEqual(profile.task_sampling["translation"], profile.default_sampling)
        self.assertEqual(profile.task_sampling["article_summary"], profile.default_sampling)
        self.assertEqual(profile.task_sampling["title_generation"], profile.default_sampling)
        self.assertEqual(profile.task_sampling["topic_clustering"], profile.reasoning_sampling)
        self.assertLess(profile.task_sampling["final_synthesis"].temperature, profile.default_sampling.temperature)
        self.assertLess(
            profile.task_sampling["final_synthesis"].presence_penalty,
            profile.reasoning_sampling.presence_penalty,
        )

    def test_gemma_profile_uses_task_specific_sampling(self) -> None:
        profile = configured_model_profile("gemma-26b-moe")
        self.assertLess(
            profile.task_sampling["translation"].temperature,
            profile.task_sampling["article_summary"].temperature,
        )
        self.assertLess(
            profile.task_sampling["article_summary"].temperature,
            profile.task_sampling["final_synthesis"].temperature,
        )
        self.assertGreater(
            profile.task_sampling["title_generation"].temperature,
            profile.task_sampling["article_summary"].temperature,
        )
        self.assertGreater(
            profile.task_sampling["topic_clustering"].top_k,
            profile.task_sampling["translation"].top_k,
        )

    def test_sampling_overrides_win(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEWS_MODEL_TEMPERATURE": "0.33",
                "NEWS_MODEL_TOP_K": "17",
                "NEWS_MODEL_REASONING_TEMPERATURE": "0.66",
                "NEWS_MODEL_REASONING_PRESENCE_PENALTY": "1.25",
                "NEWS_MODEL_FINAL_SYNTHESIS_PRESENCE_PENALTY": "0.44",
                "NEWS_MODEL_TITLE_GENERATION_TEMPERATURE": "0.77",
            },
            clear=True,
        ):
            profile = configured_model_profile("qwen-9b-dense")
        self.assertEqual(profile.default_sampling.temperature, 0.33)
        self.assertEqual(profile.default_sampling.top_k, 17)
        self.assertEqual(profile.reasoning_sampling.temperature, 0.66)
        self.assertEqual(profile.reasoning_sampling.presence_penalty, 1.25)
        self.assertEqual(profile.task_sampling["translation"].temperature, 0.33)
        self.assertEqual(profile.task_sampling["topic_clustering"].temperature, 0.66)
        self.assertEqual(profile.task_sampling["final_synthesis"].presence_penalty, 0.44)
        self.assertEqual(profile.task_sampling["title_generation"].temperature, 0.77)

    def test_image_prompt_guardrails_keep_overlay_text_separate(self) -> None:
        prompt = _enforce_text_free_image_prompt("A realistic photo of a power substation.")
        self.assertIn("no text", prompt.lower())
        self.assertIn("readable headline will be rendered later by code", prompt)

    def test_overlay_headline_is_short_single_line_text(self) -> None:
        headline = _sanitize_overlay_headline(
            "Headline: **A very long report title about grids and demand and utility bills and permits**\nExtra",
            "Daily News Brief",
        )
        self.assertNotIn("\n", headline)
        self.assertLessEqual(len(headline.split()), 11)
        self.assertNotIn("Headline:", headline)

    def test_plain_report_includes_generated_image_metadata(self) -> None:
        report_body = build_report_body(
            "Daily News",
            "## Power\nGrid upgrades are moving faster.",
            [],
            [],
            {
                "final_image_path": "output/report_image.png",
                "overlay_headline": "Grid Politics Hit Home",
                "prompt_path": "output/report_image_prompt.txt",
            },
        )
        self.assertIn("Generated image: output/report_image.png", report_body)
        self.assertIn("Overlay headline: Grid Politics Hit Home", report_body)

    def test_references_use_synthesis_included_story_reports_only(self) -> None:
        included = (
            "### Barakah nuclear plant fire\n"
            "Metadata:\n"
            "- Source: AP\n"
            "- Published: Sun, 17 May 2026 20:23:00 GMT\n"
            "- URL: https://example.com/uae\n"
            "- Topic: Iran War\n"
            "- Story: UAE nuclear plant strike\n\n"
            "Summary:\n"
            "Officials reported a fire at the Barakah nuclear plant."
        )
        omitted = (
            "### Felix Rosenqvist posts fastest qualifying average\n"
            "Metadata:\n"
            "- Source: AP\n"
            "- Published: Sun, 17 May 2026 21:10:00 GMT\n"
            "- URL: https://example.com/felix\n"
            "- Topic: US Economy\n"
            "- Story: Indianapolis qualifying\n\n"
            "Summary:\n"
            "Rosenqvist posted the fastest qualifying average."
        )
        reference_reports = filter_reports_for_references(
            [included, omitted],
            {"included_report_keys": [_report_reference_key(included)]},
        )
        report_body = build_report_body(
            "Daily News",
            "## IRAN WAR\nOfficials reported a nuclear-plant fire.",
            reference_reports,
            [{"key": "iran_war", "title": "Iran War"}],
            None,
        )

        self.assertIn("[UAE nuclear plant strike] Barakah nuclear plant fire", report_body)
        self.assertNotIn("Felix Rosenqvist", report_body)

    def test_activity_snapshot_records_available_memory_signals(self) -> None:
        with patch(
            "news_pipeline.pipeline._run_activity_command",
            side_effect=[
                {"ok": True, "parsed": {"memory_free_pct": 72}, "output_tail": ""},
                {"ok": True, "parsed": {"swapouts": 123, "swapins": 45}, "output_tail": ""},
            ],
        ):
            snapshot = capture_activity_snapshot("after_model_server_ready")

        self.assertEqual(snapshot["label"], "after_model_server_ready")
        self.assertEqual(snapshot["memory_free_pct"], 72)
        self.assertEqual(snapshot["swapouts"], 123)

    def test_model_artifact_stripper_removes_leaked_section_tags(self) -> None:
        cleaned = strip_model_artifacts("<analysis>\n## Topic\n<content>Copy</content>\n</analysis>")

        self.assertEqual(cleaned, "## Topic\nCopy")

    def test_default_synthesis_prompt_does_not_request_htmlish_tags(self) -> None:
        prompt = get_default_final_synthesis_instructions(
            [{"key": "topic_a", "title": "Topic A"}]
        )

        self.assertNotIn("<content>", prompt)
        self.assertNotIn("No high-confidence updates in supplied coverage", prompt)

    def test_publication_cleaner_drops_empty_dataset_sections(self) -> None:
        cleaned = clean_synthesis_for_publication(
            "## GOOD TOPIC\nReported facts go here.\n\n"
            "## EMPTY TOPIC\n"
            "No high-confidence updates in supplied coverage.\n\n"
            "The primary dataset is literally empty for this entire section."
        )

        self.assertIn("## GOOD TOPIC", cleaned)
        self.assertNotIn("EMPTY TOPIC", cleaned)
        self.assertNotIn("No high-confidence", cleaned)

    def test_relaxed_publication_cleaner_keeps_dev_low_coverage_sections(self) -> None:
        cleaned = clean_synthesis_for_publication(
            "## EMPTY TOPIC\nNo high-confidence updates in supplied coverage.",
            relaxed=True,
        )

        self.assertIn("## EMPTY TOPIC", cleaned)
        self.assertIn("No high-confidence", cleaned)

    def test_dev_final_synthesis_preview_groups_article_summaries_by_topic(self) -> None:
        preview = build_dev_final_synthesis_preview(
            [
                "### Article A\nMetadata:\n- Topic: Topic A\n\nSummary:\nFirst reported sentence. Second reported sentence. Third extra sentence.",
                "### Article B\nMetadata:\n- Topic: Topic B\n\nSummary:\nDifferent reported sentence. Another supported sentence.",
            ],
            [{"key": "topic_a", "title": "Topic A"}, {"key": "topic_b", "title": "Topic B"}],
        )

        self.assertIn("## TOPIC A", preview)
        self.assertIn("First reported sentence. Second reported sentence.", preview)
        self.assertIn("## TOPIC B", preview)
        self.assertIn("Different reported sentence. Another supported sentence.", preview)

    def test_loads_top_funnel_provider_metadata_and_stage_flags(self) -> None:
        path = self._write_config(
            {
                "top_funnel_providers": [
                    {
                        "key": "reddit_news",
                        "name": "Reddit r/news",
                        "url": "https://example.com/reddit.json",
                        "fetcher": "reddit_top_json",
                        "region": "us",
                        "frame": "us/western",
                        "provider_type": "social_aggregation",
                        "intended_role": "seed public-interest topics",
                        "weight": 1.25,
                        "can_seed_topics": True,
                        "can_validate_topics": False,
                        "can_enrich_coverage": False,
                    },
                    {
                        "key": "wire",
                        "name": "Wire",
                        "url": "https://example.com/rss",
                        "can_seed_topics": False,
                        "can_validate_topics": True,
                    },
                ],
                "sources": [
                    {
                        "key": "Example",
                        "name": "Example Feed",
                        "url": "https://example.com/feed.xml",
                    }
                ],
            }
        )

        providers = load_top_funnel_providers(path)
        self.assertEqual(set(providers), {"reddit_news", "wire"})
        self.assertTrue(providers["reddit_news"]["can_seed_topics"])
        self.assertFalse(providers["reddit_news"]["can_validate_topics"])
        self.assertEqual(providers["reddit_news"]["frame"], "us/western")
        self.assertEqual(providers["reddit_news"]["provider_type"], "social_aggregation")
        self.assertEqual(providers["reddit_news"]["intended_role"], "seed public-interest topics")
        self.assertEqual(providers["reddit_news"]["weight"], 1.25)
        self.assertFalse(providers["wire"]["can_seed_topics"])
        self.assertTrue(providers["wire"]["can_validate_topics"])

    def test_article_sources_default_to_enrichment_only(self) -> None:
        path = self._write_config(
            {
                "top_funnel_providers": [
                    {"key": "top", "url": "https://example.com/rss", "can_seed_topics": True}
                ],
                "sources": [
                    {
                        "key": "Example",
                        "name": "Example Feed",
                        "url": "https://example.com/feed.xml",
                        "region": "global",
                    }
                ],
            }
        )

        sources = load_sources(path)
        self.assertFalse(sources["Example"]["can_seed_topics"])
        self.assertFalse(sources["Example"]["can_validate_topics"])
        self.assertTrue(sources["Example"]["can_enrich_coverage"])
        self.assertEqual(sources["Example"]["provider_type"], "article_feed")

    def test_annotation_records_seed_validation_and_frame_metadata(self) -> None:
        topics = [
            {
                "key": "topic_01",
                "title": "US Congress budget fight",
                "rationale": "A federal funding deadline is approaching.",
                "keywords": ["congress", "budget", "funding"],
                "boost_phrases": ["congress budget fight"],
            }
        ]
        seed_stories = [
            {
                "title": "Congress budget fight intensifies",
                "description": "",
                "providers": ["google_news_top"],
                "provider_details": [
                    {"key": "google_news_top", "frame": "us/western", "weight": 1.0}
                ],
            }
        ]
        validation_stories = [
            {
                "title": "AP: Congress budget fight nears deadline",
                "description": "",
                "providers": ["ap_top"],
                "provider_details": [{"key": "ap_top", "frame": "us/western", "weight": 0.85}],
            }
        ]

        annotated = annotate_topic_discovery_signals(
            topics,
            seed_stories=seed_stories,
            validation_stories=validation_stories,
        )
        self.assertEqual(annotated[0]["seed_providers"], ["google_news_top"])
        self.assertEqual(annotated[0]["validation_providers"], ["ap_top"])
        self.assertIn("us", annotated[0]["frame_tags"])
        self.assertIn("western", annotated[0]["frame_tags"])
        self.assertGreater(annotated[0]["selection_validation_score"], 0)

    def test_annotation_uses_lenient_overlap_for_seed_headlines(self) -> None:
        topics = [
            {
                "key": "topic_hamas",
                "title": "Hamas representative listed by UK police as group member",
                "rationale": "",
                "keywords": ["hamas representative", "hamas lawyer", "hamas uk listing"],
                "boost_phrases": ["hamas representative listed by uk police"],
            }
        ]
        seed_stories = [
            {
                "title": "Lawyer who represented Hamas in court says UK police falsely listed him as member of group",
                "description": "",
                "providers": ["reddit_news"],
                "provider_details": [{"key": "reddit_news", "frame": "us/western", "weight": 1.0}],
            }
        ]

        annotated = annotate_topic_discovery_signals(
            topics,
            seed_stories=seed_stories,
            validation_stories=[],
        )

        self.assertEqual(annotated[0]["seed_providers"], ["reddit_news"])

    def test_relevance_scoring_does_not_match_keyword_substrings(self) -> None:
        topic = {
            "key": "topic_frontier",
            "title": "Frontier Airlines jet hits person on runway in Denver",
            "rationale": "Auto-generated fallback topic from a top-of-day seed headline.",
            "keywords": ["frontier", "jet", "hits", "runway", "denver", "source", "tells"],
            "boost_phrases": ["frontier airlines jet hits person on runway in denver"],
            "min_score": 4,
            "topic_source": "fallback_seed_headline",
        }

        self.assertEqual(
            _score_topic_relevance(
                {
                    "title": "Jets sign running back after AP source says deal is done",
                    "description": "",
                },
                topic,
            ),
            0,
        )
        self.assertEqual(
            _score_topic_against_story(
                topic,
                {
                    "title": "Trump plans to fire US FDA chief Makary, sources say",
                    "description": "",
                    "domain": "reuters",
                },
            ),
            0,
        )
        self.assertGreaterEqual(
            _score_topic_relevance(
                {
                    "title": "Frontier plane kills runway trespasser at Denver airport",
                    "description": "",
                },
                topic,
            ),
            4,
        )
        self.assertGreaterEqual(
            _score_topic_relevance(
                {
                    "title": "Man dies after being hit by Frontier plane that was taking off",
                    "description": "",
                },
                topic,
            ),
            4,
        )

    def test_initial_predefined_topics_match_relevant_headlines(self) -> None:
        topics = {
            topic["key"]: topic
            for topic in load_predefined_topics(default_max_articles_per_source=6)
        }

        self.assertGreaterEqual(
            _score_topic_relevance(
                {
                    "title": "Israel says Iran fired missiles after US strikes near Fordow",
                    "description": "The Pentagon said air defenses tracked drones over the Persian Gulf.",
                },
                topics["iran_war"],
            ),
            2,
        )
        self.assertGreaterEqual(
            _score_topic_relevance(
                {
                    "title": "Fed decision lifts stocks as jobs report cools rate fears",
                    "description": "Treasury yields fell after the latest payrolls data.",
                },
                topics["us_economy"],
            ),
            2,
        )
        self.assertEqual(
            _score_topic_relevance(
                {
                    "title": "Federation chess tournament opens downtown",
                    "description": "A goldfinch mural was unveiled near the venue.",
                },
                topics["us_economy"],
            ),
            0,
        )
        self.assertEqual(
            _score_topic_relevance(
                {
                    "title": "Frontier Airlines adds flights after Denver schedule change",
                    "description": "The carrier announced new service on Monday.",
                },
                topics["iran_war"],
            ),
            0,
        )

    def test_predefined_topic_matching_rejects_weak_single_token_hits(self) -> None:
        topics = {
            topic["key"]: topic
            for topic in load_predefined_topics(default_max_articles_per_source=6)
        }

        weak_examples = [
            (
                "us_economy",
                "Felix Rosenqvist posts fastest 4-lap average in 1st round of Indianapolis 500 qualifying",
            ),
            (
                "us_economy",
                "Gold jewellery, cash stolen from houses in Ranipet district",
            ),
            (
                "iran_war",
                "Samsung Electronics, labor union set to resume talks ahead of planned strike",
            ),
            (
                "iran_war",
                "Mali Military Drone Strikes Kill At Least 10 Civilians",
            ),
        ]

        for topic_key, title in weak_examples:
            with self.subTest(topic_key=topic_key, title=title):
                self.assertEqual(
                    _score_topic_relevance(
                        {
                            "title": title,
                            "description": "",
                        },
                        topics[topic_key],
                    ),
                    0,
                )

    def test_feed_selection_uses_scan_depth_before_final_source_budget(self) -> None:
        now_utc = datetime(2026, 5, 17, 18, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        topic = {
            "key": "us_economy",
            "title": "US Economy",
            "keywords": ["fed", "inflation", "payrolls"],
            "boost_phrases": ["federal reserve decision", "inflation report"],
            "min_score": 2,
            "max_articles_per_source": 3,
        }
        items = [
            {
                "title": "Fed decision lifts stocks after policy meeting",
                "description": "Federal Reserve officials held rates steady.",
                "link": "https://example.com/fed",
                "published_at": now_utc,
            },
            {
                "title": "Inflation report shows consumer prices cooling",
                "description": "The latest inflation data shaped market expectations.",
                "link": "https://example.com/inflation",
                "published_at": now_utc,
            },
            {
                "title": "Payrolls rise as employers add jobs",
                "description": "The labor department reported stronger payrolls.",
                "link": "https://example.com/payrolls",
                "published_at": now_utc,
            },
        ]

        with patch("news_pipeline.pipeline.PER_SOURCE_TOPIC_ARTICLE_CAP", 1):
            selected = _select_per_topic_feed_items(items, [topic], now_utc=now_utc)

        self.assertEqual([item["link"] for item in selected], [item["link"] for item in items])

    def test_predefined_topic_selection_uses_configured_topics_without_discovery(self) -> None:
        configured_topics = [
            {
                "id": "iran_war",
                "key": "iran_war",
                "title": "Iran War",
                "rationale": "Configured",
                "keywords": ["iran"],
                "boost_phrases": ["iran israel war"],
                "min_score": 2,
                "max_articles_per_source": 6,
                "frame_tags": ["us"],
            },
            {
                "id": "us_economy",
                "key": "us_economy",
                "title": "US Economy",
                "rationale": "Configured",
                "keywords": ["fed"],
                "boost_phrases": ["jobs report"],
                "min_score": 2,
                "max_articles_per_source": 6,
                "frame_tags": ["us"],
            },
        ]

        with patch("news_pipeline.pipeline.load_predefined_topics", return_value=configured_topics):
            topics = load_predefined_topics_for_run(topic_limit=2)

        self.assertEqual([topic["key"] for topic in topics], ["iran_war", "us_economy"])
        self.assertEqual([topic["selection_rank"] for topic in topics], [1, 2])
        self.assertTrue(all(topic["topic_source"] == "predefined_config" for topic in topics))

    def test_topic_mode_branch_skips_or_preserves_dynamic_discovery(self) -> None:
        configured_topics = [
            {
                "id": "iran_war",
                "key": "iran_war",
                "title": "Iran War",
                "keywords": ["iran"],
                "boost_phrases": ["iran israel war"],
                "min_score": 2,
                "max_articles_per_source": 6,
            }
        ]
        with patch("news_pipeline.pipeline.TOPIC_MODE", "predefined"):
            with patch("news_pipeline.pipeline.DEV", True):
                with patch("news_pipeline.pipeline.load_predefined_topics", return_value=configured_topics):
                    with patch("news_pipeline.pipeline.discover_top_stories_of_day") as discover:
                        with patch("news_pipeline.pipeline.llm_cluster_top_topics") as cluster:
                            with redirect_stdout(StringIO()):
                                topics, top_stories = select_topics_for_run(1)

        self.assertEqual([topic["key"] for topic in topics], ["iran_war"])
        self.assertEqual(top_stories, [])
        discover.assert_not_called()
        cluster.assert_not_called()

        dynamic_story = {
            "title": "Congress budget fight intensifies",
            "description": "",
            "providers": ["google_news_top"],
            "provider_details": [{"key": "google_news_top", "frame": "us/western", "weight": 1.0}],
            "url": "https://example.com/budget",
        }
        dynamic_topic = {
            "key": "topic_budget",
            "title": "US Congress budget fight",
            "rationale": "A funding deadline is approaching.",
            "keywords": ["congress", "budget", "funding"],
            "boost_phrases": ["congress budget fight"],
            "min_score": 2,
        }
        top_funnel = {
            "all_stories": [dynamic_story],
            "seed_stories": [dynamic_story],
            "validation_stories": [dynamic_story],
        }
        with patch("news_pipeline.pipeline.TOPIC_MODE", "dynamic"):
            with patch("news_pipeline.pipeline.discover_top_stories_of_day", return_value=top_funnel) as discover:
                with patch("news_pipeline.pipeline.llm_cluster_top_topics", return_value=[dynamic_topic]) as cluster:
                    with redirect_stdout(StringIO()):
                        topics, top_stories = select_topics_for_run(1)

        self.assertEqual([topic["title"] for topic in topics], ["US Congress budget fight"])
        self.assertEqual(top_stories, [dynamic_story])
        discover.assert_called_once()
        cluster.assert_called_once()

    def test_supported_fallback_clusters_require_shared_provider_evidence(self) -> None:
        topics = _cluster_supported_fallback_topics(
            [
                {
                    "title": "Frontier Airlines jet bound for LAX hits person on runway in Denver, aviation source tells ABC News",
                    "provider": "reddit_news",
                },
                {
                    "title": "'We just hit somebody' - Frontier Airlines plane kills runway trespasser at Denver airport - BBC",
                    "provider": "google_news_top",
                },
                {
                    "title": "Trump plans to fire US FDA chief Makary, sources say By Reuters",
                    "provider": "reuters_top",
                },
            ],
            2,
        )

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic_source"], "fallback_cross_provider")
        self.assertEqual(topics[0]["min_score"], 4)
        self.assertIn("frontier", topics[0]["keywords"])
        self.assertNotIn("source", topics[0]["keywords"])
        self.assertNotIn("tells", topics[0]["keywords"])

    def test_single_provider_fallback_topics_are_not_selected(self) -> None:
        annotated_candidates = [
            {
                "key": "topic_babies",
                "title": "Babies are bleeding to death as parents reject a vitamin shot",
                "rationale": "Auto-generated fallback topic from a top-of-day seed headline.",
                "keywords": ["babies", "bleeding", "vitamin", "shot"],
                "boost_phrases": ["babies reject vitamin shot"],
                "topic_source": "fallback_seed_headline",
                "seed_providers": ["reddit_news"],
                "validation_providers": [],
                "seed_matches": [{"url": "https://example.com/babies"}],
                "selection_base_score": 1.0,
            },
            {
                "key": "topic_frontier",
                "title": "Frontier Airlines plane strikes runway trespasser",
                "rationale": "Auto-generated fallback topic from a top-of-day seed headline.",
                "keywords": ["frontier", "airlines", "plane", "runway"],
                "boost_phrases": ["frontier airlines plane runway"],
                "topic_source": "fallback_cross_provider",
                "seed_providers": ["reddit_news"],
                "validation_providers": ["google_news_top"],
                "seed_matches": [{"url": "https://example.com/frontier"}],
                "selection_base_score": 0.9,
            },
        ]

        prepared = prepare_candidate_topics_for_selection(
            annotated_candidates,
            seed_stories=[],
            validation_stories=[],
            target_count=1,
        )

        self.assertEqual([topic["title"] for topic in prepared], ["Frontier Airlines plane strikes runway trespasser"])

    def test_unsupported_llm_topics_are_dropped_before_selection(self) -> None:
        annotated_candidates = [
            {
                "key": "topic_clinton",
                "title": "Clinton wins Alabama caucuses in strong showing",
                "keywords": ["hillary clinton", "alabama caucuses"],
                "boost_phrases": ["clinton wins alabama caucuses"],
                "seed_providers": [],
                "validation_providers": [],
                "seed_matches": [],
                "selection_base_score": 1.0,
            },
            {
                "key": "topic_budget",
                "title": "US Congress budget fight",
                "keywords": ["congress", "budget", "funding"],
                "boost_phrases": ["congress budget fight"],
                "seed_providers": ["google_news_top"],
                "validation_providers": [],
                "seed_matches": [{"url": "https://example.com/budget"}],
                "selection_base_score": 0.9,
            },
            {
                "key": "topic_budget_duplicate",
                "title": "Congress budget fight intensifies",
                "keywords": ["congress", "budget", "funding"],
                "boost_phrases": ["congress budget fight"],
                "seed_providers": ["reddit_news"],
                "validation_providers": [],
                "seed_matches": [{"url": "https://example.com/budget"}],
                "selection_base_score": 0.8,
            },
        ]
        seed_stories = [
            {
                "title": "Congress budget fight intensifies",
                "description": "",
                "providers": ["google_news_top"],
                "provider_details": [{"key": "google_news_top", "frame": "us/western"}],
                "url": "https://example.com/budget",
            },
            {
                "title": "Storm damage closes schools across Texas",
                "description": "",
                "providers": ["bbc_world_top"],
                "provider_details": [{"key": "bbc_world_top", "frame": "global/western"}],
                "url": "https://example.com/storm",
            },
        ]
        validation_stories = [
            {
                "title": "Storm damage closes schools across Texas after overnight flooding",
                "description": "",
                "providers": ["ap_top"],
                "provider_details": [{"key": "ap_top", "frame": "us/western"}],
                "url": "https://example.com/storm-ap",
            },
        ]

        prepared = prepare_candidate_topics_for_selection(
            annotated_candidates,
            seed_stories=seed_stories,
            validation_stories=validation_stories,
            target_count=2,
        )
        titles = [topic["title"] for topic in prepared]

        self.assertNotIn("Clinton wins Alabama caucuses in strong showing", titles)
        self.assertEqual(titles.count("US Congress budget fight"), 1)
        self.assertIn("Storm damage closes schools across Texas", titles)

    def test_top_funnel_articles_fill_selected_topic_coverage_gaps(self) -> None:
        topics = [
            {
                "key": "topic_hanta",
                "title": "Hantavirus outbreak on cruise ship",
                "rationale": "WHO reports possible spread on a cruise ship.",
                "keywords": ["hantavirus", "cruise ship", "outbreak"],
                "boost_phrases": ["hantavirus cruise ship transmission"],
                "min_score": 3,
            }
        ]
        stories = [
            {
                "title": "Hantavirus may have spread between passengers on cruise ship, WHO says",
                "url": "https://example.com/hanta",
                "description": "Health officials are investigating possible transmission.",
                "providers": ["bbc_world_top"],
                "provider_details": [{"key": "bbc_world_top", "name": "BBC World News"}],
            }
        ]

        with patch("news_pipeline.pipeline._resolve_google_news_url", side_effect=lambda url: url):
            with patch("news_pipeline.pipeline.web_scrape", return_value="Reported body text."):
                with patch("news_pipeline.pipeline._translate_if_needed", side_effect=lambda text, title="": text):
                    targets, urls, stats = build_top_funnel_article_targets_for_coverage_gaps(
                        topics,
                        stories,
                        [],
                        set(),
                        set(),
                    )

        self.assertEqual(urls, ["https://example.com/hanta"])
        self.assertEqual(stats["filled_topics"], {"Hantavirus outbreak on cruise ship": 1})
        self.assertEqual(targets[0]["topic_key"], "topic_hanta")
        self.assertEqual(targets[0]["source"], "BBC World News")

    def test_soft_selection_is_not_a_hard_non_western_quota(self) -> None:
        candidates = [
            {
                "key": f"western_{index}",
                "title": f"Western topic {index}",
                "keywords": ["topic"],
                "boost_phrases": [],
                "frame_tags": ["western"],
                "selection_base_score": 1.0 + index,
                "selection_validation_score": 0,
            }
            for index in range(4)
        ]

        selected = select_topics_soft_weighted(candidates, 4, seed="all-western")
        self.assertEqual(len(selected), 4)
        self.assertTrue(all("western" in topic["frame_tags"] for topic in selected))

    def test_article_budget_respects_caps_and_preserves_topics(self) -> None:
        topics = [
            {"key": "topic_a", "title": "Topic A"},
            {"key": "topic_b", "title": "Topic B"},
        ]
        articles = [
            {
                "article_id": "a1",
                "source": "Reuters",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "relevance_score": 10,
                "pub_date": "Sat, 02 May 2026 10:00:00 GMT",
            },
            {
                "article_id": "a2",
                "source": "AP",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "relevance_score": 9,
                "pub_date": "Sat, 02 May 2026 11:00:00 GMT",
            },
            {
                "article_id": "b1",
                "source": "BBC",
                "topic_key": "topic_b",
                "topic_title": "Topic B",
                "relevance_score": 5,
                "pub_date": "Sat, 02 May 2026 09:00:00 GMT",
            },
            {
                "article_id": "b2",
                "source": "Al Jazeera",
                "topic_key": "topic_b",
                "topic_title": "Topic B",
                "relevance_score": 4,
                "pub_date": "Sat, 02 May 2026 08:00:00 GMT",
            },
        ]

        selected, stats = budget_article_targets(
            articles,
            topics,
            total_cap=3,
            per_topic_cap=2,
        )

        self.assertEqual(len(selected), 3)
        self.assertIn("a1", [article["article_id"] for article in selected])
        self.assertIn("b1", [article["article_id"] for article in selected])
        self.assertEqual(stats["included_by_topic"]["Topic A"], 2)
        self.assertEqual(stats["included_by_topic"]["Topic B"], 1)

    def test_article_budget_limits_one_source_per_topic(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        articles = [
            {
                "article_id": "reuters-1",
                "source": "Reuters",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "relevance_score": 10,
                "pub_date": "Sat, 02 May 2026 10:00:00 GMT",
            },
            {
                "article_id": "reuters-2",
                "source": "Reuters",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "relevance_score": 9,
                "pub_date": "Sat, 02 May 2026 11:00:00 GMT",
            },
            {
                "article_id": "ap-1",
                "source": "AP",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "relevance_score": 8,
                "pub_date": "Sat, 02 May 2026 09:00:00 GMT",
            },
        ]

        selected, stats = budget_article_targets(
            articles,
            topics,
            total_cap=3,
            per_topic_cap=3,
            per_source_topic_cap=1,
        )

        self.assertEqual([article["article_id"] for article in selected], ["reuters-1", "ap-1"])
        self.assertEqual(stats["per_source_topic_cap"], 1)
        self.assertEqual(stats["included_by_source_topic"]["Topic A | Reuters"], 1)
        self.assertEqual(stats["included_by_source_topic"]["Topic A | AP"], 1)

    def test_story_aware_budget_never_selects_single_article_story(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        articles = [
            {
                "article_id": "s1-a1",
                "source": "Reuters",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "story_key": "story_1",
                "story_title": "Story One",
                "story_article_count": 2,
                "story_average_similarity": 0.5,
                "relevance_score": 10,
                "pub_date": "Sat, 02 May 2026 10:00:00 GMT",
            },
            {
                "article_id": "s1-a2",
                "source": "AP",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "story_key": "story_1",
                "story_title": "Story One",
                "story_article_count": 2,
                "story_average_similarity": 0.5,
                "relevance_score": 9,
                "pub_date": "Sat, 02 May 2026 09:00:00 GMT",
            },
            {
                "article_id": "s2-a1",
                "source": "BBC",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "story_key": "story_2",
                "story_title": "Story Two",
                "story_article_count": 2,
                "story_average_similarity": 0.4,
                "relevance_score": 8,
                "pub_date": "Sat, 02 May 2026 08:00:00 GMT",
            },
            {
                "article_id": "s2-a2",
                "source": "Al Jazeera",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "story_key": "story_2",
                "story_title": "Story Two",
                "story_article_count": 2,
                "story_average_similarity": 0.4,
                "relevance_score": 7,
                "pub_date": "Sat, 02 May 2026 07:00:00 GMT",
            },
        ]

        selected, stats = budget_article_targets(
            articles,
            topics,
            total_cap=3,
            per_topic_cap=3,
            per_source_topic_cap=1,
        )

        self.assertEqual([article["article_id"] for article in selected], ["s1-a1", "s1-a2"])
        self.assertTrue(stats["story_aware"])
        self.assertEqual(stats["selected_story_count"], 1)

    def test_final_synthesis_payload_trims_to_model_input_cap(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        reports = [
            (
                f"### Article {index}\n"
                "Metadata:\n"
                "- Source: Source\n"
                "- Published: Sat, 02 May 2026 10:00:00 GMT\n"
                "- URL: https://example.com\n"
                "- Topic: Topic A\n\n"
                "Summary:\n"
                + ("word " * 180)
            )
            for index in range(10)
        ]
        with patch("news_pipeline.pipeline.MODEL_MAX_INPUT_TOKENS", 900):
            messages, stats = build_final_synthesis_payload(reports, "May 02, 2026", topics)
        estimated_tokens = sum(estimate_message_token_count(message) for message in messages)

        self.assertLessEqual(estimated_tokens, 900)
        self.assertTrue(stats["input_budget_satisfied"])
        self.assertGreater(stats["reports_omitted_from_synthesis"], 0)

    def test_final_synthesis_payload_groups_source_summaries_by_story(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        reports = [
            (
                "### Article One\n"
                "Metadata:\n"
                "- Source: Source\n"
                "- Published: Sat, 02 May 2026 10:00:00 GMT\n"
                "- URL: https://example.com/1\n"
                "- Topic: Topic A\n\n"
                "Summary:\n"
                "Officials said the first reported development happened Monday."
            ),
            (
                "### Article Two\n"
                "Metadata:\n"
                "- Source: Source\n"
                "- Published: Sat, 02 May 2026 11:00:00 GMT\n"
                "- URL: https://example.com/2\n"
                "- Topic: Topic A\n\n"
                "Summary:\n"
                "Officials said the second reported development happened Tuesday."
            ),
        ]

        messages, _ = build_final_synthesis_payload(reports, "May 02, 2026", topics)
        payload = messages[0].content

        self.assertIn("Story: Topic A", payload)
        self.assertIn("1. Officials said the first reported development", payload)
        self.assertIn("2. Officials said the second reported development", payload)
        self.assertNotIn("[Topic:", payload)
        self.assertNotIn("<topic>", payload)

    def test_story_clustering_uses_full_text_similarity_for_three_article_story(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A", "keywords": ["topic"], "boost_phrases": []}]
        articles = [
            {
                "article_id": "a1",
                "source": "Reuters",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Ministry confirms Novorossiysk refinery drone strike",
                "description": "Officials said drones hit the Novorossiysk refinery overnight.",
                "text": (
                    "A drone strike hit the Novorossiysk refinery overnight, igniting fuel tanks. "
                    "Regional officials said firefighters contained the refinery blaze and assessed damage."
                ),
            },
            {
                "article_id": "a2",
                "source": "AP",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Governor says drone attack damages Novorossiysk refinery",
                "description": "The governor said the same refinery was damaged by drones.",
                "text": (
                    "The Novorossiysk refinery was damaged after drones struck fuel storage tanks overnight. "
                    "Firefighters worked at the refinery while authorities reported no casualties."
                ),
            },
            {
                "article_id": "a3",
                "source": "BBC",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Crews extinguish fire after refinery drone attack",
                "description": "Emergency crews extinguished a fire from the refinery drone strike.",
                "text": (
                    "Emergency crews extinguished a fire at the Novorossiysk refinery after an overnight "
                    "drone attack on fuel tanks, local authorities said."
                ),
            },
            {
                "article_id": "a4",
                "source": "France 24",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Court schedules opposition leader appeal",
                "description": "Judges set a date for a separate political case.",
                "text": "Judges scheduled an appeal hearing in a separate opposition leader case.",
            },
        ]

        selected, stats = organize_article_targets_into_stories(
            articles,
            topics,
            min_articles_per_story=2,
            max_stories_per_topic=4,
            max_articles_per_story=5,
            similarity_threshold=0.18,
        )

        self.assertEqual([article["article_id"] for article in selected], ["a1", "a2", "a3"])
        self.assertEqual(len({article["story_key"] for article in selected}), 1)
        self.assertEqual({article["story_article_count"] for article in selected}, {3})
        self.assertEqual(stats["story_count"], 1)
        self.assertEqual(stats["dropped_count"], 1)

    def test_story_clustering_rejects_weak_loose_chain(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A", "keywords": [], "boost_phrases": []}]
        articles = [
            {
                "article_id": "a1",
                "source": "One",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Alpha plant outage",
                "description": "Alpha corridor outage affects one plant.",
                "text": "alpha alpha plant outage corridor bridge",
            },
            {
                "article_id": "a2",
                "source": "Two",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Bridge corridor tariff review",
                "description": "Bridge corridor tariff review begins.",
                "text": "bridge bridge corridor corridor tariff tariff review",
            },
            {
                "article_id": "a3",
                "source": "Three",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": "Tariff ministry filing",
                "description": "Tariff ministry filing lands in court.",
                "text": "tariff tariff ministry filing court docket",
            },
        ]

        selected, stats = organize_article_targets_into_stories(
            articles,
            topics,
            min_articles_per_story=2,
            max_stories_per_topic=4,
            max_articles_per_story=5,
            similarity_threshold=0.20,
        )

        self.assertEqual(selected, [])
        self.assertEqual(stats["story_count"], 0)
        self.assertEqual(stats["dropped_count"], 3)

    def test_story_clustering_does_not_link_dayone_and_felix(self) -> None:
        topics = [
            {
                "key": "us_economy",
                "title": "US Economy",
                "rationale": "Macro, markets, housing, rates, commodities, currency, and major business news affecting the US economy.",
                "keywords": ["ipo", "stocks", "stock market"],
                "boost_phrases": ["stock market today"],
            }
        ]
        articles = [
            {
                "article_id": "dayone",
                "source": "Reuters",
                "topic_key": "us_economy",
                "topic_title": "US Economy",
                "title": "Data center operator DayOne considering dual IPO in Singapore and US",
                "description": "A source said DayOne is considering a listing.",
                "text": (
                    "A source said the company is considering a dual initial public offering. "
                    "The article includes market context and routine source language."
                ),
            },
            {
                "article_id": "felix",
                "source": "AP",
                "topic_key": "us_economy",
                "topic_title": "US Economy",
                "title": "Felix Rosenqvist posts fastest 4-lap average in Indianapolis 500 qualifying",
                "description": "Rosenqvist led the first qualifying round.",
                "text": (
                    "A source said the driver posted the fastest qualifying average. "
                    "The article includes market context and routine source language."
                ),
            },
        ]

        selected, stats = organize_article_targets_into_stories(
            articles,
            topics,
            min_articles_per_story=2,
            max_stories_per_topic=4,
            max_articles_per_story=4,
            similarity_threshold=0.10,
        )

        self.assertEqual(selected, [])
        self.assertEqual(stats["story_count"], 0)

    def test_story_clustering_links_uae_nuclear_plant_coverage_and_excludes_false_hits(self) -> None:
        topics = [
            {
                "key": "iran_war",
                "title": "Iran War",
                "rationale": "Stories about the 2026 US/Israel war with Iran and its regional consequences.",
                "keywords": ["iran", "drone", "strike", "nuclear", "ceasefire"],
                "boost_phrases": ["iran israel war"],
            }
        ]
        articles = [
            {
                "article_id": "ap-uae",
                "source": "AP",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Drone strikes UAE nuclear plant as US and Iran signal they are prepared to resume war",
                "description": "The Barakah nuclear plant in Abu Dhabi was struck by drones.",
                "text": "The UAE said drones hit the Barakah nuclear plant near Abu Dhabi and caused a fire.",
            },
            {
                "article_id": "upi-uae",
                "source": "UPI",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Drone strike sparks fire at Abu Dhabi nuclear plant",
                "description": "Officials reported a fire at the Barakah nuclear plant.",
                "text": "A drone strike sparked a fire at the Barakah nuclear plant in Abu Dhabi, UAE.",
            },
            {
                "article_id": "france-uae",
                "source": "France 24",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Drone strike sparks a fire on the edge of UAE nuclear facility",
                "description": "The Barakah facility near Abu Dhabi was hit during regional tensions.",
                "text": "The UAE nuclear facility at Barakah near Abu Dhabi reported a fire after a drone strike.",
            },
            {
                "article_id": "scmp-uae",
                "source": "SCMP",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Drone targets UAE nuclear power plant, straining Iran war ceasefire",
                "description": "The Barakah nuclear power plant was targeted.",
                "text": "A drone targeted the Barakah nuclear power plant in the UAE, officials said.",
            },
            {
                "article_id": "mali",
                "source": "AFP",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Mali Military Drone Strikes Kill At Least 10 Civilians",
                "description": "The strike happened in Mali.",
                "text": "Mali officials reported casualties after a military drone strike.",
            },
            {
                "article_id": "samsung",
                "source": "Yonhap",
                "topic_key": "iran_war",
                "topic_title": "Iran War",
                "title": "Samsung Electronics labor union set to resume talks ahead of planned strike",
                "description": "The labor dispute continued in South Korea.",
                "text": "Samsung and its labor union planned further talks before a strike.",
            },
        ]

        selected, stats = organize_article_targets_into_stories(
            articles,
            topics,
            min_articles_per_story=2,
            max_stories_per_topic=4,
            max_articles_per_story=4,
            similarity_threshold=0.18,
        )

        self.assertEqual(
            {article["article_id"] for article in selected},
            {"ap-uae", "upi-uae", "france-uae", "scmp-uae"},
        )
        self.assertEqual(stats["story_count"], 1)
        self.assertEqual(stats["dropped_by_topic"], {"Iran War": 2})

    def test_story_clustering_caps_summaries_without_shrinking_detected_cluster(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A", "keywords": [], "boost_phrases": []}]
        articles = [
            {
                "article_id": f"a{index}",
                "source": f"Source {index}",
                "topic_key": "topic_a",
                "topic_title": "Topic A",
                "title": f"Harbor refinery drone damage report {index}",
                "description": "Harbor refinery drone attack damages fuel tanks.",
                "text": (
                    "The harbor refinery drone attack damaged fuel tanks and started a fire. "
                    "Firefighters contained the refinery blaze while officials inspected the site."
                ),
                "relevance_score": 10 - index,
            }
            for index in range(1, 6)
        ]

        selected, stats = organize_article_targets_into_stories(
            articles,
            topics,
            min_articles_per_story=2,
            max_stories_per_topic=4,
            max_articles_per_story=3,
            similarity_threshold=0.18,
        )

        self.assertEqual(len(selected), 3)
        story = stats["stories_by_topic"]["Topic A"][0]
        self.assertEqual(story["cluster_article_count"], 5)
        self.assertEqual(story["selected_article_count"], 3)
        self.assertEqual(stats["dropped_count"], 2)

    def test_explicit_story_payload_drops_story_blocks_below_floor(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        reports = [
            (
                "### Article One\n"
                "Metadata:\n"
                "- Source: Reuters\n"
                "- Published: Sat, 02 May 2026 10:00:00 GMT\n"
                "- URL: https://example.com/1\n"
                "- Topic: Topic A\n"
                "- Story: Refinery drone strike\n\n"
                "Summary:\n"
                "Officials said drones hit the refinery overnight."
            ),
            (
                "### Article Two\n"
                "Metadata:\n"
                "- Source: AP\n"
                "- Published: Sat, 02 May 2026 11:00:00 GMT\n"
                "- URL: https://example.com/2\n"
                "- Topic: Topic A\n"
                "- Story: Refinery drone strike\n\n"
                "Summary:\n"
                "The governor said the same refinery was damaged by drones."
            ),
            (
                "### Article Three\n"
                "Metadata:\n"
                "- Source: BBC\n"
                "- Published: Sat, 02 May 2026 12:00:00 GMT\n"
                "- URL: https://example.com/3\n"
                "- Topic: Topic A\n"
                "- Story: Court appeal\n\n"
                "Summary:\n"
                "Judges set a date for a separate political case."
            ),
        ]

        messages, _ = build_final_synthesis_payload(reports, "May 02, 2026", topics)
        payload = messages[0].content

        self.assertIn("Story: Refinery drone strike", payload)
        self.assertIn("1. Officials said drones hit the refinery", payload)
        self.assertIn("2. The governor said the same refinery", payload)
        self.assertNotIn("Story: Court appeal", payload)
        self.assertIn("distinct paragraph per Story block", payload)

    def test_final_synthesis_validation_rejects_topic_tag_leaks(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        self.assertFalse(
            is_valid_final_synthesis_response(
                "<topic> Topic A </topic>\n<topic> Topic B </topic>",
                topics,
                uses_custom_prompt=False,
            )
        )
        self.assertEqual(clean_synthesis_for_publication("<topic> Topic A </topic>"), "")

    def test_relaxed_final_synthesis_validation_accepts_dev_preview_text(self) -> None:
        topics = [{"key": "topic_a", "title": "Topic A"}]
        text = (
            "This dev preview has enough reported context to exercise the report "
            "rendering path even though the model did not return section headings."
        )

        self.assertFalse(
            is_valid_final_synthesis_response(
                text,
                topics,
                uses_custom_prompt=False,
            )
        )
        self.assertTrue(
            is_valid_final_synthesis_response(
                text,
                topics,
                uses_custom_prompt=False,
                relaxed=True,
            )
        )
        self.assertEqual(
            describe_final_synthesis_rejection(
                text,
                topics,
                uses_custom_prompt=False,
                relaxed=True,
            ),
            "",
        )

    def test_invalid_final_synthesis_routes_to_recovery(self) -> None:
        state = {
            "messages": [AIMessage(content="<topic> Topic A </topic>")],
            "final_reports": [],
            "articles_remaining": [],
            "empty_response_count": 1,
            "final_synthesis_token_stats": {},
            "generate_final_synthesis": True,
            "final_prompt_text": None,
            "topics": [{"key": "topic_a", "title": "Topic A"}],
        }

        self.assertEqual(should_continue(state), "recover")

    def test_article_summary_pass_ends_without_final_synthesis(self) -> None:
        state = {
            "messages": [AIMessage(content="ARTICLE_SUMMARIES_COMPLETE")],
            "final_reports": ["### One\nSummary:\nDone"],
            "articles_remaining": [],
            "empty_response_count": 0,
            "final_synthesis_token_stats": {},
            "generate_final_synthesis": False,
            "final_prompt_text": None,
            "topics": [{"key": "topic_a", "title": "Topic A"}],
        }

        self.assertEqual(should_continue(state), END)

    def test_article_text_token_truncation_is_generous_but_enforced(self) -> None:
        long_text = " ".join(f"word{index}" for index in range(400))
        truncated = truncate_text_to_token_limit(long_text, 80)

        self.assertLessEqual(len(truncated), len(long_text))
        self.assertTrue(truncated.endswith("..."))
        self.assertLessEqual(len(truncated.split()), 120)

    def test_article_summary_prompt_uses_stable_system_message(self) -> None:
        article_one = {
            "title": "First article",
            "source": "Reuters",
            "pub_date": "Sat, 02 May 2026 10:00:00 GMT",
            "url": "https://example.com/1",
            "description": "First description",
            "text": "First body",
            "topic_title": "Topic A",
        }
        article_two = {
            **article_one,
            "title": "Second article",
            "url": "https://example.com/2",
            "text": "Second body",
        }

        first_messages = build_article_summary_prompt_messages(article_one, "May 02, 2026")
        second_messages = build_article_summary_prompt_messages(article_two, "May 02, 2026")

        self.assertEqual(first_messages[0].content, second_messages[0].content)
        self.assertNotEqual(first_messages[1].content, second_messages[1].content)


if __name__ == "__main__":
    unittest.main()
