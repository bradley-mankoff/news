"""Daily personal-run automation for the desktop Daily News Application.

One per-user macOS launchd LaunchAgent plus one atomic local JSON schedule
record implement ADR 0012 Slice C (ADR 0013). The scheduler is a trigger and
lifecycle projection only: the existing Run Session, report, DuckDB/CSV
history, OKF bundle, and Delivery Profile remain the execution and persistence
authorities. This module never persists credentials, API keys, report text, or
raw launchctl output, and it never calls the pipeline at import time.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

try:  # Advisory cross-process lock; unavailable only on non-Unix platforms.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .config import (
    ACTIVE_PRESET_ENV_VAR,
    DELIVERY_MODE_ENV_VAR,
    DELIVERY_MODE_OWNER,
    DELIVERY_MODES,
    PRESET_ENV_VAR,
    PRESET_MARKER_ENV_VARS,
    REMOVED_TOPIC_ENV_VARS,
    ROOT_DIR,
    _normalize_delivery_mode,
    normalize_preset_id,
    run_preset_env,
    runtime_knob_registry,
)
from .diagnostics import run_status_from_events

# ---------------------------------------------------------------------------
# Fixed schedule identity and default paths
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
SCHEDULE_LABEL = "com.bradley-mankoff.news-daily-run"
SCHEDULE_STATE_FILENAME = "daily_schedule.json"
SCHEDULE_LOCK_FILENAME = "daily_schedule.lock"
SCHEDULE_PLIST_FILENAME = "com.bradley-mankoff.news-daily-run.plist"
SCHEDULE_LOG_DIRNAME = "scheduled"

# Explicitly excluded credential/secret settings. The heuristic below also
# drops any future env whose name contains PASSWORD/SECRET/API_KEY/TOKEN
# (NEWS_TOKEN_ENCODING is a tokenizer name, not a credential, and is kept).
EXPLICIT_SECRET_ENV_VARS = frozenset(
    {
        "NEWS_SMTP_PASSWORD",
        "NEWS_UNSUBSCRIBE_SECRET",
        "NEWS_MODEL_API_KEY",
    }
)
SECRET_ENV_TOKENS = ("PASSWORD", "SECRET", "API_KEY", "TOKEN")

# Non-secret infrastructure settings launchd needs that are not part of the
# Run Settings knob registry (paths, transport host/user, tokenizer name).
KNOWN_SAFE_INFRA_ENV_VARS = (
    "NEWS_SOURCES_YAML",
    "NEWS_RECIPIENTS_YAML",
    "NEWS_OUTPUT_DIR",
    "NEWS_HISTORY_DB",
    "NEWS_HISTORY_EXPORT_CSV",
    "NEWS_ENV_JSON",
    "NEWS_EMAIL_FROM",
    "NEWS_PRIMARY_RECIPIENT",
    "NEWS_EMAIL_RECIPIENTS",
    "NEWS_SMTP_HOST",
    "NEWS_SMTP_PORT",
    "NEWS_SMTP_USERNAME",
    "NEWS_SMTP_USE_SSL",
    "NEWS_UNSUBSCRIBE_BASE_URL",
    "NEWS_UNSUBSCRIBE_HOST",
    "NEWS_UNSUBSCRIBE_PORT",
    "NEWS_TOKEN_ENCODING",
)


def _registered_safe_env_names() -> frozenset[str]:
    """Non-secret Run Settings from the runtime knob registry plus infra."""
    names = {
        str(knob["env"])
        for knob in runtime_knob_registry()
        if not knob.get("secret")
    }
    names.update(KNOWN_SAFE_INFRA_ENV_VARS)
    names.difference_update(PRESET_MARKER_ENV_VARS)
    names.difference_update(REMOVED_TOPIC_ENV_VARS)
    return frozenset(names)


_SAFE_ENV_NAMES = _registered_safe_env_names()


def _is_secret_env_name(name: str) -> bool:
    upper = str(name or "").upper()
    if upper in EXPLICIT_SECRET_ENV_VARS:
        return True
    if upper == "NEWS_TOKEN_ENCODING":
        return False
    return any(token in upper for token in SECRET_ENV_TOKENS)


def capture_safe_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Snapshot only registered non-secret settings (never credentials).

    ``NEWS_PRESET``/``NEWS_ACTIVE_PRESET`` markers are deliberately excluded:
    the schedule binds a preset by ID instead of freezing marker variables.
    """
    source = dict(os.environ if environ is None else environ)
    safe: dict[str, str] = {}
    for name in _SAFE_ENV_NAMES:
        if _is_secret_env_name(name):
            continue
        raw = source.get(name)
        if raw is None:
            continue
        text = str(raw)
        if text == "":
            continue
        safe[name] = text
    return safe


def _now_iso_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schedule data model (validated, immutable projections)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LastScheduledRun:
    """Bounded lifecycle projection of the most recent scheduled invocation.

    This is a status projection only; run/report/delivery evidence stays in
    the existing durable stores (latest_run.*, DuckDB/CSV history, OKF).
    """

    status: str = "never"  # never | running | completed | failed | interrupted
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    run_status: str = ""
    report_status: str = ""
    delivery_status: str = ""
    error_type: str = ""
    error_message: str = ""
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_status": self.run_status,
            "report_status": self.report_status,
            "delivery_status": self.delivery_status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "pid": self.pid,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "LastScheduledRun":
        if not isinstance(raw, dict):
            raise ScheduleStateError("Schedule state has an invalid last-run projection.")
        status = raw.get("status")
        if not isinstance(status, str) or status not in {
            "never",
            "running",
            "completed",
            "failed",
            "interrupted",
        }:
            raise ScheduleStateError("Schedule state has an invalid last-run status.")
        pid = raw.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
            raise ScheduleStateError("Schedule state has an invalid last-run PID.")
        text_fields = (
            "run_id",
            "started_at",
            "finished_at",
            "run_status",
            "report_status",
            "delivery_status",
            "error_type",
            "error_message",
        )
        values: dict[str, str] = {}
        for name in text_fields:
            value = raw.get(name, "")
            if not isinstance(value, str):
                raise ScheduleStateError(
                    f"Schedule state has an invalid last-run {name}."
                )
            values[name] = value.strip()
        return cls(status=status, pid=pid, **values)


@dataclass(frozen=True)
class DailySchedule:
    """One validated daily schedule record (schema version 1).

    ``base_env``/``overrides`` hold only the safe non-secret snapshot captured
    by :func:`capture_safe_env`; credentials never enter this record.
    """

    schema_version: int = SCHEMA_VERSION
    enabled: bool = False
    hour: int = 7
    minute: int = 0
    preset_id: str = ""
    delivery_mode: str = DELIVERY_MODE_OWNER
    base_env: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    root_dir: str = ""
    python_executable: str = ""
    output_dir: str = ""
    history_db_path: str = ""
    updated_at: str = ""
    launchd_status: str = "unknown"
    last_run: LastScheduledRun = field(default_factory=LastScheduledRun)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "hour": self.hour,
            "minute": self.minute,
            "preset_id": self.preset_id,
            "delivery_mode": self.delivery_mode,
            "base_env": dict(self.base_env),
            "overrides": dict(self.overrides),
            "root_dir": self.root_dir,
            "python_executable": self.python_executable,
            "output_dir": self.output_dir,
            "history_db_path": self.history_db_path,
            "updated_at": self.updated_at,
            "launchd_status": self.launchd_status,
            "last_run": self.last_run.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DailySchedule":
        if not isinstance(raw, dict):
            raise ScheduleStateError("Schedule state must be a JSON object.")

        schema_version = raw.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != SCHEMA_VERSION
        ):
            raise ScheduleStateError("Schedule state has an unsupported schema version.")

        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise ScheduleStateError("Schedule state has an invalid enabled flag.")

        hour = raw.get("hour")
        minute = raw.get("minute")
        if (
            isinstance(hour, bool)
            or not isinstance(hour, int)
            or isinstance(minute, bool)
            or not isinstance(minute, int)
        ):
            raise ScheduleStateError("Schedule state has an invalid time.")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ScheduleStateError("Schedule state has an out-of-range time.")

        preset_id = raw.get("preset_id")
        if not isinstance(preset_id, str):
            raise ScheduleStateError("Schedule state has an invalid preset ID.")
        mode = raw.get("delivery_mode")
        if not isinstance(mode, str) or mode not in _canonical_delivery_modes():
            raise ScheduleStateError("Schedule state has an invalid delivery mode.")

        envs: dict[str, dict[str, str]] = {}
        for field_name in ("base_env", "overrides"):
            value = raw.get(field_name)
            if not isinstance(value, dict):
                raise ScheduleStateError(
                    f"Schedule state has an invalid {field_name} map."
                )
            checked: dict[str, str] = {}
            for name, setting in value.items():
                if (
                    not isinstance(name, str)
                    or name not in _SAFE_ENV_NAMES
                    or _is_secret_env_name(name)
                    or not isinstance(setting, str)
                ):
                    raise ScheduleStateError(
                        f"Schedule state contains an unsafe {field_name} setting."
                    )
                checked[name] = setting
            envs[field_name] = checked

        text_values: dict[str, str] = {}
        for field_name in (
            "root_dir",
            "python_executable",
            "output_dir",
            "history_db_path",
            "updated_at",
        ):
            value = raw.get(field_name)
            if not isinstance(value, str):
                raise ScheduleStateError(
                    f"Schedule state has an invalid {field_name}."
                )
            text_values[field_name] = value.strip()

        launchd_status = raw.get("launchd_status")
        if not isinstance(launchd_status, str) or launchd_status not in {
            "loaded",
            "not_loaded",
            "unavailable",
            "unknown",
        }:
            raise ScheduleStateError("Schedule state has an invalid launchd status.")
        if "last_run" not in raw:
            raise ScheduleStateError("Schedule state has no last-run projection.")

        return cls(
            schema_version=schema_version,
            enabled=enabled,
            hour=hour,
            minute=minute,
            preset_id=preset_id.strip(),
            delivery_mode=mode,
            base_env=envs["base_env"],
            overrides=envs["overrides"],
            **text_values,
            launchd_status=launchd_status,
            last_run=LastScheduledRun.from_dict(raw["last_run"]),
        )


def _canonical_delivery_modes() -> tuple[str, ...]:
    return DELIVERY_MODES


class ScheduleStateError(ValueError):
    """Raised when schedule state exists but is corrupt/unreadable."""


class ScheduleError(ValueError):
    """Controlled scheduler failure (launchd/platform), safe for display."""


class _ProjectionError(ScheduleError):
    """The pipeline completed without producing a current run projection."""
# ---------------------------------------------------------------------------
# Time and spec validation
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_daily_time(value: Any) -> tuple[int, int]:
    """Validate an ``HH:MM`` 24-hour local time; returns ``(hour, minute)``.

    ``00:00`` and ``23:59`` are accepted; ``24:00``, ``7:5``, blank, and
    malformed values are rejected.
    """
    raw = str(value or "").strip()
    match = _TIME_RE.match(raw)
    if not match:
        raise ValueError(
            "Schedule time must be HH:MM in 24-hour local time (e.g. 07:30)."
        )
    return int(match.group(1)), int(match.group(2))


def validate_schedule_spec(
    time_value: Any,
    *,
    preset_id: str = "",
    delivery_mode: str | None = None,
    overrides: Mapping[str, str] | None = None,
) -> tuple[int, int, str, str, dict[str, str]]:
    """Validate and canonicalize one schedule spec (fail closed).

    Returns ``(hour, minute, preset_id, delivery_mode, safe_overrides)``.
    A non-empty preset must exist in ``config/run_presets.yaml``; an empty
    preset means the normal default settings.
    """
    hour, minute = parse_daily_time(time_value)
    preset = normalize_preset_id(preset_id)
    if preset:
        # Raises ValueError for unknown/deleted presets before any state or
        # plist is written.
        run_preset_env(preset)
    mode = (
        DELIVERY_MODE_OWNER
        if delivery_mode in (None, "")
        else _normalize_delivery_mode(delivery_mode)
    )
    # ``None`` means no explicit schedule override. Do not accidentally
    # persist the UI/CLI process environment as overrides, because preset-owned
    # values must remain authoritative.
    safe_overrides = capture_safe_env(overrides or {})
    return hour, minute, preset, mode, safe_overrides


# ---------------------------------------------------------------------------
# Atomic durable store
# ---------------------------------------------------------------------------


def default_state_path() -> Path:
    raw = os.environ.get("NEWS_SCHEDULE_STATE", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".config" / "news" / SCHEDULE_STATE_FILENAME


def default_lock_path() -> Path:
    raw = os.environ.get("NEWS_SCHEDULE_LOCK", "").strip()
    if raw:
        return Path(raw)
    return default_state_path().parent / SCHEDULE_LOCK_FILENAME


def default_plist_path() -> Path:
    raw = os.environ.get("NEWS_SCHEDULE_PLIST", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "Library" / "LaunchAgents" / SCHEDULE_PLIST_FILENAME


def default_log_dir() -> Path:
    raw = os.environ.get("NEWS_SCHEDULE_LOG_DIR", "").strip()
    if raw:
        return Path(raw)
    return default_state_path().parent / SCHEDULE_LOG_DIRNAME


def _lock_path_for_state(state_path: Path | None) -> Path:
    """Resolve the lock alongside an explicit state path when one is given."""
    if state_path is not None:
        return state_path.parent / SCHEDULE_LOCK_FILENAME
    return default_lock_path()


def _set_private_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
        if os.stat(path).st_mode & 0o777 != mode:
            raise OSError("private mode verification failed")
    except OSError as exc:
        raise ScheduleError("Could not secure schedule files.") from exc


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScheduleError("Could not create the schedule log directory.") from exc
    _set_private_mode(path, 0o700)


def _write_private_file(path: Path, data: bytes) -> None:
    """Atomic sibling-temp write with restrictive permissions (mirrors
    ``automation/news_ui_runtime.py`` plus ``0600`` state/plist modes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_private_mode(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        _set_private_mode(tmp, 0o600)
        os.replace(tmp, path)
        _set_private_mode(path, 0o600)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class ScheduleStore:
    """One atomic JSON schedule record under ``~/.config/news``."""

    def __init__(self, state_path: Path | None = None):
        self.path = state_path or default_state_path()

    def load(self) -> DailySchedule | None:
        """Strict read: malformed/non-object state raises; absent defaults."""
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            raise ScheduleStateError(
                f"Schedule state is unreadable ({type(exc).__name__})."
            ) from exc
        return DailySchedule.from_dict(raw)

    def load_or_default(self) -> DailySchedule:
        schedule = self.load()
        if schedule is not None:
            return schedule
        return DailySchedule(
            root_dir=str(ROOT_DIR),
            python_executable=sys.executable,
            output_dir=str(ROOT_DIR / "output" / "daily_outputs"),
            history_db_path=str(ROOT_DIR / "output" / "history" / "news_history.duckdb"),
        )

    def save(self, schedule: DailySchedule) -> None:
        _write_private_file(
            self.path,
            (json.dumps(schedule.to_dict(), indent=2) + "\n").encode("utf-8"),
        )


class ScheduleLock:
    """Advisory cross-process lock serializing scheduled invocations."""

    def __init__(self, lock_path: Path | None = None):
        self.path = lock_path or default_lock_path()
        self._handle: Any = None

    def acquire(self, *, timeout: float = 0.0) -> bool:
        if fcntl is None:  # pragma: no cover - non-Unix fallback
            raise ScheduleError("Schedule locking is unavailable; no run was started.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise ScheduleError(
                "Schedule lock is unavailable; no scheduled run was started."
            ) from exc
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        except (OSError, AttributeError):
            pass
        try:
            handle.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# launchd plist generation and lifecycle adapter
# ---------------------------------------------------------------------------


def build_plist(
    schedule: DailySchedule,
    *,
    label: str = SCHEDULE_LABEL,
    python_executable: str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> bytes:
    """XML plist for the per-user LaunchAgent.

    argv-based ``ProgramArguments``, absolute interpreter, calendar interval,
    ``RunAtLoad: false``, and no ``KeepAlive``. Environment carries only safe
    PATH/HOME/stream settings — never credentials.
    """
    executable = python_executable or schedule.python_executable or sys.executable
    log_dir = default_log_dir()
    payload = {
        "Label": label,
        "ProgramArguments": [executable, "-m", "news_pipeline.cli", "schedule", "run"],
        "WorkingDirectory": schedule.root_dir or str(ROOT_DIR),
        "StartCalendarInterval": {"Hour": schedule.hour, "Minute": schedule.minute},
        "RunAtLoad": False,
        "StandardOutPath": str(stdout_path or log_dir / "run.stdout.log"),
        "StandardErrorPath": str(stderr_path or log_dir / "run.stderr.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
            "PYTHONUNBUFFERED": "1",
        },
    }
    return plistlib.dumps(payload)


class LaunchdAdapter:
    """Bounded ``launchctl`` adapter for the per-user ``gui/<uid>`` domain."""

    def __init__(self, *, launchctl: str | None = None, uid: int | None = None):
        self.launchctl_path = (
            launchctl or shutil.which("launchctl") or "/bin/launchctl"
        )
        self.uid = os.getuid() if uid is None else uid

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def job_target(self) -> str:
        return f"{self.domain}/{SCHEDULE_LABEL}"

    def supported(self) -> bool:
        return sys.platform == "darwin" and os.path.exists(self.launchctl_path)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScheduleError(
                "launchctl is unavailable or did not respond within 15 seconds."
            ) from exc

    def load_state(self) -> str:
        """Return ``loaded`` | ``not_loaded`` | ``unavailable`` | ``unknown``."""
        if not self.supported():
            return "unavailable"
        try:
            result = self._run([self.launchctl_path, "print", self.job_target])
        except ScheduleError:
            return "unknown"
        if result.returncode == 0:
            return "loaded"
        if result.returncode == 113:
            return "not_loaded"
        return "unknown"

    def bootstrap(self, plist_path: Path) -> None:
        result = self._run(
            [self.launchctl_path, "bootstrap", self.domain, str(plist_path)]
        )
        if result.returncode != 0:
            raise ScheduleError(
                f"launchctl bootstrap failed (exit {result.returncode}); "
                "the schedule is not active."
            )

    def bootout(self) -> None:
        """Boot out the fixed label; tolerates an already-absent job."""
        result = self._run([self.launchctl_path, "bootout", self.job_target])
        if result.returncode == 0:
            return
        if self.load_state() == "not_loaded":
            return  # idempotent removal of a missing old job
        raise ScheduleError(
            f"launchctl bootout failed (exit {result.returncode}); "
            "the schedule job may still be loaded."
        )


# ---------------------------------------------------------------------------
# Lifecycle operations
# ---------------------------------------------------------------------------


def _next_run_label(schedule: DailySchedule) -> str:
    if not schedule.enabled:
        return "disabled"
    return f"{schedule.hour:02d}:{schedule.minute:02d} (local time, once daily)"


def launchd_status(schedule: DailySchedule) -> str:
    adapter = LaunchdAdapter()
    if not adapter.supported():
        return "unavailable"
    if not schedule.enabled:
        return "not_loaded"
    return adapter.load_state()


def enable_schedule(
    time_value: Any,
    *,
    preset_id: str = "",
    delivery_mode: str | None = None,
    overrides: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    plist_path: Path | None = None,
) -> DailySchedule:
    """Validate, persist, and install one daily schedule (idempotent).

    The schedule lock serializes the complete state/plist/launchd transaction
    with scheduled runs and other lifecycle operations. A bootstrap failure
    raises :class:`ScheduleError` and leaves ``launchd_status`` reflecting
    reality — never a healthy claim.
    """
    hour, minute, preset, mode, safe_overrides = validate_schedule_spec(
        time_value,
        preset_id=preset_id,
        delivery_mode=delivery_mode,
        overrides=overrides,
    )
    adapter = LaunchdAdapter()
    if not adapter.supported():
        raise ScheduleError(
            "Daily automation requires macOS launchd; scheduling is unavailable "
            "on this platform."
        )
    lock = ScheduleLock(_lock_path_for_state(state_path))
    try:
        if not lock.acquire(timeout=0.0):
            raise ScheduleError(
                "Another schedule operation is active; no changes were made."
            )
        store = ScheduleStore(state_path)
        previous = store.load_or_default()
        root_dir = str(ROOT_DIR)
        base_env = capture_safe_env()
        preset_env = run_preset_env(preset) if preset else {}
        # A selected preset owns its declared settings. Keep an ambient value
        # only when the user supplied the same key as an explicit override.
        base_env = {
            name: value
            for name, value in base_env.items()
            if name not in preset_env or name in safe_overrides
        }
        effective_env = _merge_scheduled_env(preset, base_env, safe_overrides)
        output_dir, history_db_path = _scheduled_artifact_paths(
            root_dir, effective_env
        )
        schedule = DailySchedule(
            schema_version=SCHEMA_VERSION,
            enabled=True,
            hour=hour,
            minute=minute,
            preset_id=preset,
            delivery_mode=mode,
            base_env=base_env,
            overrides=safe_overrides,
            root_dir=root_dir,
            python_executable=sys.executable,
            output_dir=output_dir,
            history_db_path=history_db_path,
            updated_at=_now_iso_local(),
            launchd_status="unknown",
            last_run=previous.last_run,
        )
        store.save(schedule)
        plist = default_plist_path() if plist_path is None else plist_path
        log_dir = default_log_dir()
        _ensure_private_directory(log_dir)
        _write_private_file(
            plist,
            build_plist(
                schedule,
                stdout_path=log_dir / "run.stdout.log",
                stderr_path=log_dir / "run.stderr.log",
            ),
        )
        # Replace any existing job first so repeated enable/update is idempotent.
        try:
            adapter.bootout()
        except ScheduleError:
            store.save(replace(schedule, launchd_status=adapter.load_state()))
            raise
        try:
            adapter.bootstrap(plist)
        except ScheduleError as error:
            store.save(replace(schedule, launchd_status=adapter.load_state()))
            raise error
        updated = replace(schedule, launchd_status="loaded", updated_at=_now_iso_local())
        store.save(updated)
        return updated
    finally:
        lock.release()


def disable_schedule(
    *,
    state_path: Path | None = None,
    plist_path: Path | None = None,
) -> DailySchedule:
    """Boot out the job, remove the plist, and mark the schedule disabled."""
    lock = ScheduleLock(_lock_path_for_state(state_path))
    try:
        if not lock.acquire(timeout=0.0):
            raise ScheduleError(
                "Another schedule operation is active; no changes were made."
            )
        store = ScheduleStore(state_path)
        try:
            schedule = store.load_or_default()
        except ScheduleStateError:
            # Disable is the recovery operation: fixed launchd identity and
            # plist paths remain known even when the JSON cannot be parsed.
            schedule = DailySchedule(
                root_dir=str(ROOT_DIR),
                python_executable=sys.executable,
                output_dir=str(ROOT_DIR / "output" / "daily_outputs"),
                history_db_path=str(ROOT_DIR / "output" / "history" / "news_history.duckdb"),
            )
        adapter = LaunchdAdapter()
        if adapter.supported():
            # A real bootout failure keeps the state enabled/loaded so the user
            # sees the problem instead of a silently disabled-looking schedule.
            adapter.bootout()
        plist = default_plist_path() if plist_path is None else plist_path
        try:
            plist.unlink()
        except FileNotFoundError:
            pass
        launchd = "unavailable" if not adapter.supported() else "not_loaded"
        disabled = replace(
            schedule,
            enabled=False,
            launchd_status=launchd,
            updated_at=_now_iso_local(),
        )
        store.save(disabled)
        return disabled
    finally:
        lock.release()


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _reconcile_stale_running_unlocked(
    schedule: DailySchedule,
    store: ScheduleStore | None = None,
) -> DailySchedule:
    last = schedule.last_run
    if last.status != "running":
        return schedule
    if last.pid is not None and _pid_is_alive(last.pid):
        return schedule
    updated = replace(
        schedule,
        last_run=replace(
            last,
            status="interrupted",
            finished_at=_now_iso_local(),
            error_type="interrupted",
            error_message="Scheduled run process is no longer running.",
        ),
    )
    if store is not None:
        store.save(updated)
    return updated


def reconcile_stale_running(
    schedule: DailySchedule,
    store: ScheduleStore | None = None,
) -> DailySchedule:
    """Convert a dead ``running`` projection to ``interrupted``.

    State reconciliation is serialized with lifecycle changes whenever a
    store is supplied. Never invents a report outcome; durable run history
    stays authoritative.
    """
    if store is None:
        return _reconcile_stale_running_unlocked(schedule)
    lock = ScheduleLock(_lock_path_for_state(store.path))
    try:
        if not lock.acquire(timeout=0.0):
            raise ScheduleError(
                "Another schedule operation is active; status was not updated."
            )
        current = store.load()
        if current is None:
            return schedule
        return _reconcile_stale_running_unlocked(current, store)
    finally:
        lock.release()


def schedule_status(*, state_path: Path | None = None) -> dict[str, Any]:
    """Safe bounded status payload for CLI/UI (never env, plist XML, or
    launchctl output)."""
    store = ScheduleStore(state_path)
    adapter = LaunchdAdapter()
    lock = ScheduleLock(_lock_path_for_state(state_path))
    lock_error: str | None = None
    try:
        try:
            acquired = lock.acquire(timeout=0.0)
        except ScheduleError as exc:
            acquired = False
            lock_error = str(exc)
        if not acquired:
            error = lock_error or "Another schedule operation is active; status is unavailable."
            return {
                "supported": adapter.supported(),
                "enabled": False,
                "time": "",
                "preset_id": "",
                "delivery_mode": "",
                "launchd_status": "unknown",
                "next_run_label": "",
                "last_run": {},
                "state_path": str(store.path),
                "plist_path": str(default_plist_path()),
                "error": error,
            }

        error: str | None = None
        try:
            schedule = store.load()
        except ScheduleStateError as exc:
            schedule = None
            error = str(exc)
        if schedule is None:
            # Absent or corrupt: fail closed to a disabled projection without
            # re-reading the (possibly corrupt) file.
            schedule = DailySchedule(
                root_dir=str(ROOT_DIR),
                python_executable=sys.executable,
                launchd_status="unknown",
            )
        if error is None:
            schedule = _reconcile_stale_running_unlocked(schedule, store)
            launchd = launchd_status(schedule)
        else:
            launchd = "unknown" if adapter.supported() else "unavailable"
        if error is not None:
            return {
                "supported": adapter.supported(),
                "enabled": False,
                "time": "",
                "preset_id": "",
                "delivery_mode": "",
                "launchd_status": launchd,
                "next_run_label": "",
                "last_run": {},
                "state_path": str(store.path),
                "plist_path": str(default_plist_path()),
                "error": error,
            }
        return {
            "supported": adapter.supported(),
            "enabled": schedule.enabled,
            "time": f"{schedule.hour:02d}:{schedule.minute:02d}",
            "preset_id": schedule.preset_id,
            "delivery_mode": schedule.delivery_mode,
            "launchd_status": launchd,
            "next_run_label": _next_run_label(schedule),
            "last_run": schedule.last_run.to_dict(),
            "state_path": str(store.path),
            "plist_path": str(default_plist_path()),
            "error": None,
        }
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Scheduled runner
# ---------------------------------------------------------------------------


def _redact_message(text: str) -> str:
    """Return a fixed safe message for durable scheduler error projections.

    Exception text is not an auditable credential boundary: credentials may
    come from presets, files, or future settings rather than ``os.environ``.
    Detailed failures remain in the canonical Run Session diagnostics.
    """
    del text
    return "Scheduled Run Session failed; inspect canonical run history."


def _merge_scheduled_env(
    preset_id: str,
    base_env: Mapping[str, str],
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """Resolve schedule settings with preset, base, then override precedence."""
    env: dict[str, str] = {}
    preset_env = run_preset_env(preset_id) if preset_id else {}
    env.update(preset_env)
    for name, value in base_env.items():
        if name not in preset_env or name in overrides:
            env[name] = value
    env.update(overrides)
    return env


def _scheduled_artifact_paths(
    root_dir: str, effective_env: Mapping[str, str]
) -> tuple[str, str]:
    """Resolve artifact paths using the same environment as the pipeline."""
    root = Path(root_dir)
    output_value = str(effective_env.get("NEWS_OUTPUT_DIR") or "output/daily_outputs")
    history_value = str(
        effective_env.get("NEWS_HISTORY_DB") or "output/history/news_history.duckdb"
    )
    output_path = Path(output_value)
    history_path = Path(history_value)
    return (
        str(output_path if output_path.is_absolute() else root / output_path),
        str(history_path if history_path.is_absolute() else root / history_path),
    )


def _scheduled_run_env(schedule: DailySchedule) -> dict[str, str]:
    """Preset -> safe base env -> explicit overrides -> forced delivery mode.

    The persisted schedule has already removed ambient values owned by the
    selected preset. Explicit overrides remain last so they intentionally win.
    """
    env = _merge_scheduled_env(
        schedule.preset_id, schedule.base_env, schedule.overrides
    )
    env[DELIVERY_MODE_ENV_VAR] = schedule.delivery_mode
    if schedule.preset_id:
        env[PRESET_ENV_VAR] = schedule.preset_id
        env[ACTIVE_PRESET_ENV_VAR] = schedule.preset_id
    else:
        env.pop(PRESET_ENV_VAR, None)
        env.pop(ACTIVE_PRESET_ENV_VAR, None)
    return env


def _read_latest_run_details_bytes(output_dir: str) -> bytes | None:
    """Read the details artifact identity without exposing its contents."""
    try:
        return (Path(output_dir) / "latest_run_details.json").read_bytes()
    except OSError:
        return None
def _read_latest_run_projection(output_dir: str) -> dict[str, Any]:
    """Bounded run/report/delivery projection from the existing outputs.

    Reads only ``latest_run_details.json``/``latest_run.md``; never a second
    schedule-owned history store.
    """
    details: dict[str, Any] = {}
    details_path = Path(output_dir) / "latest_run_details.json"
    if details_path.exists():
        try:
            parsed = json.loads(details_path.read_text(encoding="utf-8") or "{}")
            if isinstance(parsed, dict):
                details = parsed
        except (OSError, json.JSONDecodeError):
            pass
    events = details.get("events") if isinstance(details.get("events"), list) else []
    run_status = run_status_from_events(events)
    started_at = str(details.get("run_started_at") or "")
    run_id = started_at.replace("T", "_").replace(":", "-")[:19]
    report_generated = details.get("report_generated")
    if not isinstance(report_generated, bool):
        reports = details.get("reports")
        report_generated = isinstance(reports, list) and bool(reports)
    report_status = "not_generated"
    if run_status == "completed" and report_generated:
        markdown_path = Path(output_dir) / "latest_run.md"
        report_available = False
        if markdown_path.exists():
            try:
                report_available = bool(markdown_path.read_text(encoding="utf-8").strip())
            except OSError:
                report_available = False
        report_status = "available" if report_available else "unavailable"
    delivery = details.get("delivery") if isinstance(details.get("delivery"), dict) else {}
    return {
        "run_id": run_id,
        "run_status": run_status,
        "report_status": report_status,
        "delivery_status": str(delivery.get("status") or "").strip(),
    }


def _load_pipeline_module() -> Any:
    """Import the canonical pipeline only inside the scheduled-run boundary."""
    from . import pipeline

    return pipeline


def _empty_run_projection() -> dict[str, str]:
    return {
        "run_id": "",
        "run_status": "unknown",
        "report_status": "unavailable",
        "delivery_status": "",
    }


def _execute_scheduled_run(schedule: DailySchedule, store: ScheduleStore) -> int:
    started_at = _now_iso_local()
    error_type = ""
    error_message = ""
    outcome = "failed"
    projection = _empty_run_projection()
    running_written = False
    details_before = _read_latest_run_details_bytes(schedule.output_dir)
    try:
        # Apply the resolved environment BEFORE the pipeline import so the
        # module-level Runtime Config snapshot sees the scheduled settings.
        store.save(
            replace(
                schedule,
                last_run=LastScheduledRun(
                    status="running",
                    started_at=started_at,
                    pid=os.getpid(),
                ),
            )
        )
        running_written = True
        os.environ.update(_scheduled_run_env(schedule))
        pipeline = _load_pipeline_module()
        pipeline.run_pipeline()
        details_after = _read_latest_run_details_bytes(schedule.output_dir)
        if details_after is None or details_after == details_before:
            raise _ProjectionError(
                "Scheduled run did not produce a current details artifact."
            )
        outcome = "completed"
    except Exception as exc:
        outcome = "failed"
        error_type = "ProjectionError" if isinstance(exc, _ProjectionError) else type(exc).__name__
        error_message = _redact_message(str(exc))

    if outcome == "completed":
        try:
            raw_projection = _read_latest_run_projection(schedule.output_dir)
            projection = {
                "run_id": str(raw_projection.get("run_id") or ""),
                "run_status": str(raw_projection.get("run_status") or "unknown"),
                "report_status": str(
                    raw_projection.get("report_status") or "unavailable"
                ),
                "delivery_status": str(raw_projection.get("delivery_status") or ""),
            }
            if (
                not projection["run_id"]
                or projection["run_status"] != "completed"
                or projection["report_status"] == "unavailable"
            ):
                raise _ProjectionError(
                    "Scheduled run did not produce a complete canonical projection."
                )
        except Exception as exc:
            # A projection failure must not strand the durable lifecycle marker.
            outcome = "failed"
            error_type = "ProjectionError"
            error_message = _redact_message(str(exc))
            projection = _empty_run_projection()

    if not running_written:
        # The initial state write failed, so there is no durable running marker
        # to repair. The caller receives the controlled scheduler error below.
        raise ScheduleError("Could not record the scheduled run state.")

    terminal_schedule = replace(
        schedule,
        last_run=LastScheduledRun(
            status=outcome,
            run_id=projection["run_id"],
            started_at=started_at,
            finished_at=_now_iso_local(),
            run_status=projection["run_status"] if outcome == "completed" else "failed",
            report_status=projection["report_status"],
            delivery_status=projection["delivery_status"],
            error_type=error_type,
            error_message=error_message,
            pid=os.getpid(),
        ),
    )
    try:
        store.save(terminal_schedule)
    except Exception as exc:
        print(
            "schedule: could not persist terminal run state "
            f"({type(exc).__name__}); inspect the schedule directory and "
            "canonical run history.",
            file=sys.stderr,
        )
        return 2
    return 0 if outcome == "completed" else 1


def run_scheduled(*, state_path: Path | None = None) -> int:
    """Foreground entry point for ``news schedule run`` (launchd invocation).

    Fails closed when state is absent/corrupt/disabled or the preset is gone.
    The schedule lock covers the state recheck and complete run projection so
    lifecycle updates cannot be overwritten by a stale runner snapshot.
    """
    store = ScheduleStore(state_path)
    lock = ScheduleLock(_lock_path_for_state(state_path))
    try:
        try:
            acquired = lock.acquire(timeout=0.0)
        except ScheduleError as exc:
            print(f"schedule: {exc}", file=sys.stderr)
            return 2
        if not acquired:
            print(
                "schedule: another scheduled run is already active; skipping.",
                file=sys.stderr,
            )
            return 1

        try:
            schedule = store.load()
        except ScheduleStateError as exc:
            print(f"schedule: {exc}", file=sys.stderr)
            return 2
        if schedule is None or not schedule.enabled:
            print("schedule: disabled or absent; no run started.", file=sys.stderr)
            return 0
        if schedule.preset_id:
            try:
                run_preset_env(schedule.preset_id)
            except ValueError as exc:
                message = _redact_message(str(exc))
                store.save(
                    replace(
                        schedule,
                        last_run=LastScheduledRun(
                            status="failed",
                            started_at=_now_iso_local(),
                            finished_at=_now_iso_local(),
                            error_type="ValueError",
                            error_message=message,
                            pid=os.getpid(),
                        ),
                    )
                )
                print(f"schedule: {message}", file=sys.stderr)
                return 2
        schedule = store.load_or_default()
        if not schedule.enabled:
            print("schedule: disabled while waiting; no run started.", file=sys.stderr)
            return 0
        return _execute_scheduled_run(schedule, store)
    finally:
        lock.release()
