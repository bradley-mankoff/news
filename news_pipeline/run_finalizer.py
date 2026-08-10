"""Finish one daily news run from recorded outcomes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from . import history_store
from .okf import write_okf_run_bundle
from .diagnostics import RunDiagnostics


@dataclass(frozen=True)
class RunFinalizerConfig:
    run_id: str
    latest_run_details_path: Path
    latest_run_markdown_path: Path
    latest_run_log_path: Path
    history_db_path: Path
    beehiiv_paste_dir: Path
    output_dir: Path
    run_log_path: str
    run_diagnostics_path: Path | None = None
    latest_run_diagnostics_path: Path | None = None
    history_export_csv: bool = True


class CleanupVisibleOutputs(Protocol):
    def __call__(
        self,
        output_dir: Path,
        *,
        keep_paths: Iterable[Path] | None = None,
    ) -> tuple[int, int]: ...


@dataclass(frozen=True)
class RunFinalizerAdapters:
    attach_pending_activity_snapshots: Callable[[RunDiagnostics], None] = lambda diagnostics: None
    model_call_stats_snapshot: Callable[[], dict[str, Any]] = dict
    write_run_history: Callable[..., None] = history_store.write_run_history
    write_okf_run_bundle: Callable[..., Path] = write_okf_run_bundle
    cleanup_visible_outputs: CleanupVisibleOutputs = history_store.cleanup_visible_outputs
    progress: Any | None = None

@dataclass
class RunFinalizer:
    diagnostics: RunDiagnostics
    config: RunFinalizerConfig
    adapters: RunFinalizerAdapters = field(default_factory=RunFinalizerAdapters)
    report_body: str = ""
    candidate_articles: list[dict[str, Any]] | None = None
    summarized_articles: list[dict[str, Any]] | None = None
    selected_articles: list[dict[str, Any]] | None = None
    article_summary_records: list[dict[str, Any]] | None = None
    story_summary_records: list[dict[str, Any]] | None = None
    story_records: list[dict[str, Any]] | None = None

    def record_candidate_articles(self, articles: list[dict[str, Any]]) -> None:
        self.candidate_articles = articles

    def record_summarized_articles(self, articles: list[dict[str, Any]]) -> None:
        self.summarized_articles = articles

    def record_selected_articles(self, articles: list[dict[str, Any]]) -> None:
        self.selected_articles = articles

    def record_article_summary_records(self, records: list[dict[str, Any]]) -> None:
        self.article_summary_records = records

    def record_story_summary_records(self, records: list[dict[str, Any]]) -> None:
        self.story_summary_records = records

    def record_story_records(self, records: list[dict[str, Any]]) -> None:
        self.story_records = records

    def record_report_body(self, report_body: str) -> None:
        self.report_body = report_body

    def finish_failed(self, error: Exception, traceback_text: str) -> None:
        self.diagnostics.event(
            "failed",
            error_type=type(error).__name__,
            error_message=str(error),
            traceback=traceback_text,
        )
        self.finish()

    def finish(self) -> None:
        self.adapters.attach_pending_activity_snapshots(self.diagnostics)
        self.diagnostics.record_model_call_stats(self.adapters.model_call_stats_snapshot())
        self._record_diagnostic_artifact()
        self._write_details()
        self._write_history()
        self._write_okf_run_bundle()
        self._write_review()
        self._write_beehiiv_paste()
        self._cleanup_visible_outputs()

    def _durable_run_diagnostics_path(self) -> Path:
        return self.config.history_db_path.parent / "diagnostics" / f"run_diagnostics_{self.config.run_id}.log"

    def _record_diagnostic_artifact(self) -> None:
        """Record the deterministic durable diagnostic path before history writes.

        The path is recorded before the post-``run_logging()`` copy completes
        so details/history always name it; the copy step is responsible for
        making the file exist before the run session returns. No raw content
        is ever written to DuckDB ``run_logs`` by this metadata record.
        """
        source = Path(self.config.run_diagnostics_path) if self.config.run_diagnostics_path else None
        if source is None:
            return
        rolling = (
            Path(self.config.latest_run_diagnostics_path)
            if self.config.latest_run_diagnostics_path
            else Path("")
        )
        self.diagnostics.record_artifact(
            "run_diagnostics",
            str(self._durable_run_diagnostics_path()),
            representation="diagnostic",
            policy="raw_backend_transcript",
            archive_status="pending",
            source=str(source),
            rolling=str(rolling),
        )

    def _write_details(self) -> None:
        try:
            details_path = self.diagnostics.write_details_json(self.config.latest_run_details_path)
            self._detail(f"Latest run details saved: {details_path}")
        except Exception as error:
            self._warning(f"Latest run details write failed: {error}")

    def _write_history(self) -> None:
        try:
            self.adapters.write_run_history(
                self.config.history_db_path,
                run_id=self.config.run_id,
                diagnostics=self.diagnostics,
                candidate_articles=self.candidate_articles,
                summarized_articles=self.summarized_articles,
                selected_articles=self.selected_articles,
                article_summary_records=self.article_summary_records,
                story_summary_records=self.story_summary_records,
                run_log_path=self.config.run_log_path,
                export_csv=self.config.history_export_csv,
            )
            self._detail("Saved run history.")
            self._detail(f"Run history saved: {self.config.history_db_path}")
        except Exception as error:
            self._warning(f"Run history write failed: {error}")

    def _write_okf_run_bundle(self) -> None:
        try:
            bundle_path = self.adapters.write_okf_run_bundle(
                self.config.history_db_path,
                run_id=self.config.run_id,
                diagnostics=self.diagnostics,
                report_body=self.report_body,
                article_summary_records=self.article_summary_records,
                story_summary_records=self.story_summary_records,
                story_records=self.story_records,
                candidate_articles=self.candidate_articles,
                summarized_articles=self.summarized_articles,
                selected_articles=self.selected_articles,
            )
            self._detail(f"OKF Run Bundle saved: {bundle_path}")
        except Exception as error:
            self._warning(f"OKF Run Bundle write failed: {error}")

    def _write_review(self) -> None:
        try:
            review_path = self.diagnostics.write_run_review_markdown(
                self.config.latest_run_markdown_path,
                report_body=self.report_body,
            )
            self._detail(f"Latest readable run review saved: {review_path}")
        except Exception as error:
            self._warning(f"Latest readable run review write failed: {error}")

    def _write_beehiiv_paste(self) -> None:
        if not self.report_body:
            return
        try:
            target_dir = self.config.beehiiv_paste_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            date_label = (self.config.run_id or "")[:10] or "latest"
            target_path = target_dir / f"{date_label}.md"
            target_path.write_text(self.report_body, encoding="utf-8")
            self._detail(f"Beehiiv paste file saved: {target_path}")
        except Exception as error:
            self._warning(f"Beehiiv paste file write failed: {error}")

    def _cleanup_visible_outputs(self) -> None:
        keep_paths = [
            self.config.latest_run_markdown_path,
            self.config.latest_run_log_path,
            self.config.latest_run_details_path,
        ]
        if self.config.run_diagnostics_path:
            # The timestamped diagnostic source is archived after the concise
            # log context closes; retain it through visible-output cleanup so
            # the durable copy step can still read it.
            keep_paths.append(Path(self.config.run_diagnostics_path))
        try:
            deleted_count, deleted_bytes = self.adapters.cleanup_visible_outputs(
                self.config.output_dir,
                keep_paths=keep_paths,
            )
            if deleted_count:
                self._detail(
                    f"Cleaned {deleted_count} transient daily output file(s) "
                    f"after publishing latest_run.md ({deleted_bytes} bytes)."
                )
        except Exception as error:
            self._warning(f"Visible daily output cleanup failed: {error}")

    def _detail(self, message: str) -> None:
        progress = self.adapters.progress
        if progress is not None:
            progress.detail(message)

    def _warning(self, message: str) -> None:
        progress = self.adapters.progress
        if progress is not None:
            progress.warning(message)


def archive_run_diagnostics(source_path: Path, target_path: Path) -> None:
    """Atomically move a closed per-run diagnostic transcript into durable storage.

    A missing source is a no-op. The copy is written to a temporary sibling
    and renamed into place so a partially-written archive is never visible;
    the staging source is removed only after the durable copy exists. Errors
    propagate to the caller, which must treat them as warnings that never
    replace the primary run result.
    """
    if not source_path.exists():
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.tmp")
    shutil.copyfile(source_path, temp_path)
    temp_path.replace(target_path)
    source_path.unlink(missing_ok=True)
