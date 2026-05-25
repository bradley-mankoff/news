"""Configuration loading for the daily news pipeline.

The pipeline is intentionally driven by small YAML files in ``config/``:

- ``sources.yaml`` defines article feeds searched after topic selection.
- ``client.yaml`` selects active predefined topics.
- ``topics.yaml`` defines predefined topic relevance vocabulary.
- ``recipients.yaml`` defines email recipients and optional personal prompts.

Environment variables can override runtime knobs without editing YAML. See
``README.md`` for the full command list.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
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
    "!todays_news.py",
    "!news_pipeline/",
    "!news_pipeline/**",
    "!config/",
    "!config/client.yaml",
    "!config/topics.yaml",
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
CODEX_TEST_MODEL_ALIAS = "gemma-e2b-tiny"
CODEX_TEST_MODEL_NAME = "deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit"
MODEL_ALIASES = {
    "qwen-9b-dense": "TheCluster/Qwen3.5-9B-Heretic-MLX-mxfp4",
    "gemma-26b-moe": "mlx-community/gemma-4-26B-A4B-it-heretic-4bit",
    CODEX_TEST_MODEL_ALIAS: CODEX_TEST_MODEL_NAME,
    f"https://huggingface.co/{CODEX_TEST_MODEL_NAME}": CODEX_TEST_MODEL_NAME,
    f"https://hf.co/{CODEX_TEST_MODEL_NAME}": CODEX_TEST_MODEL_NAME,
}
UNSUPPORTED_MODEL_REFERENCES = {
    "qwen-9b-medium",
    "TheCluster/Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8",
}
CODEX_RUNTIME_ENV_VARS = ("CODEX_SANDBOX", "CODEX_CI", "CODEX_THREAD_ID")


@dataclass(frozen=True)
class NewsSource:
    key: str
    name: str
    url: str
    homepage: str | None = None
    region: str | None = None
    language: str | None = None
    tier: str = "core"
    topics: tuple[str, ...] = ()
    nations: tuple[str, ...] = ()
    frame: str | None = None
    provider_type: str | None = None
    intended_role: str | None = None
    weight: float = 1.0
    can_seed_topics: bool = False
    can_validate_topics: bool = False
    can_enrich_coverage: bool = True
    strict_source_match: bool = False
    source_match_aliases: tuple[str, ...] = ()
    notes: str | None = None


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
    article_summary_concurrency: int
    article_text_token_limit: int
    total_article_summary_cap: int
    per_topic_article_summary_cap: int
    topic_clustering_max_tokens: int
    translation_max_tokens: int
    article_summary_max_tokens: int
    final_synthesis_max_tokens: int
    title_generation_max_tokens: int
    server_decode_concurrency: int
    server_prompt_concurrency: int
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
    client_path: Path
    topics_path: Path
    recipients_path: Path
    env_json_path: Path
    run_started_at: datetime
    run_date: str
    timestamp: str
    output_dir: Path
    run_output_dir: Path
    used_urls_filename: str
    dev_used_urls_filename: str
    local_prod_used_urls_filename: str
    run_used_urls_path: Path
    legacy_seen_urls_path: Path
    run_mode: str
    dev: bool
    local_prod: bool
    bradley_only_delivery: bool
    shared_url_history_enabled: bool
    relaxed_final_synthesis_guards: bool
    topic_mode: str
    model_reference: str
    model_name: str
    model_profile: ModelRuntimeProfile
    model_base_url: str
    model_backend: str
    model_server_command: str
    recent_window_hours: int
    max_articles_per_source: int
    num_top_topics: int
    top_topic_probes: int
    top_of_funnel_per_provider: int
    summary_scope_label: str
    bradley_only_recipient: str
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
    per_source_topic_article_cap: int
    max_stories_per_topic: int
    max_articles_per_story: int
    story_cluster_similarity_threshold: float


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
    topic_clustering: ModelSamplingSettings | None = None,
    article_summary: ModelSamplingSettings | None = None,
    final_synthesis: ModelSamplingSettings | None = None,
    title_generation: ModelSamplingSettings | None = None,
) -> dict[str, ModelSamplingSettings]:
    return {
        "default": default,
        "translation": translation or default,
        "topic_clustering": topic_clustering or reasoning,
        "article_summary": article_summary or default,
        "final_synthesis": final_synthesis or reasoning,
        "title_generation": title_generation or default,
    }


GEMMA_DEFAULT_SAMPLING = _sampling(0.1, 0.8, 20, 0.05, 0.0, 1.1)
GEMMA_REASONING_SAMPLING = _sampling(0.3, 0.9, 40, 0.02, 0.3, 1.05)
QWEN_INSTRUCT_SAMPLING = _sampling(0.7, 0.8, 20, 0.0, 1.5, 1.0)
QWEN_REASONING_SAMPLING = _sampling(1.0, 1.0, 40, 0.0, 2.0, 1.0)
QWEN_SYNTHESIS_SAMPLING = _sampling(0.25, 0.85, 20, 0.0, 0.3, 1.05)
TINY_GEMMA_DEFAULT_SAMPLING = _sampling(0.15, 0.8, 20, 0.02, 0.0, 1.1)
TINY_GEMMA_REASONING_SAMPLING = _sampling(0.25, 0.85, 30, 0.02, 0.2, 1.08)


MODEL_RUNTIME_PROFILES = {
    "big_conservative": ModelRuntimeProfile(
        key="big_conservative",
        model_max_input_tokens=7000,
        article_summary_concurrency=1,
        article_text_token_limit=6000,
        total_article_summary_cap=72,
        per_topic_article_summary_cap=24,
        topic_clustering_max_tokens=1800,
        translation_max_tokens=1800,
        article_summary_max_tokens=1600,
        final_synthesis_max_tokens=2200,
        title_generation_max_tokens=50,
        server_decode_concurrency=1,
        server_prompt_concurrency=1,
        server_prefill_step_size=512,
        server_prompt_cache_size=2,
        server_prompt_cache_bytes="512MB",
        server_max_tokens=2500,
        default_sampling=GEMMA_DEFAULT_SAMPLING,
        reasoning_sampling=GEMMA_REASONING_SAMPLING,
        task_sampling=_task_sampling(
            default=GEMMA_DEFAULT_SAMPLING,
            reasoning=GEMMA_REASONING_SAMPLING,
            translation=_sampling(0.0, 0.9, 20, 0.0, 0.0, 1.05),
            topic_clustering=_sampling(0.15, 0.85, 30, 0.02, 0.2, 1.05),
            article_summary=_sampling(0.2, 0.85, 30, 0.02, 0.2, 1.08),
            final_synthesis=GEMMA_REASONING_SAMPLING,
            title_generation=_sampling(0.45, 0.9, 40, 0.0, 0.3, 1.05),
        ),
    ),
    "small_aggressive": ModelRuntimeProfile(
        key="small_aggressive",
        model_max_input_tokens=12000,
        article_summary_concurrency=4,
        article_text_token_limit=8000,
        total_article_summary_cap=36,
        per_topic_article_summary_cap=12,
        topic_clustering_max_tokens=2200,
        translation_max_tokens=2400,
        article_summary_max_tokens=1800,
        final_synthesis_max_tokens=2400,
        title_generation_max_tokens=60,
        server_decode_concurrency=4,
        server_prompt_concurrency=4,
        server_prefill_step_size=2048,
        server_prompt_cache_size=16,
        server_prompt_cache_bytes="3GB",
        server_max_tokens=2400,
        default_sampling=QWEN_INSTRUCT_SAMPLING,
        reasoning_sampling=QWEN_REASONING_SAMPLING,
        task_sampling=_task_sampling(
            default=QWEN_INSTRUCT_SAMPLING,
            reasoning=QWEN_REASONING_SAMPLING,
            final_synthesis=QWEN_SYNTHESIS_SAMPLING,
        ),
    ),
    "tiny_codex": ModelRuntimeProfile(
        key="tiny_codex",
        model_max_input_tokens=5000,
        article_summary_concurrency=1,
        article_text_token_limit=4000,
        total_article_summary_cap=24,
        per_topic_article_summary_cap=8,
        topic_clustering_max_tokens=900,
        translation_max_tokens=900,
        article_summary_max_tokens=700,
        final_synthesis_max_tokens=1100,
        title_generation_max_tokens=40,
        server_decode_concurrency=1,
        server_prompt_concurrency=1,
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
            topic_clustering=_sampling(0.1, 0.8, 20, 0.02, 0.2, 1.08),
            article_summary=_sampling(0.15, 0.8, 20, 0.02, 0.1, 1.08),
            final_synthesis=TINY_GEMMA_REASONING_SAMPLING,
            title_generation=_sampling(0.35, 0.85, 30, 0.0, 0.2, 1.05),
        ),
    ),
}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _configured_run_mode() -> str:
    explicit_mode = _str_env("NEWS_RUN_MODE", "").strip().lower().replace("_", "-")
    aliases = {
        "production": "prod",
        "true-prod": "prod",
        "true-production": "prod",
        "localprod": "local-prod",
    }
    if explicit_mode:
        mode = aliases.get(explicit_mode, explicit_mode)
        if mode not in {"dev", "local-prod", "prod"}:
            raise ValueError("NEWS_RUN_MODE must be one of: dev, local-prod, prod")
        return mode

    return "dev" if _bool_env("NEWS_DEV", True) else "prod"


def configured_topic_mode() -> str:
    raw_mode = _str_env("NEWS_TOPIC_MODE", "predefined").strip().lower().replace("_", "-")
    aliases = {
        "fixed": "predefined",
        "static": "predefined",
        "configured": "predefined",
    }
    mode = aliases.get(raw_mode, raw_mode)
    if mode != "predefined":
        raise ValueError("NEWS_TOPIC_MODE must be predefined; dynamic topic discovery has been retired")
    return "predefined"


def configured_max_stories_per_topic(run_mode: str | None = None) -> int:
    mode = run_mode or _configured_run_mode()
    default = 2 if mode == "dev" else 4
    return max(1, _int_env("NEWS_MAX_STORIES_PER_TOPIC", default))


def configured_max_articles_per_story(model_profile_key: str | None = None) -> int:
    profile_key = model_profile_key or _configured_model_profile_key(_configured_model_reference())
    if profile_key == "tiny_codex":
        default = 3
    elif profile_key == "small_aggressive":
        default = 5
    else:
        default = 4
    return max(2, _int_env("NEWS_MAX_ARTICLES_PER_STORY", default))


def configured_story_cluster_similarity_threshold() -> float:
    return min(
        1.0,
        max(0.0, _float_env("NEWS_STORY_CLUSTER_SIMILARITY_THRESHOLD", 0.30)),
    )


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


def _configured_model_reference(run_mode: str | None = None) -> str:
    if _bool_env("NEWS_CODEX_TESTING", False):
        return CODEX_TEST_MODEL_ALIAS
    mode = run_mode or _configured_run_mode()
    selected_model = _str_env("NEWS_MODEL", "")
    raw_model_name = _str_env("NEWS_MODEL_NAME", "")
    mode_default_model = CODEX_TEST_MODEL_ALIAS if mode == "dev" else DEFAULT_MODEL_ALIAS
    default_model = _str_env("NEWS_DEFAULT_MODEL", mode_default_model) or mode_default_model
    return selected_model or raw_model_name or default_model


def _configured_model_name() -> str:
    return resolve_model_name(_configured_model_reference())


def infer_model_backend(model_reference: str) -> str:
    resolved_name = resolve_model_name(model_reference).lower()
    if "gemma-4" in resolved_name or "gemma4" in resolved_name:
        return "mlx-vlm"
    return "mlx-lm"


def _configured_model_backend(model_reference: str) -> str:
    explicit_backend = _str_env("NEWS_MODEL_BACKEND", "")
    if explicit_backend:
        normalized = explicit_backend.strip().lower().replace("_", "-")
        if normalized not in {"mlx-lm", "mlx-vlm"}:
            raise ValueError("NEWS_MODEL_BACKEND must be one of: mlx-lm, mlx-vlm")
        return normalized
    return infer_model_backend(model_reference)


def infer_model_profile_key(model_reference: str) -> str:
    clean_reference = (model_reference or "").strip()
    if clean_reference in UNSUPPORTED_MODEL_REFERENCES:
        raise ValueError(f"Unsupported model reference: {clean_reference}")
    if is_codex_test_model_reference(clean_reference):
        return "tiny_codex"
    if clean_reference == "qwen-9b-dense":
        return "small_aggressive"
    if clean_reference == "gemma-26b-moe":
        return "big_conservative"

    resolved_name = resolve_model_name(clean_reference).lower()
    if "qwen3.5-9b" in resolved_name or "qwen-9b" in resolved_name or "9b" in resolved_name:
        return "small_aggressive"
    if "gemma-4-26b" in resolved_name or "26b" in resolved_name:
        return "big_conservative"
    return "small_aggressive"


def _configured_model_profile_key(model_reference: str) -> str:
    if _bool_env("NEWS_CODEX_TESTING", False):
        return "tiny_codex"
    explicit_profile = _str_env("NEWS_MODEL_PROFILE", "")
    if explicit_profile:
        if explicit_profile not in MODEL_RUNTIME_PROFILES:
            valid = ", ".join(sorted(MODEL_RUNTIME_PROFILES))
            raise ValueError(f"NEWS_MODEL_PROFILE must be one of: {valid}")
        return explicit_profile
    return infer_model_profile_key(model_reference)


MODEL_TASK_SAMPLING_ENV_PREFIXES = {
    "default": "NEWS_MODEL",
    "translation": "NEWS_MODEL_TRANSLATION",
    "topic_clustering": "NEWS_MODEL_TOPIC_CLUSTERING",
    "article_summary": "NEWS_MODEL_ARTICLE_SUMMARY",
    "final_synthesis": "NEWS_MODEL_FINAL_SYNTHESIS",
    "title_generation": "NEWS_MODEL_TITLE_GENERATION",
}


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


def _override_profile_from_env(profile: ModelRuntimeProfile) -> ModelRuntimeProfile:
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
        article_summary_concurrency=_int_env(
            "NEWS_ARTICLE_SUMMARY_CONCURRENCY",
            profile.article_summary_concurrency,
        ),
        article_text_token_limit=_int_env(
            "NEWS_ARTICLE_TEXT_TOKEN_LIMIT",
            profile.article_text_token_limit,
        ),
        total_article_summary_cap=_int_env(
            "NEWS_TOTAL_ARTICLE_SUMMARY_CAP",
            profile.total_article_summary_cap,
        ),
        per_topic_article_summary_cap=_int_env(
            "NEWS_PER_TOPIC_ARTICLE_SUMMARY_CAP",
            profile.per_topic_article_summary_cap,
        ),
        topic_clustering_max_tokens=_int_env(
            "NEWS_TOPIC_CLUSTERING_MAX_TOKENS",
            profile.topic_clustering_max_tokens,
        ),
        translation_max_tokens=_int_env(
            "NEWS_TRANSLATION_MAX_TOKENS",
            profile.translation_max_tokens,
        ),
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
        server_decode_concurrency=_int_env(
            "NEWS_SERVER_DECODE_CONCURRENCY",
            profile.server_decode_concurrency,
        ),
        server_prompt_concurrency=_int_env(
            "NEWS_SERVER_PROMPT_CONCURRENCY",
            profile.server_prompt_concurrency,
        ),
        server_prefill_step_size=_int_env(
            "NEWS_SERVER_PREFILL_STEP_SIZE",
            profile.server_prefill_step_size,
        ),
        server_prompt_cache_size=_int_env(
            "NEWS_SERVER_PROMPT_CACHE_SIZE",
            profile.server_prompt_cache_size,
        ),
        server_prompt_cache_bytes=_str_env(
            "NEWS_SERVER_PROMPT_CACHE_BYTES",
            profile.server_prompt_cache_bytes,
        ),
        server_max_tokens=_int_env(
            "NEWS_SERVER_MAX_TOKENS",
            profile.server_max_tokens,
        ),
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
    return _override_profile_from_env(MODEL_RUNTIME_PROFILES[profile_key])


def build_model_server_command(
    model_name: str,
    profile: ModelRuntimeProfile,
    *,
    backend: str = "mlx-lm",
) -> str:
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
        f"--decode-concurrency {profile.server_decode_concurrency} "
        f"--prompt-concurrency {profile.server_prompt_concurrency} "
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

    files_by_timestamp: dict[str, list[Path]] = {}
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        match = RUN_OUTPUT_TIMESTAMP_RE.search(path.name)
        if not match:
            continue
        files_by_timestamp.setdefault(match.group(0), []).append(path)

    if not files_by_timestamp:
        return []

    latest_timestamp = max(files_by_timestamp)
    latest_files = sorted(files_by_timestamp[latest_timestamp])
    latest_dirs = sorted({path.parent for path in latest_files})
    patterns = [
        "",
        f"# Keep generated output context narrowed to run {latest_timestamp}.",
        f"!{output_parent}/",
        f"!{output_path}/",
        f"{output_path}/*",
    ]
    for directory in latest_dirs:
        try:
            directory_pattern = directory.relative_to(root_dir).as_posix().rstrip("/")
        except ValueError:
            continue
        patterns.append(f"!{directory_pattern}/")
    for path in latest_files:
        try:
            file_pattern = path.relative_to(root_dir).as_posix()
        except ValueError:
            continue
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
        "# Ignore everything, then re-include core pipeline files plus the latest completed output run.",
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


RUN_MODE_SOURCE_TIERS = {
    "dev": {"dev"},
    "local-prod": {"dev", "core"},
    "prod": {"dev", "core"},
}


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


def _normalize_source_tier(value: Any) -> str:
    tier = str(value or "core").strip().lower().replace("_", "-")
    return tier if tier in {"dev", "core", "peripheral"} else "core"


def _source_enabled_for_run(raw_source: dict[str, Any], run_mode: str) -> bool:
    language = str(raw_source.get("language") or "").strip().lower()
    if language != "en":
        return False
    tier = _normalize_source_tier(raw_source.get("tier"))
    return tier in RUN_MODE_SOURCE_TIERS.get(run_mode, RUN_MODE_SOURCE_TIERS["prod"])


def load_sources(
    path: Path | None = None,
    *,
    run_mode: str | None = None,
    include_inactive: bool = False,
) -> dict[str, dict[str, Any]]:
    sources_path = path or CONFIG_DIR / "sources.yaml"
    payload = _load_yaml_mapping(sources_path)
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError(f"{sources_path} must define sources as a list.")

    sources: dict[str, dict[str, Any]] = {}
    selected_run_mode = run_mode or _configured_run_mode()
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        key = str(raw_source.get("key") or raw_source.get("name") or "").strip()
        url = str(raw_source.get("url") or "").strip()
        if not key or not url:
            continue
        if not include_inactive and not _source_enabled_for_run(raw_source, selected_run_mode):
            continue
        raw_source_match_aliases = raw_source.get("source_match_aliases") or []
        if isinstance(raw_source_match_aliases, str):
            raw_source_match_aliases = [raw_source_match_aliases]
        elif not isinstance(raw_source_match_aliases, list):
            raw_source_match_aliases = []
        tier = _normalize_source_tier(raw_source.get("tier"))
        sources[key] = {
            "name": str(raw_source.get("name") or key).strip(),
            "url": url,
            "homepage": str(raw_source.get("homepage") or "").strip() or None,
            "region": str(raw_source.get("region") or "").strip() or None,
            "language": str(raw_source.get("language") or "").strip() or None,
            "tier": tier,
            "topics": _coerce_source_text_list(raw_source.get("topics")),
            "nations": _coerce_source_text_list(raw_source.get("nations")),
            "frame": str(raw_source.get("frame") or raw_source.get("region") or "").strip() or None,
            "provider_type": str(raw_source.get("provider_type") or "article_feed").strip(),
            "intended_role": str(raw_source.get("intended_role") or "article enrichment").strip(),
            "weight": _coerce_float_value(raw_source.get("weight"), 1.0),
            "can_seed_topics": _coerce_bool_value(
                raw_source.get("can_seed_topics", raw_source.get("seed_topics")),
                False,
            ),
            "can_validate_topics": _coerce_bool_value(
                raw_source.get("can_validate_topics", raw_source.get("validate_topics")),
                False,
            ),
            "can_enrich_coverage": _coerce_bool_value(
                raw_source.get("can_enrich_coverage", raw_source.get("enrich_coverage")),
                True,
            ),
            "strict_source_match": _coerce_bool_value(raw_source.get("strict_source_match"), False),
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


def load_top_funnel_providers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    sources_path = path or CONFIG_DIR / "sources.yaml"
    payload = _load_yaml_mapping(sources_path)
    raw_providers = payload.get("top_funnel_providers", [])
    if not isinstance(raw_providers, list):
        raise ValueError(f"{sources_path} must define top_funnel_providers as a list.")

    providers: dict[str, dict[str, Any]] = {}
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            continue
        key = str(raw_provider.get("key") or raw_provider.get("name") or "").strip()
        url = str(raw_provider.get("url") or "").strip()
        fetcher = str(raw_provider.get("fetcher") or "rss").strip()
        if not key or not url:
            continue
        providers[key] = {
            "key": key,
            "name": str(raw_provider.get("name") or key).strip(),
            "url": url,
            "fetcher": fetcher,
            "region": str(raw_provider.get("region") or "").strip() or None,
            "frame": str(raw_provider.get("frame") or raw_provider.get("region") or "").strip() or None,
            "provider_type": str(raw_provider.get("provider_type") or "rss").strip(),
            "intended_role": str(raw_provider.get("intended_role") or "").strip() or None,
            "weight": _coerce_float_value(raw_provider.get("weight"), 1.0),
            "can_seed_topics": _coerce_bool_value(
                raw_provider.get("can_seed_topics", raw_provider.get("seed_topics")),
                False,
            ),
            "can_validate_topics": _coerce_bool_value(
                raw_provider.get("can_validate_topics", raw_provider.get("validate_topics")),
                False,
            ),
            "can_enrich_coverage": _coerce_bool_value(
                raw_provider.get("can_enrich_coverage", raw_provider.get("enrich_coverage")),
                False,
            ),
            "notes": str(raw_provider.get("notes") or "").strip() or None,
        }
    if not providers:
        raise ValueError(f"No valid top_funnel_providers entries found in {sources_path}.")
    return providers


def _coerce_text_list(value: Any, *, field_name: str, topic_id: str, path: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path} topic {topic_id!r} must define {field_name} as a list.")

    values: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        clean_item = str(raw_item or "").strip().lower()
        if not clean_item or clean_item in seen:
            continue
        seen.add(clean_item)
        values.append(clean_item)
    return values


def load_client_config(path: Path | None = None) -> dict[str, Any]:
    client_path = path or CONFIG_DIR / "client.yaml"
    payload = _load_yaml_mapping(client_path)
    raw_topic_ids = payload.get("topic_ids", payload.get("topics", []))
    if not isinstance(raw_topic_ids, list):
        raise ValueError(f"{client_path} must define topic_ids as a list.")

    topic_ids: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw_topic_id in raw_topic_ids:
        topic_id = str(raw_topic_id or "").strip()
        if not topic_id:
            continue
        if topic_id in seen:
            duplicates.add(topic_id)
        seen.add(topic_id)
        topic_ids.append(topic_id)

    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise ValueError(f"{client_path} contains duplicate topic_ids: {duplicate_list}")
    if not topic_ids:
        raise ValueError(f"{client_path} must define at least one topic_id.")

    return {"topic_ids": topic_ids}


def load_topic_definitions(path: Path | None = None) -> dict[str, dict[str, Any]]:
    topics_path = path or CONFIG_DIR / "topics.yaml"
    payload = _load_yaml_mapping(topics_path)
    raw_topics = payload.get("topics", [])
    if not isinstance(raw_topics, list):
        raise ValueError(f"{topics_path} must define topics as a list.")

    topics: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            continue
        topic_id = str(raw_topic.get("id") or raw_topic.get("key") or "").strip()
        if not topic_id:
            continue
        if topic_id in topics:
            duplicates.add(topic_id)
            continue

        title = str(raw_topic.get("title") or "").strip()
        if not title:
            raise ValueError(f"{topics_path} topic {topic_id!r} must define a title.")

        keywords = _coerce_text_list(
            raw_topic.get("keywords"),
            field_name="keywords",
            topic_id=topic_id,
            path=topics_path,
        )
        boost_phrases = _coerce_text_list(
            raw_topic.get("boost_phrases"),
            field_name="boost_phrases",
            topic_id=topic_id,
            path=topics_path,
        )
        if not keywords and not boost_phrases:
            raise ValueError(
                f"{topics_path} topic {topic_id!r} must define keywords or boost_phrases."
            )

        topics[topic_id] = {
            "id": topic_id,
            "title": title,
            "rationale": str(raw_topic.get("rationale") or "").strip(),
            "keywords": keywords,
            "boost_phrases": boost_phrases,
            "required_context_terms": _coerce_text_list(
                raw_topic.get("required_context_terms"),
                field_name="required_context_terms",
                topic_id=topic_id,
                path=topics_path,
            ),
            "max_articles_per_source": (
                _coerce_int_value(raw_topic.get("max_articles_per_source"), 0)
                if raw_topic.get("max_articles_per_source") is not None
                else None
            ),
            "frame_tags": _coerce_text_list(
                raw_topic.get("frame_tags"),
                field_name="frame_tags",
                topic_id=topic_id,
                path=topics_path,
            ),
        }

    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise ValueError(f"{topics_path} contains duplicate topic ids: {duplicate_list}")
    if not topics:
        raise ValueError(f"No valid topic entries found in {topics_path}.")
    return topics


def load_predefined_topics(
    *,
    client_path: Path | None = None,
    topics_path: Path | None = None,
    default_max_articles_per_source: int = 6,
) -> list[dict[str, Any]]:
    resolved_client_path = client_path or CONFIG_DIR / "client.yaml"
    resolved_topics_path = topics_path or CONFIG_DIR / "topics.yaml"
    client_config = load_client_config(resolved_client_path)
    topic_definitions = load_topic_definitions(resolved_topics_path)

    selected_topics: list[dict[str, Any]] = []
    for topic_id in client_config["topic_ids"]:
        if topic_id not in topic_definitions:
            raise ValueError(
                f"{resolved_client_path} references unknown topic_id {topic_id!r} "
                f"not found in {resolved_topics_path}."
            )
        topic = dict(topic_definitions[topic_id])
        topic["key"] = topic["id"]
        if topic.get("max_articles_per_source") is None:
            topic["max_articles_per_source"] = default_max_articles_per_source
        selected_topics.append(topic)

    return selected_topics


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
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=False)
    return updated_count


def load_runtime_config() -> RuntimeConfig:
    run_started_at = datetime.now()
    run_date = run_started_at.strftime("%Y-%m-%d")
    timestamp = run_started_at.strftime("%Y-%m-%d_%H-%M-%S")

    output_dir = ROOT_DIR / _str_env("NEWS_OUTPUT_DIR", "output/daily_outputs")
    run_output_dir = output_dir / run_date
    output_dir.mkdir(parents=True, exist_ok=True)
    run_output_dir.mkdir(parents=True, exist_ok=True)
    _sync_cursorignore_latest_output(ROOT_DIR, output_dir, run_output_dir)

    bradley_only_recipient = _str_env("NEWS_DEV_RECIPIENT", "bradley@mankoff.com")
    run_mode = _configured_run_mode()
    dev = run_mode == "dev"
    local_prod = run_mode == "local-prod"
    bradley_only_delivery = dev or local_prod
    local_prod_use_shared_history = _bool_env("NEWS_LOCAL_PROD_USE_SHARED_HISTORY", False)
    shared_url_history_enabled = (not dev and not local_prod) or (
        local_prod and local_prod_use_shared_history
    )
    relaxed_final_synthesis_guards = dev and _bool_env("NEWS_DEV_RELAXED_FINAL_GUARDS", True)
    used_urls_filename = "used_urls.txt"
    dev_used_urls_filename = "dev_used_urls.txt"
    local_prod_used_urls_filename = "local_prod_used_urls.txt"
    if dev:
        run_used_urls_filename = dev_used_urls_filename
    elif local_prod and not shared_url_history_enabled:
        run_used_urls_filename = local_prod_used_urls_filename
    else:
        run_used_urls_filename = used_urls_filename
    env_json_path = ROOT_DIR / _str_env("NEWS_ENV_JSON", "env.json")
    email_from = _str_env("NEWS_EMAIL_FROM", "bradley.mankoff@gmail.com")
    smtp_password = (
        _str_env("NEWS_SMTP_PASSWORD", "").replace(" ", "")
        or _load_password_from_env_json(env_json_path)
    )
    fallback_recipients = [
        addr.strip()
        for addr in _str_env("NEWS_EMAIL_RECIPIENTS", bradley_only_recipient).split(",")
        if addr.strip()
    ]

    model_reference = _configured_model_reference(run_mode)
    model_name = resolve_model_name(model_reference)
    model_profile = configured_model_profile(model_reference)
    model_base_url = _str_env("NEWS_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    model_backend = _configured_model_backend(model_reference)
    return RuntimeConfig(
        root_dir=ROOT_DIR,
        sources_path=ROOT_DIR / _str_env("NEWS_SOURCES_YAML", "config/sources.yaml"),
        client_path=ROOT_DIR / _str_env("NEWS_CLIENT_YAML", "config/client.yaml"),
        topics_path=ROOT_DIR / _str_env("NEWS_TOPICS_YAML", "config/topics.yaml"),
        recipients_path=ROOT_DIR / _str_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml"),
        env_json_path=env_json_path,
        run_started_at=run_started_at,
        run_date=run_date,
        timestamp=timestamp,
        output_dir=output_dir,
        run_output_dir=run_output_dir,
        used_urls_filename=used_urls_filename,
        dev_used_urls_filename=dev_used_urls_filename,
        local_prod_used_urls_filename=local_prod_used_urls_filename,
        run_used_urls_path=run_output_dir / run_used_urls_filename,
        legacy_seen_urls_path=output_dir / "seen_urls.txt",
        run_mode=run_mode,
        dev=dev,
        local_prod=local_prod,
        bradley_only_delivery=bradley_only_delivery,
        shared_url_history_enabled=shared_url_history_enabled,
        relaxed_final_synthesis_guards=relaxed_final_synthesis_guards,
        topic_mode=configured_topic_mode(),
        model_reference=model_reference,
        model_name=model_name,
        model_profile=model_profile,
        model_base_url=model_base_url,
        model_backend=model_backend,
        model_server_command=build_model_server_command(
            model_name,
            model_profile,
            backend=model_backend,
        ),
        recent_window_hours=_int_env("NEWS_RECENT_WINDOW_HOURS", 24),
        max_articles_per_source=_int_env("NEWS_MAX_ARTICLES_PER_SOURCE", 6),
        num_top_topics=_int_env("NEWS_NUM_TOP_TOPICS", 4),
        top_topic_probes=_int_env("NEWS_TOP_TOPIC_PROBES", 4),
        top_of_funnel_per_provider=_int_env("NEWS_TOP_OF_FUNNEL_PER_PROVIDER", 10),
        summary_scope_label=_str_env("NEWS_SUMMARY_SCOPE", "today's selected news topics"),
        bradley_only_recipient=bradley_only_recipient,
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
        image_generation_enabled=_bool_env("NEWS_IMAGE_ENABLED", not dev),
        image_generation_fail_on_error=_bool_env("NEWS_IMAGE_FAIL_ON_ERROR", False),
        image_width=_int_env("NEWS_IMAGE_WIDTH", 1024),
        image_height=_int_env("NEWS_IMAGE_HEIGHT", 1024),
        image_steps=_int_env("NEWS_IMAGE_STEPS", 4),
        image_crop_bottom_ratio=_float_env("NEWS_IMAGE_CROP_BOTTOM_RATIO", 0.12),
        image_model_id=_str_env("NEWS_IMAGE_MODEL_ID", "Runpod/FLUX.2-klein-4B-mflux-4bit"),
        image_base_model=_str_env("NEWS_IMAGE_BASE_MODEL", "flux2-klein-4b"),
        per_source_topic_article_cap=_int_env("NEWS_PER_SOURCE_TOPIC_ARTICLE_CAP", 1),
        max_stories_per_topic=configured_max_stories_per_topic(run_mode),
        max_articles_per_story=configured_max_articles_per_story(model_profile.key),
        story_cluster_similarity_threshold=configured_story_cluster_similarity_threshold(),
    )


def _coerce_pause_value(value: Any) -> bool:
    return _coerce_bool_value(value, False)
