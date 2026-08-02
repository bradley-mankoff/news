# History Scrub Runbook

Status: Gated (human decision required — force-push)

Date: 2026-08-02

## When to Use

The working tree is sanitized and the audit report (`docs/security/audit-2026-08-02.md`)
documents the remaining exposure: personal emails in the content of 115/117 commits,
personal filesystem paths in 113/117 commits and 7 commit messages, and 3 personal
author/committer emails. If the repository is to be made public, this runbook rewrites
that history with `git filter-repo` and requires a force-push of `develop` and `main`.

**This is destructive and gated**: a human must approve execution. Force-pushing
invalidates every existing clone, all `archon/task-issue-*` worktrees and open PR
diffs, and the poller's `automation/state.json` assumptions. Run only when no issues
are In Progress on the board.

## Prerequisites

- `git-filter-repo` installed: `brew install git-filter-repo` (NOT installed on this
  machine as of 2026-08-02 — verified).
- `git` >= 2.38.
- A clean mirror clone (see step 1) — never run `filter-repo` inside a live worktree.
- `automation/security_audit.py` available on the machine running the scrub (it is
  committed to the repo, so any non-bare clone has it).

## Steps

### 1. Fresh mirror clone

```bash
rm -rf /tmp/news-scrub
git clone --mirror https://github.com/bradley-mankoff/news /tmp/news-scrub
```

`git-filter-repo` operates entirely on the object database, so a mirror clone is
sufficient. (If the installed filter-repo version refuses a bare mirror, fall back to
`git clone https://github.com/bradley-mankoff/news /tmp/news-scrub` followed by
`git -C /tmp/news-scrub fetch origin '+refs/heads/*:refs/heads/*'`.)

### 2. Create the replacements file

`git filter-repo --replace-text` accepts literal `old==>new` lines. Create
`/tmp/replacements.txt` with the personal strings that the audit found (the
`[@]`, `[.]`, and `[USERNAME]` placeholders below must be restored to `@`, `.`,
and the actual username `home`):

```text
bradley[@]mankoff[.]com==>bradley@example.com
bradley[.]mankoff[@]gmail[.]com==>news@example.com
aidancoon97[@]gmail[.]com==>friend1@example.com
calzacortaandres[@]gmail[.]com==>friend2@example.com
isaacmessenger[@]yahoo[.]com==>friend3@example.com
/Users/[USERNAME]/personal_code/news==>news
/Users/[USERNAME]/news==>news
```

(The `==>` targets are safe: `example.com` is IANA-reserved and never delivers mail.)

### 3. Create the commit-message replacements file

```bash
cat > /tmp/messages.txt <<'EOF'
/Users/[USERNAME]/personal_code/news==>news
EOF
```

### 4. Create the mailmap

First discover the exact personal author/committer emails on the **unscrubbed**
mirror (run this before step 5 — SHAs and emails change once the history is
rewritten):

```bash
git -C /tmp/news-scrub log --all --format='%ae' | sort -u
```

Map the personal author/committer emails to the public noreply identity (restore
`[@]`/`[.]` placeholders; the middle line's `<hostname>` is the machine hostname of
the machine that created the early commits — the audit report redacts it
(`bradley_mankoff[@]***`), so take it from the `git log` output above):

```text
Bradley Mankoff <52721920+bradley-mankoff@users.noreply.github.com> bradley[@]mankoff[.]com
Bradley Mankoff <52721920+bradley-mankoff@users.noreply.github.com> bradley_mankoff[@]<hostname>[.]local
Bradley Mankoff <52721920+bradley-mankoff@users.noreply.github.com> brad[@]bauhealth[.]com
```

### 5. Run the rewrite

```bash
cd /tmp/news-scrub
git filter-repo \
  --replace-text /tmp/replacements.txt \
  --replace-message /tmp/messages.txt \
  --mailmap /tmp/mailmap.txt \
  --force
```

### 6. Verify

The mirror clone is bare (no working tree), so the scanner must run from a normal
checkout of this repo — the script lives there, and `--repo` targets the mirror:

```bash
# from any non-bare checkout of this repo (e.g. the local clone you used to plan the scrub)
python3 automation/security_audit.py --history-only --repo /tmp/news-scrub
echo $?   # must be 0 (no findings)
```

(Or use `automation/scrub_history.sh`, which automates steps 1–6 including this
verification and resolves the scanner from its own directory.)

### 7. Push

Force-push every branch and tag (the mirror clone has no remote, so add one):

```bash
cd /tmp/news-scrub
git remote add origin https://github.com/bradley-mankoff/news
git push --force origin develop
git push --force origin main
git push --force --tags
```

Then update every local checkout: re-clone (or fetch + `git reset --hard` on each
branch) and re-create `automation/state.json` (the poller tracks branch names and
issue state — see `automation/board_poller.py`).

### 8. Purge cached views

Old commits remain reachable by SHA-1 through GitHub's cached views, closed-PR diffs,
and forks until GitHub Support purges them. Open a support request ("sensitive data
removal") referencing the old commit SHAs (record them from the audit report's example
commits before rewriting — they are gone afterward).

## Side Effects and Caveats

- Every commit SHA changes; all open PRs and `archon/task-issue-*` branches must be
  recreated from the rewritten history.
- Any clone or fork made before the scrub retains the old data — treat them as
  compromised copies and delete them.
- The poller (`automation/board_poller.py`) reads `automation/state.json` and branch
  names; coordinate the scrub so no issue is In Progress and re-init state after push.
- If any real recipient list was ever committed (it was not — `env.json` was never
  tracked), that data would also need rotation, not just rewriting.

## Automated Wrapper

`automation/scrub_history.sh` automates steps 1-6 (clone, generate replacement files
from redacted constants, filter-repo, audit verification) and by default only
**prints** the push commands (`--dry-run`). A human passes `--execute` to push.
