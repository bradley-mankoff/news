"""Master script for the daily news pipeline.

Usage:
    uv run todays_news.py
    NEWS_MODEL=qwen-9b-dense uv run todays_news.py
    NEWS_RUN_MODE=local-prod uv run todays_news.py
    NEWS_DEV=0 uv run todays_news.py
    uv run todays_news.py --local-prod
    uv run todays_news.py --model-server-command
    uv run todays_news.py --serve-unsubscribe

The script delegates to ``news_pipeline`` modules so source loading, topic
discovery, article gathering, synthesis, email, and run diagnostics can be
edited independently. Normal report runs start the matching local model server
automatically and stop the managed server when the run exits.
"""

from news_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
