"""Compatibility wrapper for the daily news CLI.

Preferred usage:
    uv run news run --preset NAME
    NEWS_SOURCE_SCOPE=peripheral uv run news run
"""

from news_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
