"""Model Catalog: code-owned registry of curated models and Hugging Face search.

The Model Catalog owns the set of models the pipeline's backends can actually
launch, with per-task recommendations (factual extraction, structured output,
synthesis, citation fidelity, speed, context length, translation) instead of
parameter count or popularity. Every Hugging Face search result is annotated
with a runtime-fit verdict so the picker never offers a repo the configured
backend cannot launch (HANDOFF: "Model picker must validate runtime support").

This module is deliberately stdlib-only at module level (``dataclasses``,
``logging``, ``typing``) so that ``config.py``/``cli.py``/``ui.py`` can import
it without creating an import cycle. ``huggingface_hub`` is imported lazily
inside ``_hf_api()`` only (mirrors the lazy ``sentence_transformers`` import in
``embeddings.py``). Built-ins live in Python (not YAML) because they are
code-reviewed contracts; a user-editable YAML override layer is a later issue.
The drift-guard tying catalog aliases to ``config.MODEL_ALIASES`` lives in
tests (``test_model_catalog.py``), like ``prompt_catalog``'s profile drift
guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

logger = logging.getLogger(__name__)

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


# Exactly 2 curated entries (gemma-4-12b-it-4bit / gemma-e2b-tiny), aligned
# with config.py GEMMA_4_12B_IT_4BIT_* and CODEX_TEST_* constants and
# MODEL_ALIASES. The Qwythos GGUF pair is NOT curated: mlx-vlm cannot launch
# file-qualified GGUF refs (issue #124). Adding more requires runtime
# verification on Apple Silicon - out of scope for this issue.
CATALOG_MODELS: dict[str, CatalogModel] = {
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
        backend="mlx-vlm",
        hf_repo="deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit",
        context_length=None,
        description=(
            "Codex-safe test model: tiny 4-bit MLX Gemma VLM for fast runs and "
            "automated verification, served by the managed mlx-vlm backend "
            "(HF metadata: image-text-to-text)."
        ),
        task_notes={
            "speed": MODEL_RECOMMENDATION_TASK_NOTES["speed"],
        },
    ),
}


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
        for model in CATALOG_MODELS.values()
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
    picks = [model for model in CATALOG_MODELS.values() if task in model.task_notes]
    if picks:
        default = CATALOG_MODELS.get(DEFAULT_CATALOG_MODEL_ALIAS)
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
RUNTIME_FIT_EXTERNAL_ONLY = "external_only"

RUNTIME_FIT_LABELS: dict[str, str] = {
    RUNTIME_FIT_MANAGED_MLX_LM: "Managed mlx-lm",
    RUNTIME_FIT_MANAGED_MLX_VLM: "Managed mlx-vlm",
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

# Closed set of pipeline tags the CLI/UI may filter search by (ADR 0010).
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
    ``RUNTIME_FIT_*`` constants. Rules are conservative (ADR 0010): only
    curated repos, MLX libraries, and transformers+safetensors text/vision
    repos are launchable by the managed backends; everything else is
    ``external_only`` (never a hard block - only a verdict plus a picker
    guard).
    """
    repo_id = str(info.get("id") or "")
    tags = {str(tag).lower() for tag in (info.get("tags") or [])}
    library_name = str(info.get("library_name") or "").lower()
    pipeline_tag = str(info.get("pipeline_tag") or "").lower()

    for model in CATALOG_MODELS.values():
        # Match on hf_repo only (issue #92): the reference clause was
        # redundant because reference == hf_repo for every curated entry
        # (drift-guard: test_catalog_entries_are_complete), so hf_repo
        # equality is the complete, anchored match. The org-id false positive
        # came from the earlier prefix match on reference (removed in the
        # #124 follow-up 0f982ef); the org-id cases in the runtime-fit matrix
        # pin it as regression guards.
        if repo_id == model.hf_repo:
            if model.backend == "mlx-vlm":
                return {
                    "status": RUNTIME_FIT_MANAGED_MLX_VLM,
                    "reason": "Curated model, verified for this backend.",
                }
            return {
                "status": RUNTIME_FIT_MANAGED_MLX_LM,
                "reason": "Curated model, verified for this backend.",
            }

    if "gguf" in tags:
        return {
            "status": RUNTIME_FIT_EXTERNAL_ONLY,
            "reason": (
                "Managed MLX servers cannot launch GGUF files; use an external "
                "OpenAI-compatible endpoint (NEWS_MODEL_BACKEND=external)."
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
            "reason": "Transformers model outside the supported pipeline tags (ADR 0010).",
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
        "in_catalog": any(repo_id == model.hf_repo for model in CATALOG_MODELS.values()),
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
