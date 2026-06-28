"""Source catalog YAML loading and edits."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


SOURCE_FIELD_RE = re.compile(r"^    ([A-Za-z_][A-Za-z0-9_-]*):")
SOURCE_FIELD_ORDER = (
    "key",
    "name",
    "language",
    "tier",
    "region",
    "nations",
    "url",
    "homepage",
    "provider_type",
    "intended_role",
    "weight",
    "can_enrich_coverage",
    "strict_source_match",
    "source_match_mode",
    "requires_translation",
    "translation_source_language",
    "source_match_aliases",
    "notes",
)
SOURCE_LIST_FIELDS = {"nations", "source_match_aliases"}
SOURCE_BOOL_FIELDS = {
    "can_enrich_coverage",
    "strict_source_match",
    "requires_translation",
}
SOURCE_FLOAT_FIELDS = {"weight"}


@dataclass(frozen=True)
class UpsertSource:
    key: str
    updates: Mapping[str, Any]
    append_only: bool = False


@dataclass(frozen=True)
class DeleteSources:
    keys: set[str]


@dataclass(frozen=True)
class MarkTranslationRequired:
    source_languages: Mapping[str, str | None]


SourceCatalogEdit = (
    UpsertSource
    | DeleteSources
    | MarkTranslationRequired
)


@dataclass(frozen=True)
class SourceCatalogPatchResult:
    path: str
    edit_count: int
    records: list[dict[str, Any]]


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def load_source_records(path: Path) -> list[dict[str, Any]]:
    payload = _load_yaml_mapping(path)
    records = payload.get("sources", [])
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def load_source_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in load_source_records(path):
        key = str(record.get("key") or record.get("name") or "").strip()
        url = str(record.get("url") or "").strip()
        if not key or not url:
            continue
        rows.append(
            {
                "section": "sources",
                "key": key,
                "name": str(record.get("name") or key),
                "url": url,
                "fetcher": str(record.get("fetcher") or "rss").strip().lower(),
                "language": str(record.get("language") or "").strip(),
            }
        )
    return rows


def _source_record_for_key(records: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    clean_key = key.strip()
    if not clean_key:
        return None
    return next(
        (
            record
            for record in records
            if str(record.get("key") or "").strip() == clean_key
        ),
        None,
    )


def _source_block_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    in_sources = False
    start: int | None = None
    for index, line in enumerate(lines):
        if not in_sources:
            if line.startswith("sources:"):
                in_sources = True
            continue
        if line.startswith("  - "):
            if start is not None:
                ranges.append((start, index))
            start = index
            continue
        if start is not None and line.strip() and not line.startswith((" ", "#")):
            ranges.append((start, index))
            start = None
            break
    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def _source_block_key(lines: list[str], start: int, end: int) -> str:
    try:
        payload = yaml.safe_load("sources:\n" + "".join(lines[start:end])) or {}
    except yaml.YAMLError:
        return ""
    records = payload.get("sources", []) if isinstance(payload, dict) else []
    if not records or not isinstance(records[0], dict):
        return ""
    return str(records[0].get("key") or records[0].get("name") or "").strip()


def _direct_source_field_line(lines: list[str], start: int, end: int, field: str) -> int | None:
    for index in range(start, end):
        match = SOURCE_FIELD_RE.match(lines[index])
        if match and match.group(1) == field:
            return index
    return None


def _preferred_field_insert_line(
    lines: list[str],
    start: int,
    end: int,
    fields: tuple[str, ...],
) -> int:
    for field in fields:
        field_line = _direct_source_field_line(lines, start, end, field)
        if field_line is not None:
            return field_line + 1
    return start + 1


def _newline_for(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
    return "\n"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _ordered_source_record(record: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for field in SOURCE_FIELD_ORDER:
        if field not in record:
            continue
        value = record.get(field)
        if field not in {"key", "name", "url"} and value in (None, "", []):
            continue
        ordered[field] = value
    for field, value in record.items():
        if field not in ordered and field not in SOURCE_FIELD_ORDER:
            ordered[field] = value
    return ordered


def _render_source_block(record: dict[str, Any], newline: str) -> list[str]:
    lines: list[str] = []
    for index, (field, value) in enumerate(_ordered_source_record(record).items()):
        prefix = "  - " if index == 0 else "    "
        if isinstance(value, list):
            lines.append(f"{prefix}{field}:{newline}")
            for item in value:
                lines.append(f"      - {_yaml_scalar(item)}{newline}")
        else:
            lines.append(f"{prefix}{field}: {_yaml_scalar(value)}{newline}")
    return lines


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_source_value(field: str, value: Any) -> Any:
    if field in SOURCE_BOOL_FIELDS:
        return _coerce_bool(value)
    if field in SOURCE_FLOAT_FIELDS:
        if value in (None, ""):
            return None
        return float(value)
    if field in SOURCE_LIST_FIELDS:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).replace(",", "\n").splitlines() if item.strip()]
    if value is None:
        return None
    return str(value).strip()


def _find_source_range(lines: list[str], key: str) -> tuple[int, int] | None:
    clean_key = key.strip()
    for start, end in _source_block_ranges(lines):
        if _source_block_key(lines, start, end) == clean_key:
            return start, end
    return None


def _apply_upsert(
    lines: list[str],
    records: list[dict[str, Any]],
    edit: UpsertSource,
) -> tuple[list[str], list[dict[str, Any]], int]:
    key = edit.key.strip()
    if not key:
        raise ValueError("Source key is required.")
    existing = _source_record_for_key(records, key)
    if edit.append_only and existing:
        raise ValueError(f"Source {key!r} already exists.")
    record = existing or {"key": key, "name": key, "language": "en", "tier": "peripheral", "url": ""}
    fields_to_apply = {
        field: value
        for field, value in dict(edit.updates or {}).items()
        if field != "updates"
    }
    for field, value in fields_to_apply.items():
        if field not in SOURCE_FIELD_ORDER and field not in record:
            raise ValueError(f"Unsupported source field {field!r}.")
        coerced = _coerce_source_value(field, value)
        if field in {"key", "name", "url"} or coerced not in (None, "", []):
            record[field] = coerced
        else:
            record.pop(field, None)

    newline = _newline_for(lines)
    block = _render_source_block(record, newline)
    source_range = None if edit.append_only else _find_source_range(lines, key)
    if source_range:
        start, end = source_range
        lines[start:end] = block
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        if not any(line.startswith("sources:") for line in lines):
            lines.append(f"sources:{newline}")
        lines.extend(block)
    if existing is None:
        records.append(record)
    return lines, records, 1


def _apply_delete(
    lines: list[str],
    records: list[dict[str, Any]],
    edit: DeleteSources,
) -> tuple[list[str], list[dict[str, Any]], int]:
    keys = {str(key).strip() for key in edit.keys if str(key).strip()}
    if not keys:
        return lines, records, 0
    ranges_to_remove: list[tuple[int, int]] = []
    for start, end in _source_block_ranges(lines):
        if _source_block_key(lines, start, end) in keys:
            ranges_to_remove.append((start, end))
    for start, end in reversed(ranges_to_remove):
        del lines[start:end]
    records[:] = [
        record
        for record in records
        if str(record.get("key") or "").strip() not in keys
    ]
    return lines, records, len(ranges_to_remove)


def _apply_translation(
    lines: list[str],
    records: list[dict[str, Any]],
    edit: MarkTranslationRequired,
) -> tuple[list[str], list[dict[str, Any]], int]:
    updates = {
        str(key).strip(): str(language or "").strip().lower()
        for key, language in edit.source_languages.items()
        if str(key).strip()
    }
    if not updates:
        return lines, records, 0
    newline = _newline_for(lines)
    edits: list[tuple[str, int, str]] = []
    for start, end in _source_block_ranges(lines):
        key = _source_block_key(lines, start, end)
        if key not in updates:
            continue
        language = updates[key]
        requires_line = _direct_source_field_line(lines, start, end, "requires_translation")
        record = _source_record_for_key(records, key)
        if requires_line is not None:
            if lines[requires_line].strip().lower() != "requires_translation: true":
                edits.append(("replace", requires_line, f"    requires_translation: true{newline}"))
            if record is not None:
                record["requires_translation"] = True
        else:
            insert_at = _preferred_field_insert_line(lines, start, end, ("language", "url", "region", "name"))
            edits.append(("insert", insert_at, f"    requires_translation: true{newline}"))
            if record is not None:
                record["requires_translation"] = True

        if language:
            language_line = _direct_source_field_line(lines, start, end, "translation_source_language")
            if language_line is None:
                insert_at = _preferred_field_insert_line(
                    lines,
                    start,
                    end,
                    ("requires_translation", "language", "url", "region", "name"),
                )
                edits.append(("insert", insert_at, f"    translation_source_language: {language}{newline}"))
                if record is not None:
                    record["translation_source_language"] = language
    _apply_line_edits(lines, edits)
    return lines, records, len(edits)


def _apply_line_edits(lines: list[str], edits: list[tuple[str, int, str]]) -> None:
    for action, index, line in reversed(edits):
        if action == "replace":
            lines[index] = line
        else:
            lines.insert(index, line)


def load_source_records_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    try:
        payload = yaml.safe_load("".join(lines)) or {}
    except yaml.YAMLError:
        return []
    records = payload.get("sources", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def apply_source_catalog_patch(path: Path, edits: Iterable[SourceCatalogEdit]) -> SourceCatalogPatchResult:
    edit_list = list(edits)
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines(keepends=True)
    else:
        lines = []
    records = load_source_records_from_lines(lines)
    edit_count = 0
    for edit in edit_list:
        if isinstance(edit, UpsertSource):
            lines, records, count = _apply_upsert(lines, records, edit)
        elif isinstance(edit, DeleteSources):
            lines, records, count = _apply_delete(lines, records, edit)
        elif isinstance(edit, MarkTranslationRequired):
            lines, records, count = _apply_translation(lines, records, edit)
        else:
            raise TypeError(f"Unsupported source catalog edit: {edit!r}")
        edit_count += count

    if edit_count:
        content = "".join(lines)
        if content and not content.endswith("\n"):
            content += _newline_for(lines)
        path.write_text(content, encoding="utf-8")
    return SourceCatalogPatchResult(
        path=str(path),
        edit_count=edit_count,
        records=records,
    )
