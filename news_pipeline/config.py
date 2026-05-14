"""Configuration loading for the daily news pipeline.

The pipeline is intentionally driven by small YAML files in ``config/``:

- ``sources.yaml`` defines article feeds searched after top-story discovery.
- ``recipients.yaml`` defines email recipients and optional personal prompts.

Environment variables can override runtime knobs without editing YAML. See
``README.md`` for the full command list.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
CURSORIGNORE_MANAGED_START = "# >>> news-pipeline latest output >>>"
CURSORIGNORE_MANAGED_END = "# <<< news-pipeline latest output <<<"
DEFAULT_MODEL_ALIAS = "gemma-26b-moe"
MODEL_ALIASES = {
    "qwen-9b-dense": "TheCluster/Qwen3.5-9B-Heretic-MLX-mxfp4",
    "gemma-26b-moe": "mlx-community/gemma-4-26B-A4B-it-heretic-4bit",
}
UNSUPPORTED_MODEL_REFERENCES = {
    "qwen-9b-medium",
    "TheCluster/Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8",
}


@dataclass(frozen=True)
class NewsSource:
    key: str
    name: str
    url: str
    homepage: str | None = None
    region: str | None = None
    frame: str | None = None
    provider_type: str | None = None
    intended_role: str | None = None
    weight: float = 1.0
    can_seed_topics: bool = False
    can_validate_topics: bool = False
    can_enrich_coverage: bool = True
    notes: str | None = None


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str
    personal_prompt: str | None = None
    pause: bool = False


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
    recipients_path: Path
    env_json_path: Path
    run_started_at: datetime
    run_date: str
    timestamp: str
    output_dir: Path
    run_output_dir: Path
    used_urls_filename: str
    dev_used_urls_filename: str
    run_used_urls_path: Path
    legacy_seen_urls_path: Path
    run_mode: str
    dev: bool
    local_prod: bool
    bradley_only_delivery: bool
    relaxed_final_synthesis_guards: bool
    model_reference: str
    model_name: str
    model_profile: ModelRuntimeProfile
    model_base_url: str
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
    dev_source_limit: int
    dev_num_topics: int


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


MODEL_RUNTIME_PROFILES = {
    "big_conservative": ModelRuntimeProfile(
        key="big_conservative",
        model_max_input_tokens=7000,
        article_summary_concurrency=1,
        article_text_token_limit=6000,
        total_article_summary_cap=28,
        per_topic_article_summary_cap=7,
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
        article_summary_concurrency=2,
        article_text_token_limit=8000,
        total_article_summary_cap=32,
        per_topic_article_summary_cap=8,
        topic_clustering_max_tokens=2200,
        translation_max_tokens=2400,
        article_summary_max_tokens=1800,
        final_synthesis_max_tokens=2400,
        title_generation_max_tokens=60,
        server_decode_concurrency=2,
        server_prompt_concurrency=2,
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


def resolve_model_name(model_reference: str) -> str:
    """Resolve a friendly model alias to the Hugging Face repo loaded by mlx."""
    clean_reference = (model_reference or "").strip()
    if not clean_reference:
        clean_reference = DEFAULT_MODEL_ALIAS
    if clean_reference in UNSUPPORTED_MODEL_REFERENCES:
        raise ValueError(f"Unsupported model reference: {clean_reference}")
    return MODEL_ALIASES.get(clean_reference, clean_reference)


def _configured_model_reference() -> str:
    selected_model = _str_env("NEWS_MODEL", "")
    raw_model_name = _str_env("NEWS_MODEL_NAME", "")
    default_model = _str_env("NEWS_DEFAULT_MODEL", DEFAULT_MODEL_ALIAS) or DEFAULT_MODEL_ALIAS
    return selected_model or raw_model_name or default_model


def _configured_model_name() -> str:
    return resolve_model_name(_configured_model_reference())


def infer_model_profile_key(model_reference: str) -> str:
    clean_reference = (model_reference or "").strip()
    if clean_reference in UNSUPPORTED_MODEL_REFERENCES:
        raise ValueError(f"Unsupported model reference: {clean_reference}")
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


def build_model_server_command(model_name: str, profile: ModelRuntimeProfile) -> str:
    return (
        "uv run mlx_lm.server "
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


def _sync_cursorignore_latest_output(
    root_dir: Path,
    output_dir: Path,
    run_output_dir: Path,
) -> None:
    """Keep Cursor focused on source files plus the newest generated run."""
    cursorignore_path = root_dir / ".cursorignore"
    try:
        output_pattern = output_dir.relative_to(root_dir).as_posix().rstrip("/") + "/*"
        run_pattern = run_output_dir.relative_to(root_dir).as_posix().rstrip("/")
    except ValueError:
        return

    managed_lines = [
        CURSORIGNORE_MANAGED_START,
        "# Refreshed by news_pipeline.config.load_runtime_config().",
        "# Ignore dated run output folders, then re-include this run.",
        output_pattern,
        f"!{run_pattern}/",
        f"!{run_pattern}/**",
        CURSORIGNORE_MANAGED_END,
    ]
    managed_block = "\n".join(managed_lines)

    try:
        existing = cursorignore_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = "\n".join(
            [
                ".venv/",
                "__pycache__/",
                "*.py[cod]",
                ".pytest_cache/",
                ".mypy_cache/",
                ".ruff_cache/",
                ".DS_Store",
                "env.json",
                "us_outputs/",
                "",
            ]
        )

    start_index = existing.find(CURSORIGNORE_MANAGED_START)
    end_index = existing.find(CURSORIGNORE_MANAGED_END)
    if start_index != -1 and end_index != -1 and end_index >= start_index:
        end_index += len(CURSORIGNORE_MANAGED_END)
        updated = existing[:start_index].rstrip() + "\n\n" + managed_block + existing[end_index:]
    else:
        updated = existing.rstrip() + "\n\n" + managed_block + "\n"

    if updated != existing:
        cursorignore_path.write_text(updated, encoding="utf-8")


def load_sources(path: Path | None = None) -> dict[str, dict[str, Any]]:
    sources_path = path or CONFIG_DIR / "sources.yaml"
    payload = _load_yaml_mapping(sources_path)
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError(f"{sources_path} must define sources as a list.")

    sources: dict[str, dict[str, Any]] = {}
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        key = str(raw_source.get("key") or raw_source.get("name") or "").strip()
        url = str(raw_source.get("url") or "").strip()
        if not key or not url:
            continue
        sources[key] = {
            "name": str(raw_source.get("name") or key).strip(),
            "url": url,
            "homepage": str(raw_source.get("homepage") or "").strip() or None,
            "region": str(raw_source.get("region") or "").strip() or None,
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
            "personal_prompt": _coerce_prompt_value(raw_recipient.get("personal_prompt")),
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
    relaxed_final_synthesis_guards = dev and _bool_env("NEWS_DEV_RELAXED_FINAL_GUARDS", True)
    used_urls_filename = "used_urls.txt"
    dev_used_urls_filename = "dev_used_urls.txt"
    run_used_urls_filename = dev_used_urls_filename if dev else used_urls_filename
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

    model_reference = _configured_model_reference()
    model_name = resolve_model_name(model_reference)
    model_profile = configured_model_profile(model_reference)
    model_base_url = _str_env("NEWS_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    return RuntimeConfig(
        root_dir=ROOT_DIR,
        sources_path=ROOT_DIR / _str_env("NEWS_SOURCES_YAML", "config/sources.yaml"),
        recipients_path=ROOT_DIR / _str_env("NEWS_RECIPIENTS_YAML", "config/recipients.yaml"),
        env_json_path=env_json_path,
        run_started_at=run_started_at,
        run_date=run_date,
        timestamp=timestamp,
        output_dir=output_dir,
        run_output_dir=run_output_dir,
        used_urls_filename=used_urls_filename,
        dev_used_urls_filename=dev_used_urls_filename,
        run_used_urls_path=run_output_dir / run_used_urls_filename,
        legacy_seen_urls_path=output_dir / "seen_urls.txt",
        run_mode=run_mode,
        dev=dev,
        local_prod=local_prod,
        bradley_only_delivery=bradley_only_delivery,
        relaxed_final_synthesis_guards=relaxed_final_synthesis_guards,
        model_reference=model_reference,
        model_name=model_name,
        model_profile=model_profile,
        model_base_url=model_base_url,
        model_server_command=build_model_server_command(model_name, model_profile),
        recent_window_hours=_int_env("NEWS_RECENT_WINDOW_HOURS", 24),
        max_articles_per_source=_int_env("NEWS_MAX_ARTICLES_PER_SOURCE", 6),
        num_top_topics=_int_env("NEWS_NUM_TOP_TOPICS", 4),
        top_topic_probes=_int_env("NEWS_TOP_TOPIC_PROBES", 4),
        top_of_funnel_per_provider=_int_env("NEWS_TOP_OF_FUNNEL_PER_PROVIDER", 10),
        summary_scope_label=_str_env("NEWS_SUMMARY_SCOPE", "the top news stories of the day"),
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
        image_generation_enabled=_bool_env("NEWS_IMAGE_ENABLED", True),
        image_generation_fail_on_error=_bool_env("NEWS_IMAGE_FAIL_ON_ERROR", False),
        image_width=_int_env("NEWS_IMAGE_WIDTH", 1024),
        image_height=_int_env("NEWS_IMAGE_HEIGHT", 1024),
        image_steps=_int_env("NEWS_IMAGE_STEPS", 4),
        image_crop_bottom_ratio=_float_env("NEWS_IMAGE_CROP_BOTTOM_RATIO", 0.12),
        image_model_id=_str_env("NEWS_IMAGE_MODEL_ID", "Runpod/FLUX.2-klein-4B-mflux-4bit"),
        image_base_model=_str_env("NEWS_IMAGE_BASE_MODEL", "flux2-klein-4b"),
        per_source_topic_article_cap=_int_env("NEWS_PER_SOURCE_TOPIC_ARTICLE_CAP", 1),
        dev_source_limit=_int_env("NEWS_DEV_SOURCE_LIMIT", 3),
        dev_num_topics=_int_env("NEWS_DEV_NUM_TOPICS", 2),
    )


def _coerce_prompt_value(value: Any) -> str | None:
    if value is None:
        return None
    prompt_text = str(value).strip()
    return prompt_text or None


def _coerce_pause_value(value: Any) -> bool:
    return _coerce_bool_value(value, False)
