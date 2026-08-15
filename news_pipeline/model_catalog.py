"""Model Catalog: code-owned registry of curated models and Hugging Face search.

The Model Catalog owns the code-reviewed baseline of models the pipeline's
backends can actually launch, with per-task recommendations (factual
extraction, structured output, synthesis, citation fidelity, speed, context
length, translation) instead of parameter count or popularity. An optional
user overlay can add advisory entries; every Hugging Face search result is
annotated with a runtime-fit verdict so the picker never presents a catalog
entry as project-verified unless it is code-owned (HANDOFF: "Model picker must
validate runtime support").

This module is deliberately stdlib-only at module level (``dataclasses``,
``logging``, ``os``, ``re``, ``pathlib``, ``typing``) so that
``config.py``/``cli.py``/``ui.py`` can import it without creating an import
cycle. ``huggingface_hub`` is imported lazily inside ``_hf_api()`` only
(mirrors the lazy ``sentence_transformers`` import in ``embeddings.py``), and
``yaml`` is imported lazily inside the catalog loader only. Built-ins live in
Python (not YAML) because they are code-reviewed contracts; an optional,
user-editable YAML overlay (``config/model_catalog.yaml``, issue #90) may
override their descriptive/recommendation metadata and add new HF-backed
entries, and is validated and merged into the same per-process catalog
snapshot. The drift-guard tying catalog aliases to ``config.MODEL_ALIASES``
lives in tests (``test_model_catalog.py``), like ``prompt_catalog``'s profile
drift guard.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .llama_cpp_adapter import parse_llama_cpp_model_reference

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
MODEL_CATALOG_PATH = ROOT_DIR / "config" / "model_catalog.yaml"
MODEL_CATALOG_YAML_ENV_VAR = "NEWS_MODEL_CATALOG_YAML"
# Closed set of backends a YAML entry may declare (drift-guard:
# test_model_catalog.py pins this to config.SUPPORTED_MODEL_BACKENDS).
CATALOG_MODEL_BACKENDS = ("mlx-lm", "mlx-vlm", "external", "llama.cpp")
# Aliases must be safe for environment/CLI use: lowercase letters, digits,
# ".", "_", and "-", beginning with a letter or digit.
_CATALOG_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Metadata-only fields an existing built-in alias may override in YAML.
# Fields a new YAML alias must provide (context_length/task_notes optional).
_CATALOG_REQUIRED_FIELDS = ("reference", "name", "backend", "hf_repo", "description")
_CATALOG_ALL_FIELDS = (
    "reference",
    "name",
    "backend",
    "hf_repo",
    "context_length",
    "description",
    "task_notes",
)

# Issue #30 task list, in order. Feeds the config knob options / UI selector.
# Keep in sync with the CATALOG_MODELS task_notes keys (drift-guard:
# test_model_catalog.py::test_catalog_entries_are_complete).
MODEL_RECOMMENDATION_TASKS = (
    "factual_extraction",
    "structured_output",
    "synthesis",
    "citation_fidelity",
    "speed",
    "context_length",
    "translation",
)

MODEL_TASK_LABELS: dict[str, str] = {
    "factual_extraction": "Factual extraction",
    "structured_output": "Structured output",
    "synthesis": "Synthesis",
    "citation_fidelity": "Citation fidelity",
    "speed": "Speed",
    "context_length": "Context length",
    "translation": "Translation",
}

MODEL_RECOMMENDATION_TASK_NOTES: dict[str, str] = {
    "factual_extraction": (
        "Prioritize the default Gemma 4 12B model: its 256K-token context "
        "keeps long source text intact for extraction."
    ),
    "structured_output": (
        "The default Gemma 4 12B model is the verified structured-output "
        "pick; keep the machine-required JSON contracts intact."
    ),
    "synthesis": (
        "The default Gemma 4 12B model is the recommended synthesis engine "
        "for cross-source story drafting."
    ),
    "citation_fidelity": (
        "The default Gemma 4 12B model best preserves [[S1]]-style citation "
        "markers across long drafts."
    ),
    "speed": (
        "Prefer the smallest curated model (gemma-e2b-tiny) for fast, "
        "Codex-safe runs; accept lower fidelity."
    ),
    "context_length": (
        "The default Gemma 4 12B model offers 256K-token context, keeping "
        "long source text intact across all stages."
    ),
    "translation": (
        "No verified curated model yet; the translation stage is not "
        "implemented in this release - search below for a candidate."
    ),
}

DEFAULT_CATALOG_MODEL_ALIAS = "gemma-4-12b-it-4bit"


@dataclass(frozen=True)
class CatalogModel:
    alias: str
    reference: str
    name: str
    backend: str
    hf_repo: str
    context_length: int | None
    description: str
    task_notes: dict[str, str]


# Curated entries: gemma-4-12b-it-4bit / gemma-e2b-tiny (MLX) and the
# qwythos-9b-* GGUF pair (llama.cpp), aligned with config.py constants and
# MODEL_ALIASES. MLX entries keep reference == hf_repo (issue #92); llama.cpp
# entries use a file-qualified reference (owner/repo/file.gguf) with a bare
# hf_repo page id. Adding more requires runtime verification on Apple Silicon
# or an operator-installed llama-server binary - out of scope for this issue.
BUILTIN_CATALOG_MODELS: dict[str, CatalogModel] = {
    "gemma-4-12b-it-4bit": CatalogModel(
        alias="gemma-4-12b-it-4bit",
        reference="mlx-community/gemma-4-12B-it-4bit",
        name="Gemma 4 12B Instruct (4-bit)",
        backend="mlx-vlm",
        hf_repo="mlx-community/gemma-4-12B-it-4bit",
        context_length=262_144,
        description=(
            "The default model: the standard Gemma 4 12B instruction model as "
            "the mlx-community 4-bit MLX distribution, served by the managed "
            "mlx-vlm backend on Apple Silicon."
        ),
        task_notes={
            "factual_extraction": MODEL_RECOMMENDATION_TASK_NOTES["factual_extraction"],
            "structured_output": MODEL_RECOMMENDATION_TASK_NOTES["structured_output"],
            "synthesis": MODEL_RECOMMENDATION_TASK_NOTES["synthesis"],
            "citation_fidelity": MODEL_RECOMMENDATION_TASK_NOTES["citation_fidelity"],
            "context_length": (
                "256K-token context keeps long source text intact across all "
                "stages; the highest-fidelity curated pick."
            ),
        },
    ),
    "gemma-e2b-tiny": CatalogModel(
        alias="gemma-e2b-tiny",
        reference="deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit",
        name="Gemma E2B Tiny",
        backend="mlx-lm",
        hf_repo="deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit",
        context_length=None,
        description=(
            "Codex-safe test model: tiny 4-bit MLX Gemma for fast runs and "
            "automated verification, served by the managed mlx-lm backend."
        ),
        task_notes={
            "speed": MODEL_RECOMMENDATION_TASK_NOTES["speed"],
        },
    ),
    "qwythos-9b-4bit": CatalogModel(
        alias="qwythos-9b-4bit",
        reference=(
            "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/"
            "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q4_K.gguf"
        ),
        name="Qwythos 9B (Q4_K GGUF)",
        backend="llama.cpp",
        hf_repo="huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF",
        context_length=None,
        description=(
            "Legacy 9B GGUF model (Q4_K), served by the managed llama.cpp "
            "backend with an operator-installed llama-server binary (issue #75)."
        ),
        task_notes={},
    ),
    "qwythos-9b-8bit": CatalogModel(
        alias="qwythos-9b-8bit",
        reference=(
            "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF/"
            "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q8_0.gguf"
        ),
        name="Qwythos 9B (Q8_0 GGUF)",
        backend="llama.cpp",
        hf_repo="huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF",
        context_length=None,
        description=(
            "Legacy 9B GGUF model (Q8_0), served by the managed llama.cpp "
            "backend with an operator-installed llama-server binary (issue #75)."
        ),
        task_notes={},
    ),
}


# --- YAML overlay loading and merge (issue #90) ------------------------------

# Per-process snapshot: loaded on first catalog use and cached for the life of
# the process. Editing the YAML file requires restarting the CLI/UI (no hot
# reload). ``CATALOG_MODELS`` below is exposed through module ``__getattr__``
# so importing this module never touches the file system and malformed YAML
# surfaces as an actionable error at the consumer boundary (CLI exit 2 / UI
# error JSON), never as an import traceback.
_CATALOG_SNAPSHOT: dict[str, CatalogModel] | None = None


def _catalog_path_from_env() -> Path:
    """Resolve the catalog YAML path: NEWS_MODEL_CATALOG_YAML override or the
    default ``config/model_catalog.yaml``. Relative paths resolve from the
    repository root, matching the project's config-file conventions."""
    raw = os.environ.get(MODEL_CATALOG_YAML_ENV_VAR, "").strip()
    if not raw:
        return MODEL_CATALOG_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _load_catalog_yaml(path: Path) -> dict[str, Any]:
    """UTF-8 ``yaml.safe_load``; a missing file or YAML null means no
    overrides, and a non-mapping root fails closed with a path-specific
    error (mirrors ``config._load_yaml_mapping``)."""
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a hard dependency
        raise ImportError(
            "pyyaml is required to load the model catalog YAML overlay. "
            "Run: uv add pyyaml"
        ) from exc
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not load model catalog {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _validate_catalog_alias(raw_alias: Any, path: Path) -> str:
    if not isinstance(raw_alias, str):
        raise ValueError(
            f"{path} model aliases must be strings, got {type(raw_alias).__name__}."
        )
    alias = raw_alias.strip()
    if not alias or alias != raw_alias:
        raise ValueError(
            f"{path} model alias {raw_alias!r} must be a non-empty, trimmed string."
        )
    if not _CATALOG_ALIAS_RE.match(alias):
        raise ValueError(
            f"{path} model alias {alias!r} is not allowed: use lowercase letters, "
            "digits, '.', '_', and '-', starting with a letter or digit."
        )
    return alias


def _validate_catalog_repo_id(value: Any, alias: str, path: Path, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{path} model {alias!r} {field} must be a string."
        )
    repo_id = value.strip()
    if not repo_id or repo_id != value or any(ch.isspace() for ch in repo_id):
        raise ValueError(
            f"{path} model {alias!r} {field} must be a trimmed owner/repo id "
            "without whitespace."
        )
    if ".gguf" in repo_id.lower():
        raise ValueError(
            f"{path} model {alias!r} {field} {repo_id!r} is a file-qualified GGUF "
            "reference; only the llama.cpp backend may name a .gguf file, and it "
            "belongs in the reference field (owner/repo/file.gguf), never here."
        )
    if repo_id.count("/") != 1 or repo_id.startswith("/") or repo_id.endswith("/"):
        raise ValueError(
            f"{path} model {alias!r} {field} {repo_id!r} must be an owner/repo id "
            "with exactly one '/'. File-qualified references and URLs are not "
            "allowed in the model catalog."
        )
    return repo_id


def _validate_catalog_context_length(value: Any, alias: str, path: Path) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{path} model {alias!r} context_length must be null or a positive "
            f"integer, got {value!r}."
        )
    if value <= 0:
        raise ValueError(
            f"{path} model {alias!r} context_length must be a positive integer, "
            f"got {value}."
        )
    return value


def _validate_catalog_task_notes(value: Any, alias: str, path: Path) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(
            f"{path} model {alias!r} task_notes must be a mapping."
        )
    notes: dict[str, str] = {}
    for task, note in value.items():
        if not isinstance(task, str) or task not in MODEL_RECOMMENDATION_TASKS:
            valid = ", ".join(MODEL_RECOMMENDATION_TASKS)
            raise ValueError(
                f"{path} model {alias!r} task_notes key {task!r} is not a known "
                f"recommendation task. Valid tasks: {valid}."
            )
        if not isinstance(note, str) or not note.strip():
            raise ValueError(
                f"{path} model {alias!r} task_notes[{task!r}] must be a non-empty "
                "string."
            )
        notes[task] = note.strip()
    return notes


def _validate_catalog_gguf_reference(value: Any, alias: str, path: Path, field: str) -> str:
    """Validate a llama.cpp file-qualified reference (owner/repo/file.gguf).

    Delegates segment grammar (traversal, separators, control characters) to
    the stdlib-only adapter so catalog identity and launch parsing can never
    disagree; the reference must be the exact file-qualified form.
    """
    if not isinstance(value, str):
        raise ValueError(f"{path} model {alias!r} {field} must be a string.")
    reference = value.strip()
    if not reference or reference != value:
        raise ValueError(
            f"{path} model {alias!r} {field} must be a non-empty, trimmed string."
        )
    try:
        source = parse_llama_cpp_model_reference(reference)
    except ValueError as error:
        raise ValueError(f"{path} model {alias!r} {field}: {error}") from error
    if source.kind != "hf_file":
        raise ValueError(
            f"{path} model {alias!r} {field} must be a file-qualified "
            "owner/repo/file.gguf reference for the llama.cpp backend."
        )
    return reference


def _validate_catalog_entry(
    raw_alias: Any,
    raw_entry: Any,
    path: Path,
) -> CatalogModel:
    """Validate one YAML entry and return its merged CatalogModel.

    Existing built-in aliases may override only ``name``, ``description``,
    ``context_length``, and ``task_notes`` (task notes merge by task key);
    ``reference``/``backend``/``hf_repo`` must be absent or match the built-in
    identity exactly so code-reviewed runtime identity stays immutable. New
    aliases must provide the full identity plus a description.
    """
    alias = _validate_catalog_alias(raw_alias, path)
    if not isinstance(raw_entry, dict):
        raise ValueError(f"{path} model {alias!r} must be a mapping.")
    unknown_fields = [field for field in raw_entry if field not in _CATALOG_ALL_FIELDS]
    if unknown_fields:
        raise ValueError(
            f"{path} model {alias!r} has unknown field(s): "
            f"{', '.join(repr(field) for field in unknown_fields)}. Valid fields: "
            f"{', '.join(_CATALOG_ALL_FIELDS)}."
        )

    builtin = BUILTIN_CATALOG_MODELS.get(alias)
    if builtin is not None:
        for field in ("reference", "backend", "hf_repo"):
            if field not in raw_entry:
                continue
            declared = str(raw_entry[field]).strip()
            expected = getattr(builtin, field)
            if declared != expected:
                raise ValueError(
                    f"{path} model {alias!r} {field} {declared!r} does not match "
                    f"the built-in entry ({expected!r}). Runtime identity is "
                    "code-owned; override only name, description, "
                    "context_length, and task_notes, or add a new alias."
                )
        task_notes = builtin.task_notes
        if "task_notes" in raw_entry:
            task_notes = {**task_notes, **_validate_catalog_task_notes(raw_entry["task_notes"], alias, path)}
        context_length = builtin.context_length
        if "context_length" in raw_entry:
            context_length = _validate_catalog_context_length(raw_entry["context_length"], alias, path)
        return CatalogModel(
            alias=builtin.alias,
            reference=builtin.reference,
            name=str(raw_entry.get("name", builtin.name)).strip() or builtin.name,
            backend=builtin.backend,
            hf_repo=builtin.hf_repo,
            context_length=context_length,
            description=str(raw_entry.get("description", builtin.description)).strip() or builtin.description,
            task_notes=task_notes,
        )

    missing = [field for field in _CATALOG_REQUIRED_FIELDS if field not in raw_entry]
    if missing:
        raise ValueError(
            f"{path} new model alias {alias!r} must provide: {', '.join(missing)}."
        )
    backend = str(raw_entry["backend"]).strip().lower()
    if backend not in CATALOG_MODEL_BACKENDS:
        raise ValueError(
            f"{path} model {alias!r} backend {backend!r} is not supported. "
            f"Valid backends: {', '.join(CATALOG_MODEL_BACKENDS)}."
        )
    if backend == "llama.cpp":
        hf_repo = _validate_catalog_repo_id(raw_entry["hf_repo"], alias, path, "hf_repo")
        reference = _validate_catalog_gguf_reference(
            raw_entry["reference"], alias, path, "reference"
        )
        mismatched = reference.rsplit("/", 1)[0] != hf_repo
        mismatch_error = (
            f"{path} model {alias!r} reference must be a file-qualified "
            f".gguf reference under hf_repo; got reference={reference!r}, "
            f"hf_repo={hf_repo!r}."
        )
    else:
        reference = _validate_catalog_repo_id(raw_entry["reference"], alias, path, "reference")
        hf_repo = _validate_catalog_repo_id(raw_entry["hf_repo"], alias, path, "hf_repo")
        mismatched = reference != hf_repo
        mismatch_error = (
            f"{path} model {alias!r} reference must equal hf_repo (issue #92 "
            f"drift guard); got reference={reference!r}, hf_repo={hf_repo!r}."
        )
    if mismatched:
        raise ValueError(mismatch_error)
    name = str(raw_entry["name"]).strip()
    description = str(raw_entry["description"]).strip()
    if not name:
        raise ValueError(f"{path} model {alias!r} name must be a non-empty string.")
    if not description:
        raise ValueError(
            f"{path} model {alias!r} description must be a non-empty string."
        )
    context_length = None
    if "context_length" in raw_entry:
        context_length = _validate_catalog_context_length(raw_entry["context_length"], alias, path)
    task_notes = {}
    if "task_notes" in raw_entry:
        task_notes = _validate_catalog_task_notes(raw_entry["task_notes"], alias, path)
    return CatalogModel(
        alias=alias,
        reference=reference,
        name=name,
        backend=backend,
        hf_repo=hf_repo,
        context_length=context_length,
        description=description,
        task_notes=task_notes,
    )


def load_model_catalog(path: Path | None = None) -> dict[str, CatalogModel]:
    """Load and validate the YAML overlay, then merge it over the built-ins.

    Built-ins come first (in code order); YAML additions follow in YAML order;
    metadata overrides keep the built-in entry's position. A missing file,
    ``models: {}``, or a YAML null payload preserves the built-in catalog
    exactly. Any malformed or unsafe entry raises a path-specific
    ``ValueError`` - the catalog never silently falls back.
    """
    catalog_path = path or _catalog_path_from_env()
    payload = _load_catalog_yaml(catalog_path)
    unknown_top = [key for key in payload if key != "models"]
    if unknown_top:
        raise ValueError(
            f"{catalog_path} contains unknown top-level key(s): "
            f"{', '.join(repr(key) for key in unknown_top)}. Only 'models' is allowed."
        )
    raw_models = payload.get("models", {})
    if raw_models is None:
        raw_models = {}
    if not isinstance(raw_models, dict):
        raise ValueError(f"{catalog_path} must define models as a mapping.")

    merged: dict[str, CatalogModel] = {}
    for alias, builtin in BUILTIN_CATALOG_MODELS.items():
        merged[alias] = builtin
    for raw_alias, raw_entry in raw_models.items():
        model = _validate_catalog_entry(raw_alias, raw_entry, catalog_path)
        merged[model.alias] = model
    return merged


def _merged_catalog_snapshot() -> dict[str, CatalogModel]:
    global _CATALOG_SNAPSHOT
    if _CATALOG_SNAPSHOT is None:
        _CATALOG_SNAPSHOT = load_model_catalog()
    return _CATALOG_SNAPSHOT


def __getattr__(name: str) -> Any:
    if name == "CATALOG_MODELS":
        return _merged_catalog_snapshot()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def custom_catalog_aliases() -> dict[str, str]:
    """Alias -> reference for YAML-added entries only (never built-ins)."""
    return {
        model.alias: model.reference
        for model in _merged_catalog_snapshot().values()
        if model.alias not in BUILTIN_CATALOG_MODELS
    }


def catalog_model_backend(alias_or_reference: str) -> str | None:
    """Return the catalog-declared backend for an exact alias or reference.

    Matching is exact (issue #92): a bare org id or prefix sibling never
    matches. Unknown references return None so callers keep their heuristic
    fallback."""
    clean = (alias_or_reference or "").strip()
    if not clean:
        return None
    entry = _merged_catalog_snapshot().get(clean)
    if entry is not None:
        return entry.backend
    for model in _merged_catalog_snapshot().values():
        if model.reference == clean:
            return model.backend
    return None


def list_model_catalog() -> list[dict[str, Any]]:
    """Return JSON-ready catalog records (no network, offline-first)."""
    return [
        {
            "alias": model.alias,
            "reference": model.reference,
            "name": model.name,
            "backend": model.backend,
            "hf_url": f"https://huggingface.co/{model.hf_repo}",
            "context_length": model.context_length,
            "description": model.description,
            "task_notes": dict(model.task_notes),
            "is_default": model.alias == DEFAULT_CATALOG_MODEL_ALIAS,
        }
        for model in _merged_catalog_snapshot().values()
    ]


def recommend_models(task: str) -> list[dict[str, Any]]:
    """Return curated picks for a recommendation task, ordered by priority.

    Entries that carry a task note come first (catalog order), then the
    default model as a fallback when it is not already included. Returns an
    empty list when no curated entry covers the task (translation - the honest
    documented gap).
    """
    if task not in MODEL_RECOMMENDATION_TASKS:
        raise ValueError(
            f"Unknown model recommendation task {task!r}. "
            f"Valid tasks: {', '.join(MODEL_RECOMMENDATION_TASKS)}"
        )
    picks = [model for model in _merged_catalog_snapshot().values() if task in model.task_notes]
    if picks:
        default = _merged_catalog_snapshot().get(DEFAULT_CATALOG_MODEL_ALIAS)
        if default is not None and all(model.alias != default.alias for model in picks):
            picks.append(default)
    return [
        {
            "alias": model.alias,
            "name": model.name,
            "backend": model.backend,
            "hf_repo": model.hf_repo,
            "reason": model.task_notes.get(task) or model.description,
        }
        for model in picks
    ]


# --- Runtime-fit validation -------------------------------------------------

RUNTIME_FIT_MANAGED_MLX_LM = "managed_mlx_lm"
RUNTIME_FIT_MANAGED_MLX_VLM = "managed_mlx_vlm"
RUNTIME_FIT_MANAGED_LLAMA_CPP = "managed_llama_cpp"
RUNTIME_FIT_EXTERNAL_ONLY = "external_only"

RUNTIME_FIT_LABELS: dict[str, str] = {
    RUNTIME_FIT_MANAGED_MLX_LM: "Managed mlx-lm",
    RUNTIME_FIT_MANAGED_MLX_VLM: "Managed mlx-vlm",
    RUNTIME_FIT_MANAGED_LLAMA_CPP: "Managed llama.cpp",
    RUNTIME_FIT_EXTERNAL_ONLY: "External only",
}

# Without these expand fields the list_models/model_info stats come back None
# (verified live against huggingface_hub 1.26.0).
HF_SEARCH_EXPAND = [
    "pipeline_tag",
    "downloads",
    "likes",
    "lastModified",
    "library_name",
    "tags",
    "cardData",
    "config",
]

# Closed set of pipeline tags the CLI/UI may filter search by (ADR 0017).
HF_SEARCH_PIPELINE_TAGS = ("text-generation", "text2text-generation", "image-text-to-text")


def _hf_api() -> Any:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise ImportError(
            "huggingface-hub is required for Hugging Face search. "
            "Run: uv add huggingface-hub"
        ) from exc
    return HfApi()


def runtime_fit_for_hf_model(info: Mapping[str, Any]) -> dict[str, str]:
    """Classify a Hugging Face model repo into a runtime-fit verdict.

    Returns ``{"status": ..., "reason": ...}`` where status is one of the
    ``RUNTIME_FIT_*`` constants. Rules are conservative (ADR 0017): code-owned
    curated repos and user-declared catalog entries are classified by their
    declared backend, while MLX libraries and transformers+safetensors
    text/vision repos are classified by metadata; everything else is
    ``external_only`` (never a hard block - only a verdict plus a picker
    guard). User YAML metadata is advisory, not project verification.
    """
    repo_id = str(info.get("id") or "")
    tags = {str(tag).lower() for tag in (info.get("tags") or [])}
    library_name = str(info.get("library_name") or "").lower()
    pipeline_tag = str(info.get("pipeline_tag") or "").lower()

    for model in _merged_catalog_snapshot().values():
        # Match on hf_repo only (issue #92): the reference clause was
        # redundant because reference == hf_repo for every curated entry
        # (drift-guard: test_catalog_entries_are_complete), so hf_repo
        # equality is the complete, anchored match. The org-id false positive
        # came from the earlier prefix match on reference (removed in the
        # #124 follow-up 0f982ef); the org-id cases in the runtime-fit matrix
        # pin it as regression guards.
        if repo_id == model.hf_repo:
            is_builtin = model.alias in BUILTIN_CATALOG_MODELS
            if model.backend == "external":
                return {
                    "status": RUNTIME_FIT_EXTERNAL_ONLY,
                    "reason": "Catalog entry declares external use; runtime fit is advisory.",
                }
            if model.backend in ("mlx-vlm", "mlx-lm"):
                status = (
                    RUNTIME_FIT_MANAGED_MLX_VLM
                    if model.backend == "mlx-vlm"
                    else RUNTIME_FIT_MANAGED_MLX_LM
                )
                if is_builtin:
                    reason = "Curated model, verified for this backend."
                else:
                    reason = f"Catalog entry declares managed {model.backend}; runtime fit is advisory."
                return {"status": status, "reason": reason}
            if model.backend == "llama.cpp":
                if is_builtin:
                    reason = "Curated GGUF model, launchable by the managed llama.cpp server."
                else:
                    reason = "Catalog entry declares managed llama.cpp; runtime fit is advisory."
                return {"status": RUNTIME_FIT_MANAGED_LLAMA_CPP, "reason": reason}
            # Catalog loading allowlists backends; fail closed if a test or
            # future caller constructs an invalid CatalogModel directly.
            return {
                "status": RUNTIME_FIT_EXTERNAL_ONLY,
                "reason": "Catalog entry declares an unsupported backend; runtime fit is advisory.",
            }

    if "gguf" in tags:
        if "image-text-to-text" in tags or pipeline_tag == "image-text-to-text":
            return {
                "status": RUNTIME_FIT_EXTERNAL_ONLY,
                "reason": (
                    "Multimodal GGUF models need a separate mmproj file and are "
                    "not managed by this release; use an external OpenAI-compatible "
                    "endpoint (NEWS_MODEL_BACKEND=external)."
                ),
            }
        if pipeline_tag not in {"text-generation", "text2text-generation"}:
            return {
                "status": RUNTIME_FIT_EXTERNAL_ONLY,
                "reason": (
                    "GGUF metadata does not identify a supported text-generation "
                    "pipeline; use an external OpenAI-compatible endpoint "
                    "(NEWS_MODEL_BACKEND=external)."
                ),
            }
        return {
            "status": RUNTIME_FIT_MANAGED_LLAMA_CPP,
            "reason": (
                "Text-generation GGUF model; launchable by the managed llama.cpp "
                "server when a llama-server binary is installed "
                "(NEWS_LLAMA_CPP_SERVER)."
            ),
        }

    if library_name == "mlx" or "mlx" in tags:
        if "image-text-to-text" in tags or pipeline_tag == "image-text-to-text":
            return {
                "status": RUNTIME_FIT_MANAGED_MLX_VLM,
                "reason": "MLX vision-language model; launchable by the managed mlx-vlm server.",
            }
        return {
            "status": RUNTIME_FIT_MANAGED_MLX_LM,
            "reason": "MLX language model; launchable by the managed mlx-lm server.",
        }

    if library_name == "transformers" and "safetensors" in tags:
        if pipeline_tag == "image-text-to-text":
            return {
                "status": RUNTIME_FIT_MANAGED_MLX_VLM,
                "reason": "Transformers vision-language model; launchable by the managed mlx-vlm server.",
            }
        if pipeline_tag in ("text-generation", "text2text-generation"):
            return {
                "status": RUNTIME_FIT_MANAGED_MLX_LM,
                "reason": "Transformers text model; launchable by the managed mlx-lm server.",
            }
        return {
            "status": RUNTIME_FIT_EXTERNAL_ONLY,
            "reason": "Transformers model outside the supported pipeline tags (ADR 0017).",
        }

    return {
        "status": RUNTIME_FIT_EXTERNAL_ONLY,
        "reason": "Use with NEWS_MODEL_BACKEND=external and an OpenAI-compatible endpoint.",
    }


def _model_info_to_payload(info: Any) -> dict[str, Any]:
    """Map a huggingface_hub ModelInfo (or test fake) to a JSON-ready dict."""
    repo_id = str(getattr(info, "id", "") or "")
    card_data = getattr(info, "card_data", None)
    raw_config = getattr(info, "config", None)
    if isinstance(raw_config, dict):
        context_length = raw_config.get("max_position_embeddings")
    elif raw_config is not None:
        context_length = getattr(raw_config, "max_position_embeddings", None)
    else:
        context_length = None
    tags = [str(tag) for tag in (getattr(info, "tags", None) or [])]
    last_modified = getattr(info, "last_modified", None)
    if isinstance(last_modified, datetime):
        last_modified = last_modified.isoformat()
    runtime_fit = runtime_fit_for_hf_model(
        {
            "id": repo_id,
            "tags": tags,
            "library_name": getattr(info, "library_name", None),
            "pipeline_tag": getattr(info, "pipeline_tag", None),
        }
    )
    return {
        "id": repo_id,
        "author": getattr(info, "author", None),
        "hf_url": f"https://huggingface.co/{repo_id}",
        "pipeline_tag": getattr(info, "pipeline_tag", None),
        "library_name": getattr(info, "library_name", None),
        "downloads": getattr(info, "downloads", None),
        "likes": getattr(info, "likes", None),
        "last_modified": last_modified,
        "license": getattr(card_data, "license", None),
        "context_length": context_length,
        "tags": tags[:12],
        "runtime_fit": runtime_fit,
        "in_catalog": any(repo_id == model.hf_repo for model in _merged_catalog_snapshot().values()),
    }


def search_huggingface_models(
    query: str,
    *,
    pipeline_tag: str | None = None,
    limit: int = 20,
    token: bool | str | None = None,
) -> list[dict[str, Any]]:
    """Search Hugging Face models and annotate each result with a runtime fit.

    Exceptions propagate to callers (CLI/UI own the error envelope). ``limit``
    is clamped to 1-50.
    """
    api = _hf_api()
    results = api.list_models(
        search=query,
        pipeline_tag=pipeline_tag,
        limit=max(1, min(int(limit), 50)),
        expand=HF_SEARCH_EXPAND,
        token=token,
    )
    return [_model_info_to_payload(info) for info in list(results)]


def fetch_model_metadata(
    reference: str,
    *,
    token: bool | str | None = None,
) -> dict[str, Any]:
    """Fetch single-repo metadata from Hugging Face.

    Raises ``ValueError`` when the repository is not found so the UI
    ``/api/models/metadata`` error envelope holds; network/auth/rate-limit
    errors propagate unchanged (the UI endpoint owns the envelope).
    """
    api = _hf_api()
    try:
        info = api.model_info(repo_id=reference, expand=HF_SEARCH_EXPAND, token=token)
    except Exception as exc:
        from huggingface_hub.errors import RepositoryNotFoundError

        if isinstance(exc, RepositoryNotFoundError):
            raise ValueError(f"Model not found on Hugging Face: {reference!r}") from exc
        raise  # network/server errors keep their real message
    return _model_info_to_payload(info)
