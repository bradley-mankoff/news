"""Shared run-log normalization, classification, and concise write policy.

This module is the single policy authority for turning raw child-process /
terminal output into the concise, normalized run-log representation used by:

- the pipeline persistence writer (``pipeline._write_run_log``),
- the UI process reader (``RunRecord.append``),
- DuckDB ``run_logs`` insertion and backfill (``history_store``).

Policies enforced here:

- ``\\r\\n`` is a newline; a lone ``\\r`` is a terminal-line overwrite and only
  the newest segment of each physical line is retained.
- Common ANSI CSI sequences (``ESC[K`` and friends) and stray C0 control
  bytes are removed; ordinary text, tracebacks, and ``data:``-style message
  text are never stripped.
- The ``[progress]`` prefix and the email/unsubscribe wrapper labels are
  normalized consistently with the historical pipeline cleaner.
- Lines matching the ``ProgressTracker`` meter shape are classified as
  ``progress`` events with a stable stage key; stage headers, warnings,
  retries, errors, and tracebacks remain ``message`` events.
- ``ConciseLogWriter`` persists initial/final meter snapshots per stage,
  suppresses intermediate and exact-duplicate snapshots, and flushes a
  pending snapshot when a stage transition or failure closes an active
  meter without ``finish_meter()``.

Only the Python 3.12 standard library is used; no external dependency or
frontend build system is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

EventKind = Literal["message", "progress"]
# High-value message categories used by the live progress contract. ``detail``
# is the default for ordinary lines; the UI suppresses detail events and keeps
# them backend-only, while transitions/warnings/retries/errors/summaries stay
# visible as categorized notices.
MessageCategory = Literal["transition", "warning", "retry", "error", "summary", "detail"]

# ProgressTracker renders meters as:
#   [3/9 clustering] [####------------] 10000/200000 steps
#   [custom] [####------------] 1/3 steps
# with an optional " | detail" suffix. The bar is exactly 20 characters and
# the stage label is the stable per-stage identity. The optional leading
# index/count and the trailing unit are captured as structured metadata.
_METER_RE = re.compile(
    r"^\[(?:(?P<stage_index>\d+)/(?P<stage_count>\d+)\s+)?(?P<stage>[a-zA-Z][a-zA-Z0-9 _-]*)\]\s+"
    r"\[[#\-]{20}\]\s+"
    r"(?P<done>\d+)/(?P<total>\d+)\s+"
    r"(?P<unit>[a-zA-Z][a-zA-Z0-9 _/-]*)(?:\s+\|.*)?$"
)

# A stage-header line such as ``[3/9 clustering] Clustering 139 candidate
# articles.`` names a stable stage but is not a meter (no bar/counts). Meter
# lines are matched first, so this never steals a progress event.
_STAGE_HEADER_RE = re.compile(
    r"^\[(?:\d+/\d+\s+)?(?P<stage>[a-zA-Z][a-zA-Z0-9 _-]*)\]\s+(?P<text>.+)$"
)

_RETRY_RE = re.compile(r"^Retry", re.IGNORECASE)
_WARNING_RE = re.compile(r"^WARNING\b", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"^(?:Traceback \(most recent call last\):|.*(?:Error|Exception)\s*:)",
    re.IGNORECASE,
)
_UI_ERROR_RE = re.compile(r"^\[ui\]\s+.*\b(?:failed|error)\b", re.IGNORECASE)
_SUMMARY_RE = re.compile(
    r"(Daily news run (?:complete|failed)|Run (?:log|history|details|review|Bundle).*saved:|process exited with code|Story clustering:)",
    re.IGNORECASE,
)

# ANSI CSI sequences: ESC [ <params> <intermediate> <final byte>.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Stray C0 controls that should never appear in a readable log (keeps \t and
# \n; \r is handled by the overwrite normalization before this runs).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_PROGRESS_PREFIX_RE = re.compile(r"^\[progress\]\s*")


@dataclass(frozen=True)
class RunLogEvent:
    """One normalized run-log event.

    ``kind`` is ``message`` for ordinary lines or ``progress`` for meter
    snapshots. ``stage`` is the stable stage identity for progress events and
    for transition messages that begin with a stage header. ``replace`` marks
    a snapshot that supersedes the previous snapshot of the same stage.
    ``complete`` is true when a progress snapshot shows the meter at its
    final total. Progress events additionally expose structured ``done`` /
    ``total`` / ``unit`` counts and the optional stage ``index`` / ``count``;
    message events expose a ``category`` for the live notice contract.
    """

    line: str
    kind: EventKind = "message"
    stage: str | None = None
    replace: bool = False
    complete: bool = False
    done: int | None = None
    total: int | None = None
    unit: str | None = None
    stage_index: int | None = None
    stage_count: int | None = None
    category: MessageCategory = "detail"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"line": self.line, "kind": self.kind}
        if self.kind == "progress":
            if self.stage is not None:
                payload["stage"] = self.stage
            if self.replace:
                payload["replace"] = True
            if self.complete:
                payload["complete"] = True
            if self.done is not None:
                payload["done"] = self.done
            if self.total is not None:
                payload["total"] = self.total
            if self.unit is not None:
                payload["unit"] = self.unit
        else:
            payload["category"] = self.category
            if self.stage is not None:
                payload["stage"] = self.stage
        return payload


def clean_line(line: str) -> str:
    """Clean one standalone line (strip + wrapper normalization)."""
    clean = _PROGRESS_PREFIX_RE.sub("", str(line or "").strip())
    return _apply_wrappers(clean)


def _apply_wrappers(clean: str) -> str:
    return clean.replace("--- [EMAIL]:", "[email]").replace(
        "--- [UNSUBSCRIBE]:", "[unsubscribe]"
    )


def _continuation_line(line: str) -> str:
    """Clean a non-first line, preserving meaningful leading indentation."""
    return _apply_wrappers(str(line or "").rstrip())


def _line_after_overwrites(segment: str) -> str:
    """Apply terminal overwrite semantics to one physical line.

    A lone ``\\r`` moves the cursor back to the start of the line, so the
    newest ``\\r``-delimited segment is what a viewer sees. Empty trailing
    segments (a bare carriage return after content) do not erase the line.
    """
    parts = [part for part in segment.split("\r") if part]
    return parts[-1] if parts else ""


def normalize_text(raw: str) -> str:
    """Return the normalized plain-text form of raw terminal/process output.

    Carriage-return overwrites are collapsed, ANSI CSI sequences and stray
    control bytes are removed, and line endings are reduced to ``\\n``.
    """
    text = str(raw or "")
    text = text.replace("\r\n", "\n")
    text = "\n".join(_line_after_overwrites(segment) for segment in text.split("\n"))
    text = _ANSI_CSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text


def normalize_lines(raw: str) -> list[str]:
    """Return normalized, cleaned, non-empty lines from raw output.

    The first line is stripped like the historical whole-message cleaner;
    continuation lines keep their leading indentation so multiline
    tracebacks stay readable.
    """
    lines: list[str] = []
    for index, line in enumerate(normalize_text(raw).split("\n")):
        clean = clean_line(line) if index == 0 else _continuation_line(line)
        if clean:
            lines.append(clean)
    return lines


def _classify_message(clean: str) -> MessageCategory:
    """Classify one normalized non-meter line into a message category.

    Order matters: retries are named before the exception they embed, warnings
    before their text, errors before summary/transition phrasing, and stage
    headers are the last structural marker before the ``detail`` default.
    """
    if _RETRY_RE.match(clean):
        return "retry"
    if _WARNING_RE.match(clean):
        return "warning"
    if _ERROR_RE.match(clean) or _UI_ERROR_RE.match(clean):
        return "error"
    if _SUMMARY_RE.search(clean):
        return "summary"
    if _STAGE_HEADER_RE.match(clean):
        return "transition"
    return "detail"


def parse_event(line: str) -> RunLogEvent:
    """Classify one normalized line into a structured event."""
    clean = _continuation_line(line)
    meter = _METER_RE.match(clean)
    if meter:
        total = int(meter.group("total"))
        done = int(meter.group("done"))
        stage_index = int(meter.group("stage_index")) if meter.group("stage_index") else None
        stage_count = int(meter.group("stage_count")) if meter.group("stage_count") else None
        return RunLogEvent(
            line=clean,
            kind="progress",
            stage=meter.group("stage"),
            complete=total > 0 and done >= total,
            done=done,
            total=total,
            unit=meter.group("unit"),
            stage_index=stage_index,
            stage_count=stage_count,
        )
    category = _classify_message(clean)
    stage = None
    if category == "transition":
        header = _STAGE_HEADER_RE.match(clean)
        if header:
            stage = header.group("stage")
    return RunLogEvent(line=clean, kind="message", category=category, stage=stage)


def parse_stream(raw: str) -> list[RunLogEvent]:
    """Parse raw output into normalized structured events, in order."""
    return [parse_event(line) for line in normalize_lines(raw)]


def normalize_file_text(raw: str) -> str:
    """Whole-file normalization used at history ingestion.

    Beyond ``normalize_text``, consecutive same-stage meter snapshots are
    collapsed to their first and last lines so legacy raw transcripts stay
    concise; meaningful messages, warnings, errors, and tracebacks are
    preserved verbatim. Timestamped lines (the concise writer's format) do
    not match the meter shape and are never collapsed.
    """
    kept: list[str] = []
    run_stage: str | None = None
    run_first: str | None = None
    run_last: str | None = None

    def finish_run() -> None:
        nonlocal run_stage, run_first, run_last
        if run_first is not None:
            kept.append(run_first)
            if run_last is not None and run_last != run_first:
                kept.append(run_last)
        run_stage = None
        run_first = None
        run_last = None

    for event in parse_stream(raw):
        if event.kind == "progress":
            if run_stage == event.stage:
                run_last = event.line
                continue
            finish_run()
            run_stage = event.stage
            run_first = event.line
            run_last = None
        else:
            finish_run()
            kept.append(event.line)
    finish_run()
    return "\n".join(kept)


class ConciseLogWriter:
    """Append-only writer policy for concise persisted run logs.

    - Message lines flush any pending meter snapshot and append.
    - The first meter snapshot of a stage appends immediately; later
      non-final snapshots are held as a single pending line (exact
      duplicates ignored) and flushed when the stage transitions or closes
      without ``finish_meter()``. Final snapshots append immediately and
      supersede any pending intermediate line.
    """

    def __init__(self, write_line: Callable[[str], None]) -> None:
        self._write_line = write_line
        self._pending_stage: str | None = None
        self._pending_line: str | None = None
        self._pending_written = False
        self._last_written_line: str | None = None

    def message(self, line: str) -> None:
        """Append an ordinary message, flushing any pending meter first."""
        self.flush()
        self._write_line(line)

    def meter(self, stage: str, line: str, *, final: bool = False) -> None:
        """Record one meter snapshot for ``stage``.

        ``final`` marks the terminal snapshot (``finish_meter`` or a meter
        that reached its total); it supersedes any pending intermediate line.
        """
        if self._pending_stage is not None and self._pending_stage != stage:
            self.flush()
        if final:
            if self._last_written_line == line:
                return  # exact duplicate terminal snapshot
            self._pending_stage = None
            self._pending_line = None
            self._pending_written = False
            self._write_line(line)
            self._last_written_line = line
            return
        if self._pending_stage == stage:
            if self._pending_line == line:
                return  # exact duplicate snapshot
            self._pending_line = line
            self._pending_written = False
            return
        # First snapshot for this stage: append immediately and remember it
        # so a transition flush can write any later intermediate line.
        self._pending_stage = stage
        self._pending_line = line
        self._pending_written = True
        self._write_line(line)
        self._last_written_line = line

    def flush(self) -> None:
        """Write any pending intermediate meter snapshot, if not yet written."""
        if self._pending_stage is None:
            return
        if not self._pending_written:
            self._write_line(self._pending_line)
            self._last_written_line = self._pending_line
        self._pending_stage = None
        self._pending_line = None
        self._pending_written = False
