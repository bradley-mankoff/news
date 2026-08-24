"""Deterministic translation policy (Mode B slice 01 / DN-10, issue #33).

Translation is explicit-only: it runs for a source exactly when translation
is enabled AND the source declares a language AND that declared language
differs from the target. Article content is never sniffed for script or
language, declared languages are never retagged, and an undeclared language
records ``unknown_language`` instead of guessing. The translation stage that
consumes these rules ships separately (DN-11 / issue #172).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_TARGET_LANGUAGE = "en"

# Deterministic gate statuses; every outcome names its cause so runs can
# record why translation did or did not happen without re-deriving it.
STATUS_DISABLED = "disabled"
STATUS_UNKNOWN_LANGUAGE = "unknown_language"
STATUS_SAME_LANGUAGE = "same_language"
STATUS_NEEDS_TRANSLATION = "needs_translation"


def normalize_declared_language(value: Any) -> str | None:
    """Normalize one declared language tag without mapping it elsewhere.

    Lowercases and folds underscore separators to hyphens (``zh_Hans`` ->
    ``zh-hans``); empty or whitespace-only values normalize to None. A
    missing declaration stays missing: callers record a status instead of
    inferring a language from content.
    """
    normalized = str(value or "").strip().lower().replace("_", "-")
    return normalized or None


def translation_decision(
    *,
    enabled: bool,
    source_language: Any,
    target_language: Any = DEFAULT_TARGET_LANGUAGE,
) -> dict[str, Any]:
    """Decide whether translation applies to one source's declared language.

    The only enable path is explicit: ``enabled`` plus a declared source
    language that differs from the target. Disabled, undeclared, and
    same-language outcomes all return ``runs=False`` with the cause as
    ``status``; unknown languages are recorded, never guessed.
    """
    normalized_target = (
        normalize_declared_language(target_language) or DEFAULT_TARGET_LANGUAGE
    )
    normalized_source = normalize_declared_language(source_language)
    if not enabled:
        status = STATUS_DISABLED
    elif not normalized_source:
        status = STATUS_UNKNOWN_LANGUAGE
    elif normalized_source == normalized_target:
        status = STATUS_SAME_LANGUAGE
    else:
        status = STATUS_NEEDS_TRANSLATION
    return {
        "runs": status == STATUS_NEEDS_TRANSLATION,
        "status": status,
        "source_language": normalized_source,
        "target_language": normalized_target,
    }


@dataclass(frozen=True)
class TranslationProvenance:
    """Durable record of one translation outcome.

    Keeps the original and translated text together with the declared
    languages, model identity, and terminal status so any downstream stage
    can audit what changed without re-deriving it.
    """

    original_text: str
    translated_text: str
    source_language: str | None
    target_language: str
    model: str | None
    status: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready projection for diagnostics and history records."""
        return {
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "model": self.model,
            "status": self.status,
        }


def build_translation_provenance(
    *,
    original_text: Any,
    translated_text: Any,
    source_language: Any,
    target_language: Any,
    model: Any,
    status: Any,
) -> TranslationProvenance:
    """Normalize raw stage outputs into a provenance record.

    Both texts are preserved verbatim (the stage is non-destructive);
    languages normalize through :func:`normalize_declared_language`, an
    absent model normalizes to None, and a blank status stays blank rather
    than being invented.
    """
    model_reference = str(model or "").strip()
    return TranslationProvenance(
        original_text=str(original_text or ""),
        translated_text=str(translated_text or ""),
        source_language=normalize_declared_language(source_language),
        target_language=(
            normalize_declared_language(target_language) or DEFAULT_TARGET_LANGUAGE
        ),
        model=model_reference or None,
        status=str(status or "").strip(),
    )
