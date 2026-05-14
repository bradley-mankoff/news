"""Command-line entry point for the daily news pipeline.

Usage:
    uv run todays_news.py
    NEWS_MODEL=qwen-9b-dense uv run todays_news.py
    NEWS_RUN_MODE=local-prod uv run todays_news.py
    NEWS_DEV=0 uv run todays_news.py
    uv run todays_news.py --local-prod
    uv run todays_news.py --model-server-command
    uv run todays_news.py --serve-unsubscribe
"""

from __future__ import annotations

import os
import sys

from .config import load_runtime_config


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode_flags = {
        "--dev": "dev",
        "--local-prod": "local-prod",
        "--prod": "prod",
    }
    for flag, mode in mode_flags.items():
        if flag in args:
            os.environ["NEWS_RUN_MODE"] = mode
            args.remove(flag)
    if "--serve-unsubscribe" in args:
        from .pipeline import serve_unsubscribe_endpoint

        serve_unsubscribe_endpoint()
        return 0
    if "--model-server-command" in args:
        config = load_runtime_config()
        print(config.model_server_command)
        return 0
    from .pipeline import run_pipeline

    run_pipeline()
    return 0
