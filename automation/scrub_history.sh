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
#   REPO_URL   remote to clone (default: https://github.com/bradley-mankoff/news)
#   WORKDIR    scratch dir for the mirror clone (default: /tmp/news-scrub)
#   SCRUB_USER username in the audited personal paths (default: home)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_SCRIPT="$SCRIPT_DIR/security_audit.py"

REPO_URL="${REPO_URL:-https://github.com/bradley-mankoff/news}"
WORKDIR="${WORKDIR:-/tmp/news-scrub}"
SCRUB_USER="${SCRUB_USER:-home}"

# The next command in this script is `rm -rf "$WORKDIR"`; guard the one
# env-controlled destructive path against typos like WORKDIR=/ or $HOME.
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

echo "==> Cloning mirror of $REPO_URL into $WORKDIR"
rm -rf "$WORKDIR"
git clone --mirror "$REPO_URL" "$WORKDIR"

# --- generate replacement files from redacted constants ----------------------
# The [@] / [.] / [USERNAME] placeholders keep this script free of raw
# personal data; sed restores them before filter-repo reads the files.
REPLACEMENTS_TXT="$(mktemp)"
MESSAGES_TXT="$(mktemp)"

cleanup() {
  rm -f "$REPLACEMENTS_TXT" "$MESSAGES_TXT"
  # On failure, remove the mirror too: it holds the raw pre-scrub history
  # (the very data the scrub is meant to contain). Keep it on success so the
  # human can inspect the rewritten history before pushing.
  if [ "${1:-0}" -ne 0 ]; then
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
  git -C "$WORKDIR" push --force origin develop
  git -C "$WORKDIR" push --force origin main
  git -C "$WORKDIR" push --force --tags
  echo "==> Done. Re-clone all local checkouts; old clones retain the pre-scrub history."
fi
