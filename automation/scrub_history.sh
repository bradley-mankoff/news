#!/usr/bin/env bash
# Scrub personal data from git history with git-filter-repo (GATED).
#
# Usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH] [--keep-ref REF]
#
# Performs the history scrub described in docs/security/history-scrub.md on a
# fresh mirror clone: generate replacement files (from redacted constants
# below), run git filter-repo, then verify with automation/security_audit.py
# (history-only). Push commands are PRINTED by default; a human passes
# --execute to actually force-push develop, main, tags, and any explicitly
# supplied --keep-ref values.
#
# DANGER: rewriting history invalidates every clone, worktree, open PR diff,
# and the poller's automation/state.json. Run only when no issues are In
# Progress and only with explicit human approval.
#
# Environment:
#   REPO_URL   remote to clone (default: https://github.com/bradley-mankoff/news)
#   WORKDIR    scratch dir for the mirror clone (default: /tmp/news-scrub)
#   SCRUB_USER username in the audited personal paths (default: home)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_SCRIPT="$SCRIPT_DIR/security_audit.py"

REPO_URL="${REPO_URL:-https://github.com/bradley-mankoff/news}"
WORKDIR="${WORKDIR:-/tmp/news-scrub}"
SCRUB_USER="${SCRUB_USER:-home}"

# The env-controlled destructive path below is `rm -rf "$WORKDIR"`; guard it
# against typos like WORKDIR=/ or $HOME before anything can reach it.
# Require an absolute path (kills relative typos like `..` or `.`) and reject
# top-level/system directories (kills `/tmp` or `/Users` typos).
case "$WORKDIR" in
  ""|"/"|"$HOME"|"$HOME"/*)
    echo "error: refusing unsafe WORKDIR: $WORKDIR (must be a scratch dir, e.g. /tmp/news-scrub)" >&2
    exit 1
    ;;
  /*) ;;
  *)
    echo "error: refusing unsafe WORKDIR: $WORKDIR (must be an absolute path)" >&2
    exit 1
    ;;
esac
case "$WORKDIR" in
  /tmp|/var|/Users|/home|/etc|/System|/private|/opt|/usr|/bin|/sbin)
    echo "error: refusing unsafe WORKDIR: $WORKDIR (top-level system directory)" >&2
    exit 1
    ;;
esac

DRY_RUN=1
MAILMAP=""
KEEP_REFS=()

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
    --keep-ref)
      if [ $# -lt 2 ]; then
        echo "error: --keep-ref requires a full refs/heads/* or refs/tags/* ref" >&2
        exit 2
      fi
      KEEP_REFS+=("$2")
      shift
      ;;
    --keep-ref=*) KEEP_REFS+=("${arg#--keep-ref=}") ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *)
      echo "error: unknown argument: $arg" >&2
      echo "usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH] [--keep-ref REF]" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "${#KEEP_REFS[@]}" -gt 0 ]; then
  for ref in "${KEEP_REFS[@]}"; do
    case "$ref" in
      refs/heads/*|refs/tags/*) ;;
      *)
        echo "error: --keep-ref must be refs/heads/* or refs/tags/*: $ref" >&2
        exit 2
        ;;
    esac
    if ! git check-ref-format "$ref" >/dev/null 2>&1; then
      echo "error: invalid --keep-ref: $ref" >&2
      exit 2
    fi
  done
fi

DRY_RUN_MARKER="$SCRIPT_DIR/.scrub-dryrun-ok"
if [ "$DRY_RUN" -eq 0 ]; then
  # A prior dry-run is not evidence for an execute run that failed or was
  # interrupted; invalidate it before starting the destructive path.
  rm -f "$DRY_RUN_MARKER"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY RUN: clone + rewrite + verify will run; push commands will only be printed."
else
  echo "==> EXECUTE MODE: push commands WILL be run. You are rewriting public history."
fi

command -v git-filter-repo >/dev/null 2>&1 || {
  echo "error: git-filter-repo not found. Install it first: brew install git-filter-repo" >&2
  exit 1
}

echo "==> Recording source refs for dry-run freshness checks"
SOURCE_REFS_SHA256="$(git ls-remote "$REPO_URL" "refs/heads/*" "refs/tags/*" | shasum -a 256 | awk '{print $1}')"

echo "==> Cloning mirror of $REPO_URL into $WORKDIR"
rm -rf "$WORKDIR"
git clone --mirror "$REPO_URL" "$WORKDIR"

# --- generate replacement files from redacted constants ----------------------
# The [@] / [.] / [USERNAME] placeholders keep this script free of raw
# personal data; sed restores them before filter-repo reads the files.
REPLACEMENTS_TXT="$(mktemp)"
MESSAGES_TXT="$(mktemp)"

# VERIFY_PASSED gates the cleanup trap's mirror removal: once the rewritten
# history has been verified clean, the mirror holds the ONLY copy of it and
# must survive any later failure (e.g. a transient push error) so the human
# can retry the push instead of redoing the entire clone+rewrite cycle.
VERIFY_PASSED=0

cleanup() {
  rm -f "$REPLACEMENTS_TXT" "$MESSAGES_TXT"
  # On failure BEFORE verification, remove the mirror too: it holds the raw
  # pre-scrub history (the very data the scrub is meant to contain). Keep it
  # on success (and after verification) so the human can inspect the
  # rewritten history before pushing.
  if [ "${1:-0}" -ne 0 ] && [ "$VERIFY_PASSED" -eq 0 ]; then
    echo "note: removing $WORKDIR (scrub did not complete); the mirror holds raw" >&2
    echo "      pre-scrub history and must not linger." >&2
    rm -rf "$WORKDIR"
  fi
}
trap 'cleanup $?' EXIT

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
if [ "${#KEEP_REFS[@]}" -gt 0 ]; then
  for ref in "${KEEP_REFS[@]}"; do
    PUSH_CMDS+=("git -C $WORKDIR push --force origin $ref:$ref")
  done
fi

if [ "$DRY_RUN" -eq 1 ]; then
  cat > "$DRY_RUN_MARKER" <<EOF
{
  "verified_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "repo_url": "$REPO_URL",
  "workdir": "$WORKDIR",
  "source_refs_sha256": "$SOURCE_REFS_SHA256"
}
EOF
  echo "==> DRY RUN: push commands below were NOT executed."
  printf '    %s\n' "${PUSH_CMDS[@]}"
  echo "    Verified dry-run manifest: $DRY_RUN_MARKER"
  echo "    Re-run with --execute to push. After pushing: update all local checkouts,"
  echo "    re-init automation/state.json, and contact GitHub Support to purge cached views."
else
  echo "==> Pushing rewritten history (develop, main, tags, requested keep refs)"
  # No eval: the commands are fixed and known, so execute them directly with
  # a quoted WORKDIR (avoids shell injection via an env-controlled path).
  # Check each push individually: a partial force-push is the one failure
  # mode where the human must know exactly which refs moved. VERIFY_PASSED is
  # already 1 here, so the cleanup trap keeps the rewritten mirror for retry.
  PUSH_FAILED=0
  for target in "origin develop" "origin main"; do
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
  if [ "$PUSH_FAILED" -eq 0 ] && ! git -C "$WORKDIR" push --force origin --tags; then
    echo "error: push failed: git push --force origin --tags" >&2
    echo "       Earlier pushes in this list may already have reached the remote." >&2
    echo "       The rewritten mirror is kept at $WORKDIR for retry/inspection." >&2
    PUSH_FAILED=1
  elif [ "$PUSH_FAILED" -eq 0 ]; then
    echo "    ok: git push --force origin --tags"
  fi
  if [ "$PUSH_FAILED" -eq 0 ] && [ "${#KEEP_REFS[@]}" -gt 0 ]; then
    for ref in "${KEEP_REFS[@]}"; do
      if ! git -C "$WORKDIR" push --force origin "$ref:$ref"; then
        echo "error: push failed: git push --force origin $ref:$ref" >&2
        echo "       Earlier pushes in this list may already have reached the remote." >&2
        echo "       The rewritten mirror is kept at $WORKDIR for retry/inspection." >&2
        PUSH_FAILED=1
        break
      fi
      echo "    ok: git push --force origin $ref:$ref"
    done
  fi
  if [ "$PUSH_FAILED" -eq 1 ]; then
    echo "error: force-push incomplete; do NOT re-run the scrub from scratch without" >&2
    echo "       checking which refs moved (see the messages above)." >&2
    exit 1
  fi
  echo "==> Done. Re-clone all local checkouts; old clones retain the pre-scrub history."
fi
