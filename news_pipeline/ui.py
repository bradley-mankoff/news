"""Local browser control panel for the daily news pipeline."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import subprocess
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from .config import (
    CONFIG_DIR,
    REMOVED_TOPIC_ENV_VARS,
    ROOT_DIR,
    RUN_PRESETS_PATH,
    SOURCE_SCOPES,
    RECIPIENT_SCOPES,
    VALID_SOURCE_MATCH_MODES,
    configured_removed_topic_env_vars,
    load_run_presets,
    load_recipients,
    load_sources,
    normalize_preset_id,
    run_preset_env,
    resolve_runtime_config,
    RuntimeConfigRequest,
    runtime_knob_registry,
)
from .source_catalog import (
    DeleteSources,
    UpsertSource,
    apply_source_catalog_patch,
    load_source_records,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
RECIPIENT_HEADER = """# Email recipients for generated reports.
#
# Usage:
# - pause: true keeps the recipient configured but skips delivery.
# - NEWS_RECIPIENT_SCOPE=bradley sends only to bradley@mankoff.com,
#   regardless of this file.
"""
RUN_PRESET_HEADER = """# Saved run presets for the daily news pipeline.
#
# Run Presets are env-style defaults. Shell/UI overrides still win.
"""


def build_knob_registry() -> list[dict[str, Any]]:
    return runtime_knob_registry()


def _config_path_from_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


def _mask_secret(value: str | None) -> str:
    return "********" if value else ""


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
        return {
            "preset_id": config.preset_id or "custom",
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
                "assignments": _json_ready(config.model_assignments),
                "article_summary": _json_ready(config.model_assignments["article_summary"]),
                "story_drafting": _json_ready(config.model_assignments["story_drafting"]),
                "tuning": _json_ready(config.model_tuning),
                "pipeline_budget": _json_ready(config.pipeline_budget),
                "server_settings": _json_ready(config.model_server_settings),
            },
            "translation": {
                "enabled": config.translation_enabled,
                "reference": config.translation_model_reference,
                "name": config.translation_model_name,
                "backend": config.translation_model_backend,
                "base_url": config.translation_model_base_url,
                "target_language": config.translation_target_language,
                "server_command": config.translation_model_server_command,
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
                "bradley_recipient": config.bradley_recipient,
                "fallback_recipients": config.email_recipients_fallback,
                "email_from": config.email_from,
                "smtp_host": config.smtp_host,
                "smtp_port": config.smtp_port,
                "smtp_username": config.smtp_username,
                "smtp_use_ssl": config.smtp_use_ssl,
                "smtp_password_set": bool(config.smtp_password),
                "unsubscribe_base_url": config.unsubscribe_base_url,
                "unsubscribe_host": config.unsubscribe_host,
                "unsubscribe_port": config.unsubscribe_port,
                "unsubscribe_secret_set": bool(config.unsubscribe_secret),
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
        payload["presets"][preset_id] = {
            "name": record.get("name") or preset_id,
            "description": record.get("description") or "",
            "env": dict(record.get("env") or {}),
        }
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
    env = _coerce_preset_env(body.get("env"))
    if not env and isinstance(body.get("updates"), dict):
        env = _coerce_preset_env(body["updates"].get("env"))
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    record = {
        "id": preset_id,
        "name": str(updates.get("name") or existing.get("name") or preset_id).strip(),
        "description": str(updates.get("description") or existing.get("description") or "").strip(),
        "env": env or dict(existing.get("env") or {}),
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
    }
    _write_presets(records)
    return {"path": str(RUN_PRESETS_PATH), "preset": records[target_id]}


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
    rendered = " ".join(shlex.quote(part) for part in command)
    if env:
        rendered = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items())) + " " + rendered
    return {
        "command": command,
        "command_text": rendered,
        "env": env,
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
        self.lines: list[str] = []
        self.status = "starting"
        self.returncode: int | None = None
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    def append(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "run_id": self.run_id,
                "command": self.command,
                "env": {key: _mask_secret(value) if "PASSWORD" in key or "SECRET" in key else value for key, value in self.env.items()},
                "started_at": self.started_at,
                "status": self.status,
                "returncode": self.returncode,
                "line_count": len(self.lines),
            }


class RunManager:
    def __init__(self):
        self.runs: dict[str, RunRecord] = {}
        self.lock = threading.Lock()

    def start(self, body: dict[str, Any]) -> RunRecord:
        command, env = build_command(body)
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(run_id, command, env)
        with self.lock:
            self.runs[run_id] = record
        thread = threading.Thread(target=self._run_process, args=(record,), daemon=True)
        thread.start()
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
        process = record.process
        if process and process.poll() is None:
            process.terminate()
            record.append("[ui] terminate requested\n")
            record.status = "stopping"
        return record.snapshot()

    def _run_process(self, record: RunRecord) -> None:
        env = dict(os.environ)
        env.update(record.env)
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
            record.process = process
            record.status = "running"
            assert process.stdout is not None
            for line in process.stdout:
                record.append(line)
            record.returncode = process.wait()
            record.status = "completed" if record.returncode == 0 else "failed"
            record.append(f"[ui] process exited with code {record.returncode}\n")
        except Exception as exc:
            record.status = "failed"
            record.returncode = -1
            record.append(f"[ui] failed to start process: {exc}\n")


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
            elif parsed.path == "/api/sources":
                self._send_json(list_sources())
            elif parsed.path == "/api/recipients":
                self._send_json(list_recipients())
            elif parsed.path == "/api/runs":
                self._send_json({"runs": RUN_MANAGER.list()})
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
                record = RUN_MANAGER.start(body)
                self._send_json(record.snapshot(), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/presets":
                self._send_json(upsert_preset(body, append_only=True), status=HTTPStatus.CREATED)
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

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/sources":
                self._send_json(upsert_source(body))
            elif parsed.path == "/api/presets":
                self._send_json(upsert_preset(body))
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
            elif parsed.path == "/api/presets":
                self._send_json(delete_preset((params.get("id") or [""])[0]))
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
        index = 0
        while True:
            with record.lock:
                lines = record.lines[index:]
                index = len(record.lines)
                status = record.status
                done = status in {"completed", "failed"}
            try:
                for line in lines:
                    payload = json.dumps({"line": line, "status": status})
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
      --bg: #f7f8fa;
      --surface: #ffffff;
      --line: #d9dee7;
      --ink: #1f2430;
      --muted: #657083;
      --blue: #255f99;
      --green: #19735a;
      --gold: #9c6b16;
      --red: #b33a3a;
      --focus: #0f7a9f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 2;
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
      background: #fdfdfd;
    }
    button.primary { background: var(--blue); color: white; border-color: var(--blue); }
    button.danger { color: var(--red); border-color: #e5b4b4; }
    button:focus, input:focus, select:focus, textarea:focus { outline: 2px solid var(--focus); outline-offset: 1px; }
    input, select, textarea { width: 100%; min-height: 34px; padding: 6px 8px; }
    textarea { min-height: 76px; resize: vertical; }
    main { display: grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 63px); }
    nav {
      border-right: 1px solid var(--line);
      background: #eef1f5;
      padding: 12px;
    }
    nav button {
      width: 100%;
      text-align: left;
      margin-bottom: 6px;
      background: transparent;
      border-color: transparent;
    }
    nav button.active { background: #fff; border-color: var(--line); color: var(--blue); }
    section.view { display: none; padding: 18px; }
    section.view.active { display: block; }
    .grid { display: grid; gap: 12px; }
    .cols { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
    .stat { border-left: 4px solid var(--blue); padding: 8px 10px; background: #fff; border-radius: 6px; }
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
    #logPane { min-height: 280px; max-height: 52vh; }
    .knob-group { margin-bottom: 18px; }
    .knobs { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }
    .knob { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fff; }
    .knob label { display: block; font-weight: 600; margin-bottom: 4px; }
    .knob code { color: var(--muted); font-size: 12px; }
    .knob-details { margin-top: 12px; }
    .knob-details > summary { cursor: pointer; color: var(--muted); font-weight: 600; }
    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
    @media (max-width: 780px) {
      main { grid-template-columns: 1fr; }
      nav { display: flex; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      nav button { width: auto; white-space: nowrap; }
      .row { grid-template-columns: 1fr; gap: 4px; }
      header { align-items: flex-start; flex-direction: column; }
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
    <section id="dashboard" class="view active">
      <div class="grid cols">
        <div class="panel">
          <h2>Run</h2>
          <div class="row"><label for="presetSelect">Run preset</label><select id="presetSelect"></select></div>
          <div class="row"><label for="actionSelect">Action</label><select id="actionSelect"></select></div>
          <div id="sourceOptions">
            <h3>Source Tool Options</h3>
            <div class="form-grid">
              <label>Limit<input id="opt_limit" type="number" min="1"></label>
              <label>Recent days<input id="opt_recent_days" type="number" min="1" value="7"></label>
              <label>Timeout<input id="opt_timeout" type="number" min="1"></label>
              <label>Concurrency<input id="opt_concurrency" type="number" min="1"></label>
              <label>Section<select id="opt_section"><option value=""></option><option>sources</option><option>all</option></select></label>
              <label>Language model<input id="opt_language_model"></label>
              <label>Language samples<input id="opt_language_samples" type="number" min="1"></label>
              <label>Min language confidence<input id="opt_min_language_confidence" type="number" step="0.01" min="0" max="1"></label>
            </div>
            <div class="toolbar">
              <label><input id="opt_probe_articles" type="checkbox"> Probe articles</label>
              <label><input id="opt_prune_unscrapable" type="checkbox"> Prune unscrapable</label>
              <label><input id="opt_only_failures" type="checkbox"> Only failures</label>
              <label><input id="opt_write_languages" type="checkbox"> Write languages</label>
              <label><input id="opt_overwrite_languages" type="checkbox"> Overwrite languages</label>
              <label><input id="opt_json" type="checkbox"> JSON</label>
            </div>
          </div>
          <div class="toolbar">
            <button id="previewBtn">Preview</button>
            <button id="runBtn" class="primary">Run</button>
            <button id="refreshBtn">Refresh</button>
          </div>
        </div>
        <div class="panel">
          <h2>Effective Snapshot</h2>
          <div id="stats" class="stats"></div>
        </div>
      </div>
      <div class="panel" style="margin-top:12px">
        <h2>Command Preview</h2>
        <pre id="previewPane"></pre>
      </div>
      <div class="panel" style="margin-top:12px">
        <div class="toolbar"><h2 style="margin-right:auto">Run Log</h2><button id="stopBtn" class="danger">Stop</button></div>
        <pre id="logPane"></pre>
      </div>
    </section>
    <section id="knobs" class="view">
      <div class="toolbar">
        <input id="knobSearch" placeholder="Filter settings">
        <label><input id="showAdvanced" type="checkbox"> Advanced</label>
        <button id="savePresetBtn">Save run preset</button>
        <button id="loadPresetBtn">Load run preset</button>
        <button id="clearKnobsBtn">Clear overrides</button>
      </div>
      <div id="knobContainer"></div>
    </section>
    <section id="presets" class="view">
      <div class="toolbar">
        <button id="newPresetBtn">New preset</button>
        <button id="duplicatePresetBtn">Duplicate</button>
        <button id="reloadPresetsBtn">Reload</button>
      </div>
      <div class="grid cols">
        <div class="table-wrap"><table id="presetTable"></table></div>
        <div class="panel">
          <h2>Run Preset Editor</h2>
          <div class="form-grid">
            <label>ID<input id="preset_id"></label>
            <label>Name<input id="preset_name"></label>
          </div>
          <label>Description<textarea id="preset_description"></textarea></label>
          <label>Environment<textarea id="preset_env" spellcheck="false"></textarea></label>
          <div class="toolbar" style="margin-top:12px">
            <button id="savePresetEditorBtn" class="primary">Save run preset</button>
            <button id="deletePresetBtn" class="danger">Delete run preset</button>
            <button id="loadPresetIntoKnobsBtn">Load into settings</button>
          </div>
        </div>
      </div>
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
  </main>
  <script>
    const state = { schema: null, presets: [], sources: [], recipients: [], activeRun: null };
    const tabs = [
      ["dashboard", "Dashboard"],
      ["knobs", "Run Settings"],
      ["presets", "Run Presets"],
      ["sources", "Sources"],
      ["recipients", "Recipients"]
    ];
    const sourceFields = ["key","name","language","tier","region","nations","url","homepage","provider_type","intended_role","weight","can_enrich_coverage","strict_source_match","source_match_mode","requires_translation","translation_source_language","source_match_aliases","notes"];

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
    function collectEnv() {
      const env = {};
      document.querySelectorAll("[data-env]").forEach(input => {
        const name = input.dataset.env;
        let val = input.type === "checkbox" ? (input.checked ? "1" : "") : input.value.trim();
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
    function requestBody() {
      return { action: value("actionSelect"), preset: value("presetSelect"), env: collectEnv(), options: collectOptions() };
    }
    function renderTabs() {
      $("tabs").innerHTML = tabs.map(([id, label]) => `<button data-tab="${id}">${label}</button>`).join("");
      document.querySelectorAll("nav button").forEach(btn => btn.onclick = () => showTab(btn.dataset.tab));
      showTab("dashboard");
    }
    function renderSchema() {
      const schema = state.schema;
      $("actionSelect").innerHTML = schema.actions.map(action => `<option value="${action}">${action}</option>`).join("");
      state.presets = (schema.presets && schema.presets.presets) || [];
      renderPresetSelect();
      if (schema.runtime && schema.runtime.preset_id && schema.runtime.preset_id !== "custom") {
        $("presetSelect").value = schema.runtime.preset_id;
      }
      if (schema.removed_topic_env_vars.length) {
        setStatus(`Removed topic env vars set: ${schema.removed_topic_env_vars.join(", ")}`, "warn");
      } else {
        setStatus("Ready", "good");
      }
      renderStats();
      renderKnobs();
      applySelectedPresetToKnobs();
      renderPresets();
    }
    function renderPresetSelect() {
      const options = [`<option value="">Custom</option>`].concat(
        state.presets.map(preset => `<option value="${preset.id}">${preset.name || preset.id}</option>`)
      );
      $("presetSelect").innerHTML = options.join("");
    }
    function renderStats() {
      const s = state.schema;
      const runtime = s.runtime || {};
      const source = s.sources || {};
      const recipients = s.recipients || {};
      const model = runtime.model || {};
      const assignments = model.assignments || {};
      const articleSummary = model.article_summary || assignments.article_summary || {};
      const storyDrafting = model.story_drafting || assignments.story_drafting || {};
      const items = [
        ["Run preset", runtime.preset_id || "custom"],
        ["Source scope", runtime.source_scope || "-"],
        ["Sources", source.total ?? 0],
        ["Core selected", source.selected ? source.selected.core : "-"],
        ["Peripheral selected", source.selected ? source.selected.peripheral : "-"],
        ["Recipients", `${recipients.total ?? 0} total`],
        ["Recipient scope", runtime.recipient_scope || "-"],
        ["Model", model.reference || "-"],
        ["Article Summarization", articleSummary.reference || "-"],
        ["Story Drafting", storyDrafting.reference || "-"],
        ["Images", runtime.image && runtime.image.enabled ? "on" : "off"]
      ];
      $("stats").innerHTML = items.map(([label, val]) => `<div class="stat"><span class="muted">${escapeHtml(label)}</span><strong>${escapeHtml(String(val ?? ""))}</strong></div>`).join("");
    }
    function inputForKnob(knob) {
      const current = state.schema.current_env[knob.env] || "";
      if (knob.type === "select") {
        const options = current && !knob.options.includes(current) ? [current, ...knob.options] : knob.options;
        const opts = ["", ...options].map(opt => `<option value="${escapeHtml(opt)}" ${current === opt ? "selected" : ""}>${escapeHtml(opt)}</option>`).join("");
        return `<select data-env="${knob.env}">${opts}</select>`;
      }
      if (knob.type === "bool") {
        const normalized = String(current).toLowerCase();
        const truthy = ["1","true","yes","on"].includes(normalized);
        const falsey = ["0","false","no","off"].includes(normalized);
        return `<select data-env="${knob.env}">
          <option value="">inherit</option>
          <option value="1" ${truthy ? "selected" : ""}>on</option>
          <option value="0" ${falsey ? "selected" : ""}>off</option>
        </select>`;
      }
      const type = knob.type === "password" ? "password" : (knob.type === "number" ? "number" : "text");
      const min = knob.min !== null && knob.min !== undefined ? ` min="${knob.min}"` : "";
      const max = knob.max !== null && knob.max !== undefined ? ` max="${knob.max}"` : "";
      const step = knob.step !== null && knob.step !== undefined ? ` step="${knob.step}"` : "";
      return `<input data-env="${knob.env}" type="${type}" value="${current}" placeholder="${knob.default ?? ""}"${min}${max}${step}>`;
    }
    function renderKnobs() {
      const search = value("knobSearch").toLowerCase();
      const showAdv = checked("showAdvanced");
      const groups = {};
      state.schema.knobs.forEach(knob => {
        const hay = `${knob.label} ${knob.env} ${knob.group}`.toLowerCase();
        if (search && !hay.includes(search)) return;
        if (knob.advanced && !showAdv && knob.group !== "Model Tuning") return;
        (groups[knob.group] ||= []).push(knob);
      });
      const orderedGroups = ["Run Settings", "Model Selection", "Model Tuning", "Pipeline Budget", "Model Server Settings"];
      const renderKnobCards = list => list.map(knob => `
            <div class="knob">
              <label>${escapeHtml(knob.label)}</label>
              ${inputForKnob(knob)}
              <code>${escapeHtml(knob.env)}</code>
            </div>
          `).join("");
      $("knobContainer").innerHTML = [...orderedGroups, ...Object.keys(groups).filter(group => !orderedGroups.includes(group)).sort()].map(group => {
        const knobs = groups[group];
        if (!knobs || !knobs.length) return "";
        if (group === "Model Tuning") {
          const basicKnobs = knobs.filter(knob => !knob.advanced);
          const advancedKnobs = knobs.filter(knob => knob.advanced);
          const advancedOpen = showAdv || (search && advancedKnobs.some(knob => `${knob.label} ${knob.env}`.toLowerCase().includes(search)));
          return `
            <div class="knob-group">
              <h2>${escapeHtml(group)}</h2>
              ${basicKnobs.length ? `<div class="knobs">${renderKnobCards(basicKnobs)}</div>` : ""}
              ${advancedKnobs.length ? `
                <details class="knob-details"${advancedOpen ? " open" : ""}>
                  <summary>Advanced tuning</summary>
                  <div class="knobs">${renderKnobCards(advancedKnobs)}</div>
                </details>
              ` : ""}
            </div>
          `;
        }
        return `
          <div class="knob-group">
            <h2>${escapeHtml(group)}</h2>
            <div class="knobs">${renderKnobCards(knobs)}</div>
          </div>
        `;
      }).join("");
    }
    function escapeHtml(text) {
      return String(text ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
    }
    function currentPreset() {
      return state.presets.find(preset => preset.id === value("presetSelect")) || null;
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
    function setKnobEnv(env) {
      document.querySelectorAll("[data-env]").forEach(el => {
        const val = env[el.dataset.env] || "";
        el.value = val;
      });
    }
    function applySelectedPresetToKnobs() {
      const preset = currentPreset();
      setKnobEnv(preset ? preset.env || {} : {});
    }
    async function loadPresets() {
      const data = await api("/api/presets");
      state.presets = data.presets || [];
      renderPresetSelect();
      renderPresets();
    }
    function renderPresets() {
      const rows = state.presets || [];
      if (!$("presetTable")) return;
      $("presetTable").innerHTML = `<thead><tr><th>ID</th><th>Name</th><th>Description</th></tr></thead><tbody>` +
        rows.map(preset => `<tr data-id="${escapeHtml(preset.id || "")}"><td>${escapeHtml(preset.id || "")}</td><td>${escapeHtml(preset.name || "")}</td><td>${escapeHtml(preset.description || "")}</td></tr>`).join("") +
        `</tbody>`;
      document.querySelectorAll("#presetTable tr[data-id]").forEach(row => row.onclick = () => editPreset(row.dataset.id));
    }
    function editPreset(id) {
      const preset = state.presets.find(item => item.id === id) || { id: "", name: "", description: "", env: {} };
      $("preset_id").value = preset.id || "";
      $("preset_name").value = preset.name || "";
      $("preset_description").value = preset.description || "";
      $("preset_env").value = envToText(preset.env || {});
    }
    function collectPreset() {
      return {
        id: value("preset_id"),
        name: value("preset_name"),
        description: value("preset_description"),
        env: textToEnv(value("preset_env"))
      };
    }
    async function savePresetEditor() {
      const body = collectPreset();
      const exists = state.presets.some(preset => preset.id === body.id);
      await api("/api/presets", { method: exists ? "PATCH" : "POST", body: JSON.stringify(body) });
      await loadPresets();
      $("presetSelect").value = body.id;
    }
    async function deleteSelectedPreset() {
      const id = value("preset_id");
      if (!id) return;
      await api(`/api/presets?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadPresets();
      editPreset("");
    }
    async function duplicateSelectedPreset() {
      const source = value("preset_id") || value("presetSelect");
      if (!source) return;
      const target = `${source}-copy`;
      const data = await api("/api/presets/duplicate", { method: "POST", body: JSON.stringify({ source_id: source, target_id: target }) });
      await loadPresets();
      editPreset(data.preset.id);
    }
    async function preview() {
      const data = await api("/api/preview", { method: "POST", body: JSON.stringify(requestBody()) });
      $("previewPane").textContent = data.command_text + (data.runtime_error ? `\n\nPreview error: ${data.runtime_error}` : "");
      return data;
    }
    async function runAction() {
      const data = await api("/api/run", { method: "POST", body: JSON.stringify(requestBody()) });
      state.activeRun = data.run_id;
      $("logPane").textContent = "";
      const events = new EventSource(`/api/runs/${data.run_id}/events`);
      events.onmessage = event => {
        const payload = JSON.parse(event.data);
        $("logPane").textContent += payload.line;
        $("logPane").scrollTop = $("logPane").scrollHeight;
      };
      events.addEventListener("status", event => {
        const payload = JSON.parse(event.data);
        $("logPane").textContent += `\n[ui] ${payload.status}\n`;
        events.close();
      });
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
      const val = Array.isArray(src[field]) ? src[field].join("\n") : (src[field] ?? "");
      if (["can_enrich_coverage","strict_source_match","requires_translation"].includes(field)) {
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
    function wireEvents() {
      $("refreshBtn").onclick = init;
      $("previewBtn").onclick = () => preview().catch(err => setStatus(err.message, "bad"));
      $("runBtn").onclick = () => runAction().catch(err => setStatus(err.message, "bad"));
      $("stopBtn").onclick = () => state.activeRun && api(`/api/runs/${state.activeRun}/stop`, { method: "POST", body: "{}" });
      $("presetSelect").onchange = () => { applySelectedPresetToKnobs(); preview().catch(() => {}); };
      $("knobSearch").oninput = () => { renderKnobs(); applySelectedPresetToKnobs(); };
      $("showAdvanced").onchange = () => { renderKnobs(); applySelectedPresetToKnobs(); };
      $("clearKnobsBtn").onclick = () => { document.querySelectorAll("[data-env]").forEach(el => { if (el.type === "checkbox") el.checked = false; else el.value = ""; }); };
      $("savePresetBtn").onclick = () => {
        const preset = currentPreset() || { id: "", name: "", description: "" };
        $("preset_id").value = preset.id || "";
        $("preset_name").value = preset.name || preset.id || "";
        $("preset_description").value = preset.description || "";
        $("preset_env").value = envToText(collectEnv());
        showTab("presets");
      };
      $("loadPresetBtn").onclick = () => applySelectedPresetToKnobs();
      $("reloadPresetsBtn").onclick = loadPresets;
      $("newPresetBtn").onclick = () => editPreset("");
      $("duplicatePresetBtn").onclick = () => duplicateSelectedPreset().catch(err => setStatus(err.message, "bad"));
      $("savePresetEditorBtn").onclick = () => savePresetEditor().catch(err => setStatus(err.message, "bad"));
      $("deletePresetBtn").onclick = () => deleteSelectedPreset().catch(err => setStatus(err.message, "bad"));
      $("loadPresetIntoKnobsBtn").onclick = () => { $("presetSelect").value = value("preset_id"); applySelectedPresetToKnobs(); showTab("knobs"); };
      $("sourceSearch").oninput = renderSources;
      $("reloadSourcesBtn").onclick = loadSources;
      $("newSourceBtn").onclick = () => editSource("");
      $("saveSourceBtn").onclick = () => saveSource().catch(err => setStatus(err.message, "bad"));
      $("deleteSourceBtn").onclick = () => deleteSelectedSource().catch(err => setStatus(err.message, "bad"));
      $("reloadRecipientsBtn").onclick = loadRecipients;
      $("newRecipientBtn").onclick = () => editRecipient("");
      $("saveRecipientBtn").onclick = () => saveRecipient().catch(err => setStatus(err.message, "bad"));
      $("deleteRecipientBtn").onclick = () => deleteSelectedRecipient().catch(err => setStatus(err.message, "bad"));
      $("actionSelect").onchange = () => $("sourceOptions").classList.toggle("hidden", !["check-sources","prune-sources","source-languages"].includes(value("actionSelect")));
    }
    async function init() {
      state.schema = await api("/api/schema");
      renderSchema();
      await loadSources();
      await loadRecipients();
      $("sourceOptions").classList.add("hidden");
      await preview().catch(() => {});
    }
    renderTabs();
    wireEvents();
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
