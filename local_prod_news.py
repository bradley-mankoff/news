"""Run the daily news pipeline in local-production review mode.

This uses the production source/topic scope and shared URL history, but sends
the generated report only to NEWS_DEV_RECIPIENT.
"""

from __future__ import annotations

import os


os.environ["NEWS_RUN_MODE"] = "local-prod"

from news_pipeline.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
