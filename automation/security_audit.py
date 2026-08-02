#!/usr/bin/env python3
"""Audit a git repository's working tree and full history for secrets and personal data.

Usage: security_audit.py [--report PATH] [--repo PATH] [--history-only]

Scans every tracked file in the working tree plus the full git history
(content of every commit, commit messages, and author/committer metadata)
against pattern lists for personal data and common secret formats. Emits a
Markdown report to stdout, or to --report PATH when given.

The report redacts every match (emails, paths, and secret values; shown as
[@] / [.] / ***) so a checked-in report never contains the raw sensitive data
it documents. This script's own pattern constants are assembled from parts
for the same reason.

Exit codes:
  0 - no findings (clean)
  1 - findings exist
  2 - usage error or scan failure (e.g. --repo is not a git repository)

Example: python3 automation/security_audit.py --report docs/security/audit-2026-08-02.md
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Personal addresses are assembled from parts so this file never contains
# the raw strings it detects.
PERSONAL_EMAILS = [
    "bradley@" + "mankoff.com",
    "bradley." + "mankoff" + "@gmail.com",
    "aidancoon97" + "@gmail.com",
    "calzacortaandres" + "@gmail.com",
    "isaacmessenger" + "@yahoo.com",
    "brad@bau" + "health.com",
    "bradley_mankoff" + "@",
]

PERSONAL_PATH_RE = re.compile(r"/Users/[A-Za-z_]+/|/home/[A-Za-z_]+/")

SECRET_PATTERNS = [
    ("openai-api-key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    # 10+ digits avoids false positives from short mock/placeholder tokens
    # (tightened in #49 so the scanner's self-scan stays clean).
    ("slack-token", re.compile(r"xox[baprs]-[0-9]{10,}")),
    ("private-key-header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Plain alternation only: this pattern is also passed to `git grep`, which uses POSIX ERE (no `(?:...)` groups).
SECRET_COMBINED_RE = re.compile("|".join(rx.pattern for _, rx in SECRET_PATTERNS))

LOCAL_HOST_EMAIL_RE = re.compile(r"@[A-Za-z0-9.-]+\.local")


@dataclass
class Finding:
    category: str
    path: str
    line: int
    snippet: str


@dataclass
class CategoryStats:
    category: str
    label: str
    commit_count: int
    match_count: int
    example_commits: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)  # full deduped commit list (for union math)


@dataclass
class AuthorStat:
    email: str
    name: str
    count: int = 0


@dataclass
class HistoryReport:
    total_commits: int
    content_stats: list[CategoryStats] = field(default_factory=list)
    message_hits: list[tuple[str, str, str]] = field(default_factory=list)
    authors: list[tuple[str, str, int, bool]] = field(default_factory=list)
    env_json_commits: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(
            self.content_stats
            or self.message_hits
            or any(personal for _, _, _, personal in self.authors)
            or self.env_json_commits
        )


def _git(root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    # timeout matches the sibling automation/move_item.py convention so a hung
    # git (e.g. network filesystem) cannot stall CI or the scrub gate forever.
    r = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}"
        )
    return r


# --- working-tree scan -------------------------------------------------------

def _match_categories(text: str) -> list[str]:
    cats: list[str] = []
    if any(email in text for email in PERSONAL_EMAILS):
        cats.append("personal-email")
    if PERSONAL_PATH_RE.search(text):
        cats.append("personal-path")
    for name, rx in SECRET_PATTERNS:
        if rx.search(text):
            cats.append(f"secret:{name}")
    return cats


def scan_working_tree(root: Path) -> tuple[list[Finding], int]:
    """Scan every tracked file (git ls-files) for personal data and secrets.

    Returns (findings, skipped_count). Tracked files that cannot be read
    (deleted on disk, unreadable, broken symlink, submodule gitlink) are
    reported to stderr and counted so a "clean" report never silently claims
    coverage it did not have. Raises RuntimeError on a bare repository: `git
    ls-files` would silently return an empty list there, so the caller must
    pass --history-only.
    """
    bare = _git(root, ["rev-parse", "--is-bare-repository"]).stdout.strip() == "true"
    if bare:
        raise RuntimeError(
            "repository is bare (no working tree) — pass --history-only to skip the tree scan"
        )
    files = _git(root, ["ls-files", "-z"]).stdout.split("\0")
    findings: list[Finding] = []
    skipped: list[str] = []
    for rel in files:
        if not rel:
            continue
        try:
            # Truncation keeps Finding.snippet bounded for the report.
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append(rel)
            print(f"warning: cannot read tracked file {rel}: {exc}", file=sys.stderr)
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for category in _match_categories(line):
                # Keep the full line: redaction (which masks whole lines) must
                # run before any truncation so a token straddling the cutoff
                # can never survive in the report.
                findings.append(Finding(category, rel, lineno, line.strip()))
    if skipped:
        print(
            f"warning: {len(skipped)} tracked file(s) skipped (see above): "
            + ", ".join(skipped),
            file=sys.stderr,
        )
    return findings, len(skipped)


# --- history scan ------------------------------------------------------------

def _all_commits(root: Path) -> list[str]:
    out = _git(root, ["rev-list", "--all"]).stdout
    return [c for c in out.splitlines() if c]


def _grep_commits(root: Path, commits: list[str], pattern: str, fixed: bool) -> list[str]:
    if not commits:
        return []
    args = ["grep", "-I", "-n", "-F" if fixed else "-E", pattern, *commits]
    r = _git(root, args, check=False)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {r.stderr.strip()[:300]}")
    return [line for line in r.stdout.splitlines() if line]


def _parse_grep_line(line: str) -> tuple[str, str, int, str] | None:
    """Parse `commit:path:lineno:snippet`; return None for malformed lines.

    Filenames may legally contain `:` on POSIX, which misaligns the fields;
    skipping beats aborting the whole audit on one unusual path.
    """
    commit, _, rest = line.partition(":")
    path, _, rest = rest.partition(":")
    lineno_s, _, snippet = rest.partition(":")
    try:
        return commit, path, int(lineno_s), snippet
    except ValueError:
        return None


def _stats_from_grep(category: str, label: str, lines: list[str]) -> CategoryStats:
    commits_seen: list[str] = []
    for line in lines:
        parsed = _parse_grep_line(line)
        if parsed is None:
            continue
        commit, *_ = parsed
        if commit not in commits_seen:
            commits_seen.append(commit)
    return CategoryStats(
        category=category,
        label=label,
        commit_count=len(commits_seen),
        match_count=len(lines),
        # Keep the first 3 for the report's example-commit column; the full
        # list is retained for exact union math in the summary rows.
        example_commits=commits_seen[:3],
        commits=commits_seen,
    )


def _is_personal_email(email: str) -> bool:
    return email in PERSONAL_EMAILS or email.startswith("bradley_mankoff" + "@")


def scan_history(root: Path) -> HistoryReport:
    """Scan all commits' content, messages, and author metadata."""
    commits = _all_commits(root)

    content_stats: list[CategoryStats] = []
    for email in PERSONAL_EMAILS:
        lines = _grep_commits(root, commits, email, fixed=True)
        if lines:
            content_stats.append(
                _stats_from_grep("personal-email", _redact_email(email), lines)
            )
    path_lines = _grep_commits(root, commits, PERSONAL_PATH_RE.pattern, fixed=False)
    if path_lines:
        content_stats.append(
            _stats_from_grep("personal-path", "personal filesystem paths", path_lines)
        )
    secret_lines = _grep_commits(root, commits, SECRET_COMBINED_RE.pattern, fixed=False)
    if secret_lines:
        content_stats.append(
            _stats_from_grep("secret", "secret patterns", secret_lines)
        )

    # Full messages (subject + body), not just subjects: secrets are commonly
    # pasted into message bodies, and `git grep` only scans tree contents.
    # The trailing %x1f makes records self-delimiting: git appends a newline
    # after %B, which would otherwise merge the next commit's hash into the
    # previous body.
    message_hits: list[tuple[str, str, str]] = []
    msg_out = _git(root, ["log", "--all", "--format=%h%x1f%B%x1f"]).stdout
    blocks = msg_out.split("\x1f")
    for i in range(0, len(blocks) - 1, 2):
        commit = blocks[i].strip()
        body = blocks[i + 1]
        if not commit:
            continue
        # Redact the FULL body before truncating so a cut-off secret can never
        # survive truncation; collapse newlines for the report table cell.
        detail = " ".join(_redact_text(body).split())[:200]
        if PERSONAL_PATH_RE.search(body):
            message_hits.append((commit, "personal-path", detail))
        elif any(email in body for email in PERSONAL_EMAILS):
            message_hits.append((commit, "personal-email", detail))
        elif SECRET_COMBINED_RE.search(body):
            message_hits.append((commit, "secret", detail))

    # \x1f (unit separator) is robust against `|` and newlines in git names;
    # `|` is legal in author/committer names and would break a split on it.
    author_map: dict[str, AuthorStat] = {}
    meta_out = _git(root, ["log", "--all", "--format=%an%x1f%ae%x1f%cn%x1f%ce"]).stdout
    for line in meta_out.splitlines():
        an, ae, cn, ce = line.split("\x1f")
        for email, name in ((ae, an), (ce, cn)):
            entry = author_map.setdefault(email, AuthorStat(email=email, name=name))
            entry.count += 1
            entry.name = name
    authors = sorted(
        (
            (email, stat.name, stat.count, _is_personal_email(email))
            for email, stat in author_map.items()
        ),
        key=lambda item: -item[2],
    )

    env_out = _git(
        root, ["log", "--all", "--oneline", "--", "env.json"], check=False
    )
    # Same 0/1 allowlist as _grep_commits: any other exit means the check never
    # ran, and the Clean Checks section must not claim it did.
    if env_out.returncode not in (0, 1):
        raise RuntimeError(f"git log env.json failed: {env_out.stderr.strip()[:300]}")
    env_json_commits = [
        line.split(maxsplit=1)[0] for line in env_out.stdout.splitlines() if line.strip()
    ]

    return HistoryReport(
        total_commits=len(commits),
        content_stats=content_stats,
        message_hits=message_hits,
        authors=authors,
        env_json_commits=env_json_commits,
    )


# --- redaction ---------------------------------------------------------------

def _redact_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return f"{local}[@]***"
    return f"{local}[@]{domain.replace('.', '[.]')}"


def _redact_author_email(email: str) -> str:
    # Stricter than _redact_email on purpose: in the Author Metadata table a
    # `.local` hostname is machine identity (personal data), so mask the whole
    # domain instead of showing it with [.] separators.
    local, _, domain = email.partition("@")
    if not domain or ".local" in domain:
        return f"{local}[@]***"
    return f"{local}[@]{domain.replace('.', '[.]')}"


def _redact_path(match: re.Match) -> str:
    prefix = "/Users/" if match.group(0).startswith("/Users/") else "/home/"
    return prefix + "***/"


def _redact_text(text: str) -> str:
    out = LOCAL_HOST_EMAIL_RE.sub("[@]***", text)
    for email in PERSONAL_EMAILS:
        if email in out:
            out = out.replace(email, _redact_email(email))
    out = PERSONAL_PATH_RE.sub(_redact_path, out)
    # Secrets last: mask the WHOLE line so partial-token fragments (e.g. the
    # tail of a slack token after its numeric prefix) cannot leak either, and
    # the redacted form cannot itself match a secret pattern.
    return "\n".join(
        "[REDACTED]" if SECRET_COMBINED_RE.search(line) else line
        for line in out.splitlines()
    )


# --- report ------------------------------------------------------------------

def _fmt_examples(commits: list[str]) -> str:
    return " ".join(f"`{c}`" for c in commits) if commits else "—"


def _union_commit_count(stats: list[CategoryStats]) -> int:
    """Number of distinct commits touched by any pattern in the category.

    The summary rows read as per-category totals; max() over per-pattern
    commit counts would understate exposure when patterns hit disjoint sets.
    """
    seen: set[str] = set()
    for s in stats:
        seen.update(s.commits)
    return len(seen)


def write_report(
    findings: list[Finding], history: HistoryReport, skipped_files: int = 0
) -> str:
    """Render the Markdown report.

    Every snippet is passed through _redact_text (emails, paths, and secret
    values) so the report can be checked in without leaking the data it
    documents. History statistics carry redacted labels only — no raw
    snippets.
    """
    lines: list[str] = []
    add = lines.append
    add("# Security Audit Report")
    add("")
    add("Status: Complete")
    add("")
    add(f"Date: {date.today().isoformat()}")
    add("")
    add("## Summary")
    add("")
    add("| Area | Result |")
    add("|---|---|")
    tree_secrets = [f for f in findings if f.category.startswith("secret:")]
    tree_personal = [f for f in findings if f.category in ("personal-email", "personal-path")]
    history_secrets = [s for s in history.content_stats if s.category == "secret"]
    history_emails = [s for s in history.content_stats if s.category == "personal-email"]
    history_paths = [s for s in history.content_stats if s.category == "personal-path"]
    total = history.total_commits or 0
    add(f"| Working tree — secrets | {'NONE' if not tree_secrets else f'{len(tree_secrets)} finding(s)'} |")
    add(f"| Working tree — personal data | {'NONE' if not tree_personal else f'{len(tree_personal)} finding(s)'} |")
    add(f"| History — secrets | {'NONE' if not history_secrets else 'findings (see below)'} |")
    email_commits = _union_commit_count(history_emails)
    path_commits = _union_commit_count(history_paths)
    add(f"| History — personal emails in content | {email_commits}/{total} commits |")
    add(f"| History — personal paths in content | {path_commits}/{total} commits |")
    add(f"| History — personal data in commit messages | {len(history.message_hits)}/{total} commits |")
    personal_authors = sum(1 for _, _, _, personal in history.authors if personal)
    add(f"| Author identities with personal emails | {personal_authors} of {len(history.authors)} |")
    add("")
    add("## Working Tree Findings")
    add("")
    if not findings:
        add("No findings — every tracked file is clean.")
    else:
        add("```")
        for f in sorted(findings, key=lambda f: (f.path, f.line)):
            add(f"{f.path}:{f.line}: {f.category} — {_redact_text(f.snippet)[:200]}")
        add("```")
    if skipped_files:
        add("")
        add(f"Note: {skipped_files} tracked file(s) could not be read and were skipped")
        add("(see stderr warnings) — the tree scan above did not cover them.")
    add("")
    add("## History Findings")
    add("")
    def add_pattern_section(title: str, stats: list[CategoryStats]) -> None:
        if not stats:
            return
        add(f"### {title}")
        add("")
        add("| Pattern | Affected commits | Matches | Example commits |")
        add("|---|---|---|---|")
        for s in stats:
            add(f"| `{s.label}` | {s.commit_count}/{total} | {s.match_count} | {_fmt_examples(s.example_commits)} |")
        add("")

    add_pattern_section("Personal emails in commit content", history_emails)
    add_pattern_section("Personal filesystem paths in commit content", history_paths)
    add_pattern_section("Secret patterns in commit content", history_secrets)
    if not any((history_emails, history_paths, history_secrets)):
        add("No personal data or secret patterns found in any commit.")
        add("")
    add("## Commit Message Findings")
    add("")
    if not history.message_hits:
        add("No personal data or secrets in commit messages.")
    else:
        add("| Commit | Category | Detail |")
        add("|---|---|---|")
        for commit, category, detail in history.message_hits:
            add(f"| `{commit}` | {category} | {detail} |")
    add("")
    add("## Author Metadata")
    add("")
    if not history.authors:
        add("No commits found.")
    else:
        add("| Email | Name | Commits | Status |")
        add("|---|---|---|---|")
        for email, name, count, personal in history.authors:
            shown = _redact_author_email(email) if personal else email
            add(f"| `{shown}` | {name} | {count} | {'personal' if personal else 'safe'} |")
    add("")
    add("## Clean Checks")
    add("")
    add(f"- [{'x' if not history.env_json_commits else ' '}] `env.json` was never committed")
    message_secrets = [h for h in history.message_hits if h[1] == "secret"]
    add(f"- [{'x' if not history_secrets and not message_secrets else ' '}] No secret patterns in any commit")
    add(f"- [{'x' if not tree_personal else ' '}] Working tree contains no personal emails or paths")
    add(f"- [{'x' if not tree_secrets else ' '}] Working tree contains no secret patterns")
    add("")
    add("## Recommendations")
    add("")
    add("1. Run this scanner in CI on every PR (`python3 automation/security_audit.py`); it is stdlib-only and exits 0 when clean.")
    add("2. Before making the repository public, execute the history scrub described in `docs/security/history-scrub.md` (gated: requires a human decision because it force-pushes `develop` and `main`).")
    add("3. After any force-push, contact GitHub Support to purge cached views and closed-PR diffs of the old commits.")
    add("4. Add a `.mailmap` after the scrub so future author identities stay canonical.")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="security_audit.py",
        description="Audit a git repository's working tree and full history for secrets and personal data.",
    )
    parser.add_argument("--report", metavar="PATH", help="write the report to PATH instead of stdout")
    parser.add_argument("--repo", metavar="PATH", default=str(ROOT), help="repository to audit (default: this repo)")
    parser.add_argument("--history-only", action="store_true", help="skip the working-tree scan (e.g. on a bare mirror clone)")
    args = parser.parse_args(argv)

    try:
        root = Path(args.repo).resolve()
        if args.history_only:
            findings, skipped = [], 0
        else:
            findings, skipped = scan_working_tree(root)
        history = scan_history(root)
        report = write_report(findings, history, skipped_files=skipped)
    except Exception as exc:  # RuntimeError from _git, OSError, timeout, ...
        # Never traceback: a crash must not be indistinguishable from
        # "findings exist" (exit 1) for CI or the scrub verify gate.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        print(f"report written to {out}")
    else:
        print(report)
    return 1 if (findings or history.has_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
