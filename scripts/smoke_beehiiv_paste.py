"""Smoke test for the beehiiv paste writer.

Runs the production finalizer wiring against a fresh temp dir, records a
report body, calls finish(), and asserts the beehiiv paste file lands at
the expected sibling-of-output_dir path with the expected content.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.config import load_runtime_config
from news_pipeline.diagnostics import RunDiagnostics
from news_pipeline.pipeline import _new_run_finalizer  # type: ignore[attr-defined]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="news-beehiiv-smoke-") as tmp:
        tmp_path = Path(tmp)
        # Point the runtime config at a fresh root_dir so output_dir
        # lives inside the temp dir.
        config = load_runtime_config(
            overrides={
                "NEWS_OUTPUT_DIR": str(tmp_path / "output" / "daily_outputs"),
                "NEWS_HISTORY_DB": str(tmp_path / "output" / "history" / "news_history.duckdb"),
            },
        )
        diagnostics = RunDiagnostics(
            run_started_at=datetime.now().isoformat(timespec="seconds"),
            settings={
                "preset_id": "smoke",
                "source_count": 0,
                "history_db_path": str(config.history_db_path),
                "latest_run_markdown_path": str(config.latest_run_markdown_path),
                "run_staging_dir": str(config.run_staging_dir),
                "url_reuse_blocking_enabled": False,
            },
        )
        finalizer = _new_run_finalizer(diagnostics, config)
        body = (
            "Daily News Summary\n"
            "==================\n\n"
            "Smoke-test content for the beehiiv paste writer.\n"
        )
        finalizer.record_report_body(body)
        diagnostics.event("completed")
        finalizer.finish()

        beehiiv_dir = tmp_path / "output" / "beehiiv"
        expected = beehiiv_dir / f"{config.timestamp[:10]}.md"
        if not expected.exists():
            print(f"FAIL: expected file not found at {expected}", file=sys.stderr)
            return 1
        if expected.read_text(encoding="utf-8") != body:
            print(f"FAIL: file content does not match body at {expected}", file=sys.stderr)
            return 1
        # Confirm cleanup did not sweep the beehiiv file.
        leftover = list((tmp_path / "output" / "daily_outputs").rglob("*.md"))
        if any(path.name == expected.name for path in leftover):
            print("FAIL: cleanup moved the beehiiv file into the daily_outputs dir", file=sys.stderr)
            return 1
        # Confirm the daily_outputs cleanup left the run review intact.
        review = tmp_path / "output" / "daily_outputs" / "latest_run.md"
        if not review.exists():
            print(f"FAIL: run review missing at {review}", file=sys.stderr)
            return 1
        print(f"OK: beehiiv paste file at {expected}")
        print(f"OK: beehiiv dir is sibling of output_dir: {beehiiv_dir.parent == tmp_path / 'output'}")
        print(f"OK: daily_outputs review still present: {review}")
        # Tee the file for the user to eyeball.
        shutil.copy(expected, Path.cwd() / "smoke_beehiiv_paste.md")
        print("OK: copied sample to ./smoke_beehiiv_paste.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
