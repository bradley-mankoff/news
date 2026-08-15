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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "bradley-mankoff/news"
ROOT = Path(__file__).resolve().parent.parent
FREEZE_FILE = ROOT / "automation" / ".scrub-freeze.json"
DRY_RUN_MANIFEST = ROOT / "automation" / ".scrub-dryrun-manifest.json"
BACKUP_MANIFEST_NAME = ".scrub-backup-manifest.json"
ARTIFACT_MAX_AGE = timedelta(hours=24)
KEEP_LABEL = "rewrite-with-keep-set"
CLOSE_LABEL = "close-on-scrub"

# Declared identity/content set the filter must remove. Keep in sync with
# automation/scrub_history.sh replacement constants and the audit report.
DECLARED_IDENTITIES = ["bradley_mankoff"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_repo_root() -> None:
    """Reject a copied/misconfigured script before it can mutate anything."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"repository root is not a Git checkout: {ROOT} "
            f"({result.stderr.strip()[:200]})"
        )
    reported = Path(result.stdout.strip()).resolve()
    if reported != ROOT.resolve():
        raise RuntimeError(
            f"repository root mismatch: expected {ROOT}, Git reported {reported}"
        )


def _manifest_timestamp_is_fresh(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        completed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if completed.tzinfo is None:
        return False
    age = datetime.now(timezone.utc) - completed.astimezone(timezone.utc)
    return timedelta(0) <= age <= ARTIFACT_MAX_AGE


def _load_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


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


def remote_ref_snapshot() -> dict[str, str]:
    r = run(["git", "ls-remote", "--heads", "origin"])
    if r.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {r.stderr.strip()[:300]}")
    snapshot: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        snapshot[ref] = sha
    return snapshot


def remote_refs() -> list[str]:
    return sorted(remote_ref_snapshot())


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


def _mirror_ref_snapshot(path: Path) -> dict[str, str] | None:
    result = run(["git", "-C", str(path), "show-ref", "--heads"])
    if result.returncode != 0:
        return None
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    return refs


def backup_fresh_and_clean() -> tuple[bool, str]:
    """Require a recent, ref-bound, fsck-clean backup mirror."""
    current_refs = remote_ref_snapshot()
    candidates = [
        path for path in Path("/tmp").glob("news-scrub-backup-*")
        if path.is_dir()
    ]
    for candidate in sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True):
        manifest = _load_manifest(candidate / BACKUP_MANIFEST_NAME)
        if not manifest:
            continue
        if manifest.get("manifest_version") != 1:
            continue
        if manifest.get("kind") != "backup":
            continue
        if manifest.get("repo_url") != f"https://github.com/{REPO}":
            continue
        if manifest.get("mirror_path") != str(candidate.resolve()):
            continue
        if manifest.get("remote_refs") != current_refs:
            continue
        if manifest.get("fsck_status") != 0 or not _manifest_timestamp_is_fresh(
            manifest.get("completed_at")
        ):
            continue
        if _mirror_ref_snapshot(candidate) != current_refs:
            continue
        result = run(["git", "-C", str(candidate), "fsck", "--no-dangling"])
        if result.returncode == 0:
            return True, f"backup {candidate.name} is current and fsck clean"
    return False, (
        "no recent ref-matching backup manifest with a clean fsck found under "
        "/tmp/news-scrub-backup-*"
    )


def dry_run_artifact_ok() -> tuple[bool, str]:
    """Require the shell wrapper's fresh, ref-bound, audited manifest."""
    manifest = _load_manifest(DRY_RUN_MANIFEST)
    if not manifest:
        return False, f"no valid dry-run manifest at {DRY_RUN_MANIFEST}"
    if manifest.get("manifest_version") != 1 or manifest.get("kind") != "dry-run":
        return False, "dry-run manifest has an unsupported kind or version"
    if manifest.get("repo_url") != f"https://github.com/{REPO}":
        return False, "dry-run manifest targets a different repository"
    if manifest.get("audit_status") != 0:
        return False, "dry-run manifest does not record a passing audit"
    if set(manifest.get("declared_identities", [])) != set(DECLARED_IDENTITIES):
        return False, "dry-run manifest identity set does not match policy"
    if manifest.get("remote_refs") != remote_ref_snapshot():
        return False, "dry-run manifest does not match current remote refs"
    mirror = Path(str(manifest.get("mirror_path", ""))).expanduser()
    if not mirror.is_dir():
        return False, f"dry-run mirror is missing: {mirror}"
    if not _manifest_timestamp_is_fresh(manifest.get("completed_at")):
        return False, "dry-run manifest is missing or older than 24 hours"
    return True, f"dry-run manifest verified ({manifest['completed_at']})"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "plan", "execute"])
    parser.add_argument("--freeze", nargs=2, metavar=("START", "END"),
                        help="set the freeze window (START END as ISO timestamps)")
    parser.add_argument("--unfreeze", action="store_true")
    args = parser.parse_args(argv)

    try:
        validate_repo_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

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
        try:
            results = gates(freeze, prs)
        except RuntimeError as exc:
            print(f"ERROR: gate evaluation failed: {exc}", file=sys.stderr)
            return 1
        all_ok = True
        for name, ok, detail in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
            all_ok = all_ok and ok
        print(f"\nresult: {'READY' if all_ok else 'NOT READY'}")
        return 0 if all_ok else 1

    if args.mode == "plan":
        try:
            print(plan_text(prs))
        except RuntimeError as exc:
            print(f"ERROR: plan failed: {exc}", file=sys.stderr)
            return 1
        return 0

    # execute
    try:
        results = gates(freeze, prs)
    except RuntimeError as exc:
        print(f"ERROR: gate evaluation failed: {exc}", file=sys.stderr)
        return 1
    failed = [(n, d) for n, ok, d in results if not ok]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if failed:
        print("\nABORT — gates not met; nothing executed. Keep freeze up.")
        return 1

    try:
        keep, delete, close_prs = classify(prs, remote_refs())
    except RuntimeError as exc:
        print(f"ERROR: could not compute the mutation plan: {exc}", file=sys.stderr)
        return 1
    print("\nEXECUTING:")
    print(" 1. close close-on-scrub PRs:", [p["number"] for p in close_prs])
    for p in close_prs:
        result = gh(["pr", "close", str(p["number"]), "-R", REPO])
        if result.returncode != 0:
            print(
                f"ERROR: could not close PR #{p['number']}: "
                f"{result.stderr.strip()[:300]}",
                file=sys.stderr,
            )
            print("ABORT — no branch deletion or history rewrite was attempted.", file=sys.stderr)
            return 1
        print(f"    closed #{p['number']}: ok")
    print(f" 2. delete delete-set heads ({len(delete)})")
    for head in delete:
        result = run(["git", "push", "origin", f":{head.removeprefix('refs/heads/')}"])
        if result.returncode != 0:
            print(
                f"ERROR: could not delete {head}: "
                f"{result.stderr.strip()[:300]}",
                file=sys.stderr,
            )
            print("ABORT — history rewrite was not attempted; review completed remote mutations.", file=sys.stderr)
            return 1
        print(f"    deleted {head}: ok")
    print(" 3. rewrite + force-push keep-set via scrub_history.sh --execute")
    result = run(["bash", "automation/scrub_history.sh", "--execute"], timeout=1800)
    print(f"    scrub exit={result.returncode}")
    print(" 4. verify: run `python3 automation/security_audit.py` (must exit 0)")
    print("    and `git clone` fresh; `git log --all` must show zero declared identities")
    print("\nGITHUB SUPPORT REQUEST (staged, must be filed manually):")
    print(f"  Network: {REPO} — force-pushed rewritten history (scrub of personal data "
          f"on {now()}). Please run GC and purge cached refs/diffs for refs: "
          f"{', '.join(keep[:5])}.")
    print("\nDo NOT unfreeze until verification passes and contributors are notified.")
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
