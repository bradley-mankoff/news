"""Configuration, command, hook, logging, and durable-state primitives."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(
    os.environ.get("PM_HARNESS_ROOT", Path(__file__).resolve().parents[2])
).resolve()
DRY_RUN = "--dry-run" in sys.argv

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def validate_config(cfg: dict) -> dict:
    """Fail early when the portable harness interface is incomplete."""
    if not isinstance(cfg, dict):
        raise ValueError("invalid PM harness config: root must be an object")
    required = {
        "repo",
        "project_owner",
        "project_number",
        "status_field",
        "default_lane",
        "state_file",
        "max_concurrent_workflows",
        "lanes",
        "dispatch",
    }
    errors = [f"missing {name}" for name in sorted(required - cfg.keys())]
    lanes = cfg.get("lanes")
    if isinstance(lanes, dict):
        roles = set(lanes.values())
        missing_roles = {
            "backlog",
            "todo",
            "in_progress",
            "ready",
            "review",
            "done",
        } - roles
        errors.extend(
            f"missing lane role {role}" for role in sorted(missing_roles))
        if cfg.get("default_lane") not in lanes:
            errors.append("default_lane is not a configured lane")
    elif "lanes" in cfg:
        errors.append("lanes must be an object")
    dispatch_cfg = cfg.get("dispatch")
    if not isinstance(dispatch_cfg, dict) or not all(
            isinstance(dispatch_cfg.get(name), dict)
            for name in ("todo", "review")):
        errors.append("dispatch must define todo and review objects")
    for field in (
        "max_concurrent_workflows",
        "poll_interval_seconds",
        "poll_timeout_seconds",
    ):
        try:
            if int(cfg.get(field, 0)) <= 0:
                errors.append(f"{field} must be positive")
        except (TypeError, ValueError):
            errors.append(f"{field} must be an integer")
    hooks = cfg.get("hooks") or {}
    if not isinstance(hooks, dict) or any(
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
            for command in hooks.values()):
        errors.append("hooks must map names to non-empty argv string lists")
    if errors:
        raise ValueError("invalid PM harness config: " + "; ".join(errors))
    return cfg


def load_config(path: str | Path | None = None) -> dict:
    config_path = Path(
        path
        or os.environ.get(
            "PM_HARNESS_CONFIG",
            ROOT / "automation" / "config.json",
        )
    )
    cfg = json.loads(config_path.read_text())
    cfg.setdefault("poll_interval_seconds", 45)
    cfg.setdefault("poll_timeout_seconds", 300)
    return validate_config(cfg)


def gh(args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, env=env
    )


def _run_git(
    args: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_hook(cfg: dict, name: str) -> str:
    """Run one configured repo hook; hook failure never changes board truth."""
    command = (cfg.get("hooks") or {}).get(name)
    if not command:
        return f"HOOK SKIPPED {name}: not configured"
    if not isinstance(command, list) or not all(
            isinstance(part, str) and part for part in command):
        return f"HOOK FAILED {name}: command must be a string list"
    if runtime_dry_run():
        return f"[dry-run] HOOK {name}: {' '.join(command)}"
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=int(cfg.get("hook_timeout_seconds", 120)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"HOOK FAILED {name}: {exc}"
    detail = (result.stderr or result.stdout).strip()[:300]
    return (
        f"HOOK OK {name}"
        if result.returncode == 0
        else f"HOOK FAILED {name}: {detail or f'exit {result.returncode}'}"
    )


def hydrate_state_for_items(state: dict, items: list[dict]) -> None:
    """Map issue-keyed persisted records to this poll's current board item IDs."""
    item_by_issue = {
        int(content["number"]): item["id"]
        for item in items
        if (content := item.get("content") or {}).get("__typename") == "Issue"
    }
    for key, rec in list(state.items()):
        if key == "_meta" or not isinstance(rec, dict):
            continue
        number = rec.get("issue_number")
        try:
            number = int(number if number is not None else key)
        except (TypeError, ValueError):
            continue
        rec["issue_number"] = number
        item_id = item_by_issue.get(number)
        if not item_id or item_id == key:
            continue
        state[item_id] = rec
        del state[key]


def persisted_state(state: dict) -> dict:
    """Return schema-v2 state keyed by issue number, never project item ID."""
    meta = dict(state.get("_meta") or {})
    meta["schema_version"] = 2
    output = {"_meta": meta}
    for key, rec in state.items():
        if key == "_meta" or not isinstance(rec, dict):
            continue
        number = rec.get("issue_number")
        if number is None:
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
        output[str(number)] = rec
    return output


def save_state(cfg: dict, state: dict) -> None:
    if runtime_dry_run():
        log("[dry-run] state not saved")
        return
    path = ROOT / cfg["state_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(persisted_state(state), indent=2) + "\n")
    os.replace(tmp, path)


def runtime_dry_run() -> bool:
    return DRY_RUN
