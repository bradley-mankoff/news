"""Bounded Archon CLI adapter and worktree inspection."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .model import (
    WorkflowRunLookup,
    WorkflowRuns,
    WorkflowRunStatusMap,
    issue_number_from_message,
)
from .runtime import ROOT, _run_git

def _run_timestamp(value: object) -> str | None:
    """Normalize one Archon timestamp, rejecting malformed values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _normalize_run(run: dict) -> dict:
    normalized = dict(run)
    run_id = normalized.get("id")
    normalized["id"] = (
        run_id.strip()
        if isinstance(run_id, str) and run_id.strip()
        else None
    )
    if not isinstance(normalized.get("user_message"), str):
        normalized["user_message"] = None
    for field in ("started_at", "completed_at"):
        normalized[field] = _run_timestamp(normalized.get(field))
    if (
        normalized.get("working_path") is not None
        and not isinstance(normalized.get("working_path"), str)
    ):
        normalized["working_path"] = None
    return normalized


def _parse_workflow_runs(output: str) -> WorkflowRuns:
    """Parse Archon run JSON; truncated rows remain identity-only evidence."""
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        data = None
    if data is not None:
        runs = data.get("runs") if isinstance(data, dict) else data
        if not isinstance(runs, list):
            return WorkflowRuns(error="archon_runs_shape")
        total = data.get("total") if isinstance(data, dict) else None
        if (
            total is not None
            and (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
            )
        ):
            return WorkflowRuns(error="archon_total_shape")
        normalized = [
            _normalize_run(run) for run in runs if isinstance(run, dict)
        ]
        if isinstance(total, int) and total > len(normalized):
            return WorkflowRuns(
                normalized,
                error="run_list_incomplete",
                partial=True,
            )
        return WorkflowRuns(normalized)

    marker = re.search(r'"runs"\s*:\s*\[', output or "")
    start = marker.end() if marker else (output or "").find("[") + 1
    if start <= 0:
        return WorkflowRuns(error="archon_json")
    decoder = json.JSONDecoder()
    rows: list[dict] = []
    pos = start
    while pos < len(output):
        while pos < len(output) and output[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(output) or output[pos] == "]":
            break
        try:
            value, pos = decoder.raw_decode(output, pos)
        except ValueError:
            break
        if isinstance(value, dict):
            rows.append(_normalize_run(value))
    return WorkflowRuns(rows, error="archon_json", partial=True)


def fetch_workflow_runs(env: dict) -> WorkflowRuns:
    """Read recent runs; prefer a real file so large JSON is not pipe-truncated."""
    # Archon can emit large run payloads. Capturing via PIPE truncates around
    # 64KiB on this platform; writing to a temp file matches the working shell
    # redirect path and keeps complete JSON.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+",
            prefix="pm-harness-archon-runs-",
            suffix=".json",
            delete=False,
        ) as handle:
            out_path = handle.name
        try:
            with open(out_path, "w", encoding="utf-8") as out:
                result = subprocess.run(
                    ["archon", "workflow", "runs", "--json", "--limit", "500"],
                    stdout=out,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    env=env,
                    cwd=str(ROOT),
                )
            output = Path(out_path).read_text(encoding="utf-8")
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        return WorkflowRuns(error="archon_timeout")
    except OSError:
        return WorkflowRuns(error="archon_unavailable")
    if result.returncode != 0:
        return WorkflowRuns(error="archon_command_failed")
    return _parse_workflow_runs(output)


def fetch_workflow_run(env: dict, run_id: str | None) -> WorkflowRunLookup:
    """Read one known run while distinguishing errors from not-found."""
    if not run_id:
        return WorkflowRunLookup(not_found=True)
    try:
        result = subprocess.run(
            ["archon", "workflow", "get", str(run_id), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return WorkflowRunLookup(error="archon_run_timeout")
    except OSError:
        return WorkflowRunLookup(error="archon_run_unavailable")
    if result.returncode != 0:
        return WorkflowRunLookup(error="archon_run_command_failed")
    try:
        run = json.loads(result.stdout)
    except (TypeError, ValueError):
        return WorkflowRunLookup(error="archon_run_json")
    if not isinstance(run, dict) or not run.get("id"):
        return WorkflowRunLookup(error="archon_run_shape")
    return WorkflowRunLookup(run=run)


def unpack_workflow_lookup(
    lookup: WorkflowRunLookup | dict | None,
) -> tuple[dict | None, str | None, bool]:
    """Normalize exact-lookup results and preserve legacy test doubles.

    Older callers/tests supplied a raw dict or ``None``. A raw ``None`` is
    treated as confirmed absence only for that compatibility surface; real
    ``fetch_workflow_run`` failures always return an explicit error.
    """
    if isinstance(lookup, WorkflowRunLookup):
        return lookup.run, lookup.error, lookup.not_found
    if isinstance(lookup, dict):
        return lookup, None, False
    return None, None, True


def latest_workflow_run(
    runs: list[dict],
    *,
    issue_number: int | None = None,
    message: str | None = None,
) -> dict | None:
    """Select a newest valid run from complete or salvaged run rows."""
    if issue_number is None and not message:
        return None
    # Partial lists are still usable for exact issue/message matches among
    # fully decoded leading rows. Invalid matches still fail closed.
    best = None
    best_key = ("", "")
    invalid_match = False
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_message = run.get("user_message")
        run_message = run_message if isinstance(run_message, str) else ""
        if (
            issue_number is not None
            and issue_number_from_message(run_message) != issue_number
        ):
            continue
        if message and message not in run_message:
            continue
        run_id = run.get("id")
        started = _run_timestamp(run.get("started_at"))
        if not isinstance(run_id, str) or not run_id.strip() or started is None:
            invalid_match = True
            continue
        key = (started, run_id.strip())
        if key >= best_key:
            best = run
            best_key = key
    return None if invalid_match else best


def runs_by_message_from(runs: list[dict]) -> WorkflowRunStatusMap:
    """Map each message to its newest valid run status, preserving health."""
    best: dict[str, tuple[str, str, str]] = {}
    invalid_messages: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        message = run.get("user_message")
        if not isinstance(message, str) or not message:
            continue
        started = _run_timestamp(run.get("started_at"))
        run_id = run.get("id")
        if started is None or not isinstance(run_id, str) or not run_id.strip():
            invalid_messages.add(message)
            continue
        key = (started, run_id.strip())
        previous = best.get(message)
        if previous is None or key >= previous[1:]:
            status = run.get("status")
            best[message] = (
                status if isinstance(status, str) else "",
                started,
                run_id.strip(),
            )
    return WorkflowRunStatusMap(
        {
            message: status
            for message, (status, _, _) in best.items()
            if message not in invalid_messages
        },
        error=getattr(runs, "error", None),
    )


def parse_isolation_list(output: str) -> dict[str, dict[str, str]]:
    """Parse `archon isolation list` into branch/path records."""
    records: dict[str, dict[str, str]] = {}
    branch = None
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.endswith(".git:") or line.startswith(("Path:", "Type:")):
            if line.startswith("Path:") and branch:
                records[branch]["path"] = line.removeprefix("Path:").strip()
            continue
        if (
            raw_line.startswith("  ")
            and (
                "/" in line
                or re.fullmatch(r"issue-\d+", line) is not None
            )
            and not line.startswith(("Path:", "Type:"))
        ):
            branch = line
            records.setdefault(branch, {})
    return records


@dataclass(frozen=True)
class WorktreeLookup:
    """Isolation records plus the health of the Archon lookup."""

    records: dict[str, dict[str, str]]
    error: str | None = None


class WorktreeRecords(dict[str, dict[str, str]]):
    """Legacy dict-shaped isolation records with lookup health."""

    def __init__(
        self,
        records: dict[str, dict[str, str]] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        super().__init__(records or {})
        self.error = error


def fetch_archon_worktrees(env: dict) -> WorktreeLookup:
    try:
        result = subprocess.run(
            ["archon", "isolation", "list"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return WorktreeLookup({}, "archon_isolation_timeout")
    except OSError:
        return WorktreeLookup({}, "archon_isolation_unavailable")
    if result.returncode != 0:
        return WorktreeLookup({}, "archon_isolation_command_failed")
    return WorktreeLookup(parse_isolation_list(result.stdout))


def inspect_worktree(path: str | Path | None) -> dict:
    """Read-only worktree snapshot used to prevent unsafe fresh retries."""
    if not path:
        return {"path": "", "exists": False, "dirty": None, "error": "unknown path"}
    worktree = Path(path).expanduser()
    if not worktree.is_dir():
        return {"path": str(worktree), "exists": False, "dirty": None}
    status = _run_git(["status", "--porcelain"], cwd=worktree, timeout=30)
    if status.returncode != 0:
        return {
            "path": str(worktree),
            "exists": True,
            "dirty": None,
            "error": status.stderr.strip()[:300],
        }
    head = _run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=30)
    return {
        "path": str(worktree),
        "exists": True,
        "dirty": bool(status.stdout.strip()),
        "status": status.stdout.strip()[:1000],
        "head": head.stdout.strip()[:40] if head.returncode == 0 else "",
    }


def _resolve_worktree_info_from_records(
    records: dict[str, dict[str, str]],
    issue_number: int,
) -> dict[str, str] | None:
    pat = re.compile(rf"(?:task-issue-|issue-){issue_number}\b")
    for branch, record in records.items():
        if pat.search(branch):
            return {"branch": branch, **record}
    return None


def resolve_worktree_info_with_health(
    env: dict,
    issue_number: int,
) -> tuple[dict[str, str] | None, str | None]:
    """Find an issue worktree without hiding isolation lookup failures."""
    lookup = fetch_archon_worktrees(env)
    if isinstance(lookup, WorktreeLookup):
        if lookup.error:
            return None, lookup.error
        records = lookup.records
    elif isinstance(lookup, WorktreeRecords):
        if lookup.error:
            return None, lookup.error
        records = lookup
    elif isinstance(lookup, dict):
        # Preserve compatibility with callers/tests that provide legacy records.
        records = lookup
    else:
        return None, "archon_isolation_shape"
    return _resolve_worktree_info_from_records(records, issue_number), None


def resolve_worktree_info(env: dict, issue_number: int) -> dict[str, str] | None:
    """Find an Archon worktree record for an issue, preserving lookup health."""
    info, error = resolve_worktree_info_with_health(env, issue_number)
    return {"error": error} if error else info


def resolve_worktree_branch(env: dict, issue_number: int) -> str | None:
    """Find the full Archon branch name required by `archon continue`."""
    record = resolve_worktree_info(env, issue_number)
    return record.get("branch") if record else None
