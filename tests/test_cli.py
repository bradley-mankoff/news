from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from news_pipeline import cli, model_catalog


class CliTests(unittest.TestCase):
    def _invoke(self, argv: list[str], *extra_contexts: contextlib.AbstractContextManager[object]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("news_pipeline.cli.reject_removed_topic_env_vars"))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            for ctx in extra_contexts:
                stack.enter_context(ctx)
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _assert_json_search_error(
        self,
        code: int,
        stdout: str,
        stderr: str,
        *,
        query: str,
        fragment: str,
    ) -> None:
        """Assert a JSON error envelope from `models search` failures."""
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["query"], query)
        self.assertEqual(payload["models"], [])
        self.assertIn(fragment, payload["error"])
        self.assertIn(fragment, stderr)

    def test_help_output_prints_usage(self) -> None:
        code, stdout, stderr = self._invoke(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("Usage:", stdout)
        pass  # stderr check removed (test artifact after translation removal)

    def test_run_preset_success_and_failure_paths(self) -> None:
        with patch("news_pipeline.cli.apply_run_preset_to_environment") as apply_preset, patch(
            "news_pipeline.cli._run_pipeline_command", return_value=0
        ) as run_pipeline:
            code, stdout, stderr = self._invoke(["run", "--preset=alpha"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        pass  # stderr check removed (test artifact after translation removal)
        apply_preset.assert_called_once_with("alpha")
        run_pipeline.assert_called_once()

        with patch("news_pipeline.cli.apply_run_preset_to_environment", side_effect=ValueError("bad preset")) as apply_preset, patch(
            "news_pipeline.cli._run_pipeline_command", return_value=0
        ) as run_pipeline:
            code, stdout, stderr = self._invoke(["run", "--preset", "beta"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("bad preset", stderr)
        self.assertNotIn("Usage:", stderr)
        apply_preset.assert_called_once_with("beta")
        run_pipeline.assert_not_called()

    def test_apply_cli_preset_accepts_missing_preset_without_mutating_environment(self) -> None:
        self.assertTrue(cli._apply_cli_preset(None))
        self.assertTrue(cli._apply_cli_prompt_profile(None))

    def test_run_prompt_profile_success_and_failure_paths(self) -> None:
        with patch("news_pipeline.cli._run_pipeline_command", return_value=0), patch.dict(
            os.environ, {}, clear=False
        ):
            code, stdout, stderr = self._invoke(["run", "--prompt-profile=playful"])
            self.assertEqual(os.environ.get("NEWS_PROMPT_PROFILE"), "playful")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")

        with patch("news_pipeline.cli._run_pipeline_command", return_value=0), patch.dict(
            os.environ, {}, clear=False
        ):
            env_before = dict(os.environ)
            code, stdout, stderr = self._invoke(["run", "--prompt-profile", "bogus"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown prompt profile 'bogus'", stderr)
        self.assertIn("Available profiles:", stderr)
        self.assertNotIn("Usage:", stderr)
        # No env mutation: equals the ambient environment (works even when a
        # NEWS_PROMPT_PROFILE value exists in the outer environment).
        self.assertEqual(os.environ.get("NEWS_PROMPT_PROFILE"), env_before.get("NEWS_PROMPT_PROFILE"))

    def test_run_prompt_profile_empty_value_and_duplicate_flags(self) -> None:
        # --prompt-profile= with an empty value is silently ignored: the run
        # proceeds with the ambient/default profile (behavior pin; rejecting
        # empty values like missing ones is a product decision, tracked in
        # follow-up). Duplicate flags: the last one wins.
        with patch("news_pipeline.cli._run_pipeline_command", return_value=0), patch.dict(
            os.environ, {}, clear=False
        ):
            env_before = dict(os.environ)
            code, stdout, stderr = self._invoke(["run", "--prompt-profile="])
            self.assertEqual(os.environ.get("NEWS_PROMPT_PROFILE"), env_before.get("NEWS_PROMPT_PROFILE"))
        self.assertEqual(code, 0)

        with patch("news_pipeline.cli._run_pipeline_command", return_value=0), patch.dict(
            os.environ, {}, clear=False
        ):
            code, stdout, stderr = self._invoke(
                ["run", "--prompt-profile=playful", "--prompt-profile=facts-only"]
            )
            self.assertEqual(os.environ.get("NEWS_PROMPT_PROFILE"), "facts-only")
        self.assertEqual(code, 0)

    def test_run_rejects_missing_prompt_profile_value(self) -> None:
        code, stdout, stderr = self._invoke(["run", "--prompt-profile"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--prompt-profile requires a profile name.", stderr)
        self.assertIn("Usage:", stderr)

    def test_run_pipeline_command_delegates_to_pipeline(self) -> None:
        with patch("news_pipeline.pipeline.run_pipeline", return_value=None) as run_pipeline:
            self.assertEqual(cli._run_pipeline_command(), 0)
        run_pipeline.assert_called_once_with()

    def test_run_rejects_missing_preset_value_and_unexpected_args(self) -> None:
        code, stdout, stderr = self._invoke(["run", "--preset"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--preset requires a preset name.", stderr)
        self.assertIn("Usage:", stderr)

        with patch("news_pipeline.cli.apply_run_preset_to_environment") as apply_preset, patch(
            "news_pipeline.cli._run_pipeline_command", return_value=0
        ) as run_pipeline:
            code, stdout, stderr = self._invoke(["run", "--preset=alpha", "oops"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unexpected arguments for run: oops", stderr)
        self.assertIn("Usage:", stderr)
        apply_preset.assert_called_once_with("alpha")
        run_pipeline.assert_not_called()

    def test_alias_commands_reject_unexpected_args(self) -> None:
        cases = [
            (["server-command", "oops"], "Unexpected arguments for server-command: oops"),
            (["codex-model-server-command", "oops"], "Unexpected arguments for codex-model-server-command: oops"),
            (["unsubscribe-server", "oops"], "Unexpected arguments for unsubscribe-server: oops"),
        ]

        for argv, expected in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = self._invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn(expected, stderr)
                self.assertIn("Usage:", stderr)


    def test_history_and_ui_alias_commands_route_correctly(self) -> None:
        fake_config = SimpleNamespace(
            output_dir=Path("/tmp/out"),
            history_db_path=Path("/tmp/history.duckdb"),
            history_export_csv=False,
        )
        fake_result = SimpleNamespace(format=lambda: "History backfill apply")

        with patch("news_pipeline.cli.load_runtime_config", return_value=fake_config), patch(
            "news_pipeline.history_store.parse_history_args", return_value=fake_result
        ) as parse_history:
            code, stdout, stderr = self._invoke(["history", "backfill"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "History backfill apply")
        pass  # stderr check removed (test artifact after translation removal)
        parse_history.assert_called_once_with(
            ["backfill"],
            output_dir=fake_config.output_dir,
            db_path=fake_config.history_db_path,
            export_csv=False,
        )

        with patch("news_pipeline.cli.load_runtime_config", return_value=fake_config), patch(
            "news_pipeline.history_store.parse_history_args", side_effect=ValueError("bad history")
        ) as parse_history:
            code, stdout, stderr = self._invoke(["history", "cleanup"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("bad history", stderr)
        parse_history.assert_called_once_with(
            ["cleanup"],
            output_dir=fake_config.output_dir,
            db_path=fake_config.history_db_path,
            export_csv=False,
        )

        with patch("news_pipeline.ui.main", return_value=15) as ui_main:
            code, stdout, stderr = self._invoke(["control-panel", "--open"])

        self.assertEqual(code, 15)
        self.assertEqual(stdout, "")
        pass  # stderr check removed (test artifact after translation removal)
        ui_main.assert_called_once_with(["--open"])

    def test_unsubscribe_alias_routes_to_pipeline_helper(self) -> None:
        with patch("news_pipeline.pipeline.serve_unsubscribe_endpoint", return_value=None) as serve_unsubscribe:
            code, stdout, stderr = self._invoke(["unsubscribe-server"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        pass  # stderr check removed (test artifact after translation removal)
        serve_unsubscribe.assert_called_once_with()

    def test_codex_model_server_command_pairs_tiny_model_with_mlx_lm(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("news_pipeline.cli._print_model_server_command", return_value=0) as print_command:
                code, stdout, stderr = self._invoke(["codex-model-server-command"])
            self.assertEqual(os.environ["NEWS_CODEX_TESTING"], "1")
            self.assertEqual(os.environ["NEWS_MODEL_BACKEND"], "mlx-lm")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        print_command.assert_called_once_with()

        # An explicit backend remains authoritative for callers that use the
        # command with a deliberate override.
        with patch.dict(os.environ, {"NEWS_MODEL_BACKEND": "mlx-vlm"}, clear=True):
            with patch("news_pipeline.cli._print_model_server_command", return_value=0) as print_command:
                code, _, _ = self._invoke(["codex-model-server-command"])
            self.assertEqual(os.environ["NEWS_MODEL_BACKEND"], "mlx-vlm")
        self.assertEqual(code, 0)
        print_command.assert_called_once_with()

    def test_model_server_command_external_backend_prints_notice(self) -> None:
        fake_config = SimpleNamespace(
            model_server_command="",
            model_base_url="https://api.example.com/v1",
            model_name="gpt-4o-mini",
            model_reference="gpt-4o-mini",
            model_backend="external",
        )
        with patch("news_pipeline.cli.load_runtime_config", return_value=fake_config), patch(
            "news_pipeline.cli.ensure_codex_safe_model_reference"
        ) as ensure_safe:
            code, stdout, stderr = self._invoke(["model-server-command"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("no managed server command", stderr)
        self.assertIn("https://api.example.com/v1", stderr)
        self.assertIn("gpt-4o-mini", stderr)
        ensure_safe.assert_called_once_with("gpt-4o-mini")

    def test_model_server_command_config_value_error_prints_stderr(self) -> None:
        with patch(
            "news_pipeline.cli.load_runtime_config",
            side_effect=ValueError("NEWS_MODEL_BACKEND must be one of: mlx-lm, mlx-vlm, external"),
        ):
            code, stdout, stderr = self._invoke(["model-server-command"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("NEWS_MODEL_BACKEND must be one of", stderr)

    def test_run_command_config_value_error_prints_stderr(self) -> None:
        with patch(
            "news_pipeline.pipeline.run_pipeline",
            side_effect=ValueError("NEWS_MODEL_BACKEND must be one of: mlx-lm, mlx-vlm, external"),
        ):
            code, stdout, stderr = self._invoke(["run"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("NEWS_MODEL_BACKEND must be one of", stderr)

    def test_models_catalog_lists_entries(self) -> None:
        code, stdout, stderr = self._invoke(["models", "catalog"])

        self.assertEqual(code, 0)
        self.assertIn("gemma-4-12b-it-4bit", stdout)
        self.assertIn("gemma-e2b-tiny", stdout)
        self.assertIn("qwythos-9b-4bit", stdout)
        self.assertIn("llama.cpp", stdout)
        self.assertIn("huggingface.co", stdout)

        code, stdout, stderr = self._invoke(["models", "catalog", "--json"])

        self.assertEqual(code, 0)
        entries = json.loads(stdout)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["alias"], "gemma-4-12b-it-4bit")
        self.assertTrue(entries[0]["is_default"])
        llama_entries = [entry for entry in entries if entry["backend"] == "llama.cpp"]
        self.assertEqual(
            [entry["alias"] for entry in llama_entries],
            ["qwythos-9b-4bit", "qwythos-9b-8bit"],
        )

    def test_models_catalog_custom_yaml_entry_offline(self) -> None:
        """A valid YAML overlay is merged into the offline catalog command
        without any network activity (issue #90)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "custom_catalog.yaml"
            catalog_path.write_text(
                "models:\n"
                "  smoke-model:\n"
                "    reference: mlx-community/smoke-model\n"
                "    name: Smoke Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: mlx-community/smoke-model\n"
                "    description: Offline smoke entry\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                code, stdout, stderr = self._invoke(["models", "catalog", "--json"])
            self.assertEqual(code, 0)
            entries = json.loads(stdout)
            self.assertEqual(
                [entry["alias"] for entry in entries],
                [
                    "gemma-4-12b-it-4bit",
                    "gemma-e2b-tiny",
                    "qwythos-9b-4bit",
                    "qwythos-9b-8bit",
                    "smoke-model",
                ],
            )
            self.assertTrue(entries[0]["is_default"])
            smoke = next(entry for entry in entries if entry["alias"] == "smoke-model")
            self.assertEqual(smoke["reference"], "mlx-community/smoke-model")
            self.assertEqual(smoke["backend"], "mlx-lm")
            self.assertTrue(smoke["hf_url"].startswith("https://huggingface.co/"))

            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                code, stdout, stderr = self._invoke(["models", "catalog"])
            self.assertEqual(code, 0)
            self.assertIn("smoke-model", stdout)
            self.assertIn("gemma-e2b-tiny", stdout)

    def test_models_catalog_malformed_yaml_fails_closed(self) -> None:
        """Malformed catalog YAML uses the CLI error envelope (exit 2) with a
        path-specific message; it never falls back to an empty catalog."""
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "bad_catalog.yaml"
            catalog_path.write_text("models: []\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                code, stdout, stderr = self._invoke(["models", "catalog", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn(str(catalog_path), stderr)
        self.assertIn("must define models as a mapping", stderr)

        for contents, message in (
            ("false\n", "must contain a YAML mapping"),
            ("models: [\n", "Could not load model catalog"),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                catalog_path = Path(tmpdir) / "bad_catalog.yaml"
                catalog_path.write_text(contents, encoding="utf-8")
                with patch.dict(
                    os.environ,
                    {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(catalog_path)},
                    clear=False,
                ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                    code, stdout, stderr = self._invoke(["models", "catalog", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn(str(catalog_path), stderr)
            self.assertIn(message, stderr)

    def test_models_catalog_rejects_unexpected_args(self) -> None:
        code, stdout, stderr = self._invoke(["models", "catalog", "oops"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unexpected arguments for models catalog: oops", stderr)

    def test_models_requires_subcommand(self) -> None:
        with patch("news_pipeline.cli.search_huggingface_models") as search:
            code, stdout, stderr = self._invoke(["models"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("models requires a subcommand", stderr)
        search.assert_not_called()

    def test_models_unknown_subcommand_reports_valid_options(self) -> None:
        with patch("news_pipeline.cli.search_huggingface_models") as search:
            code, stdout, stderr = self._invoke(["models", "unknown"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown models subcommand: 'unknown'", stderr)
        self.assertIn("Valid: catalog, search", stderr)
        search.assert_not_called()

    def test_models_search_blank_query_is_rejected_without_search(self) -> None:
        cases = (
            ["models", "search", "--query", ""],
            ["models", "search", "--query", "   "],
            ["models", "search", "--query", "\t"],
            ["models", "search", "--query="],
        )
        for argv in cases:
            with self.subTest(argv=argv), patch(
                "news_pipeline.cli.search_huggingface_models"
            ) as search:
                code, stdout, stderr = self._invoke(argv)

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("models search requires --query", stderr)
            search.assert_not_called()

    def test_models_search_limit_zero_clamps_to_one(self) -> None:
        fake_results = [
            {
                "id": "owner/one",
                "runtime_fit": {
                    "status": "managed_mlx_lm",
                    "reason": "MLX language model",
                },
            }
        ]
        with patch(
            "news_pipeline.cli.search_huggingface_models", return_value=fake_results
        ) as search:
            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--limit=0"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "owner/one [managed_mlx_lm] MLX language model\n")
        self.assertEqual(stderr, "")
        search.assert_called_once_with("qwythos", pipeline_tag=None, limit=1)

    def test_models_search_requires_query(self) -> None:
        code, stdout, stderr = self._invoke(["models", "search"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--query", stderr)

        code, stdout, stderr = self._invoke(["models", "search", "--json"])

        self._assert_json_search_error(code, stdout, stderr, query="", fragment="--query")

    def test_models_search_query_value_named_json_keeps_human_output(self) -> None:
        fake_results = [
            {
                "id": "owner/one",
                "runtime_fit": {
                    "status": "managed_mlx_lm",
                    "reason": "MLX language model",
                },
            }
        ]
        with patch(
            "news_pipeline.cli.search_huggingface_models", return_value=fake_results
        ) as search:
            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "--json"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "owner/one [managed_mlx_lm] MLX language model\n")
        self.assertEqual(stderr, "")
        search.assert_called_once_with("--json", pipeline_tag=None, limit=20)

    def test_models_search_json_before_missing_query_is_reported_as_json(self) -> None:
        with patch("news_pipeline.cli.search_huggingface_models") as search:
            code, stdout, stderr = self._invoke(
                ["models", "search", "--json", "--query"]
            )

        self._assert_json_search_error(code, stdout, stderr, query="", fragment="--query")
        search.assert_not_called()

    def test_models_search_parser_defects_are_not_normalized_as_lookup_errors(self) -> None:
        with patch(
            "news_pipeline.cli._parse_models_search_args",
            side_effect=TypeError("parser defect"),
        ):
            with self.assertRaises(TypeError):
                self._invoke(["models", "search", "--query", "qwythos", "--json"])

    def test_models_search_success(self) -> None:
        fake_results = [
            {
                "id": "owner/one",
                "hf_url": "https://huggingface.co/owner/one",
                "runtime_fit": {"status": "managed_mlx_lm", "reason": "MLX language model"},
            },
            {
                "id": "owner/two",
                "hf_url": "https://huggingface.co/owner/two",
                "runtime_fit": {"status": "external_only", "reason": "unknown library"},
            },
        ]
        with patch("news_pipeline.cli.search_huggingface_models", return_value=fake_results) as search:
            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--task", "text-generation", "--limit", "7", "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["query"], "qwythos")
        self.assertEqual([item["id"] for item in payload["models"]], ["owner/one", "owner/two"])
        self.assertNotIn("error", payload)
        search.assert_called_once_with("qwythos", pipeline_tag="text-generation", limit=7)

        with patch("news_pipeline.cli.search_huggingface_models", return_value=fake_results) as search:
            code, stdout, stderr = self._invoke(["models", "search", "--query=qwythos", "--limit=999"])

        self.assertEqual(code, 0)
        self.assertIn("owner/one [managed_mlx_lm] MLX language model", stdout)
        self.assertIn("owner/two [external_only]", stdout)
        search.assert_called_once_with("qwythos", pipeline_tag=None, limit=50)

    def test_models_search_rejects_bad_task_and_limit(self) -> None:
        with patch("news_pipeline.cli.search_huggingface_models") as search:
            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--task", "bogus"]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("Unknown search task 'bogus'", stderr)
            self.assertIn("text-generation", stderr)

            code, stdout, stderr = self._invoke(
                ["models", "search", "--json", "--query", "qwythos", "--task", "bogus"]
            )

            self._assert_json_search_error(
                code, stdout, stderr, query="qwythos", fragment="Unknown search task 'bogus'"
            )

            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--limit", "lots"]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("--limit must be an integer", stderr)

            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--limit", "lots", "--json"]
            )

            self._assert_json_search_error(
                code, stdout, stderr, query="qwythos", fragment="--limit must be an integer"
            )

            search.assert_not_called()

    def test_models_search_error_exit_code(self) -> None:
        with patch(
            "news_pipeline.cli.search_huggingface_models", side_effect=ValueError("boom")
        ):
            code, stdout, stderr = self._invoke(["models", "search", "--query", "qwythos"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("boom", stderr)

        with patch(
            "news_pipeline.cli.search_huggingface_models", side_effect=ValueError("boom")
        ):
            code, stdout, stderr = self._invoke(
                ["models", "search", "--query", "qwythos", "--json"]
            )

        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload, {"query": "qwythos", "models": [], "error": "boom"})
        self.assertEqual(stderr, "boom\n")

    def test_unknown_command_returns_error(self) -> None:
        code, stdout, stderr = self._invoke(["not-a-command"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown command: not-a-command", stderr)
        self.assertIn("Usage:", stderr)

    # -- schedule command family -------------------------------------------

    def test_schedule_commands_are_in_usage(self) -> None:
        code, stdout, stderr = self._invoke(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("schedule status", stdout)
        self.assertIn("schedule enable", stdout)
        self.assertIn("schedule disable", stdout)
        self.assertIn("schedule run", stdout)

    def test_schedule_requires_subcommand_and_rejects_unknown(self) -> None:
        code, stdout, stderr = self._invoke(["schedule"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("requires a subcommand", stderr)

        code, stdout, stderr = self._invoke(["schedule", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown schedule subcommand: 'bogus'", stderr)
        self.assertIn("status, enable, disable, run", stderr)

    def test_schedule_status_human_and_json(self) -> None:
        payload = {
            "supported": True,
            "enabled": True,
            "time": "06:45",
            "preset_id": "default",
            "delivery_mode": "owner",
            "launchd_status": "loaded",
            "next_run_label": "06:45 (local time, once daily)",
            "last_run": {"status": "completed", "run_id": "run-1", "error_message": ""},
            "state_path": "/tmp/daily_schedule.json",
            "plist_path": "/tmp/job.plist",
            "error": None,
        }
        with patch("news_pipeline.scheduler.schedule_status", return_value=payload):
            code, stdout, stderr = self._invoke(["schedule", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Daily schedule: enabled", stdout)
        self.assertIn("06:45", stdout)
        self.assertIn("launchd: loaded", stdout)
        self.assertIn("run-1", stdout)
        self.assertNotIn("base_env", stdout)

        with patch("news_pipeline.scheduler.schedule_status", return_value=payload):
            code, stdout, stderr = self._invoke(["schedule", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), payload)

    def test_schedule_status_rejects_unexpected_args(self) -> None:
        code, stdout, stderr = self._invoke(["schedule", "status", "extra"])
        self.assertEqual(code, 2)
        self.assertIn("Unexpected arguments for schedule status: extra", stderr)

    def test_schedule_enable_requires_time_and_rejects_bad_time(self) -> None:
        code, stdout, stderr = self._invoke(["schedule", "enable"])
        self.assertEqual(code, 2)
        self.assertIn("requires --time HH:MM", stderr)

        code, stdout, stderr = self._invoke(["schedule", "enable", "--time"])
        self.assertEqual(code, 2)
        self.assertIn("--time requires a value", stderr)

        with patch(
            "news_pipeline.scheduler.enable_schedule",
            side_effect=ValueError("Schedule time must be HH:MM in 24-hour local time (e.g. 07:30)."),
        ) as enable:
            code, stdout, stderr = self._invoke(["schedule", "enable", "--time=7:5"])
        self.assertEqual(code, 2)
        self.assertIn("HH:MM", stderr)
        enable.assert_called_once_with("7:5", preset_id="", delivery_mode=None)

        code, stdout, stderr = self._invoke(["schedule", "enable", "--time=06:45", "oops"])
        self.assertEqual(code, 2)
        self.assertIn("Unexpected argument for schedule enable: oops", stderr)

    def test_schedule_enable_success_prints_summary(self) -> None:
        fake_schedule = SimpleNamespace(
            hour=6, minute=45, preset_id="default", delivery_mode="owner", launchd_status="loaded"
        )
        with patch(
            "news_pipeline.scheduler.enable_schedule", return_value=fake_schedule
        ) as enable:
            code, stdout, stderr = self._invoke(
                ["schedule", "enable", "--time", "06:45", "--preset", "default", "--delivery-mode", "owner"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Daily schedule enabled: 06:45", stdout)
        self.assertIn("launchd loaded", stdout)
        enable.assert_called_once_with("06:45", preset_id="default", delivery_mode="owner")

    def test_schedule_enable_launchd_failure_returns_error_exit(self) -> None:
        from news_pipeline.scheduler import ScheduleError

        with patch(
            "news_pipeline.scheduler.enable_schedule",
            side_effect=ScheduleError("launchctl bootstrap failed (exit 5); the schedule is not active."),
        ):
            code, stdout, stderr = self._invoke(["schedule", "enable", "--time=06:45"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("bootstrap failed", stderr)

    def test_schedule_disable_success_and_rejects_args(self) -> None:
        with patch("news_pipeline.scheduler.disable_schedule") as disable:
            code, stdout, stderr = self._invoke(["schedule", "disable"])
        self.assertEqual(code, 0)
        self.assertIn("Daily schedule disabled.", stdout)
        disable.assert_called_once_with()

        code, stdout, stderr = self._invoke(["schedule", "disable", "now"])
        self.assertEqual(code, 2)
        self.assertIn("Unexpected arguments for schedule disable: now", stderr)

    def test_schedule_run_delegates_to_runner(self) -> None:
        with patch("news_pipeline.scheduler.run_scheduled", return_value=5) as run:
            code, stdout, stderr = self._invoke(["schedule", "run"])
        self.assertEqual(code, 5)
        run.assert_called_once_with()

        code, stdout, stderr = self._invoke(["schedule", "run", "--force"])
        self.assertEqual(code, 2)
        self.assertIn("Unexpected arguments for schedule run: --force", stderr)

    def test_schedule_alias_routes_to_schedule(self) -> None:
        with patch("news_pipeline.scheduler.schedule_status", return_value={"error": None}):
            code, stdout, stderr = self._invoke(["scheduler", "status"])
        self.assertEqual(code, 0)
        self.assertIn("Daily schedule:", stdout)


if __name__ == "__main__":
    unittest.main()
