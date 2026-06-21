"""Command-line entry point for the daily news pipeline."""

from __future__ import annotations

import os
import sys

from .config import (
    apply_run_preset_to_environment,
    ensure_codex_safe_model_reference,
    load_runtime_config,
    reject_removed_topic_env_vars,
)


USAGE = """\
Usage:
  uv run news run [--preset NAME]
  uv run news check-sources [--sources-yaml PATH] [--only-failures]
  uv run news prune-sources [--sources-yaml PATH] [--recent-days 7]
  uv run news source-languages --sources-yaml PATH [--write-languages]
  uv run news model-server-command
  uv run news codex-model-server-command
  uv run news test-translation-model
  uv run news serve-unsubscribe
  uv run news ui [--host 127.0.0.1] [--port 8766] [--open]
  uv run news history backfill [--dry-run|--apply]
  uv run news history cleanup [--dry-run|--apply]
  uv run news history export
"""

ACTION_ALIASES = {
    "model-server-command": "model-server-command",
    "server-command": "model-server-command",
    "--model-server-command": "model-server-command",
    "codex-model-server-command": "codex-model-server-command",
    "codex-server-command": "codex-model-server-command",
    "test-translation-model": "test-translation-model",
    "translation-model-test": "test-translation-model",
    "probe-translation-model": "test-translation-model",
    "--test-translation-model": "test-translation-model",
    "serve-unsubscribe": "serve-unsubscribe",
    "unsubscribe-server": "serve-unsubscribe",
    "--serve-unsubscribe": "serve-unsubscribe",
    "check-sources": "check-sources",
    "source-check": "check-sources",
    "sources": "check-sources",
    "prune-sources": "prune-sources",
    "prune-stale-sources": "prune-sources",
    "source-languages": "source-languages",
    "detect-source-languages": "source-languages",
    "source-language": "source-languages",
    "history": "history",
    "ui": "ui",
    "local-ui": "ui",
    "control-panel": "ui",
}

def _consume_preset_arg(args: list[str]) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    preset: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--preset":
            if index + 1 >= len(args):
                raise ValueError("--preset requires a preset name.")
            preset = args[index + 1]
            index += 2
            continue
        if arg.startswith("--preset="):
            preset = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return preset, remaining


def _apply_cli_preset(preset: str | None) -> bool:
    if not preset:
        return True
    try:
        apply_run_preset_to_environment(preset)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return False
    return True


def _run_pipeline_command() -> int:
    from .pipeline import run_pipeline

    run_pipeline()
    return 0


def _print_model_server_command() -> int:
    config = load_runtime_config()
    ensure_codex_safe_model_reference(config.model_reference)
    print(config.model_server_command)
    return 0


def _print_codex_model_server_command() -> int:
    os.environ["NEWS_CODEX_TESTING"] = "1"
    return _print_model_server_command()


def _serve_unsubscribe() -> int:
    from .pipeline import serve_unsubscribe_endpoint

    serve_unsubscribe_endpoint()
    return 0


def _run_ui(args: list[str]) -> int:
    from .ui import main as ui_main

    return ui_main(args)


def _run_history(args: list[str]) -> int:
    from .history_store import parse_history_args

    config = load_runtime_config(materialize_outputs=False)
    try:
        result = parse_history_args(
            args,
            output_dir=config.output_dir,
            db_path=config.history_db_path,
            export_csv=config.history_export_csv,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(result.format())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    command = args.pop(0) if args else "run"
    action = ACTION_ALIASES.get(command)
    if action == "ui":
        return _run_ui(args)
    if action == "history":
        return _run_history(args)

    reject_removed_topic_env_vars()

    if command == "run":
        try:
            preset, args = _consume_preset_arg(args)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        if not _apply_cli_preset(preset):
            return 2
        if args:
            print(f"Unexpected arguments for run: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_pipeline_command()

    if action == "model-server-command":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _print_model_server_command()
    if action == "codex-model-server-command":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _print_codex_model_server_command()
    if action == "test-translation-model":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        from .pipeline import run_translation_model_smoke_test

        return run_translation_model_smoke_test()
    if action == "serve-unsubscribe":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _serve_unsubscribe()
    if action == "check-sources":
        from .source_checks import main as source_check_main

        return source_check_main(args)
    if action == "prune-sources":
        from .source_checks import main as source_check_main

        return source_check_main(["--prune-inactive", *args])
    if action == "source-languages":
        from .source_checks import main as source_check_main

        return source_check_main(["--detect-languages", *args])

    print(f"Unknown command: {command}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2
