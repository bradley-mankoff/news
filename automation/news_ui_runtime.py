#!/usr/bin/env python3
"""News-only integration hook that refreshes the local develop UI runtime."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRY_RUN = "--dry-run" in sys.argv


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


UI_HOST = os.environ.get("NEWS_UI_HOST", "127.0.0.1")
try:
    UI_PORT = int(os.environ.get("NEWS_UI_PORT", "8766"))
except ValueError:
    UI_PORT = 8766
UI_RUNTIME_WORKTREE = Path(
    os.environ.get("NEWS_UI_RUNTIME_WORKTREE", str(ROOT.parent / "news-ui-runtime"))
).expanduser()
UI_RUNTIME_STATE_PATH = Path(
    os.environ.get(
        "NEWS_UI_RUNTIME_STATE",
        str(ROOT / "automation" / "ui_runtime.json"),
    )
).expanduser()
UI_CMUX_STATE_PATH = Path(
    os.environ.get(
        "NEWS_UI_CMUX_STATE",
        str(Path.home() / ".config" / "news" / "ui-cmux.json"),
    )
).expanduser()
UI_CMUX_REQUEST_PATH = Path(
    os.environ.get(
        "NEWS_UI_CMUX_REQUEST",
        str(Path.home() / ".config" / "news" / "ui-runtime-request.json"),
    )
).expanduser()
UI_LOG_PATH = Path(os.environ.get("NEWS_UI_LOG", "/tmp/news-ui.log")).expanduser()
UI_NEWS_BIN = ROOT / ".venv" / "bin" / "news"


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_owner(host: str, port: int) -> str:
    """Best-effort identity of the process listening on host:port."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-i", f"TCP:{port}", "-sTCP:LISTEN", "-F", "pcu"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        if line[0] == "p":
            if current:
                entries.append(current)
            current = {"pid": line[1:]}
        elif line[0] in ("c", "u"):
            current[line[0]] = line[1:]
    if current:
        entries.append(current)
    if not entries:
        return ""
    first = entries[0]
    return f"{first.get('c', '?')} pid {first.get('pid', '?')}"


def _last_log_lines(path: Path, limit: int = 5) -> list[str]:
    """Tail of the UI log, for one actionable failure line."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def _load_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _load_cmux_registration() -> dict | None:
    data = _load_json_file(UI_CMUX_STATE_PATH)
    required = ("socket_path", "workspace_ref", "surface_ref")
    if not all(str(data.get(key) or "").strip() for key in required):
        return None
    return {
        "socket_path": str(data["socket_path"]),
        "workspace_ref": str(data["workspace_ref"]),
        "surface_ref": str(data["surface_ref"]),
        "request_path": str(
            data.get("request_path") or UI_CMUX_REQUEST_PATH
        ),
    }


def register_ui_surface() -> int:
    """Persist the current terminal surface as the UI's optional cmux owner."""
    result = subprocess.run(
        ["cmux", "identify", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"cmux identify failed: {result.stderr.strip()[:300]}")
        return 1
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        print(f"cmux identify returned invalid JSON: {error}")
        return 1
    caller = payload.get("caller") if isinstance(payload, dict) else None
    if not isinstance(caller, dict) or caller.get("surface_type") != "terminal":
        print("The current cmux surface is not a terminal; UI registration refused.")
        return 1
    socket_path = str(payload.get("socket_path") or "").strip()
    workspace_ref = str(caller.get("workspace_ref") or "").strip()
    surface_ref = str(caller.get("surface_ref") or "").strip()
    if not socket_path or not workspace_ref or not surface_ref:
        print("cmux identify did not provide a complete terminal target.")
        return 1
    _write_json_file(
        UI_CMUX_STATE_PATH,
        {
            "socket_path": socket_path,
            "workspace_ref": workspace_ref,
            "surface_ref": surface_ref,
            "request_path": str(UI_CMUX_REQUEST_PATH),
        },
    )
    print(
        f"Registered cmux UI surface {surface_ref} in workspace {workspace_ref}."
    )
    return 0


def _cmux_request_path(registration: dict) -> Path:
    return Path(
        str(registration.get("request_path") or UI_CMUX_REQUEST_PATH)
    ).expanduser()


def _request_cmux_ui(registration: dict, action: str) -> bool:
    try:
        _write_json_file(
            _cmux_request_path(registration),
            {
                "action": action,
                "host": UI_HOST,
                "port": UI_PORT,
                "worktree": str(UI_RUNTIME_WORKTREE),
                "news_bin": str(UI_NEWS_BIN),
            },
        )
    except OSError as error:
        log(f"LOCAL SYNC: cmux UI {action} request failed: {error}")
        return False
    return True


def _load_ui_state() -> dict:
    return _load_json_file(UI_RUNTIME_STATE_PATH)


def _record_sync_error(detail: str) -> None:
    """Persist one actionable sync-failure line for mechanic/health to read."""
    state = _load_ui_state()
    state["error"] = detail
    state["last_sync_failed_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    _write_json_file(UI_RUNTIME_STATE_PATH, state)


def _record_sync_ok() -> None:
    """Clear the persisted error field once a sync succeeds."""
    state = _load_ui_state()
    if state.pop("error", None) is not None or state.pop(
            "last_sync_failed_at", None) is not None:
        _write_json_file(UI_RUNTIME_STATE_PATH, state)


def _clear_ui_state() -> None:
    try:
        UI_RUNTIME_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def _managed_ui_pid() -> int | None:
    state = _load_ui_state()
    if state.get("mode") != "process":
        return None
    try:
        pid = int(state.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_marker(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _managed_process_ownership() -> bool | None:
    """True/False for owned/already-dead; None when ownership is unverifiable."""
    state = _load_ui_state()
    if state.get("mode") != "process":
        return False
    pid = _managed_ui_pid()
    if pid is None or not _pid_alive(pid):
        return False
    expected_marker = str(state.get("start_marker") or "").strip()
    current_marker = _process_start_marker(pid)
    if not expected_marker or not current_marker:
        return None
    return current_marker == expected_marker


def _ui_running() -> bool:
    state = _load_ui_state()
    if state.get("mode") == "process":
        return (
            _managed_process_ownership() is True
            and _port_open(UI_HOST, UI_PORT)
        )
    if state.get("mode") == "cmux":
        return _port_open(UI_HOST, UI_PORT)
    return False


def _stop_managed_process() -> bool:
    ownership = _managed_process_ownership()
    if ownership is False:
        _clear_ui_state()
        return True
    if ownership is None:
        log("LOCAL SYNC: refusing to stop UI; process ownership is unverifiable")
        return False
    pid = _managed_ui_pid()
    if pid is None:
        _clear_ui_state()
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_ui_state()
        return True
    deadline = time.monotonic() + 10
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
    stopped = not _pid_alive(pid)
    if stopped:
        _clear_ui_state()
    return stopped


def _stop_cmux_ui(registration: dict) -> bool:
    if not _request_cmux_ui(registration, "stop"):
        return False
    deadline = time.monotonic() + 10
    while _port_open(UI_HOST, UI_PORT) and time.monotonic() < deadline:
        time.sleep(0.2)
    stopped = not _port_open(UI_HOST, UI_PORT)
    if stopped:
        _clear_ui_state()
    return stopped


def _start_cmux_ui(registration: dict) -> bool:
    if _port_open(UI_HOST, UI_PORT):
        if not _stop_cmux_ui(registration):
            return False
    if not _request_cmux_ui(registration, "start"):
        return False
    _write_json_file(
        UI_RUNTIME_STATE_PATH,
        {
            "mode": "cmux",
            "workspace_ref": registration["workspace_ref"],
            "surface_ref": registration["surface_ref"],
        },
    )
    return _wait_for_ui()


def _stop_ui() -> bool:
    state = _load_ui_state()
    mode = state.get("mode")
    if mode == "process":
        return _stop_managed_process()
    if mode == "cmux":
        registration = _load_cmux_registration()
        return bool(registration and _stop_cmux_ui(registration))
    return True


def _wait_for_ui(process: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _port_open(UI_HOST, UI_PORT):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _start_managed_process() -> bool:
    if not UI_NEWS_BIN.exists():
        log(f"LOCAL SYNC: cannot start UI - missing {UI_NEWS_BIN}")
        return False
    if _port_open(UI_HOST, UI_PORT):
        log(
            f"LOCAL SYNC: UI port {UI_HOST}:{UI_PORT} is occupied by an "
            "unmanaged process; refusing to kill it"
        )
        return False
    UI_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    runtime_path = str(UI_RUNTIME_WORKTREE)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        runtime_path
        if not existing_pythonpath
        else runtime_path + os.pathsep + existing_pythonpath
    )
    try:
        with UI_LOG_PATH.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(
                [
                    str(UI_NEWS_BIN),
                    "ui",
                    "--host",
                    UI_HOST,
                    "--port",
                    str(UI_PORT),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(UI_RUNTIME_WORKTREE),
                env=env,
            )
    except OSError as error:
        log(f"LOCAL SYNC: UI start failed: {error}")
        return False
    start_marker = _process_start_marker(process.pid)
    if not start_marker:
        log("LOCAL SYNC: UI start failed - could not verify process ownership")
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        return False
    _write_json_file(
        UI_RUNTIME_STATE_PATH,
        {
            "mode": "process",
            "pid": process.pid,
            "start_marker": start_marker,
            "worktree": str(UI_RUNTIME_WORKTREE),
        },
    )
    if _wait_for_ui(process):
        return True
    _stop_managed_process()
    return False


def _restart_ui() -> bool:
    """Restart the single owned UI, in cmux when explicitly registered."""
    registration = _load_cmux_registration()
    state = _load_ui_state()
    if state.get("mode") == "process" and not _stop_managed_process():
        return False
    if registration is not None:
        return _start_cmux_ui(registration)
    if state.get("mode") == "cmux":
        log("LOCAL SYNC: cmux UI is registered but its registration is invalid")
        return False
    return _start_managed_process()


def _run_git(
    args: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 90,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
    )


def _sync_ui_runtime() -> tuple[bool, str]:
    runtime = UI_RUNTIME_WORKTREE
    if runtime.exists():
        if not (runtime / ".git").exists():
            return False, f"UI runtime exists but is not a git worktree: {runtime}"
    else:
        runtime.parent.mkdir(parents=True, exist_ok=True)
        added = _run_git(
            ["worktree", "add", "--detach", str(runtime), "origin/develop"],
            cwd=ROOT,
            timeout=120,
        )
        if added.returncode != 0:
            return False, f"creating UI worktree failed: {added.stderr.strip()[:250]}"

    status = _run_git(["status", "--porcelain"], cwd=runtime, timeout=30)
    if status.returncode != 0:
        return False, f"checking UI worktree failed: {status.stderr.strip()[:250]}"
    if status.stdout.strip():
        return False, f"UI runtime is dirty; refusing to overwrite {runtime}"

    target = _run_git(["rev-parse", "origin/develop"], cwd=ROOT, timeout=30)
    if target.returncode != 0:
        return False, f"reading origin/develop failed: {target.stderr.strip()[:250]}"
    head = _run_git(["rev-parse", "HEAD"], cwd=runtime, timeout=30)
    if head.returncode != 0:
        return False, f"reading UI runtime HEAD failed: {head.stderr.strip()[:250]}"

    updated = head.stdout.strip() != target.stdout.strip()
    if updated:
        if _ui_running() and not _stop_ui():
            return False, "existing owned UI did not stop cleanly"
        reset = _run_git(["reset", "--hard", "origin/develop"], cwd=runtime, timeout=90)
        if reset.returncode != 0:
            return False, f"updating UI runtime failed: {reset.stderr.strip()[:250]}"
        version_detail = f"UI runtime updated to {target.stdout.strip()[:12]}"
    else:
        version_detail = f"UI runtime already at {target.stdout.strip()[:12]}"

    if _ui_running():
        return True, f"{version_detail}; UI already running"
    if _port_open(UI_HOST, UI_PORT) and _load_cmux_registration() is None:
        owner = _port_owner(UI_HOST, UI_PORT)
        owner_detail = f" (owner: {owner})" if owner else ""
        return False, (
            f"{version_detail}; UI port {UI_HOST}:{UI_PORT} is occupied by "
            f"an unmanaged process{owner_detail}"
        )
    if _restart_ui():
        return True, f"{version_detail}; UI started/restarted"
    tail = _last_log_lines(UI_LOG_PATH)
    log_detail = f" - last log: {tail[-1][:200]}" if tail else ""
    return False, f"{version_detail}; UI start failed - see {UI_LOG_PATH}{log_detail}"


def sync_local_develop() -> str:
    """Fetch develop and refresh the poller-owned UI runtime."""
    if DRY_RUN:
        return "LOCAL SYNC: dry-run (no fetch/worktree/UI mutations)"
    fetched = _run_git(["fetch", "origin", "develop"], cwd=ROOT, timeout=90)
    if fetched.returncode != 0:
        detail = f"git fetch: {fetched.stderr.strip()[:250]}"
        _record_sync_error(detail)
        return f"LOCAL SYNC FAILED: {detail}"
    ok, detail = _sync_ui_runtime()
    if ok:
        _record_sync_ok()
    else:
        _record_sync_error(detail)
    return f"{'LOCAL SYNC' if ok else 'LOCAL SYNC FAILED'}: {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "register"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    global DRY_RUN
    DRY_RUN = args.dry_run
    if args.command == "sync":
        result = sync_local_develop()
        log(result)
        return int(result.startswith("LOCAL SYNC FAILED"))
    result = register_ui_surface()
    if result:
        return result
    try:
        from automation.ui_surface_runner import main as run_ui_surface
    except ModuleNotFoundError:
        from ui_surface_runner import main as run_ui_surface
    return run_ui_surface()


if __name__ == "__main__":
    raise SystemExit(main())
