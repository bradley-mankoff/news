# Secret-Prevention Runbook (Gitleaks pre-commit hook)

Status: Active (checked-in, owner-selected prevention control)

Date: 2026-08-06

## When to Use

Before going public, every future commit must be scanned for secrets. This
runbook covers the two checked-in Gitleaks prevention controls: the local
pre-commit hook (what it does, how to install it, how to verify it, how to
update it, and where its boundaries are) and the CI pull-request gate. They
are the **prevention** controls; neither rewrites existing history and both
are separate from the audit/scrub controls below.

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

## CI Gate (pull-request secret scan)

`.github/workflows/ci.yml` runs a separate `Secret scan` job on every pull
request targeting `develop` or `main`. It is the remote merge-safety path: a
contributor whose local hook is bypassed (`--no-verify`), skipped
(`SKIP=gitleaks`), or never installed is still blocked from merging a new
secret into the base branches.

- The job checks out the PR head with full history (`fetch-depth: 0`) and
  `persist-credentials: false`, so the `BASE_SHA..HEAD_SHA` range is
  resolvable and no `GITHUB_TOKEN` is left in the checkout's git config.
- The job grants only `contents: read`. No repository secret, write
  permission, PR comment, or SARIF upload is used; the container receives no
  `GITHUB_TOKEN` and no `GITLEAKS_LICENSE`. The workspace is mounted
  read-only (`/repo:ro`).
- The scanner is the official `ghcr.io/gitleaks/gitleaks:v8.30.1` container,
  the same detector release the local hook is pinned to. It runs
  `git --redact --no-banner --no-color --verbose --exit-code 1` with
  `--log-opts="--no-merges ${BASE_SHA}..${HEAD_SHA}"`.
- Scope is the PR's non-merge commit range only. CI does not scan all
  repository history: known historical findings (documented in
  `docs/security/audit-2026-08-02.md`) lie outside any new PR's range and
  stay the responsibility of the audit/scrub controls, not this gate.
- Findings and scanner/image failures both exit nonzero and fail the check;
  the step does not mask the scanner's status (`continue-on-error` and
  `|| true` are not used). `--redact` replaces detected values with
  `REDACTED` in the job logs.

### Reproduce the gate locally (Docker required)

```bash
BASE_SHA="$(git merge-base origin/develop HEAD)"
HEAD_SHA="$(git rev-parse HEAD)"
test -n "$BASE_SHA" && test -n "$HEAD_SHA"
docker run --rm \
  --volume "$PWD:/repo:ro" \
  ghcr.io/gitleaks/gitleaks:v8.30.1 \
  git --redact --no-banner --no-color --verbose --exit-code 1 \
  --log-opts="--no-merges ${BASE_SHA}..${HEAD_SHA}" \
  /repo
```

A clean range exits 0; a finding exits 1 with the value redacted; an image
or scanner error also exits nonzero. Only ever test against a synthetic
fixture assembled from parts — never a real credential.

## Updating Gitleaks

Detector improvements arrive in new Gitleaks releases. To update:

1. Check the upstream release: https://github.com/gitleaks/gitleaks/releases
2. Bump `rev:` in `.pre-commit-config.yaml` to the new tag.
3. Run `uv run pre-commit validate-config` and exercise the staged-only hook
   with an appropriate staged synthetic fixture using
   `uv run pre-commit run gitleaks`. Use
   `uv run python automation/security_audit.py` for working-tree/history
   coverage.
4. Update the pinned revision everywhere it appears in the same release:
   `rev:` in `.pre-commit-config.yaml`, the image tag in the `Secret scan`
   job in `.github/workflows/ci.yml`, the constants in
   `tests/test_secret_prevention.py` and `tests/test_ci_secret_scanning.py`,
   and the version references in this runbook and `README.md`. Re-run the
   test suite.
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
- **Native GitHub secret scanning is not configured here.** Enabling
  GitHub's own secret scanning / push protection and any alert triage is an
  owner-level GitHub setting to decide after the public flip; it is
  deliberately separate from the checked-in hook and the CI gate above. See
  GitHub's push-protection documentation when the time comes.
- **No new scanner taxonomy.** Gitleaks is the prevention detector;
  `automation/security_audit.py` remains the audit control with its own
  categories and redaction.

## Relationship to the Audit/Scrub Controls

| Control | What it scans | Runs when | Owner |
|---------|---------------|-----------|-------|
| Gitleaks pre-commit hook (this runbook) | Staged diff of future commits | Every `git commit` | Checked in, `.pre-commit-config.yaml` |
| Gitleaks CI gate | PR non-merge commit range (`BASE_SHA..HEAD_SHA`) | Every pull request to `develop`/`main` | Checked in, `.github/workflows/ci.yml`, pinned `v8.30.1` |
| `automation/security_audit.py` | Working tree + full history | Manual | Checked in, exits 0 when clean |
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
