from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
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
    configured_model_profile,
    infer_model_profile_key,
    load_sources,
    load_top_funnel_providers,
    resolve_model_name,
)
from news_pipeline.pipeline import (
    ProgressTracker,
    _cluster_supported_fallback_topics,
    _enforce_text_free_image_prompt,
    _sanitize_overlay_headline,
    _score_topic_against_story,
    _score_topic_relevance,
    annotate_topic_discovery_signals,
    budget_article_targets,
    build_article_summary_prompt_messages,
    build_dev_final_synthesis_preview,
    build_final_synthesis_payload,
    build_report_body,
    build_top_funnel_article_targets_for_coverage_gaps,
    clean_synthesis_for_publication,
    describe_final_synthesis_rejection,
    estimate_message_token_count,
    get_default_final_synthesis_instructions,
    is_valid_final_synthesis_response,
    prepare_candidate_topics_for_selection,
    select_topics_soft_weighted,
    should_continue,
    strip_model_artifacts,
    truncate_text_to_token_limit,
)


class ConfigAndTopicSelectionTests(unittest.TestCase):
    def test_progress_tracker_allocates_article_summary_progress(self) -> None:
        tracker = ProgressTracker()
        with redirect_stdout(StringIO()):
            tracker.reset(total_sources=20)
            for source_index in range(1, 21):
                tracker.start_source(source_index)
                tracker.source_completed()

            self.assertEqual(tracker._percent(), 60)

            tracker.start_article_summary(12)
            self.assertEqual(tracker._percent(), 60)

            for _ in range(6):
                tracker.article_completed()
            self.assertEqual(tracker._percent(), 75)

            for _ in range(6):
                tracker.article_completed()
            self.assertEqual(tracker._percent(), 90)

            tracker.set_final_step("reports", 1)
            self.assertEqual(tracker._percent(), 92)

            tracker.finish("done")
            self.assertEqual(tracker._percent(), 100)

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

        self.assertIn("article 2/2", output.getvalue())
        self.assertNotIn("article 0/0", output.getvalue())

    def _write_config(self, payload: dict) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "sources.yaml"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def test_cursorignore_manager_keeps_only_current_run_visible(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root_dir = Path(temp_dir.name)
        output_dir = root_dir / "output" / "daily_outputs"
        run_output_dir = output_dir / "2026-05-02"
        run_output_dir.mkdir(parents=True)
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

        cursorignore = cursorignore_path.read_text(encoding="utf-8")
        self.assertIn("custom-rule", cursorignore)
        self.assertIn("output/daily_outputs/*", cursorignore)
        self.assertIn("!output/daily_outputs/2026-05-02/", cursorignore)
        self.assertIn("!output/daily_outputs/2026-05-02/**", cursorignore)
        self.assertNotIn("2026-05-01", cursorignore)

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
