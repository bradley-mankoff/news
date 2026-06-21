"""Configuration loading for the daily news pipeline.

The pipeline is intentionally driven by small YAML files in ``config/``:

- ``sources.yaml`` defines article feeds searched by run-mode tier and language.
- ``recipients.yaml`` defines email recipients and optional personal prompts.

Environment variables can override runtime knobs without editing YAML. See
``README.md`` for the full command list.
"""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from .source_catalog import MarkTranslationRequired, apply_source_catalog_patch


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
RUN_PRESETS_PATH = CONFIG_DIR / "run_presets.yaml"
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
DEFAULT_MODEL_ALIAS = "gemma-26b-moe"
GEMMA_12B_OPTIQ_MODEL_ALIAS = "https://huggingface.co/EgorKodin/Huihui-gemma-4-12B-it-abliterated-mlx-4bit"
GEMMA_12B_OPTIQ_MODEL_NAME = "EgorKodin/Huihui-gemma-4-12B-it-abliterated-mlx-4bit"
CODEX_TEST_MODEL_ALIAS = "gemma-e2b-tiny"
CODEX_TEST_MODEL_NAME = "deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit"
DEFAULT_TRANSLATION_MODEL = "google/translategemma-4b-it"
GEMMA_4_ARTICLE_SUMMARY_CAP = 40
CORE_SOURCE_TIER = "core"
PERIPHERAL_SOURCE_TIER = "peripheral"
SOURCE_SCOPE_CORE = CORE_SOURCE_TIER
SOURCE_SCOPE_PERIPHERAL = PERIPHERAL_SOURCE_TIER
SOURCE_SCOPES = (SOURCE_SCOPE_CORE, SOURCE_SCOPE_PERIPHERAL)
RECIPIENT_SCOPE_BRADLEY = "bradley"
RECIPIENT_SCOPE_ALL = "all"
RECIPIENT_SCOPES = (RECIPIENT_SCOPE_BRADLEY, RECIPIENT_SCOPE_ALL)
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
    GEMMA_12B_OPTIQ_MODEL_ALIAS: GEMMA_12B_OPTIQ_MODEL_NAME,
    "gemma-26b-moe": "mlx-community/gemma-4-26B-A4B-it-heretic-4bit",
    f"https://huggingface.co/{DEFAULT_TRANSLATION_MODEL}": DEFAULT_TRANSLATION_MODEL,
    f"https://hf.co/{DEFAULT_TRANSLATION_MODEL}": DEFAULT_TRANSLATION_MODEL,
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
    temperature: float
    top_p: float
    top_k: int
    min_p: float
    presence_penalty: float
    repetition_penalty: float


@dataclass(frozen=True)
class ModelRuntimeProfile:
    key: str
    model_max_input_tokens: int
    article_text_token_limit: int
    total_article_summary_cap: int
    translation_max_tokens: int
    article_summary_max_tokens: int
    final_synthesis_max_tokens: int
    title_generation_max_tokens: int
    server_prefill_step_size: int
    server_prompt_cache_size: int
    server_prompt_cache_bytes: str
    server_max_tokens: int
    default_sampling: ModelSamplingSettings
    reasoning_sampling: ModelSamplingSettings
    task_sampling: dict[str, ModelSamplingSettings]


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
    write_legacy_diagnostics: bool
    run_used_urls_path: Path
    preset_id: str
    source_scope: str
    recipient_scope: str
    url_reuse_blocking_enabled: bool
    relaxed_final_synthesis_guards: bool
    model_reference: str
    model_name: str
    model_profile: ModelRuntimeProfile
    model_base_url: str
    model_backend: str
    model_concurrency: int
    article_summary_concurrency: int
    story_synthesis_concurrency: int
    source_collection_concurrency: int
    model_server_command: str
    translation_model_reference: str
    translation_model_name: str
    translation_model_base_url: str
    translation_model_backend: str
    translation_model_server_command: str
    translation_target_language: str
    translation_enabled: bool
    recent_window_hours: int
    max_articles_per_source: int
    bradley_recipient: str
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


def _sampling(
    temperature: float,
    top_p: float,
    top_k: int,
    min_p: float,
    presence_penalty: float,
    repetition_penalty: float,
) -> ModelSamplingSettings:
    return ModelSamplingSettings(
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
        presence_penalty=presence_penalty,
        repetition_penalty=repetition_penalty,
    )


def _task_sampling(
    *,
    default: ModelSamplingSettings,
    reasoning: ModelSamplingSettings,
    translation: ModelSamplingSettings | None = None,
    story_discovery: ModelSamplingSettings | None = None,
    story_scale_screening: ModelSamplingSettings | None = None,
    article_summary: ModelSamplingSettings | None = None,
    final_synthesis: ModelSamplingSettings | None = None,
    title_generation: ModelSamplingSettings | None = None,
) -> dict[str, ModelSamplingSettings]:
    return {
        "default": default,
        "translation": translation or default,
        "story_discovery": story_discovery or reasoning,
        "story_scale_screening": story_scale_screening
        or _sampling(0.0, 0.9, 20, 0.0, 0.0, 1.05),
        "article_summary": article_summary or default,
        "final_synthesis": final_synthesis or reasoning,
        "title_generation": title_generation or default,
    }


GEMMA_DEFAULT_SAMPLING = _sampling(0.1, 0.8, 20, 0.05, 0.0, 1.1)
GEMMA_REASONING_SAMPLING = _sampling(0.3, 0.9, 40, 0.02, 0.3, 1.05)
# Gemma-4 family guidance starts at temp=1/top_p=0.95/top_k=64; task
# overrides below keep deterministic news extraction/synthesis paths calmer.
GEMMA_12B_DEFAULT_SAMPLING = _sampling(1.0, 0.95, 64, 0.0, 0.0, 1.0)
GEMMA_12B_REASONING_SAMPLING = _sampling(0.7, 0.9, 64, 0.0, 0.2, 1.05)
GEMMA_12B_SYNTHESIS_SAMPLING = _sampling(0.25, 0.85, 40, 0.0, 0.3, 1.05)
TINY_GEMMA_DEFAULT_SAMPLING = _sampling(0.15, 0.8, 20, 0.02, 0.0, 1.1)
TINY_GEMMA_REASONING_SAMPLING = _sampling(0.25, 0.85, 30, 0.02, 0.2, 1.08)

DEFAULT_PIPELINE_CONCURRENCY = 4
DEFAULT_ARTICLE_SUMMARY_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY
DEFAULT_STORY_SYNTHESIS_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY
DEFAULT_SOURCE_COLLECTION_CONCURRENCY = DEFAULT_PIPELINE_CONCURRENCY


MODEL_RUNTIME_PROFILES = {
    "big_conservative": ModelRuntimeProfile(
        key="big_conservative",
        model_max_input_tokens=6000,
        article_text_token_limit=4500,
        total_article_summary_cap=72,
        translation_max_tokens=1800,
        article_summary_max_tokens=1000,
        final_synthesis_max_tokens=1800,
        title_generation_max_tokens=50,
        server_prefill_step_size=512,
        server_prompt_cache_size=2,
        server_prompt_cache_bytes="512MB",
        server_max_tokens=1800,
        default_sampling=GEMMA_DEFAULT_SAMPLING,
        reasoning_sampling=GEMMA_REASONING_SAMPLING,
        task_sampling=_task_sampling(
            default=GEMMA_DEFAULT_SAMPLING,
            reasoning=GEMMA_REASONING_SAMPLING,
            translation=_sampling(0.0, 0.9, 20, 0.0, 0.0, 1.05),
            story_discovery=_sampling(0.15, 0.85, 30, 0.02, 0.2, 1.05),
            article_summary=_sampling(0.2, 0.85, 30, 0.02, 0.2, 1.08),
            final_synthesis=GEMMA_REASONING_SAMPLING,
            title_generation=_sampling(0.45, 0.9, 40, 0.0, 0.3, 1.05),
        ),
    ),
    "gemma_12b_optiq": ModelRuntimeProfile(
        key="gemma_12b_optiq",
        model_max_input_tokens=12000,
        article_text_token_limit=8000,
        total_article_summary_cap=36,
        translation_max_tokens=2400,
        article_summary_max_tokens=1800,
        final_synthesis_max_tokens=2400,
        title_generation_max_tokens=60,
        server_prefill_step_size=2048,
        server_prompt_cache_size=16,
        server_prompt_cache_bytes="3GB",
        server_max_tokens=2400,
        default_sampling=GEMMA_12B_DEFAULT_SAMPLING,
        reasoning_sampling=GEMMA_12B_REASONING_SAMPLING,
        task_sampling=_task_sampling(
            default=GEMMA_12B_DEFAULT_SAMPLING,
            reasoning=GEMMA_12B_REASONING_SAMPLING,
            translation=_sampling(0.0, 0.9, 20, 0.0, 0.0, 1.05),
            story_discovery=_sampling(0.2, 0.9, 40, 0.0, 0.2, 1.05),
            story_scale_screening=_sampling(0.0, 0.9, 20, 0.0, 0.0, 1.05),
            article_summary=_sampling(0.2, 0.85, 40, 0.0, 0.2, 1.08),
            final_synthesis=GEMMA_12B_SYNTHESIS_SAMPLING,
            title_generation=_sampling(0.45, 0.9, 40, 0.0, 0.3, 1.05),
        ),
    ),
    "tiny_codex": ModelRuntimeProfile(
        key="tiny_codex",
        model_max_input_tokens=5000,
        article_text_token_limit=4000,
        total_article_summary_cap=24,
        translation_max_tokens=900,
        article_summary_max_tokens=700,
        final_synthesis_max_tokens=1100,
        title_generation_max_tokens=40,
        server_prefill_step_size=512,
        server_prompt_cache_size=2,
        server_prompt_cache_bytes="256MB",
        server_max_tokens=1200,
        default_sampling=TINY_GEMMA_DEFAULT_SAMPLING,
        reasoning_sampling=TINY_GEMMA_REASONING_SAMPLING,
        task_sampling=_task_sampling(
            default=TINY_GEMMA_DEFAULT_SAMPLING,
            reasoning=TINY_GEMMA_REASONING_SAMPLING,
            translation=_sampling(0.0, 0.85, 20, 0.0, 0.0, 1.05),
            story_discovery=_sampling(0.1, 0.8, 20, 0.02, 0.2, 1.08),
            article_summary=_sampling(0.15, 0.8, 20, 0.02, 0.1, 1.08),
            final_synthesis=TINY_GEMMA_REASONING_SAMPLING,
            title_generation=_sampling(0.35, 0.85, 30, 0.0, 0.2, 1.05),
        ),
    ),
}


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


def _configured_model_name() -> str:
    return resolve_model_name(_configured_model_reference())


def infer_model_backend(model_reference: str) -> str:
    resolved_name = resolve_model_name(model_reference).lower()
    if resolved_name == GEMMA_12B_OPTIQ_MODEL_NAME.lower():
        return "mlx-lm"
    if "gemma-4" in resolved_name or "gemma4" in resolved_name:
        return "mlx-vlm"
    return "mlx-lm"


def _configured_model_backend(model_reference: str) -> str:
    return infer_model_backend(model_reference)


def _default_total_article_summary_cap(
    profile: ModelRuntimeProfile,
    *,
    model_reference: str,
) -> int:
    if is_gemma_4_model_reference(model_reference):
        return GEMMA_4_ARTICLE_SUMMARY_CAP
    return profile.total_article_summary_cap


def _default_article_summary_concurrency(model_reference: str) -> int:
    if is_codex_test_model_reference(model_reference):
        return 8
    return DEFAULT_ARTICLE_SUMMARY_CONCURRENCY


def _default_story_synthesis_concurrency(model_reference: str) -> int:
    if is_codex_test_model_reference(model_reference):
        return 2
    if is_gemma_4_model_reference(model_reference):
        return 1
    return DEFAULT_STORY_SYNTHESIS_CONCURRENCY


def _configured_translation_model_reference() -> str:
    return _str_env("NEWS_TRANSLATION_MODEL", DEFAULT_TRANSLATION_MODEL) or DEFAULT_TRANSLATION_MODEL


def _configured_translation_enabled() -> bool:
    return _bool_env("NEWS_TRANSLATION_ENABLED", False)


def _configured_translation_model_backend(model_reference: str) -> str:
    return infer_model_backend(model_reference)


def infer_model_profile_key(model_reference: str) -> str:
    clean_reference = (model_reference or "").strip()
    if clean_reference in UNSUPPORTED_MODEL_REFERENCES:
        raise ValueError(f"Unsupported model reference: {clean_reference}")
    if is_codex_test_model_reference(clean_reference):
        return "tiny_codex"
    resolved_model_name = resolve_model_name(clean_reference)
    if resolved_model_name == GEMMA_12B_OPTIQ_MODEL_NAME:
        return "gemma_12b_optiq"
    if clean_reference == "gemma-26b-moe":
        return "big_conservative"

    resolved_name = resolved_model_name.lower()
    if "gemma" in resolved_name and "26b" in resolved_name:
        return "big_conservative"
    if "gemma" in resolved_name and "12b" in resolved_name and "optiq" in resolved_name:
        return "gemma_12b_optiq"
    raise ValueError(f"Unsupported model reference: {clean_reference or resolved_model_name}")


def _configured_model_profile_key(model_reference: str) -> str:
    if _bool_env("NEWS_CODEX_TESTING", False):
        return "tiny_codex"
    return infer_model_profile_key(model_reference)


MODEL_TASK_SAMPLING_ENV_PREFIXES = {
    "default": "NEWS_MODEL",
    "translation": "NEWS_MODEL_TRANSLATION",
    "story_discovery": "NEWS_MODEL_STORY_DISCOVERY",
    "story_scale_screening": "NEWS_MODEL_STORY_SCALE_SCREENING",
    "article_summary": "NEWS_MODEL_ARTICLE_SUMMARY",
    "final_synthesis": "NEWS_MODEL_FINAL_SYNTHESIS",
    "title_generation": "NEWS_MODEL_TITLE_GENERATION",
}


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
    }


def runtime_knob_registry() -> list[dict[str, Any]]:
    knobs = [
        _runtime_knob("Run", "Source scope", "NEWS_SOURCE_SCOPE", "select", default="core", options=list(SOURCE_SCOPES)),
        _runtime_knob("Run", "Recipient scope", "NEWS_RECIPIENT_SCOPE", "select", default="bradley", options=list(RECIPIENT_SCOPES)),
        _runtime_knob("Run", "Block reused URLs", "NEWS_BLOCK_REUSED_URLS", "bool", default=False),
        _runtime_knob("Image", "Image generation", "NEWS_IMAGE_ENABLED", "bool", default=False),
        _runtime_knob("Model", "Model", "NEWS_MODEL", "select", default=DEFAULT_MODEL_ALIAS, options=sorted(MODEL_ALIASES)),
        _runtime_knob("Model", "Model base URL", "NEWS_MODEL_BASE_URL", default="http://127.0.0.1:8080/v1", advanced=True),
        _runtime_knob("Summary", "Model input cap", "NEWS_MODEL_MAX_INPUT_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Summary", "Article text token limit", "NEWS_ARTICLE_TEXT_TOKEN_LIMIT", "number", minimum=1, step=1),
        _runtime_knob("Summary", "Total article summary cap", "NEWS_TOTAL_ARTICLE_SUMMARY_CAP", "number", minimum=0, step=1),
        _runtime_knob("Summary", "Article summary max tokens", "NEWS_ARTICLE_SUMMARY_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Summary", "Story synthesis max tokens", "NEWS_FINAL_SYNTHESIS_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Summary", "Title max tokens", "NEWS_TITLE_GENERATION_MAX_TOKENS", "number", minimum=1, step=1),
        _runtime_knob("Story", "Recent window hours", "NEWS_RECENT_WINDOW_HOURS", "number", minimum=1, step=1),
        _runtime_knob("Story", "Max articles per source", "NEWS_MAX_ARTICLES_PER_SOURCE", "number", minimum=1, step=1),
        _runtime_knob("Story", "Min articles per story", "NEWS_MIN_ARTICLES_PER_STORY", "number", minimum=2, step=1),
        _runtime_knob("Story", "Story cluster similarity", "NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Story", "Story scale screening", "NEWS_STORY_SCALE_SCREENING_ENABLED", "bool"),
        _runtime_knob("Story", "Max stories", "NEWS_MAX_STORIES", "number", minimum=1, step=1),
        _runtime_knob("Story", "Story selection overlap", "NEWS_STORY_SELECTION_OVERLAP_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Story", "Story dedup threshold", "NEWS_STORY_DEDUP_THRESHOLD", "number", minimum=0, maximum=1, step=0.01),
        _runtime_knob("Story", "Backfill batch multiplier", "NEWS_STORY_BACKFILL_BATCH_MULTIPLIER", "number", minimum=1, step=1),
        _runtime_knob("Story", "Component overlap suppress", "NEWS_STORY_COMPONENT_OVERLAP_SUPPRESS_THRESHOLD", "number", minimum=0, maximum=1, step=0.01, advanced=True),
        _runtime_knob("Story", "Relax final synthesis guards", "NEWS_RELAX_FINAL_SYNTHESIS_GUARDS", "bool", advanced=True),
        _runtime_knob("Story", "Embedding model", "NEWS_EMBEDDING_MODEL", default="all-mpnet-base-v2", advanced=True),
        _runtime_knob("Advanced", "Token encoding", "NEWS_TOKEN_ENCODING", default="o200k_base", advanced=True),
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
                    "Sampling",
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


def _override_sampling_from_env(
    settings: ModelSamplingSettings,
    *,
    prefix: str,
) -> ModelSamplingSettings:
    return ModelSamplingSettings(
        temperature=_float_env(f"{prefix}_TEMPERATURE", settings.temperature),
        top_p=_float_env(f"{prefix}_TOP_P", settings.top_p),
        top_k=_int_env(f"{prefix}_TOP_K", settings.top_k),
        min_p=_float_env(f"{prefix}_MIN_P", settings.min_p),
        presence_penalty=_float_env(
            f"{prefix}_PRESENCE_PENALTY",
            settings.presence_penalty,
        ),
        repetition_penalty=_float_env(
            f"{prefix}_REPETITION_PENALTY",
            settings.repetition_penalty,
        ),
    )


def _profile_sampling_with_base_overrides(
    profile: ModelRuntimeProfile,
    *,
    default_sampling: ModelSamplingSettings,
    reasoning_sampling: ModelSamplingSettings,
) -> dict[str, ModelSamplingSettings]:
    task_sampling: dict[str, ModelSamplingSettings] = {}
    for task, settings in profile.task_sampling.items():
        task_base = _override_sampling_from_env(settings, prefix="NEWS_MODEL")
        if task == "default" or settings == profile.default_sampling:
            task_base = default_sampling
        elif settings == profile.reasoning_sampling:
            task_base = reasoning_sampling
        prefix = MODEL_TASK_SAMPLING_ENV_PREFIXES.get(task, f"NEWS_MODEL_{task.upper()}")
        task_sampling[task] = _override_sampling_from_env(task_base, prefix=prefix)
    return task_sampling


def _override_profile_from_env(
    profile: ModelRuntimeProfile,
    *,
    model_reference: str,
) -> ModelRuntimeProfile:
    default_sampling = _override_sampling_from_env(
        profile.default_sampling,
        prefix="NEWS_MODEL",
    )
    reasoning_sampling = _override_sampling_from_env(
        _override_sampling_from_env(profile.reasoning_sampling, prefix="NEWS_MODEL"),
        prefix="NEWS_MODEL_REASONING",
    )
    return ModelRuntimeProfile(
        key=profile.key,
        model_max_input_tokens=_int_env(
            "NEWS_MODEL_MAX_INPUT_TOKENS",
            profile.model_max_input_tokens,
        ),
        article_text_token_limit=_int_env(
            "NEWS_ARTICLE_TEXT_TOKEN_LIMIT",
            profile.article_text_token_limit,
        ),
        total_article_summary_cap=_int_env(
            "NEWS_TOTAL_ARTICLE_SUMMARY_CAP",
            _default_total_article_summary_cap(
                profile,
                model_reference=model_reference,
            ),
        ),
        translation_max_tokens=profile.translation_max_tokens,
        article_summary_max_tokens=_int_env(
            "NEWS_ARTICLE_SUMMARY_MAX_TOKENS",
            profile.article_summary_max_tokens,
        ),
        final_synthesis_max_tokens=_int_env(
            "NEWS_FINAL_SYNTHESIS_MAX_TOKENS",
            profile.final_synthesis_max_tokens,
        ),
        title_generation_max_tokens=_int_env(
            "NEWS_TITLE_GENERATION_MAX_TOKENS",
            profile.title_generation_max_tokens,
        ),
        server_prefill_step_size=profile.server_prefill_step_size,
        server_prompt_cache_size=profile.server_prompt_cache_size,
        server_prompt_cache_bytes=profile.server_prompt_cache_bytes,
        server_max_tokens=profile.server_max_tokens,
        default_sampling=default_sampling,
        reasoning_sampling=reasoning_sampling,
        task_sampling=_profile_sampling_with_base_overrides(
            profile,
            default_sampling=default_sampling,
            reasoning_sampling=reasoning_sampling,
        ),
    )


def configured_model_profile(model_reference: str | None = None) -> ModelRuntimeProfile:
    reference = model_reference or _configured_model_reference()
    profile_key = _configured_model_profile_key(reference)
    return _override_profile_from_env(
        MODEL_RUNTIME_PROFILES[profile_key],
        model_reference=reference,
    )


def build_model_server_command(
    model_name: str,
    profile: ModelRuntimeProfile,
    *,
    backend: str = "mlx-lm",
    model_concurrency: int = DEFAULT_PIPELINE_CONCURRENCY,
) -> str:
    concurrency = max(1, int(model_concurrency))
    if backend == "mlx-vlm":
        return (
            "uv run python -m mlx_vlm.server "
            f"--model {model_name} "
            "--host 127.0.0.1 "
            "--port 8080 "
            f"--prefill-step-size {profile.server_prefill_step_size} "
            f"--max-tokens {profile.server_max_tokens} "
            "--log-level INFO"
        )
    return (
        "uv run python -m mlx_lm server "
        f"--model {model_name} "
        f"--decode-concurrency {concurrency} "
        f"--prompt-concurrency {concurrency} "
        f"--prefill-step-size {profile.server_prefill_step_size} "
        f"--prompt-cache-size {profile.server_prompt_cache_size} "
        f"--prompt-cache-bytes {profile.server_prompt_cache_bytes} "
        f"--max-tokens {profile.server_max_tokens} "
        "--log-level INFO"
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


def _coerce_int_value(value: Any, default: int) -> int:
    try:
        return int(value)
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


def _source_requires_translation(raw_source: dict[str, Any]) -> bool:
    explicit = raw_source.get("requires_translation", raw_source.get("translate"))
    if explicit is not None:
        return _coerce_bool_value(explicit, False)
    return False


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
    scope = str(value or RECIPIENT_SCOPE_BRADLEY).strip().lower().replace("_", "-")
    aliases = {
        "bradley-only": RECIPIENT_SCOPE_BRADLEY,
        "single": RECIPIENT_SCOPE_BRADLEY,
        "full": RECIPIENT_SCOPE_ALL,
    }
    normalized = aliases.get(scope, scope)
    if normalized not in RECIPIENT_SCOPES:
        raise ValueError("NEWS_RECIPIENT_SCOPE must be one of: " + ", ".join(RECIPIENT_SCOPES))
    return normalized


def _configured_recipient_scope() -> str:
    return _normalize_recipient_scope(_str_env("NEWS_RECIPIENT_SCOPE", RECIPIENT_SCOPE_BRADLEY))


def _source_enabled_for_scope(
    raw_source: dict[str, Any],
    source_scope: str,
    *,
    source_key: str | None = None,
) -> bool:
    language = str(raw_source.get("language") or "").strip().lower()
    if language != "en" or _source_requires_translation(raw_source):
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
        requires_translation_explicit = (
            raw_source.get("requires_translation", raw_source.get("translate")) is not None
        )
        requires_translation = _source_requires_translation(raw_source)
        translation_source_language = str(
            raw_source.get("translation_source_language") or language
        ).strip().lower()
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
            "requires_translation": requires_translation,
            "requires_translation_explicit": requires_translation_explicit,
            "translation_source_language": translation_source_language or None,
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


def write_source_translation_flags(
    path: Path,
    source_languages: dict[str, str | None],
) -> int:
    result = apply_source_catalog_patch(path, [MarkTranslationRequired(source_languages)])
    return result.edit_count


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
    write_legacy_diagnostics = _bool_env("NEWS_WRITE_LEGACY_DIAGNOSTICS", False)
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

    bradley_recipient = _str_env("NEWS_BRADLEY_RECIPIENT", "bradley@mankoff.com")
    source_scope = _configured_source_scope()
    recipient_scope = _configured_recipient_scope()
    url_reuse_blocking_enabled = _bool_env("NEWS_BLOCK_REUSED_URLS", False)
    relaxed_final_synthesis_guards = _bool_env("NEWS_RELAX_FINAL_SYNTHESIS_GUARDS", False)
    tracked_urls_filename = "tracked_urls.txt"
    blocking_urls_filename = "blocking_urls.txt"
    run_used_urls_filename = (
        blocking_urls_filename if url_reuse_blocking_enabled else tracked_urls_filename
    )
    env_json_path = ROOT_DIR / _str_env("NEWS_ENV_JSON", "env.json")
    email_from = _str_env("NEWS_EMAIL_FROM", "bradley.mankoff@gmail.com")
    smtp_password = (
        _str_env("NEWS_SMTP_PASSWORD", "").replace(" ", "")
        or _load_password_from_env_json(env_json_path)
    )
    fallback_recipients = [
        addr.strip()
        for addr in _str_env("NEWS_EMAIL_RECIPIENTS", bradley_recipient).split(",")
        if addr.strip()
    ]

    model_reference = _configured_model_reference()
    model_name = resolve_model_name(model_reference)
    model_profile = configured_model_profile(model_reference)
    total_article_summary_cap_gemma_4_derived = (
        is_gemma_4_model_reference(model_reference)
        and "NEWS_TOTAL_ARTICLE_SUMMARY_CAP" not in _active_env()
    )
    model_base_url = _str_env("NEWS_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    model_backend = _configured_model_backend(model_reference)
    article_summary_concurrency = max(1, _default_article_summary_concurrency(model_reference))
    story_synthesis_concurrency = max(1, _default_story_synthesis_concurrency(model_reference))
    model_concurrency = max(
        1,
        article_summary_concurrency,
        story_synthesis_concurrency,
    )
    source_collection_concurrency = DEFAULT_SOURCE_COLLECTION_CONCURRENCY
    translation_enabled = _configured_translation_enabled()
    translation_model_reference = _configured_translation_model_reference()
    translation_model_name = resolve_model_name(translation_model_reference)
    translation_model_base_url = _str_env("NEWS_TRANSLATION_MODEL_BASE_URL", model_base_url)
    translation_model_backend = _configured_translation_model_backend(translation_model_reference)
    translation_model_server_command = ""
    if translation_enabled:
        try:
            translation_model_profile = configured_model_profile(translation_model_reference)
        except ValueError:
            translation_model_profile = model_profile
        translation_model_server_command = build_model_server_command(
            translation_model_name,
            translation_model_profile,
            backend=translation_model_backend,
            model_concurrency=model_concurrency,
        )
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
        write_legacy_diagnostics=write_legacy_diagnostics,
        run_used_urls_path=run_output_dir / run_used_urls_filename,
        preset_id=preset_id,
        source_scope=source_scope,
        recipient_scope=recipient_scope,
        url_reuse_blocking_enabled=url_reuse_blocking_enabled,
        relaxed_final_synthesis_guards=relaxed_final_synthesis_guards,
        model_reference=model_reference,
        model_name=model_name,
        model_profile=model_profile,
        model_base_url=model_base_url,
        model_backend=model_backend,
        model_concurrency=model_concurrency,
        article_summary_concurrency=article_summary_concurrency,
        story_synthesis_concurrency=story_synthesis_concurrency,
        source_collection_concurrency=source_collection_concurrency,
        model_server_command=build_model_server_command(
            model_name,
            model_profile,
            backend=model_backend,
            model_concurrency=model_concurrency,
        ),
        translation_model_reference=translation_model_reference,
        translation_model_name=translation_model_name,
        translation_model_base_url=translation_model_base_url,
        translation_model_backend=translation_model_backend,
        translation_model_server_command=translation_model_server_command,
        translation_target_language=_str_env("NEWS_TRANSLATION_TARGET_LANGUAGE", "en") or "en",
        translation_enabled=translation_enabled,
        recent_window_hours=_int_env("NEWS_RECENT_WINDOW_HOURS", 24),
        max_articles_per_source=_int_env("NEWS_MAX_ARTICLES_PER_SOURCE", 6),
        bradley_recipient=bradley_recipient,
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
        min_articles_per_story=configured_min_articles_per_story(),
        story_cluster_similarity_threshold=configured_story_cluster_similarity_threshold(),
        total_article_summary_cap_gemma_4_derived=total_article_summary_cap_gemma_4_derived,
        story_scale_screening_enabled=_bool_env("NEWS_STORY_SCALE_SCREENING_ENABLED", True),
        max_stories=max(1, _int_env("NEWS_MAX_STORIES", 4)),
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
