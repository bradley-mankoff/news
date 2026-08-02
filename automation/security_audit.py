#!/usr/bin/env python3
"""Audit a git repository's working tree and full history for secrets and personal data.

Usage: security_audit.py [--report PATH] [--repo PATH] [--history-only]

Scans every tracked file in the working tree plus the full git history
(content of every commit, commit messages, and author/committer metadata)
against pattern lists for personal data and common secret formats. Emits a
Markdown report to stdout, or to --report PATH when given.

The report redacts every match ([@] / [.] / ***) so a checked-in report never
contains the raw personal data it documents. This script's own pattern
constants are assembled from parts for the same reason.

Exit codes:
  0 - no findings (clean)
  1 - findings exist
  2 - usage error

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
    ("slack-token", re.compile(r"xox[baprs]-")),
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
    r = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
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


def scan_working_tree(root: Path) -> list[Finding]:
    """Scan every tracked file (git ls-files) for personal data and secrets."""
    files = _git(root, ["ls-files", "-z"]).stdout.split("\0")
    findings: list[Finding] = []
    for rel in files:
        if not rel:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for category in _match_categories(line):
                findings.append(Finding(category, rel, lineno, line.strip()[:200]))
    return findings


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


def _parse_grep_line(line: str) -> tuple[str, str, int, str]:
    commit, _, rest = line.partition(":")
    path, _, rest = rest.partition(":")
    lineno_s, _, snippet = rest.partition(":")
    return commit, path, int(lineno_s), snippet


def _stats_from_grep(category: str, label: str, lines: list[str]) -> CategoryStats:
    commits_seen: list[str] = []
    for line in lines:
        commit, *_ = _parse_grep_line(line)
        if commit not in commits_seen:
            commits_seen.append(commit)
    return CategoryStats(
        category=category,
        label=label,
        commit_count=len(commits_seen),
        match_count=len(lines),
        example_commits=commits_seen[:3],
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

    message_hits: list[tuple[str, str, str]] = []
    msg_out = _git(root, ["log", "--all", "--format=%h|%s"]).stdout
    for line in msg_out.splitlines():
        commit, _, subject = line.partition("|")
        if PERSONAL_PATH_RE.search(subject):
            message_hits.append((commit, "personal-path", _redact_text(subject)))
        elif any(email in subject for email in PERSONAL_EMAILS):
            message_hits.append((commit, "personal-email", _redact_text(subject)))

    author_map: dict[str, dict[str, object]] = {}
    meta_out = _git(root, ["log", "--all", "--format=%an|%ae|%cn|%ce"]).stdout
    for line in meta_out.splitlines():
        an, ae, cn, ce = line.split("|")
        for email, name in ((ae, an), (ce, cn)):
            entry = author_map.setdefault(email, {"name": name, "count": 0})
            entry["count"] = int(entry["count"]) + 1
            entry["name"] = name
    authors = sorted(
        (
            (email, str(entry["name"]), int(entry["count"]), _is_personal_email(email))
            for email, entry in author_map.items()
        ),
        key=lambda item: -item[2],
    )

    env_out = _git(
        root, ["log", "--all", "--oneline", "--", "env.json"], check=False
    ).stdout
    env_json_commits = [
        line.split(maxsplit=1)[0] for line in env_out.splitlines() if line.strip()
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
    return PERSONAL_PATH_RE.sub(_redact_path, out)


# --- report ------------------------------------------------------------------

def _fmt_examples(commits: list[str]) -> str:
    return " ".join(f"`{c}`" for c in commits) if commits else "—"


def write_report(findings: list[Finding], history: HistoryReport) -> str:
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
    email_commits = max((s.commit_count for s in history_emails), default=0)
    path_commits = max((s.commit_count for s in history_paths), default=0)
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
            add(f"{f.path}:{f.line}: {f.category} — {_redact_text(f.snippet)}")
        add("```")
    add("")
    add("## History Findings")
    add("")
    if history_emails:
        add("### Personal emails in commit content")
        add("")
        add("| Pattern | Affected commits | Matches | Example commits |")
        add("|---|---|---|---|")
        for s in history_emails:
            add(f"| `{s.label}` | {s.commit_count}/{total} | {s.match_count} | {_fmt_examples(s.example_commits)} |")
        add("")
    if history_paths:
        add("### Personal filesystem paths in commit content")
        add("")
        add("| Pattern | Affected commits | Matches | Example commits |")
        add("|---|---|---|---|")
        for s in history_paths:
            add(f"| `{s.label}` | {s.commit_count}/{total} | {s.match_count} | {_fmt_examples(s.example_commits)} |")
        add("")
    if history_secrets:
        add("### Secret patterns in commit content")
        add("")
        add("| Pattern | Affected commits | Matches | Example commits |")
        add("|---|---|---|---|")
        for s in history_secrets:
            add(f"| `{s.label}` | {s.commit_count}/{total} | {s.match_count} | {_fmt_examples(s.example_commits)} |")
        add("")
    if not any((history_emails, history_paths, history_secrets)):
        add("No personal data or secret patterns found in any commit.")
        add("")
    add("## Commit Message Findings")
    add("")
    if not history.message_hits:
        add("No personal data in commit messages.")
    else:
        add("| Commit | Category | Subject |")
        add("|---|---|---|")
        for commit, category, subject in history.message_hits:
            add(f"| `{commit}` | {category} | {subject} |")
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
    add(f"- [{'x' if not history_secrets else ' '}] No secret patterns in any commit")
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

    root = Path(args.repo).resolve()
    findings: list[Finding] = []
    if not args.history_only:
        findings = scan_working_tree(root)
    history = scan_history(root)
    report = write_report(findings, history)

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
