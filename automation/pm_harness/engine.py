#!/usr/bin/env python3
"""Public interface and bounded supervisor for the reusable PM harness."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import traceback

from .cycle import poll
from .runtime import (
    DRY_RUN,
    ROOT,
    gh,
    load_config,
    log,
)

def poll_from_disk() -> int:
    """Run one poll against durable state inside the isolated child process."""
    cfg = load_config()
    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()
    state_path = ROOT / cfg["state_file"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    try:
        poll(cfg, env, state)
    except Exception:
        log("poll error:\n" + traceback.format_exc().rstrip())
        return 1
    return 0


def _terminate_poll_process(process: subprocess.Popen) -> None:
    """Terminate the isolated poll process group, then force it if needed."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_poll_process(cfg: dict) -> int:
    """Run one bounded poll in its own process group."""
    command = [
        sys.executable,
        "-m",
        "automation.pm_harness",
        "--poll-child",
    ]
    if DRY_RUN:
        command.append("--dry-run")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            start_new_session=True,
        )
        process.wait(timeout=int(cfg.get("poll_timeout_seconds", 300)))
    except (OSError, ValueError) as exc:
        log(f"POLL START FAILED: {exc}")
        return 1
    except subprocess.TimeoutExpired:
        _terminate_poll_process(process)
        log(
            "POLL TIMEOUT: exceeded "
            f"{cfg.get('poll_timeout_seconds', 300)}s; subprocess tree terminated"
        )
        return 124
    return int(process.returncode or 0)


def main() -> int:
    if "--poll-child" in sys.argv:
        return poll_from_disk()
    cfg = load_config()
    once = "--once" in sys.argv or DRY_RUN
    consecutive_failures = 0
    while True:
        result = run_poll_process(cfg)
        if result == 0:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            log(f"POLL FAILED exit={result} attempt={consecutive_failures}")
            if consecutive_failures > 3:
                log(
                    "POLLER STUCK: repeated isolated poll failures — "
                    "check gh/archon auth and board config"
                )
        if once:
            return result
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
