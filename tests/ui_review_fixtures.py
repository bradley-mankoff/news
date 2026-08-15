"""Shared real-server fixture for Report Review route and browser tests.

The fixture owns a temporary output/history/schedule environment, writes
deterministic completed and failed run records through the real
``write_run_history`` and ``write_okf_run_bundle`` adapters, writes the
rolling ``latest_run.*`` artifacts, and serves the real ``NewsUIServer`` on
an ephemeral localhost port. Only environment variables are patched; every
route reader (DuckDB, OKF bundle, rolling files) is exercised for real.

The terminal-refresh browser test substitutes only ``ui_module.build_command``
with :meth:`ReviewFixture.child_command`, which launches a tiny child process
that atomically replaces the rolling latest artifacts and exits successfully.
No model pipeline, SMTP, network source fetch, or developer output path is
ever touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

from news_pipeline import ui as ui_module
from news_pipeline.diagnostics import RunDiagnostics
from news_pipeline.history_store import connect, write_run_history
from news_pipeline.okf import write_okf_run_bundle
from news_pipeline.ui import NewsUIHandler, NewsUIServer

COMPLETED_RUN_ID = "2026-08-10_10-00-00"
FAILED_RUN_ID = "2026-08-11_10-00-00"
REFRESHED_RUN_ID = "2026-08-12_10-00-00"

HISTORICAL_REPORT_BODY = (
    "Historical report with <script>alert('historical')</script>"
)
LATEST_REPORT_BODY = "Latest report with <script>alert('latest')</script>"
REFRESHED_REPORT_BODY = "Refreshed report with <script>alert('latest')</script>"


def _settings() -> dict[str, Any]:
    """Deterministic settings shared by every fixture record."""
    return {
        "preset_id": "daily",
        "url_reuse_blocking_enabled": False,
        "source_count": 1,
        "model": "gemma-e2b-tiny",
        "model_label": "default_model",
        "story_cluster_similarity_threshold": 0.31,
        "story_selection_overlap_threshold": 0.25,
        "story_embedding_dedup_threshold": 0.85,
        "min_articles_per_story": 2,
        "max_stories": 4,
    }


def _delivery_details() -> dict[str, Any]:
    """Explicit non-empty delivery outcome shared by every fixture record."""
    return {
        "status": "failed",
        "recipients": ["reader@example.com"],
        "reason": "delivery refused for: reader@example.com",
        "error_type": "SMTPRecipientsRefused",
        "error_message": "refused recipient",
        "phase": "send",
        "accepted_recipients": [],
        "rejected_recipients": ["reader@example.com"],
    }


def completed_diagnostics(started_at: str) -> RunDiagnostics:
    """Deterministic completed run with a recorded report and delivery."""
    diagnostics = RunDiagnostics(
        run_started_at=started_at,
        settings=_settings(),
        events=[
            {"at": started_at, "label": "story_clustering", "story_count": 1},
            {"at": started_at, "label": "completed"},
        ],
    )
    diagnostics.record_report(path="output/daily_outputs/latest_run.md")
    diagnostics.record_delivery(
        "failed",
        recipients=["reader@example.com"],
        reason="delivery refused for: reader@example.com",
        error_type="SMTPRecipientsRefused",
        error_message="refused recipient",
        phase="send",
        rejected_recipients=["reader@example.com"],
    )
    return diagnostics


def failed_diagnostics(started_at: str) -> RunDiagnostics:
    """Deterministic failed run with no report and an explicit delivery."""
    diagnostics = RunDiagnostics(
        run_started_at=started_at,
        settings=_settings(),
        events=[
            {"at": started_at, "label": "story_clustering", "story_count": 1},
            {
                "at": started_at,
                "label": "failed",
                "error_type": "RuntimeError",
                "error_message": "boom",
            },
        ],
    )
    diagnostics.record_delivery(
        "failed",
        recipients=["reader@example.com"],
        reason="delivery refused",
        error_type="RuntimeError",
        error_message="smtp down",
        phase="send",
        rejected_recipients=["reader@example.com"],
    )
    return diagnostics


def _latest_details(*, completed: bool) -> dict[str, Any]:
    """Rolling latest details; only ``settings`` is malformed when completed.

    The wrong-shaped ``settings`` value (a JSON list instead of an object) is
    deliberately unrelated to events/report/delivery so #160's field-specific
    recovery stays observable while valid status data remains readable.
    """
    if completed:
        started_at = "2026-08-09T10:00:00"
        return {
            "run_started_at": started_at,
            "settings": ["not", "an", "object"],  # wrong-shaped on purpose
            "report_generated": True,
            "reports": [{"path": "output/daily_outputs/latest_run.md"}],
            "delivery": _delivery_details(),
            "events": [{"at": started_at, "label": "completed"}],
        }
    started_at = "2026-08-08T10:00:00"
    return {
        "run_started_at": started_at,
        "settings": _settings(),
        "report_generated": False,
        "reports": [],
        "delivery": _delivery_details(),
        "events": [
            {"at": started_at, "label": "story_clustering", "story_count": 1},
            {
                "at": started_at,
                "label": "failed",
                "error_type": "RuntimeError",
                "error_message": "boom",
            },
        ],
    }


def write_latest_state(output_dir: Path, *, completed: bool) -> None:
    """Write deterministic rolling ``latest_run.md``/details for one state."""
    output_dir.mkdir(parents=True, exist_ok=True)
    details = _latest_details(completed=completed)
    report_text = LATEST_REPORT_BODY if completed else ""
    (output_dir / "latest_run.md").write_text(report_text, encoding="utf-8")
    (output_dir / "latest_run_details.json").write_text(
        json.dumps(details, indent=2) + "\n", encoding="utf-8"
    )


def _refreshed_latest_details() -> dict[str, Any]:
    """Completed rolling state the fake terminal child atomically installs."""
    return {
        "run_started_at": "2026-08-12T10:00:00",
        "settings": _settings(),
        "report_generated": True,
        "reports": [{"path": "output/daily_outputs/latest_run.md"}],
        "delivery": _delivery_details(),
        "events": [{"at": "2026-08-12T10:00:00", "label": "completed"}],
    }


def child_script_text() -> str:
    """Source of the deterministic terminal child used by the browser test.

    The child writes both rolling artifacts through sibling temporary files
    and ``os.replace`` so a concurrent review refresh can never observe
    half-written JSON, then exits successfully.
    """
    details = repr(_refreshed_latest_details())
    return f"""import json
import os
from pathlib import Path

output_dir = Path(os.environ["NEWS_OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
details = {details}
report = {json.dumps(REFRESHED_REPORT_BODY)}
tmp_details = output_dir / "latest_run_details.json.tmp-"
tmp_report = output_dir / "latest_run.md.tmp-"
tmp_details.write_text(json.dumps(details, indent=2) + "\\n", encoding="utf-8")
tmp_report.write_text(report, encoding="utf-8")
os.replace(tmp_details, output_dir / "latest_run_details.json")
os.replace(tmp_report, output_dir / "latest_run.md")
"""


class ReviewFixture:
    """Temporary artifacts plus a real ``NewsUIServer`` lifetime."""

    def __init__(self, *, latest_completed: bool = True) -> None:
        self.latest_completed = latest_completed
        self.completed_run_id = COMPLETED_RUN_ID
        self.failed_run_id = FAILED_RUN_ID
        self.root: Path | None = None
        self.output_dir: Path | None = None
        self.history_db: Path | None = None
        self.child_script: Path | None = None
        self.host = ""
        self.port = 0
        self.base_url = ""
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._env_patch: Any = None
        self.server: NewsUIServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ReviewFixture":
        self._tmp = tempfile.TemporaryDirectory(prefix="news-ui-review-")
        self.root = Path(self._tmp.name)
        assert self.root is not None
        self.output_dir = self.root / "daily_outputs"
        self.history_db = self.root / "history" / "news_history.duckdb"
        schedule_dir = self.root / "schedule"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        schedule_dir.mkdir(parents=True, exist_ok=True)
        self.child_script = self.root / "refresh_latest_child.py"
        self.child_script.write_text(child_script_text(), encoding="utf-8")

        completed = completed_diagnostics("2026-08-10T10:00:00")
        write_run_history(
            self.history_db,
            run_id=self.completed_run_id,
            diagnostics=completed,
            export_csv=False,
        )
        write_okf_run_bundle(
            self.history_db,
            run_id=self.completed_run_id,
            diagnostics=completed,
            report_body=HISTORICAL_REPORT_BODY,
        )
        failed = failed_diagnostics("2026-08-11T10:00:00")
        write_run_history(
            self.history_db,
            run_id=self.failed_run_id,
            diagnostics=failed,
            export_csv=False,
        )
        # Wrong-shaped persisted settings for the completed run only; every
        # scalar status/report/delivery column stays valid.
        with connect(self.history_db) as con:
            con.execute(
                "UPDATE runs SET settings_json = ? WHERE run_id = ?",
                ['["not", "a", "dict"]', self.completed_run_id],
            )
        write_latest_state(self.output_dir, completed=self.latest_completed)

        self._env_patch = patch.dict(
            os.environ,
            {
                "NEWS_OUTPUT_DIR": str(self.output_dir),
                "NEWS_HISTORY_DB": str(self.history_db),
                "NEWS_SCHEDULE_STATE": str(schedule_dir / "state.json"),
                "NEWS_SCHEDULE_LOCK": str(schedule_dir / "schedule.lock"),
                "NEWS_SCHEDULE_PLIST": str(schedule_dir / "schedule.plist"),
                "NEWS_SCHEDULE_LOG_DIR": str(schedule_dir / "logs"),
            },
        )
        self._env_patch.start()

        # Other tests in this process may leak fake run records into the
        # module-global RUN_MANAGER (e.g. the in-memory /api/run handler
        # test uses a stub thread that never spawns a child). A fresh fixture
        # must not inherit a phantom "active" run, or the real /api/run
        # would answer 409. Records that never spawned a real process are
        # test artifacts, never in-flight production runs.
        with ui_module.RUN_MANAGER.lock:
            ui_module.RUN_MANAGER.runs = {
                run_id: record
                for run_id, record in ui_module.RUN_MANAGER.runs.items()
                if not (record.is_active() and record.process is None)
            }

        self.server = NewsUIServer(("127.0.0.1", 0), NewsUIHandler)
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            name="news-ui-review-fixture",
            daemon=True,
        )
        self._thread.start()
        host, port = self.server.server_address[:2]
        self.host = str(host)
        self.port = int(port)
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self.server is not None:
                self.server.shutdown()
                if self._thread is not None:
                    self._thread.join(timeout=10)
        finally:
            if self.server is not None:
                self.server.server_close()
            if self._env_patch is not None:
                self._env_patch.stop()
            if self._tmp is not None:
                self._tmp.cleanup()

    def child_command(self) -> tuple[list[str], dict[str, str]]:
        """Terminal substitute command: run the atomic latest-refresh child.

        The child inherits the fixture's patched ``NEWS_OUTPUT_DIR`` from the
        server process environment, so no extra env entries are needed.
        """
        assert self.child_script is not None
        return [sys.executable, str(self.child_script)], {}
