#!/usr/bin/env python3
"""Move one configured-repository issue to a configured project lane."""

from __future__ import annotations

import os
import sys

try:
    from .pm_harness.github import fetch_project, move_to_lane
    from .pm_harness.runtime import gh, load_config
except ImportError:  # direct `python3 automation/move_item.py`
    from pm_harness.github import fetch_project, move_to_lane
    from pm_harness.runtime import gh, load_config


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: move_item.py <issue-number> <lane>", file=sys.stderr)
        return 2
    try:
        issue_number = int(sys.argv[1])
    except ValueError:
        print(f"invalid issue number: {sys.argv[1]!r}", file=sys.stderr)
        return 2
    lane = sys.argv[2]
    cfg = load_config()
    if lane not in cfg["lanes"]:
        print(
            f"unknown lane {lane!r}; choose one of {', '.join(cfg['lanes'])}",
            file=sys.stderr,
        )
        return 2

    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()
    try:
        project_id, field_id, options, items = fetch_project(cfg, env)
    except RuntimeError as exc:
        print(f"cannot read project: {exc}", file=sys.stderr)
        return 1

    item = next(
        (
            candidate
            for candidate in items
            if (candidate.get("content") or {}).get("__typename") == "Issue"
            and (candidate.get("content") or {}).get("number") == issue_number
            and (candidate.get("content") or {})
            .get("repository", {})
            .get("nameWithOwner")
            == cfg["repo"]
        ),
        None,
    )
    if item is None:
        print(f"issue #{issue_number} is not on the configured board", file=sys.stderr)
        return 1
    option_id = options.get(lane)
    if option_id is None:
        print(f"lane {lane!r} is not present on the configured board", file=sys.stderr)
        return 1
    if not move_to_lane(cfg, env, project_id, item["id"], field_id, option_id):
        print(f"failed to move issue #{issue_number} to {lane}", file=sys.stderr)
        return 1
    print(f"moved issue #{issue_number} -> {lane}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
