#!/usr/bin/env python3
"""Keep the poller-owned UI running inside one registered cmux terminal.

The board poller runs under launchd, while cmux is configured in cmux-only
access mode. The poller writes atomic requests here; this long-lived runner is
started once from the registered terminal surface and performs UI restarts
inside that surface.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


DEFAULT_REQUEST_PATH = (
    Path.home() / ".config" / "news" / "ui-runtime-request.json"
)
REQUEST_PATH = Path(
    os.environ.get("NEWS_UI_CMUX_REQUEST", str(DEFAULT_REQUEST_PATH))
).expanduser()


def _read_request() -> dict | None:
    try:
        data = json.loads(REQUEST_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        REQUEST_PATH.unlink()
    except FileNotFoundError:
        pass
    return data


def _stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def _start(request: dict) -> subprocess.Popen | None:
    runtime = Path(str(request.get("worktree") or "")).expanduser()
    news_bin = Path(str(request.get("news_bin") or "")).expanduser()
    host = str(request.get("host") or "127.0.0.1")
    port = str(request.get("port") or "8766")
    if not runtime.is_dir() or not news_bin.exists():
        print(
            f"UI runner cannot start: runtime={runtime} news={news_bin}",
            flush=True,
        )
        return None
    env = os.environ.copy()
    runtime_path = str(runtime)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        runtime_path
        if not existing_pythonpath
        else runtime_path + os.pathsep + existing_pythonpath
    )
    process = subprocess.Popen(
        [str(news_bin), "ui", "--host", host, "--port", port],
        cwd=runtime_path,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"UI runner started PID {process.pid} on {host}:{port}", flush=True)
    return process


def main() -> int:
    active: subprocess.Popen | None = None
    print(f"UI runner watching {REQUEST_PATH}", flush=True)
    try:
        while True:
            request = _read_request()
            if request is not None:
                action = str(request.get("action") or "").strip().lower()
                _stop(active)
                active = None
                if action == "start":
                    active = _start(request)
                elif action == "stop":
                    print("UI runner stopped active UI", flush=True)
                else:
                    print(f"UI runner ignored action {action!r}", flush=True)
            if active is not None and active.poll() is not None:
                print(
                    f"UI runner UI exited with code {active.returncode}",
                    flush=True,
                )
                active = None
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 130
    finally:
        _stop(active)


if __name__ == "__main__":
    raise SystemExit(main())
