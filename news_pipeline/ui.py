"""Local browser control panel for the daily news pipeline."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shlex
import subprocess
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from . import run_log
from . import scheduler as scheduler_module
from .config import (
    MODEL_TASK_IMAGE_ART_DIRECTION,
    MODEL_TUNING_PRESET_ENV_VARS,
    MODEL_TUNING_PRESETS_PATH,
    REMOVED_TOPIC_ENV_VARS,
    _MAX_TOKENS_TUNING_FIELDS,
    ROOT_DIR,
    RUN_PRESETS_PATH,
    SOURCE_SCOPES,
    RECIPIENT_SCOPES,
    VALID_SOURCE_MATCH_MODES,
    configured_removed_topic_env_vars,
    load_run_presets,
    load_model_tuning_presets,
    load_recipients,
    load_sources,
    normalize_preset_id,
    run_preset_env,
    resolve_runtime_config,
    RuntimeConfigRequest,
    runtime_knob_registry,
)
from .diagnostics import _duration_label, run_status_from_events
from .history_store import (
    _normalize_delivery_recipients,
    get_run_details,
    list_recent_run_summaries,
)
from .okf import okf_run_bundle_path
from .source_catalog import (
    DeleteSources,
    UpsertSource,
    apply_source_catalog_patch,
    load_source_records,
)
from .prompt_catalog import (
    DEFAULT_PROMPT_PROFILE_ID,
    compare_prompt_profiles,
    list_prompt_profiles,
)
from .model_catalog import (
    MODEL_RECOMMENDATION_TASKS,
    MODEL_TASK_LABELS,
    RUNTIME_FIT_LABELS,
    fetch_model_metadata,
    list_model_catalog,
    recommend_models,
    search_huggingface_models,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
RECIPIENT_HEADER = """# Additional email recipients for generated reports.
#
# This is a public template: real addresses and credentials belong in a local
# recipient file referenced by NEWS_RECIPIENTS_YAML (or env/ignored env.json),
# never in tracked config. The owner recipient is configured separately with
# NEWS_PRIMARY_RECIPIENT.
#
# Usage:
# - pause: true keeps the recipient configured but skips delivery
#   (skipped: user_disabled).
# - NEWS_DELIVERY_MODE=recipients selects the active entries below; the owner
#   is included only when listed here.
"""
RUN_PRESET_HEADER = """# Saved run presets for the daily news pipeline.
#
# Run Presets are env-style defaults. Shell/UI overrides still win.
"""
MODEL_TUNING_PRESET_HEADER = """# Saved model tuning presets for the daily news pipeline.
#
# Model Tuning Presets are explicit overlays for a model or model-task pair.
"""


def build_knob_registry() -> list[dict[str, Any]]:
    return runtime_knob_registry()


def _config_path_from_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


def _mask_secret(value: str | None) -> str:
    return "********" if value else ""


def _is_secret_env_key(key: str) -> bool:
    upper_key = key.upper()
    return any(token in upper_key for token in ("PASSWORD", "SECRET", "API_KEY", "TOKEN"))


def _display_env(env: dict[str, str]) -> dict[str, str]:
    """Return an environment projection safe for API responses and previews."""
    return {
        key: _mask_secret(value) if _is_secret_env_key(key) else value
        for key, value in env.items()
    }


def _now_iso_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_ready(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _json_ready(val) for key, val in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _clean_env_for_config(environ: dict[str, str]) -> dict[str, str]:
    clean = dict(environ)
    for name in REMOVED_TOPIC_ENV_VARS:
        clean.pop(name, None)
    return clean


def _ui_base_env(preset_id: str | None, overrides: dict[str, str]) -> dict[str, str]:
    base_env = _clean_env_for_config(dict(os.environ))
    if not preset_id:
        return base_env
    try:
        preset_env = run_preset_env(preset_id)
    except ValueError:
        return base_env
    for name in preset_env:
        if name not in overrides:
            base_env.pop(name, None)
    return base_env


def _preset_env_over_inherited_env(preset_id: str | None, overrides: dict[str, str]) -> dict[str, str]:
    if not preset_id:
        return {}
    try:
        preset_env = run_preset_env(preset_id)
    except ValueError:
        return {}
    return {
        name: value
        for name, value in preset_env.items()
        if name not in overrides and os.environ.get(name) not in {None, "", value}
    }


def _runtime_snapshot(
    env_overlay: dict[str, str] | None = None,
    *,
    preset_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    env_overlay = env_overlay or {}
    base_env = _ui_base_env(preset_id, env_overlay)
    try:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env=base_env,
                preset_id=preset_id,
                overrides=env_overlay,
                materialize_outputs=False,
            )
        )
        config = resolution.config
        task_keys = (
            "article_summary",
            "story_drafting",
            "story_scale_screening",
            "title_generation",
            "image_art_direction",
        )
        missing = [task for task in task_keys if task not in config.model_assignments]
        if missing:
            raise KeyError(f"runtime config missing model assignments for tasks: {missing}")
        assignments = _json_ready(config.model_assignments)
        return {
            "preset_id": config.preset_id or "custom",
            "prompt_profile_id": config.prompt_profile_id,
            "source_scope": config.source_scope,
            "recipient_scope": config.recipient_scope,
            "url_reuse_blocking_enabled": config.url_reuse_blocking_enabled,
            "relaxed_story_drafting_guards": config.relaxed_story_drafting_guards,
            "paths": {
                "sources": str(config.sources_path),
                "recipients": str(config.recipients_path),
                "output_dir": str(config.output_dir),
                "run_output_dir": str(config.run_output_dir),
                "run_used_urls_path": str(config.run_used_urls_path),
            },
            "model": {
                "reference": config.model_reference,
                "name": config.model_name,
                "backend": config.model_backend,
                "base_url": config.model_base_url,
                "concurrency": config.model_concurrency,
                "concurrency_source": "derived_from_model_stage_concurrency",
                "article_summary_concurrency": config.article_summary_concurrency,
                "story_synthesis_concurrency": config.story_synthesis_concurrency,
                "server_command": config.model_server_command,
                "assignments": assignments,
                "article_summary": assignments["article_summary"],
                "story_drafting": assignments["story_drafting"],
                "story_scale_screening": assignments["story_scale_screening"],
                "title_generation": assignments["title_generation"],
                "image_art_direction": assignments["image_art_direction"],
                "tuning": _json_ready(config.model_tuning),
                "pipeline_budget": _json_ready(config.pipeline_budget),
                "server_settings": _json_ready(config.model_server_settings),
            },
            "funnel": {
                "recent_window_hours": config.recent_window_hours,
                "source_collection_concurrency": config.source_collection_concurrency,
                "max_articles_per_source": config.max_articles_per_source,
                "min_articles_per_story": config.min_articles_per_story,
                "story_cluster_similarity_threshold": config.story_cluster_similarity_threshold,
                "story_scale_screening_enabled": config.story_scale_screening_enabled,
                "max_stories": config.max_stories,
                "story_selection_overlap_threshold": config.story_selection_overlap_threshold,
                "story_embedding_dedup_threshold": config.story_embedding_dedup_threshold,
                "story_backfill_batch_multiplier": config.story_backfill_batch_multiplier,
            },
            "image": {
                "enabled": config.image_generation_enabled,
                "fail_on_error": config.image_generation_fail_on_error,
                "width": config.image_width,
                "height": config.image_height,
                "steps": config.image_steps,
                "crop_bottom_ratio": config.image_crop_bottom_ratio,
                "model_id": config.image_model_id,
                "base_model": config.image_base_model,
            },
            "delivery": {
                **config.delivery_profile.public_snapshot(),
                "primary_recipient": config.primary_recipient,
                "fallback_recipients": config.email_recipients_fallback,
                "email_from": config.email_from,
                "unsubscribe_base_url": config.unsubscribe_base_url,
                "unsubscribe_host": config.unsubscribe_host,
                "unsubscribe_port": config.unsubscribe_port,
            },
        }, None
    except Exception as exc:  # pragma: no cover - surfaced to browser
        return None, str(exc)


def _source_summary() -> dict[str, Any]:
    path = _config_path_from_env("NEWS_SOURCES_YAML", "config/sources.yaml")
    try:
        payload = _load_yaml_mapping(path)
        records = payload.get("sources", [])
        if not isinstance(records, list):
            records = []
        selected = {}
        for scope in SOURCE_SCOPES:
            try:
                selected[scope] = len(load_sources(path=path, source_scope=scope))
            except Exception:
                selected[scope] = None
        tiers: dict[str, int] = {}
        languages: dict[str, int] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            tiers[str(record.get("tier") or "peripheral")] = tiers.get(str(record.get("tier") or "peripheral"), 0) + 1
            languages[str(record.get("language") or "")] = languages.get(str(record.get("language") or ""), 0) + 1
        return {
            "path": str(path),
            "total": len(records),
            "selected": selected,
            "tiers": tiers,
            "languages": languages,
            "error": None,
        }
    except Exception as exc:
        return {"path": str(path), "total": 0, "selected": {}, "tiers": {}, "languages": {}, "error": str(exc)}


def _recipient_summary() -> dict[str, Any]:
    path = _config_path_from_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml")
    try:
        recipients = load_recipients(path=path)
        paused = sum(1 for recipient in recipients.values() if recipient.get("pause"))
        return {"path": str(path), "total": len(recipients), "paused": paused, "error": None}
    except Exception as exc:
        return {"path": str(path), "total": 0, "paused": 0, "error": str(exc)}


def _review_paths() -> dict[str, Path]:
    """Resolve the read-only review artifacts without a runtime snapshot."""
    output_dir = _config_path_from_env("NEWS_OUTPUT_DIR", "output/daily_outputs")
    history_db = _config_path_from_env("NEWS_HISTORY_DB", "output/history/news_history.duckdb")
    return {
        "output_dir": output_dir,
        "history_db": history_db,
        "latest_run_markdown": output_dir / "latest_run.md",
        "latest_run_details": output_dir / "latest_run_details.json",
    }


def _delivery_status_label(delivery: dict[str, Any]) -> str:
    status = str((delivery or {}).get("status") or "").strip()
    return status or "not recorded"


def _report_generated_from_details(details: dict[str, Any]) -> bool:
    explicit = details.get("report_generated")
    if isinstance(explicit, bool):
        return explicit
    reports = details.get("reports")
    if isinstance(reports, list):
        return bool(reports)
    try:
        return int(details.get("report_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def _run_id_from_started_at(started_at: str) -> str:
    if not started_at:
        return ""
    return started_at.replace("T", "_").replace(":", "-")[:19]


def _details_field(
    details: dict[str, Any],
    key: str,
    shape: str,
    errors: dict[str, str],
    *,
    default: Any,
) -> Any:
    """Read one latest-details field with a bounded shape diagnostic.

    ``None`` and absent keys keep the safe default without a warning; a
    wrong top-level shape (or a non-object list item) records a concise
    field-specific error and returns the default so unrelated fields and
    the rolling review text remain usable.
    """
    value = details.get(key)
    if value is None:
        return default
    if shape == "list":
        if not isinstance(value, list):
            if key not in errors:
                errors[key] = "expected a JSON list"
            return default
        items = [item for item in value if isinstance(item, dict)]
        if len(items) != len(value) and key not in errors:
            errors[key] = "expected object list items"
        return items
    if not isinstance(value, dict):
        if key not in errors:
            errors[key] = "expected a JSON object"
        return default
    return value


def latest_review_payload() -> dict[str, Any]:
    """Combine the rolling ``latest_run.md``/details into one JSON payload.

    Tolerant read: a missing DuckDB, details file, or review file degrades to
    a visible empty/error state instead of failing UI boot. The details
    document and review Markdown are read independently: recoverable metadata
    corruption is reported through ``metadata_read_errors`` (normally ``{}``)
    while readable review text and valid detail fields stay available.
    ``error`` remains reserved for unexpected whole-operation failures.
    """
    paths = _review_paths()
    metadata_read_errors: dict[str, str] = {}
    payload: dict[str, Any] = {
        "run_id": "",
        "run_started_at": "",
        "run_status": "unknown",
        "report_status": "not_generated",
        "delivery_status": "not recorded",
        "delivery": {},
        "preset_id": "custom",
        "duration_label": "",
        "report_text": "",
        "metadata_read_errors": metadata_read_errors,
        "error": None,
        "paths": {key: str(value) for key, value in paths.items()},
    }
    details: dict[str, Any] = {}
    if paths["latest_run_details"].exists():
        try:
            raw = paths["latest_run_details"].read_text(encoding="utf-8") or "{}"
        except (OSError, UnicodeError) as exc:
            metadata_read_errors["latest_run_details"] = (
                f"could not decode details file ({type(exc).__name__})"
            )
        else:
            try:
                parsed = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                metadata_read_errors["latest_run_details"] = (
                    f"invalid JSON details ({type(exc).__name__})"
                )
            else:
                if not isinstance(parsed, dict):
                    metadata_read_errors["latest_run_details"] = (
                        "expected a JSON object"
                    )
                else:
                    details = parsed
    events = _details_field(
        details, "events", "list", metadata_read_errors, default=[]
    )
    settings = _details_field(
        details, "settings", "object", metadata_read_errors, default={}
    )
    delivery = _normalize_delivery_recipients(
        _details_field(
            details, "delivery", "object", metadata_read_errors, default={}
        ),
        metadata_read_errors,
    )
    reports = _details_field(
        details, "reports", "list", metadata_read_errors, default=[]
    )
    report_generated = details.get("report_generated")
    if report_generated is not None and not isinstance(report_generated, bool):
        if "report_generated" not in metadata_read_errors:
            metadata_read_errors["report_generated"] = "expected a boolean"
    started_at = str(details.get("run_started_at") or "")
    run_status = run_status_from_events(events)
    report_text = ""
    if paths["latest_run_markdown"].exists():
        try:
            report_text = paths["latest_run_markdown"].read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            metadata_read_errors["latest_run_markdown"] = (
                f"could not read review file ({type(exc).__name__})"
            )
    try:
        payload.update(
            {
                "run_id": _run_id_from_started_at(started_at),
                "run_started_at": started_at,
                "run_status": run_status,
                "report_status": (
                    "available"
                    if run_status == "completed"
                    and _report_generated_from_details(details)
                    and report_text.strip()
                    else (
                        "unavailable"
                        if run_status == "completed"
                        and _report_generated_from_details(details)
                        else "not_generated"
                    )
                ),
                "delivery_status": _delivery_status_label(delivery),
                "delivery": delivery,
                "preset_id": str(settings.get("preset_id") or "custom"),
                "duration_label": _duration_label(started_at, events),
                "report_text": report_text,
            }
        )
    except Exception as exc:
        payload["error"] = str(exc)
    return payload


def recent_history_payload(limit: int = 20) -> dict[str, Any]:
    """Return durable recent-run summaries, degrading errors to a field."""
    paths = _review_paths()
    try:
        summaries = list_recent_run_summaries(paths["history_db"], limit=limit)
        return {"runs": summaries, "path": str(paths["history_db"]), "error": None}
    except Exception as exc:
        return {"runs": [], "path": str(paths["history_db"]), "error": str(exc)}


def run_detail_payload(run_id: str) -> dict[str, Any] | None:
    """Return one decoded run from durable history, or ``None`` when absent."""
    return get_run_details(_review_paths()["history_db"], run_id)


def read_historical_report(run_id: str) -> str | None:
    """Return the stable OKF ``report.md`` text for one run, or ``None``.

    Only runs present in durable history are readable, and the resolved report
    path must stay inside the run's OKF bundle root; there is no arbitrary
    path-based report route.
    """
    paths = _review_paths()
    details = get_run_details(paths["history_db"], run_id)
    if details is None or details.get("report_status") != "available":
        return None
    okf_root = okf_run_bundle_path(paths["history_db"], run_id)
    report_path = (okf_root / "report.md").resolve()
    if not report_path.is_relative_to(okf_root.resolve()):
        return None
    if not report_path.is_file():
        return None
    return report_path.read_text(encoding="utf-8", errors="replace")


def schedule_payload() -> dict[str, Any]:
    """Safe Schedule tab projection (bounded fixed fields only).

    Never returns stored env maps, plist XML, launchctl output, report text,
    or credentials; the scheduler module owns the redacted shape.
    """
    return scheduler_module.schedule_status()


def update_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist schedule changes through the scheduler module.

    The UI endpoint is an adapter, not a second precedence implementation;
    invalid time/preset/mode raises and the caller returns 400.
    """
    time_value = str(body.get("time") or "").strip()
    preset_id = str(body.get("preset_id") or body.get("preset") or "").strip()
    raw_mode = body.get("delivery_mode")
    raw_overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
    if not time_value:
        raise ValueError("Schedule time is required (HH:MM, 24-hour local time).")
    scheduler_module.enable_schedule(
        time_value,
        preset_id=preset_id,
        delivery_mode=raw_mode if raw_mode not in (None, "") else None,
        overrides=raw_overrides,
    )
    return schedule_payload()


def disable_schedule_payload() -> dict[str, Any]:
    scheduler_module.disable_schedule()
    return schedule_payload()


def schema_payload() -> dict[str, Any]:
    knobs = build_knob_registry()
    current_env = {}
    for knob in knobs:
        raw = os.environ.get(knob["env"], "")
        current_env[knob["env"]] = _mask_secret(raw) if knob.get("secret") else raw
    runtime, runtime_error = _runtime_snapshot()
    removed = configured_removed_topic_env_vars()
    return {
        "source_scopes": list(SOURCE_SCOPES),
        "recipient_scopes": list(RECIPIENT_SCOPES),
        "actions": [
            "run",
            "check-sources",
            "prune-sources",
            "source-languages",
            "model-server-command",
            "codex-model-server-command",
            "serve-unsubscribe",
        ],
        "knobs": knobs,
        "current_env": current_env,
        "removed_topic_env_vars": sorted(removed),
        "runtime": runtime,
        "runtime_error": runtime_error,
        "presets": list_presets(),
        "model_tuning_presets": list_model_tuning_presets(),
        "prompt_profiles": list_prompt_profiles(),
        "model_catalog": list_model_catalog(),
        "model_recommendations": {
            task: recommend_models(task) for task in MODEL_RECOMMENDATION_TASKS
        },
        "model_recommendation_tasks": list(MODEL_RECOMMENDATION_TASKS),
        "model_task_labels": dict(MODEL_TASK_LABELS),
        "runtime_fit_labels": dict(RUNTIME_FIT_LABELS),
        "sources": _source_summary(),
        "recipients": _recipient_summary(),
        "source_match_modes": sorted(VALID_SOURCE_MATCH_MODES),
    }


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _write_yaml_mapping(path: Path, payload: dict[str, Any], *, header: str = "") -> None:
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path.write_text((header.rstrip() + "\n" if header else "") + text, encoding="utf-8")


def _preset_records() -> dict[str, dict[str, Any]]:
    return load_run_presets(RUN_PRESETS_PATH)


def _write_presets(records: dict[str, dict[str, Any]]) -> None:
    payload = {"presets": {}}
    for preset_id in sorted(records):
        record = records[preset_id]
        payload_record: dict[str, Any] = {
            "name": record.get("name") or preset_id,
            "description": record.get("description") or "",
            "env": dict(record.get("env") or {}),
        }
        modified_at = str(record.get("modified_at") or "").strip()
        if modified_at:
            payload_record["modified_at"] = modified_at
        payload["presets"][preset_id] = payload_record
    _write_yaml_mapping(RUN_PRESETS_PATH, payload, header=RUN_PRESET_HEADER)


def list_presets() -> dict[str, Any]:
    records = _preset_records()
    return {
        "path": str(RUN_PRESETS_PATH),
        "presets": [records[preset_id] for preset_id in sorted(records)],
    }


def _coerce_preset_env(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    env: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or value is None:
            continue
        text = str(value).strip()
        if text:
            env[name] = text
    return env


def upsert_preset(body: dict[str, Any], *, append_only: bool = False) -> dict[str, Any]:
    preset_id = normalize_preset_id(str(body.get("id") or body.get("preset_id") or ""))
    if not preset_id:
        raise ValueError("Preset id is required.")
    records = _preset_records()
    if append_only and preset_id in records:
        raise ValueError(f"Preset {preset_id!r} already exists.")
    existing = records.get(preset_id, {"id": preset_id, "name": preset_id, "description": "", "env": {}})
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    env_source = body if "env" in body else updates if isinstance(updates, dict) and "env" in updates else None
    env = _coerce_preset_env(env_source.get("env")) if env_source is not None else dict(existing.get("env") or {})
    record = {
        "id": preset_id,
        "name": str(updates.get("name") or existing.get("name") or preset_id).strip(),
        "description": str(updates.get("description") or existing.get("description") or "").strip(),
        "env": env,
        "modified_at": _now_iso_local(),
    }
    records[preset_id] = record
    _write_presets(records)
    return {"path": str(RUN_PRESETS_PATH), "preset": record}


def delete_preset(preset_id: str) -> dict[str, Any]:
    clean_id = normalize_preset_id(preset_id)
    records = _preset_records()
    if clean_id not in records:
        raise ValueError(f"Preset {preset_id!r} not found.")
    del records[clean_id]
    _write_presets(records)
    return {"path": str(RUN_PRESETS_PATH), "deleted": clean_id}


def duplicate_preset(body: dict[str, Any]) -> dict[str, Any]:
    source_id = normalize_preset_id(str(body.get("source_id") or body.get("source") or ""))
    target_id = normalize_preset_id(str(body.get("target_id") or body.get("target") or ""))
    if not source_id or not target_id:
        raise ValueError("Source and target preset ids are required.")
    records = _preset_records()
    if source_id not in records:
        raise ValueError(f"Preset {source_id!r} not found.")
    if target_id in records:
        raise ValueError(f"Preset {target_id!r} already exists.")
    source = records[source_id]
    records[target_id] = {
        "id": target_id,
        "name": str(body.get("name") or f"{source.get('name') or source_id} copy").strip(),
        "description": str(source.get("description") or "").strip(),
        "env": dict(source.get("env") or {}),
        "modified_at": _now_iso_local(),
    }
    _write_presets(records)
    return {"path": str(RUN_PRESETS_PATH), "preset": records[target_id]}


def _model_tuning_preset_records() -> dict[str, dict[str, Any]]:
    return load_model_tuning_presets(MODEL_TUNING_PRESETS_PATH)


def _write_model_tuning_presets(records: dict[str, dict[str, Any]]) -> None:
    payload = {"presets": {}}
    for preset_id in sorted(records):
        record = records[preset_id]
        payload_record: dict[str, Any] = {
            "name": record.get("name") or preset_id,
            "description": record.get("description") or "",
            "tuning": dict(record.get("tuning") or {}),
        }
        model = str(record.get("model") or "").strip()
        task = str(record.get("task") or "").strip()
        if model:
            payload_record["model"] = model
        if task:
            payload_record["task"] = task
        modified_at = str(record.get("modified_at") or "").strip()
        if modified_at:
            payload_record["modified_at"] = modified_at
        payload["presets"][preset_id] = payload_record
    _write_yaml_mapping(MODEL_TUNING_PRESETS_PATH, payload, header=MODEL_TUNING_PRESET_HEADER)


def list_model_tuning_presets() -> dict[str, Any]:
    records = _model_tuning_preset_records()
    return {
        "path": str(MODEL_TUNING_PRESETS_PATH),
        "presets": [records[preset_id] for preset_id in sorted(records)],
    }


def _coerce_optional_mapping(body: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    if field_name in body:
        raw = body.get(field_name)
        if raw in (None, ""):
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        return {}
    return None


# Selectable model-tuning preset task scopes mirror MODEL_TUNING_PRESET_ENV_VARS
# minus the global "default" overlay. Story Discovery has no LLM stage and is
# not a selectable preset assignment; all five configured LLM tasks are.
_MODEL_TUNING_PRESET_TASK_SCOPES: tuple[str, ...] = tuple(
    task for task in MODEL_TUNING_PRESET_ENV_VARS if task != "default"
)
# The six sampling knobs accepted by the runtime model-tuning overlay.
_MODEL_TUNING_SAMPLING_FIELDS: frozenset[str] = frozenset(
    {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"}
)
# The runtime shorthand max_tokens plus the canonical per-task max-token fields
# (model_max_input_tokens and the four task *_max_tokens fields), derived from
# the config task map so the allowed-field set cannot drift from runtime.
_MODEL_TUNING_MAX_TOKEN_FIELDS: frozenset[str] = frozenset(_MAX_TOKENS_TUNING_FIELDS) | frozenset(
    {"max_tokens"}
)
# Sampling knob ranges mirror runtime_knob_registry(): top_k is a whole number
# >= 0 with no upper bound; the remaining knobs are finite closed intervals.
_MODEL_TUNING_SAMPLING_RANGES: dict[str, tuple[float, float | None]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "top_k": (0.0, None),
    "min_p": (0.0, 1.0),
    "presence_penalty": (-2.0, 2.0),
    "repetition_penalty": (0.0, 3.0),
}


def _coerce_model_tuning_number(value: Any) -> float:
    """Coerce a non-integer tuning value to a finite float for range checks.

    Integer-valued fields use :func:`_coerce_model_tuning_integer` instead so
    their accepted string spellings remain compatible with runtime ``int()``
    coercion.
    """
    if isinstance(value, bool):
        raise ValueError(f"got {value!r}, expected a number")
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise ValueError(f"got {value!r}, expected a number") from None
    elif isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            raise ValueError(f"got {value!r}, expected a finite number") from None
    else:
        raise ValueError(f"got {value!r}, expected a number")
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"got {value!r}, expected a finite number")
    return number


def _coerce_model_tuning_integer(value: Any) -> int:
    """Coerce an integer-valued tuning field using the runtime ``int()`` contract."""
    if isinstance(value, bool):
        raise ValueError(f"got {value!r}, expected a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ValueError(f"got {value!r}, expected a whole number") from None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"got {value!r}, expected a whole number")


def _validate_model_tuning_sampling_value(preset_id: str, field_name: str, value: Any) -> None:
    if field_name == "top_k":
        try:
            number = _coerce_model_tuning_integer(value)
        except ValueError:
            raise ValueError(
                f"Model tuning preset {preset_id!r} field {field_name!r} must be a "
                f"whole number, got {value!r}."
            ) from None
    else:
        try:
            number = _coerce_model_tuning_number(value)
        except ValueError:
            raise ValueError(
                f"Model tuning preset {preset_id!r} field {field_name!r} must be a number, "
                f"got {value!r}."
            ) from None
    minimum, maximum = _MODEL_TUNING_SAMPLING_RANGES[field_name]
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        bound = (
            f"at least {minimum:g}"
            if maximum is None
            else f"between {minimum:g} and {maximum:g}"
        )
        raise ValueError(
            f"Model tuning preset {preset_id!r} field {field_name!r} must be "
            f"{bound}, got {value!r}."
        )


def _validate_model_tuning_token_value(preset_id: str, field_name: str, value: Any) -> None:
    try:
        number = _coerce_model_tuning_integer(value)
    except ValueError:
        raise ValueError(
            f"Model tuning preset {preset_id!r} field {field_name!r} must be a "
            f"whole number greater than zero, got {value!r}."
        ) from None
    if number < 1:
        raise ValueError(
            f"Model tuning preset {preset_id!r} field {field_name!r} must be a "
            f"whole number greater than zero, got {value!r}."
        )


def _validate_model_tuning_record(preset_id: str, record: Mapping[str, Any]) -> None:
    """Validate a complete model-tuning record before it is persisted.

    Mirrors the runtime vocabulary and bounds in news_pipeline/config.py while
    applying the stricter integer spelling contract used by runtime ``int()``
    coercion. Neither the dedicated editor nor the Advanced Settings task
    panels can write tuning that runtime resolution would reject. The record is
    validated as-is; accepted field names and values are stored unchanged.
    """
    task = str(record.get("task") or "").strip()
    if task and task not in _MODEL_TUNING_PRESET_TASK_SCOPES:
        if task == MODEL_TASK_IMAGE_ART_DIRECTION:
            raise ValueError(
                f"Model tuning preset {preset_id!r} task {task!r} is not selectable; "
                "use title_generation because image art direction shares that call."
            )
        raise ValueError(
            f"Model tuning preset {preset_id!r} task {task!r} is not selectable; "
            f"supported task scopes: {', '.join(_MODEL_TUNING_PRESET_TASK_SCOPES)}."
        )
    tuning = record.get("tuning", {})
    if tuning in (None, ""):
        tuning = {}
    if not isinstance(tuning, dict):
        raise ValueError(f"Model tuning preset {preset_id!r} tuning must be a mapping.")
    supported_fields = sorted(_MODEL_TUNING_SAMPLING_FIELDS | _MODEL_TUNING_MAX_TOKEN_FIELDS)
    for field_name, value in tuning.items():
        field = str(field_name or "").strip()
        if field in _MODEL_TUNING_SAMPLING_FIELDS:
            _validate_model_tuning_sampling_value(preset_id, field, value)
        elif field in _MODEL_TUNING_MAX_TOKEN_FIELDS:
            _validate_model_tuning_token_value(preset_id, field, value)
        else:
            raise ValueError(
                f"Model tuning preset {preset_id!r} has unsupported tuning field "
                f"{field_name!r}. Supported fields: {', '.join(supported_fields)}."
            )


def _coerce_model_tuning_mapping(
    preset_id: str, body: Mapping[str, Any], field_name: str
) -> dict[str, Any] | None:
    """Strict tuning coercion for the model-tuning upsert boundary.

    Returns None when the field is omitted so a metadata-only PATCH preserves
    the existing mapping; an explicit null, array, scalar, or non-empty string
    raises instead of silently collapsing to an empty mapping.
    """
    if field_name not in body:
        return None
    raw = body.get(field_name)
    if raw == "":
        return {}
    if raw is None or not isinstance(raw, dict):
        raise ValueError(f"Model tuning preset {preset_id!r} tuning must be a mapping.")
    return dict(raw)


def _merged_model_tuning_text(
    existing: Mapping[str, Any], updates: Mapping[str, Any], field_name: str
) -> str:
    if field_name in updates:
        return str(updates.get(field_name) or "").strip()
    return str(existing.get(field_name) or "").strip()


def upsert_model_tuning_preset(body: dict[str, Any], *, append_only: bool = False) -> dict[str, Any]:
    preset_id = normalize_preset_id(str(body.get("id") or body.get("preset_id") or ""))
    if not preset_id:
        raise ValueError("Preset id is required.")
    records = _model_tuning_preset_records()
    if append_only and preset_id in records:
        raise ValueError(f"Preset {preset_id!r} already exists.")
    existing = records.get(preset_id, {"id": preset_id, "name": preset_id, "description": "", "tuning": {}})
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    tuning = _coerce_model_tuning_mapping(preset_id, body, "tuning")
    if tuning is None and isinstance(updates, dict):
        tuning = _coerce_model_tuning_mapping(preset_id, updates, "tuning")
    record = {
        "id": preset_id,
        "name": str(updates.get("name") or existing.get("name") or preset_id).strip(),
        "description": _merged_model_tuning_text(existing, updates, "description"),
        "model": _merged_model_tuning_text(existing, updates, "model"),
        "task": _merged_model_tuning_text(existing, updates, "task"),
        "tuning": tuning if tuning is not None else dict(existing.get("tuning") or {}),
        "modified_at": _now_iso_local(),
    }
    _validate_model_tuning_record(preset_id, record)
    records[preset_id] = record
    _write_model_tuning_presets(records)
    return {"path": str(MODEL_TUNING_PRESETS_PATH), "preset": record}


def delete_model_tuning_preset(preset_id: str) -> dict[str, Any]:
    clean_id = normalize_preset_id(preset_id)
    records = _model_tuning_preset_records()
    if clean_id not in records:
        raise ValueError(f"Preset {preset_id!r} not found.")
    del records[clean_id]
    _write_model_tuning_presets(records)
    return {"path": str(MODEL_TUNING_PRESETS_PATH), "deleted": clean_id}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def list_sources() -> dict[str, Any]:
    path = _config_path_from_env("NEWS_SOURCES_YAML", "config/sources.yaml")
    records = load_source_records(path)
    return {"path": str(path), "sources": records}


def upsert_source(body: dict[str, Any], *, append_only: bool = False) -> dict[str, Any]:
    path = _config_path_from_env("NEWS_SOURCES_YAML", "config/sources.yaml")
    key = str(body.get("key") or "").strip()
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    if not key and isinstance(updates, dict):
        key = str(updates.get("key") or "").strip()
    if not key:
        raise ValueError("Source key is required.")

    result = apply_source_catalog_patch(path, [UpsertSource(key, dict(updates or {}), append_only=append_only)])
    result_key = str(dict(updates or {}).get("key") or key).strip()
    record = next((record for record in result.records if str(record.get("key") or "").strip() == result_key), {})
    return {"path": result.path, "source": record}


def delete_source(key: str) -> dict[str, Any]:
    path = _config_path_from_env("NEWS_SOURCES_YAML", "config/sources.yaml")
    result = apply_source_catalog_patch(path, [DeleteSources({key})])
    if result.edit_count == 0:
        raise ValueError(f"Source {key!r} not found.")
    return {"path": result.path, "deleted": key}


def list_recipients() -> dict[str, Any]:
    path = _config_path_from_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml")
    payload = _load_yaml_mapping(path)
    records = payload.get("recipients", [])
    if not isinstance(records, list):
        records = []
    return {"path": str(path), "recipients": [record for record in records if isinstance(record, dict)]}


def _write_recipients(records: list[dict[str, Any]]) -> None:
    path = _config_path_from_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml")
    _write_yaml_mapping(path, {"recipients": records}, header=RECIPIENT_HEADER)


def upsert_recipient(body: dict[str, Any], *, append_only: bool = False) -> dict[str, Any]:
    email = str(body.get("email") or "").strip()
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    if not email and isinstance(updates, dict):
        email = str(updates.get("email") or "").strip()
    if "@" not in email:
        raise ValueError("Valid recipient email is required.")
    records = list_recipients()["recipients"]
    target = next((record for record in records if str(record.get("email") or "").strip().lower() == email.lower()), None)
    if append_only and target:
        raise ValueError(f"Recipient {email!r} already exists.")
    if target is None:
        target = {"email": email, "name": email, "pause": False}
        records.append(target)
    for field in ("email", "name"):
        if field in updates:
            target[field] = str(updates[field] or "").strip()
    if "pause" in updates:
        target["pause"] = _coerce_bool(updates["pause"])
    _write_recipients(records)
    return {"recipient": target}


def delete_recipient(email: str) -> dict[str, Any]:
    records = list_recipients()["recipients"]
    remaining = [record for record in records if str(record.get("email") or "").strip().lower() != email.lower()]
    if len(remaining) == len(records):
        raise ValueError(f"Recipient {email!r} not found.")
    _write_recipients(remaining)
    return {"deleted": email}


def _normalize_env_overrides(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    env: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name or value is None:
            continue
        text = str(value)
        if text == "":
            continue
        env[name] = text
    return env


def _add_option(args: list[str], flag: str, value: Any) -> None:
    if value is None or value == "":
        return
    args.extend([flag, str(value)])


def _add_bool_option(args: list[str], flag: str, value: Any) -> None:
    if _coerce_bool(value):
        args.append(flag)


def _body_preset_id(body: dict[str, Any]) -> str:
    return normalize_preset_id(str(body.get("preset") or body.get("preset_id") or ""))


def build_command(body: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    action = str(body.get("action") or "run").strip()
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    overrides = _normalize_env_overrides(body.get("env"))
    preset_id = _body_preset_id(body)
    try:
        resolution = resolve_runtime_config(
            RuntimeConfigRequest(
                base_env=_ui_base_env(preset_id, overrides),
                preset_id=preset_id,
                overrides=overrides,
                materialize_outputs=False,
            )
        )
        env = {
            **_preset_env_over_inherited_env(preset_id, overrides),
            **resolution.command_env_delta,
        }
    except ValueError as error:
        if "Topic-based runtime configuration has been removed" not in str(error):
            raise
        env = dict(overrides)
        if preset_id:
            env.setdefault("NEWS_PRESET", preset_id)
    command = ["uv", "run", "news"]
    if action == "run":
        command.append("run")
        if preset_id:
            command.extend(["--preset", preset_id])
    elif action in {"model-server-command", "codex-model-server-command", "serve-unsubscribe"}:
        if preset_id:
            env.setdefault("NEWS_PRESET", preset_id)
        command.append(action)
    elif action in {"check-sources", "prune-sources", "source-languages"}:
        command.append(action)
        _add_option(command, "--sources-yaml", options.get("sources_yaml"))
        _add_option(command, "--timeout", options.get("timeout"))
        _add_option(command, "--concurrency", options.get("concurrency"))
        _add_option(command, "--recent-days", options.get("recent_days"))
        _add_bool_option(command, "--probe-articles", options.get("probe_articles"))
        _add_bool_option(command, "--prune-unscrapable", options.get("prune_unscrapable"))
        _add_bool_option(command, "--only-failures", options.get("only_failures"))
        _add_bool_option(command, "--write-languages", options.get("write_languages"))
        _add_bool_option(command, "--overwrite-languages", options.get("overwrite_languages"))
        _add_option(command, "--language-model", options.get("language_model"))
        _add_option(command, "--language-samples", options.get("language_samples"))
        _add_option(command, "--min-language-confidence", options.get("min_language_confidence"))
        _add_option(command, "--limit", options.get("limit"))
        _add_option(command, "--section", options.get("section"))
        _add_bool_option(command, "--json", options.get("json"))
    else:
        raise ValueError(f"Unsupported action: {action}")
    return command, env


def preview_payload(body: dict[str, Any]) -> dict[str, Any]:
    command, env = build_command(body)
    preset_id = _body_preset_id(body)
    runtime, runtime_error = _runtime_snapshot(
        _normalize_env_overrides(body.get("env")),
        preset_id=preset_id,
    )
    display_env = _display_env(env)
    rendered = " ".join(shlex.quote(part) for part in command)
    if display_env:
        rendered = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(display_env.items())
        ) + " " + rendered
    return {
        "command": command,
        "command_text": rendered,
        "env": display_env,
        "runtime": runtime,
        "runtime_error": runtime_error,
        "removed_topic_env_vars": sorted(
            configured_removed_topic_env_vars({**os.environ, **_normalize_env_overrides(body.get("env"))})
        ),
    }


class RunRecord:
    def __init__(self, run_id: str, command: list[str], env: dict[str, str]):
        self.run_id = run_id
        self.command = command
        self.env = env
        self.started_at = time.time()
        # Normalized structured events (message/progress) in arrival order.
        # Progress snapshots replace the previous snapshot of the same stage
        # in place, so the event history stays bounded by meaningful messages
        # plus one line per active stage.
        self.events: list[dict[str, Any]] = []
        self._event_sequences: list[int] = []
        self._next_event_sequence = 0
        self._progress_index: dict[str, int] = {}
        self.status = "starting"
        self.stop_requested = False
        self.returncode: int | None = None
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    def append(self, line: str) -> None:
        with self.lock:
            for event in run_log.parse_stream(line):
                if event.kind == "progress":
                    self._append_progress(event)
                else:
                    self.events.append(event.to_dict())
                    self._event_sequences.append(self._next_sequence())

    def _next_sequence(self) -> int:
        self._next_event_sequence += 1
        return self._next_event_sequence

    def _append_progress(self, event: run_log.RunLogEvent) -> None:
        payload = event.to_dict()
        index = self._progress_index.get(event.stage or "")
        if index is not None:
            if self.events[index].get("line") == payload["line"]:
                return  # exact duplicate snapshot
            payload["replace"] = True
            self.events[index] = payload
            self._event_sequences[index] = self._next_sequence()
        else:
            self._progress_index[event.stage or ""] = len(self.events)
            self.events.append(payload)
            self._event_sequences.append(self._next_sequence())

    def stream_state(self, sequence: int) -> tuple[list[dict[str, Any]], int, str, bool]:
        """Return current events newer than a stream's transport cursor.

        Events are coalesced for replay, so a progress replacement can keep the
        same list index.  The sequence cursor makes each replacement visible
        to existing streams while retaining bounded in-memory event state.
        """
        with self.lock:
            updates = sorted(
                (
                    (event_sequence, dict(event))
                    for event, event_sequence in zip(self.events, self._event_sequences)
                    if event_sequence > sequence
                ),
                key=lambda item: item[0],
            )
            next_sequence = self._next_event_sequence
            status = self.status
            done = status in {"completed", "failed", "stopped"}
        return [event for _, event in updates], next_sequence, status, done

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "run_id": self.run_id,
                "command": self.command,
                "env": _display_env(self.env),
                "started_at": self.started_at,
                "status": self.status,
                "returncode": self.returncode,
                "line_count": len(self.events),
            }

    def is_active(self) -> bool:
        with self.lock:
            return self.status in {"starting", "running", "stopping"}


class RunAlreadyActiveError(RuntimeError):
    """Raised when a run is requested while another run is still active."""


class RunManager:
    def __init__(self):
        self.runs: dict[str, RunRecord] = {}
        self.lock = threading.Lock()

    def active(self) -> RunRecord | None:
        with self.lock:
            for record in self.runs.values():
                if record.is_active():
                    return record
            return None

    def start(self, body: dict[str, Any]) -> RunRecord:
        command, env = build_command(body)
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(run_id, command, env)
        with self.lock:
            for existing in self.runs.values():
                if existing.is_active():
                    raise RunAlreadyActiveError(
                        f"A run is already active ({existing.run_id}, status {existing.status}); "
                        f"stop it or wait for it to finish before starting another."
                    )
            self.runs[run_id] = record
        thread = threading.Thread(target=self._run_process, args=(record,), daemon=True)
        try:
            thread.start()
        except Exception as exc:
            with self.lock:
                record.status = "failed"
                record.returncode = -1
                record.append(f"[ui] failed to start thread: {exc}\n")
            raise
        return record

    def get(self, run_id: str) -> RunRecord | None:
        with self.lock:
            return self.runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [record.snapshot() for record in sorted(self.runs.values(), key=lambda item: item.started_at, reverse=True)]

    def stop(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record is None:
            raise ValueError(f"Run {run_id!r} not found.")
        with record.lock:
            if record.status in {"completed", "failed", "stopped"} or record.stop_requested:
                process = None
                should_request = False
            else:
                # Record the intent before inspecting or terminating the child
                # so the worker owns the eventual terminal transition. This
                # also covers a stop click that arrives before Popen() returns.
                record.stop_requested = True
                record.status = "stopping"
                process = record.process
                should_request = True
        if should_request:
            self._append_process_event(record, "[ui] terminate requested\n")
        if should_request and process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception as exc:
                self._append_process_event(record, f"[ui] terminate request failed: {exc}\n")
        return record.snapshot()

    @staticmethod
    def _append_process_event(record: RunRecord, line: str) -> None:
        """Report a lifecycle detail without masking the process outcome."""
        try:
            record.append(line)
        except Exception:
            # A logging/normalization failure must not prevent child cleanup.
            pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> Exception | None:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception as exc:
            return exc
        return None

    def _run_process(self, record: RunRecord) -> None:
        env = dict(os.environ)
        env.update(record.env)

        # Spawn is its own phase so only spawn failures are described as start
        # failures.  In particular, a stop request can safely arrive while
        # Popen is in flight and is honored immediately after assignment.
        try:
            process = subprocess.Popen(
                record.command,
                cwd=str(ROOT_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            with record.lock:
                record.returncode = -1
                record.status = "failed"
            self._append_process_event(record, f"[ui] failed to start process: {exc}\n")
            return

        with record.lock:
            record.process = process
            stop_requested = record.stop_requested
            if not stop_requested:
                record.status = "running"

        if stop_requested:
            termination_error = self._terminate_process(process)
            if termination_error is not None:
                self._append_process_event(record, f"[ui] terminate request failed: {termination_error}\n")

        # Stream is a separate phase.  If reading or normalization fails,
        # terminate the child before the reap phase so it cannot outlive the
        # failed UI run.
        stream_error: Exception | None = None
        try:
            if process.stdout is None:
                raise RuntimeError("process did not provide stdout")
            for line in process.stdout:
                record.append(line)
        except Exception as exc:
            stream_error = exc
            self._append_process_event(record, f"[ui] process output failed: {exc}\n")
            termination_error = self._terminate_process(process)
            if termination_error is not None:
                self._append_process_event(record, f"[ui] failed to terminate process after output error: {termination_error}\n")

        # Reap is deliberately separate from both spawn and stream handling so
        # its error retains the actual phase and does not become a fake start
        # failure.  A failed wait gets one cleanup attempt before returning.
        try:
            returncode = process.wait()
        except Exception as exc:
            termination_error = self._terminate_process(process)
            if termination_error is not None:
                self._append_process_event(record, f"[ui] failed to terminate process after wait error: {termination_error}\n")
            try:
                process.wait()
            except Exception as reap_exc:
                self._append_process_event(record, f"[ui] failed to reap process: {reap_exc}\n")
            with record.lock:
                record.returncode = -1
                record.status = "stopped" if record.stop_requested else "failed"
            self._append_process_event(record, f"[ui] failed to wait for process: {exc}\n")
            return

        with record.lock:
            record.returncode = returncode
            if record.stop_requested:
                record.status = "stopped"
            elif stream_error is not None:
                record.status = "failed"
            else:
                record.status = "completed" if returncode == 0 else "failed"
        self._append_process_event(record, f"[ui] process exited with code {returncode}\n")


RUN_MANAGER = RunManager()


class NewsUIServer(ThreadingHTTPServer):
    daemon_threads = True


class NewsUIHandler(BaseHTTPRequestHandler):
    server_version = "NewsControlPanel/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[news-ui] {self.address_string()} - {format % args}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object.")
        return payload

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, exc: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": str(exc)}, status=status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/schema":
                self._send_json(schema_payload())
            elif parsed.path == "/api/presets":
                self._send_json(list_presets())
            elif parsed.path == "/api/model-tuning-presets":
                self._send_json(list_model_tuning_presets())
            elif parsed.path.startswith("/api/prompt-profiles/compare"):
                params = parse_qs(parsed.query)
                profile = (params.get("profile") or [""])[0] or DEFAULT_PROMPT_PROFILE_ID
                self._send_json(
                    {
                        "profile": profile,
                        "baseline": DEFAULT_PROMPT_PROFILE_ID,
                        "diffs": compare_prompt_profiles(profile),
                    }
                )
            elif parsed.path == "/api/models/search":
                params = parse_qs(parsed.query)
                query = (params.get("q") or [""])[0].strip()
                if not query:
                    self._send_json(
                        {"query": "", "models": [], "error": "Missing query parameter q."},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                pipeline_tag = (params.get("pipeline_tag") or [None])[0] or None
                raw_limit = (params.get("limit") or [""])[0]
                try:
                    limit = int(raw_limit) if raw_limit else 20
                except ValueError:
                    limit = 20
                try:
                    models = search_huggingface_models(
                        query, pipeline_tag=pipeline_tag, limit=limit
                    )
                except Exception as exc:
                    models = []
                    self._send_json({"query": query, "models": [], "error": str(exc)})
                else:
                    self._send_json({"query": query, "models": models, "error": None})
            elif parsed.path == "/api/models/metadata":
                params = parse_qs(parsed.query)
                reference = (params.get("model") or [""])[0].strip()
                if not reference:
                    self._send_json(
                        {"model": "", "info": None, "error": "Missing model parameter."},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                try:
                    info = fetch_model_metadata(reference)
                except Exception as exc:
                    self._send_json({"model": reference, "info": None, "error": str(exc)})
                else:
                    self._send_json({"model": reference, "info": info, "error": None})
            elif parsed.path == "/api/sources":
                self._send_json(list_sources())
            elif parsed.path == "/api/recipients":
                self._send_json(list_recipients())
            elif parsed.path == "/api/runs":
                self._send_json({"runs": RUN_MANAGER.list()})
            elif parsed.path == "/api/history":
                params = parse_qs(parsed.query)
                raw_limit = (params.get("limit") or [""])[0]
                try:
                    limit = int(raw_limit) if raw_limit else 20
                except ValueError:
                    limit = 20
                self._send_json(recent_history_payload(limit))
            elif parsed.path.startswith("/api/history/") and parsed.path.endswith("/report"):
                parts = parsed.path.split("/")
                if len(parts) < 5:
                    self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                run_id = parts[3]
                report_text = read_historical_report(run_id)
                if report_text is None:
                    self._send_json(
                        {"error": "Report not available for this run."},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                body = report_text.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path.startswith("/api/history/"):
                run_id = parsed.path.split("/")[3]
                details = run_detail_payload(run_id)
                if details is None:
                    self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(details)
            elif parsed.path == "/api/review/latest":
                self._send_json(latest_review_payload())
            elif parsed.path == "/api/schedule":
                self._send_json(schedule_payload())
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/events"):
                run_id = parsed.path.split("/")[3]
                self._stream_run_events(run_id)
            elif parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.split("/")[3]
                record = RUN_MANAGER.get(run_id)
                if record is None:
                    self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(record.snapshot())
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/preview":
                self._send_json(preview_payload(body))
            elif parsed.path == "/api/run":
                try:
                    record = RUN_MANAGER.start(body)
                except RunAlreadyActiveError as exc:
                    self._send_error_json(exc, status=HTTPStatus.CONFLICT)
                    return
                self._send_json(record.snapshot(), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/presets":
                self._send_json(upsert_preset(body, append_only=True), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/model-tuning-presets":
                self._send_json(upsert_model_tuning_preset(body, append_only=True), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/presets/duplicate":
                self._send_json(duplicate_preset(body), status=HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/stop"):
                run_id = parsed.path.split("/")[3]
                self._send_json(RUN_MANAGER.stop(run_id))
            elif parsed.path == "/api/sources":
                self._send_json(upsert_source(body, append_only=True), status=HTTPStatus.CREATED)
            elif parsed.path == "/api/recipients":
                self._send_json(upsert_recipient(body, append_only=True), status=HTTPStatus.CREATED)
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/schedule":
                self._send_json(update_schedule(body))
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/sources":
                self._send_json(upsert_source(body))
            elif parsed.path == "/api/presets":
                self._send_json(upsert_preset(body))
            elif parsed.path == "/api/model-tuning-presets":
                self._send_json(upsert_model_tuning_preset(body))
            elif parsed.path == "/api/recipients":
                self._send_json(upsert_recipient(body))
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/sources":
                self._send_json(delete_source((params.get("key") or [""])[0]))
            elif parsed.path == "/api/schedule":
                self._send_json(disable_schedule_payload())
            elif parsed.path == "/api/presets":
                self._send_json(delete_preset((params.get("id") or [""])[0]))
            elif parsed.path == "/api/model-tuning-presets":
                self._send_json(delete_model_tuning_preset((params.get("id") or [""])[0]))
            elif parsed.path == "/api/recipients":
                self._send_json(delete_recipient((params.get("email") or [""])[0]))
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def _stream_run_events(self, run_id: str) -> None:
        record = RUN_MANAGER.get(run_id)
        if record is None:
            self._send_json({"error": "Run not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sequence = 0
        index = 0
        while True:
            stream_state = getattr(record, "stream_state", None)
            if stream_state is not None:
                events, sequence, status, done = stream_state(sequence)
            else:
                # Keep the small fake-record compatibility path used by
                # handler tests; production records use revision-aware state.
                with record.lock:
                    events = record.events[index:]
                    index = len(record.events)
                    status = record.status
                    done = status in {"completed", "failed", "stopped"}
            try:
                for event in events:
                    payload = json.dumps({**event, "status": status})
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if done:
                payload = json.dumps(record.snapshot())
                try:
                    self.wfile.write(f"event: status\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            time.sleep(0.5)


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Control Panel</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f1ebe1;
      --surface: #fffdf8;
      --line: #d8cfc0;
      --ink: #1d2430;
      --muted: #6d7480;
      --blue: #285c94;
      --green: #17735f;
      --gold: #9a6715;
      --red: #b33f3f;
      --focus: #0f7a9f;
      --shadow: 0 18px 50px rgba(36, 44, 60, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(255, 255, 255, 0.7), transparent 28%),
        radial-gradient(circle at top right, rgba(208, 184, 141, 0.18), transparent 24%),
        linear-gradient(180deg, #f7f1e6 0%, var(--bg) 42%, #ece4d8 100%);
      color: var(--ink);
      font: 14px/1.45 "Avenir Next", "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 253, 248, 0.88);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 4;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.8) inset;
    }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0; }
    h2 { font-size: 15px; margin: 0 0 12px; letter-spacing: 0; }
    h3 { font-size: 13px; margin: 18px 0 8px; color: var(--muted); letter-spacing: 0; }
    button, select, input, textarea {
      font: inherit;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
    }
    button {
      min-height: 34px;
      padding: 6px 10px;
      cursor: pointer;
      background: linear-gradient(180deg, #fff, #f5f0e7);
      transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(36, 44, 60, 0.08); }
    button.primary { background: var(--blue); color: white; border-color: var(--blue); }
    button.danger { color: var(--red); border-color: #e5b4b4; }
    button:focus, input:focus, select:focus, textarea:focus { outline: 2px solid var(--focus); outline-offset: 1px; }
    input, select, textarea {
      width: 100%;
      min-height: 34px;
      padding: 7px 9px;
      border-radius: 10px;
      background: #fff;
      border: 1px solid var(--line);
    }
    textarea { min-height: 76px; resize: vertical; }
    main { display: grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 63px); }
    body.nav-collapsed main { grid-template-columns: 58px 1fr; }
    nav {
      border-right: 1px solid var(--line);
      background: rgba(236, 240, 245, 0.92);
      padding: 12px;
      position: sticky;
      top: 63px;
      align-self: start;
      min-height: calc(100vh - 63px);
    }
    nav button {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 8px;
      text-align: left;
      margin-bottom: 6px;
      background: transparent;
      border-color: transparent;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    nav button.active { background: #fff; border-color: var(--line); color: var(--blue); }
    nav svg { width: 18px; height: 18px; flex: 0 0 18px; stroke: currentColor; }
    .nav-toggle {
      justify-content: center;
      width: 34px;
      margin-left: auto;
      margin-bottom: 12px;
      border-color: var(--line);
      background: rgba(255, 255, 255, 0.62);
      color: var(--muted);
      border-radius: 999px;
    }
    .nav-toggle:hover { color: var(--blue); }
    body:not(.nav-collapsed) .expand-icon { display: none; }
    body.nav-collapsed .collapse-icon { display: none; }
    body.nav-collapsed nav { padding: 12px 8px; }
    body.nav-collapsed nav button:not(#navToggle) {
      justify-content: center;
      padding-left: 0;
      padding-right: 0;
    }
    body.nav-collapsed nav button:not(#navToggle) .tab-text { display: none; }
    section.view { display: none; padding: 18px; }
    section.view.active { display: block; }
    .grid { display: grid; gap: 12px; }
    .cols { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
    .stat { border-left: 4px solid var(--blue); padding: 8px 10px; background: #fff; border-radius: 12px; box-shadow: 0 6px 18px rgba(36, 44, 60, 0.06); }
    .stat strong { display: block; font-size: 20px; }
    .row { display: grid; grid-template-columns: minmax(120px, 190px) 1fr; gap: 10px; align-items: center; margin-bottom: 10px; }
    .row label { color: var(--muted); }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    .toolbar > * { width: auto; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    table { border-collapse: collapse; width: 100%; min-width: 760px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: #f3f5f8; z-index: 1; }
    tr:hover td { background: #fafcff; }
    .muted { color: var(--muted); }
    .warn { color: var(--gold); }
    .bad { color: var(--red); }
    .good { color: var(--green); }
    .badge {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--muted);
      white-space: nowrap;
    }
    .badge.good { color: var(--green); border-color: #b9d8cf; background: #f2faf7; }
    .badge.warn { color: var(--gold); border-color: #e2cf9f; background: #fdf8ec; }
    .badge.bad { color: var(--red); border-color: #e5b4b4; background: #fdf2f2; }
    .badge.blue { color: var(--blue); border-color: #b9cfe4; background: #f2f7fc; }
    .badge-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .review-report {
      min-height: 200px;
      max-height: 60vh;
      background: #fffdf8;
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      white-space: pre-wrap;
    }
    button:disabled { opacity: .55; cursor: not-allowed; }
    #status:empty { display: none; }
    .hidden { display: none !important; }
    pre {
      margin: 0;
      padding: 12px;
      overflow: auto;
      min-height: 72px;
      background: #111827;
      color: #e8edf7;
      border-radius: 8px;
      white-space: pre-wrap;
    }
    #logPane {
      min-height: 280px;
      max-height: 52vh;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 13px;
      line-height: 1.55;
    }
    .knob-group { margin-bottom: 18px; }
    .knobs { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }
    .knob { border: 1px solid var(--line); border-radius: 14px; padding: 10px; background: #fff; box-shadow: 0 6px 16px rgba(36, 44, 60, 0.05); }
    .knob label { display: flex; gap: 6px; align-items: center; font-weight: 600; margin-bottom: 4px; }
    .knob code { display: none; }
    .knob-links { margin-top: 6px; display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; }
    .knob-links a { color: var(--blue); text-decoration: none; }
    .knob-links a:hover { text-decoration: underline; }
    .knob-details { margin-top: 12px; }
    .knob-details > summary { cursor: pointer; color: var(--muted); font-weight: 600; }
    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
    .field { display: grid; gap: 6px; align-content: start; }
    .field > span { display: flex; gap: 6px; align-items: center; font-weight: 600; min-width: 0; }
    .field code { display: none; }
    .env-info {
      display: inline-grid;
      place-items: center;
      width: 16px;
      height: 16px;
      flex: 0 0 16px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      cursor: help;
      background: #fff;
    }
    .env-info:focus-visible {
      outline: 2px solid var(--blue);
      outline-offset: 2px;
    }
    .env-tooltip {
      /* Fixed (not absolute): escapes clipping ancestors and keeps the JS
         viewport clamping in positionEnvTooltip() viewport-relative. */
      position: fixed;
      z-index: 60;
      display: block;
      max-width: min(320px, calc(100vw - 16px));
      /* Tall hints stay readable on short viewports. */
      max-height: calc(100vh - 16px);
      overflow-y: auto;
      padding: 8px 10px;
      background: #111827;
      color: #e8edf7;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.45;
      box-shadow: 0 10px 30px rgba(20, 26, 38, 0.35);
      pointer-events: none;
    }
    .stack { display: grid; gap: 14px; }
    .eyebrow { margin: 0 0 4px; text-transform: uppercase; letter-spacing: 0.12em; font-size: 11px; color: var(--muted); }
    .banner {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      position: sticky;
      top: 66px;
      z-index: 3;
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(248,240,224,0.96));
      border-color: rgba(216, 207, 192, 0.9);
    }
    .banner-copy h2 { margin-bottom: 6px; font-size: 20px; }
    .banner-copy p { margin: 0; }
    .banner-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .banner-actions button { white-space: nowrap; }
    .model-card h2 { margin-bottom: 6px; }
    .details {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      background: #fcfbf7;
    }
    .details > summary { cursor: pointer; font-weight: 700; color: var(--blue); }
    .details[open] > summary { margin-bottom: 12px; }
    dialog {
      border: none;
      padding: 0;
      background: transparent;
      max-width: min(1180px, calc(100vw - 24px));
    }
    dialog::backdrop { background: rgba(23, 28, 39, 0.42); backdrop-filter: blur(2px); }
    .dialog-shell {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 28px 80px rgba(20, 26, 38, 0.28);
      padding: 16px;
      min-width: min(1120px, calc(100vw - 24px));
    }
    .dialog-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 14px; }
    .dialog-grid { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 14px; }
    .selected td { background: #f3f7fb; }
    .selected td:first-child { box-shadow: inset 3px 0 0 var(--blue); }
    @media (max-width: 780px) {
      main { grid-template-columns: 1fr; }
      nav { display: flex; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      nav button { width: auto; white-space: nowrap; }
      .row { grid-template-columns: 1fr; gap: 4px; }
      header { align-items: flex-start; flex-direction: column; }
      .banner, .dialog-head { flex-direction: column; }
      .banner-actions {
        width: 100%;
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        justify-content: stretch;
      }
      .banner-actions button { width: 100%; white-space: normal; }
      #runBtn { grid-column: 1 / -1; }
      .dialog-shell { min-width: auto; }
      .dialog-grid { grid-template-columns: 1fr; }
      .banner { top: 88px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>News Control Panel</h1>
    <div id="status" class="muted">Loading...</div>
  </header>
  <main>
    <nav id="tabs"></nav>
    <section id="runSetup" class="view active">
      <div id="runSetupMount"></div>
    </section>
    <section id="review" class="view">
      <div id="reviewMount"></div>
      <div id="historyMount"></div>
    </section>
    <section id="schedule" class="view">
      <div id="scheduleMount"></div>
    </section>
    <section id="advanced" class="view">
      <div id="advancedPanels" class="stack"></div>
      <section class="panel">
        <p class="eyebrow">Raw environment</p>
        <h2>Environment overrides</h2>
        <div class="toolbar">
          <input id="knobSearch" placeholder="Filter raw settings">
          <button id="clearKnobsBtn">Clear overrides</button>
        </div>
        <div id="knobContainer"></div>
      </section>
    </section>
    <section id="sources" class="view">
      <div class="toolbar">
        <input id="sourceSearch" placeholder="Filter sources">
        <button id="newSourceBtn">New source</button>
        <button id="reloadSourcesBtn">Reload</button>
      </div>
      <div class="grid cols">
        <div class="table-wrap"><table id="sourceTable"></table></div>
        <div class="panel">
          <h2>Source Editor</h2>
          <div id="sourceForm" class="form-grid"></div>
          <div class="toolbar">
            <button id="saveSourceBtn" class="primary">Save source</button>
            <button id="deleteSourceBtn" class="danger">Delete source</button>
          </div>
        </div>
      </div>
    </section>
    <section id="recipients" class="view">
      <div class="toolbar">
        <button id="newRecipientBtn">New recipient</button>
        <button id="reloadRecipientsBtn">Reload</button>
      </div>
      <div class="grid cols">
        <div class="table-wrap"><table id="recipientTable"></table></div>
        <div class="panel">
          <h2>Recipient Editor</h2>
          <p class="muted">Additional recipients for the configured-recipients delivery mode; the owner is configured with NEWS_PRIMARY_RECIPIENT. The checked-in config/recipients.yaml is a public template.</p>
          <div class="form-grid">
            <label>Email<input id="recipient_email"></label>
            <label>Name<input id="recipient_name"></label>
            <label>Paused<select id="recipient_pause"><option value="false">false</option><option value="true">true</option></select></label>
          </div>
          <div class="toolbar" style="margin-top:12px">
            <button id="saveRecipientBtn" class="primary">Save recipient</button>
            <button id="deleteRecipientBtn" class="danger">Delete recipient</button>
          </div>
        </div>
      </div>
    </section>
    <section id="modelTuning" class="view">
      <div class="toolbar">
        <button id="newModelTuningPresetBtn">New preset</button>
        <button id="reloadModelTuningPresetsBtn">Reload</button>
      </div>
      <div class="grid cols">
        <div class="table-wrap"><table id="modelTuningPresetTable"></table></div>
        <div class="panel">
          <h2>Model Tuning Preset Editor</h2>
          <p class="muted">Saved model/task inference overlays. Every preset is listed regardless of the currently selected model or task; the Advanced Settings task cards filter the same records.</p>
          <div id="modelTuningPresetError" class="bad" role="alert"></div>
          <div class="form-grid">
            <label>ID<input id="modelTuningPresetId"></label>
            <label>Name<input id="modelTuningPresetName"></label>
            <label>Model (optional)<input id="modelTuningPresetModel" placeholder="Model reference or alias"></label>
            <label>Supported task (optional)<select id="modelTuningPresetTask">
              <option value="">Global / all tasks</option>
              <option value="article_summary">article_summary</option>
              <option value="story_drafting">story_drafting</option>
              <option value="story_scale_screening">story_scale_screening</option>
              <option value="title_generation">title_generation</option>
              <option value="image_art_direction">image_art_direction</option>
            </select></label>
          </div>
          <label>Description<textarea id="modelTuningPresetDescription"></textarea></label>
          <label>Tuning (JSON)<textarea id="modelTuningPresetTuning" spellcheck="false"></textarea></label>
          <p class="muted">Accepted tuning fields: temperature (0-2), top_p (0-1), top_k (whole number &gt;= 0), min_p (0-1), presence_penalty (-2 to 2), repetition_penalty (0-3), max_tokens, model_max_input_tokens, article_summary_max_tokens, story_drafting_max_tokens, story_scale_screening_max_tokens, title_generation_max_tokens, image_art_direction_max_tokens (positive whole numbers).</p>
          <div class="toolbar" style="margin-top:12px">
            <button id="saveModelTuningPresetBtn" class="primary">Save preset</button>
            <button id="deleteModelTuningPresetBtn" class="danger">Delete preset</button>
          </div>
        </div>
      </div>
    </section>
  </main>
  <dialog id="runPresetDialog">
    <div class="dialog-shell">
      <div class="dialog-head">
        <div>
          <p class="eyebrow">Run presets</p>
          <h2>Saved run presets</h2>
        </div>
        <div class="toolbar">
          <button id="newPresetBtn">New preset</button>
          <button id="reloadPresetsBtn">Reload</button>
          <button id="closeRunPresetDialogBtn" class="danger">Close</button>
        </div>
      </div>
      <div class="dialog-grid">
        <div class="table-wrap"><table id="presetTable"></table></div>
        <div class="panel">
          <h3>Run preset editor</h3>
          <div class="form-grid">
            <label>ID<input id="preset_id"></label>
            <label>Name<input id="preset_name"></label>
          </div>
          <label>Description<textarea id="preset_description"></textarea></label>
          <label>Environment<textarea id="preset_env" spellcheck="false"></textarea></label>
          <div class="toolbar" style="margin-top:12px">
            <button id="applyPresetBtn">Apply</button>
            <button id="renamePresetBtn">Rename display name</button>
            <button id="savePresetEditorBtn" class="primary">Save/Edit</button>
            <button id="deletePresetBtn" class="danger">Delete</button>
          </div>
        </div>
      </div>
    </div>
  </dialog>
  <script>
    // Every env rendered as a dedicated knob (Run Setup or Advanced panels) must
    // be listed here so renderAdvancedKnobs() omits it from the raw override
    // list; otherwise the env appears twice and collectEnv() gets two inputs.
    const SURFACED_ENVS = new Set([
      "NEWS_MODEL",  // dedicated "Default model" knob in Run Setup; suppress the Advanced-tab duplicate
      "NEWS_SOURCE_SCOPE",
      "NEWS_DELIVERY_MODE",  // dedicated "Delivery mode" control in Run Setup; suppress the Advanced-tab duplicate
      "NEWS_PROMPT_PROFILE",  // has a dedicated panel control; suppress the Advanced-tab duplicate
      "NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY",       // dedicated per-stage editors in the
      "NEWS_PROMPT_OVERRIDE_STORY_SCALE_SCREENING", // Editorial approach panel; suppress
      "NEWS_PROMPT_OVERRIDE_STORY_DRAFTING",        // the Advanced-tab duplicates
      "NEWS_PROMPT_OVERRIDE_TITLE_GENERATION",
      "NEWS_PROMPT_OVERRIDE_IMAGE_ART_DIRECTION",
      "NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET",
      "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET",
      "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
      "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_TUNING_PRESET",
      "NEWS_MODEL_MAX_INPUT_TOKENS",
      "NEWS_ARTICLE_SUMMARY_MAX_TOKENS",
      "NEWS_STORY_DRAFTING_MAX_TOKENS",
      "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS",
      "NEWS_TITLE_GENERATION_MAX_TOKENS",
      "NEWS_IMAGE_ART_DIRECTION_MAX_TOKENS",
      "NEWS_ARTICLE_TEXT_TOKEN_LIMIT",
      "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL",
      "NEWS_MODEL_STORY_DRAFTING_BASE_URL",
      "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL",
      "NEWS_MODEL_TITLE_GENERATION_BASE_URL",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_BASE_URL",
      "NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE",
      "NEWS_MODEL_ARTICLE_SUMMARY_TOP_P",
      "NEWS_MODEL_ARTICLE_SUMMARY_TOP_K",
      "NEWS_MODEL_ARTICLE_SUMMARY_MIN_P",
      "NEWS_MODEL_ARTICLE_SUMMARY_PRESENCE_PENALTY",
      "NEWS_MODEL_ARTICLE_SUMMARY_REPETITION_PENALTY",
      "NEWS_MODEL_STORY_DRAFTING_TEMPERATURE",
      "NEWS_MODEL_STORY_DRAFTING_TOP_P",
      "NEWS_MODEL_STORY_DRAFTING_TOP_K",
      "NEWS_MODEL_STORY_DRAFTING_MIN_P",
      "NEWS_MODEL_STORY_DRAFTING_PRESENCE_PENALTY",
      "NEWS_MODEL_STORY_DRAFTING_REPETITION_PENALTY",
      "NEWS_MODEL_STORY_SCALE_SCREENING_TEMPERATURE",
      "NEWS_MODEL_STORY_SCALE_SCREENING_TOP_P",
      "NEWS_MODEL_STORY_SCALE_SCREENING_TOP_K",
      "NEWS_MODEL_STORY_SCALE_SCREENING_MIN_P",
      "NEWS_MODEL_STORY_SCALE_SCREENING_PRESENCE_PENALTY",
      "NEWS_MODEL_STORY_SCALE_SCREENING_REPETITION_PENALTY",
      "NEWS_MODEL_TITLE_GENERATION_TEMPERATURE",
      "NEWS_MODEL_TITLE_GENERATION_TOP_P",
      "NEWS_MODEL_TITLE_GENERATION_TOP_K",
      "NEWS_MODEL_TITLE_GENERATION_MIN_P",
      "NEWS_MODEL_TITLE_GENERATION_PRESENCE_PENALTY",
      "NEWS_MODEL_TITLE_GENERATION_REPETITION_PENALTY",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_TEMPERATURE",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_TOP_P",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_TOP_K",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_MIN_P",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_PRESENCE_PENALTY",
      "NEWS_MODEL_IMAGE_ART_DIRECTION_REPETITION_PENALTY",
      "NEWS_SOURCE_COLLECTION_CONCURRENCY",
      "NEWS_ARTICLE_SUMMARY_CONCURRENCY",
      "NEWS_STORY_SYNTHESIS_CONCURRENCY",
      "NEWS_MAX_STORIES",
      "NEWS_MIN_ARTICLES_PER_STORY",
      "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD",
      "NEWS_STORY_SELECTION_OVERLAP_THRESHOLD",
      "NEWS_STORY_DEDUP_THRESHOLD",
      "NEWS_STORY_BACKFILL_BATCH_MULTIPLIER",
      "NEWS_BLOCK_REUSED_URLS",
      "NEWS_IMAGE_ENABLED",
      "NEWS_STORY_SCALE_SCREENING_ENABLED",
      "NEWS_RELAX_STORY_DRAFTING_GUARDS"
    ]);
    const TASK_CONFIG = {
      article_summary: {
        label: "Article Summarization",
        modelEnv: "NEWS_MODEL_ARTICLE_SUMMARY",
        presetEnv: "NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET",
        baseUrlEnv: "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL",
        presetSelectId: "article_tuning_preset",
        idInputId: "article_tuning_id",
        nameInputId: "article_tuning_name",
        descriptionInputId: "article_tuning_description",
        modelSelectId: "article_model",
        saveButtonId: "article_tuning_save",
        renameButtonId: "article_tuning_rename",
        deleteButtonId: "article_tuning_delete",
        taskMaxTokensEnv: "NEWS_ARTICLE_SUMMARY_MAX_TOKENS",
        taskSamplingPrefix: "NEWS_MODEL_ARTICLE_SUMMARY",
        runtimeKey: "article_summary"
      },
      story_drafting: {
        label: "Story Writing",
        modelEnv: "NEWS_MODEL_STORY_DRAFTING",
        presetEnv: "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET",
        baseUrlEnv: "NEWS_MODEL_STORY_DRAFTING_BASE_URL",
        presetSelectId: "story_tuning_preset",
        idInputId: "story_tuning_id",
        nameInputId: "story_tuning_name",
        descriptionInputId: "story_tuning_description",
        modelSelectId: "story_model",
        saveButtonId: "story_tuning_save",
        renameButtonId: "story_tuning_rename",
        deleteButtonId: "story_tuning_delete",
        taskMaxTokensEnv: "NEWS_STORY_DRAFTING_MAX_TOKENS",
        taskSamplingPrefix: "NEWS_MODEL_STORY_DRAFTING",
        runtimeKey: "story_drafting"
      },
      story_scale_screening: {
        label: "Story Scale Screening",
        modelEnv: "NEWS_MODEL_STORY_SCALE_SCREENING",
        presetEnv: "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
        baseUrlEnv: "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL",
        presetSelectId: "scale_tuning_preset",
        idInputId: "scale_tuning_id",
        nameInputId: "scale_tuning_name",
        descriptionInputId: "scale_tuning_description",
        modelSelectId: "scale_model",
        saveButtonId: "scale_tuning_save",
        renameButtonId: "scale_tuning_rename",
        deleteButtonId: "scale_tuning_delete",
        taskMaxTokensEnv: "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS",
        taskSamplingPrefix: "NEWS_MODEL_STORY_SCALE_SCREENING",
        runtimeKey: "story_scale_screening"
      },
      title_generation: {
        label: "Title Generation",
        modelEnv: "NEWS_MODEL_TITLE_GENERATION",
        presetEnv: "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
        baseUrlEnv: "NEWS_MODEL_TITLE_GENERATION_BASE_URL",
        presetSelectId: "title_tuning_preset",
        idInputId: "title_tuning_id",
        nameInputId: "title_tuning_name",
        descriptionInputId: "title_tuning_description",
        modelSelectId: "title_model",
        saveButtonId: "title_tuning_save",
        renameButtonId: "title_tuning_rename",
        deleteButtonId: "title_tuning_delete",
        taskMaxTokensEnv: "NEWS_TITLE_GENERATION_MAX_TOKENS",
        taskSamplingPrefix: "NEWS_MODEL_TITLE_GENERATION",
        runtimeKey: "title_generation"
      },
      image_art_direction: {
        label: "Image Art Direction",
        modelEnv: "NEWS_MODEL_IMAGE_ART_DIRECTION",
        presetEnv: "NEWS_MODEL_IMAGE_ART_DIRECTION_TUNING_PRESET",
        baseUrlEnv: "NEWS_MODEL_IMAGE_ART_DIRECTION_BASE_URL",
        presetSelectId: "image_art_tuning_preset",
        idInputId: "image_art_tuning_id",
        nameInputId: "image_art_tuning_name",
        descriptionInputId: "image_art_tuning_description",
        modelSelectId: "image_art_model",
        saveButtonId: "image_art_tuning_save",
        renameButtonId: "image_art_tuning_rename",
        deleteButtonId: "image_art_tuning_delete",
        taskMaxTokensEnv: "NEWS_IMAGE_ART_DIRECTION_MAX_TOKENS",
        taskSamplingPrefix: "NEWS_MODEL_IMAGE_ART_DIRECTION",
        runtimeKey: "image_art_direction"
      }
    };
    const state = {
      schema: null,
      presets: [],
      modelTuningPresets: [],
      sources: [],
      recipients: [],
      activeRun: null,
      selectedRunPresetId: "",
      selectedModelTuningPresetId: "",
      selectedModelReference: "",
      selectedModelRequiredBackend: "",
      latestReview: null,
      history: [],
      historyError: null,
      selectedRunId: null,
      selectedRun: null,
      schedule: null,
      scheduleError: null
    };
    const icons = {
      chevronLeft: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>`,
      chevronRight: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>`,
      gear: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.05.05a2 2 0 1 1-2.83 2.83l-.05-.05A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21a2 2 0 1 1-4 0v-.08A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.05.05a2 2 0 1 1-2.83-2.83l.05-.05A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3a2 2 0 1 1 0-4h.08A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.05-.05a2 2 0 1 1 2.83-2.83l.05.05A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3a2 2 0 1 1 4 0v.08A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.05-.05a2 2 0 1 1 2.83 2.83l-.05.05A1.7 1.7 0 0 0 19.4 9c.36.24.72.47 1 .6.34.16.72.2 1.1.2h.5a2 2 0 1 1 0 4h-.08A1.7 1.7 0 0 0 20.4 14c-.28.13-.64.36-1 .6Z"/></svg>`,
      microscope: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 0 0 7-7"/><path d="M9 14h2"/><path d="M8 6h4"/><path d="m9 6 6 6"/><path d="m11 4 6 6"/><path d="M12 6 9 9"/><path d="m17 10-3 3"/></svg>`,
      newspaper: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 1.5 17V5.5H18A2.5 2.5 0 0 1 20.5 8v11.5Z"/><path d="M20.5 8H23v9a2.5 2.5 0 0 1-2.5 2.5"/><path d="M5 9h8"/><path d="M5 13h10"/><path d="M5 17h6"/></svg>`,
      clock: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>`,
      book: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/></svg>`,
      person: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/></svg>`,
      sliders: `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 6h-8"/><path d="M21 12h-8"/><path d="M21 18h-8"/><path d="M8 6H3"/><path d="M8 12H3"/><path d="M8 18H3"/><path d="M8 4v4"/><path d="M8 10v4"/><path d="M8 16v4"/></svg>`
    };
    const tabs = [
      ["runSetup", "Run Setup", "gear"],
      ["review", "Report Review", "book"],
      ["schedule", "Schedule", "clock"],
      ["advanced", "Advanced Settings", "microscope"],
      ["modelTuning", "Model Tuning", "sliders"],
      ["sources", "Sources", "newspaper"],
      ["recipients", "Recipients", "person"]
    ];
    const KNOB_HINTS = {
      NEWS_DELIVERY_MODE: "Chooses the delivery policy: no delivery, owner only (default), or explicit configured recipients. Legacy NEWS_RECIPIENT_SCOPE still maps to this mode when set.",
      NEWS_RECIPIENT_SCOPE: "Legacy migration value for NEWS_DELIVERY_MODE (primary -> owner, all -> recipients); prefer the delivery mode control.",
      NEWS_SOURCE_SCOPE: "Chooses the source pool: core sources only, or the full source list.",
      NEWS_MODEL: "Default local model alias used when a task-specific model is not set.",
      NEWS_MODEL_ARTICLE_SUMMARY: "Model used for article summarization before story drafting.",
      NEWS_MODEL_STORY_DRAFTING: "Model used for writing the final story drafts.",
      NEWS_MODEL_STORY_SCALE_SCREENING: "Model used for global story scale screening before story selection.",
      NEWS_MODEL_TITLE_GENERATION: "Model used for the title generation call (overlay headline).",
      NEWS_MODEL_TUNING_PRESET: "Default saved tuning overlay applied before direct tuning overrides.",
      NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET: "Saved tuning overlay for the article summarization model.",
      NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET: "Saved tuning overlay for the story writing model.",
      NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET: "Saved tuning overlay for the story scale screening model.",
      NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET: "Saved tuning overlay for the title generation model.",
      NEWS_MODEL_IMAGE_ART_DIRECTION: "Model used for the image art direction call (text-free FLUX prompt).",
      NEWS_MODEL_IMAGE_ART_DIRECTION_TUNING_PRESET: "Saved tuning overlay for the image art direction model.",
      NEWS_MODEL_MAX_INPUT_TOKENS: "Shared input token cap sent to model calls.",
      NEWS_ARTICLE_SUMMARY_MAX_TOKENS: "Maximum generated tokens for each article summary.",
      NEWS_STORY_DRAFTING_MAX_TOKENS: "Maximum generated tokens for each final story draft.",
      NEWS_STORY_SCALE_SCREENING_MAX_TOKENS: "Maximum generated tokens for each story scale screening call.",
      NEWS_TITLE_GENERATION_MAX_TOKENS: "Maximum generated tokens for the title generation call.",
      NEWS_IMAGE_ART_DIRECTION_MAX_TOKENS: "Maximum generated tokens for the image art direction call.",
      NEWS_ARTICLE_TEXT_TOKEN_LIMIT: "Article text trimmed to this token budget before summarization.",
      NEWS_TOTAL_ARTICLE_SUMMARY_CAP: "Upper bound on article summaries kept for story synthesis.",
      NEWS_RECENT_WINDOW_HOURS: "How far back source collection looks for recent articles.",
      NEWS_MAX_ARTICLES_PER_SOURCE: "Maximum articles retained from each source in one run.",
      NEWS_MIN_ARTICLES_PER_STORY: "Minimum article count required before a story cluster is kept.",
      NEWS_MAX_STORIES: "Maximum number of final stories selected for the report.",
      NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD: "Embedding similarity needed to group articles into a story cluster.",
      NEWS_STORY_SELECTION_OVERLAP_THRESHOLD: "Overlap limit used to avoid selecting near-duplicate story candidates.",
      NEWS_STORY_DEDUP_THRESHOLD: "Embedding similarity threshold for dropping duplicate story drafts.",
      NEWS_STORY_BACKFILL_BATCH_MULTIPLIER: "How many extra candidate clusters to inspect when backfilling weak story slots.",
      NEWS_SOURCE_COLLECTION_CONCURRENCY: "Parallelism for source fetching during collection.",
      NEWS_ARTICLE_SUMMARY_CONCURRENCY: "Parallelism for article summarization model calls.",
      NEWS_STORY_SYNTHESIS_CONCURRENCY: "Parallelism for story synthesis and drafting work.",
      NEWS_MODEL_CONCURRENCY: "Concurrency passed to the local model server command.",
      NEWS_BLOCK_REUSED_URLS: "Blocks URLs already seen in history from appearing in later runs.",
      NEWS_IMAGE_ENABLED: "Turns report image generation on or off.",
      NEWS_STORY_SCALE_SCREENING_ENABLED: "Runs an extra screen to reject story clusters that are too small or narrow.",
      NEWS_RELAX_STORY_DRAFTING_GUARDS: "Loosens story drafting guardrails for debugging or recovery runs.",
      NEWS_EMBEDDING_MODEL: "Sentence embedding model used for clustering and deduplication.",
      NEWS_TOKEN_ENCODING: "Tokenizer name used for local token counting.",
      NEWS_MODEL_BASE_URL: "Default OpenAI-compatible model server endpoint.",
      NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL: "Model server endpoint for article summarization calls.",
      NEWS_MODEL_STORY_DRAFTING_BASE_URL: "Model server endpoint for story writing calls.",
      NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL: "Model server endpoint for story scale screening calls.",
      NEWS_MODEL_TITLE_GENERATION_BASE_URL: "Model server endpoint for title generation calls.",
      NEWS_MODEL_IMAGE_ART_DIRECTION_BASE_URL: "Model server endpoint for image art direction calls.",
      NEWS_MODEL_SERVER_PREFILL_STEP_SIZE: "Prefill step size passed to the local MLX model server.",
      NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE: "Prompt cache item count passed to the local model server.",
      NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES: "Prompt cache memory budget passed to the local model server.",
      NEWS_MODEL_SERVER_MAX_TOKENS: "Server-side maximum token setting for the local model process.",
      id: "YAML key for this saved preset.",
      name: "Human-facing display name stored with this preset.",
      description: "Optional note stored with this preset.",
      "command action": "Command the utility buttons should preview or run.",
      "--limit": "Maximum number of sources or records processed by a source utility.",
      "--recent-days": "Recent-day window for source pruning utilities.",
      "--timeout": "Per-source timeout for source utility checks.",
      "--concurrency": "Parallelism used by source utility commands.",
      "--section": "Source YAML section that the utility should target.",
      "--language-model": "Model used by source language detection.",
      "--language-samples": "Sample count used by source language detection.",
      "--min-language-confidence": "Minimum confidence accepted for source language detection."
    };
    const sourceFields = ["key","name","language","tier","region","nations","url","homepage","provider_type","intended_role","weight","can_enrich_coverage","strict_source_match","source_match_mode","source_match_aliases","notes"];

    function $(id) { return document.getElementById(id); }
    function value(id) { const el = $(id); return el ? el.value : ""; }
    function checked(id) { const el = $(id); return Boolean(el && el.checked); }
    async function api(path, options={}) {
      const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }
    function setStatus(text, cls="muted") {
      $("status").className = cls;
      $("status").textContent = text;
    }
    function showTab(id) {
      document.querySelectorAll("section.view").forEach(el => el.classList.toggle("active", el.id === id));
      document.querySelectorAll("nav button").forEach(el => el.classList.toggle("active", el.dataset.tab === id));
    }
    function escapeHtml(text) {
      return String(text ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
    }
    function formatDefault(value, fallback="none") {
      if (value === true) return "on";
      if (value === false) return "off";
      if (value === null || value === undefined || value === "") return fallback;
      return String(value);
    }
    function currentControlValue(env) {
      const el = document.querySelector(`[data-env="${env}"]`);
      if (!el) return (state.schema && state.schema.current_env && state.schema.current_env[env]) || "";
      if (el.type === "checkbox") return el.checked ? "1" : "";
      return el.value;
    }
    function setControlValue(env, next) {
      const el = document.querySelector(`[data-env="${env}"]`);
      if (!el) return;
      const valueText = next === null || next === undefined ? "" : String(next);
      if (el.type === "checkbox") {
        el.checked = ["1", "true", "yes", "on"].includes(valueText.toLowerCase());
        return;
      }
      if (el.tagName === "SELECT" && valueText && !Array.from(el.options).some(option => option.value === valueText)) {
        el.insertAdjacentHTML("afterbegin", `<option value="${escapeHtml(valueText)}">${escapeHtml(valueText)}</option>`);
      }
      el.value = valueText;
    }
    function knobByEnv(env) {
      return (state.schema && state.schema.knobs || []).find(knob => knob.env === env) || null;
    }
    function inputForKnob(knob, { emptyLabel, optionLabels = {}, id = "" } = {}) {
      const current = currentControlValue(knob.env);
      const idAttr = id ? ` id="${escapeHtml(id)}"` : "";
      if (knob.type === "select") {
        const options = [...new Set(knob.options || [])];
        if (current && !options.includes(current)) options.unshift(current);
        const opts = [
          `<option value="">${escapeHtml(emptyLabel || `default: ${formatDefault(knob.default)}`)}</option>`,
          ...options.map(opt => `<option value="${escapeHtml(opt)}"${current === opt ? " selected" : ""}>${escapeHtml(optionLabels[opt] || opt)}</option>`)
        ].join("");
        return `<select${idAttr} data-env="${escapeHtml(knob.env)}">${opts}</select>` + (knob.option_links && Object.keys(knob.option_links).length
          ? `<div class="knob-links" data-links-for="${escapeHtml(knob.env)}"></div>` : "");
      }
      if (knob.type === "bool") {
        const normalized = String(current || "").toLowerCase();
        const truthy = ["1","true","yes","on"].includes(normalized);
        const falsey = ["0","false","no","off"].includes(normalized);
        return `<select${idAttr} data-env="${escapeHtml(knob.env)}">
          <option value="">default: ${escapeHtml(formatDefault(knob.default, "off"))}</option>
          <option value="1"${truthy ? " selected" : ""}>on</option>
          <option value="0"${falsey ? " selected" : ""}>off</option>
        </select>`;
      }
      const type = knob.type === "password" ? "password" : knob.type === "number" ? "number" : "text";
      const attrs = [
        `data-env="${escapeHtml(knob.env)}"`,
        `type="${type}"`
      ];
      if (id) attrs.unshift(`id="${escapeHtml(id)}"`);
      const min = knob.min !== null && knob.min !== undefined ? ` min="${knob.min}"` : "";
      const max = knob.max !== null && knob.max !== undefined ? ` max="${knob.max}"` : "";
      const step = knob.step !== null && knob.step !== undefined ? ` step="${knob.step}"` : "";
      const placeholder = knob.default !== null && knob.default !== undefined ? ` placeholder="${escapeHtml(String(knob.default))}"` : "";
      return `<input ${attrs.join(" ")} value="${escapeHtml(current)}"${placeholder}${min}${max}${step}>`;
    }
    function knobField(env, label, options={}) {
      const knob = knobByEnv(env);
      if (!knob) return "";
      return `<label class="field"><span>${escapeHtml(label)}</span>${inputForKnob(knob, options)}<code>${escapeHtml(env)}</code></label>`;
    }
    function knobHint(name) {
      if (KNOB_HINTS[name]) return KNOB_HINTS[name];
      if (name.endsWith("_TEMPERATURE")) return "Sampling temperature. Higher values make model output more varied.";
      if (name.endsWith("_TOP_P")) return "Nucleus sampling cutoff. Lower values narrow the token pool.";
      if (name.endsWith("_TOP_K")) return "Top-k sampling cutoff. Lower values restrict candidate tokens.";
      if (name.endsWith("_MIN_P")) return "Minimum probability sampling cutoff for filtering very unlikely tokens.";
      if (name.endsWith("_PRESENCE_PENALTY")) return "Penalty for repeating already mentioned concepts.";
      if (name.endsWith("_REPETITION_PENALTY")) return "Penalty for repeated token patterns in model output.";
      return "Runtime setting passed through to the pipeline or local utility command.";
    }
    let envTipSeq = 0; // document-unique tooltip ids across all decorator runs
    function positionEnvTooltip(icon, tooltip) {
      const GUTTER = 8; // viewport-safe margin on every edge
      const r = icon.getBoundingClientRect();
      const t = tooltip.getBoundingClientRect();
      let top = r.bottom + GUTTER;
      // Flip above the icon when the tooltip would overflow the viewport bottom.
      if (top + t.height > window.innerHeight - GUTTER) top = Math.max(GUTTER, r.top - t.height - GUTTER);
      // Center on the icon, clamped to keep a GUTTER margin on narrow viewports.
      const desiredLeft = r.left + r.width / 2 - t.width / 2;
      const maxLeft = Math.max(GUTTER, window.innerWidth - t.width - GUTTER);
      const left = Math.min(Math.max(GUTTER, desiredLeft), maxLeft);
      tooltip.style.top = `${top}px`;
      tooltip.style.left = `${left}px`;
    }
    // Live tooltips (tooltip -> icon) shared by the delegated scroll/resize
    // handlers below: one global listener pair instead of one per tooltip, so
    // knob-search re-renders can never accumulate document/window listeners.
    const liveTooltips = new Map();
    // Shared iteration for the delegated scroll/resize handlers: prunes
    // entries whose icon was re-rendered away (e.g. knob search), then runs
    // the handler on the remaining (icon, tooltip) pairs.
    function forEachLiveTooltip(handler) {
      liveTooltips.forEach((icon, tooltip) => {
        if (!icon.isConnected) { liveTooltips.delete(tooltip); return; }
        handler(icon, tooltip);
      });
    }
    function hideTooltipsOnScroll() {
      forEachLiveTooltip((icon, tooltip) => {
        // Hide only when the trigger actually left the viewport: Tab-focus
        // scroll-into-view also fires scroll, and hiding then would swallow
        // the tooltip on the keyboard path this feature exists for.
        const r = icon.getBoundingClientRect();
        if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) {
          tooltip.classList.add("hidden");
        }
      });
    }
    function repositionOpenTooltips() {
      forEachLiveTooltip((icon, tooltip) => {
        if (!tooltip.classList.contains("hidden")) positionEnvTooltip(icon, tooltip);
      });
    }
    // Capture phase: scroll events don't bubble, so this is the only way to
    // catch scrolling inside nested containers (Advanced panels, knob list).
    document.addEventListener("scroll", hideTooltipsOnScroll, true);
    window.addEventListener("resize", repositionOpenTooltips);
    function attachEnvTooltip(icon, tooltip) {
      const show = () => {
        tooltip.classList.remove("hidden");
        positionEnvTooltip(icon, tooltip);
        liveTooltips.set(tooltip, icon);
      };
      const hide = () => {
        tooltip.classList.add("hidden");
        liveTooltips.delete(tooltip);
      };
      icon.addEventListener("mouseenter", show);
      icon.addEventListener("mouseleave", hide);
      icon.addEventListener("focus", show);
      icon.addEventListener("blur", hide);
      icon.addEventListener("keydown", ev => {
        // Escape dismisses; Enter/Space activate like a real button (and
        // preventDefault keeps Space from scrolling the page).
        if (ev.key === "Escape") { ev.preventDefault(); hide(); }
        else if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); show(); }
      });
    }
    function decorateEnvHints(root=document) {
      root.querySelectorAll(".field code, .knob code").forEach(code => {
        try {
          const name = code.textContent.trim();
          const label = code.closest("label") || code.closest(".field") || code.closest(".knob");
          const titleTarget = label && (label.querySelector("span") || label);
          // Idempotency guard: each label is decorated at most once across the
          // three call sites; re-runs must not duplicate tooltips or listeners.
          if (!name || !titleTarget || titleTarget.querySelector(".env-info")) return;
          const tip = `${name}: ${knobHint(name)}`;
          // envTipSeq keeps ids unique document-wide across decorator runs.
          const tipId = `env-tip-${++envTipSeq}`;
          titleTarget.insertAdjacentHTML("beforeend",
            `<span class="env-info" tabindex="0" role="button" aria-label="Help for ${escapeHtml(name)}" aria-describedby="${tipId}">i</span>` +
            `<span id="${tipId}" class="env-tooltip hidden" role="tooltip">${escapeHtml(tip)}</span>`);
          const icon = titleTarget.querySelector(".env-info");
          const tipEl = document.getElementById(tipId);
          if (!icon || !tipEl) {
            console.warn(`decorateEnvHints: tooltip nodes missing for "${name}" (id=${tipId})`);
            return;
          }
          attachEnvTooltip(icon, tipEl);
        } catch (err) {
          // One bad hint must never abort the rest of the page's init.
          console.error(`decorateEnvHints: skipping tooltip for "${code.textContent.trim()}"`, err);
        }
      });
    }
    function renderKnobLinks(env) {
      const container = document.querySelector(`[data-links-for="${env}"]`);
      if (!container) {
        console.warn(`renderKnobLinks: no [data-links-for="${env}"] container in the DOM`);
        return;
      }
      const knob = knobByEnv(env);
      if (!knob) {
        container.innerHTML = `<span class="muted">Links unavailable</span>`;
        return;
      }
      const links = knob.option_links ? knob.option_links : {};
      // An empty select means "use the backend default"; mirror that
      // resolution so the default model's links show on initial load and
      // after Clear overrides / Reset defaults. Do NOT pre-select the
      // default here (that would change collectEnv() submission semantics).
      const value = currentControlValue(env) || (knob.default !== undefined && knob.default !== null ? String(knob.default) : "");
      if (!value) { container.innerHTML = ""; return; }
      const entry = links[value];
      if (!entry) {
        // Only fires for values set outside the offered options (external or
        // typed-in ids) — drift-guard tests pin that every option has a link.
        container.innerHTML = `<span class="muted">No Hugging Face page for this external model</span>`;
        return;
      }
      // page and hardware are the same URL on purpose: HF's native Hardware
      // Compatibility panel lives on the model page (see _model_option_links);
      // a future "#hardware" anchor is a one-line change here.
      container.innerHTML = [
        `<a href="${escapeHtml(entry.page)}" target="_blank" rel="noopener noreferrer">Hugging Face page</a>`,
        `<a href="${escapeHtml(entry.hardware)}" target="_blank" rel="noopener noreferrer" title="Native Hardware Compatibility panel (GGUF/MLX) on the model page">Hardware compatibility</a>`
      ].join(" · ");
    }
    // Programmatic value changes (preset apply, clear/reset, startup restore)
    // do not fire `change` events, so re-render links after those paths or the
    // .knob-links container keeps the previous model's links.
    function refreshModelKnobLinks() {
      renderKnobLinks("NEWS_MODEL");
      renderKnobLinks("NEWS_MODEL_ARTICLE_SUMMARY");
      renderKnobLinks("NEWS_MODEL_STORY_DRAFTING");
      renderKnobLinks("NEWS_MODEL_STORY_SCALE_SCREENING");
      renderKnobLinks("NEWS_MODEL_TITLE_GENERATION");
      renderKnobLinks("NEWS_MODEL_IMAGE_ART_DIRECTION");
      // Preset apply, reset/clear, and startup restore re-render the hint so
      // a stale mismatch message cannot outlive a matching model/backend pair.
      const reference = currentControlValue("NEWS_MODEL");
      state.selectedModelReference = reference;
      state.selectedModelRequiredBackend = catalogBackendForReference(reference);
      renderModelBackendHint(requiredBackendForSelectedModel());
    }
    function renderTabs() {
      $("tabs").innerHTML = `<button id="navToggle" class="nav-toggle" title="Collapse navigation" aria-label="Collapse navigation"><span class="collapse-icon">${icons.chevronLeft}</span><span class="expand-icon">${icons.chevronRight}</span></button>` +
        tabs.map(([id, label, icon]) => `<button class="tab-button" data-tab="${id}" title="${escapeHtml(label)}">${icons[icon]}<span class="tab-text">${escapeHtml(label)}</span></button>`).join("");
      $("navToggle").onclick = () => {
        document.body.classList.toggle("nav-collapsed");
        const collapsed = document.body.classList.contains("nav-collapsed");
        $("navToggle").title = collapsed ? "Expand navigation" : "Collapse navigation";
        $("navToggle").setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
      };
      document.querySelectorAll("nav button[data-tab]").forEach(btn => btn.onclick = () => showTab(btn.dataset.tab));
      showTab("runSetup");
    }
    function selectedRunPreset() {
      return state.presets.find(preset => preset.id === state.selectedRunPresetId) || null;
    }
    function renderPresetSummary() {
      const preset = selectedRunPreset();
      const runtime = (state.schema && state.schema.runtime) || {};
      const pieces = [
        preset ? `${preset.name || preset.id}` : "Custom run",
        `Sources ${runtime.source_scope || "core"}`,
        `Recipients ${runtime.recipient_scope || "primary"}`
      ];
      $("presetSummary").textContent = pieces.join(" · ");
    }
    function collectEnv() {
      const env = {};
      const profile = selectedPromptProfile();
      document.querySelectorAll("[data-env]").forEach(input => {
        const name = input.dataset.env;
        let val = input.type === "checkbox" ? (input.checked ? "1" : "") : input.value.trim();
        if (name.startsWith("NEWS_PROMPT_OVERRIDE_") && profile) {
          const task = Object.keys(PROMPT_OVERRIDE_ENVS).find(key => PROMPT_OVERRIDE_ENVS[key] === name);
          const profileText = task && profile.prompts ? (profile.prompts[task] || "") : "";
          if (!val || val === profileText.trim()) return;
        }
        if (val) env[name] = val;
      });
      return env;
    }
    function collectOptions() {
      return {
        limit: value("opt_limit"),
        recent_days: value("opt_recent_days"),
        timeout: value("opt_timeout"),
        concurrency: value("opt_concurrency"),
        section: value("opt_section"),
        language_model: value("opt_language_model"),
        language_samples: value("opt_language_samples"),
        min_language_confidence: value("opt_min_language_confidence"),
        probe_articles: checked("opt_probe_articles"),
        prune_unscrapable: checked("opt_prune_unscrapable"),
        only_failures: checked("opt_only_failures"),
        write_languages: checked("opt_write_languages"),
        overwrite_languages: checked("opt_overwrite_languages"),
        json: checked("opt_json")
      };
    }
    function requestBody(action="run") {
      return { action, preset: state.selectedRunPresetId, env: collectEnv(), options: collectOptions() };
    }
    function envToText(env) {
      return Object.entries(env || {}).map(([key, val]) => `${key}=${val}`).join("\n");
    }
    function textToEnv(text) {
      const env = {};
      String(text || "").split(/\r?\n/).forEach(line => {
        const clean = line.trim();
        if (!clean || clean.startsWith("#")) return;
        const eq = clean.indexOf("=");
        if (eq <= 0) return;
        const key = clean.slice(0, eq).trim();
        const val = clean.slice(eq + 1).trim();
        if (key && val) env[key] = val;
      });
      return env;
    }
    function renderStats() {
      const s = state.schema || {};
      const runtime = s.runtime || {};
      const source = s.sources || {};
      const recipients = s.recipients || {};
      const model = runtime.model || {};
      const assignments = model.assignments || {};
      const articleSummary = model.article_summary || assignments.article_summary || {};
      const storyDrafting = model.story_drafting || assignments.story_drafting || {};
      const scaleScreening = model.story_scale_screening || assignments.story_scale_screening || {};
      const titleGeneration = model.title_generation || assignments.title_generation || {};
      const imageArtDirection = model.image_art_direction || assignments.image_art_direction || {};
      const items = [
        ["Run preset", state.selectedRunPresetId || runtime.preset_id || "custom"],
        ["Source scope", runtime.source_scope || "-"],
        ["Recipient scope", runtime.recipient_scope || "-"],
        ["Sources", source.total ?? 0],
        ["Core selected", source.selected ? source.selected.core : "-"],
        ["Peripheral selected", source.selected ? source.selected.peripheral : "-"],
        ["Recipients", `${recipients.total ?? 0} total`],
        ["Model", model.reference || "-"],
        ["Article Summarization", articleSummary.reference || "-"],
        ["Story Writing", storyDrafting.reference || "-"],
        ["Story Scale Screening", scaleScreening.reference || "-"],
        ["Title Generation", titleGeneration.reference || "-"],
        ["Image Art Direction", imageArtDirection.reference || "-"],
        ["Article concurrency", model.article_summary_concurrency ?? "-"],
        ["Story concurrency", model.story_synthesis_concurrency ?? "-"],
        ["Images", runtime.image && runtime.image.enabled ? "on" : "off"]
      ];
      $("stats").innerHTML = items.map(([label, val]) => `<div class="stat"><span class="muted">${escapeHtml(label)}</span><strong>${escapeHtml(String(val ?? ""))}</strong></div>`).join("");
    }
    function renderRunSetup() {
      const schema = state.schema || {};
      const runtime = schema.runtime || {};
      const defaultRuntime = runtime.model ? runtime.model : {};
      const runtimeError = schema.runtime_error || "";
      const actionOptions = (schema.actions || []).map(action => `<option value="${escapeHtml(action)}"${action === "run" ? " selected" : ""}>${escapeHtml(action)}</option>`).join("");
      const promptProfileOptions = (schema.prompt_profiles || []).map(p => `<option value="${escapeHtml(p.id)}"${currentControlValue("NEWS_PROMPT_PROFILE") === p.id ? " selected" : ""}>${escapeHtml(p.name)}</option>`).join("");
      const sourceToolHidden = !["check-sources", "prune-sources", "source-languages"].includes(value("actionSelect"));
      const sourceScopes = {
        core: "Core",
        peripheral: "All"
      };
      const defaultModel = knobField("NEWS_MODEL", "Default model", { emptyLabel: "default: gemma-4-12b-it-4bit" });
      const sharedModelTokens = knobField("NEWS_MODEL_MAX_INPUT_TOKENS", "Shared model input cap");
      const articleTokenCap = knobField("NEWS_ARTICLE_SUMMARY_MAX_TOKENS", "Article summary max tokens");
      const storyTokenCap = knobField("NEWS_STORY_DRAFTING_MAX_TOKENS", "Story drafting max tokens");
      const scaleTokenCap = knobField("NEWS_STORY_SCALE_SCREENING_MAX_TOKENS", "Scale screening max tokens");
      const titleTokenCap = knobField("NEWS_TITLE_GENERATION_MAX_TOKENS", "Title generation max tokens");
      const articleBaseUrl = knobField("NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL", "Base URL");
      const storyBaseUrl = knobField("NEWS_MODEL_STORY_DRAFTING_BASE_URL", "Base URL");
      const scaleBaseUrl = knobField("NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL", "Base URL");
      const titleBaseUrl = knobField("NEWS_MODEL_TITLE_GENERATION_BASE_URL", "Base URL");
      const articleSampling = [
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_TEMPERATURE", "Temperature"),
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_TOP_P", "Top P"),
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_TOP_K", "Top K"),
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_MIN_P", "Min P"),
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_PRESENCE_PENALTY", "Presence penalty"),
        knobField("NEWS_MODEL_ARTICLE_SUMMARY_REPETITION_PENALTY", "Repetition penalty")
      ].join("");
      const storySampling = [
        knobField("NEWS_MODEL_STORY_DRAFTING_TEMPERATURE", "Temperature"),
        knobField("NEWS_MODEL_STORY_DRAFTING_TOP_P", "Top P"),
        knobField("NEWS_MODEL_STORY_DRAFTING_TOP_K", "Top K"),
        knobField("NEWS_MODEL_STORY_DRAFTING_MIN_P", "Min P"),
        knobField("NEWS_MODEL_STORY_DRAFTING_PRESENCE_PENALTY", "Presence penalty"),
        knobField("NEWS_MODEL_STORY_DRAFTING_REPETITION_PENALTY", "Repetition penalty")
      ].join("");
      const scaleSampling = [
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_TEMPERATURE", "Temperature"),
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_TOP_P", "Top P"),
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_TOP_K", "Top K"),
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_MIN_P", "Min P"),
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_PRESENCE_PENALTY", "Presence penalty"),
        knobField("NEWS_MODEL_STORY_SCALE_SCREENING_REPETITION_PENALTY", "Repetition penalty")
      ].join("");
      const titleSampling = [
        knobField("NEWS_MODEL_TITLE_GENERATION_TEMPERATURE", "Temperature"),
        knobField("NEWS_MODEL_TITLE_GENERATION_TOP_P", "Top P"),
        knobField("NEWS_MODEL_TITLE_GENERATION_TOP_K", "Top K"),
        knobField("NEWS_MODEL_TITLE_GENERATION_MIN_P", "Min P"),
        knobField("NEWS_MODEL_TITLE_GENERATION_PRESENCE_PENALTY", "Presence penalty"),
        knobField("NEWS_MODEL_TITLE_GENERATION_REPETITION_PENALTY", "Repetition penalty")
      ].join("");
      $("runSetupMount").innerHTML = `
        <div class="banner panel">
          <div class="banner-copy">
            <p class="eyebrow">Guided run setup</p>
            <h2>Plan the run top to bottom</h2>
            <p id="presetSummary" class="muted"></p>
          </div>
          <div class="banner-actions">
            <button id="resetDefaultsBtn">Reset defaults</button>
            <button id="openRunPresetDrawerBtn">Preset drawer</button>
            <button id="savePresetBtn">Save preset</button>
            <button id="previewBtn">Preview</button>
            <button id="runBtn" class="primary">Run</button>
          </div>
        </div>
        <div class="stack">
          ${runtimeError ? `<p class="bad" style="margin:0 0 8px">Configuration error: ${escapeHtml(runtimeError)}</p>` : ""}
          <section class="panel">
            <p class="eyebrow">Routing</p>
            <h2>Delivery and sources</h2>
            <div class="form-grid">
              <label class="field"><span>Delivery</span>
                <select id="deliveryMode" data-env="NEWS_DELIVERY_MODE">
                  <option value="">default: Owner only</option>
                  <option value="disabled"${currentControlValue("NEWS_DELIVERY_MODE") === "disabled" ? " selected" : ""}>No delivery</option>
                  <option value="owner"${currentControlValue("NEWS_DELIVERY_MODE") === "owner" ? " selected" : ""}>Owner only</option>
                  <option value="recipients"${currentControlValue("NEWS_DELIVERY_MODE") === "recipients" ? " selected" : ""}>Configured recipients</option>
                </select>
                <code>NEWS_DELIVERY_MODE</code>
              </label>
              <label class="field"><span>Source list</span>
                <select id="sourceScope" data-env="NEWS_SOURCE_SCOPE">
                  <option value="">default: Core</option>
                  <option value="core"${currentControlValue("NEWS_SOURCE_SCOPE") === "core" ? " selected" : ""}>Core</option>
                  <option value="peripheral"${currentControlValue("NEWS_SOURCE_SCOPE") === "peripheral" ? " selected" : ""}>All</option>
                </select>
                <code>NEWS_SOURCE_SCOPE</code>
              </label>
            </div>
          </section>
          <section class="panel">
            <p class="eyebrow">Editorial approach</p>
            <h2>Prompt profile</h2>
            <p id="promptProfileDescription" class="muted"></p>
            <div class="form-grid">
              <label class="field"><span>Profile</span>
                <select id="promptProfileSelect" data-env="NEWS_PROMPT_PROFILE">
                  <option value="">default: Balanced</option>
                  ${promptProfileOptions}
                </select>
                <code>NEWS_PROMPT_PROFILE</code>
              </label>
            </div>
            <div class="toolbar">
              <button id="restorePromptProfileBtn">Restore defaults</button>
            </div>
          </section>
          <section class="panel">
            <p class="eyebrow">Snapshot</p>
            <h2>Effective runtime snapshot</h2>
            <div id="stats" class="stats"></div>
          </section>
          <section class="panel model-card">
            <p class="eyebrow">Model</p>
            <h2>Default model</h2>
            <p class="muted">Resolved: ${escapeHtml(defaultRuntime.name || defaultRuntime.reference || "-")}</p>
            <div class="form-grid">
              ${defaultModel}
            </div>
            <p id="modelBackendHint" class="muted hidden" aria-live="polite"></p>
            <p class="muted">Per-task model alternatives and sampling are in Advanced Settings.</p>
          </section>
          <section class="panel">
            <p class="eyebrow">Model catalog</p>
            <h2>Model catalog and Hugging Face search</h2>
            <p class="muted">Built-in models are verified for the managed backends; user-overlay entries and searchable Hugging Face models carry advisory runtime-fit verdicts (hardware fitting lives on the Hugging Face model page).</p>
            <div class="form-grid">
              <label class="field"><span>Recommendation task</span>
                <select id="recommendationTask">
                  <option value="">Pick a task…</option>
                </select>
                <code>task</code>
              </label>
            </div>
            <div id="recommendationReadout" class="stack"></div>
            <div id="catalogCards" class="stack"></div>
            <details class="details">
              <summary>Search Hugging Face</summary>
              <div class="form-grid">
                <label class="field"><span>Query</span><input id="modelSearchQuery" type="text" placeholder="e.g. gemma"><code>q</code></label>
                <label class="field"><span>Pipeline tag</span>
                  <select id="modelSearchPipeline">
                    <option value="">any</option>
                    <option value="text-generation">text-generation</option>
                    <option value="text2text-generation">text2text-generation</option>
                    <option value="image-text-to-text">image-text-to-text</option>
                  </select>
                  <code>pipeline_tag</code>
                </label>
                <label class="field"><span>Limit</span><input id="modelSearchLimit" type="number" min="1" max="50" value="10"><code>limit</code></label>
              </div>
              <div class="toolbar">
                <button id="modelSearchBtn">Search</button>
              </div>
              <div id="modelSearchResults" class="stack"></div>
            </details>
          </section>
          <section class="panel">
            <details class="details">
              <summary>Utilities</summary>
              <div class="form-grid">
                <label class="field"><span>Action</span><select id="actionSelect">${actionOptions}</select><code>command action</code></label>
              </div>
              <div id="sourceOptions">
                <div class="form-grid">
                  <label class="field"><span>Limit</span><input id="opt_limit" type="number" min="1"><code>--limit</code></label>
                  <label class="field"><span>Recent days</span><input id="opt_recent_days" type="number" min="1" value="7"><code>--recent-days</code></label>
                  <label class="field"><span>Timeout</span><input id="opt_timeout" type="number" min="1"><code>--timeout</code></label>
                  <label class="field"><span>Concurrency</span><input id="opt_concurrency" type="number" min="1"><code>--concurrency</code></label>
                  <label class="field"><span>Section</span><select id="opt_section"><option value=""></option><option>sources</option><option>all</option></select><code>--section</code></label>
                  <label class="field"><span>Language model</span><input id="opt_language_model"><code>--language-model</code></label>
                  <label class="field"><span>Language samples</span><input id="opt_language_samples" type="number" min="1"><code>--language-samples</code></label>
                  <label class="field"><span>Min language confidence</span><input id="opt_min_language_confidence" type="number" step="0.01" min="0" max="1"><code>--min-language-confidence</code></label>
                </div>
                <div class="toolbar">
                  <label><input id="opt_probe_articles" type="checkbox"> Probe articles</label>
                  <label><input id="opt_prune_unscrapable" type="checkbox"> Prune unscrapable</label>
                  <label><input id="opt_only_failures" type="checkbox"> Only failures</label>
                  <label><input id="opt_write_languages" type="checkbox"> Write languages</label>
                  <label><input id="opt_overwrite_languages" type="checkbox"> Overwrite languages</label>
                  <label><input id="opt_json" type="checkbox"> JSON</label>
                </div>
                <div class="toolbar">
                  <button id="utilityPreviewBtn">Preview utility</button>
                  <button id="utilityRunBtn" class="primary">Run utility</button>
                </div>
              </div>
            </details>
          </section>
          <section class="panel">
            <h2>Command preview</h2>
            <pre id="previewPane"></pre>
          </section>
          <section class="panel">
            <div class="toolbar"><h2 style="margin-right:auto">Run log</h2><button id="stopBtn" class="danger">Stop</button></div>
            <pre id="logPane" role="log" aria-live="polite" aria-label="Run log"></pre>
          </section>
        </div>
      `;
      renderPresetSummary();
      renderModelTuningPanels();
      decorateEnvHints($("runSetupMount"));
      renderPromptProfilePanel();
      renderModelCatalogPanel();
      $("actionSelect").value = "run";
      $("sourceOptions").classList.add("hidden");
      $("actionSelect").onchange = () => {
        $("sourceOptions").classList.toggle("hidden", !["check-sources","prune-sources","source-languages"].includes(value("actionSelect")));
      };
      $("recommendationTask").onchange = () => renderRecommendations(value("recommendationTask"));
      $("modelSearchBtn").onclick = () => searchHuggingFaceModels().catch(err => setStatus(err.message, "bad"));
      $("modelSearchQuery").onkeydown = ev => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          searchHuggingFaceModels().catch(err => setStatus(err.message, "bad"));
        }
      };
    }
    // Knob labels for each task's per-task max-tokens env (modelTuningPanel).
    const TASK_MAX_TOKENS_LABELS = {
      article_summary: "Article summary max tokens",
      story_drafting: "Story drafting max tokens",
      story_scale_screening: "Scale screening max tokens",
      title_generation: "Title generation max tokens",
      image_art_direction: "Image art direction max tokens"
    };
    const SAMPLING_FIELDS = [
      ["TEMPERATURE", "Temperature"],
      ["TOP_P", "Top P"],
      ["TOP_K", "Top K"],
      ["MIN_P", "Min P"],
      ["PRESENCE_PENALTY", "Presence penalty"],
      ["REPETITION_PENALTY", "Repetition penalty"]
    ];
    function samplingFields(prefix) {
      return SAMPLING_FIELDS.map(([suffix, label]) => knobField(`${prefix}_${suffix}`, label)).join("");
    }
    function modelTuningPanel(task) {
      const meta = TASK_CONFIG[task];
      const runtime = (state.schema && state.schema.runtime) || {};
      const taskRuntime = runtime.model && runtime.model[meta.runtimeKey] ? runtime.model[meta.runtimeKey] : {};
      const sharedCap = task === "article_summary" ? knobField("NEWS_MODEL_MAX_INPUT_TOKENS", "Shared model input cap") : "";
      return `
        <section class="panel model-card">
          <p class="eyebrow">Model Tuning</p>
          <h2>${escapeHtml(meta.label)}</h2>
          <p class="muted">Resolved: ${escapeHtml(taskRuntime.name || taskRuntime.reference || "-")}</p>
          <div class="form-grid">
            <label class="field"><span>Tuning preset</span><select id="${meta.presetSelectId}" data-task="${task}" data-env="${meta.presetEnv}"></select><code>${meta.presetEnv}</code></label>
            <label class="field"><span>Preset id</span><input id="${meta.idInputId}"><code>id</code></label>
            <label class="field"><span>Display name</span><input id="${meta.nameInputId}"><code>name</code></label>
            <label class="field"><span>Description</span><textarea id="${meta.descriptionInputId}"></textarea><code>description</code></label>
            ${sharedCap}
            ${knobField(meta.taskMaxTokensEnv, TASK_MAX_TOKENS_LABELS[task] || "Max tokens")}
            ${knobField(meta.baseUrlEnv, "Base URL")}
            ${samplingFields(meta.taskSamplingPrefix)}
          </div>
          <div class="toolbar">
            <button id="${meta.saveButtonId}" class="primary">Save current settings</button>
            <button id="${meta.renameButtonId}">Rename display name</button>
            <button id="${meta.deleteButtonId}" class="danger">Delete preset</button>
          </div>
        </section>`;
    }
    function renderAdvancedPanels() {
      // Static container from the #advanced view; guard keeps this idempotent.
      // Must run before renderModelTuningControls() so the tuning <select>s exist.
      const container = $("advancedPanels");
      if (!container) {
        console.error("renderAdvancedPanels: missing #advancedPanels container");
        return;
      }
      container.innerHTML = `
        ${modelTuningPanel("article_summary")}
        ${modelTuningPanel("story_drafting")}
        ${modelTuningPanel("story_scale_screening")}
        ${modelTuningPanel("title_generation")}
        ${modelTuningPanel("image_art_direction")}
        <section class="panel">
          <p class="eyebrow">Budgets</p>
          <h2>Run budgets and quotas</h2>
          <div class="form-grid">
            ${knobField("NEWS_SOURCE_COLLECTION_CONCURRENCY", "Source collection concurrency")}
            ${knobField("NEWS_ARTICLE_SUMMARY_CONCURRENCY", "Article summary concurrency")}
            ${knobField("NEWS_STORY_SYNTHESIS_CONCURRENCY", "Story synthesis concurrency")}
            ${knobField("NEWS_ARTICLE_TEXT_TOKEN_LIMIT", "Article text token limit")}
            ${knobField("NEWS_MAX_STORIES", "Max stories")}
            ${knobField("NEWS_MIN_ARTICLES_PER_STORY", "Min articles per story")}
            ${knobField("NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD", "Story cluster similarity")}
            ${knobField("NEWS_STORY_SELECTION_OVERLAP_THRESHOLD", "Story selection overlap")}
            ${knobField("NEWS_STORY_DEDUP_THRESHOLD", "Story dedup threshold")}
            ${knobField("NEWS_STORY_BACKFILL_BATCH_MULTIPLIER", "Backfill batch multiplier")}
          </div>
        </section>
        <section class="panel">
          <p class="eyebrow">Peripheral</p>
          <h2>Optional run settings</h2>
          <div class="form-grid">
            ${knobField("NEWS_IMAGE_ENABLED", "Image generation")}
            ${knobField("NEWS_BLOCK_REUSED_URLS", "Block reused URLs")}
            ${knobField("NEWS_STORY_SCALE_SCREENING_ENABLED", "Story scale screening")}
            ${knobField("NEWS_RELAX_STORY_DRAFTING_GUARDS", "Relax story drafting guards")}
          </div>
        </section>
        <section class="panel">
          <p class="eyebrow">Prompt templates</p>
          <h2>Full prompt templates</h2>
          <p class="muted">Read-only preview of the exact instructions the selected profile supplies to each LLM stage.</p>
          <div id="promptProfileReadouts" class="stack"></div>
          <div class="toolbar">
            <button id="comparePromptProfileBtn">Compare with balanced</button>
          </div>
          <details class="details">
            <summary>Comparison with balanced</summary>
            <div id="promptProfileCompare"></div>
          </details>
        </section>
      `;
      decorateEnvHints($("advancedPanels"));
      renderPromptProfilePanel();
    }
    function renderModelTuningControls(task, { preserveEditor = false } = {}) {
      const meta = TASK_CONFIG[task];
      if (!meta) return;
      const select = $(meta.presetSelectId);
      if (!select) return;
      const selectedModel = currentControlValue(meta.modelEnv) || (state.schema && state.schema.runtime && state.schema.runtime.model && state.schema.runtime.model[meta.runtimeKey] && state.schema.runtime.model[meta.runtimeKey].reference) || "";
      const resolvedName = state.schema && state.schema.runtime && state.schema.runtime.model && state.schema.runtime.model[meta.runtimeKey] && state.schema.runtime.model[meta.runtimeKey].name || "";
      const presets = filteredModelTuningPresets(task, selectedModel, resolvedName);
      const currentPresetId = currentControlValue(meta.presetEnv);
      if (currentPresetId && !presets.some(preset => preset.id === currentPresetId)) {
        const existing = state.modelTuningPresets.find(preset => preset.id === currentPresetId);
        presets.unshift(existing || {
          id: currentPresetId,
          name: currentPresetId,
          description: "",
          modified_at: "",
          model: "",
          task: task,
          tuning: {}
        });
      }
      const currentValue = select.value || currentPresetId || "";
      select.innerHTML = [
        `<option value="">custom values</option>`,
        ...presets.map(preset => {
          const labelParts = [preset.name || preset.id, preset.id];
          if (preset.model && preset.model !== selectedModel) labelParts.push(`model: ${preset.model}`);
          if (preset.task && preset.task !== task) labelParts.push(`task: ${preset.task}`);
          if (preset.modified_at) labelParts.push(`updated ${preset.modified_at}`);
          return `<option value="${escapeHtml(preset.id)}"${currentValue === preset.id ? " selected" : ""}>${escapeHtml(labelParts.join(" · "))}</option>`;
        })
      ].join("");
      if (currentValue) select.value = currentValue;
      const preset = state.modelTuningPresets.find(item => item.id === currentValue) || null;
      if (preset && !preserveEditor) {
        loadModelTuningEditor(task, preset, { refresh: false });
      }
    }
    function renderModelTuningPanels() {
      Object.keys(TASK_CONFIG).forEach(task => renderModelTuningControls(task));
    }
    function filteredModelTuningPresets(task, selectedModel, resolvedName) {
      const matches = state.modelTuningPresets.filter(preset => {
        const modelOk = !preset.model || preset.model === selectedModel || preset.model === resolvedName;
        const taskOk = !preset.task || preset.task === task;
        return modelOk && taskOk;
      });
      return matches.length ? matches : state.modelTuningPresets.slice();
    }
    function modelTuningPayload(task) {
      const meta = TASK_CONFIG[task];
      const tuning = {};
      const sharedMax = valueByEnv("NEWS_MODEL_MAX_INPUT_TOKENS");
      if (sharedMax) tuning.model_max_input_tokens = sharedMax;
      const taskMax = valueByEnv(meta.taskMaxTokensEnv);
      if (taskMax) tuning[`${meta.runtimeKey}_max_tokens`] = taskMax;
      const samplingFields = SAMPLING_FIELDS.map(([suffix]) => [suffix.toLowerCase(), `${meta.taskSamplingPrefix}_${suffix}`]);
      samplingFields.forEach(([key, env]) => {
        const raw = valueByEnv(env);
        if (raw) tuning[key] = raw;
      });
      return {
        id: value(meta.idInputId).trim(),
        name: value(meta.nameInputId).trim(),
        description: value(meta.descriptionInputId).trim(),
        model: value(meta.modelSelectId).trim(),
        task,
        tuning
      };
    }
    function valueByEnv(env) {
      const el = document.querySelector(`[data-env="${env}"]`);
      return el ? el.value.trim() : "";
    }
    function loadModelTuningEditor(task, preset, { refresh = true } = {}) {
      const meta = TASK_CONFIG[task];
      if (!meta) return;
      setControlValue(meta.presetEnv, preset ? preset.id || "" : "");
      $(meta.idInputId).value = preset ? preset.id || "" : "";
      $(meta.nameInputId).value = preset ? preset.name || preset.id || "" : "";
      $(meta.descriptionInputId).value = preset ? preset.description || "" : "";
      if (preset && preset.model) {
        setControlValue(meta.modelEnv, preset.model);
      }
      if (preset) {
        const tuning = preset.tuning || {};
        if (tuning.model_max_input_tokens) setControlValue("NEWS_MODEL_MAX_INPUT_TOKENS", tuning.model_max_input_tokens);
        const taskMaxTokens = tuning[`${meta.runtimeKey}_max_tokens`];
        if (taskMaxTokens) setControlValue(meta.taskMaxTokensEnv, taskMaxTokens);
        SAMPLING_FIELDS.forEach(([suffix]) => {
          const field = suffix.toLowerCase();
          if (tuning[field] !== undefined && tuning[field] !== null && tuning[field] !== "") {
            setControlValue(`${meta.taskSamplingPrefix}_${suffix}`, tuning[field]);
          }
        });
      }
      if (refresh) renderModelTuningControls(task, { preserveEditor: true });
    }
    function openRunPresetDialog() {
      const dialog = $("runPresetDialog");
      if (!dialog.open) dialog.showModal();
    }
    function closeRunPresetDialog() {
      const dialog = $("runPresetDialog");
      if (dialog.open) dialog.close();
    }
    function renderRunPresetDrawer() {
      const rows = state.presets || [];
      const selected = state.selectedRunPresetId;
      $("presetTable").innerHTML = `
        <thead><tr><th>Name</th><th>ID</th><th>Modified</th><th>Actions</th></tr></thead>
        <tbody>
          ${rows.map(preset => `
            <tr data-id="${escapeHtml(preset.id || "")}" class="${preset.id === selected ? "selected" : ""}">
              <td><strong>${escapeHtml(preset.name || preset.id || "")}</strong></td>
              <td><code>${escapeHtml(preset.id || "")}</code></td>
              <td>${escapeHtml(preset.modified_at || "Last modified: unknown")}</td>
              <td>
                <div class="toolbar">
                  <button data-action="apply" data-id="${escapeHtml(preset.id || "")}">Apply</button>
                  <button data-action="edit" data-id="${escapeHtml(preset.id || "")}">Edit</button>
                  <button data-action="rename" data-id="${escapeHtml(preset.id || "")}">Rename</button>
                  <button data-action="delete" class="danger" data-id="${escapeHtml(preset.id || "")}">Delete</button>
                </div>
              </td>
            </tr>
          `).join("")}
        </tbody>
      `;
      document.querySelectorAll("#presetTable [data-action]").forEach(btn => {
        btn.onclick = event => {
          event.stopPropagation();
          const id = btn.dataset.id || "";
          const preset = state.presets.find(item => item.id === id) || null;
          if (!preset) return;
          if (btn.dataset.action === "apply") {
            applyRunPreset(preset);
            closeRunPresetDialog();
          } else if (btn.dataset.action === "edit") {
            editRunPreset(id);
          } else if (btn.dataset.action === "rename") {
            editRunPreset(id);
            $("preset_name").focus();
          } else if (btn.dataset.action === "delete") {
            editRunPreset(id);
            deleteSelectedPreset();
          }
        };
      });
      document.querySelectorAll("#presetTable tr[data-id]").forEach(row => {
        row.onclick = () => editRunPreset(row.dataset.id);
      });
    }
    function editRunPreset(id) {
      const preset = state.presets.find(item => item.id === id) || { id: "", name: "", description: "", env: {} };
      $("preset_id").value = preset.id || "";
      $("preset_name").value = preset.name || preset.id || "";
      $("preset_description").value = preset.description || "";
      $("preset_env").value = envToText(preset.env || {});
    }
    function collectRunPresetEditor() {
      return {
        id: value("preset_id").trim(),
        name: value("preset_name").trim(),
        description: value("preset_description").trim(),
        env: textToEnv(value("preset_env"))
      };
    }
    function prepRunPresetEditorFromCurrent() {
      const preset = selectedRunPreset();
      $("preset_id").value = preset ? preset.id || "" : "";
      $("preset_name").value = preset ? preset.name || preset.id || "" : "";
      $("preset_description").value = preset ? preset.description || "" : "";
      $("preset_env").value = envToText(collectEnv());
    }
    function applyRunPreset(preset) {
      state.selectedRunPresetId = preset.id || "";
      setKnobEnv(preset.env || {});
      renderPresetSummary();
      renderModelTuningPanels();
      renderPromptProfilePanel();
      refreshModelKnobLinks();
      void previewQuietly("run");
    }
    function resetAllOverrides() {
      document.querySelectorAll("[data-env]").forEach(el => {
        if (el.type === "checkbox") el.checked = false;
        else el.value = "";
      });
      state.selectedRunPresetId = "";
      renderPresetSummary();
      renderModelTuningPanels();
      renderPromptProfilePanel();
      refreshModelKnobLinks();
      void previewQuietly("run");
    }
    function setKnobEnv(env) {
      document.querySelectorAll("[data-env]").forEach(el => {
        const val = env[el.dataset.env] || "";
        if (el.type === "checkbox") {
          el.checked = ["1", "true", "yes", "on"].includes(String(val).toLowerCase());
          return;
        }
        if (el.tagName === "SELECT" && val && !Array.from(el.options).some(option => option.value === val)) {
          el.insertAdjacentHTML("afterbegin", `<option value="${escapeHtml(val)}">${escapeHtml(val)}</option>`);
        }
        el.value = val;
      });
    }
    async function savePresetEditor() {
      const body = collectRunPresetEditor();
      if (!body.id) throw new Error("Preset id is required.");
      const exists = state.presets.some(preset => preset.id === body.id);
      await api("/api/presets", { method: exists ? "PATCH" : "POST", body: JSON.stringify(body) });
      await loadPresets();
      state.selectedRunPresetId = body.id;
      renderPresetSummary();
      renderRunPresetDrawer();
    }
    async function renamePresetDisplayName() {
      const id = value("preset_id").trim();
      if (!id) return;
      await api("/api/presets", { method: "PATCH", body: JSON.stringify({ id, name: value("preset_name").trim() }) });
      await loadPresets();
      renderPresetSummary();
      renderRunPresetDrawer();
    }
    async function deleteSelectedPreset() {
      const id = value("preset_id").trim();
      if (!id) return;
      const ok = await confirmAction(`Delete run preset ${id}? This cannot be undone.`);
      if (!ok) return;
      await api(`/api/presets?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadPresets();
      if (state.selectedRunPresetId === id) {
        state.selectedRunPresetId = "";
      }
      editRunPreset("");
      renderPresetSummary();
      renderRunPresetDrawer();
    }
    async function loadPresets() {
      const data = await api("/api/presets");
      state.presets = data.presets || [];
      renderRunPresetDrawer();
    }
    async function loadModelTuningPresets() {
      const data = await api("/api/model-tuning-presets");
      state.modelTuningPresets = data.presets || [];
      renderModelTuningPanels();
      renderModelTuningEditor();
    }
    function renderAdvancedKnobs() {
      const search = value("knobSearch").toLowerCase();
      const groups = {};
      (state.schema.knobs || []).forEach(knob => {
        if (SURFACED_ENVS.has(knob.env)) return;
        const hay = `${knob.label} ${knob.env} ${knob.group}`.toLowerCase();
        if (search && !hay.includes(search)) return;
        (groups[knob.group] ||= []).push(knob);
      });
      const orderedGroups = ["Run Settings", "Model Selection", "Model Tuning", "Pipeline Budget", "Model Server Settings"];
      const renderCards = list => list.map(knob => `
        <div class="knob">
          <label>${escapeHtml(knob.label)}</label>
          ${inputForKnob(knob)}
          <code>${escapeHtml(knob.env)}</code>
        </div>
      `).join("");
      $("knobContainer").innerHTML = [...orderedGroups, ...Object.keys(groups).filter(group => !orderedGroups.includes(group)).sort()].map(group => {
        const knobs = groups[group];
        if (!knobs || !knobs.length) return "";
        return `
          <div class="knob-group">
            <h2>${escapeHtml(group)}</h2>
            <div class="knobs">${renderCards(knobs)}</div>
          </div>
        `;
      }).join("");
      decorateEnvHints($("knobContainer"));
      renderKnobLinks("NEWS_MODEL");
    }
    function collectModelTuningPresetBody(task) {
      return modelTuningPayload(task);
    }
    async function saveModelTuningPreset(task) {
      const body = collectModelTuningPresetBody(task);
      if (!body.id) throw new Error("Model tuning preset id is required.");
      const exists = state.modelTuningPresets.some(preset => preset.id === body.id);
      await api("/api/model-tuning-presets", { method: exists ? "PATCH" : "POST", body: JSON.stringify(body) });
      await loadModelTuningPresets();
      renderModelTuningControls(task);
    }
    async function renameModelTuningPreset(task) {
      const meta = TASK_CONFIG[task];
      const id = value(meta.idInputId).trim();
      if (!id) return;
      await api("/api/model-tuning-presets", { method: "PATCH", body: JSON.stringify({ id, name: value(meta.nameInputId).trim() }) });
      await loadModelTuningPresets();
      renderModelTuningControls(task);
    }
    async function deleteModelTuningPreset(task) {
      const meta = TASK_CONFIG[task];
      const id = value(meta.idInputId).trim();
      if (!id) return;
      const ok = await confirmAction(`Delete model tuning preset ${id}? This cannot be undone.`);
      if (!ok) return;
      await api(`/api/model-tuning-presets?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadModelTuningPresets();
      $(meta.presetSelectId).value = "";
      $(meta.idInputId).value = "";
      $(meta.nameInputId).value = "";
      $(meta.descriptionInputId).value = "";
      renderModelTuningControls(task);
    }
    // Dedicated Model Tuning tab: an all-record catalog editor. It lists every
    // saved preset (no model/task filtering), mirrors the Run Preset drawer's
    // row-selection/New/Reload/Save/Delete patterns, and edits the nested
    // tuning map as formatted JSON. The server validator stays authoritative;
    // client JSON parsing only gives immediate feedback without losing edits.
    function renderModelTuningEditor() {
      const rows = state.modelTuningPresets || [];
      const selected = state.selectedModelTuningPresetId;
      $("modelTuningPresetTable").innerHTML = `
        <thead><tr><th>Name</th><th>ID</th><th>Model</th><th>Task</th><th>Modified</th><th>Tuning fields</th></tr></thead>
        <tbody>
          ${rows.map(preset => `
            <tr data-id="${escapeHtml(preset.id || "")}" class="${preset.id === selected ? "selected" : ""}">
              <td><strong>${escapeHtml(preset.name || preset.id || "")}</strong></td>
              <td><code>${escapeHtml(preset.id || "")}</code></td>
              <td>${escapeHtml(preset.model || "")}</td>
              <td>${escapeHtml(preset.task || "global")}</td>
              <td>${escapeHtml(preset.modified_at || "Last modified: unknown")}</td>
              <td>${escapeHtml(Object.keys(preset.tuning || {}).join(", "))}</td>
            </tr>
          `).join("")}
        </tbody>
      `;
      document.querySelectorAll("#modelTuningPresetTable tr[data-id]").forEach(row => {
        row.onclick = () => editModelTuningPreset(row.dataset.id);
      });
    }
    function editModelTuningPreset(id) {
      const preset = state.modelTuningPresets.find(item => item.id === id) || null;
      $("modelTuningPresetError").textContent = "";
      $("modelTuningPresetId").value = preset ? preset.id || "" : "";
      $("modelTuningPresetName").value = preset ? preset.name || preset.id || "" : "";
      $("modelTuningPresetDescription").value = preset ? preset.description || "" : "";
      $("modelTuningPresetModel").value = preset ? preset.model || "" : "";
      $("modelTuningPresetTask").value = preset ? preset.task || "" : "";
      $("modelTuningPresetTuning").value = preset ? JSON.stringify(preset.tuning || {}, null, 2) : "{}";
      state.selectedModelTuningPresetId = id;
    }
    function collectModelTuningPresetEditorBody() {
      const rawTuning = value("modelTuningPresetTuning").trim();
      let tuning = {};
      if (rawTuning) {
        try {
          tuning = JSON.parse(rawTuning);
        } catch (err) {
          throw new Error(`Tuning JSON is not valid: ${err.message}`);
        }
      }
      if (!tuning || typeof tuning !== "object" || Array.isArray(tuning)) {
        throw new Error("Tuning must be a JSON object (mapping).");
      }
      return {
        id: value("modelTuningPresetId").trim(),
        name: value("modelTuningPresetName").trim(),
        description: value("modelTuningPresetDescription").trim(),
        model: value("modelTuningPresetModel").trim(),
        task: value("modelTuningPresetTask").trim(),
        tuning
      };
    }
    async function saveModelTuningEditor() {
      let body;
      try {
        body = collectModelTuningPresetEditorBody();
      } catch (err) {
        $("modelTuningPresetError").textContent = err.message;
        return;
      }
      if (!body.id) {
        $("modelTuningPresetError").textContent = "Model tuning preset id is required.";
        return;
      }
      const exists = state.modelTuningPresets.some(preset => preset.id === body.id);
      try {
        await api("/api/model-tuning-presets", { method: exists ? "PATCH" : "POST", body: JSON.stringify(body) });
        await loadModelTuningPresets();
        state.selectedModelTuningPresetId = body.id;
        const preset = state.modelTuningPresets.find(item => item.id === body.id);
        if (preset) editModelTuningPreset(preset.id);
        $("modelTuningPresetError").textContent = "";
        setStatus(`Model tuning preset ${body.id} saved.`, "good");
      } catch (err) {
        $("modelTuningPresetError").textContent = err.message;
      }
    }
    async function deleteModelTuningEditor() {
      const id = value("modelTuningPresetId").trim();
      if (!id) return;
      const ok = await confirmAction(`Delete model tuning preset ${id}? This cannot be undone.`);
      if (!ok) return;
      try {
        await api(`/api/model-tuning-presets?id=${encodeURIComponent(id)}`, { method: "DELETE" });
        await loadModelTuningPresets();
        if (state.selectedModelTuningPresetId === id) state.selectedModelTuningPresetId = "";
        editModelTuningPreset("");
        $("modelTuningPresetError").textContent = "";
        setStatus(`Model tuning preset ${id} deleted.`, "good");
      } catch (err) {
        $("modelTuningPresetError").textContent = err.message;
      }
    }
    async function reloadModelTuningPresets() {
      await loadModelTuningPresets();
      if (state.selectedModelTuningPresetId) {
        const preset = state.modelTuningPresets.find(item => item.id === state.selectedModelTuningPresetId);
        if (preset) editModelTuningPreset(preset.id);
        else state.selectedModelTuningPresetId = "";
      }
      $("modelTuningPresetError").textContent = "";
      setStatus("Model tuning presets reloaded.", "good");
    }
    function confirmAction(message) {
      return Promise.resolve(window.confirm(message));
    }
    async function preview(action="run") {
      const data = await api("/api/preview", { method: "POST", body: JSON.stringify(requestBody(action)) });
      $("previewPane").textContent = data.command_text + (data.runtime_error ? `\n\nPreview error: ${data.runtime_error}` : "");
      return data;
    }
    function updateRunControls() {
      const active = Boolean(state.activeRun);
      ["runBtn", "utilityRunBtn"].forEach(id => {
        const btn = $(id);
        if (!btn) return;
        btn.disabled = active;
        btn.title = active ? "A run is already active; stop it or wait for it to finish." : "";
      });
      const stopBtn = $("stopBtn");
      if (stopBtn) stopBtn.disabled = !active;
    }
    async function previewQuietly(action="run") {
      // Auto-refresh path: surface failures inline in the preview pane instead
      // of discarding them, so a rejected config never silently freezes the
      // preview on stale content while the user is editing.
      try {
        await preview(action);
      } catch (err) {
        $("previewPane").textContent = `Preview unavailable: ${err.message}`;
      }
    }
    // Run log rendering: one mutable progress row per active stage, plain
    // message rows appended, and a restrained spinner while a stage is live.
    // Glyphs are decoration only: numeric counts and status words stay in
    // every line so the log reads fine when glyphs are unavailable.
    const SPINNER_GLYPHS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    const TERMINAL_GLYPHS = { completed: "✓", failed: "✗", stopped: "■" };
    const TERMINAL_STATUSES = ["completed", "failed", "stopped"];
    const logState = { rows: [], progressByStage: new Map(), spinnerIndex: 0, spinnerTimer: null };
    function resetLog() {
      stopSpinner();
      logState.rows = [];
      logState.progressByStage = new Map();
      logState.spinnerIndex = 0;
      $("logPane").textContent = "";
    }
    function stopSpinner() {
      if (logState.spinnerTimer !== null) {
        clearInterval(logState.spinnerTimer);
        logState.spinnerTimer = null;
      }
    }
    function renderLog() {
      const pane = $("logPane");
      const rows = [];
      for (const row of logState.rows) {
        let text = row.text;
        if (row.stage) {
          if (row.live) {
            text = `${SPINNER_GLYPHS[logState.spinnerIndex % SPINNER_GLYPHS.length]} ${text}`;
          } else if (row.complete) {
            text = `✓ ${text}`;
          }
        }
        rows.push(text);
      }
      pane.textContent = rows.join("\n");
      pane.scrollTop = pane.scrollHeight;
    }
    function startSpinner() {
      stopSpinner();
      logState.spinnerTimer = setInterval(() => {
        logState.spinnerIndex += 1;
        renderLog();
      }, 160);
    }
    function appendLogEvent(payload) {
      const parts = String(payload.line || "").split("\n");
      for (const line of parts) {
        if (!line) continue;
        if (payload.kind === "progress" && payload.stage) {
          const index = logState.progressByStage.get(payload.stage);
          const live = !payload.complete;
          const complete = Boolean(payload.complete);
          if (index !== undefined) {
            if (logState.rows[index].text === line) continue; // defensive duplicate
            logState.rows[index].text = line;
            logState.rows[index].live = live;
            logState.rows[index].complete = complete;
          } else {
            logState.progressByStage.set(payload.stage, logState.rows.length);
            logState.rows.push({ stage: payload.stage, text: line, live: live, complete: complete });
          }
        } else {
          logState.rows.push({ stage: null, text: line });
        }
      }
      renderLog();
    }
    function terminalGlyph(status) {
      return TERMINAL_GLYPHS[status] || "•";
    }
    function finalizeRun(payload, events) {
      if (!TERMINAL_STATUSES.includes(payload.status)) return false;
      stopSpinner();
      for (const row of logState.rows) {
        if (row.stage) row.live = false;
      }
      appendLogEvent({ line: `${terminalGlyph(payload.status)} [ui] ${payload.status}` });
      state.activeRun = null;
      updateRunControls();
      setStatus(`Run ${payload.run_id} ${payload.status}.`, payload.status === "failed" ? "bad" : "muted");
      events.close();
      refreshReviewData();
      return true;
    }
    async function runAction(action="run") {
      if (state.activeRun) {
        setStatus(`A run is already active (${state.activeRun}); stop it or wait for it to finish.`, "warn");
        return;
      }
      const data = await api("/api/run", { method: "POST", body: JSON.stringify(requestBody(action)) });
      state.activeRun = data.run_id;
      updateRunControls();
      resetLog();
      startSpinner();
      const events = new EventSource(`/api/runs/${data.run_id}/events`);
      let recoveryStarted = false;
      const pollRunStatus = async () => {
        if (state.activeRun !== data.run_id) return;
        try {
          const payload = await api(`/api/runs/${encodeURIComponent(data.run_id)}`);
          if (finalizeRun(payload, events)) return;
        } catch (err) {
          setStatus(`Live stream unavailable; status check failed: ${err.message}`, "bad");
        }
        window.setTimeout(pollRunStatus, 1000);
      };
      const recoverFromStream = () => {
        if (recoveryStarted || state.activeRun !== data.run_id) return;
        recoveryStarted = true;
        events.close();
        stopSpinner();
        setStatus("Live run stream disconnected; checking run status…", "warn");
        void pollRunStatus();
      };
      events.onmessage = event => {
        try {
          appendLogEvent(JSON.parse(event.data));
        } catch (err) {
          setStatus(`Live run stream sent invalid data: ${err.message}`, "bad");
          recoverFromStream();
        }
      };
      events.onerror = () => {
        recoverFromStream();
      };
      events.addEventListener("status", event => {
        try {
          const payload = JSON.parse(event.data);
          if (!finalizeRun(payload, events) && !TERMINAL_STATUSES.includes(payload.status)) {
            setStatus(`Run status: ${payload.status}`, "warn");
          }
        } catch (err) {
          setStatus(`Run status stream sent invalid data: ${err.message}`, "bad");
          recoverFromStream();
        }
      });
    }
    function badgeClass(status) {
      if (!status) return "";
      if (status === "completed" || status === "available" || status === "sent") return "good";
      if (status === "failed" || status === "unavailable") return "bad";
      if (status.startsWith("skipped") || status === "aborted" || status === "not_generated") return "warn";
      return "";
    }
    function deliveryLabel(status) {
      return status || "not recorded";
    }
    function renderBadges(runStatus, reportStatus, deliveryStatus) {
      return `<div class="badge-row">` +
        `<span class="badge ${badgeClass(runStatus)}">run: ${escapeHtml(runStatus || "unknown")}</span>` +
        `<span class="badge ${badgeClass(reportStatus)}">report: ${escapeHtml(reportStatus || "unavailable")}</span>` +
        `<span class="badge ${badgeClass(deliveryStatus)}">delivery: ${escapeHtml(deliveryLabel(deliveryStatus))}</span>` +
        `</div>`;
    }
    async function loadReview() {
      try {
        state.latestReview = await api("/api/review/latest");
      } catch (err) {
        state.latestReview = { error: err.message, report_status: "unavailable" };
      }
      renderReview();
    }
    function metadataWarnings(errors) {
      const entries = Object.entries(errors || {});
      if (!entries.length) return "";
      return `<div class="bad" style="margin:0 0 8px">` +
        entries.map(([field, message]) =>
          `<p style="margin:0 0 4px"><strong>${escapeHtml(field)}:</strong> ${escapeHtml(message)}</p>`
        ).join("") +
        `</div>`;
    }
    function renderReview() {
      const review = state.latestReview || {};
      const delivery = review.delivery || {};
      const reportStatus = review.report_status || "unavailable";
      const runStatus = review.run_status || "unknown";
      const deliveryStatus = review.delivery_status || "not recorded";
      const acceptedRecipients = Array.isArray(delivery.accepted_recipients)
        ? delivery.accepted_recipients : [];
      const rejectedRecipients = Array.isArray(delivery.rejected_recipients)
        ? delivery.rejected_recipients : [];
      const deliveryMeta = [
        delivery.reason ? `reason: ${delivery.reason}` : "",
        delivery.phase ? `phase: ${delivery.phase}` : "",
        acceptedRecipients.length ? `accepted: ${acceptedRecipients.join(", ")}` : "",
        rejectedRecipients.length ? `rejected: ${rejectedRecipients.join(", ")}` : "",
        delivery.error_type ? `${delivery.error_type}: ${delivery.error_message}` : ""
      ].filter(Boolean).join(" · ");
      $("reviewMount").innerHTML = `
        <div class="banner panel">
          <div class="banner-copy">
            <p class="eyebrow">Report Review</p>
            <h2>Latest run review</h2>
            <p class="muted">Read-only view of the current report; run, report, and delivery status are separate.</p>
          </div>
          <div class="banner-actions">
            <button id="refreshReviewBtn">Refresh</button>
          </div>
        </div>
        ${review.error ? `<p class="bad" style="margin:0 0 8px">${escapeHtml(review.error)}</p>` : ""}
        ${metadataWarnings(review.metadata_read_errors)}
        <div class="stack">
          <section class="panel">
            <p class="eyebrow">Status</p>
            <h2>Outcomes</h2>
            ${renderBadges(runStatus, reportStatus, deliveryStatus)}
            <div class="form-grid" style="margin-top:12px">
              <div class="field"><span>Run id</span><code>${escapeHtml(review.run_id || "—")}</code></div>
              <div class="field"><span>Started</span><code>${escapeHtml(review.run_started_at || "—")}</code></div>
              <div class="field"><span>Preset</span><code>${escapeHtml(review.preset_id || "custom")}</code></div>
              <div class="field"><span>Duration</span><code>${escapeHtml(review.duration_label || "—")}</code></div>
            </div>
            ${deliveryMeta ? `<p class="muted" style="margin:10px 0 0">${escapeHtml(deliveryMeta)}</p>` : ""}
          </section>
          <section class="panel">
            <div class="toolbar"><h2 style="margin-right:auto">Report</h2></div>
            <pre id="reviewReportPane" class="review-report"></pre>
          </section>
        </div>
      `;
      $("refreshReviewBtn").onclick = () => refreshReviewData();
      const pane = $("reviewReportPane");
      if (review.report_text && review.report_text.trim()) {
        pane.textContent = review.report_text;
      } else if (reportStatus === "available") {
        pane.textContent = "(empty report)";
      } else if (runStatus === "failed" || runStatus === "aborted") {
        pane.textContent = "No report was generated for this run.";
      } else {
        pane.textContent = "No completed report is available yet. Run the pipeline from Run Setup to generate one.";
      }
    }
    async function loadHistory() {
      try {
        const data = await api("/api/history");
        state.history = data.runs || [];
        state.historyError = data.error || null;
      } catch (err) {
        state.history = [];
        state.historyError = err.message;
      }
      renderHistory();
    }
    function renderHistory() {
      const rows = state.history || [];
      $("historyMount").innerHTML = `
        <section class="panel">
          <div class="toolbar">
            <h2 style="margin-right:auto">Recent runs</h2>
            <button id="refreshHistoryBtn">Refresh</button>
          </div>
          ${state.historyError ? `<p class="bad" style="margin:0 0 8px">${escapeHtml(state.historyError)}</p>` : ""}
          ${rows.length
            ? `<div class="table-wrap"><table id="historyTable">
                <thead><tr><th>Run</th><th>Started</th><th>Status</th><th>Report</th><th>Delivery</th><th>Preset</th></tr></thead>
                <tbody>${rows.map(run => `
                  <tr data-run-id="${escapeHtml(run.run_id)}">
                    <td><code>${escapeHtml(run.run_id)}</code></td>
                    <td>${escapeHtml(run.run_started_at || "—")}</td>
                    <td><span class="badge ${badgeClass(run.run_status)}">${escapeHtml(run.run_status || "unknown")}</span></td>
                    <td><span class="badge ${badgeClass(run.report_status)}">${escapeHtml(run.report_status || "unavailable")}</span></td>
                    <td><span class="badge ${badgeClass(run.delivery_status)}">${escapeHtml(deliveryLabel(run.delivery_status))}</span></td>
                    <td>${escapeHtml(run.preset_id || "custom")}</td>
                  </tr>`).join("")}
                </tbody>
              </table></div>`
            : `<p class="muted">No runs recorded yet. Completed and failed sessions appear here from durable history.</p>`}
        </section>
        <section class="panel">
          <p class="eyebrow">Run detail</p>
          <h2>Selected run</h2>
          <div id="runDetail"></div>
        </section>
      `;
      $("refreshHistoryBtn").onclick = () => loadHistory();
      document.querySelectorAll("#historyTable tr[data-run-id]").forEach(row => {
        row.onclick = () => selectRun(row.dataset.runId);
      });
      if (state.selectedRunId) {
        const stillThere = rows.some(run => run.run_id === state.selectedRunId);
        if (!stillThere) state.selectedRunId = null;
      }
      renderRunDetail();
    }
    async function selectRun(runId) {
      state.selectedRunId = runId;
      try {
        state.selectedRun = await api(`/api/history/${encodeURIComponent(runId)}`);
      } catch (err) {
        state.selectedRun = null;
        setStatus(err.message, "bad");
      }
      renderRunDetail();
    }
    function renderRunDetail() {
      const container = $("runDetail");
      if (!container) return;
      if (!state.selectedRun) {
        container.innerHTML = `<p class="muted">Select a run above to inspect its metadata and open its OKF report.</p>`;
        return;
      }
      const run = state.selectedRun;
      const delivery = run.delivery || {};
      const acceptedRecipients = Array.isArray(delivery.accepted_recipients)
        ? delivery.accepted_recipients : [];
      const rejectedRecipients = Array.isArray(delivery.rejected_recipients)
        ? delivery.rejected_recipients : [];
      const deliveryMeta = [
        delivery.reason ? `reason: ${delivery.reason}` : "",
        delivery.phase ? `phase: ${delivery.phase}` : "",
        acceptedRecipients.length ? `accepted: ${acceptedRecipients.join(", ")}` : "",
        rejectedRecipients.length ? `rejected: ${rejectedRecipients.join(", ")}` : "",
        delivery.error_type ? `${delivery.error_type}: ${delivery.error_message}` : ""
      ].filter(Boolean).join(" · ");
      container.innerHTML = `
        <div class="form-grid">
          <div class="field"><span>Run id</span><code>${escapeHtml(run.run_id || "—")}</code></div>
          <div class="field"><span>Started</span><code>${escapeHtml(run.run_started_at || "—")}</code></div>
          <div class="field"><span>Completed</span><code>${escapeHtml(run.run_completed_at || "—")}</code></div>
          <div class="field"><span>Preset</span><code>${escapeHtml(run.preset_id || "custom")}</code></div>
          <div class="field"><span>Duration</span><code>${escapeHtml(run.duration_label || "—")}</code></div>
          <div class="field"><span>Model</span><code>${escapeHtml(run.model_name || run.model || "—")}</code></div>
          <div class="field"><span>Artifacts</span><code>${run.artifacts ? run.artifacts.length : 0} recorded</code></div>
          <div class="field"><span>OKF bundle</span><code>${escapeHtml(run.okf_path || "—")}</code></div>
        </div>
        ${renderBadges(run.run_status, run.report_status, run.delivery_status)}
        ${metadataWarnings(run.metadata_read_errors)}
        ${deliveryMeta ? `<p class="muted" style="margin:10px 0 0">${escapeHtml(deliveryMeta)}</p>` : ""}
        <div class="toolbar" style="margin-top:12px">
          <button id="openReportBtn" ${run.report_status === "available" ? "" : "disabled"}>Open report</button>
        </div>
        <pre id="selectedReportPane" class="review-report"></pre>
      `;
      const pane = $("selectedReportPane");
      if (run.report_status === "available") {
        pane.textContent = "Loading report…";
        openHistoricalReport(run.run_id)
          .then(text => { pane.textContent = text; })
          .catch(err => { pane.textContent = `Report unavailable: ${err.message}`; });
      } else if (run.report_status === "unavailable") {
        pane.textContent = "This run completed but its OKF report artifact is missing.";
      } else {
        pane.textContent = "No report was generated for this run.";
      }
    }
    async function openHistoricalReport(runId) {
      const res = await fetch(`/api/history/${encodeURIComponent(runId)}/report`);
      if (!res.ok) {
        let message = `Report unavailable (${res.status})`;
        try {
          const data = await res.json();
          if (data && data.error) message = data.error;
        } catch (_err) { /* non-JSON error body */ }
        throw new Error(message);
      }
      return res.text();
    }
    function refreshReviewData() {
      return Promise.all([loadReview(), loadHistory()]).then(() => {
        const review = state.latestReview || {};
        if (review.report_status === "available") {
          showTab("review");
        }
      }).catch(() => {});
    }
    // Daily Automation (ADR 0012 Slice C): the Schedule tab is a thin adapter
    // over GET/PUT/DELETE /api/schedule. launchd owns invocation; this tab
    // only renders durable state and lifecycle status. No browser timer ever
    // replaces launchd.
    function scheduleLaunchdBadge(status) {
      const cls = status === "loaded" ? "good" : status === "not_loaded" ? "warn" : "bad";
      return `<span class="badge ${cls}">launchd: ${escapeHtml(status || "unknown")}</span>`;
    }
    async function loadSchedule() {
      try {
        state.schedule = await api("/api/schedule");
        state.scheduleError = state.schedule.error || null;
      } catch (err) {
        state.schedule = null;
        state.scheduleError = err.message;
      }
      renderSchedule();
    }
    function schedulePresetOptions() {
      const presets = (state.schema && state.schema.presets && state.schema.presets.presets) || [];
      const selected = state.schedule ? state.schedule.preset_id || "" : "";
      return `<option value="">default settings</option>` + presets.map(preset =>
        `<option value="${escapeHtml(preset.id)}"${selected === preset.id ? " selected" : ""}>${escapeHtml(preset.name || preset.id)}</option>`
      ).join("");
    }
    function renderSchedule() {
      const s = state.schedule || {};
      const supported = s.supported !== false;
      const enabled = Boolean(s.enabled);
      const launchd = s.launchd_status || "unknown";
      const last = s.last_run || {};
      const error = state.scheduleError || s.error || "";
      const deliveryMode = s.delivery_mode || "owner";
      const timeValue = s.time || "";
      $("scheduleMount").innerHTML = `
        <div class="banner panel">
          <div class="banner-copy">
            <p class="eyebrow">Daily Automation</p>
            <h2>One daily personal run</h2>
            <p class="muted">Enables one daily Run Session in your local time zone. The UI does not need to stay open: macOS launchd starts <code>news schedule run</code>. Delivery defaults to owner only; additional recipients are an explicit opt-in.</p>
          </div>
          <div class="banner-actions">
            <button id="openReviewFromScheduleBtn">Open Report Review</button>
          </div>
        </div>
        ${error ? `<p class="bad" style="margin:0 0 8px">${escapeHtml(error)}</p>` : ""}
        ${!supported ? `<p class="bad" style="margin:0 0 8px">Daily automation requires macOS launchd; scheduling is unavailable on this platform.</p>` : ""}
        <div class="stack">
          <section class="panel">
            <p class="eyebrow">Schedule</p>
            <h2>Daily run settings</h2>
            <div class="form-grid">
              <label class="field"><span>Time (local)</span><input id="schedule_time" type="time" value="${escapeHtml(timeValue)}" ${supported ? "" : "disabled"}><code>HH:MM</code></label>
              <label class="field"><span>Run preset</span><select id="schedule_preset" ${supported ? "" : "disabled"}>${schedulePresetOptions()}</select><code>preset id</code></label>
              <label class="field"><span>Delivery</span>
                <select id="schedule_delivery_mode" ${supported ? "" : "disabled"}>
                  <option value="owner"${deliveryMode === "owner" ? " selected" : ""}>Owner only (default)</option>
                  <option value="disabled"${deliveryMode === "disabled" ? " selected" : ""}>No delivery</option>
                  <option value="recipients"${deliveryMode === "recipients" ? " selected" : ""}>Configured recipients</option>
                </select>
                <code>delivery mode</code>
              </label>
            </div>
            <div class="toolbar" style="margin-top:12px">
              <button id="enableScheduleBtn" class="primary" ${supported ? "" : "disabled"}>${enabled ? "Update schedule" : "Enable schedule"}</button>
              <button id="disableScheduleBtn" class="danger" ${enabled && supported ? "" : "disabled"}>Disable schedule</button>
            </div>
            <p class="muted" style="margin:10px 0 0">Exactly one daily schedule at one local time; no weekly recurrence or cron expressions. Credentials are never stored in schedule state or the launchd plist.</p>
          </section>
          <section class="panel">
            <p class="eyebrow">Status</p>
            <h2>Schedule lifecycle</h2>
            <div class="badge-row">
              <span class="badge ${enabled ? "good" : "warn"}">schedule: ${enabled ? "enabled" : "disabled"}</span>
              ${scheduleLaunchdBadge(launchd)}
              <span class="badge ${badgeClass(last.status)}">last run: ${escapeHtml(last.status || "never")}</span>
            </div>
            <div class="form-grid" style="margin-top:12px">
              <div class="field"><span>Next daily time</span><code>${escapeHtml(s.next_run_label || (s.time ? s.time + " (local time)" : "—"))}</code></div>
              <div class="field"><span>Schedule state</span><code>${escapeHtml(s.state_path || "—")}</code></div>
              <div class="field"><span>Last run id</span><code>${escapeHtml(last.run_id || "—")}</code></div>
              <div class="field"><span>Last run started</span><code>${escapeHtml(last.started_at || "—")}</code></div>
            </div>
            ${last.run_status || last.report_status || last.delivery_status ? renderBadges(last.run_status, last.report_status, last.delivery_status) : ""}
            ${last.error_message ? `<p class="bad" style="margin:10px 0 0">${escapeHtml(last.error_message)}</p>` : ""}
          </section>
        </div>
      `;
      $("enableScheduleBtn").onclick = () => saveSchedule().catch(err => setStatus(err.message, "bad"));
      $("disableScheduleBtn").onclick = () => disableSchedule().catch(err => setStatus(err.message, "bad"));
      $("openReviewFromScheduleBtn").onclick = () => { showTab("review"); refreshReviewData(); };
    }
    async function saveSchedule() {
      const time = value("schedule_time").trim();
      if (!time) throw new Error("Pick a daily time (HH:MM, 24-hour local time).");
      const body = {
        time,
        preset_id: value("schedule_preset"),
        delivery_mode: value("schedule_delivery_mode")
      };
      const saved = await api("/api/schedule", { method: "PUT", body: JSON.stringify(body) });
      state.schedule = saved;
      state.scheduleError = saved.error || null;
      renderSchedule();
      setStatus("Daily schedule saved.", "good");
    }
    async function disableSchedule() {
      const ok = await confirmAction("Disable the daily schedule? The launchd job is removed; existing reports and history are kept.");
      if (!ok) return;
      const saved = await api("/api/schedule", { method: "DELETE" });
      state.schedule = saved;
      state.scheduleError = saved.error || null;
      renderSchedule();
      setStatus("Daily schedule disabled.", "muted");
    }
    async function loadSources() {
      const data = await api("/api/sources");
      state.sources = data.sources || [];
      renderSources();
    }
    function renderSources() {
      const q = value("sourceSearch").toLowerCase();
      const rows = state.sources.filter(src => JSON.stringify(src).toLowerCase().includes(q));
      $("sourceTable").innerHTML = `<thead><tr><th>Key</th><th>Name</th><th>Tier</th><th>Language</th><th>Region</th><th>URL</th></tr></thead><tbody>` +
        rows.map(src => `<tr data-key="${src.key || ""}"><td>${src.key || ""}</td><td>${src.name || ""}</td><td>${src.tier || ""}</td><td>${src.language || ""}</td><td>${src.region || ""}</td><td>${src.url || ""}</td></tr>`).join("") +
        `</tbody>`;
      document.querySelectorAll("#sourceTable tr[data-key]").forEach(row => row.onclick = () => editSource(row.dataset.key));
    }
    function sourceInput(field, src) {
      if (["can_enrich_coverage","strict_source_match"].includes(field)) {
        return `<label>${field}<select id="source_${field}"><option value=""></option><option value="false" ${val === false ? "selected" : ""}>false</option><option value="true" ${val === true ? "selected" : ""}>true</option></select></label>`;
      }
      if (field === "tier") return `<label>${field}<select id="source_${field}"><option></option><option ${val === "core" ? "selected" : ""}>core</option><option ${val === "peripheral" ? "selected" : ""}>peripheral</option></select></label>`;
      if (field === "source_match_mode") return `<label>${field}<select id="source_${field}"><option></option><option ${val === "feed_label" ? "selected" : ""}>feed_label</option><option ${val === "wire_attribution" ? "selected" : ""}>wire_attribution</option></select></label>`;
      if (["nations","source_match_aliases","notes"].includes(field)) return `<label>${field}<textarea id="source_${field}">${val}</textarea></label>`;
      return `<label>${field}<input id="source_${field}" value="${String(val).replaceAll("&","&amp;").replaceAll('"',"&quot;")}"></label>`;
    }
    function editSource(key) {
      const src = state.sources.find(item => item.key === key) || {};
      $("sourceForm").innerHTML = sourceFields.map(field => sourceInput(field, src)).join("");
    }
    function collectSource() {
      const updates = {};
      sourceFields.forEach(field => {
        const el = $(`source_${field}`);
        if (!el) return;
        updates[field] = el.value;
      });
      return updates;
    }
    async function saveSource() {
      const updates = collectSource();
      const exists = state.sources.some(src => src.key === updates.key);
      await api("/api/sources", { method: exists ? "PATCH" : "POST", body: JSON.stringify({ key: updates.key, updates }) });
      await loadSources();
    }
    async function deleteSelectedSource() {
      const key = value("source_key");
      if (!key) return;
      await api(`/api/sources?key=${encodeURIComponent(key)}`, { method: "DELETE" });
      await loadSources();
      editSource("");
    }
    async function loadRecipients() {
      const data = await api("/api/recipients");
      state.recipients = data.recipients || [];
      renderRecipients();
    }
    function renderRecipients() {
      $("recipientTable").innerHTML = `<thead><tr><th>Email</th><th>Name</th><th>Paused</th></tr></thead><tbody>` +
        state.recipients.map(rec => `<tr data-email="${rec.email || ""}"><td>${rec.email || ""}</td><td>${rec.name || ""}</td><td>${rec.pause ? "true" : "false"}</td></tr>`).join("") +
        `</tbody>`;
      document.querySelectorAll("#recipientTable tr[data-email]").forEach(row => row.onclick = () => editRecipient(row.dataset.email));
    }
    function editRecipient(email) {
      const rec = state.recipients.find(item => item.email === email) || { email: "", name: "", pause: false };
      $("recipient_email").value = rec.email || "";
      $("recipient_name").value = rec.name || "";
      $("recipient_pause").value = rec.pause ? "true" : "false";
    }
    async function saveRecipient() {
      const updates = { email: value("recipient_email"), name: value("recipient_name"), pause: value("recipient_pause") };
      const exists = state.recipients.some(rec => rec.email === updates.email);
      await api("/api/recipients", { method: exists ? "PATCH" : "POST", body: JSON.stringify({ email: updates.email, updates }) });
      await loadRecipients();
    }
    async function deleteSelectedRecipient() {
      const email = value("recipient_email");
      if (!email) return;
      await api(`/api/recipients?email=${encodeURIComponent(email)}`, { method: "DELETE" });
      await loadRecipients();
      editRecipient("");
    }
    const PROMPT_TASK_LABELS = {
      article_summary: "Article Summarization",
      story_scale_screening: "Story Scale Screening",
      story_drafting: "Story Drafting",
      title_generation: "Title Generation",
      image_art_direction: "Image Art Direction"
    };
    // Mirrors PROMPT_TASK_OVERRIDE_ENV_VARS in news_pipeline/prompt_catalog.py
    // (drift-guard: tests/test_prompt_catalog.py::test_override_env_vars_cover_all_tasks).
    const PROMPT_OVERRIDE_ENVS = {
      article_summary: "NEWS_PROMPT_OVERRIDE_ARTICLE_SUMMARY",
      story_scale_screening: "NEWS_PROMPT_OVERRIDE_STORY_SCALE_SCREENING",
      story_drafting: "NEWS_PROMPT_OVERRIDE_STORY_DRAFTING",
      title_generation: "NEWS_PROMPT_OVERRIDE_TITLE_GENERATION",
      image_art_direction: "NEWS_PROMPT_OVERRIDE_IMAGE_ART_DIRECTION"
    };
    function selectedPromptProfile() {
      const profiles = (state.schema && state.schema.prompt_profiles) || [];
      const id = currentControlValue("NEWS_PROMPT_PROFILE") ||
        (state.schema && state.schema.runtime && state.schema.runtime.prompt_profile_id) || "balanced";
      return profiles.find(profile => profile.id === id) || null;
    }
    // Tracks which profile's defaults the override editors currently hold, so
    // livePromptOverrides() can distinguish a stale default from a real edit
    // when the profile select changes (the editors keep the previous profile's
    // text until renderPromptProfilePanel() re-renders them).
    let lastRenderedPromptProfileId = null;
    function livePromptOverrides() {
      // Snapshot only edits that differ from the currently selected profile's
      // text OR the previously rendered profile's defaults, so profile
      // switches keep real edits and drop stale defaults.
      const overrides = {};
      const profile = selectedPromptProfile();
      const prev = (lastRenderedPromptProfileId &&
        ((state.schema && state.schema.prompt_profiles) || []).find(p => p.id === lastRenderedPromptProfileId)) || null;
      document.querySelectorAll("[data-env^='NEWS_PROMPT_OVERRIDE_']").forEach(el => {
        const task = Object.keys(PROMPT_OVERRIDE_ENVS).find(key => PROMPT_OVERRIDE_ENVS[key] === el.dataset.env);
        if (!task) return;
        const value = el.value.trim();
        if (!value) return;
        const newText = ((profile && profile.prompts && profile.prompts[task]) || "").trim();
        const oldText = ((prev && prev.prompts && prev.prompts[task]) || "").trim();
        // A value equal to the previously rendered profile's text is a stale
        // default, not an edit; a value equal to the new profile's text needs
        // no override either.
        if (value === oldText || value === newText) return;
        overrides[task] = value;
      });
      return overrides;
    }
    function renderPromptProfilePanel() {
      const profile = selectedPromptProfile();
      const descriptionEl = $("promptProfileDescription");
      if (descriptionEl) descriptionEl.textContent = profile ? profile.description : "";
      const readoutsEl = $("promptProfileReadouts");
      if (!readoutsEl) return;
      const overrides = livePromptOverrides();
      // The editors still hold the previous render's text at this point, so
      // record the profile those defaults came from AFTER the diff above.
      lastRenderedPromptProfileId = profile ? profile.id : null;
      if (!profile) {
        readoutsEl.innerHTML = `<p class="muted">No prompt profile selected.</p>`;
        return;
      }
      readoutsEl.innerHTML = Object.entries(PROMPT_TASK_LABELS).map(([task, label]) => {
        const profileText = (profile.prompts && profile.prompts[task]) || "";
        const effective = overrides[task] || profileText;
        return `<label class="field"><span>${escapeHtml(label)}</span>
          <textarea data-env="${escapeHtml(PROMPT_OVERRIDE_ENVS[task])}" rows="4" spellcheck="false">${escapeHtml(effective)}</textarea>
          <code>${escapeHtml(PROMPT_OVERRIDE_ENVS[task])}</code>
          <button type="button" class="prompt-stage-restore" data-task="${escapeHtml(task)}">Restore default</button></label>`;
      }).join("");
      document.querySelectorAll("#promptProfileReadouts .prompt-stage-restore").forEach(btn => {
        btn.onclick = () => {
          const task = btn.dataset.task || "";
          const editor = document.querySelector(`[data-env="${PROMPT_OVERRIDE_ENVS[task]}"]`);
          if (editor) editor.value = (profile.prompts && profile.prompts[task]) || "";
        };
      });
    }
    function modelTaskLabels() {
      return (state.schema && state.schema.model_task_labels) || {};
    }
    function runtimeFitLabels() {
      return (state.schema && state.schema.runtime_fit_labels) || {};
    }
    // Runtime-fit status -> required NEWS_MODEL_BACKEND value for the
    // compatibility hint (mirrors model_catalog.py's RUNTIME_FIT_* verdicts;
    // any other status maps to no hint rather than a guessed backend).
    const RUNTIME_FIT_BACKENDS = {
      managed_mlx_lm: "mlx-lm",
      managed_mlx_vlm: "mlx-vlm",
      external_only: "external"
    };
    function modelCatalogEntries() {
      return (state.schema && state.schema.model_catalog) || [];
    }
    // Exact alias/reference lookup mirroring catalog_model_backend (issue
    // #92): unknown references return "" so callers keep their fallback.
    function catalogBackendForReference(reference) {
      const clean = (reference || "").trim();
      if (!clean) return "";
      const entry = modelCatalogEntries().find(entry => entry.alias === clean || entry.reference === clean);
      return (entry && entry.backend) || "";
    }
    function requiredBackendForSelectedModel() {
      const reference = String(currentControlValue("NEWS_MODEL") || "").trim();
      if (reference !== state.selectedModelReference) {
        state.selectedModelReference = reference;
        state.selectedModelRequiredBackend = catalogBackendForReference(reference);
      }
      return state.selectedModelRequiredBackend;
    }
    function useModelReference(reference, requiredBackend = "") {
      const sel = document.querySelector('[data-env="NEWS_MODEL"]');
      if (!sel) return;
      if (!Array.from(sel.options).some(option => option.value === reference)) {
        sel.insertAdjacentHTML("afterbegin", `<option value="${escapeHtml(reference)}">${escapeHtml(reference)}</option>`);
      }
      sel.value = reference;
      sel.dispatchEvent(new Event("change"));
      // The delegated change listener derives the required backend from the
      // catalog; an explicit requiredBackend (HF runtime-fit status) wins for
      // search results that are not catalog entries. Store it after dispatch
      // because the synthetic change event intentionally handles catalog-only
      // direct selections as well.
      state.selectedModelReference = String(reference || "").trim();
      state.selectedModelRequiredBackend = String(
        requiredBackend || catalogBackendForReference(reference) || ""
      ).trim();
      renderModelBackendHint(requiredBackendForSelectedModel());
    }
    // Live compatibility hint under the Default model control: compares the
    // selected model's known backend with the explicit NEWS_MODEL_BACKEND
    // override (unsaved Advanced Settings edits included via
    // currentControlValue). Never mutates the backend control; an empty
    // override means config.py infers the backend from the model.
    function renderModelBackendHint(requiredBackend = "") {
      const hint = document.getElementById("modelBackendHint");
      if (!hint) return;
      const required = String(requiredBackend || "").trim();
      const current = String(currentControlValue("NEWS_MODEL_BACKEND") || "").trim();
      if (!required || !current) {
        // Unknown model/status or unset override: no conflict to report.
        hint.textContent = "";
        hint.classList.add("hidden");
        return;
      }
      if (required === current) {
        hint.textContent = "";
        hint.classList.add("hidden");
        return;
      }
      hint.textContent = `This model needs NEWS_MODEL_BACKEND=${required}`;
      hint.classList.remove("hidden");
    }
    function renderModelCatalogPanel() {
      const select = $("recommendationTask");
      if (!select) return;
      const tasks = (state.schema && state.schema.model_recommendation_tasks) || [];
      const labels = modelTaskLabels();
      select.innerHTML = `<option value="">Pick a task…</option>` + tasks.map(task =>
        `<option value="${escapeHtml(task)}">${escapeHtml(labels[task] || task)}</option>`
      ).join("");
      const cards = $("catalogCards");
      cards.innerHTML = modelCatalogEntries().map(entry => `
        <div class="knob">
          <label>${escapeHtml(entry.name)} <code>${escapeHtml(entry.alias)}</code></label>
          <p class="muted">${escapeHtml(entry.description)}</p>
          <p class="muted">Backend: ${escapeHtml(entry.backend)} · Context: ${entry.context_length != null ? escapeHtml(String(entry.context_length)) : "n/a"} · <a href="${escapeHtml(entry.hf_url)}" target="_blank" rel="noopener">Hugging Face page</a></p>
          <div class="toolbar"><button data-use-model="${escapeHtml(entry.alias)}">Set as default model</button></div>
        </div>
      `).join("");
      cards.querySelectorAll("[data-use-model]").forEach(btn => {
        btn.onclick = () => useModelReference(btn.dataset.useModel, catalogBackendForReference(btn.dataset.useModel));
      });
      renderRecommendations(select.value);
    }
    function renderRecommendations(task) {
      const container = $("recommendationReadout");
      if (!container) return;
      if (!task) {
        container.innerHTML = `<p class="muted">Pick a task to see curated recommendations.</p>`;
        return;
      }
      const picks = (state.schema && state.schema.model_recommendations && state.schema.model_recommendations[task]) || [];
      if (!picks.length) {
        container.innerHTML = `<p class="muted">No verified curated model for this task yet — search below for a candidate.</p>`;
        return;
      }
      container.innerHTML = picks.map(pick => `
        <div class="knob">
          <label>${escapeHtml(pick.name)} <code>${escapeHtml(pick.alias)}</code></label>
          <p class="muted">${escapeHtml(pick.reason)}</p>
          <div class="toolbar"><button data-use-model="${escapeHtml(pick.alias)}">Use</button></div>
        </div>
      `).join("");
      container.querySelectorAll("[data-use-model]").forEach(btn => {
        btn.onclick = () => useModelReference(btn.dataset.useModel, catalogBackendForReference(btn.dataset.useModel));
      });
    }
    function refreshHuggingFaceUseButtons() {
      const backendExternal = currentControlValue("NEWS_MODEL_BACKEND") === "external";
      document.querySelectorAll("[data-use-hf-model]").forEach(btn => {
        const disabled = btn.dataset.useHfBackend === "external" && !backendExternal;
        btn.disabled = disabled;
        btn.textContent = disabled
          ? "External only — set NEWS_MODEL_BACKEND=external to use"
          : "Use";
      });
    }
    async function searchHuggingFaceModels() {
      const container = $("modelSearchResults");
      if (!container) return;
      const query = value("modelSearchQuery").trim();
      if (!query) {
        container.innerHTML = `<p class="muted">Enter a query to search Hugging Face.</p>`;
        return;
      }
      const pipeline = value("modelSearchPipeline");
      const limit = parseInt(value("modelSearchLimit") || "10", 10) || 10;
      container.innerHTML = `<p class="muted">Searching…</p>`;
      try {
        const data = await api(`/api/models/search?q=${encodeURIComponent(query)}&pipeline_tag=${encodeURIComponent(pipeline)}&limit=${limit}`);
        if (data.error) {
          container.innerHTML = `<p class="muted">${escapeHtml(data.error)}</p>`;
          return;
        }
        const models = data.models || [];
        if (!models.length) {
          container.innerHTML = `<p class="muted">No models found.</p>`;
          return;
        }
        const backendExternal = currentControlValue("NEWS_MODEL_BACKEND") === "external";
        container.innerHTML = models.map(item => {
          const fit = item.runtime_fit || {};
          const fitLabel = runtimeFitLabels()[fit.status] || fit.status || "unknown";
          const hfBackend = Object.prototype.hasOwnProperty.call(RUNTIME_FIT_BACKENDS, fit.status)
            ? RUNTIME_FIT_BACKENDS[fit.status]
            : "";
          const externalOnly = fit.status === "external_only";
          const useDisabled = externalOnly && !backendExternal;
          return `
            <div class="knob">
              <label><a href="${escapeHtml(item.hf_url)}" target="_blank" rel="noopener">${escapeHtml(item.id)}</a></label>
              <p class="muted">${escapeHtml(item.pipeline_tag || "-")} · ${escapeHtml(item.library_name || "-")} · downloads ${escapeHtml(String(item.downloads ?? "-"))} · likes ${escapeHtml(String(item.likes ?? "-"))} · context ${item.context_length != null ? escapeHtml(String(item.context_length)) : "-"}</p>
              <p class="muted">Fit: ${escapeHtml(fitLabel)} — ${escapeHtml(fit.reason || "")}</p>
              <div class="toolbar">
                <button data-use-hf-model="${escapeHtml(item.id)}" data-use-hf-backend="${escapeHtml(hfBackend)}" ${useDisabled ? "disabled" : ""}>${useDisabled ? "External only — set NEWS_MODEL_BACKEND=external to use" : "Use"}</button>
              </div>
            </div>
          `;
        }).join("");
        container.querySelectorAll("[data-use-hf-model]").forEach(btn => {
          btn.onclick = () => useModelReference(btn.dataset.useHfModel, btn.dataset.useHfBackend || "");
        });
      } catch (err) {
        container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
      }
    }
    async function comparePromptProfiles() {
      const selectedId = currentControlValue("NEWS_PROMPT_PROFILE") || "balanced";
      const data = await api(`/api/prompt-profiles/compare?profile=${encodeURIComponent(selectedId)}`);
      const container = $("promptProfileCompare");
      if (!container) return;
      const diffs = data.diffs || {};
      const entries = Object.entries(diffs);
      container.innerHTML = entries.length
        ? entries.map(([task, diff]) => `<div class="knob"><code>${escapeHtml(task)}</code><pre>${escapeHtml(diff)}</pre></div>`).join("")
        : `<p class="muted">No differences from balanced.</p>`;
    }
    function wireEvents() {
      $("previewBtn").onclick = () => preview("run").catch(err => setStatus(err.message, "bad"));
      $("runBtn").onclick = () => runAction("run").catch(err => setStatus(err.message, "bad"));
      $("utilityPreviewBtn").onclick = () => preview(value("actionSelect") || "run").catch(err => setStatus(err.message, "bad"));
      $("utilityRunBtn").onclick = () => runAction(value("actionSelect") || "run").catch(err => setStatus(err.message, "bad"));
      $("stopBtn").onclick = () => {
        const runId = state.activeRun;
        if (!runId) return;
        api(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST", body: "{}" })
          .then(() => setStatus("Stop requested; waiting for the run to finish…", "warn"))
          .catch(err => setStatus(`Stop failed: ${err.message}`, "bad"));
      };
      $("openRunPresetDrawerBtn").onclick = () => { renderRunPresetDrawer(); openRunPresetDialog(); };
      $("savePresetBtn").onclick = () => { prepRunPresetEditorFromCurrent(); renderRunPresetDrawer(); openRunPresetDialog(); };
      $("closeRunPresetDialogBtn").onclick = closeRunPresetDialog;
      $("newPresetBtn").onclick = () => { editRunPreset(""); $("preset_env").value = envToText(collectEnv()); };
      $("reloadPresetsBtn").onclick = loadPresets;
      $("applyPresetBtn").onclick = () => {
        const id = value("preset_id").trim();
        const preset = state.presets.find(item => item.id === id);
        if (!preset) return;
        applyRunPreset(preset);
        closeRunPresetDialog();
      };
      $("renamePresetBtn").onclick = () => renamePresetDisplayName().catch(err => setStatus(err.message, "bad"));
      $("savePresetEditorBtn").onclick = () => savePresetEditor().catch(err => setStatus(err.message, "bad"));
      $("deletePresetBtn").onclick = () => deleteSelectedPreset().catch(err => setStatus(err.message, "bad"));
      $("knobSearch").oninput = renderAdvancedKnobs;
      $("clearKnobsBtn").onclick = resetAllOverrides;
      $("resetDefaultsBtn").onclick = resetAllOverrides;
      $("promptProfileSelect").onchange = () => {
        renderPromptProfilePanel();
        previewQuietly("run");
      };
      $("restorePromptProfileBtn").onclick = () => {
        const el = document.querySelector('[data-env="NEWS_PROMPT_PROFILE"]');
        if (el) el.value = "";
        document.querySelectorAll("[data-env^='NEWS_PROMPT_OVERRIDE_']").forEach(editor => { editor.value = ""; });
        renderPromptProfilePanel();
        previewQuietly("run");
      };
      $("comparePromptProfileBtn").onclick = () => comparePromptProfiles().catch(err => setStatus(err.message, "bad"));
      $("sourceSearch").oninput = renderSources;
      $("reloadSourcesBtn").onclick = loadSources;
      $("newSourceBtn").onclick = () => editSource("");
      $("saveSourceBtn").onclick = () => saveSource().catch(err => setStatus(err.message, "bad"));
      $("deleteSourceBtn").onclick = () => deleteSelectedSource().catch(err => setStatus(err.message, "bad"));
      $("reloadRecipientsBtn").onclick = loadRecipients;
      $("newRecipientBtn").onclick = () => editRecipient("");
      $("saveRecipientBtn").onclick = () => saveRecipient().catch(err => setStatus(err.message, "bad"));
      $("deleteRecipientBtn").onclick = () => deleteSelectedRecipient().catch(err => setStatus(err.message, "bad"));
      $("newModelTuningPresetBtn").onclick = () => { editModelTuningPreset(""); $("modelTuningPresetTuning").value = "{}"; };
      $("reloadModelTuningPresetsBtn").onclick = () => reloadModelTuningPresets().catch(err => $("modelTuningPresetError").textContent = err.message);
      $("saveModelTuningPresetBtn").onclick = () => saveModelTuningEditor().catch(err => $("modelTuningPresetError").textContent = err.message);
      $("deleteModelTuningPresetBtn").onclick = () => deleteModelTuningEditor().catch(err => $("modelTuningPresetError").textContent = err.message);
      $("actionSelect").onchange = () => $("sourceOptions").classList.toggle("hidden", !["check-sources","prune-sources","source-languages"].includes(value("actionSelect")));
      Object.values(TASK_CONFIG).forEach(meta => {
        const modelSelect = $(meta.modelSelectId);
        if (modelSelect) modelSelect.onchange = () => {
          renderModelTuningControls(meta.runtimeKey);
          previewQuietly("run");
        };
        const tuningSelect = $(meta.presetSelectId);
        if (tuningSelect) tuningSelect.onchange = () => {
          const preset = state.modelTuningPresets.find(item => item.id === tuningSelect.value) || null;
          if (preset) loadModelTuningEditor(meta.runtimeKey, preset);
          previewQuietly("run");
        };
        $(meta.saveButtonId).onclick = () => saveModelTuningPreset(meta.runtimeKey).catch(err => setStatus(err.message, "bad"));
        $(meta.renameButtonId).onclick = () => renameModelTuningPreset(meta.runtimeKey).catch(err => setStatus(err.message, "bad"));
        $(meta.deleteButtonId).onclick = () => deleteModelTuningPreset(meta.runtimeKey).catch(err => setStatus(err.message, "bad"));
      });
    }
    function applySelectedPresetFromState() {
      const preset = selectedRunPreset();
      if (!preset) return;
      applyRunPreset(preset);
      renderRunPresetDrawer();
    }
    async function init() {
      state.schema = await api("/api/schema");
      let bootWarning = "";
      let activeRunStatus = "";
      try {
        const runsData = await api("/api/runs");
        const active = (runsData.runs || []).find(run => ["starting", "running", "stopping"].includes(run.status));
        if (active) {
          state.activeRun = active.run_id;
          activeRunStatus = `Run ${active.run_id} is active (${active.status}).`;
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        bootWarning = `Active run status unavailable: ${message}`;
      }
      updateRunControls();
      renderTabs();
      renderRunSetup();
      renderAdvancedPanels();
      renderAdvancedKnobs();
      renderStats();
      renderRunPresetDrawer();
      renderPresetSummary();
      renderModelTuningPanels();
      wireEvents();
      document.addEventListener("change", (event) => {
        const el = event.target;
        if (el && el.matches && el.matches("select[data-env]")) {
          // Only selects with a links container render links; unrelated
          // selects (e.g. the backend knob) must not warn. Model/backend
          // changes additionally refresh the compatibility hint below.
          if (document.querySelector(`[data-links-for="${el.dataset.env}"]`)) {
            renderKnobLinks(el.dataset.env);
          }
          if (el.dataset.env === "NEWS_MODEL") {
            state.selectedModelReference = String(el.value || "").trim();
            state.selectedModelRequiredBackend = catalogBackendForReference(el.value);
            renderModelBackendHint(requiredBackendForSelectedModel());
          } else if (el.dataset.env === "NEWS_MODEL_BACKEND") {
            refreshHuggingFaceUseButtons();
            renderModelBackendHint(requiredBackendForSelectedModel());
          }
        }
      });
      await loadSources();
      await loadRecipients();
      await loadReview();
      await loadHistory();
      await loadSchedule();
      state.presets = (state.schema.presets && state.schema.presets.presets) || [];
      state.modelTuningPresets = (state.schema.model_tuning_presets && state.schema.model_tuning_presets.presets) || [];
      renderModelTuningEditor();
      if (state.schema.runtime && state.schema.runtime.preset_id && state.schema.runtime.preset_id !== "custom") {
        state.selectedRunPresetId = state.schema.runtime.preset_id;
        applySelectedPresetFromState();
      }
      refreshModelKnobLinks();
      renderRunPresetDrawer();
      renderPresetSummary();
      if (bootWarning) {
        setStatus(bootWarning, "warn");
      } else if (activeRunStatus) {
        setStatus(activeRunStatus, "warn");
      } else if (state.schema.removed_topic_env_vars && state.schema.removed_topic_env_vars.length) {
        setStatus(`Removed topic env vars set: ${state.schema.removed_topic_env_vars.join(", ")}`, "warn");
      } else {
        setStatus("");
      }
      $("sourceOptions").classList.add("hidden");
      $("actionSelect").onchange();
      await preview("run").catch(err => setStatus(err.message, "bad"));
    }
    init().catch(err => setStatus(err.message, "bad"));
  </script>
</body>
</html>
"""


def serve_ui(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, open_browser: bool = False) -> int:
    server = NewsUIServer((host, port), NewsUIHandler)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}"
    print(f"News control panel: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping news control panel.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="news ui",
        description="Serve the local news control panel.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)
    return serve_ui(args.host, args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
