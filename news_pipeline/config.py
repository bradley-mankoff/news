"""Configuration loading for the daily news pipeline.

The pipeline is intentionally driven by small YAML files in ``config/``:

- ``sources.yaml`` defines article feeds searched by run-mode tier and language.
- ``recipients.yaml`` defines email recipients and optional personal prompts.

Environment variables can override Run Settings without editing YAML. See
``README.md`` for the full command list.
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from .prompt_catalog import (
    DEFAULT_PROMPT_PROFILE_ID,
    PROMPT_PROFILE_ENV_VAR,
    PROMPT_PROFILE_IDS,
    PROMPT_TASK_OVERRIDE_ENV_VARS,
    get_prompt_profile,
)
from .prompt_contracts import validate_editorial_instructions



ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
RUN_PRESETS_PATH = CONFIG_DIR / "run_presets.yaml"
MODEL_TUNING_PRESETS_PATH = CONFIG_DIR / "model_tuning_presets.yaml"
CURSORIGNORE_MANAGED_START = "# >>> news-pipeline latest output >>>"
CURSORIGNORE_MANAGED_END = "# <<< news-pipeline latest output <<<"
ASSISTANT_CONTEXT_MANAGED_START = "# >>> news-pipeline core context >>>"
ASSISTANT_CONTEXT_MANAGED_END = "# <<< news-pipeline core context <<<"
ASSISTANT_CONTEXT_IGNORE_FILES = (".codexignore", ".cursorignore", ".continueignore")
ASSISTANT_CONTEXT_CORE_PATTERNS = (
    "*",
    "!AGENTS.md",
    "!.codexignore",
    "!.continueignore",
    "!.cursorignore",
    "!.gitignore",
    "!.python-version",
    "!.codex/",
    "!.codex/config.toml",
    "!pyproject.toml",
    "!uv.lock",
    "!news_pipeline/",
    "!news_pipeline/**",
    "!config/",
    "!config/run_presets.yaml",
    "!config/sources.yaml",
    "!config/recipients.yaml",
    "__pycache__/",
    "**/__pycache__/",
    "*.py[cod]",
    "**/*.py[cod]",
    ".DS_Store",
    "**/.DS_Store",
)
RUN_OUTPUT_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
QWWYTHOS_REPO = "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF"
QWWYTHOS_Q4K_FILENAME = "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q4_K.gguf"
QWWYTHOS_Q8_FILENAME = "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q8_0.gguf"
QWWYTHOS_MMPROJ_FILENAME = "mmproj-model-bf16.gguf"
QWWYTHOS_9B_4BIT_MODEL_ALIAS = "qwythos-9b-4bit"
QWWYTHOS_9B_4BIT_MODEL_REFERENCE = f"{QWWYTHOS_REPO}/{QWWYTHOS_Q4K_FILENAME}"
QWWYTHOS_9B_8BIT_MODEL_ALIAS = "qwythos-9b-8bit"
QWWYTHOS_9B_8BIT_MODEL_REFERENCE = f"{QWWYTHOS_REPO}/{QWWYTHOS_Q8_FILENAME}"
MODEL_BACKEND_MLX_LM = "mlx-lm"
MODEL_BACKEND_MLX_VLM = "mlx-vlm"
MODEL_BACKEND_EXTERNAL = "external"
SUPPORTED_MODEL_BACKENDS = (MODEL_BACKEND_MLX_LM, MODEL_BACKEND_MLX_VLM, MODEL_BACKEND_EXTERNAL)
# Retained public alias for the Qwythos default backend; prefer MODEL_BACKEND_MLX_VLM.
QWWYTHOS_MODEL_BACKEND = MODEL_BACKEND_MLX_VLM
DEFAULT_MODEL_ALIAS = QWWYTHOS_9B_8BIT_MODEL_ALIAS
CODEX_TEST_MODEL_ALIAS = "gemma-e2b-tiny"
CODEX_TEST_MODEL_NAME = "deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit"
MODEL_TASK_ARTICLE_SUMMARY = "article_summary"
MODEL_TASK_STORY_DRAFTING = "story_drafting"
MODEL_TASK_STORY_SCALE_SCREENING = "story_scale_screening"
MODEL_TASK_TITLE_GENERATION = "title_generation"
MODEL_TASK_IMAGE_ART_DIRECTION = "image_art_direction"
MODEL_TASK_STORY_DISCOVERY = "story_discovery"
GEMMA_4_ARTICLE_SUMMARY_CAP = 40
CORE_SOURCE_TIER = "core"
PERIPHERAL_SOURCE_TIER = "peripheral"
SOURCE_SCOPE_CORE = CORE_SOURCE_TIER
SOURCE_SCOPE_PERIPHERAL = PERIPHERAL_SOURCE_TIER
SOURCE_SCOPES = (SOURCE_SCOPE_CORE, SOURCE_SCOPE_PERIPHERAL)
RECIPIENT_SCOPE_PRIMARY = "primary"
RECIPIENT_SCOPE_ALL = "all"
RECIPIENT_SCOPES = (RECIPIENT_SCOPE_PRIMARY, RECIPIENT_SCOPE_ALL)
PRESET_ENV_VAR = "NEWS_PRESET"
ACTIVE_PRESET_ENV_VAR = "NEWS_ACTIVE_PRESET"
PRESET_MARKER_ENV_VARS = {PRESET_ENV_VAR, ACTIVE_PRESET_ENV_VAR}
SOURCE_SCOPE_TIERS = {
    SOURCE_SCOPE_CORE: {CORE_SOURCE_TIER},
    SOURCE_SCOPE_PERIPHERAL: {CORE_SOURCE_TIER, PERIPHERAL_SOURCE_TIER},
}
SOURCE_MATCH_MODE_FEED_LABEL = "feed_label"
SOURCE_MATCH_MODE_WIRE_ATTRIBUTION = "wire_attribution"
VALID_SOURCE_MATCH_MODES = {
    SOURCE_MATCH_MODE_FEED_LABEL,
    SOURCE_MATCH_MODE_WIRE_ATTRIBUTION,
}
MODEL_ALIASES = {
    QWWYTHOS_9B_4BIT_MODEL_ALIAS: QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
    QWWYTHOS_9B_8BIT_MODEL_ALIAS: QWWYTHOS_9B_8BIT_MODEL_REFERENCE,
    f"https://huggingface.co/{QWWYTHOS_9B_4BIT_MODEL_REFERENCE}": QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
    f"https://hf.co/{QWWYTHOS_9B_4BIT_MODEL_REFERENCE}": QWWYTHOS_9B_4BIT_MODEL_REFERENCE,
    f"https://huggingface.co/{QWWYTHOS_9B_8BIT_MODEL_REFERENCE}": QWWYTHOS_9B_8BIT_MODEL_REFERENCE,
    f"https://hf.co/{QWWYTHOS_9B_8BIT_MODEL_REFERENCE}": QWWYTHOS_9B_8BIT_MODEL_REFERENCE,
    CODEX_TEST_MODEL_ALIAS: CODEX_TEST_MODEL_NAME,
    f"https://huggingface.co/{CODEX_TEST_MODEL_NAME}": CODEX_TEST_MODEL_NAME,
    f"https://hf.co/{CODEX_TEST_MODEL_NAME}": CODEX_TEST_MODEL_NAME,
}
UNSUPPORTED_MODEL_REFERENCES: set[str] = set()
CODEX_RUNTIME_ENV_VARS = ("CODEX_SANDBOX", "CODEX_CI", "CODEX_THREAD_ID")
REMOVED_TOPIC_ENV_VARS = (
    "NEWS_TOPIC_IDS",
    "NEWS_TOPIC_MODE",
    "NEWS_CLIENT_YAML",
    "NEWS_TOPICS_YAML",
    "NEWS_MODEL_TOPIC_CLUSTERING",
    "NEWS_MODEL_TOPIC_COUNTRY_GATE",
    "NEWS_MODEL_STORY_TOPIC_VALIDATION",
    "NEWS_NUM_TOP_TOPICS",
    "NEWS_TOP_TOPIC_PROBES",
    "NEWS_TOPIC_RELEVANCE_MIN_SCORE",
    "NEWS_STORY_TOPIC_FIT_MIN_SCORE",
    "NEWS_STORY_TOPIC_VALIDATION_ENABLED",
    "NEWS_US_TOPIC_COUNTRY_GATE_ENABLED",
    "NEWS_MAX_STORIES_PER_TOPIC",
    "NEWS_TOPIC_EMBEDDING_THRESHOLD",
    "NEWS_PER_SOURCE_TOPIC_ARTICLE_CAP",
    "NEWS_SUMMARY_SCOPE",
)
REMOVED_SOURCE_TOPIC_FIELDS = {
    "allowed_topic_ids",
    "only_topic_ids",
    "source_topic_ids",
    "can_seed_topics",
    "seed_topics",
    "can_validate_topics",
    "validate_topics",
}
_CONFIG_ENV: ContextVar[Mapping[str, str] | None] = ContextVar(
    "news_pipeline_config_env",
    default=None,
)


@dataclass(frozen=True)
class ModelSamplingSettings:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None


@dataclass(frozen=True)
class ModelTuningSettings:
    model_max_input_tokens: int | None = None
    article_summary_max_tokens: int | None = None
    story_drafting_max_tokens: int | None = None
    story_scale_screening_max_tokens: int | None = None
    title_generation_max_tokens: int | None = None
    task_sampling: dict[str, ModelSamplingSettings] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineBudget:
    article_text_token_limit: int
    total_article_summary_cap: int
    recent_window_hours: int
    max_articles_per_source: int
    min_articles_per_story: int
    max_stories: int
    story_cluster_similarity_threshold: float
    story_selection_overlap_threshold: float
    story_embedding_dedup_threshold: float
    story_backfill_batch_multiplier: int


@dataclass(frozen=True)
class ModelServerSettings:
    base_url: str
    prefill_step_size: int | None = None
    prompt_cache_size: int | None = None
    prompt_cache_bytes: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class TaskModelAssignment:
    task: str
    reference: str
    name: str
    backend: str
    base_url: str
    server_command: str
    tuning: ModelTuningSettings


@dataclass(frozen=True)
class RuntimeConfig:
    root_dir: Path
    sources_path: Path
    recipients_path: Path
    env_json_path: Path
    run_started_at: datetime
    run_date: str
    timestamp: str
    output_dir: Path
    run_output_dir: Path
    latest_run_markdown_path: Path
    latest_run_log_path: Path
    latest_run_details_path: Path
    run_staging_dir: Path
    history_db_path: Path
    history_export_csv: bool
    run_used_urls_path: Path
    preset_id: str
    prompt_profile_id: str
    prompt_instruction_overrides: dict[str, str]
    source_scope: str
    recipient_scope: str
    url_reuse_blocking_enabled: bool
    relaxed_story_drafting_guards: bool
    model_reference: str
    model_name: str
    model_base_url: str
    model_backend: str
    model_concurrency: int
    article_summary_concurrency: int
    story_synthesis_concurrency: int
    source_collection_concurrency: int
    model_max_input_tokens: int
    article_text_token_limit: int
    total_article_summary_cap: int
    article_summary_max_tokens: int
    story_drafting_max_tokens: int
    model_assignments: dict[str, TaskModelAssignment]
    model_tuning: ModelTuningSettings
    pipeline_budget: PipelineBudget
    model_server_settings: ModelServerSettings
    model_server_command: str
    recent_window_hours: int
    max_articles_per_source: int
    primary_recipient: str
    email_recipients_fallback: list[str]
    email_from: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_use_ssl: bool
    smtp_password: str
    unsubscribe_base_url: str
    unsubscribe_host: str
    unsubscribe_port: int
    unsubscribe_secret: str
    token_encoding_name: str
    image_generation_enabled: bool
    image_generation_fail_on_error: bool
    image_width: int
    image_height: int
    image_steps: int
    image_crop_bottom_ratio: float
    image_model_id: str
    image_base_model: str
    min_articles_per_story: int
    story_cluster_similarity_threshold: float
    total_article_summary_cap_gemma_4_derived: bool
    story_scale_screening_enabled: bool
    max_stories: int
    story_selection_overlap_threshold: float
    story_embedding_dedup_threshold: float
    story_backfill_batch_multiplier: int


@dataclass(frozen=True)
class RuntimeConfigRequest:
    base_env: Mapping[str, str] | None = None
    preset_id: str | None = None
    overrides: Mapping[str, str] | None = None
    materialize_outputs: bool = True
    run_started_at: datetime | None = None


@dataclass(frozen=True)
class RuntimeConfigResolution:
    config: RuntimeConfig
    effective_env: dict[str, str]
    preset_env: dict[str, str]
    command_env_delta: dict[str, str]
    removed_topic_env_vars: list[str]


MODEL_SPECIFIC_TUNING_DEFAULTS: dict[str, ModelTuningSettings] = {}


DEFAULT_PIPELINE_CONCURRENCY = 4
DEFAULT_ARTICLE_SUMMARY_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY
DEFAULT_STORY_SYNTHESIS_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY
DEFAULT_SOURCE_COLLECTION_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY
DEFAULT_TOTAL_ARTICLE_SUMMARY_CAP = 40
GEMMA_4_ARTICLE_SUMMARY_CAP = DEFAULT_TOTAL_ARTICLE_SUMMARY_CAP
DEFAULT_ARTICLE_TEXT_TOKEN_LIMIT = 4500
DEFAULT_MODEL_MAX_INPUT_TOKENS = 6000
DEFAULT_ARTICLE_SUMMARY_MAX_TOKENS = 1000
DEFAULT_STORY_DRAFTING_MAX_TOKENS = 1800
# Mirrors story_selection.STORY_SCALE_VALIDATION_MAX_TOKENS; keep in sync.
DEFAULT_STORY_SCALE_SCREENING_MAX_TOKENS = 3000
DEFAULT_TITLE_GENERATION_MAX_TOKENS = 700
DEFAULT_MODEL_SERVER_PREFILL_STEP_SIZE = 512
DEFAULT_MODEL_SERVER_PROMPT_CACHE_SIZE = 2
DEFAULT_MODEL_SERVER_PROMPT_CACHE_BYTES = "512MB"
DEFAULT_MODEL_SERVER_MAX_TOKENS = 1800
# Retained for compatibility: story_discovery has no LLM stage, but legacy
# NEWS_MODEL_STORY_DISCOVERY_* env vars must keep resolving. Do not remove.
MODEL_TASK_SAMPLING_ENV_PREFIXES = {
    "default": "NEWS_MODEL",
    MODEL_TASK_STORY_DISCOVERY: "NEWS_MODEL_STORY_DISCOVERY",
    MODEL_TASK_STORY_SCALE_SCREENING: "NEWS_MODEL_STORY_SCALE_SCREENING",
    MODEL_TASK_ARTICLE_SUMMARY: "NEWS_MODEL_ARTICLE_SUMMARY",
    MODEL_TASK_STORY_DRAFTING: "NEWS_MODEL_STORY_DRAFTING",
    MODEL_TASK_TITLE_GENERATION: "NEWS_MODEL_TITLE_GENERATION",
}
MODEL_TUNING_PRESET_ENV_VARS = {
    "default": "NEWS_MODEL_TUNING_PRESET",
    MODEL_TASK_ARTICLE_SUMMARY: "NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET",
    MODEL_TASK_STORY_DRAFTING: "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET",
    MODEL_TASK_STORY_SCALE_SCREENING: "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET",
    MODEL_TASK_TITLE_GENERATION: "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET",
}
MODEL_REASONING_SAMPLING_ENV_PREFIX = "NEWS_MODEL_REASONING"


def _empty_model_sampling_map() -> dict[str, ModelSamplingSettings]:
    # Keys mirror MODEL_TASK_SAMPLING_ENV_PREFIXES; story_discovery stays for
    # legacy env-var compatibility (see comment on that map). Do not remove.
    return {
        "default": ModelSamplingSettings(),
        MODEL_TASK_STORY_DISCOVERY: ModelSamplingSettings(),
        MODEL_TASK_STORY_SCALE_SCREENING: ModelSamplingSettings(),
        MODEL_TASK_ARTICLE_SUMMARY: ModelSamplingSettings(),
        MODEL_TASK_STORY_DRAFTING: ModelSamplingSettings(),
        MODEL_TASK_TITLE_GENERATION: ModelSamplingSettings(),
        "reasoning": ModelSamplingSettings(),
    }


def _configured_model_server_settings(base_url: str | None = None) -> ModelServerSettings:
    return ModelServerSettings(
        base_url=base_url or _str_env("NEWS_MODEL_BASE_URL", "http://127.0.0.1:8080/v1"),
        prefill_step_size=_int_env("NEWS_MODEL_SERVER_PREFILL_STEP_SIZE", DEFAULT_MODEL_SERVER_PREFILL_STEP_SIZE),
        prompt_cache_size=_int_env("NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE", DEFAULT_MODEL_SERVER_PROMPT_CACHE_SIZE),
        prompt_cache_bytes=_str_env("NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES", DEFAULT_MODEL_SERVER_PROMPT_CACHE_BYTES)
        or DEFAULT_MODEL_SERVER_PROMPT_CACHE_BYTES,
        max_tokens=_int_env("NEWS_MODEL_SERVER_MAX_TOKENS", DEFAULT_MODEL_SERVER_MAX_TOKENS),
    )


def _configured_pipeline_budget() -> PipelineBudget:
    return PipelineBudget(
        article_text_token_limit=max(
            500,
            _int_env("NEWS_ARTICLE_TEXT_TOKEN_LIMIT", DEFAULT_ARTICLE_TEXT_TOKEN_LIMIT),
        ),
        total_article_summary_cap=max(
            0,
            _int_env("NEWS_TOTAL_ARTICLE_SUMMARY_CAP", DEFAULT_TOTAL_ARTICLE_SUMMARY_CAP),
        ),
        recent_window_hours=_int_env("NEWS_RECENT_WINDOW_HOURS", 24),
        max_articles_per_source=_int_env("NEWS_MAX_ARTICLES_PER_SOURCE", 4),
        min_articles_per_story=configured_min_articles_per_story(),
        max_stories=max(1, _int_env("NEWS_MAX_STORIES", 4)),
        story_cluster_similarity_threshold=configured_story_cluster_similarity_threshold(),
        story_selection_overlap_threshold=_bounded_env_float(
            "NEWS_STORY_SELECTION_OVERLAP_THRESHOLD",
            0.25,
        ),
        story_embedding_dedup_threshold=_bounded_env_float(
            "NEWS_STORY_DEDUP_THRESHOLD",
            0.85,
        ),
        story_backfill_batch_multiplier=max(
            1,
            _int_env("NEWS_STORY_BACKFILL_BATCH_MULTIPLIER", 2),
        ),
    )


def _task_assignment_entry(
    task: str,
    *,
    reference: str,
    base_url: str,
    presets: dict[str, dict[str, Any]],
    model_concurrency: int,
) -> TaskModelAssignment:
    name = resolve_model_name(reference)
    backend = infer_model_backend(reference)
    tuning = _configured_model_tuning(reference, task=task, presets=presets)
    return TaskModelAssignment(
        task=task,
        reference=reference,
        name=name,
        backend=backend,
        base_url=base_url,
        server_command=build_model_server_command(
            name,
            _configured_model_server_settings(base_url),
            backend=backend,
            model_concurrency=model_concurrency,
        ),
        tuning=tuning,
    )


def _configured_model_assignments(
    *,
    default_reference: str,
    default_tuning: ModelTuningSettings,
    default_server_settings: ModelServerSettings,
    model_concurrency: int,
    presets: dict[str, dict[str, Any]],
) -> dict[str, TaskModelAssignment]:
    default_name = resolve_model_name(default_reference)
    default_backend = _configured_model_backend(default_reference)
    default_server_command = build_model_server_command(
        default_name,
        default_server_settings,
        backend=default_backend,
        model_concurrency=model_concurrency,
    )

    task_env_suffixes = {
        MODEL_TASK_ARTICLE_SUMMARY: "ARTICLE_SUMMARY",
        MODEL_TASK_STORY_DRAFTING: "STORY_DRAFTING",
        MODEL_TASK_STORY_SCALE_SCREENING: "STORY_SCALE_SCREENING",
        MODEL_TASK_TITLE_GENERATION: "TITLE_GENERATION",
    }
    task_entries = {}
    for task, env_suffix in task_env_suffixes.items():
        reference = _str_env(f"NEWS_MODEL_{env_suffix}", default_reference) or default_reference
        base_url = _str_env(
            f"NEWS_MODEL_{env_suffix}_BASE_URL",
            default_server_settings.base_url,
        ) or default_server_settings.base_url
        task_entries[task] = _task_assignment_entry(
            task,
            reference=reference,
            base_url=base_url,
            presets=presets,
            model_concurrency=model_concurrency,
        )
    return {
        "default": TaskModelAssignment(
            task="default",
            reference=default_reference,
            name=default_name,
            backend=default_backend,
            base_url=default_server_settings.base_url,
            server_command=default_server_command,
            tuning=default_tuning,
        ),
        **task_entries,
    }


def _optional_int_env(name: str, environ: Mapping[str, str] | None = None) -> int | None:
    raw = (environ or _active_env()).get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(
            f"Invalid integer value for {name!r}: {raw.strip()!r}. "
            "Expected a whole number (e.g. 700)."
        ) from None


def _optional_float_env(name: str, environ: Mapping[str, str] | None = None) -> float | None:
    raw = (environ or _active_env()).get(name)
    if raw is None or not raw.strip():
        return None
    return float(raw.strip())


def _merge_model_sampling_settings(
    base: ModelSamplingSettings,
    overlay: ModelSamplingSettings,
) -> ModelSamplingSettings:
    return ModelSamplingSettings(
        temperature=overlay.temperature if overlay.temperature is not None else base.temperature,
        top_p=overlay.top_p if overlay.top_p is not None else base.top_p,
        top_k=overlay.top_k if overlay.top_k is not None else base.top_k,
        min_p=overlay.min_p if overlay.min_p is not None else base.min_p,
        presence_penalty=(
            overlay.presence_penalty
            if overlay.presence_penalty is not None
            else base.presence_penalty
        ),
        repetition_penalty=(
            overlay.repetition_penalty
            if overlay.repetition_penalty is not None
            else base.repetition_penalty
        ),
    )


def _merge_model_tuning_settings(
    base: ModelTuningSettings,
    overlay: ModelTuningSettings,
) -> ModelTuningSettings:
    task_sampling = dict(base.task_sampling)
    for task, overlay_sampling in overlay.task_sampling.items():
        task_sampling[task] = _merge_model_sampling_settings(
            task_sampling.get(task, ModelSamplingSettings()),
            overlay_sampling,
        )
    return ModelTuningSettings(
        model_max_input_tokens=(
            overlay.model_max_input_tokens
            if overlay.model_max_input_tokens is not None
            else base.model_max_input_tokens
        ),
        article_summary_max_tokens=(
            overlay.article_summary_max_tokens
            if overlay.article_summary_max_tokens is not None
            else base.article_summary_max_tokens
        ),
        story_drafting_max_tokens=(
            overlay.story_drafting_max_tokens
            if overlay.story_drafting_max_tokens is not None
            else base.story_drafting_max_tokens
        ),
        story_scale_screening_max_tokens=(
            overlay.story_scale_screening_max_tokens
            if overlay.story_scale_screening_max_tokens is not None
            else base.story_scale_screening_max_tokens
        ),
        title_generation_max_tokens=(
            overlay.title_generation_max_tokens
            if overlay.title_generation_max_tokens is not None
            else base.title_generation_max_tokens
        ),
        task_sampling=task_sampling,
    )


def _base_model_tuning(model_reference: str) -> ModelTuningSettings:
    resolved_name = resolve_model_name(model_reference)
    tuning = ModelTuningSettings(
        model_max_input_tokens=DEFAULT_MODEL_MAX_INPUT_TOKENS,
        article_summary_max_tokens=DEFAULT_ARTICLE_SUMMARY_MAX_TOKENS,
        story_drafting_max_tokens=DEFAULT_STORY_DRAFTING_MAX_TOKENS,
        story_scale_screening_max_tokens=DEFAULT_STORY_SCALE_SCREENING_MAX_TOKENS,
        title_generation_max_tokens=DEFAULT_TITLE_GENERATION_MAX_TOKENS,
        task_sampling=_empty_model_sampling_map(),
    )
    model_default_tuning = MODEL_SPECIFIC_TUNING_DEFAULTS.get(resolved_name)
    if model_default_tuning is not None:
        tuning = _merge_model_tuning_settings(tuning, model_default_tuning)
    return tuning


def _task_max_tokens_field(task: str) -> str:
    if task == "default":
        return "model_max_input_tokens"
    if task == MODEL_TASK_ARTICLE_SUMMARY:
        return "article_summary_max_tokens"
    if task == MODEL_TASK_STORY_DRAFTING:
        return "story_drafting_max_tokens"
    if task == MODEL_TASK_STORY_SCALE_SCREENING:
        return "story_scale_screening_max_tokens"
    if task == MODEL_TASK_TITLE_GENERATION:
        return "title_generation_max_tokens"
    # image_art_direction is produced by the same LLM call as title_generation
    # (generate_image_art_brief); it inherits that task's token cap by design.
    if task == MODEL_TASK_IMAGE_ART_DIRECTION:
        return "title_generation_max_tokens"
    raise ValueError(f"Unsupported model tuning task {task!r}.")


def _selected_model_tuning_preset_id(task: str) -> str:
    env_var = MODEL_TUNING_PRESET_ENV_VARS.get(task)
    if not env_var:
        return ""
    return normalize_preset_id(_str_env(env_var, ""))


def _validate_model_tuning_preset_scope(
    *,
    preset_id: str,
    preset: Mapping[str, Any],
    assignment_reference: str,
    assignment_name: str,
    assignment_task: str,
) -> None:
    preset_model = str(preset.get("model") or "").strip()
    if preset_model and preset_model not in {assignment_reference, assignment_name}:
        raise ValueError(
            f"Model tuning preset {preset_id!r} expects model {preset_model!r}, "
            f"but configured model is {assignment_reference!r} ({assignment_name!r})."
        )
    preset_task = str(preset.get("task") or "").strip()
    if assignment_task == "default":
        if preset_task:
            raise ValueError(
                f"Model tuning preset {preset_id!r} is scoped to task {preset_task!r}, "
                "but the default model assignment does not accept a task scope."
            )
        return
    if preset_task and preset_task != assignment_task:
        raise ValueError(
            f"Model tuning preset {preset_id!r} expects task {preset_task!r}, "
            f"but configured task is {assignment_task!r}."
        )


def _apply_model_tuning_preset(
    tuning: ModelTuningSettings,
    *,
    preset_id: str,
    preset: Mapping[str, Any],
    assignment_task: str,
) -> ModelTuningSettings:
    raw_tuning = preset.get("tuning", {})
    if raw_tuning in (None, ""):
        raw_tuning = {}
    if not isinstance(raw_tuning, dict):
        raise ValueError(f"Model tuning preset {preset_id!r} tuning must be a mapping.")

    updates: dict[str, Any] = {}
    task_sampling = dict(tuning.task_sampling)
    target_sampling = task_sampling.get(assignment_task, ModelSamplingSettings())
    for key, value in raw_tuning.items():
        field_name = str(key).strip()
        if field_name in {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"}:
            target_sampling = _merge_model_sampling_settings(
                target_sampling,
                _sampling_settings_from_mapping({field_name: value}),
            )
            continue
        if field_name == "max_tokens":
            field_name = _task_max_tokens_field(assignment_task)
        if field_name not in {
            "model_max_input_tokens",
            "article_summary_max_tokens",
            "story_drafting_max_tokens",
            "story_scale_screening_max_tokens",
            "title_generation_max_tokens",
        }:
            raise ValueError(
                f"Unsupported tuning field {key!r} in model tuning preset {preset_id!r}."
            )
        try:
            coerced_value = _coerce_optional_int_value(value)
        except (TypeError, ValueError):
            coerced_value = None
        if coerced_value is None:
            raise ValueError(
                f"Model tuning preset {preset_id!r} field {key!r} must be a number, "
                f"got {value!r}."
            )
        updates[field_name] = coerced_value

    task_sampling[assignment_task] = target_sampling
    updates["task_sampling"] = task_sampling
    return replace(tuning, **updates)


def _override_sampling_from_env(
    settings: ModelSamplingSettings,
    *,
    prefix: str,
) -> ModelSamplingSettings:
    return _merge_model_sampling_settings(
        settings,
        ModelSamplingSettings(
            temperature=_optional_float_env(f"{prefix}_TEMPERATURE"),
            top_p=_optional_float_env(f"{prefix}_TOP_P"),
            top_k=_optional_int_env(f"{prefix}_TOP_K"),
            min_p=_optional_float_env(f"{prefix}_MIN_P"),
            presence_penalty=_optional_float_env(f"{prefix}_PRESENCE_PENALTY"),
            repetition_penalty=_optional_float_env(f"{prefix}_REPETITION_PENALTY"),
        ),
    )


def _sampling_settings_from_mapping(raw: Mapping[str, Any] | None) -> ModelSamplingSettings:
    payload = raw or {}
    if not isinstance(payload, Mapping):
        raise ValueError("Model sampling settings must be a mapping.")
    return ModelSamplingSettings(
        temperature=_coerce_optional_float_value(payload.get("temperature")),
        top_p=_coerce_optional_float_value(payload.get("top_p")),
        top_k=_coerce_optional_int_value(payload.get("top_k")),
        min_p=_coerce_optional_float_value(payload.get("min_p")),
        presence_penalty=_coerce_optional_float_value(payload.get("presence_penalty")),
        repetition_penalty=_coerce_optional_float_value(payload.get("repetition_penalty")),
    )


def _coerce_optional_int_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return int(value)


def _coerce_optional_float_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return float(value)


def _apply_model_tuning_env_overrides(tuning: ModelTuningSettings) -> ModelTuningSettings:
    task_sampling = {
        task: _override_sampling_from_env(
            tuning.task_sampling.get(task, ModelSamplingSettings()),
            prefix=prefix,
        )
        for task, prefix in MODEL_TASK_SAMPLING_ENV_PREFIXES.items()
    }
    task_sampling["reasoning"] = _override_sampling_from_env(
        tuning.task_sampling.get("reasoning", ModelSamplingSettings()),
        prefix=MODEL_REASONING_SAMPLING_ENV_PREFIX,
    )
    model_max_input_tokens = _optional_int_env("NEWS_MODEL_MAX_INPUT_TOKENS")
    article_summary_max_tokens = _optional_int_env("NEWS_ARTICLE_SUMMARY_MAX_TOKENS")
    story_drafting_max_tokens = _optional_int_env("NEWS_STORY_DRAFTING_MAX_TOKENS")
    story_scale_screening_max_tokens = _optional_int_env("NEWS_STORY_SCALE_SCREENING_MAX_TOKENS")
    title_generation_max_tokens = _optional_int_env("NEWS_TITLE_GENERATION_MAX_TOKENS")
    return ModelTuningSettings(
        model_max_input_tokens=(
            tuning.model_max_input_tokens
            if model_max_input_tokens is None
            else model_max_input_tokens
        ),
        article_summary_max_tokens=(
            tuning.article_summary_max_tokens
            if article_summary_max_tokens is None
            else article_summary_max_tokens
        ),
        story_drafting_max_tokens=(
            tuning.story_drafting_max_tokens
            if story_drafting_max_tokens is None
            else story_drafting_max_tokens
        ),
        story_scale_screening_max_tokens=(
            tuning.story_scale_screening_max_tokens
            if story_scale_screening_max_tokens is None
            else story_scale_screening_max_tokens
        ),
        title_generation_max_tokens=(
            tuning.title_generation_max_tokens
            if title_generation_max_tokens is None
            else title_generation_max_tokens
        ),
        task_sampling=task_sampling,
    )


def _configured_model_tuning(
    model_reference: str,
    *,
    task: str = "default",
    preset_id: str | None = None,
    presets: dict[str, dict[str, Any]] | None = None,
) -> ModelTuningSettings:
    tuning = _base_model_tuning(model_reference)
    selected_preset_id = (
        normalize_preset_id(preset_id)
        if preset_id is not None
        else _selected_model_tuning_preset_id(task)
    )
    if selected_preset_id:
        preset_records = presets if presets is not None else load_model_tuning_presets()
        preset = preset_records.get(selected_preset_id)
        if preset is None:
            valid = ", ".join(sorted(preset_records)) or "none configured"
            raise ValueError(
                f"Unknown model tuning preset {selected_preset_id!r}. Available presets: {valid}."
            )
        assignment_name = resolve_model_name(model_reference)
        _validate_model_tuning_preset_scope(
            preset_id=selected_preset_id,
            preset=preset,
            assignment_reference=model_reference,
            assignment_name=assignment_name,
            assignment_task=task,
        )
        tuning = _apply_model_tuning_preset(
            tuning,
            preset_id=selected_preset_id,
            preset=preset,
            assignment_task=task,
        )
    return _apply_model_tuning_env_overrides(tuning)


def _active_env() -> Mapping[str, str]:
    return _CONFIG_ENV.get() or os.environ


def _bool_env(name: str, default: bool, environ: Mapping[str, str] | None = None) -> bool:
    raw = (environ or _active_env()).get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, environ: Mapping[str, str] | None = None) -> int:
    raw = (environ or _active_env()).get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _float_env(name: str, default: float, environ: Mapping[str, str] | None = None) -> float:
    raw = (environ or _active_env()).get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _str_env(name: str, default: str, environ: Mapping[str, str] | None = None) -> str:
    raw = (environ or _active_env()).get(name)
    return default if raw is None else raw.strip()


def normalize_preset_id(value: str | None) -> str:
    return str(value or "").strip()


def load_run_presets(path: Path | None = None) -> dict[str, dict[str, Any]]:
    presets_path = path or RUN_PRESETS_PATH
    payload = _load_yaml_mapping(presets_path)
    raw_presets = payload.get("presets", {})
    if not isinstance(raw_presets, dict):
        raise ValueError(f"{presets_path} must define presets as a mapping.")

    presets: dict[str, dict[str, Any]] = {}
    for raw_id, raw_preset in raw_presets.items():
        preset_id = normalize_preset_id(str(raw_id))
        if not preset_id or not isinstance(raw_preset, dict):
            continue
        raw_env = raw_preset.get("env", {})
        if not isinstance(raw_env, dict):
            raise ValueError(f"Preset {preset_id!r} env must be a mapping.")
        env = {
            str(name).strip(): str(value).strip()
            for name, value in raw_env.items()
            if str(name).strip() and value is not None and str(value).strip() != ""
        }
        presets[preset_id] = {
            "id": preset_id,
            "name": str(raw_preset.get("name") or preset_id).strip(),
            "description": str(raw_preset.get("description") or "").strip(),
            "env": env,
        }
        modified_at = str(raw_preset.get("modified_at") or "").strip()
        if modified_at:
            presets[preset_id]["modified_at"] = modified_at
    return presets


def load_model_tuning_presets(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load saved model tuning presets.

    Preset YAML shape:

    presets:
      concise-story-drafting:
        model: mlx-community/example-model
        task: story_drafting
        tuning:
          temperature: 0.2
          top_p: 0.9
          max_tokens: 1400
    """
    presets_path = path or MODEL_TUNING_PRESETS_PATH
    payload = _load_yaml_mapping(presets_path)
    raw_presets = payload.get("presets", {})
    if not isinstance(raw_presets, dict):
        raise ValueError(f"{presets_path} must define presets as a mapping.")

    presets: dict[str, dict[str, Any]] = {}
    for raw_id, raw_preset in raw_presets.items():
        preset_id = normalize_preset_id(str(raw_id))
        if not preset_id or not isinstance(raw_preset, dict):
            continue
        raw_tuning = raw_preset.get("tuning", {})
        if raw_tuning in (None, ""):
            raw_tuning = {}
        if not isinstance(raw_tuning, dict):
            raise ValueError(f"Model tuning preset {preset_id!r} tuning must be a mapping.")
        preset: dict[str, Any] = {"id": preset_id, "tuning": dict(raw_tuning)}
        for field_name in ("name", "description", "modified_at"):
            field_value = str(raw_preset.get(field_name) or "").strip()
            if field_value:
                preset[field_name] = field_value
        raw_model = str(raw_preset.get("model") or "").strip()
        raw_task = str(raw_preset.get("task") or "").strip()
        if raw_model:
            preset["model"] = raw_model
        if raw_task:
            preset["task"] = raw_task
        presets[preset_id] = preset
    return presets


def run_preset_env(preset_id: str, path: Path | None = None) -> dict[str, str]:
    normalized = normalize_preset_id(preset_id)
    presets = load_run_presets(path)
    if normalized not in presets:
        valid = ", ".join(sorted(presets)) or "none configured"
        raise ValueError(f"Unknown run preset {preset_id!r}. Available presets: {valid}.")
    return dict(presets[normalized].get("env") or {})


def _configured_preset_id() -> str:
    return normalize_preset_id(_str_env(PRESET_ENV_VAR, ""))


def apply_run_preset_to_environment(
    preset_id: str | None = None,
    *,
    override_existing: bool = False,
) -> str:
    normalized = normalize_preset_id(preset_id) if preset_id is not None else _configured_preset_id()
    if not normalized:
        return ""
    previous_preset_id = normalize_preset_id(os.getenv(ACTIVE_PRESET_ENV_VAR))
    previous_env: dict[str, str] = {}
    if previous_preset_id and previous_preset_id != normalized:
        try:
            previous_env = run_preset_env(previous_preset_id)
        except ValueError:
            previous_env = {}
    preset_env = run_preset_env(normalized)
    for name, value in preset_env.items():
        current_value = os.getenv(name)
        came_from_previous_preset = (
            bool(previous_env)
            and name in previous_env
            and current_value == previous_env.get(name)
        )
        if override_existing or not current_value or came_from_previous_preset:
            os.environ[name] = value
    os.environ[PRESET_ENV_VAR] = normalized
    os.environ[ACTIVE_PRESET_ENV_VAR] = normalized
    return normalized


def configured_min_articles_per_story() -> int:
    return max(2, _int_env("NEWS_MIN_ARTICLES_PER_STORY", 4))


def configured_story_cluster_similarity_threshold() -> float:
    return min(
        1.0,
        max(0.0, _float_env("NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD", 0.27)),
    )


def _bounded_env_float(
    name: str,
    default: float,
    *,
    lower: float = 0.0,
    upper: float = 1.0,
) -> float:
    try:
        value = _float_env(name, default)
    except ValueError:
        value = default
    return min(upper, max(lower, value))


def resolve_model_name(model_reference: str) -> str:
    """Resolve a friendly model alias to the Hugging Face repo loaded by mlx."""
    clean_reference = (model_reference or "").strip()
    if not clean_reference:
        clean_reference = DEFAULT_MODEL_ALIAS
    if clean_reference in UNSUPPORTED_MODEL_REFERENCES:
        raise ValueError(f"Unsupported model reference: {clean_reference}")
    return MODEL_ALIASES.get(clean_reference, clean_reference)


def hf_model_page_url(model_choice: str) -> str | None:
    """Return the Hugging Face model-page URL for a model choice, or None.

    Aliases and https://huggingface.co/... / https://hf.co/... URL keys in
    MODEL_ALIASES are normalized through resolve_model_name first. Only
    repos derived from MODEL_ALIASES values are ever emitted: external ids,
    unknown URL-shaped ids, and ids without an owner/name shape (no "/")
    all yield None so callers can render a muted note instead of a broken
    link. Unsupported references also yield None rather than raising.
    """
    clean = (model_choice or "").strip()
    if not clean:
        return None
    try:
        resolved = resolve_model_name(clean)
    except ValueError:
        return None
    if resolved not in MODEL_ALIASES.values():
        return None
    # Defense in depth: URL-shaped alias values would double-prefix below,
    # so never emit them (the drift-guard test pins the same invariant).
    if resolved.startswith(("http://", "https://")):
        return None
    repo = resolved
    # GGUF references are repo + "/" + file; the page lives at the repo.
    if repo.endswith(".gguf") and "/" in repo:
        repo = repo.rsplit("/", 1)[0]
    if "/" not in repo:  # external/unknown ids have no HF repo page
        return None
    return f"https://huggingface.co/{repo}"


def _model_option_links() -> dict[str, dict[str, str]]:
    """Map every MODEL_ALIASES key that resolves to a Hugging Face repo to
    its HF page + hardware links.

    Both values are the same URL: Hugging Face's native Hardware
    Compatibility panel is embedded in each model page (typically shown for
    repos offering GGUF or MLX files), so the model-page URL is the single
    correct destination. The two keys exist so a future HF anchor (e.g.
    "#hardware") is a one-line change here.
    """
    links: dict[str, dict[str, str]] = {}
    for option in MODEL_ALIASES:
        url = hf_model_page_url(option)
        if url is None:
            continue
        links[option] = {"page": url, "hardware": url}
    return links


def is_codex_test_model_reference(model_reference: str) -> bool:
    clean_reference = (model_reference or "").strip()
    return (
        clean_reference == CODEX_TEST_MODEL_ALIAS
        or resolve_model_name(clean_reference) == CODEX_TEST_MODEL_NAME
    )


def is_gemma_4_model_reference(model_reference: str) -> bool:
    clean_reference = (model_reference or "").strip()
    resolved_name = resolve_model_name(clean_reference)
    return any(
        "gemma-4" in value.lower() or "gemma4" in value.lower()
        for value in (clean_reference, resolved_name)
    )


def codex_model_guard_active() -> bool:
    return any(_str_env(name, "") for name in CODEX_RUNTIME_ENV_VARS)


def ensure_codex_safe_model_reference(model_reference: str) -> None:
    if not codex_model_guard_active():
        return
    if is_codex_test_model_reference(model_reference):
        return
    raise RuntimeError(
        "Codex model guard is active; Codex-run verification may only use "
        f"{CODEX_TEST_MODEL_ALIAS} ({CODEX_TEST_MODEL_NAME}). "
        "Set NEWS_CODEX_TESTING=1 before model-related checks."
    )


def _configured_model_reference() -> str:
    if _bool_env("NEWS_CODEX_TESTING", False):
        return CODEX_TEST_MODEL_ALIAS
    selected_model = _str_env("NEWS_MODEL", "")
    return selected_model or DEFAULT_MODEL_ALIAS




def infer_model_backend(model_reference: str) -> str:
    resolved_name = resolve_model_name(model_reference).lower()
    if "qwythos" in resolved_name or "gemma-4" in resolved_name or "gemma4" in resolved_name:
        return MODEL_BACKEND_MLX_VLM
    return MODEL_BACKEND_MLX_LM


def configured_model_api_key() -> str:
    """Return the API key used for OpenAI-compatible endpoints.

    Reads NEWS_MODEL_API_KEY; defaults to the unauthenticated "not-needed"
    sentinel so local managed servers keep working without configuration.
    """
    return _str_env("NEWS_MODEL_API_KEY", "not-needed") or "not-needed"


def _configured_model_backend(model_reference: str) -> str:
    """Resolve the default model's backend: NEWS_MODEL_BACKEND override
    (validated against SUPPORTED_MODEL_BACKENDS) or inferred from the
    reference. Per-task models always use inference."""
    configured = _str_env("NEWS_MODEL_BACKEND", "").strip().lower()
    if not configured:
        return infer_model_backend(model_reference)
    if configured not in SUPPORTED_MODEL_BACKENDS:
        raise ValueError(
            "NEWS_MODEL_BACKEND must be one of: " + ", ".join(SUPPORTED_MODEL_BACKENDS)
        )
    if configured == MODEL_BACKEND_EXTERNAL and not _str_env("NEWS_MODEL_BASE_URL", ""):
        raise ValueError(
            "NEWS_MODEL_BACKEND=external requires NEWS_MODEL_BASE_URL to point at an "
            "OpenAI-compatible endpoint (without it, the pipeline would poll the "
            "default managed-server loopback URL and time out)."
        )
    return configured




def _default_story_synthesis_concurrency(model_reference: str) -> int:
    if is_codex_test_model_reference(model_reference):
        return 2
    resolved_name = resolve_model_name(model_reference).lower()
    if "qwythos" in resolved_name or is_gemma_4_model_reference(model_reference):
        return 1
    return DEFAULT_STORY_SYNTHESIS_CONCURRENCY


def _default_article_summary_concurrency(model_reference: str) -> int:
    if is_codex_test_model_reference(model_reference):
        return 8
    return DEFAULT_ARTICLE_SUMMARY_CONCURRENCY



def _runtime_knob(
    group: str,
    label: str,
    env: str,
    value_type: str = "text",
    *,
    default: str | int | float | bool | None = None,
    options: list[str] | None = None,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    step: int | float | None = None,
    advanced: bool = False,
    secret: bool = False,
    option_links: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": env.lower(),
        "group": group,
        "label": label,
        "env": env,
        "type": value_type,
        "default": default,
        "options": options or [],
        "min": minimum,
        "max": maximum,
        "step": step,
        "advanced": advanced,
        "secret": secret,
        # option_links maps each offered option -> {"page": url, "hardware":
        # url} for the three model-choice knobs; the drift-guard test pins
        # that its keys cover `options` exactly. Non-model knobs omit it and
        # fall back to {}.
        "option_links": option_links or {},
    }


def runtime_knob_registry() -> list[dict[str, Any]]:
    tuning_presets = sorted(load_model_tuning_presets())
    model_links = _model_option_links()
    knobs = [
        _runtime_knob("Run Settings", "Source scope", "NEWS_SOURCE_SCOPE", "select", default="core", options=list(SOURCE_SCOPES)),
        _runtime_knob("Run Settings", "Recipient scope", "NEWS_RECIPIENT_SCOPE", "select", default="primary", options=list(RECIPIENT_SCOPES)),
        _runtime_knob("Run Settings", "Block reused URLs", "NEWS_BLOCK_REUSED_URLS", "bool", default=False),
        _runtime_knob("Run Settings", "Image generation", "NEWS_IMAGE_ENABLED", "bool", default=False),
        _runtime_knob("Run Settings", "Story scale screening", "NEWS_STORY_SCALE_SCREENING_ENABLED", "bool"),
        _runtime_knob(
            "Run Settings",
            "Prompt profile",
            PROMPT_PROFILE_ENV_VAR,
            "select",
            default=DEFAULT_PROMPT_PROFILE_ID,
            options=list(PROMPT_PROFILE_IDS),
        ),
        *[
            _runtime_knob(
                "Run Settings",
                f"Prompt override ({task.replace('_', ' ')})",
                env_var,
                "text",
                advanced=True,
            )
            for task, env_var in PROMPT_TASK_OVERRIDE_ENV_VARS.items()
        ],
        _runtime_knob("Run Settings", "Relax story drafting guards", "NEWS_RELAX_STORY_DRAFTING_GUARDS", "bool", advanced=True),
        _runtime_knob("Run Settings", "Embedding model", "NEWS_EMBEDDING_MODEL", default="all-mpnet-base-v2", advanced=True),
        _runtime_knob("Run Settings", "Token encoding", "NEWS_TOKEN_ENCODING", default="o200k_base", advanced=True),
        _runtime_knob("Model Selection", "Default model", "NEWS_MODEL", "select", default=DEFAULT_MODEL_ALIAS, options=sorted(MODEL_ALIASES), option_links=model_links),
        _runtime_knob("Model Selection", "Model backend", "NEWS_MODEL_BACKEND", "select", options=sorted(SUPPORTED_MODEL_BACKENDS)),
        _runtime_knob("Model Selection", "Article Summarization model", "NEWS_MODEL_ARTICLE_SUMMARY", "select", options=sorted(MODEL_ALIASES), option_links=model_links),
        _runtime_knob("Model Selection", "Story Drafting model", "NEWS_MODEL_STORY_DRAFTING", "select", options=sorted(MODEL_ALIASES), option_links=model_links),
        _runtime_knob("Model Selection", "Story Scale Screening model", "NEWS_MODEL_STORY_SCALE_SCREENING", "select", options=sorted(MODEL_ALIASES), option_links=model_links),
        _runtime_knob("Model Selection", "Title Generation model", "NEWS_MODEL_TITLE_GENERATION", "select", options=sorted(MODEL_ALIASES), option_links=model_links),
        _runtime_knob("Model Tuning", "Default tuning preset", "NEWS_MODEL_TUNING_PRESET", "select", options=tuning_presets),
        _runtime_knob("Model Tuning", "Article Summarization tuning preset", "NEWS_MODEL_ARTICLE_SUMMARY_TUNING_PRESET", "select", options=tuning_presets),
        _runtime_knob("Model Tuning", "Story Drafting tuning preset", "NEWS_MODEL_STORY_DRAFTING_TUNING_PRESET", "select", options=tuning_presets),
        _runtime_knob("Model Tuning", "Story Scale Screening tuning preset", "NEWS_MODEL_STORY_SCALE_SCREENING_TUNING_PRESET", "select", options=tuning_presets),
        _runtime_knob("Model Tuning", "Title Generation tuning preset", "NEWS_MODEL_TITLE_GENERATION_TUNING_PRESET", "select", options=tuning_presets),
        _runtime_knob("Model Tuning", "Model input cap", "NEWS_MODEL_MAX_INPUT_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Model Tuning", "Article summary max tokens", "NEWS_ARTICLE_SUMMARY_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Model Tuning", "Story drafting max tokens", "NEWS_STORY_DRAFTING_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Model Tuning", "Story scale screening max tokens", "NEWS_STORY_SCALE_SCREENING_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Model Tuning", "Title generation max tokens", "NEWS_TITLE_GENERATION_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Article text token limit", "NEWS_ARTICLE_TEXT_TOKEN_LIMIT", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Total article summary cap", "NEWS_TOTAL_ARTICLE_SUMMARY_CAP", "number", minimum=0, step=1),
        _runtime_knob("Pipeline Budget", "Recent window hours", "NEWS_RECENT_WINDOW_HOURS", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Max articles per source", "NEWS_MAX_ARTICLES_PER_SOURCE", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Min articles per story", "NEWS_MIN_ARTICLES_PER_STORY", "number", minimum=2, step=1),
        _runtime_knob("Pipeline Budget", "Max stories", "NEWS_MAX_STORIES", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Story cluster similarity", "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Pipeline Budget", "Story selection overlap", "NEWS_STORY_SELECTION_OVERLAP_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Pipeline Budget", "Story dedup threshold", "NEWS_STORY_DEDUP_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Pipeline Budget", "Backfill batch multiplier", "NEWS_STORY_BACKFILL_BATCH_MULTIPLIER", "number", minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Source collection concurrency", "NEWS_SOURCE_COLLECTION_CONCURRENCY", "number", default=DEFAULT_SOURCE_COLLECTION_CONCURRENCY, minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Article summary concurrency", "NEWS_ARTICLE_SUMMARY_CONCURRENCY", "number", default=DEFAULT_ARTICLE_SUMMARY_CONCURRENCY, minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Story synthesis concurrency", "NEWS_STORY_SYNTHESIS_CONCURRENCY", "number", default=DEFAULT_STORY_SYNTHESIS_CONCURRENCY, minimum=1, step=1),
        _runtime_knob("Pipeline Budget", "Component overlap suppress", "NEWS_STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD", "number", minimum=0, maximum=1, step=0.01, advanced=True),
        _runtime_knob("Model Server Settings", "Model concurrency", "NEWS_MODEL_CONCURRENCY", "number", default=DEFAULT_PIPELINE_CONCURRENCY, minimum=1, step=1, advanced=True),
        _runtime_knob("Model Server Settings", "Model base URL", "NEWS_MODEL_BASE_URL", default="http://127.0.0.1:8080/v1"),
        _runtime_knob("Model Server Settings", "Article Summarization base URL", "NEWS_MODEL_ARTICLE_SUMMARY_BASE_URL", default="http://127.0.0.1:8080/v1"),
        _runtime_knob("Model Server Settings", "Story Drafting base URL", "NEWS_MODEL_STORY_DRAFTING_BASE_URL", default="http://127.0.0.1:8080/v1"),
        _runtime_knob("Model Server Settings", "Story Scale Screening base URL", "NEWS_MODEL_STORY_SCALE_SCREENING_BASE_URL", default="http://127.0.0.1:8080/v1"),
        _runtime_knob("Model Server Settings", "Title Generation base URL", "NEWS_MODEL_TITLE_GENERATION_BASE_URL", default="http://127.0.0.1:8080/v1"),
        _runtime_knob("Model Server Settings", "Server prefill step size", "NEWS_MODEL_SERVER_PREFILL_STEP_SIZE", "number", minimum=1, step=1),
        _runtime_knob("Model Server Settings", "Server prompt cache size", "NEWS_MODEL_SERVER_PROMPT_CACHE_SIZE", "number", minimum=0, step=1),
        _runtime_knob("Model Server Settings", "Server prompt cache bytes", "NEWS_MODEL_SERVER_PROMPT_CACHE_BYTES"),
        _runtime_knob("Model Server Settings", "Server max tokens", "NEWS_MODEL_SERVER_MAX_TOKENS", "number", minimum=1, step=1),
    ]
    sampling_suffixes = [
        ("TEMPERATURE", "Temperature", "number", 0, 2, 0.01),
        ("TOP_P", "Top P", "number", 0, 1, 0.01),
        ("TOP_K", "Top K", "number", 0, None, 1),
        ("MIN_P", "Min P", "number", 0, 1, 0.01),
        ("PRESENCE_PENALTY", "Presence penalty", "number", -2, 2, 0.01),
        ("REPETITION_PENALTY", "Repetition penalty", "number", 0, 3, 0.01),
    ]
    sampling_prefixes = {
        **MODEL_TASK_SAMPLING_ENV_PREFIXES,
        "reasoning": "NEWS_MODEL_REASONING",
    }
    for task, prefix in sorted(sampling_prefixes.items()):
        task_label = task.replace("_", " ").title()
        for suffix, suffix_label, value_type, minimum, maximum, step in sampling_suffixes:
            knobs.append(
                _runtime_knob(
                    "Model Tuning",
                    f"{task_label} {suffix_label}",
                    f"{prefix}_{suffix}",
                    value_type,
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                    advanced=True,
                )
            )
    return knobs

def build_model_server_command(
    model_name: str,
    settings: ModelServerSettings,
    *,
    backend: str = "mlx-lm",
    model_concurrency: int = DEFAULT_PIPELINE_CONCURRENCY,
) -> str:
    """Return the managed server command for the backend.

    External backends have no managed server: returns "" (callers treat the
    empty string as "connect to the endpoint directly").
    """
    if backend == MODEL_BACKEND_EXTERNAL:
        return ""
    concurrency = max(1, int(model_concurrency))
    parsed_base_url = urlparse(settings.base_url or "")
    port = parsed_base_url.port or 8080
    extra_flags: list[str] = []
    if settings.prefill_step_size is not None:
        extra_flags.extend(["--prefill-step-size", str(settings.prefill_step_size)])
    if backend != MODEL_BACKEND_MLX_VLM:
        if settings.prompt_cache_size is not None:
            extra_flags.extend(["--prompt-cache-size", str(settings.prompt_cache_size)])
        if settings.prompt_cache_bytes:
            extra_flags.extend(["--prompt-cache-bytes", str(settings.prompt_cache_bytes)])
    if settings.max_tokens is not None:
        extra_flags.extend(["--max-tokens", str(settings.max_tokens)])
    if backend == MODEL_BACKEND_MLX_VLM:
        return " ".join(
            [
                "uv run python -m mlx_vlm.server",
                f"--model {model_name}",
                "--host 127.0.0.1",
                f"--port {port}",
                *extra_flags,
                "--log-level INFO",
            ]
        )
    return " ".join(
        [
            "uv run python -m mlx_lm server",
            f"--model {model_name}",
            f"--decode-concurrency {concurrency}",
            f"--prompt-concurrency {concurrency}",
            "--host 127.0.0.1",
            f"--port {port}",
            *extra_flags,
            "--log-level INFO",
        ]
    )


def _coerce_bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_float_value(value: Any, default: float = 1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default




def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return payload


def _load_password_from_env_json(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as env_file:
            payload = json.load(env_file)
        raw_password = str(payload.get("pw", "")).strip()
        return raw_password.replace(" ", "")
    except Exception:
        return ""


def _strip_managed_block(existing: str, start_marker: str, end_marker: str) -> str:
    start_index = existing.find(start_marker)
    end_index = existing.find(end_marker)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        return existing
    end_index += len(end_marker)
    return existing[:start_index].rstrip() + existing[end_index:]


def _latest_run_output_patterns(
    root_dir: Path,
    output_dir: Path,
) -> list[str]:
    try:
        output_parent = output_dir.parent.relative_to(root_dir).as_posix().rstrip("/")
        output_path = output_dir.relative_to(root_dir).as_posix().rstrip("/")
    except ValueError:
        return []
    if not output_dir.exists():
        return []

    latest_paths = (
        output_dir / "latest_run.md",
        output_dir / "latest_run.log",
        output_dir / "latest_run_details.json",
    )
    patterns = [
        "",
        "# Keep generated output context narrowed to rolling run review artifacts.",
        f"!{output_parent}/",
        f"!{output_path}/",
        f"{output_path}/*",
    ]
    for latest_path in latest_paths:
        try:
            file_pattern = latest_path.relative_to(root_dir).as_posix()
        except ValueError:
            return patterns
        patterns.append(f"!{file_pattern}")
    return patterns


def _sync_cursorignore_latest_output(
    root_dir: Path,
    output_dir: Path,
    run_output_dir: Path,
) -> None:
    """Keep assistant context tools focused on core files plus the newest run."""
    del run_output_dir
    latest_output_patterns = _latest_run_output_patterns(root_dir, output_dir)
    managed_lines = [
        ASSISTANT_CONTEXT_MANAGED_START,
        "# Refreshed by news_pipeline.config.load_runtime_config().",
        "# Ignore everything, then re-include core pipeline files plus rolling run artifacts.",
        *ASSISTANT_CONTEXT_CORE_PATTERNS,
        *latest_output_patterns,
        ASSISTANT_CONTEXT_MANAGED_END,
    ]
    managed_block = "\n".join(managed_lines)

    for ignore_name in ASSISTANT_CONTEXT_IGNORE_FILES:
        ignore_path = root_dir / ignore_name
        try:
            existing = ignore_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""

        unmanaged = _strip_managed_block(
            existing,
            CURSORIGNORE_MANAGED_START,
            CURSORIGNORE_MANAGED_END,
        )
        unmanaged = _strip_managed_block(
            unmanaged,
            ASSISTANT_CONTEXT_MANAGED_START,
            ASSISTANT_CONTEXT_MANAGED_END,
        )

        unmanaged = unmanaged.rstrip()
        if unmanaged:
            updated = f"{unmanaged}\n\n{managed_block}\n"
        else:
            updated = f"{managed_block}\n"

        if updated != existing:
            ignore_path.write_text(updated, encoding="utf-8")


def sync_assistant_context_latest_output(config: RuntimeConfig) -> None:
    _sync_cursorignore_latest_output(config.root_dir, config.output_dir, config.run_output_dir)


def _coerce_source_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []

    clean_items: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean_item = str(item or "").strip()
        if not clean_item or clean_item in seen:
            continue
        seen.add(clean_item)
        clean_items.append(clean_item)
    return clean_items


def _normalize_source_tier(value: Any, *, source_key: str | None = None) -> str:
    tier = str(value or "").strip().lower()
    if not tier:
        return PERIPHERAL_SOURCE_TIER
    if tier in {CORE_SOURCE_TIER, PERIPHERAL_SOURCE_TIER}:
        return tier
    source_label = f" for source {source_key!r}" if source_key else ""
    raise ValueError(
        f"Unsupported source tier {value!r}{source_label}; "
        f"expected {CORE_SOURCE_TIER!r} or {PERIPHERAL_SOURCE_TIER!r}."
    )


def _normalize_source_scope(value: Any) -> str:
    scope = str(value or SOURCE_SCOPE_CORE).strip().lower().replace("_", "-")
    aliases = {
        "all": SOURCE_SCOPE_PERIPHERAL,
        "full": SOURCE_SCOPE_PERIPHERAL,
    }
    normalized = aliases.get(scope, scope)
    if normalized not in SOURCE_SCOPES:
        raise ValueError("NEWS_SOURCE_SCOPE must be one of: " + ", ".join(SOURCE_SCOPES))
    return normalized


def _configured_source_scope() -> str:
    return _normalize_source_scope(_str_env("NEWS_SOURCE_SCOPE", SOURCE_SCOPE_CORE))


def _normalize_recipient_scope(value: Any) -> str:
    scope = str(value or RECIPIENT_SCOPE_PRIMARY).strip().lower().replace("_", "-")
    aliases = {
        "single": RECIPIENT_SCOPE_PRIMARY,
        "full": RECIPIENT_SCOPE_ALL,
    }
    normalized = aliases.get(scope, scope)
    if normalized not in RECIPIENT_SCOPES:
        raise ValueError("NEWS_RECIPIENT_SCOPE must be one of: " + ", ".join(RECIPIENT_SCOPES))
    return normalized


def _configured_recipient_scope() -> str:
    return _normalize_recipient_scope(_str_env("NEWS_RECIPIENT_SCOPE", RECIPIENT_SCOPE_PRIMARY))


def _source_enabled_for_scope(
    raw_source: dict[str, Any],
    source_scope: str,
    *,
    source_key: str | None = None,
) -> bool:
    language = str(raw_source.get("language") or "").strip().lower()
    if language != "en":
        return False
    selected_tiers = SOURCE_SCOPE_TIERS[_normalize_source_scope(source_scope)]
    return (
        _normalize_source_tier(raw_source.get("tier"), source_key=source_key)
        in selected_tiers
    )


def _normalize_source_match_mode(value: Any, *, source_key: str) -> str:
    mode = str(value or SOURCE_MATCH_MODE_FEED_LABEL).strip().lower().replace("-", "_")
    if mode not in VALID_SOURCE_MATCH_MODES:
        valid = ", ".join(sorted(VALID_SOURCE_MATCH_MODES))
        raise ValueError(
            f"config/sources.yaml source {source_key!r} source_match_mode must be one of: {valid}."
        )
    return mode


def _reject_removed_source_topic_fields(raw_source: dict[str, Any], *, source_key: str) -> None:
    removed_fields = sorted(REMOVED_SOURCE_TOPIC_FIELDS.intersection(raw_source))
    if removed_fields:
        raise ValueError(
            f"config/sources.yaml source {source_key!r} uses removed topic field(s): "
            + ", ".join(removed_fields)
            + ". This branch uses global story-first source selection."
        )


def load_sources(
    path: Path | None = None,
    *,
    source_scope: str | None = None,
    include_inactive: bool = False,
) -> dict[str, dict[str, Any]]:
    sources_path = path or CONFIG_DIR / "sources.yaml"
    payload = _load_yaml_mapping(sources_path)
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError(f"{sources_path} must define sources as a list.")

    sources: dict[str, dict[str, Any]] = {}
    selected_source_scope = (
        _normalize_source_scope(source_scope)
        if source_scope is not None
        else _configured_source_scope()
    )
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        key = str(raw_source.get("key") or raw_source.get("name") or "").strip()
        url = str(raw_source.get("url") or "").strip()
        if not key or not url:
            continue
        _reject_removed_source_topic_fields(raw_source, source_key=key)
        if not include_inactive and not _source_enabled_for_scope(
            raw_source,
            selected_source_scope,
            source_key=key,
        ):
            continue
        raw_source_match_aliases = raw_source.get("source_match_aliases") or []
        if isinstance(raw_source_match_aliases, str):
            raw_source_match_aliases = [raw_source_match_aliases]
        elif not isinstance(raw_source_match_aliases, list):
            raw_source_match_aliases = []
        language = str(raw_source.get("language") or "").strip().lower()
        tier = _normalize_source_tier(raw_source.get("tier"), source_key=key)

        sources[key] = {
            "name": str(raw_source.get("name") or key).strip(),
            "url": url,
            "homepage": str(raw_source.get("homepage") or "").strip() or None,
            "region": str(raw_source.get("region") or "").strip() or None,
            "language": language or None,
            "tier": tier,
            "nations": _coerce_source_text_list(raw_source.get("nations")),
            "frame": str(raw_source.get("frame") or raw_source.get("region") or "").strip() or None,
            "provider_type": str(raw_source.get("provider_type") or "article_feed").strip(),
            "intended_role": str(raw_source.get("intended_role") or "article enrichment").strip(),
            "weight": _coerce_float_value(raw_source.get("weight"), 1.0),
            "can_enrich_coverage": _coerce_bool_value(
                raw_source.get("can_enrich_coverage", raw_source.get("enrich_coverage")),
                True,
            ),
            "strict_source_match": _coerce_bool_value(raw_source.get("strict_source_match"), False),
            "source_match_mode": _normalize_source_match_mode(
                raw_source.get("source_match_mode"),
                source_key=key,
            ),
            "source_match_aliases": [
                str(alias).strip()
                for alias in raw_source_match_aliases
                if str(alias).strip()
            ],
            "notes": str(raw_source.get("notes") or "").strip() or None,
        }
    if not sources:
        raise ValueError(f"No valid source entries found in {sources_path}.")
    return sources


def load_recipients(path: Path | None = None) -> dict[str, dict[str, Any]]:
    recipients_path = path or CONFIG_DIR / "recipients.yaml"
    payload = _load_yaml_mapping(recipients_path)
    raw_recipients = payload.get("recipients", [])
    if not raw_recipients:
        return {}
    if not isinstance(raw_recipients, list):
        raise ValueError(f"{recipients_path} must define recipients as a list.")

    recipients: dict[str, dict[str, Any]] = {}
    for raw_recipient in raw_recipients:
        if not isinstance(raw_recipient, dict):
            continue
        email = str(raw_recipient.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        recipients[email] = {
            "name": str(raw_recipient.get("name") or email).strip(),
            "pause": _coerce_pause_value(raw_recipient.get("pause")),
        }
    return recipients


def update_recipient_pause_setting(
    target_email: str,
    *,
    pause: bool,
    path: Path | None = None,
) -> int:
    recipients_path = path or CONFIG_DIR / "recipients.yaml"
    payload = _load_yaml_mapping(recipients_path)
    raw_recipients = payload.get("recipients", [])
    if not isinstance(raw_recipients, list):
        return 0

    clean_target = target_email.strip().lower()
    updated_count = 0
    for raw_recipient in raw_recipients:
        if not isinstance(raw_recipient, dict):
            continue
        if str(raw_recipient.get("email") or "").strip().lower() == clean_target:
            raw_recipient["pause"] = pause
            updated_count += 1

    if updated_count:
        with recipients_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    return updated_count


def configured_removed_topic_env_vars(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    values = environ if environ is not None else _active_env()
    return [name for name in REMOVED_TOPIC_ENV_VARS if name in values]


def reject_removed_topic_env_vars() -> None:
    configured = configured_removed_topic_env_vars()
    if configured:
        raise ValueError(
            "Topic-based runtime configuration has been removed from this branch. "
            "Unset removed environment variable(s): "
            + ", ".join(sorted(configured))
        )


def _clean_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    return {
        str(name): str(value)
        for name, value in (environ or {}).items()
        if value is not None
    }


def _runtime_command_env_delta(
    *,
    base_env: Mapping[str, str],
    effective_env: Mapping[str, str],
    preset_env: Mapping[str, str],
    preset_id: str,
) -> dict[str, str]:
    delta: dict[str, str] = {}
    for name, value in effective_env.items():
        if name in PRESET_MARKER_ENV_VARS:
            continue
        if base_env.get(name) == value:
            continue
        if name in preset_env and preset_env.get(name) == value:
            continue
        delta[name] = value
    if preset_id and preset_id != "custom":
        delta.setdefault(PRESET_ENV_VAR, preset_id)
    return delta


def _resolve_effective_env(
    request: RuntimeConfigRequest,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
    base_env = _clean_env(request.base_env if request.base_env is not None else os.environ)
    requested_preset = normalize_preset_id(request.preset_id)
    env_preset = normalize_preset_id(base_env.get(PRESET_ENV_VAR))
    preset_id = requested_preset or env_preset
    preset_env = run_preset_env(preset_id) if preset_id else {}
    effective_env = {**preset_env, **base_env}
    if preset_id:
        effective_env[PRESET_ENV_VAR] = preset_id
        effective_env[ACTIVE_PRESET_ENV_VAR] = preset_id
    if request.overrides:
        effective_env.update(_clean_env(request.overrides))
    if preset_id:
        effective_env[PRESET_ENV_VAR] = preset_id
        effective_env[ACTIVE_PRESET_ENV_VAR] = preset_id
    return preset_id or "custom", base_env, preset_env, effective_env


def _build_runtime_config(
    *,
    preset_id: str,
    materialize_outputs: bool,
    run_started_at: datetime | None,
) -> RuntimeConfig:
    reject_removed_topic_env_vars()
    run_started_at = run_started_at or datetime.now()
    run_date = run_started_at.strftime("%Y-%m-%d")
    timestamp = run_started_at.strftime("%Y-%m-%d_%H-%M-%S")

    sources_path = ROOT_DIR / _str_env("NEWS_SOURCES_YAML", "config/sources.yaml")
    recipients_path = ROOT_DIR / _str_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml")

    output_dir = ROOT_DIR / _str_env("NEWS_OUTPUT_DIR", "output/daily_outputs")
    history_db_path = ROOT_DIR / _str_env("NEWS_HISTORY_DB", "output/history/news_history.duckdb")
    history_export_csv = _bool_env("NEWS_HISTORY_EXPORT_CSV", True)
    run_staging_dir = output_dir / ".staging" / timestamp
    run_output_dir = run_staging_dir
    latest_run_markdown_path = output_dir / "latest_run.md"
    latest_run_log_path = output_dir / "latest_run.log"
    latest_run_details_path = output_dir / "latest_run_details.json"
    if materialize_outputs:
        output_dir.mkdir(parents=True, exist_ok=True)
        history_db_path.parent.mkdir(parents=True, exist_ok=True)
        run_staging_dir.mkdir(parents=True, exist_ok=True)
        _sync_cursorignore_latest_output(ROOT_DIR, output_dir, run_output_dir)

    if _active_env().get("NEWS_BRADLEY_RECIPIENT"):
        print("warning: NEWS_BRADLEY_RECIPIENT is obsolete; "
              "set NEWS_PRIMARY_RECIPIENT instead (see SETTINGS.md)",
              file=sys.stderr)
    primary_recipient = _str_env("NEWS_PRIMARY_RECIPIENT", "primary@example.com")
    source_scope = _configured_source_scope()
    recipient_scope = _configured_recipient_scope()
    url_reuse_blocking_enabled = _bool_env("NEWS_BLOCK_REUSED_URLS", False)
    relaxed_story_drafting_guards = _bool_env("NEWS_RELAX_STORY_DRAFTING_GUARDS", False)
    # Empty-but-present NEWS_PROMPT_PROFILE (a common "unset" idiom in .env
    # files / docker-compose) counts as unset, matching sibling knobs and the
    # CLI/UI semantics. Strict validation of non-empty ids happens below.
    prompt_profile_id = _str_env(PROMPT_PROFILE_ENV_VAR, DEFAULT_PROMPT_PROFILE_ID) or DEFAULT_PROMPT_PROFILE_ID
    # Resolved once at import time in pipeline.py; fails fast on unknown ids.
    get_prompt_profile(prompt_profile_id)
    # Per-stage prompt overrides (NEWS_PROMPT_OVERRIDE_<TASK>): non-empty
    # values only; empty-but-present counts as unset like sibling knobs.
    prompt_instruction_overrides = {
        task: value
        for task, env_var in PROMPT_TASK_OVERRIDE_ENV_VARS.items()
        if (value := _str_env(env_var, "").strip())
    }
    # Editorial sentences must never weaken the pipeline-owned output contracts
    # (parsers, retries, citation renderers, sanitizers depend on them); a
    # violating profile fails fast at config resolution, not mid-run.
    profile_violations = validate_editorial_instructions(get_prompt_profile(prompt_profile_id).prompts)
    if profile_violations:
        raise ValueError(
            f"Prompt profile {prompt_profile_id!r} violates pipeline-owned output contracts: "
            + "; ".join(profile_violations)
        )

    tracked_urls_filename = "tracked_urls.txt"
    blocking_urls_filename = "blocking_urls.txt"
    run_used_urls_filename = (
        blocking_urls_filename if url_reuse_blocking_enabled else tracked_urls_filename
    )
    env_json_path = ROOT_DIR / _str_env("NEWS_ENV_JSON", "env.json")
    email_from = _str_env("NEWS_EMAIL_FROM", "news@example.com")
    smtp_password = (
        _str_env("NEWS_SMTP_PASSWORD", "").replace(" ", "")
        or _load_password_from_env_json(env_json_path)
    )
    fallback_recipients = [
        addr.strip()
        for addr in _str_env("NEWS_EMAIL_RECIPIENTS", primary_recipient).split(",")
        if addr.strip()
    ]

    pipeline_budget = _configured_pipeline_budget()
    default_reference = _configured_model_reference()
    default_tuning = _configured_model_tuning(default_reference, task="default")
    model_base_url = _str_env("NEWS_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    model_server_settings = _configured_model_server_settings(model_base_url)
    article_summary_concurrency = max(
        1,
        _int_env(
            "NEWS_ARTICLE_SUMMARY_CONCURRENCY",
            _default_article_summary_concurrency(default_reference),
        ),
    )
    story_synthesis_concurrency = max(
        1,
        _int_env(
            "NEWS_STORY_SYNTHESIS_CONCURRENCY",
            _default_story_synthesis_concurrency(default_reference),
        ),
    )
    source_collection_concurrency = max(
        1,
        _int_env("NEWS_SOURCE_COLLECTION_CONCURRENCY", DEFAULT_SOURCE_COLLECTION_CONCURRENCY),
    )
    model_concurrency = max(
        1,
        _int_env(
            "NEWS_MODEL_CONCURRENCY",
            max(
                _default_article_summary_concurrency(default_reference),
                _default_story_synthesis_concurrency(default_reference),
                article_summary_concurrency,
                story_synthesis_concurrency,
            ),
        ),
        article_summary_concurrency,
        story_synthesis_concurrency,
    )
    tuning_presets = load_model_tuning_presets()
    model_assignments = _configured_model_assignments(
        default_reference=default_reference,
        default_tuning=default_tuning,
        default_server_settings=model_server_settings,
        model_concurrency=model_concurrency,
        presets=tuning_presets,
    )
    default_model_assignment = model_assignments["default"]
    default_tuning = default_model_assignment.tuning
    model_reference = default_model_assignment.reference
    model_name = default_model_assignment.name
    model_backend = default_model_assignment.backend
    return RuntimeConfig(
        root_dir=ROOT_DIR,
        sources_path=sources_path,
        recipients_path=recipients_path,
        env_json_path=env_json_path,
        run_started_at=run_started_at,
        run_date=run_date,
        timestamp=timestamp,
        output_dir=output_dir,
        run_output_dir=run_output_dir,
        latest_run_markdown_path=latest_run_markdown_path,
        latest_run_log_path=latest_run_log_path,
        latest_run_details_path=latest_run_details_path,
        run_staging_dir=run_staging_dir,
        history_db_path=history_db_path,
        history_export_csv=history_export_csv,
        run_used_urls_path=run_output_dir / run_used_urls_filename,
        preset_id=preset_id,
        prompt_profile_id=prompt_profile_id,
        prompt_instruction_overrides=prompt_instruction_overrides,
        source_scope=source_scope,
        recipient_scope=recipient_scope,
        url_reuse_blocking_enabled=url_reuse_blocking_enabled,
        relaxed_story_drafting_guards=relaxed_story_drafting_guards,
        model_reference=model_reference,
        model_name=model_name,
        model_base_url=model_base_url,
        model_backend=model_backend,
        model_concurrency=model_concurrency,
        article_summary_concurrency=article_summary_concurrency,
        story_synthesis_concurrency=story_synthesis_concurrency,
        source_collection_concurrency=source_collection_concurrency,
        model_max_input_tokens=default_tuning.model_max_input_tokens or DEFAULT_MODEL_MAX_INPUT_TOKENS,
        article_text_token_limit=pipeline_budget.article_text_token_limit,
        total_article_summary_cap=pipeline_budget.total_article_summary_cap,
        article_summary_max_tokens=default_tuning.article_summary_max_tokens
        or DEFAULT_ARTICLE_SUMMARY_MAX_TOKENS,
        story_drafting_max_tokens=default_tuning.story_drafting_max_tokens
        or DEFAULT_STORY_DRAFTING_MAX_TOKENS,
        model_assignments=model_assignments,
        model_tuning=default_tuning,
        pipeline_budget=pipeline_budget,
        model_server_settings=model_server_settings,
        model_server_command=default_model_assignment.server_command,
        recent_window_hours=pipeline_budget.recent_window_hours,
        max_articles_per_source=pipeline_budget.max_articles_per_source,
        primary_recipient=primary_recipient,
        email_recipients_fallback=fallback_recipients,
        email_from=email_from,
        smtp_host=_str_env("NEWS_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_int_env("NEWS_SMTP_PORT", 465),
        smtp_username=_str_env("NEWS_SMTP_USERNAME", email_from),
        smtp_use_ssl=_bool_env("NEWS_SMTP_USE_SSL", True),
        smtp_password=smtp_password,
        unsubscribe_base_url=_str_env(
            "NEWS_UNSUBSCRIBE_BASE_URL",
            "http://127.0.0.1:8765/unsubscribe",
        ),
        unsubscribe_host=_str_env("NEWS_UNSUBSCRIBE_HOST", "127.0.0.1"),
        unsubscribe_port=_int_env("NEWS_UNSUBSCRIBE_PORT", 8765),
        unsubscribe_secret=_str_env("NEWS_UNSUBSCRIBE_SECRET", ""),
        token_encoding_name=_str_env("NEWS_TOKEN_ENCODING", "o200k_base") or "o200k_base",
        image_generation_enabled=_bool_env("NEWS_IMAGE_ENABLED", False),
        image_generation_fail_on_error=False,
        image_width=1024,
        image_height=1024,
        image_steps=4,
        image_crop_bottom_ratio=0.12,
        image_model_id="Runpod/FLUX.2-klein-4B-mflux-4bit",
        image_base_model="flux2-klein-4b",
        min_articles_per_story=pipeline_budget.min_articles_per_story,
        story_cluster_similarity_threshold=pipeline_budget.story_cluster_similarity_threshold,
        total_article_summary_cap_gemma_4_derived=is_gemma_4_model_reference(model_reference)
        and "NEWS_TOTAL_ARTICLE_SUMMARY_CAP" not in _active_env(),
        story_scale_screening_enabled=_bool_env("NEWS_STORY_SCALE_SCREENING_ENABLED", True),
        max_stories=pipeline_budget.max_stories,
        story_selection_overlap_threshold=pipeline_budget.story_selection_overlap_threshold,
        story_embedding_dedup_threshold=pipeline_budget.story_embedding_dedup_threshold,
        story_backfill_batch_multiplier=pipeline_budget.story_backfill_batch_multiplier,
    )


def resolve_runtime_config(request: RuntimeConfigRequest | None = None) -> RuntimeConfigResolution:
    request = request or RuntimeConfigRequest()
    preset_id, base_env, preset_env, effective_env = _resolve_effective_env(request)
    token = _CONFIG_ENV.set(effective_env)
    try:
        removed = sorted(configured_removed_topic_env_vars(effective_env))
        config = _build_runtime_config(
            preset_id=preset_id,
            materialize_outputs=request.materialize_outputs,
            run_started_at=request.run_started_at,
        )
    finally:
        _CONFIG_ENV.reset(token)
    return RuntimeConfigResolution(
        config=config,
        effective_env=dict(effective_env),
        preset_env=dict(preset_env),
        command_env_delta=_runtime_command_env_delta(
            base_env=base_env,
            effective_env=effective_env,
            preset_env=preset_env,
            preset_id=preset_id,
        ),
        removed_topic_env_vars=removed,
    )


def load_runtime_config(
    *,
    materialize_outputs: bool = True,
    environ: Mapping[str, str] | None = None,
    preset_id: str | None = None,
    overrides: Mapping[str, str] | None = None,
    run_started_at: datetime | None = None,
) -> RuntimeConfig:
    return resolve_runtime_config(
        RuntimeConfigRequest(
            base_env=environ,
            preset_id=preset_id,
            overrides=overrides,
            materialize_outputs=materialize_outputs,
            run_started_at=run_started_at,
        )
    ).config


def _coerce_pause_value(value: Any) -> bool:
    return _coerce_bool_value(value, False)
