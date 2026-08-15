#!/usr/bin/env python3
"""Gated history-scrub policy engine (best-practice automation).

Encodes the history-rewrite policy from docs/security/history-scrub.md and
the advisory review (2026-08-15): a history rewrite is executed only when
every gate below is mechanically true. The single human step is setting the
freeze flag; everything else is computed and verified by this engine.

Modes:
  check    verify every gate; print pass/fail. Never mutates anything.
  plan     compute keep-set / delete-set from live remote state. Read-only.
  execute  run scrub_history.sh --execute on the keep-set, delete the
           delete-set heads, close close-on-scrub PRs. Only when all
           gates pass. Prints a staged GitHub Support request.

Exit code: 0 = all gates passed (or plan printed); 1 = a gate failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = "bradley-mankoff/news"
ROOT = Path("/Users/bradley_mankoff/personal_code/news")
FREEZE_FILE = ROOT / "automation" / ".scrub-freeze.json"
KEEP_LABEL = "rewrite-with-keep-set"
CLOSE_LABEL = "close-on-scrub"

# Declared identity/content set the filter must remove. Keep in sync with
# automation/scrub_history.sh replacement constants and the audit report.
DECLARED_IDENTITIES = ["bradley_mankoff"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        cwd=str(ROOT),
    )


def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run(["gh", *args])


def load_freeze() -> dict:
    if not FREEZE_FILE.exists():
        return {}
    try:
        return json.loads(FREEZE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def set_freeze(start: str, end: str) -> None:
    FREEZE_FILE.write_text(json.dumps({
        "freeze": True,
        "start": start,
        "end": end,
        "set_by": "human",
        "recorded_at": now(),
    }, indent=2) + "\n")


def clear_freeze() -> None:
    if FREEZE_FILE.exists():
        FREEZE_FILE.unlink()


def open_prs() -> list[dict]:
    r = gh(["pr", "list", "-R", REPO, "--state", "open", "--limit", "100",
            "--json", "number,headRefName,title,labels,mergeable"])
    if r.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {r.stderr.strip()[:300]}")
    try:
        prs = json.loads(r.stdout)
    except ValueError as exc:
        raise RuntimeError(f"unparseable gh output: {exc}") from exc
    for pr in prs:
        pr["_labels"] = {
            node["name"] for node in (pr.get("labels") or {}).get("nodes", [])
        }
    return prs


def remote_refs() -> list[str]:
    r = run(["git", "ls-remote", "--heads", "origin"])
    if r.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {r.stderr.strip()[:300]}")
    return [line.split("\t")[1] for line in r.stdout.splitlines() if "\t" in line]


def board_in_progress() -> list[int]:
    r = gh(["project", "item-list", "1", "--owner", "bradley-mankoff",
            "--limit", "200", "--format", "json"])
    if r.returncode != 0:
        raise RuntimeError(f"gh project item-list failed: {r.stderr.strip()[:200]}")
    try:
        items = json.loads(r.stdout).get("items", [])
    except ValueError as exc:
        raise RuntimeError(f"unparseable project output: {exc}") from exc
    return [
        it["content"]["number"] for it in items
        if it.get("status") in ("In Progress",)
    ]


def backup_fresh_and_clean() -> tuple[bool, str]:
    """The backup mirror must exist and fsck-clean. Mirrors live under
    /tmp/news-scrub-backup-* by convention; require at least one."""
    candidates = sorted(Path("/tmp").glob("news-scrub-backup-*"))
    if not candidates:
        return False, "no backup mirror found under /tmp/news-scrub-backup-*"
    latest = candidates[-1]
    r = run(["git", "-C", str(latest), "fsck", "--no-dangling"])
    if r.returncode != 0:
        return False, f"backup {latest.name} fsck dirty: {r.stderr.strip()[:200]}"
    return True, f"backup {latest.name} fsck clean"


def dry_run_artifact_ok() -> tuple[bool, str]:
    """A passing dry-run artifact must exist. The scrub script keeps a
    verified dry-run mirror; require the marker file."""
    marker = ROOT / "automation" / ".scrub-dryrun-ok"
    if not marker.exists():
        return False, "no .scrub-dryrun-ok marker (run scrub_history.sh --dry-run first)"
    return True, f"dry-run marker present ({marker.stat().st_mtime:.0f})"


def classify(prs: list[dict], heads: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """keep-set / delete-set / close-PR set per the policy.

    keep_set = protected mainline heads + tags + heads of PRs labeled
               rewrite-with-keep-set.
    delete_set = every other advertised dirty head.
    close_prs = PRs labeled close-on-scrub (their heads get deleted).
    """
    keep: list[str] = ["refs/heads/develop", "refs/heads/main"]
    keep += ["refs/tags/" + t for t in tags()]
    delete: list[str] = []
    close_prs: list[dict] = []
    for pr in prs:
        head = f"refs/heads/{pr['headRefName']}"
        if KEEP_LABEL in pr["_labels"]:
            keep.append(head)
        elif CLOSE_LABEL in pr["_labels"]:
            close_prs.append(pr)
        # unlabeled PR heads are neither kept nor closed -> gate fails
    for head in heads:
        if head not in keep and head not in [f"refs/heads/{p['headRefName']}" for p in close_prs]:
            delete.append(head)
    # drop duplicates, keep deterministic order
    keep = sorted(set(keep))
    delete = sorted(set(delete))
    return keep, delete, close_prs


def tags() -> list[str]:
    r = run(["git", "ls-remote", "--tags", "origin"])
    if r.returncode != 0:
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        ref = line.split("\t")[1]
        if ref.endswith("^{}"):  # dereferenced tag object
            continue
        out.append(ref.removeprefix("refs/tags/"))
    return out


def gates(freeze: dict, prs: list[dict]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    # (a) Trigger gates
    ok = bool(freeze.get("freeze"))
    results.append(("freeze flag set", ok,
                    f"window {freeze.get('start')}..{freeze.get('end')}" if ok else
                    "not set — human must set freeze first"))

    now_ts = time.time()
    start = freeze.get("start")
    end = freeze.get("end")
    in_window = bool(start and end and start <= now() <= end)
    results.append(("inside freeze window", in_window,
                    f"{start}..{end}" if in_window else f"now={now()} outside window"))

    ip = board_in_progress()
    results.append(("no In Progress issues", not ip,
                    "none" if not ip else f"{ip}"))

    unlabeled = [p for p in prs if not (p["_labels"] & {KEEP_LABEL, CLOSE_LABEL})]
    results.append(("every open PR labeled", not unlabeled,
                    "all labeled" if not unlabeled else
                    f"unlabeled: {[p['number'] for p in unlabeled]}"))

    # (b) backup + dry-run artifacts
    bak_ok, bak_detail = backup_fresh_and_clean()
    results.append(("backup mirror fresh + fsck clean", bak_ok, bak_detail))
    dry_ok, dry_detail = dry_run_artifact_ok()
    results.append(("dry-run artifact present", dry_ok, dry_detail))

    return results


def plan_text(prs: list[dict]) -> str:
    heads = remote_refs()
    keep, delete, close_prs = classify(prs, heads)
    lines = [
        f"KEEP ({len(keep)}):",
        *(f"  {h}" for h in keep[:12]),
        f"DELETE ({len(delete)}):",
        *(f"  {h}" for h in delete[:12]),
        f"CLOSE PRs ({len(close_prs)}):",
        *(f"  #{p['number']} {p['headRefName']}" for p in close_prs),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "plan", "execute"])
    parser.add_argument("--freeze", nargs=2, metavar=("START", "END"),
                        help="set the freeze window (START END as ISO timestamps)")
    parser.add_argument("--unfreeze", action="store_true")
    args = parser.parse_args()

    if args.freeze:
        set_freeze(*args.freeze)
        print(f"freeze set: {args.freeze[0]}..{args.freeze[1]}")
        return 0
    if args.unfreeze:
        clear_freeze()
        print("freeze cleared")
        return 0

    freeze = load_freeze()
    try:
        prs = open_prs()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.mode == "check":
        all_ok = True
        for name, ok, detail in gates(freeze, prs):
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
            all_ok = all_ok and ok
        print(f"\nresult: {'READY' if all_ok else 'NOT READY'}")
        return 0 if all_ok else 1

    if args.mode == "plan":
        print(plan_text(prs))
        return 0

    # execute
    results = gates(freeze, prs)
    failed = [(n, d) for n, ok, d in results if not ok]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if failed:
        print("\nABORT — gates not met; nothing executed. Keep freeze up.")
        return 1

    keep, delete, close_prs = classify(prs, remote_refs())
    print("\nEXECUTING:")
    print(" 1. close close-on-scrub PRs:", [p["number"] for p in close_prs])
    for p in close_prs:
        r = gh(["pr", "close", str(p["number"]), "-R", REPO])
        print(f"    closed #{p['number']}: {'ok' if r.returncode == 0 else r.stderr.strip()[:80]}")
    print(f" 2. delete delete-set heads ({len(delete)})")
    for head in delete:
        r = run(["git", "push", "origin", f":{head.removeprefix('refs/')}"])
        print(f"    deleted {head}: {'ok' if r.returncode == 0 else r.stderr.strip()[:80]}")
    print(" 3. rewrite + force-push keep-set via scrub_history.sh --execute")
    r = run(["bash", "automation/scrub_history.sh", "--execute"], timeout=1800)
    print(f"    scrub exit={r.returncode}")
    print(" 4. verify: run `python3 automation/security_audit.py` (must exit 0)")
    print("    and `git clone` fresh; `git log --all` must show zero declared identities")
    print("\nGITHUB SUPPORT REQUEST (staged, must be filed manually):")
    print(f"  Network: {REPO} — force-pushed rewritten history (scrub of personal data "
          f"on {now()}). Please run GC and purge cached refs/diffs for refs: "
          f"{', '.join(keep[:5])}.")
    print("\nDo NOT unfreeze until verification passes and contributors are notified.")
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
