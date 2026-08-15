#!/usr/bin/env bash
# Scrub personal data from git history with git-filter-repo (GATED).
#
# Usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH]
#
# Dry-run creates and verifies a fresh mirror, restores its origin remote, and
# retains both the rewritten mirror and a ref manifest. Execute consumes that
# exact verified mirror; it never reclones or rewrites a new snapshot. Push
# commands are PRINTED by default; a human passes --execute to actually
# force-push develop + main + tags.
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
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_SCRIPT="$SCRIPT_DIR/security_audit.py"

REPO_URL="${REPO_URL:-https://github.com/bradley-mankoff/news}"
WORKDIR="${WORKDIR:-/tmp/news-scrub}"
SCRUB_USER="${SCRUB_USER:-home}"
STATE_FILE="${WORKDIR}.scrub-state"

# The next command in dry-run mode is `rm -rf "$WORKDIR"`; guard the
# env-controlled destructive path against typos like WORKDIR=/ or $HOME.
case "$WORKDIR" in
  ""|"/"|"$HOME"|"$HOME"/*)
    echo "error: refusing unsafe WORKDIR: $WORKDIR (must be a scratch dir, e.g. /tmp/news-scrub)" >&2
    exit 1
    ;;
esac

case "$REPO_URL" in
  *[[:space:]]*)
    echo "error: REPO_URL must not contain whitespace" >&2
    exit 2
    ;;
esac

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
    -h|--help) sed -n '1,22p' "$0"; exit 0 ;;
    *)
      echo "error: unknown argument: $arg" >&2
      echo "usage: scrub_history.sh [--dry-run|--execute] [--mailmap PATH]" >&2
      exit 2
      ;;
  esac
  shift
done

# These manifests deliberately contain only ref names and object IDs. They
# bind execute mode to the exact source and rewritten graphs reviewed in
# dry-run mode without storing the sensitive mirror's contents in logs.
manifest_from_repo() {
  git -C "$1" for-each-ref --format='%(refname) %(objectname)' | LC_ALL=C sort
}

manifest_from_remote() {
  git ls-remote --refs "$1" |
    awk -F '\t' 'NF == 2 { print $2 " " $1 }' |
    LC_ALL=C sort
}

target_manifest_from_repo() {
  local repo="$1"
  local ref
  for ref in refs/heads/develop refs/heads/main; do
    git -C "$repo" show-ref --verify --quiet "$ref" || {
      echo "error: verified mirror is missing required ref $ref" >&2
      return 1
    }
  done
  git -C "$repo" for-each-ref \
    --format='%(refname) %(objectname)' \
    refs/heads/develop refs/heads/main refs/tags |
    LC_ALL=C sort
}

state_manifest() {
  local kind="$1"
  awk -v kind="$kind" '$1 == kind && NF == 3 { print $2 " " $3 }' "$STATE_FILE" |
    LC_ALL=C sort
}

compare_manifest() {
  local expected="$1"
  local actual="$2"
  local description="$3"
  if ! cmp -s "$expected" "$actual"; then
    echo "error: $description changed; discard the retained mirror and rerun --dry-run" >&2
    return 1
  fi
}

SOURCE_MANIFEST=""
REMOTE_MANIFEST=""
LOCAL_TARGET_MANIFEST=""
APPROVED_TARGET_MANIFEST=""
STATE_TMP=""
REPLACEMENTS_TXT=""
MESSAGES_TXT=""
REMOVE_ON_FAILURE=0

cleanup() {
  local status=$?
  rm -f "$SOURCE_MANIFEST" "$REMOTE_MANIFEST" "$LOCAL_TARGET_MANIFEST" \
    "$APPROVED_TARGET_MANIFEST" "$STATE_TMP" "$REPLACEMENTS_TXT" "$MESSAGES_TXT"
  # On failure, remove the mirror and approval state too: the mirror may be
  # partially rewritten or may no longer correspond to the remote snapshot.
  # Keep both on success so a human can inspect the rewritten graph and then
  # invoke --execute against this exact verified artifact.
  if [ "$status" -ne 0 ] && [ "$REMOVE_ON_FAILURE" -eq 1 ]; then
    rm -rf "$WORKDIR" "$STATE_FILE"
  fi
  return "$status"
}
trap cleanup EXIT

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY RUN: clone + rewrite + verify will run; push commands will only be printed."

  command -v git-filter-repo >/dev/null 2>&1 || {
    echo "error: git-filter-repo not found. Install it first: brew install git-filter-repo" >&2
    exit 1
  }

  echo "==> Cloning a fresh mirror into $WORKDIR"
  REMOVE_ON_FAILURE=1
  rm -rf "$WORKDIR" "$STATE_FILE"
  git clone --mirror "$REPO_URL" "$WORKDIR"

  SOURCE_MANIFEST="$(mktemp)"
  manifest_from_repo "$WORKDIR" > "$SOURCE_MANIFEST"

  # --- generate replacement files from redacted constants --------------------
  # The [@] / [.] / [USERNAME] placeholders keep this script free of raw
  # personal data; sed restores them before filter-repo reads the files.
  REPLACEMENTS_TXT="$(mktemp)"
  MESSAGES_TXT="$(mktemp)"

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

  # --- rewrite history -------------------------------------------------------
  FILTER_ARGS=(--replace-text "$REPLACEMENTS_TXT" --replace-message "$MESSAGES_TXT")
  if [ -n "$MAILMAP" ]; then
    [ -f "$MAILMAP" ] || { echo "error: mailmap not found: $MAILMAP" >&2; exit 1; }
    FILTER_ARGS+=(--mailmap "$MAILMAP")
  fi

  echo "==> Running git filter-repo (this rewrites every commit)"
  git -C "$WORKDIR" filter-repo "${FILTER_ARGS[@]}" --force

  # git-filter-repo removes origin by default after a rewrite. Restore the
  # configured URL before verification so the retained mirror is immediately
  # usable for the later execute phase and its printed commands are valid.
  if git -C "$WORKDIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$WORKDIR" remote set-url origin "$REPO_URL"
  else
    git -C "$WORKDIR" remote add origin "$REPO_URL"
  fi
else
  echo "==> EXECUTE MODE: using the retained, verified dry-run mirror."
  echo "    No clone or rewrite will run; push commands WILL be run."

  if [ ! -f "$STATE_FILE" ] || [ ! -d "$WORKDIR" ]; then
    echo "error: no verified dry-run mirror found; run --dry-run first" >&2
    exit 1
  fi
  git -C "$WORKDIR" rev-parse --is-bare-repository >/dev/null 2>&1 || {
    echo "error: retained WORKDIR is not a git mirror; run --dry-run first" >&2
    exit 1
  }
  grep -qx 'scrub-history-state-v1' "$STATE_FILE" || {
    echo "error: invalid dry-run state; run --dry-run again" >&2
    exit 1
  }
  REMOVE_ON_FAILURE=1

  STATE_REMOTE_URL="$(awk '$1 == "remote-url" { print $2; exit }' "$STATE_FILE")"
  if [ "$STATE_REMOTE_URL" != "$REPO_URL" ]; then
    echo "error: REPO_URL does not match the retained dry-run source" >&2
    exit 1
  fi

  ORIGIN_URL="$(git -C "$WORKDIR" remote get-url origin 2>/dev/null)" || {
    echo "error: retained mirror has no origin; run --dry-run again" >&2
    exit 1
  }
  if [ "$ORIGIN_URL" != "$REPO_URL" ]; then
    echo "error: retained mirror origin does not match REPO_URL" >&2
    exit 1
  fi

  SOURCE_MANIFEST="$(mktemp)"
  state_manifest source > "$SOURCE_MANIFEST"
  [ -s "$SOURCE_MANIFEST" ] || {
    echo "error: retained dry-run source manifest is empty" >&2
    exit 1
  }
  REMOTE_MANIFEST="$(mktemp)"
  manifest_from_remote "$ORIGIN_URL" > "$REMOTE_MANIFEST"
  compare_manifest "$SOURCE_MANIFEST" "$REMOTE_MANIFEST" "remote ref snapshot"

  APPROVED_TARGET_MANIFEST="$(mktemp)"
  state_manifest rewritten > "$APPROVED_TARGET_MANIFEST"
  LOCAL_TARGET_MANIFEST="$(mktemp)"
  target_manifest_from_repo "$WORKDIR" > "$LOCAL_TARGET_MANIFEST"
  compare_manifest "$APPROVED_TARGET_MANIFEST" "$LOCAL_TARGET_MANIFEST" "retained rewritten refs"
fi

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

if [ "$DRY_RUN" -eq 1 ]; then
  LOCAL_TARGET_MANIFEST="$(mktemp)"
  target_manifest_from_repo "$WORKDIR" > "$LOCAL_TARGET_MANIFEST"
  STATE_TMP="$(mktemp)"
  {
    echo "scrub-history-state-v1"
    printf 'remote-url %s\n' "$REPO_URL"
    awk '{ printf "source %s %s\n", $1, $2 }' "$SOURCE_MANIFEST"
    awk '{ printf "rewritten %s %s\n", $1, $2 }' "$LOCAL_TARGET_MANIFEST"
  } > "$STATE_TMP"
  mv "$STATE_TMP" "$STATE_FILE"
  STATE_TMP=""
fi

# --- push (dry-run default) --------------------------------------------------
PUSH_CMDS=(
  "git -C $WORKDIR push --force origin develop"
  "git -C $WORKDIR push --force origin main"
  "git -C $WORKDIR push --force --tags"
)

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY RUN: push commands below were NOT executed."
  printf '    %s\n' "${PUSH_CMDS[@]}"
  echo "    Verified mirror retained at $WORKDIR. Re-run with --execute to push this exact snapshot."
  echo "    After pushing: update all local checkouts, re-init automation/state.json,"
  echo "    and contact GitHub Support to purge cached views."
else
  echo "==> Pushing the retained rewritten history (develop, main, tags)"
  # No eval: the commands are fixed and known, so execute them directly with
  # a quoted WORKDIR (avoids shell injection via an env-controlled path).
  git -C "$WORKDIR" push --force origin develop
  git -C "$WORKDIR" push --force origin main
  git -C "$WORKDIR" push --force --tags
  echo "==> Done. Re-clone all local checkouts; old clones retain the pre-scrub history."
fi
