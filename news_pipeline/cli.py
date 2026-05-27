"""Command-line entry point for the daily news pipeline."""

from __future__ import annotations

import os
import sys

from .config import ensure_codex_safe_model_reference, load_runtime_config


USAGE = """\
Usage:
  uv run news dev
  uv run news local-prod
  uv run news loose-local-prod
  uv run news prod
  uv run news check-sources [--sources-yaml PATH] [--only-failures]
  uv run news prune-sources [--sources-yaml PATH] [--recent-days 7]
  uv run news source-languages --sources-yaml PATH [--write-languages]
  uv run news model-server-command
  uv run news codex-model-server-command
  uv run news test-translation-model
  uv run news serve-unsubscribe

Compatibility:
  uv run todays_news.py [dev|local-prod|loose-local-prod|prod]
  uv run todays_news.py --dev|--local-prod|--loose-local-prod|--prod
  uv run todays_news.py --model-server-command
  uv run todays_news.py --test-translation-model
  uv run todays_news.py --serve-unsubscribe
  NEWS_RUN_MODE=local-prod uv run todays_news.py
  NEWS_RUN_MODE=loose-local-prod uv run todays_news.py
  NEWS_DEV=0 uv run todays_news.py

"""

RUN_MODE_COMMANDS = {
    "dev": "dev",
    "local-prod": "local-prod",
    "local_prod": "local-prod",
    "localprod": "local-prod",
    "loose-local-prod": "loose-local-prod",
    "loose_local_prod": "loose-local-prod",
    "loose-localprod": "loose-local-prod",
    "looselocal-prod": "loose-local-prod",
    "looselocalprod": "loose-local-prod",
    "prod": "prod",
    "production": "prod",
}
MODE_FLAGS = {
    "--dev": "dev",
    "--local-prod": "local-prod",
    "--loose-local-prod": "loose-local-prod",
    "--prod": "prod",
}
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
}


def _apply_legacy_flags(args: list[str]) -> list[str]:
    remaining = list(args)
    for flag, mode in MODE_FLAGS.items():
        if flag in remaining:
            os.environ["NEWS_RUN_MODE"] = mode
            remaining.remove(flag)
    return remaining


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


def main(argv: list[str] | None = None) -> int:
    args = _apply_legacy_flags(sys.argv[1:] if argv is None else argv)

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    command = args.pop(0) if args else "run"
    if command == "run":
        if args:
            print(f"Unexpected arguments for run: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_pipeline_command()

    if command in RUN_MODE_COMMANDS:
        os.environ["NEWS_RUN_MODE"] = RUN_MODE_COMMANDS[command]
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_pipeline_command()

    action = ACTION_ALIASES.get(command)
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
