# Secret-Prevention Runbook (Gitleaks pre-commit hook)

Status: Active (checked-in, owner-selected prevention control)

Date: 2026-08-06

## When to Use

Before going public, every future commit must be scanned for secrets. This
runbook covers the checked-in Gitleaks pre-commit hook: what it does, how to
install it, how to verify it, how to update it, and where its boundaries are.
It is the **prevention** control; it does not rewrite existing history and
does not replace the audit/scrub controls below.

## How It Works

`.pre-commit-config.yaml` pins the upstream `gitleaks/gitleaks` repository at
revision `v8.30.1` with only the `gitleaks` hook. The hook entry is the
upstream staged-only, redacted invocation and is intentionally not overridden:

```text
gitleaks git --pre-commit --redact --staged --verbose
```

- `--pre-commit` scans the staged diff (like `git diff`), so only changes a
  developer is about to commit are checked.
- `--staged` restricts the scan to staged content, so an unstaged secret in
  the working tree does not block unrelated commits.
- `--redact` replaces detected secrets with `REDACTED` in the hook output, so
  a rejected commit's terminal output never echoes the raw secret.
- `minimum_pre_commit_version: "4.6.1"` requires pre-commit 4.6.1 or newer.

## Prerequisites

- `uv` with the project's development group installed:
  `uv sync --group dev` (installs `pre-commit>=4.6.1` as a dev-only
  dependency; production dependencies are unchanged).
- `git` >= 2.38.
- Network access on first hook run: pre-commit downloads the pinned Gitleaks
  release into its hook cache (`PRE_COMMIT_HOME`, default
  `~/.cache/pre-commit`) once per revision.

## Steps

### 1. Install the hook

```bash
uv sync --group dev
uv run pre-commit install
```

This writes the hook into `.git/hooks/pre-commit` in the current checkout.
Every worktree and fresh clone needs its own `pre-commit install` — the hook
is not copied by `git clone`.

### 2. Verify the hook fires on staged secrets

Stage a file containing a **synthetic** secret (never a real one — assemble a
test token from parts, e.g. the deterministic access-key form used in
`tests/test_secret_prevention.py`) and attempt a normal commit:

```bash
git add notes.txt
git commit -m "test"
```

The commit must be rejected with a non-zero exit, `REDACTED` in the output,
and no commit created (`git log --oneline` unchanged). Delete the test file
afterwards.

### 3. Normal operation

No action is needed day-to-day. On every `git commit`, pre-commit runs the
pinned Gitleaks binary against the staged diff. A clean scan prints
`Detect hardcoded secrets...Passed` and the commit proceeds. A finding prints
a `Finding:`/`Secret: REDACTED` block and aborts the commit.

## Manual Commands

```bash
# Run the configured staged-only prevention hook against the current index.
uv run pre-commit run gitleaks

# --all-files does not override Gitleaks' explicit --staged mode.
uv run pre-commit run gitleaks --all-files

# Scan the working tree and history with the separate audit control.
uv run python automation/security_audit.py

# Validate the config file
uv run pre-commit validate-config

# Run every configured hook once
uv run pre-commit run --all-files
```

## Updating Gitleaks

Detector improvements arrive in new Gitleaks releases. To update:

1. Check the upstream release: https://github.com/gitleaks/gitleaks/releases
2. Bump `rev:` in `.pre-commit-config.yaml` to the new tag.
3. Run `uv run pre-commit validate-config` and exercise the staged-only hook
   with an appropriate staged synthetic fixture using
   `uv run pre-commit run gitleaks`. Use
   `uv run python automation/security_audit.py` for working-tree/history
   coverage.
4. Update the pinned revision reference in `tests/test_secret_prevention.py`
   and re-run the test suite.
5. Commit the bump as its own change so the release notes stay visible.

Do not bump revisions in the same commit as unrelated changes; a pin change
is a deliberate detector change.

## Bypass Caveats

- `git commit --no-verify` and `SKIP=gitleaks` bypass the hook entirely.
  Nothing prevents a determined developer from using them; the hook is a
  guardrail, not a wall. Do not use either in this repository.
- The hook scans only the **staged diff**. Secrets already in history, in
  unstaged working-tree files, or introduced in files that skip the diff
  (e.g. binary blobs) are not caught here — see the boundaries below.
- The hook does not scan commit messages.
- A malicious or compromised machine can always disable local hooks; treat
  the hook as prevention, and keep the audit as verification.

## Control Boundaries (what this hook does NOT do)

- **History is not rewritten.** Existing historical personal-data findings
  (documented in `docs/security/audit-2026-08-02.md`) are a separate concern.
  The audit scanner (`automation/security_audit.py`) reports them and exits 1;
  the history scrub (`docs/security/history-scrub.md`, gated runbook +
  `automation/scrub_history.sh`) rewrites history with `git filter-repo` and
  requires explicit human approval. The hook intentionally does not scan
  history, so ordinary commits are not blocked by old findings.
- **GitHub-side protection is not configured here.** Enabling push protection
  for the repository and any secret-scanning alerts is an owner-level GitHub
  setting to decide after the public flip; it is deliberately separate from
  this checked-in hook. See GitHub's push-protection documentation when the
  time comes.
- **No new scanner taxonomy.** Gitleaks is the prevention detector;
  `automation/security_audit.py` remains the audit control with its own
  categories and redaction.

## Relationship to the Audit/Scrub Controls

| Control | What it scans | Runs when | Owner |
|---------|---------------|-----------|-------|
| Gitleaks pre-commit hook (this runbook) | Staged diff of future commits | Every `git commit` | Checked in, `.pre-commit-config.yaml` |
| `automation/security_audit.py` | Working tree + full history | Manual / CI | Checked in, exits 0 when clean |
| `automation/scrub_history.sh` | Entire history (rewrite) | Gated, human approval | Checked in, dry-run by default |

## Troubleshooting

- **`pre-commit: command not found`**: run `uv sync --group dev` and install
  the hook again with `uv run pre-commit install`.
- **Hook not running on commit**: confirm `.git/hooks/pre-commit` exists
  (re-run `uv run pre-commit install`), and that the config
  `minimum_pre_commit_version` is satisfied by the installed pre-commit
  (`uv run pre-commit --version`).
- **First run is slow**: pre-commit downloads the pinned Gitleaks release
  once; later runs reuse the cache. Set `PRE_COMMIT_HOME` to relocate it.
- **False positive**: never silence a finding by deleting the hook or using
  `--no-verify`. Investigate the finding first; if it is genuinely not a
  secret, fix the file content or, only where justified, discuss an
  allowlist change in the issue.
