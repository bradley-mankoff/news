"""Scheduler module tests: validation, atomic state, plist shape, launchd
lifecycle, and the scheduled runner.

No real launch agent, model server, SMTP server, browser, or network is used:
launchctl calls are faked through the adapter and the pipeline entry point is
mocked. Temporary paths replace the user's real schedule state/plist.
"""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from news_pipeline import scheduler as sched
from news_pipeline.scheduler import (
    DailySchedule,
    DELIVERY_MODE_OWNER,
    LaunchdAdapter,
    LastScheduledRun,
    ScheduleError,
    ScheduleLock,
    ScheduleStateError,
    ScheduleStore,
    build_plist,
    capture_safe_env,
    disable_schedule,
    enable_schedule,
    parse_daily_time,
    reconcile_stale_running,
    run_scheduled,
    schedule_status,
    validate_schedule_spec,
)

_SECRETS = {
    "NEWS_SMTP_PASSWORD": "hunter2",
    "NEWS_UNSUBSCRIBE_SECRET": "unsub-secret",
    "NEWS_MODEL_API_KEY": "api-key-123",
}


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(
            os.environ,
            {
                "NEWS_SCHEDULE_STATE": str(self.tmp / "daily_schedule.json"),
                "NEWS_SCHEDULE_PLIST": str(self.tmp / "com.bradley-mankoff.news-daily-run.plist"),
                "NEWS_SCHEDULE_LOCK": str(self.tmp / "daily_schedule.lock"),
                "NEWS_SCHEDULE_LOG_DIR": str(self.tmp / "logs"),
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    # -- helpers -----------------------------------------------------------

    def _fake_adapter(self, *, supported: bool = True, load_state: str = "not_loaded") -> LaunchdAdapter:
        return LaunchdAdapter(launchctl="/fake/launchctl", uid=501)

    def _mock_launchd(self, adapter: LaunchdAdapter | None = None, **attrs) -> list:
        adapter = adapter or self._fake_adapter()
        patchers = [
            patch.object(sched, "LaunchdAdapter", return_value=adapter),
            patch.object(adapter, "supported", return_value=attrs.get("supported", True)),
            patch.object(adapter, "load_state", return_value=attrs.get("load_state", "not_loaded")),
            patch.object(adapter, "bootout", side_effect=attrs.get("bootout_side_effect")),
            patch.object(adapter, "bootstrap", side_effect=attrs.get("bootstrap_side_effect")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        return patchers

    def _enabled_schedule(self, time_value: str = "06:45", **kwargs) -> DailySchedule:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter, **kwargs)
        return enable_schedule(time_value, preset_id="default")

    def _store(self) -> ScheduleStore:
        return ScheduleStore()

    def _write_success_projection(self) -> None:
        schedule = self._store().load()
        output = Path(schedule.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "latest_run.md").write_text("# Report\n", encoding="utf-8")
        (output / "latest_run_details.json").write_text(
            json.dumps(
                {
                    "run_started_at": "2026-08-13T06:45:00",
                    "report_generated": True,
                    "reports": [{"title": "x"}],
                    "events": [{"label": "completed"}],
                    "delivery": {"status": "skipped: not_configured"},
                }
            ),
            encoding="utf-8",
        )
    # -- time and spec validation ------------------------------------------

    def test_parse_daily_time_accepts_boundaries_and_rejects_malformed(self) -> None:
        self.assertEqual(parse_daily_time("00:00"), (0, 0))
        self.assertEqual(parse_daily_time("23:59"), (23, 59))
        self.assertEqual(parse_daily_time("07:30"), (7, 30))
        for bad in ("24:00", "7:5", "7:05", "07:5", "", "  ", "abc", "07:60", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_daily_time(bad)

    def test_validate_schedule_spec_defaults_to_owner_and_filters_overrides(self) -> None:
        hour, minute, preset, mode, overrides = validate_schedule_spec(
            "08:15",
            overrides={"NEWS_MODEL": "gemma-4-12b-it-4bit", "NEWS_SMTP_PASSWORD": "x"},
        )
        self.assertEqual((hour, minute), (8, 15))
        self.assertEqual(preset, "")
        self.assertEqual(mode, DELIVERY_MODE_OWNER)
        self.assertEqual(overrides, {"NEWS_MODEL": "gemma-4-12b-it-4bit"})
        for explicit in ("disabled", "owner", "recipients"):
            self.assertEqual(
                validate_schedule_spec("08:15", delivery_mode=explicit)[3], explicit
            )
        with self.assertRaises(ValueError):
            validate_schedule_spec("08:15", delivery_mode="everyone")

    def test_validate_schedule_spec_unknown_preset_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown run preset"):
            validate_schedule_spec("08:15", preset_id="no-such-preset")
        # Empty preset means default settings (valid).
        validate_schedule_spec("08:15", preset_id="")
        validate_schedule_spec("08:15", preset_id="default")

    def test_capture_safe_env_excludes_secrets_markers_and_unknown(self) -> None:
        environ = {
            **_SECRETS,
            "NEWS_PRESET": "default",
            "NEWS_ACTIVE_PRESET": "default",
            "NEWS_MODEL": "gemma-4-12b-it-4bit",
            "NEWS_TOKEN_ENCODING": "o200k_base",
            "NEWS_OUTPUT_DIR": "/tmp/out",
            "SOME_UNREGISTERED_VAR": "keep-out",
            "NEWS_TOPIC_IDS": "1,2",
        }
        safe = capture_safe_env(environ)
        for secret_name in _SECRETS:
            self.assertNotIn(secret_name, safe)
        self.assertNotIn("NEWS_PRESET", safe)
        self.assertNotIn("NEWS_ACTIVE_PRESET", safe)
        self.assertNotIn("SOME_UNREGISTERED_VAR", safe)
        self.assertNotIn("NEWS_TOPIC_IDS", safe)
        self.assertEqual(safe["NEWS_MODEL"], "gemma-4-12b-it-4bit")
        self.assertEqual(safe["NEWS_TOKEN_ENCODING"], "o200k_base")
        self.assertEqual(safe["NEWS_OUTPUT_DIR"], "/tmp/out")

    # -- atomic store -------------------------------------------------------

    def test_schedule_store_absent_defaults_and_corrupt_state_fails_closed(self) -> None:
        store = self._store()
        self.assertIsNone(store.load())
        default = store.load_or_default()
        self.assertFalse(default.enabled)
        self.assertEqual(default.delivery_mode, DELIVERY_MODE_OWNER)
        self.assertEqual(default.last_run.status, "never")

        self.tmp.joinpath("daily_schedule.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ScheduleStateError):
            store.load()
        self.tmp.joinpath("daily_schedule.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ScheduleStateError):
            store.load()
        self.tmp.joinpath("daily_schedule.json").write_text(
            json.dumps({"hour": 25, "minute": 0}), encoding="utf-8"
        )
        with self.assertRaises(ScheduleStateError):
            store.load()
        self.tmp.joinpath("daily_schedule.json").write_text(
            json.dumps({"delivery_mode": "everyone"}), encoding="utf-8"
        )
        with self.assertRaises(ScheduleStateError):
            store.load()

    def test_schedule_store_atomic_save_permissions_and_last_run_preserved(self) -> None:
        store = self._store()
        schedule = store.load_or_default()
        schedule = replace(
            schedule,
            enabled=True,
            hour=6,
            minute=45,
            last_run=LastScheduledRun(status="completed", run_id="run-1"),
        )
        store.save(schedule)
        self.assertEqual(store.load().last_run.run_id, "run-1")
        mode = os.stat(self.tmp / "daily_schedule.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertFalse((self.tmp / "daily_schedule.json.tmp").exists())
        # A later settings update preserves the last-run projection.
        updated = replace(store.load(), hour=7, minute=0)
        store.save(updated)
        self.assertEqual(store.load().hour, 7)
        self.assertEqual(store.load().last_run.run_id, "run-1")

    def test_schedule_store_rejects_semantically_unsafe_state(self) -> None:
        valid = self._store().load_or_default().to_dict()
        invalid_records = [
            {**valid, "enabled": "false"},
            {**valid, "schema_version": 99},
            {**valid, "base_env": {"NEWS_SMTP_PASSWORD": "leak"}},
            {**valid, "overrides": ["NEWS_MODEL"]},
            {**valid, "last_run": {**valid["last_run"], "status": "active"}},
        ]
        for raw in invalid_records:
            with self.subTest(raw=raw):
                self.tmp.joinpath("daily_schedule.json").write_text(
                    json.dumps(raw), encoding="utf-8"
                )
                with self.assertRaises(ScheduleStateError):
                    self._store().load()

        self.tmp.joinpath("daily_schedule.json").write_text(
            json.dumps({**valid, "enabled": "false"}), encoding="utf-8"
        )
        with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
            self.assertEqual(run_scheduled(), 2)
            run_pipeline.assert_not_called()

    # -- plist generation ---------------------------------------------------

    def test_build_plist_shape_and_no_secrets(self) -> None:
        schedule = DailySchedule(
            enabled=True,
            hour=6,
            minute=45,
            root_dir="/repo/root",
            python_executable="/abs/python3",
        )
        with patch.dict(os.environ, {**_SECRETS, "PATH": "/usr/bin:/bin"}, clear=False):
            data = build_plist(schedule)
        payload = plistlib.loads(data)
        self.assertEqual(payload["Label"], sched.SCHEDULE_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            ["/abs/python3", "-m", "news_pipeline.cli", "schedule", "run"],
        )
        self.assertEqual(payload["WorkingDirectory"], "/repo/root")
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 6, "Minute": 45})
        self.assertFalse(payload["RunAtLoad"])
        self.assertNotIn("KeepAlive", payload)
        self.assertTrue(str(payload["StandardOutPath"]).endswith("run.stdout.log"))
        self.assertTrue(str(payload["StandardErrorPath"]).endswith("run.stderr.log"))
        plist_text = data.decode("utf-8", errors="replace")
        for secret_value in _SECRETS.values():
            self.assertNotIn(secret_value, plist_text)
        for secret_name in _SECRETS:
            self.assertNotIn(secret_name, plist_text)
        env = payload["EnvironmentVariables"]
        self.assertIn("PATH", env)
        self.assertEqual(env["HOME"], str(Path.home()))

    # -- launchd adapter ----------------------------------------------------

    def test_launchd_load_state_statuses(self) -> None:
        adapter = LaunchdAdapter(launchctl="/fake/launchctl", uid=501)
        with patch.object(adapter, "supported", return_value=True):
            for returncode, expected in ((0, "loaded"), (113, "not_loaded"), (7, "unknown")):
                with self.subTest(returncode=returncode):
                    with patch.object(
                        sched.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=returncode),
                    ):
                        self.assertEqual(adapter.load_state(), expected)
            with patch.object(
                sched.subprocess, "run", side_effect=OSError("launchctl missing")
            ):
                self.assertEqual(adapter.load_state(), "unknown")
        with patch.object(adapter, "supported", return_value=False):
            self.assertEqual(adapter.load_state(), "unavailable")

    def test_launchd_bootout_tolerates_absent_job(self) -> None:
        adapter = LaunchdAdapter(launchctl="/fake/launchctl", uid=501)
        with patch.object(
            sched.subprocess, "run", return_value=SimpleNamespace(returncode=113)
        ), patch.object(adapter, "load_state", return_value="not_loaded"):
            adapter.bootout()  # must not raise

    def test_launchd_bootstrap_failure_raises_bounded_error(self) -> None:
        adapter = LaunchdAdapter(launchctl="/fake/launchctl", uid=501)
        with patch.object(
            sched.subprocess, "run", return_value=SimpleNamespace(returncode=5)
        ):
            with self.assertRaisesRegex(ScheduleError, "bootstrap failed"):
                adapter.bootstrap(self.tmp / "job.plist")

    def test_launchd_adapter_bounds_calls_and_subprocess_failures(self) -> None:
        adapter = LaunchdAdapter(launchctl="/fake/launchctl", uid=501)
        plist = self.tmp / "job.plist"
        with patch.object(
            sched.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            adapter.bootstrap(plist)
        run.assert_called_once_with(
            ["/fake/launchctl", "bootstrap", "gui/501", str(plist)],
            capture_output=True,
            text=True,
            timeout=15,
        )

        for operation in ("bootstrap", "bootout"):
            with self.subTest(operation=operation):
                with patch.object(
                    sched.subprocess,
                    "run",
                    side_effect=sched.subprocess.TimeoutExpired("launchctl", 15),
                ):
                    with self.assertRaisesRegex(ScheduleError, "launchctl is unavailable"):
                        if operation == "bootstrap":
                            adapter.bootstrap(plist)
                        else:
                            adapter.bootout()

    def test_schedule_lock_fails_closed_when_lock_file_is_unavailable(self) -> None:
        lock = ScheduleLock(self.tmp / "daily_schedule.lock")
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ScheduleError, "lock is unavailable"):
                lock.acquire(timeout=0.0)

    # -- enable/disable lifecycle ------------------------------------------

    def test_enable_schedule_persists_state_and_bootstraps(self) -> None:
        order: list[str] = []

        def record_bootout(*args, **kwargs) -> None:
            order.append("bootout")

        def record_bootstrap(*args, **kwargs) -> None:
            order.append("bootstrap")

        adapter = self._fake_adapter()
        self._mock_launchd(
            adapter, bootout_side_effect=record_bootout, bootstrap_side_effect=record_bootstrap
        )
        schedule = enable_schedule(
            "06:45", preset_id="default", delivery_mode="disabled"
        )
        self.assertTrue(schedule.enabled)
        self.assertEqual((schedule.hour, schedule.minute), (6, 45))
        self.assertEqual(schedule.preset_id, "default")
        self.assertEqual(schedule.delivery_mode, "disabled")
        self.assertEqual(schedule.launchd_status, "loaded")
        self.assertTrue((self.tmp / "com.bradley-mankoff.news-daily-run.plist").exists())
        self.assertTrue((self.tmp / "logs").is_dir())
        self.assertEqual(os.stat(self.tmp / "logs").st_mode & 0o777, 0o700)
        self.assertEqual(adapter.bootout.call_count, 1)
        self.assertEqual(adapter.bootstrap.call_count, 1)
        # Replacement ordering: bootout the old job before bootstrapping.
        self.assertEqual(order, ["bootout", "bootstrap"])

    def test_enable_schedule_unsupported_platform_fails_closed(self) -> None:
        adapter = self._fake_adapter(supported=False)
        self._mock_launchd(adapter, supported=False)
        with self.assertRaisesRegex(ScheduleError, "macOS launchd"):
            enable_schedule("06:45")
        self.assertFalse((self.tmp / "daily_schedule.json").exists())

    def test_enable_schedule_respects_lifecycle_lock(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        lock = ScheduleLock()
        self.assertTrue(lock.acquire(timeout=0.0))
        try:
            with self.assertRaisesRegex(ScheduleError, "Another schedule operation"):
                enable_schedule("06:45")
        finally:
            lock.release()
        self.assertFalse((self.tmp / "daily_schedule.json").exists())

    def test_enable_schedule_bootstrap_failure_is_not_healthy(self) -> None:
        adapter = self._fake_adapter(load_state="not_loaded")
        self._mock_launchd(
            adapter,
            bootstrap_side_effect=ScheduleError("launchctl bootstrap failed (exit 5); the schedule is not active."),
        )
        with self.assertRaises(ScheduleError):
            enable_schedule("06:45")
        state = self._store().load()
        self.assertIsNotNone(state)
        self.assertTrue(state.enabled)
        self.assertEqual(state.launchd_status, "not_loaded")

    def test_enable_schedule_unknown_preset_writes_nothing(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        with self.assertRaisesRegex(ValueError, "Unknown run preset"):
            enable_schedule("06:45", preset_id="no-such-preset")
        self.assertFalse((self.tmp / "daily_schedule.json").exists())
        self.assertFalse((self.tmp / "com.bradley-mankoff.news-daily-run.plist").exists())
        self.assertEqual(adapter.bootstrap.call_count, 0)

    def test_disable_schedule_is_idempotent_and_removes_plist(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule("06:45")
        disabled = disable_schedule()
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.launchd_status, "not_loaded")
        self.assertFalse((self.tmp / "com.bradley-mankoff.news-daily-run.plist").exists())
        # Second disable is a no-op success.
        disabled_again = disable_schedule()
        self.assertFalse(disabled_again.enabled)

    def test_disable_schedule_recovers_from_corrupt_state(self) -> None:
        adapter = self._fake_adapter(load_state="loaded")
        self._mock_launchd(adapter)
        plist = self.tmp / "com.bradley-mankoff.news-daily-run.plist"
        plist.write_text("stale plist", encoding="utf-8")
        self.tmp.joinpath("daily_schedule.json").write_text("{broken", encoding="utf-8")

        disabled = disable_schedule()

        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.launchd_status, "not_loaded")
        self.assertFalse(plist.exists())
        self.assertFalse(self._store().load().enabled)
        adapter.bootout.assert_called_once_with()
    def test_disable_schedule_bootout_failure_keeps_state_enabled(self) -> None:
        calls = {"n": 0}

        def bootout_fails_second_call() -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise ScheduleError(
                    "launchctl bootout failed (exit 7); the schedule job may still be loaded."
                )

        adapter = self._fake_adapter(load_state="loaded")
        self._mock_launchd(adapter, bootout_side_effect=bootout_fails_second_call)
        enable_schedule("06:45")
        with self.assertRaises(ScheduleError):
            disable_schedule()
        state = self._store().load()
        self.assertTrue(state.enabled)

    def test_enable_preserves_last_run_projection(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule("06:45")
        store = self._store()
        store.save(replace(store.load(), last_run=LastScheduledRun(status="completed", run_id="run-9")))
        enable_schedule("07:15", preset_id="default")
        self.assertEqual(self._store().load().last_run.run_id, "run-9")
        self.assertEqual(self._store().load().hour, 7)

    # -- scheduled runner ---------------------------------------------------

    def test_run_scheduled_disabled_and_absent_noop(self) -> None:
        # Absent state: no-op, pipeline untouched.
        with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
            self.assertEqual(run_scheduled(), 0)
            run_pipeline.assert_not_called()
        # Disabled state: no-op, pipeline untouched.
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule("06:45")
        disable_schedule()
        with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
            self.assertEqual(run_scheduled(), 0)
            run_pipeline.assert_not_called()

    def test_run_scheduled_corrupt_state_fails_closed(self) -> None:
        self.tmp.joinpath("daily_schedule.json").write_text("{broken", encoding="utf-8")
        with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
            self.assertEqual(run_scheduled(), 2)
            run_pipeline.assert_not_called()

    def test_run_scheduled_unknown_preset_fails_closed(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule("06:45", preset_id="default")
        store = self._store()
        store.save(replace(store.load(), preset_id="gone-preset"))
        with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
            self.assertEqual(run_scheduled(), 2)
            run_pipeline.assert_not_called()
        last = self._store().load().last_run
        self.assertEqual(last.status, "failed")
        self.assertEqual(
            last.error_message,
            "Scheduled Run Session failed; inspect canonical run history.",
        )

    def test_run_scheduled_success_projection(self) -> None:
        self._enabled_schedule()
        out = self.tmp / "out"
        out.mkdir()
        (out / "latest_run.md").write_text("# Report body\n", encoding="utf-8")
        details_path = out / "latest_run_details.json"
        details_path.write_text(
            json.dumps(
                {
                    "run_started_at": "2026-08-13T06:45:00",
                    "report_generated": True,
                    "reports": [{"title": "x"}],
                    "delivery": {"status": "skipped: not_configured"},
                    "events": [
                        {"label": "completed", "at": "2026-08-13T06:50:00", "line": "done"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = self._store()
        store.save(replace(store.load(), output_dir=str(out)))
        def write_current_details() -> None:
            details = json.loads(details_path.read_text(encoding="utf-8"))
            details["run_started_at"] = "2026-08-13T06:46:00"
            details_path.write_text(json.dumps(details), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False), patch(
            "news_pipeline.pipeline.run_pipeline",
            side_effect=write_current_details,
        ) as run_pipeline:
            self.assertEqual(run_scheduled(), 0)
            run_pipeline.assert_called_once_with()
        last = self._store().load().last_run
        self.assertEqual(last.status, "completed")
        self.assertEqual(last.run_id, "2026-08-13_06-46-00")
        self.assertEqual(last.run_status, "completed")
        self.assertEqual(last.report_status, "available")
        self.assertEqual(last.delivery_status, "skipped: not_configured")
        self.assertEqual(last.error_message, "")

    def test_enable_schedule_preset_wins_over_ambient_settings_and_resolves_paths(self) -> None:
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        output_dir = self.tmp / "custom-output"
        history_db = self.tmp / "custom-history" / "news.duckdb"
        with patch.dict(
            os.environ,
            {
                "NEWS_SOURCE_SCOPE": "core",
                "NEWS_OUTPUT_DIR": str(output_dir),
                "NEWS_HISTORY_DB": str(history_db),
            },
            clear=False,
        ):
            schedule = enable_schedule("06:45", preset_id="default")

        self.assertEqual(schedule.base_env.get("NEWS_SOURCE_SCOPE"), None)
        self.assertEqual(schedule.output_dir, str(output_dir))
        self.assertEqual(schedule.history_db_path, str(history_db))
        with patch.dict(os.environ, {}, clear=False), patch(
            "news_pipeline.pipeline.run_pipeline"
        ) as run_pipeline:
            # The stored path is the path used by projection, not the default.
            self.assertEqual(run_scheduled(), 1)
            run_pipeline.assert_called_once_with()
        self.assertEqual(self._store().load().last_run.error_type, "ProjectionError")

    def test_run_scheduled_failure_does_not_copy_previous_projection(self) -> None:
        self._enabled_schedule()
        out = self.tmp / "out"
        out.mkdir()
        (out / "latest_run.md").write_text("# Previous report\n", encoding="utf-8")
        (out / "latest_run_details.json").write_text(
            json.dumps(
                {
                    "run_started_at": "2026-08-12T06:45:00",
                    "report_generated": True,
                    "reports": [{"title": "old"}],
                    "events": [{"label": "completed"}],
                    "delivery": {"status": "sent"},
                }
            ),
            encoding="utf-8",
        )
        store = self._store()
        store.save(replace(store.load(), output_dir=str(out)))
        with patch.object(sched, "_load_pipeline_module", side_effect=ImportError("boom")):
            self.assertEqual(run_scheduled(), 1)
        last = self._store().load().last_run
        self.assertEqual(last.status, "failed")
        self.assertEqual(last.run_id, "")
        self.assertEqual(last.report_status, "unavailable")
        self.assertEqual(last.delivery_status, "")

    def test_run_scheduled_terminal_state_write_failure_is_controlled(self) -> None:
        self._enabled_schedule()
        store = self._store()
        with patch.object(
            sched,
            "ScheduleStore",
            return_value=store,
        ), patch.object(
            store,
            "save",
            side_effect=[None, OSError("disk full")],
        ), patch.object(
            sched,
            "_load_pipeline_module",
            side_effect=ImportError("pipeline unavailable"),
        ):
            self.assertEqual(run_scheduled(), 2)
    def test_run_scheduled_failure_records_bounded_redacted_error(self) -> None:
        self._enabled_schedule()
        with patch.dict(os.environ, {**_SECRETS}, clear=False), patch(
            "news_pipeline.pipeline.run_pipeline",
            side_effect=ValueError("config boom hunter2 api-key-123"),
        ) as run_pipeline:
            self.assertEqual(run_scheduled(), 1)
            run_pipeline.assert_called_once_with()
        last = self._store().load().last_run
        self.assertEqual(last.status, "failed")
        self.assertEqual(last.run_status, "failed")
        self.assertEqual(last.error_type, "ValueError")
        self.assertNotIn("hunter2", last.error_message)
        self.assertNotIn("api-key-123", last.error_message)
        self.assertEqual(
            last.error_message,
            "Scheduled Run Session failed; inspect canonical run history.",
        )

    def test_run_scheduled_setup_failure_gets_terminal_projection(self) -> None:
        self._enabled_schedule()
        with patch.object(
            sched,
            "_load_pipeline_module",
            side_effect=ImportError("pipeline import failed"),
        ):
            self.assertEqual(run_scheduled(), 1)
        last = self._store().load().last_run
        self.assertEqual(last.status, "failed")
        self.assertEqual(last.error_type, "ImportError")
        self.assertNotEqual(last.status, "running")

    def test_run_scheduled_projection_failure_gets_terminal_projection(self) -> None:
        self._enabled_schedule()
        with patch("news_pipeline.pipeline.run_pipeline"), patch.object(
            sched,
            "_read_latest_run_projection",
            side_effect=ValueError("malformed details"),
        ):
            self.assertEqual(run_scheduled(), 1)
        last = self._store().load().last_run
        self.assertEqual(last.status, "failed")
        self.assertEqual(last.error_type, "ProjectionError")
        self.assertEqual(last.report_status, "unavailable")

    def test_run_scheduled_delivery_mode_forced_and_precedence(self) -> None:
        # Isolate the ambient run settings so the assertions are deterministic.
        for name in ("NEWS_SOURCE_SCOPE", "NEWS_MODEL_BASE_URL", "NEWS_DELIVERY_MODE"):
            os.environ.pop(name, None)
        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule(
            "06:45",
            preset_id="default",
            delivery_mode="disabled",
            overrides={"NEWS_MODEL_BASE_URL": "http://127.0.0.1:9999/v1"},
        )
        store = self._store()
        store.save(replace(store.load(), output_dir=str(self.tmp / "delivery-out")))
        run_env: dict[str, str] = {}
        with patch.dict(os.environ, {}, clear=False), patch(
            "news_pipeline.pipeline.run_pipeline",
            side_effect=self._write_success_projection,
        ):
            self.assertEqual(run_scheduled(), 0)
            run_env = dict(os.environ)
        # Schedule delivery mode wins over preset/ambient values.
        self.assertEqual(run_env.get("NEWS_DELIVERY_MODE"), "disabled")
        # Explicit override wins over the preset/base environment.
        self.assertEqual(
            run_env.get("NEWS_MODEL_BASE_URL"), "http://127.0.0.1:9999/v1"
        )
        # Preset values are applied and the preset marker is set in the run
        # environment (never persisted in state).
        self.assertEqual(run_env.get("NEWS_SOURCE_SCOPE"), "peripheral")
        self.assertEqual(run_env.get("NEWS_PRESET"), "default")
        stored = self._store().load()
        self.assertNotIn("NEWS_PRESET", stored.base_env)
        self.assertNotIn("NEWS_PRESET", stored.overrides)

    def test_run_scheduled_lock_prevents_duplicate_execution(self) -> None:
        self._enabled_schedule()
        store = self._store()
        store.save(replace(store.load(), output_dir=str(self.tmp / "lock-out")))
        lock = ScheduleLock()
        self.assertTrue(lock.acquire(timeout=0.0))
        try:
            with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
                self.assertEqual(run_scheduled(), 1)
                run_pipeline.assert_not_called()
        finally:
            lock.release()
        # After release a second invocation proceeds.
        with patch.dict(os.environ, {}, clear=False), patch(
            "news_pipeline.pipeline.run_pipeline",
            side_effect=self._write_success_projection,
        ):
            self.assertEqual(run_scheduled(), 0)

    def test_schedule_lock_serializes_concurrent_owners(self) -> None:
        first = ScheduleLock()
        second = ScheduleLock()
        self.assertTrue(first.acquire(timeout=0.0))
        self.assertFalse(second.acquire(timeout=0.0))
        first.release()
        self.assertTrue(second.acquire(timeout=0.0))
        second.release()

    def test_run_scheduled_uses_lock_derived_from_custom_state_path(self) -> None:
        custom_state = self.tmp / "custom" / "daily_schedule.json"
        custom_store = ScheduleStore(custom_state)
        custom_store.save(replace(custom_store.load_or_default(), enabled=True))
        custom_lock = ScheduleLock(custom_state.parent / sched.SCHEDULE_LOCK_FILENAME)
        self.assertTrue(custom_lock.acquire(timeout=0.0))
        try:
            with patch("news_pipeline.pipeline.run_pipeline") as run_pipeline:
                self.assertEqual(run_scheduled(state_path=custom_state), 1)
                run_pipeline.assert_not_called()
        finally:
            custom_lock.release()

    # -- status and reconciliation ------------------------------------------

    def test_schedule_status_payload_is_safe_and_bounded(self) -> None:
        payload = schedule_status()
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["error"], None)
        self.assertEqual(payload["time"], "07:00")
        self.assertEqual(payload["next_run_label"], "disabled")
        for forbidden in ("base_env", "overrides", "env", "plist_xml", "command"):
            self.assertNotIn(forbidden, payload)

        adapter = self._fake_adapter()
        self._mock_launchd(adapter)
        enable_schedule("06:45")
        payload = schedule_status()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["time"], "06:45")
        self.assertEqual(payload["launchd_status"], "not_loaded")
        self.assertIn("once daily", payload["next_run_label"])

    def test_schedule_status_corrupt_state_reports_error_not_enabled(self) -> None:
        self.tmp.joinpath("daily_schedule.json").write_text("{broken", encoding="utf-8")
        payload = schedule_status()
        self.assertFalse(payload["enabled"])
        self.assertIn("unreadable", payload["error"])

    def test_reconcile_stale_running_marks_dead_pid_interrupted(self) -> None:
        store = self._store()
        schedule = store.load_or_default()
        store.save(
            replace(
                schedule,
                enabled=True,
                last_run=LastScheduledRun(status="running", pid=999_999_999),
            )
        )
        reconciled = reconcile_stale_running(store.load(), store)
        self.assertEqual(reconciled.last_run.status, "interrupted")
        self.assertEqual(store.load().last_run.status, "interrupted")
        self.assertEqual(store.load().last_run.report_status, "")

    def test_reconcile_stale_running_keeps_live_pid(self) -> None:
        store = self._store()
        schedule = store.load_or_default()
        store.save(
            replace(
                schedule,
                enabled=True,
                last_run=LastScheduledRun(status="running", pid=os.getpid()),
            )
        )
        reconciled = reconcile_stale_running(store.load(), store)
        self.assertEqual(reconciled.last_run.status, "running")


if __name__ == "__main__":
    unittest.main()
