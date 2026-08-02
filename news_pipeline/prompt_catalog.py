"""Prompt Catalog: code-owned registry of editorial instruction bundles.

Prompt Profiles customize the *editorial instruction sentences* injected into
the five LLM prompt stages (article summary, story scale screening, story
drafting, title generation, image art direction). The machine-required output
contracts (``DATABASE_ENTRY:`` blocks, ``Headline:``/``Main story:``/
``Contradictions:`` format, ``[[S1]]`` citation markers, strict JSON for image
art, retry correction messages, scale label vocabulary) stay hardcoded in the
stage modules. Profiles only swap the editorial sentences.

This module is deliberately stdlib-only (``dataclasses``, ``difflib``,
``typing``) so that ``config.py`` can import it without creating an import
cycle. Built-ins live in Python (not YAML) because they are code-reviewed
contracts; a user-editable YAML override layer is a later issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from typing import Any

PROMPT_TASKS = (
    "article_summary",
    "story_scale_screening",
    "story_drafting",
    "title_generation",
    "image_art_direction",
)

DEFAULT_PROMPT_PROFILE_ID = "balanced"
PROMPT_PROFILE_ENV_VAR = "NEWS_PROMPT_PROFILE"

PROMPT_PROFILE_IDS = (
    "balanced",
    "consensus-and-contradiction",
    "explain-like-im-five",
    "facts-only",
    "playful",
)


@dataclass(frozen=True)
class PromptProfile:
    id: str
    name: str
    description: str
    prompts: dict[str, str]


PROMPT_PROFILES: dict[str, PromptProfile] = {
    "balanced": PromptProfile(
        id="balanced",
        name="Balanced",
        description=(
            "The default editorial approach: neutral, factual reporting exactly "
            "as the pipeline has always produced it."
        ),
        prompts={
            "article_summary": (
                "Prioritize facts that help later clustering and story synthesis; include major "
                "concrete developments without inventing relevance."
            ),
            "story_scale_screening": (
                "Be conservative: mark obviously_small_scale only when the supplied story draft and\n"
                "article summaries make the limited scale plain. Do not invent broader stakes, but do\n"
                "not mark national elections, interstate conflicts, civil wars, landmark court cases,\n"
                "nationally important US state races, major public-health events, or stories with clear\n"
                "global market/supply-chain/security implications as obviously small."
            ),
            "story_drafting": (
                "Prioritize the headline, lede, and details around the central event supported "
                "by the supplied source summaries and evidence."
            ),
            "title_generation": "Keep overlay_headline punchy, factual, and <= 11 words.",
            "image_art_direction": (
                "The image_prompt is for FLUX and must request a realistic documentary "
                "photograph with absolutely no text or typography in the image."
            ),
        },
    ),
    "consensus-and-contradiction": PromptProfile(
        id="consensus-and-contradiction",
        name="Consensus and Contradiction",
        description=(
            "Highlights where sources converge and separately surfaces direct "
            "factual disagreements about the same claim."
        ),
        prompts={
            "article_summary": (
                "Preserve concrete claims, uncertainty, and facts that can be compared against "
                "other reporting."
            ),
            "story_scale_screening": "Prefer developments independently reported across regions and outlets.",
            "story_drafting": (
                "Highlight where sources converge. Separately identify direct factual "
                "disagreements about the same claim."
            ),
            "title_generation": "Prefer a title expressing the day's central shared development.",
            "image_art_direction": "Depict the central event without sensationalism.",
        },
    ),
    "playful": PromptProfile(
        id="playful",
        name="Playful",
        description=(
            "A light, conversational tone for story prose and titles without "
            "weakening factual extraction or screening."
        ),
        prompts={
            "article_summary": (
                "Keep the summary strictly factual; a playful tone does not apply to article summaries."
            ),
            "story_scale_screening": "Keep the strict conservative screening rules; tone does not change scale judgments.",
            "story_drafting": (
                "Write with a light, playful, conversational tone while keeping the reporting "
                "factual, specific, and properly cited."
            ),
            "title_generation": (
                "Prefer a witty, playful title that still names the day's central development "
                "in at most 11 words."
            ),
            "image_art_direction": (
                "Depict the central event with a warm, approachable tone but no humor, "
                "cartoonish elements, or text."
            ),
        },
    ),
    "facts-only": PromptProfile(
        id="facts-only",
        name="Facts Only",
        description=(
            "Plain factual recap with no commentary, opinion, rhetorical framing, "
            "or editorial judgment."
        ),
        prompts={
            "article_summary": (
                "Report only verifiable facts and concrete claims; omit analysis, commentary, "
                "framing, and speculation."
            ),
            "story_scale_screening": "Judge scale only from the supplied facts; ignore tone, framing, and editorializing.",
            "story_drafting": (
                "Write a plain factual recap with no commentary, opinion, rhetorical framing, or "
                "editorial judgment; keep every factual claim cited."
            ),
            "title_generation": (
                "Prefer a plain, literal title naming the day's central development with no "
                "flourish, at most 11 words."
            ),
            "image_art_direction": (
                "Depict the central event as a neutral, matter-of-fact documentary scene with "
                "no text or typography."
            ),
        },
    ),
    "explain-like-im-five": PromptProfile(
        id="explain-like-im-five",
        name="Explain Like I'm Five",
        description=(
            "Simple, plain-language explanations a general reader can follow, "
            "with technical terms defined in one short clause."
        ),
        prompts={
            "article_summary": (
                "Keep concrete facts and uncertainty intact; simplify technical wording only "
                "where it does not change the facts."
            ),
            "story_scale_screening": "Keep the strict conservative screening rules; state the scale judgment in simple language.",
            "story_drafting": (
                "Explain the story in simple, plain language a general reader can follow; define "
                "technical terms in one short clause; keep every factual claim cited."
            ),
            "title_generation": (
                "Prefer a short, plain-language title a general reader can understand, at most 11 words."
            ),
            "image_art_direction": (
                "Depict the central event clearly and simply with everyday recognizable scenes "
                "and no text or typography."
            ),
        },
    ),
}

DEFAULT_PROMPT_INSTRUCTIONS: dict[str, str] = dict(
    PROMPT_PROFILES[DEFAULT_PROMPT_PROFILE_ID].prompts
)


def list_prompt_profiles() -> list[dict[str, Any]]:
    """Return catalog records sorted by profile id."""
    return [
        {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "prompts": dict(profile.prompts),
        }
        for profile in sorted(PROMPT_PROFILES.values(), key=lambda item: item.id)
    ]


def get_prompt_profile(profile_id: str) -> PromptProfile:
    """Look up a prompt profile, raising ValueError for unknown ids."""
    normalized = str(profile_id or "").strip()
    profile = PROMPT_PROFILES.get(normalized)
    if profile is None:
        valid = ", ".join(sorted(PROMPT_PROFILES)) or "none configured"
        raise ValueError(
            f"Unknown prompt profile {profile_id!r}. Available profiles: {valid}."
        )
    return profile


def resolve_prompt_instructions(profile_id: str | None = None) -> dict[str, str]:
    """Return the per-task instruction map for a profile (default: balanced)."""
    return dict(get_prompt_profile(profile_id or DEFAULT_PROMPT_PROFILE_ID).prompts)


def compare_prompt_profiles(
    profile_id: str,
    *,
    baseline_id: str = DEFAULT_PROMPT_PROFILE_ID,
) -> dict[str, str]:
    """Return per-task unified diffs (baseline -> profile) for changed tasks."""
    baseline = get_prompt_profile(baseline_id).prompts
    target = get_prompt_profile(profile_id).prompts
    diffs: dict[str, str] = {}
    for task in PROMPT_TASKS:
        baseline_text = baseline.get(task, "")
        target_text = target.get(task, "")
        if baseline_text == target_text:
            continue
        diff_lines = list(
            unified_diff(
                baseline_text.splitlines(),
                target_text.splitlines(),
                fromfile=f"{baseline_id}:{task}",
                tofile=f"{profile_id}:{task}",
                lineterm="",
            )
        )
        if diff_lines:
            diffs[task] = "\n".join(diff_lines)
    return diffs
