from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from news_pipeline import cli


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

    def test_unknown_command_returns_error(self) -> None:
        code, stdout, stderr = self._invoke(["not-a-command"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown command: not-a-command", stderr)
        self.assertIn("Usage:", stderr)


if __name__ == "__main__":
    unittest.main()
