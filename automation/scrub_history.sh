#!/usr/bin/env bash
# Scrub personal data from git history with git-filter-repo (GATED).
#
# Usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH]
#
# Performs the history scrub described in docs/security/history-scrub.md on a
# fresh mirror clone: generate replacement files (from redacted constants
# below), run git filter-repo, then verify with automation/security_audit.py
# (history-only). Push commands are PRINTED by default; a human passes
# --execute to actually force-push develop + main + tags.
#
# DANGER: rewriting history invalidates every clone, worktree, open PR diff,
# and the poller's automation/state.json. Run only when no issues are In
# Progress and only with explicit human approval.
#
# Environment:
#   REPO_URL       remote to clone (default: https://github.com/bradley-mankoff/news)
#   WORKDIR        scratch dir for the rewritten mirror (default: /tmp/news-scrub)
#   BACKUP_WORKDIR scratch dir for the pre-rewrite backup (default: timestamped /tmp path)
#   SCRUB_USER     username in the audited personal paths (default: home)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_SCRIPT="$SCRIPT_DIR/security_audit.py"

REPO_URL="${REPO_URL:-https://github.com/bradley-mankoff/news}"
WORKDIR="${WORKDIR:-/tmp/news-scrub}"
BACKUP_WORKDIR="${BACKUP_WORKDIR:-/tmp/news-scrub-backup-$(date -u +%Y%m%dT%H%M%SZ)}"
SCRUB_USER="${SCRUB_USER:-home}"
MANIFEST_PATH="$SCRIPT_DIR/.scrub-dryrun-manifest.json"
BACKUP_MANIFEST_NAME=".scrub-backup-manifest.json"

# Both env-controlled destructive paths are rm -rf targets. Require absolute
# scratch paths and reject top-level/system directories before anything can
# reach them. Also reject the same path for the backup and rewritten mirror.
validate_scratch_path() {
  local name="$1"
  local value="$2"
  case "$value" in
    ""|"/"|"$HOME"|"$HOME"/*)
      echo "error: refusing unsafe $name: $value (must be a scratch dir)" >&2
      exit 1
      ;;
    /*) ;;
    *)
      echo "error: refusing unsafe $name: $value (must be an absolute path)" >&2
      exit 1
      ;;
  esac
  case "$value" in
    /tmp|/var|/Users|/home|/etc|/System|/private|/opt|/usr|/bin|/sbin)
      echo "error: refusing unsafe $name: $value (top-level system directory)" >&2
      exit 1
      ;;
  esac
}
validate_scratch_path WORKDIR "$WORKDIR"
validate_scratch_path BACKUP_WORKDIR "$BACKUP_WORKDIR"
if [ "$WORKDIR" = "$BACKUP_WORKDIR" ]; then
  echo "error: WORKDIR and BACKUP_WORKDIR must be different paths" >&2
  exit 1
fi

DRY_RUN=1
MAILMAP=""

while [ $# -gt 0 ]; do
  arg="$1"
  case "$arg" in
    --execute) DRY_RUN=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --mailmap)
      # Space-separated form advertised in usage; consume the next argument.
      if [ $# -lt 2 ]; then
        echo "error: --mailmap requires a PATH argument" >&2
        exit 2
      fi
      MAILMAP="$2"
      shift
      ;;
    --mailmap=*) MAILMAP="${arg#--mailmap=}" ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *)
      echo "error: unknown argument: $arg" >&2
      echo "usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH]" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY RUN: clone + rewrite + verify will run; push commands will only be printed."
else
  echo "==> EXECUTE MODE: push commands WILL be run. You are rewriting public history."
fi

command -v git-filter-repo >/dev/null 2>&1 || {
  echo "error: git-filter-repo not found. Install it first: brew install git-filter-repo" >&2
  exit 1
}

# The manifest records the exact pre-rewrite remote heads used by both the
# backup and the dry-run. It is written atomically only after verification.
REMOTE_REFS_TXT="$(mktemp)"
REPLACEMENTS_TXT="$(mktemp)"
MESSAGES_TXT="$(mktemp)"

# VERIFY_PASSED gates the cleanup trap's mirror removal: once the rewritten
# history has been verified clean, the mirror holds the only rewritten copy
# and must survive a later push failure for retry/inspection.
VERIFY_PASSED=0

cleanup() {
  rm -f "$REMOTE_REFS_TXT" "$REPLACEMENTS_TXT" "$MESSAGES_TXT"
  # On failure BEFORE verification, remove the mirror too: it holds the raw
  # pre-scrub history (the very data the scrub is meant to contain). Keep it
  # on success (and after verification) so the human can inspect the
  # rewritten history before pushing.
  if [ "${1:-0}" -ne 0 ] && [ "$VERIFY_PASSED" -eq 0 ]; then
    echo "note: removing raw scrub mirrors (scrub did not complete)" >&2
    rm -rf "$WORKDIR" "$BACKUP_WORKDIR"
  fi
}
trap 'cleanup $?' EXIT

if ! git ls-remote --heads "$REPO_URL" > "$REMOTE_REFS_TXT"; then
  echo "error: could not snapshot remote heads from $REPO_URL" >&2
  exit 1
fi

write_manifest() {
  local manifest_path="$1"
  local kind="$2"
  local mirror_path="$3"
  local audit_status="$4"
  local fsck_status="$5"
  python3 - "$manifest_path" "$kind" "$mirror_path" "$REPO_URL" "$REMOTE_REFS_TXT" "$audit_status" "$fsck_status" <<'PY'
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

manifest_path, kind, mirror_path, repo_url, refs_path, audit, fsck = sys.argv[1:]
refs = {}
for line in Path(refs_path).read_text(encoding="utf-8").splitlines():
    if "\t" in line:
        sha, ref = line.split("\t", 1)
        refs[ref] = sha
manifest = {
    "manifest_version": 1,
    "kind": kind,
    "repo_url": repo_url,
    "mirror_path": str(Path(mirror_path).resolve()),
    "remote_refs": refs,
    "declared_identities": ["bradley_mankoff"],
    "audit_status": int(audit),
    "fsck_status": int(fsck),
    "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
tmp = Path(manifest_path).with_name(Path(manifest_path).name + ".tmp")
tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, manifest_path)
PY
}

# Keep an exact, ref-bound backup before rewriting. The backup contains the
# pre-scrub history, so operators must protect or delete it according to the
# security runbook after the support purge is complete.
echo "==> Cloning ref-bound backup mirror into $BACKUP_WORKDIR"
rm -rf "$BACKUP_WORKDIR"
git clone --mirror "$REPO_URL" "$BACKUP_WORKDIR"
if ! git -C "$BACKUP_WORKDIR" fsck --no-dangling; then
  echo "error: backup mirror failed fsck" >&2
  exit 1
fi
write_manifest "$BACKUP_WORKDIR/$BACKUP_MANIFEST_NAME" "backup" "$BACKUP_WORKDIR" 0 0

echo "==> Cloning mirror of $REPO_URL into $WORKDIR"
rm -rf "$WORKDIR"
git clone --mirror "$REPO_URL" "$WORKDIR"

cat > "$REPLACEMENTS_TXT" <<'EOF'
bradley[@]mankoff[.]com==>bradley@example.com
bradley[.]mankoff[@]gmail[.]com==>news@example.com
aidancoon97[@]gmail[.]com==>friend1@example.com
calzacortaandres[@]gmail[.]com==>friend2@example.com
isaacmessenger[@]yahoo[.]com==>friend3@example.com
/Users/[USERNAME]/personal_code/news==>news
/Users/[USERNAME]/news==>news
EOF

cat > "$MESSAGES_TXT" <<'EOF'
/Users/[USERNAME]/personal_code/news==>news
EOF

sed -i.bak -e 's/\[@\]/@/g' -e 's/\[\.\]/./g' -e "s/\[USERNAME\]/$SCRUB_USER/g" \
  "$REPLACEMENTS_TXT" "$MESSAGES_TXT"
rm -f "$REPLACEMENTS_TXT.bak" "$MESSAGES_TXT.bak"

# --- rewrite history ---------------------------------------------------------
FILTER_ARGS=(--replace-text "$REPLACEMENTS_TXT" --replace-message "$MESSAGES_TXT")
if [ -n "$MAILMAP" ]; then
  [ -f "$MAILMAP" ] || { echo "error: mailmap not found: $MAILMAP" >&2; exit 1; }
  FILTER_ARGS+=(--mailmap "$MAILMAP")
fi

echo "==> Running git filter-repo (this rewrites every commit)"
git -C "$WORKDIR" filter-repo "${FILTER_ARGS[@]}" --force

# --- verify ------------------------------------------------------------------
echo "==> Verifying rewritten history with the audit scanner (history-only)"
if python3 "$AUDIT_SCRIPT" --history-only --repo "$WORKDIR"; then
  AUDIT_STATUS=0
else
  AUDIT_STATUS=$?
fi
if [ "$AUDIT_STATUS" -eq 1 ]; then
  echo "error: audit still finds personal data in the rewritten history." >&2
  echo "       If author emails remain, provide a --mailmap file (see docs/security/history-scrub.md)." >&2
  exit 1
elif [ "$AUDIT_STATUS" -ne 0 ]; then
  # Exit 2+ means the scanner itself failed (git error, timeout, ...), not
  # that data remains — do not advise a mailmap for a scanner crash.
  echo "error: audit scanner failed (exit $AUDIT_STATUS); fix the scanner issue before pushing." >&2
  exit 1
fi
echo "==> Verification passed: rewritten history is clean."
VERIFY_PASSED=1
write_manifest "$MANIFEST_PATH" "dry-run" "$WORKDIR" "$AUDIT_STATUS" 0

echo "==> Wrote ref-bound dry-run manifest to $MANIFEST_PATH"

# --- push (dry-run default) --------------------------------------------------
# git filter-repo removes the 'origin' remote as part of its default
# finalization, so re-add it before the push phase. This runs in dry-run too:
# the printed commands must be executable as printed.
echo "==> Re-adding origin remote for the push phase (filter-repo removes it)."
git -C "$WORKDIR" remote add origin "$REPO_URL"

PUSH_CMDS=(
  "git -C $WORKDIR push --force origin develop"
  "git -C $WORKDIR push --force origin main"
  "git -C $WORKDIR push --force --tags"
)

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY RUN: push commands below were NOT executed."
  printf '    %s\n' "${PUSH_CMDS[@]}"
  echo "    Re-run with --execute to push. After pushing: update all local checkouts,"
  echo "    re-init automation/state.json, and contact GitHub Support to purge cached views."
else
  echo "==> Pushing rewritten history (develop, main, tags)"
  # No eval: the commands are fixed and known, so execute them directly with
  # a quoted WORKDIR (avoids shell injection via an env-controlled path).
  # Check each push individually: a partial force-push is the one failure
  # mode where the human must know exactly which refs moved. VERIFY_PASSED is
  # already 1 here, so the cleanup trap keeps the rewritten mirror for retry.
  PUSH_FAILED=0
  for target in "origin develop" "origin main" "--tags"; do
    # shellcheck disable=SC2086
    if ! git -C "$WORKDIR" push --force $target; then
      echo "error: push failed: git push --force $target" >&2
      echo "       Earlier pushes in this list may already have reached the remote." >&2
      echo "       The rewritten mirror is kept at $WORKDIR for retry/inspection." >&2
      PUSH_FAILED=1
      break
    fi
    echo "    ok: git push --force $target"
  done
  if [ "$PUSH_FAILED" -eq 1 ]; then
    echo "error: force-push incomplete; do NOT re-run the scrub from scratch without" >&2
    echo "       checking which refs moved (see the messages above)." >&2
    exit 1
  fi
  echo "==> Done. Re-clone all local checkouts; old clones retain the pre-scrub history."
fi
