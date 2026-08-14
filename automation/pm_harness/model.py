"""Archon run records and recovery classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

ACTIVE_WORKFLOW_STATUSES = frozenset(
    {"running", "pending", "queued", "scheduled", "paused"}
)


WORKFLOW_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


TRANSIENT_WORKFLOW_ERROR_RE = re.compile(
    r"stream ended without finish_reason|websocket closed|connection reset|"
    r"connection aborted|temporarily unavailable|rate limit|timed out|"
    r"\btimeout\b|\bHTTP\s*[45]\d\d\b",
    re.I,
)


ORCHESTRATION_WORKFLOW_ERROR_RE = re.compile(
    r"no open pr found|no pr found for branch|no open pull request|"
    r"verify-pr-base|pull request|orchestrat|merge conflict",
    re.I,
)


VALIDATION_WORKFLOW_ERROR_RE = re.compile(
    r"\b(?:pytest|tests?|compileall|validation|lint|type[- ]?check)\b.*"
    r"\b(?:failed|error|errors|nonzero)\b|"
    r"\b(?:failed|error|errors|nonzero)\b.*"
    r"\b(?:pytest|tests?|compileall|validation|lint|type[- ]?check)\b",
    re.I,
)


STEP_VALIDATION_RE = re.compile(
    r"(?:^|[^a-z])(?:tests?|coverage|lint|type[-_ ]?check|validation|"
    r"pytest|compileall)(?:[^a-z]|$)",
    re.I,
)


RECOVERY_ACTIONS = frozenset({
    "monitoring",
    "retry_available",
    "retrying",
    "resume_required",
    "resumed",
    "manual_review",
    "discarded",
})


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one workflow dispatch attempt."""

    started: bool
    reason: str = ""
    pid: int | None = None

    def __bool__(self) -> bool:
        return self.started


class WorkflowRuns(list[dict]):
    """Run rows plus lookup health; partial rows are usable only by identity."""

    def __init__(
        self,
        runs: list[dict] | None = None,
        *,
        error: str | None = None,
        partial: bool = False,
    ) -> None:
        super().__init__(runs or [])
        self.error = error
        self.partial = partial


class WorkflowRunStatusMap(dict[str, str]):
    """Message/status projection retaining full-run lookup health."""

    def __init__(
        self,
        statuses: dict[str, str] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        super().__init__(statuses or {})
        self.error = error


def parse_run_metadata(run: dict) -> tuple[str, str]:
    """Return bounded error and failed-step text from one run."""
    metadata = run.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    meta = metadata if isinstance(metadata, dict) else {}
    error = meta.get("error") or run.get("error") or ""
    step = (
        meta.get("failed_step")
        or meta.get("step")
        or meta.get("stage")
        or run.get("failed_step")
        or ""
    )
    if isinstance(error, (dict, list)):
        error = json.dumps(error)
    if isinstance(step, (dict, list)):
        step = json.dumps(step)
    return str(error)[:500], str(step)[:200]


def issue_number_from_message(message: str) -> int | None:
    match = re.search(r"\bissue\s+#?(\d+)\b", message or "", re.I)
    return int(match.group(1)) if match else None


def failed_workflow_step(error: str) -> str | None:
    match = re.search(
        r"completed with failures:\s*['\"]([^'\"]+)['\"]",
        error or "",
        re.I,
    )
    return match.group(1) if match else None


def classify_workflow_failure(status: str, error: str = "", step: str = "") -> str:
    """Classify a terminal Archon failure for safe recovery.

    Validation and orchestration failures are never transient: a test,
    coverage, lint, or type-check failure (or a broken PR/verification
    handoff) must not be auto-retried even when the error blob also
    mentions transport text like websocket/timeout.
    """
    normalized_status = (status or "").lower()
    if normalized_status == "cancelled":
        return "transient"
    if normalized_status != "failed":
        return "unknown"
    blob = f"{step or ''}\n{error or ''}"
    if STEP_VALIDATION_RE.search(step or "") or VALIDATION_WORKFLOW_ERROR_RE.search(blob):
        return "validation"
    if ORCHESTRATION_WORKFLOW_ERROR_RE.search(blob):
        return "orchestration"
    if TRANSIENT_WORKFLOW_ERROR_RE.search(error or ""):
        return "transient"
    return "unknown"


def workflow_run_details(run: dict, *, branch: str | None = None) -> dict:
    """Return bounded, board-safe diagnostics for one Archon run."""
    status = str(run.get("status") or "").lower()
    error, metadata_step = parse_run_metadata(run)
    details = {
        "run_id": str(run.get("id") or ""),
        "workflow": str(run.get("workflow_name") or ""),
        "message": str(run.get("user_message") or "")[:1000],
        "status": status or "unknown",
        "started_at": str(run.get("started_at") or ""),
        "completed_at": str(run.get("completed_at") or ""),
        "last_activity_at": str(run.get("last_activity_at") or ""),
        "working_path": str(run.get("working_path") or ""),
        "failed_step": metadata_step or failed_workflow_step(error) or "",
        "failure_class": classify_workflow_failure(
            status, error, metadata_step or failed_workflow_step(error) or ""),
        "error": error,
    }
    if branch:
        details["branch"] = branch
    return details


def recovery_action(status: str, failure_class: str, dirty: bool | None) -> str:
    normalized_status = (status or "").lower()
    if normalized_status in ACTIVE_WORKFLOW_STATUSES or normalized_status == "completed":
        return "monitoring"
    if failure_class != "transient":
        return "manual_review"
    if dirty is False:
        return "retry_available"
    if dirty is True:
        return "resume_required"
    return "manual_review"


def build_recovery_comment(issue_number: int, details: dict, worktree: dict,
                           action: str, *, retry_number: int | None = None) -> str:
    """Build one idempotently-posted explanation for a stopped run."""
    failure = details.get("error") or "no error detail was recorded"
    step = details.get("failed_step") or "unknown step"
    path = worktree.get("path") or details.get("working_path") or "unknown"
    lines = [
        "## Automation recovery",
        f"- Run: `{details.get('run_id') or 'unknown'}`",
        f"- Failed step: `{step}`",
        f"- Classification: `{details.get('failure_class') or 'unknown'}`",
        f"- Worktree: `{path}` "
        f"({'dirty' if worktree.get('dirty') else 'clean' if worktree.get('exists') else 'missing'})",
        f"- Error: {failure[:500]}",
    ]
    if action == "retrying":
        lines.append(f"- Action: automatic transient retry {retry_number or 1}/1 started.")
    elif action == "retry_available":
        lines.append("- Action: one clean-worktree transient retry is available.")
    elif action == "resume_required":
        lines.extend([
            "- Action: do not start a fresh run; resume or explicitly discard the "
            "existing worktree.",
            f"- Inspect: `python3 automation/workflow_recovery.py status {issue_number}`",
            f"- Resume: `python3 automation/workflow_recovery.py resume {issue_number}`",
            f"- Discard: `python3 automation/workflow_recovery.py discard {issue_number}`",
        ])
    else:
        lines.extend([
            "- Action: no automatic retry; inspect the workflow and issue before "
            "requeueing.",
            f"- Inspect: `python3 automation/workflow_recovery.py status {issue_number}`",
        ])
    return "\n".join(lines)
