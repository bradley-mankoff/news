"""Compatibility wrapper for the daily news CLI.

Preferred usage:
    uv run news dev
    uv run news dev --topics sports,science_space_tech
    uv run news local-prod
    uv run news loose-local-prod
    uv run news prod
"""

from news_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
