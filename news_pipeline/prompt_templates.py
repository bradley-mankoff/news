"""Full-template prompt catalog: stdlib-only registry of system/user templates.

Prompt Profiles (``prompt_catalog.py``) customize the *editorial instruction
sentences* injected into the five LLM prompt stages. Full prompt templates
(ADR 0015) are a separate, advanced per-task replacement: each of the five
LLM tasks has a ``system`` and a ``user`` message template using Python
``string.Template`` placeholders. The machine-required output contracts stay
code-owned in ``prompt_contracts.py`` and are injected as placeholder values,
never as editable template text.

This module is deliberately stdlib-only (``dataclasses``, ``json``,
``string.Template``, ``typing``) so that ``config.py`` and ``ui.py`` can
import it without creating an import cycle (it must never import stage
modules or ``config.py``). Built-in templates live in Python because they are
extracted verbatim from the stage renderers and are the byte-identity
baseline; user overrides travel as per-task JSON env values under the
separate ``NEWS_PROMPT_TEMPLATE_<TASK>`` namespace and are validated here
before they can reach a model.

Placeholder rules:

- ``$name`` / ``${name}`` placeholders; ``$$`` is a literal dollar sign.
- Every custom template must include its task's dynamic input placeholders
  AND its code-owned contract placeholders (see the per-task maps below).
- Unknown placeholders, malformed ``$`` syntax, and missing required
  placeholders fail closed.
- Contract strings and dynamic payloads are inserted as substitution VALUES
  and are never re-parsed as templates (no Jinja, no ``.format()`` on
  user-authored text, no ``eval``, no ``safe_substitute``).
- ``$editorial_instructions`` is optional: a template that omits it replaces
  the selected profile's editorial sentence for that task entirely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from string import Template
from typing import Any, Mapping

from .prompt_catalog import PROMPT_TASKS
from .prompt_contracts import (
    ARTICLE_SUMMARY_OUTPUT_CONTRACT,
    IMAGE_ART_JSON_CONTRACT,
    IMAGE_ART_OVERLAY_PROTOCOL,
    STORY_DRAFTING_CITATION_CONTRACT,
    STORY_DRAFTING_OUTPUT_CONTRACT,
    STORY_SCALE_SCREENING_JSON_CONTRACT,
    TITLE_GENERATION_JSON_CONTRACT,
    validate_prompt_contract,
)

PROMPT_TEMPLATE_ENV_PREFIX = "NEWS_PROMPT_TEMPLATE_"
# Maps each canonical prompt task to its full-template override env var. These
# env vars carry the Advanced Settings system/user editors and layer on top of
# the built-in structural templates (override wins per task). Existing
# NEWS_PROMPT_OVERRIDE_<TASK> values remain sentence-level editorial
# overrides and are never reinterpreted as full templates.
PROMPT_TEMPLATE_ENV_VARS: dict[str, str] = {
    task: f"{PROMPT_TEMPLATE_ENV_PREFIX}{task.upper()}" for task in PROMPT_TASKS
}

# Required dynamic input placeholders per task (across system + user). These
# carry the per-call payloads the stage renderers already build.
PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "article_summary": ("now_label", "recent_window_hours", "article_payload"),
    "story_scale_screening": ("story_blocks",),
    "story_drafting": ("now_label", "story_title", "source_summary_lines"),
    "title_generation": ("report_title", "synthesis_body"),
    "image_art_direction": ("synthesis_body",),
}

# Code-owned contract placeholders per task. The VALUES are the pipeline-owned
# constants from prompt_contracts.py (parsers, retries, citation renderers,
# and sanitizers depend on them), so the placeholder itself must stay present
# but the text is never user-editable.
PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "article_summary": ("output_contract",),
    "story_scale_screening": ("scale_contract",),
    "story_drafting": ("citation_contract", "output_contract"),
    "title_generation": ("title_contract", "overlay_protocol"),
    "image_art_direction": ("image_contract",),
}

# Optional placeholder every task may use: the selected profile/override
# editorial sentence for that task. Omitting it intentionally bypasses the
# profile text for that task (documented precedence).
PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS: tuple[str, ...] = (
    "editorial_instructions",
)

PROMPT_TEMPLATE_PLACEHOLDER_DESCRIPTIONS: dict[str, str] = {
    "now_label": "Human-readable run date (e.g. \"August 14, 2026\").",
    "recent_window_hours": "Recent-window hour count used for article selection.",
    "article_payload": "The selected article's metadata, text, and expected summary block.",
    "story_blocks": "The drafted story candidates with their article summaries.",
    "story_title": "The story title being synthesized.",
    "source_summary_lines": "Source summaries and cleaned article evidence with citation IDs.",
    "report_title": "The report's headline, used as the overlay fallback.",
    "synthesis_body": "The final synthesized news body text.",
    "output_contract": "Pipeline-owned output protocol (DATABASE_ENTRY: / story format).",
    "citation_contract": "Pipeline-owned citation-marker protocol.",
    "scale_contract": "Pipeline-owned strict-JSON scale screening protocol.",
    "title_contract": "Pipeline-owned overlay_headline JSON protocol.",
    "overlay_protocol": "Pipeline-owned overlay-headline rendering protocol.",
    "image_contract": "Pipeline-owned image_prompt JSON protocol.",
    "editorial_instructions": (
        "Optional: the selected profile's editorial sentence for this task. "
        "Omitting it replaces the profile text for this task."
    ),
}

PROMPT_TEMPLATE_TASK_LABELS: dict[str, str] = {
    "article_summary": "Article Summarization",
    "story_scale_screening": "Story Scale Screening",
    "story_drafting": "Story Drafting",
    "title_generation": "Title Generation",
    "image_art_direction": "Image Art Direction",
}


@dataclass(frozen=True)
class PromptTemplate:
    task: str
    label: str
    system: str
    user: str
    required_placeholders: tuple[str, ...]
    optional_placeholders: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Built-in templates, extracted verbatim from the stage renderers so the
# balanced/no-override rendered messages remain byte-identical (ADR 0011
# golden tests). Contract strings and dynamic payloads are placeholder VALUES;
# only the surrounding structure lives in the template text.
# ---------------------------------------------------------------------------

DEFAULT_PROMPT_TEMPLATES: dict[str, PromptTemplate] = {
    "article_summary": PromptTemplate(
        task="article_summary",
        label=PROMPT_TEMPLATE_TASK_LABELS["article_summary"],
        system=(
            "Today: $now_label.\n"
            "Current Task: Summarize one preselected article from the last $recent_window_hours hours\n"
            "for story discovery, selection, and synthesis.\n"
            "1. Use only the provided article metadata, URL, description, and article text.\n"
            "2. Do not call tools in this step.\n"
            "3. Ignore outlet style and focus on concrete reported claims.\n"
            "4. Include key facts: what reportedly happened, where, timeline, named actors, casualties or damage if reported, and what remains unconfirmed.\n"
            "5. If the article text is thin, summarize only what is actually supported by the provided text and metadata.\n"
            "6. Do not recap the general history of a longstanding subject or conflict; include background only\n"
            "   when the article reports a new fact about it or one short clause is needed for orientation.\n"
            "7. $editorial_instructions\n"
            "$output_contract"
        ),
        user="$article_payload",
        required_placeholders=(
            *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS["article_summary"],
            *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS["article_summary"],
        ),
        optional_placeholders=PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS,
    ),
    "story_scale_screening": PromptTemplate(
        task="story_scale_screening",
        label=PROMPT_TEMPLATE_TASK_LABELS["story_scale_screening"],
        system=(
            "You are a strict but conservative scale-screening editor for a global daily\n"
            "news newsletter.\n"
            "\n"
            "Your job is to label each drafted story by substantive news scale. The labels\n"
            "are used to avoid obvious small local stories, not to separate good stories\n"
            "from great stories.\n"
            "\n"
            "Scale labels:\n"
            "- obviously_large_scale: the story has clear broad stakes, such as effects across\n"
            "  multiple countries, cross-border conflict, major civil war or mass displacement,\n"
            "  oil, gas, food, semiconductors, shipping lanes, critical minerals, supply chains,\n"
            "  sanctions, currency or financial markets, global public health, major migration,\n"
            "  multinational regulation, national politics, national economic effects, major\n"
            "  national legal effects, or major geopolitical/security implications.\n"
            "- obviously_small_scale: the story is plainly a routine single-country domestic\n"
            "  matter, local crime, local accident, city/province dispute, provincial or municipal\n"
            "  politics, or ordinary single-company item without broader market, supply-chain,\n"
            "  diplomatic, humanitarian, legal, national political, or security effects.\n"
            "- not_obvious: the scale is borderline or the supplied evidence does not justify an\n"
            "  obvious large/small conclusion.\n"
            "\n"
            "$editorial_instructions\n"
            "\n"
            "$scale_contract"
        ),
        user="Screen these candidate stories for global-news scale.\n\n$story_blocks",
        required_placeholders=(
            *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS["story_scale_screening"],
            *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS["story_scale_screening"],
        ),
        optional_placeholders=PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS,
    ),
    "story_drafting": PromptTemplate(
        task="story_drafting",
        label=PROMPT_TEMPLATE_TASK_LABELS["story_drafting"],
        system=(
            "Today: $now_label.\n"
            "You are synthesizing prewritten article summaries and cleaned article evidence into one newsletter story.\n"
            "Use only the supplied source summaries and cleaned article evidence.\n"
            "Write one custom story headline, then one cohesive main story paragraph, roughly 70-130 words.\n"
            "The headline should be factual, specific, 4-10 words, and not copied wholesale from a source headline.\n"
            "$citation_contract\n"
            "In the main story, try to support important claims with concrete evidence details from the\n"
            "cleaned article evidence when it is available. Paraphrase those details in your own words;\n"
            "do not quote article text, copy distinctive article wording, or use quotation marks around\n"
            "article-body phrasing. Cite the source IDs for the article or articles that supply each\n"
            "paraphrased evidence detail.\n"
            "If a source says it appears to cite another listed source, prefer the listed primary source\n"
            "for shared facts and cite the derivative source only for unique reporting or analysis.\n"
            "$editorial_instructions\n"
            "Lead with today's reported development. Include concrete reported claims, named actors,\n"
            "places, timing, figures, damage, statements, deadlines, and uncertainty when supported.\n"
            "Then assess whether the sources directly or materially contradict each other.\n"
            "A reportable contradiction is a factual disagreement about the same claim, count,\n"
            "timeline, attribution, status, quote, or outcome where the cited accounts cannot\n"
            "both be true in the same context. Do not require identical wording.\n"
            "Omission, different focus, routine updates over time, or one source addressing a\n"
            "subject another source does not address is not a contradiction.\n"
            "If there is no direct or material factual contradiction, write exactly 'NONE' for Contradictions.\n"
            "If there is a contradiction, write 1-3 concise prose sentences under Contradictions.\n"
            "Each contradiction sentence must cite the disagreeing sources and must use the cleaned article evidence,\n"
            "not only the source summaries.\n"
            "Do not write bullets, source-material notes, methodology, bibliography, or preamble.\n"
            "Do not merge in background material unless a source summary reports it as part of today's update."
        ),
        user=(
            "Story: $story_title\n"
            "\n"
            "        Source summaries and cleaned article evidence to paraphrase, not quote:\n"
            "        $source_summary_lines\n"
            "\n"
            "$output_contract"
        ),
        required_placeholders=(
            *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS["story_drafting"],
            *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS["story_drafting"],
        ),
        optional_placeholders=PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS,
    ),
    "title_generation": PromptTemplate(
        task="title_generation",
        label=PROMPT_TEMPLATE_TASK_LABELS["title_generation"],
        system=(
            "You are writing the overlay headline for a news report. "
            "$title_contract $editorial_instructions $overlay_protocol"
        ),
        user=(
            "Use the final news output below to create the overlay headline.\n"
            "\n"
            "Report title: $report_title\n"
            "\n"
            "Final output:\n$synthesis_body"
        ),
        required_placeholders=(
            *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS["title_generation"],
            *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS["title_generation"],
        ),
        optional_placeholders=PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS,
    ),
    "image_art_direction": PromptTemplate(
        task="image_art_direction",
        label=PROMPT_TEMPLATE_TASK_LABELS["image_art_direction"],
        system=(
            "You are preparing art direction for a text-to-image news illustration. "
            "$image_contract $editorial_instructions"
        ),
        user=(
            "Use the final news output below to create the text-free image prompt.\n"
            "\n"
            "Final output:\n$synthesis_body"
        ),
        required_placeholders=(
            *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS["image_art_direction"],
            *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS["image_art_direction"],
        ),
        optional_placeholders=PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS,
    ),
}


# ---------------------------------------------------------------------------
# Sample values used ONLY by validate_prompt_template(). Dynamic placeholders
# receive representative values (payload skeletons deliberately include the
# parser-facing markers the stage payloads supply); contract placeholders
# receive the REAL code-owned constants so a rendered custom template must
# still contain every required protocol marker.
# ---------------------------------------------------------------------------

_SAMPLE_ARTICLE_PAYLOAD = (
    "Selected article:\n\n"
    "Title: Sample article\n"
    "Source: Sample Wire\n"
    "Published: 2024-01-01\n"
    "URL: https://example.com/sample\n"
    "Description: A sample description.\n"
    "Article text:\nSample article body.\n\n"
    "Return exactly this block, replacing only the summary text:\n\n"
    "DATABASE_ENTRY:\n"
    "### Sample article\n"
    "Metadata:\n"
    "- Source: Sample Wire\n"
    "- Published: 2024-01-01\n"
    "- URL: https://example.com/sample\n\n"
    "Summary:\n"
    "<4-7 sentence article summary in plain prose, no brackets>"
)

_SAMPLE_SOURCE_SUMMARY_LINES = (
    "S1:\n"
    "Title: Sample article\n"
    "Article ID: a1\n"
    "Source: Sample Wire\n"
    "Published: 2024-01-01\n"
    "URL: https://example.com/sample\n"
    "Summary: A sample summary.\n"
    "Cleaned article evidence to paraphrase, not quote: Sample evidence.\n"
    "Citation precedence: Cite this source only for facts it directly supports."
)

_TEMPLATE_SAMPLE_VALUES: dict[str, dict[str, str]] = {
    "article_summary": {
        "now_label": "Monday, January 1, 2024",
        "recent_window_hours": "24",
        "article_payload": _SAMPLE_ARTICLE_PAYLOAD,
        "output_contract": ARTICLE_SUMMARY_OUTPUT_CONTRACT,
        "editorial_instructions": "Sample editorial instructions.",
    },
    "story_scale_screening": {
        "story_blocks": (
            "Story key: sample-key\n"
            "Story title: Sample story\n"
            "Story draft: Sample draft text.\n"
            "Article summaries:\n- A sample summary."
        ),
        "scale_contract": STORY_SCALE_SCREENING_JSON_CONTRACT,
        "editorial_instructions": "Sample editorial instructions.",
    },
    "story_drafting": {
        "now_label": "Monday, January 1, 2024",
        "story_title": "Sample story",
        "source_summary_lines": _SAMPLE_SOURCE_SUMMARY_LINES,
        "citation_contract": STORY_DRAFTING_CITATION_CONTRACT,
        "output_contract": STORY_DRAFTING_OUTPUT_CONTRACT,
        "editorial_instructions": "Sample editorial instructions.",
    },
    "title_generation": {
        "report_title": "Sample report title",
        "synthesis_body": "Sample synthesized news body.",
        "title_contract": TITLE_GENERATION_JSON_CONTRACT,
        "overlay_protocol": IMAGE_ART_OVERLAY_PROTOCOL,
        "editorial_instructions": "Sample editorial instructions.",
    },
    "image_art_direction": {
        "synthesis_body": "Sample synthesized news body.",
        "image_contract": IMAGE_ART_JSON_CONTRACT,
        "editorial_instructions": "Sample editorial instructions.",
    },
}


def _allowed_placeholders(task: str) -> set[str]:
    return (
        set(PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS.get(task, ()))
        | set(PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS.get(task, ()))
        | set(PROMPT_TEMPLATE_OPTIONAL_PLACEHOLDERS)
    )


def parse_prompt_template_override(
    task: str,
    raw_json: str,
    *,
    source: str = "prompt template override",
) -> dict[str, str]:
    """Parse a raw ``NEWS_PROMPT_TEMPLATE_<TASK>`` JSON string.

    Accepts only a JSON object with non-empty string ``system`` and ``user``
    values and no unknown keys. Returns the ``{"system": ..., "user": ...}``
    body. Raises deterministic ``ValueError`` messages naming ``task`` and
    ``source`` (env var / preset path) so typos fail fast at the config,
    preset, or UI boundary.
    """
    if task not in PROMPT_TASKS:
        raise ValueError(
            f"{source} for unknown prompt task {task!r}; "
            f"valid tasks: {', '.join(PROMPT_TASKS)}."
        )
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{source} for task {task!r} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ValueError(
            f"{source} for task {task!r} must be a JSON object "
            "with 'system' and 'user' strings."
        )
    unknown_keys = sorted(set(payload) - {"system", "user"})
    if unknown_keys:
        raise ValueError(
            f"{source} for task {task!r} contains unknown key(s) "
            f"{unknown_keys!r}; expected only 'system' and 'user'."
        )
    system = payload.get("system")
    user = payload.get("user")
    if not isinstance(system, str) or not system.strip():
        raise ValueError(
            f"{source} for task {task!r} requires a non-empty string 'system'."
        )
    if not isinstance(user, str) or not user.strip():
        raise ValueError(
            f"{source} for task {task!r} requires a non-empty string 'user'."
        )
    return {"system": system, "user": user}


def validate_prompt_template(task: str, template: Mapping[str, str]) -> list[str]:
    """Return deterministic violations for a parsed template body.

    Checks, in order: non-empty string roles; ``string.Template`` syntax;
    unknown placeholders against the task's allowlist; required dynamic and
    code-owned contract placeholders; substitution with the task's fixed
    sample value map; and ``validate_prompt_contract()`` on the rendered
    system+user text. An empty result means the template is valid.
    """
    if task not in PROMPT_TASKS:
        return [
            f"unknown prompt task {task!r}; "
            f"valid tasks: {', '.join(PROMPT_TASKS)}."
        ]
    violations: list[str] = []
    system = template.get("system")
    user = template.get("user")
    if not isinstance(system, str) or not system.strip():
        violations.append("'system' must be a non-empty string")
    if not isinstance(user, str) or not user.strip():
        violations.append("'user' must be a non-empty string")
    if violations:
        return violations

    allowed = _allowed_placeholders(task)
    required = (
        *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS[task],
        *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS[task],
    )
    system_template = Template(system)
    user_template = Template(user)
    # substitute() is the only operation that detects malformed ``$`` syntax
    # (get_identifiers() silently skips invalid placeholders), so it runs
    # before the identifier checks. The sample map covers every allowed
    # placeholder, so a KeyError here can only mean an unknown placeholder.
    try:
        sample_values = _TEMPLATE_SAMPLE_VALUES[task]
        system_text = system_template.substitute(sample_values)
        user_text = user_template.substitute(sample_values)
    except ValueError as error:
        violations.append(f"malformed placeholder syntax: {error}")
        return violations
    except KeyError as error:
        violations.append(
            f"unknown placeholder(s) [{error.args[0]!r}]; "
            f"allowed placeholders: {sorted(allowed)}"
        )
        return violations

    identifiers = set(system_template.get_identifiers()) | set(
        user_template.get_identifiers()
    )
    unknown = sorted(identifiers - allowed)
    if unknown:
        violations.append(
            f"unknown placeholder(s) {unknown!r}; "
            f"allowed placeholders: {sorted(allowed)}"
        )
    missing_required = [name for name in required if name not in identifiers]
    if missing_required:
        violations.append(f"missing required placeholder(s) {missing_required!r}")
    if unknown or missing_required:
        return violations

    rendered = "\n\n".join((system_text, user_text))

    missing_markers = validate_prompt_contract(task, rendered)
    if missing_markers:
        violations.append(
            f"rendered template violates the pipeline-owned output contract; "
            f"missing markers: {missing_markers!r}"
        )
    return violations


def assert_valid_prompt_template(task: str, template: Mapping[str, str]) -> None:
    """Raise ``ValueError`` listing every violation for a parsed template body."""
    violations = validate_prompt_template(task, template)
    if violations:
        raise ValueError(
            f"Prompt template for task {task!r} is invalid: "
            + "; ".join(violations)
        )


def resolve_prompt_templates(
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, PromptTemplate]:
    """Return built-in templates for untouched tasks and validated custom
    templates for overridden tasks.

    Raises ``ValueError`` naming the task and the violations for any override
    that fails validation, so an invalid template fails closed before any
    model call.
    """
    templates: dict[str, PromptTemplate] = {}
    for task in PROMPT_TASKS:
        default = DEFAULT_PROMPT_TEMPLATES[task]
        raw = (overrides or {}).get(task)
        if raw is None:
            templates[task] = default
            continue
        if not isinstance(raw, dict):
            raise ValueError(
                f"Prompt template override for task {task!r} must be an object "
                "with 'system' and 'user' strings."
            )
        body = {"system": raw.get("system"), "user": raw.get("user")}
        assert_valid_prompt_template(task, body)
        templates[task] = PromptTemplate(
            task=task,
            label=default.label,
            system=str(body["system"]),
            user=str(body["user"]),
            required_placeholders=default.required_placeholders,
            optional_placeholders=default.optional_placeholders,
        )
    return templates


def render_prompt_template(
    task: str,
    template: PromptTemplate,
    values: Mapping[str, str],
) -> tuple[str, str]:
    """Render ``(system, user)`` text from a resolved template.

    ``values`` must supply every placeholder the template uses (the stage
    builders supply the full task-specific value map). Raises ``ValueError``
    for unknown placeholders, malformed syntax, missing values or required
    placeholders, or a rendered pair that fails ``validate_prompt_contract``.
    Values are inserted verbatim and are never re-parsed as templates.
    """
    if task not in PROMPT_TASKS:
        raise ValueError(
            f"Unknown prompt task {task!r}; valid tasks: {', '.join(PROMPT_TASKS)}."
        )
    allowed = _allowed_placeholders(task)
    required = (
        *PROMPT_TEMPLATE_DYNAMIC_PLACEHOLDERS[task],
        *PROMPT_TEMPLATE_CONTRACT_PLACEHOLDERS[task],
    )
    try:
        identifiers = set(Template(template.system).get_identifiers()) | set(
            Template(template.user).get_identifiers()
        )
    except ValueError as error:
        raise ValueError(
            f"Prompt template for task {task!r} has malformed placeholder syntax: {error}"
        ) from error
    unknown = sorted(identifiers - allowed)
    if unknown:
        raise ValueError(
            f"Prompt template for task {task!r} uses unknown placeholder(s) {unknown!r}"
        )
    missing_required = [name for name in required if name not in identifiers]
    if missing_required:
        raise ValueError(
            f"Prompt template for task {task!r} is missing required "
            f"placeholder(s) {missing_required!r}"
        )
    missing_values = sorted(name for name in identifiers if name not in values)
    if missing_values:
        raise ValueError(
            f"Prompt template for task {task!r} is missing value(s) for "
            f"placeholder(s) {missing_values!r}"
        )
    try:
        system_text = Template(template.system).substitute(values)
        user_text = Template(template.user).substitute(values)
    except (KeyError, ValueError) as error:
        raise ValueError(
            f"Prompt template for task {task!r} failed to render: {error}"
        ) from error
    missing_markers = validate_prompt_contract(task, f"{system_text}\n\n{user_text}")
    if missing_markers:
        raise ValueError(
            f"Prompt contract violation for {task!r}; missing markers: {missing_markers!r}"
        )
    return system_text, user_text


def list_prompt_templates() -> list[dict[str, Any]]:
    """Return JSON-ready catalog records (task, label, env var, default source
    text, and placeholder metadata) for the UI schema."""
    records: list[dict[str, Any]] = []
    for task in PROMPT_TASKS:
        template = DEFAULT_PROMPT_TEMPLATES[task]
        records.append(
            {
                "task": template.task,
                "label": template.label,
                "env_var": PROMPT_TEMPLATE_ENV_VARS[task],
                "system": template.system,
                "user": template.user,
                "required_placeholders": list(template.required_placeholders),
                "optional_placeholders": list(template.optional_placeholders),
                "placeholder_descriptions": {
                    name: PROMPT_TEMPLATE_PLACEHOLDER_DESCRIPTIONS[name]
                    for name in (*template.required_placeholders, *template.optional_placeholders)
                },
            }
        )
    return records
