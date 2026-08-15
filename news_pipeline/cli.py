"""Command-line entry point for the daily news pipeline."""

from __future__ import annotations

import json
import os
import sys
from typing import Callable

from .config import (
    MODEL_BACKEND_EXTERNAL,
    apply_run_preset_to_environment,
    ensure_codex_safe_model_reference,
    load_runtime_config,
    reject_removed_topic_env_vars,
)
from .model_catalog import (
    HF_SEARCH_PIPELINE_TAGS,
    list_model_catalog,
    search_huggingface_models,
)
from .prompt_catalog import PROMPT_PROFILE_ENV_VAR, get_prompt_profile


USAGE = """\
Usage:
  uv run news run [--preset NAME] [--prompt-profile NAME]
  uv run news check-sources [--sources-yaml PATH] [--only-failures]
  uv run news prune-sources [--sources-yaml PATH] [--recent-days 7]
  uv run news source-languages --sources-yaml PATH [--write-languages]
  uv run news model-server-command
  uv run news codex-model-server-command
  uv run news serve-unsubscribe
  uv run news ui [--host 127.0.0.1] [--port 8766] [--open]
  uv run news schedule status [--json]
  uv run news schedule enable --time HH:MM [--preset NAME] [--delivery-mode MODE]
  uv run news schedule disable
  uv run news schedule run
  uv run news history backfill [--dry-run|--apply]
  uv run news history cleanup [--dry-run|--apply]
  uv run news history export
  uv run news models catalog [--json]
  uv run news models search --query Q [--task T] [--limit N] [--json]
"""

ACTION_ALIASES = {
    "model-server-command": "model-server-command",
    "server-command": "model-server-command",
    "--model-server-command": "model-server-command",
    "codex-model-server-command": "codex-model-server-command",
    "serve-unsubscribe": "serve-unsubscribe",
    "unsubscribe-server": "serve-unsubscribe",
    "--serve-unsubscribe": "serve-unsubscribe",
    "check-sources": "check-sources",
    "source-check": "check-sources",
    "sources": "check-sources",
    "prune-sources": "prune-sources",
    "prune-stale-sources": "prune-sources",
    "source-languages": "source-languages",
    "detect-source-languages": "source-languages",
    "source-language": "source-languages",
    "history": "history",
    "models": "models",
    "model-catalog": "models",
    "schedule": "schedule",
    "scheduler": "schedule",
    "ui": "ui",
    "local-ui": "ui",
    "control-panel": "ui",
}

def _consume_preset_arg(args: list[str]) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    preset: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--preset":
            if index + 1 >= len(args):
                raise ValueError("--preset requires a preset name.")
            preset = args[index + 1]
            index += 2
            continue
        if arg.startswith("--preset="):
            preset = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return preset, remaining


def _apply_cli_preset(preset: str | None) -> bool:
    if not preset:
        return True
    try:
        apply_run_preset_to_environment(preset)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return False
    return True


def _consume_prompt_profile_arg(args: list[str]) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    profile: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--prompt-profile":
            if index + 1 >= len(args):
                raise ValueError("--prompt-profile requires a profile name.")
            profile = args[index + 1]
            index += 2
            continue
        if arg.startswith("--prompt-profile="):
            profile = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return profile, remaining


def _apply_cli_prompt_profile(profile: str | None) -> bool:
    if not profile:
        return True
    try:
        get_prompt_profile(profile)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return False
    os.environ[PROMPT_PROFILE_ENV_VAR] = profile
    return True


def _run_pipeline_command() -> int:
    from .pipeline import run_pipeline

    run_pipeline()
    return 0


def _run_with_error_report(command: Callable[[], int]) -> int:
    """Run a command, reporting ValueError config errors to stderr."""
    try:
        return command()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2


def _print_model_server_command() -> int:
    config = load_runtime_config()
    ensure_codex_safe_model_reference(config.model_reference)
    if config.model_backend == MODEL_BACKEND_EXTERNAL or not config.model_server_command:
        print(
            f"external backend: no managed server command. "
            f"Connect {config.model_base_url} directly (model {config.model_name}).",
            file=sys.stderr,
        )
        return 2
    print(config.model_server_command)
    return 0


def _print_codex_model_server_command() -> int:
    os.environ["NEWS_CODEX_TESTING"] = "1"
    return _print_model_server_command()


def _serve_unsubscribe() -> int:
    from .pipeline import serve_unsubscribe_endpoint

    serve_unsubscribe_endpoint()
    return 0


def _run_ui(args: list[str]) -> int:
    from .ui import main as ui_main

    return ui_main(args)


def _parse_models_search_args(args: list[str]) -> tuple[str, str | None, int, bool]:
    """Parse `news models search` arguments into (query, pipeline_tag, limit, json)."""
    query = ""
    pipeline_tag: str | None = None
    limit = 20
    as_json = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--query":
            if index + 1 >= len(args):
                raise ValueError("--query requires a value.")
            query = args[index + 1]
            index += 2
            continue
        if arg.startswith("--query="):
            query = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--task":
            if index + 1 >= len(args):
                raise ValueError("--task requires a value.")
            pipeline_tag = args[index + 1]
            index += 2
            continue
        if arg.startswith("--task="):
            pipeline_tag = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--limit":
            if index + 1 >= len(args):
                raise ValueError("--limit requires a value.")
            limit = _parse_models_search_limit(args[index + 1])
            index += 2
            continue
        if arg.startswith("--limit="):
            limit = _parse_models_search_limit(arg.split("=", 1)[1])
            index += 1
            continue
        if arg == "--json":
            as_json = True
            index += 1
            continue
        raise ValueError(f"Unexpected argument for models search: {arg}")
    if pipeline_tag is not None and pipeline_tag not in HF_SEARCH_PIPELINE_TAGS:
        valid = ", ".join(HF_SEARCH_PIPELINE_TAGS)
        raise ValueError(
            f"Unknown search task {pipeline_tag!r}. Valid tasks: {valid}"
        )
    if not query.strip():
        raise ValueError("models search requires --query (e.g. --query gemma).")
    return query, pipeline_tag, limit, as_json


def _parse_models_search_limit(raw: str) -> int:
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        raise ValueError(f"--limit must be an integer, got {raw!r}.") from None


def _run_models(args: list[str]) -> int:
    """Run the `news models` command (catalog / search subcommands).

    Network calls happen only for `search`; `catalog` is offline-first.
    """
    if not args:
        raise ValueError("models requires a subcommand: catalog or search.")
    subcommand = args[0]
    rest = args[1:]
    if subcommand == "catalog":
        unexpected = [arg for arg in rest if arg != "--json"]
        if unexpected:
            raise ValueError(
                f"Unexpected arguments for models catalog: {' '.join(unexpected)}"
            )
        as_json = "--json" in rest
        entries = list_model_catalog()
        if as_json:
            print(json.dumps(entries, indent=2))
        else:
            for entry in entries:
                context = "-" if entry["context_length"] is None else str(entry["context_length"])
                print(
                    f"{entry['alias']:<20} {entry['backend']:<8} "
                    f"ctx={context:<8} {entry['hf_url']}"
                )
        return 0
    if subcommand == "search":
        # Detect --json before parsing so validation failures use the same
        # JSON error envelope as lookup failures (issue #93).
        as_json = "--json" in rest
        query = ""
        for index, arg in enumerate(rest):
            if arg == "--query" and index + 1 < len(rest):
                query = rest[index + 1]
            elif arg.startswith("--query="):
                query = arg.split("=", 1)[1]
        try:
            query, pipeline_tag, limit, _ = _parse_models_search_args(rest)
            results = search_huggingface_models(
                query, pipeline_tag=pipeline_tag, limit=limit
            )
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            if as_json:
                print(
                    json.dumps(
                        {"query": query, "models": [], "error": str(exc)}, indent=2
                    )
                )
            return 2
        if as_json:
            print(json.dumps({"query": query, "models": results}, indent=2))
        else:
            for item in results:
                fit = item.get("runtime_fit") or {}
                print(f"{item['id']} [{fit.get('status', 'unknown')}] {fit.get('reason', '')}")
        return 0
    raise ValueError(f"Unknown models subcommand: {subcommand!r}. Valid: catalog, search.")


def _run_history(args: list[str]) -> int:
    from .history_store import parse_history_args

    config = load_runtime_config(materialize_outputs=False)
    try:
        result = parse_history_args(
            args,
            output_dir=config.output_dir,
            db_path=config.history_db_path,
            export_csv=config.history_export_csv,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(result.format())
    return 0


def _format_schedule_status(payload: dict[str, object]) -> str:
    """Compact human-readable schedule status (no env/plist/launchctl text)."""
    if payload.get("error"):
        return f"Daily schedule: unavailable — {payload['error']}"
    last = payload.get("last_run") or {}
    if not isinstance(last, dict):
        last = {}
    lines = [
        "Daily schedule: "
        + ("enabled" if payload.get("enabled") else "disabled"),
        "  Time: " + str(payload.get("time") or "—")
        + " (local time, once daily)",
        "  Preset: " + str(payload.get("preset_id") or "default settings"),
        "  Delivery: " + str(payload.get("delivery_mode") or "owner"),
        "  launchd: " + str(payload.get("launchd_status") or "unknown"),
    ]
    last_status = str(last.get("status") or "never")
    run_id = str(last.get("run_id") or "")
    lines.append(
        "  Last run: " + last_status + (f" (run {run_id})" if run_id else "")
    )
    error_message = str(last.get("error_message") or "").strip()
    if error_message:
        lines.append(f"  Last error: {error_message}")
    return "\n".join(lines)


def _run_schedule_status(args: list[str]) -> int:
    from .scheduler import schedule_status

    as_json = "--json" in args
    unexpected = [arg for arg in args if arg != "--json"]
    if unexpected:
        raise ValueError(
            f"Unexpected arguments for schedule status: {' '.join(unexpected)}"
        )
    payload = schedule_status()
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_format_schedule_status(payload))
    return 0


def _run_schedule_enable(args: list[str]) -> int:
    from .scheduler import enable_schedule

    time_value = ""
    preset = ""
    delivery_mode: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--time":
            if index + 1 >= len(args):
                raise ValueError("--time requires a value (HH:MM).")
            time_value = args[index + 1]
            index += 2
            continue
        if arg.startswith("--time="):
            time_value = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--preset":
            if index + 1 >= len(args):
                raise ValueError("--preset requires a preset name.")
            preset = args[index + 1]
            index += 2
            continue
        if arg.startswith("--preset="):
            preset = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--delivery-mode":
            if index + 1 >= len(args):
                raise ValueError("--delivery-mode requires a mode.")
            delivery_mode = args[index + 1]
            index += 2
            continue
        if arg.startswith("--delivery-mode="):
            delivery_mode = arg.split("=", 1)[1]
            index += 1
            continue
        raise ValueError(f"Unexpected argument for schedule enable: {arg}")
    if not time_value:
        raise ValueError("schedule enable requires --time HH:MM.")
    schedule = enable_schedule(
        time_value, preset_id=preset, delivery_mode=delivery_mode
    )
    print(
        f"Daily schedule enabled: {schedule.hour:02d}:{schedule.minute:02d} "
        f"(local time, once daily), preset "
        f"{schedule.preset_id or 'default settings'}, "
        f"delivery {schedule.delivery_mode}, "
        f"launchd {schedule.launchd_status}."
    )
    return 0


def _run_schedule(args: list[str]) -> int:
    from .scheduler import disable_schedule, run_scheduled

    if not args:
        raise ValueError(
            "schedule requires a subcommand: status, enable, disable, run."
        )
    subcommand = args[0]
    rest = args[1:]
    if subcommand == "status":
        return _run_schedule_status(rest)
    if subcommand == "enable":
        return _run_schedule_enable(rest)
    if subcommand == "disable":
        if rest:
            raise ValueError(
                f"Unexpected arguments for schedule disable: {' '.join(rest)}"
            )
        disable_schedule()
        print("Daily schedule disabled.")
        return 0
    if subcommand == "run":
        if rest:
            raise ValueError(
                f"Unexpected arguments for schedule run: {' '.join(rest)}"
            )
        return run_scheduled()
    raise ValueError(
        f"Unknown schedule subcommand: {subcommand!r}. "
        "Valid: status, enable, disable, run."
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    command = args.pop(0) if args else "run"
    action = ACTION_ALIASES.get(command)
    if action == "ui":
        return _run_ui(args)
    if action == "history":
        return _run_history(args)
    if action == "models":
        return _run_with_error_report(lambda: _run_models(args))
    if action == "schedule":
        return _run_with_error_report(lambda: _run_schedule(args))

    reject_removed_topic_env_vars()

    if command == "run":
        try:
            preset, args = _consume_preset_arg(args)
            profile, args = _consume_prompt_profile_arg(args)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        if not _apply_cli_preset(preset):
            return 2
        if not _apply_cli_prompt_profile(profile):
            return 2
        if args:
            print(f"Unexpected arguments for run: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_with_error_report(_run_pipeline_command)

    if action == "model-server-command":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_with_error_report(_print_model_server_command)
    if action == "codex-model-server-command":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _run_with_error_report(_print_codex_model_server_command)
    if action == "serve-unsubscribe":
        if args:
            print(f"Unexpected arguments for {command}: {' '.join(args)}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return _serve_unsubscribe()
    if action == "check-sources":
        from .source_checks import main as source_check_main

        return source_check_main(args)
    if action == "prune-sources":
        from .source_checks import main as source_check_main

        return source_check_main(["--prune-inactive", *args])
    if action == "source-languages":
        from .source_checks import main as source_check_main

        return source_check_main(["--detect-languages", *args])

    print(f"Unknown command: {command}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2

