"""Pure board policy: dependencies, routing, readiness, and verdicts."""

from __future__ import annotations

import re
from .model import ACTIVE_WORKFLOW_STATUSES

VERDICT_VALUES = ("approve", "request-changes", "block")

READY_TEST_HEADINGS = (
    "How to test",
    "Human testing",
    "Test instructions",
    "Test plan",
)

VALIDATION_HEADINGS = ("Validation", "Verification")

NONE_GUIDANCE_RE = re.compile(r"^[*_\s]*(?:none|n/?a|not applicable)\.?[*_\s]*$", re.I)

RUNNABLE_GUIDANCE_RE = re.compile(
    r"```|^[ \t]*(?:\$[ \t]*)?"
    r"(?:python3?|pytest|uv|news|npm|pnpm|yarn|curl|make|git|gh)\b",
    re.M,
)

SHELL_FENCE_LANGUAGES = frozenset(
    {"", "bash", "sh", "shell", "zsh", "console", "terminal"}
)

SHELL_COMMAND_RE = re.compile(
    r"^[ \t]*(?:\$[ \t]*)?(?:[\w.-]+/)*"
    r"(?:python3?|pytest|uv|news|npm|pnpm|yarn|curl|make|git|gh)\b"
)

SHELL_INLINE_COMMENT_RE = re.compile(r"[ \t]+#[ \t].*$")

def parse_dep_refs(body: str) -> list[int]:
    """Issue refs from `Depends on: #N` lines in an issue body.

    Accepts `Depends on: #42, #57`, `- Depends on: #42`, `**Depends on:** #42`,
    and the GitHub issue-form rendering `### Depends on` with the refs on the
    following line.
    """
    refs: set[int] = set()
    pending = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        hm = re.match(r"^#{1,6}\s*depends\s+on\s*:?\s*(.*)$", line, re.I)
        dm = re.match(r"^\s*(?:[-*]\s*|\*+\s*)?depends\s+on\s*:?\s*(.*)$", line, re.I)
        if hm:
            pending = True
            line = hm.group(1).strip()
        elif dm:
            pending = False
            line = dm.group(1).strip()
        elif pending and line and not line.startswith("##"):
            pending = False
        else:
            continue
        if line:
            refs.update(int(x) for x in re.findall(r"#(\d+)", line))
    return sorted(refs)

def dep_gate(deps: list[int], dep_lanes: dict[int, str], dep_states: dict[int, str],
             done_lane: str) -> tuple[list[int], list[int]]:
    """(unsatisfied, cancelled) for a dep list.

    A dep is satisfied only when its issue is on the board in the done lane.
    cancelled deps are closed without being done (subset of unsatisfied);
    off-board deps are unsatisfied but never "cancelled" (we cannot tell a
    shipped-and-removed issue from an abandoned one).
    """
    unsatisfied: list[int] = []
    cancelled: list[int] = []
    for n in deps:
        lane = dep_lanes.get(n)
        if lane != done_lane:
            unsatisfied.append(n)
            if lane is not None and dep_states.get(n) == "CLOSED":
                cancelled.append(n)
    return unsatisfied, cancelled

def fmt_deps(nums: list[int]) -> str:
    return ", ".join(f"#{n}" for n in nums)

def issue_is_runnable(content: dict, status: str,
                      number_lane: dict[int, str],
                      number_state: dict[int, str],
                      todo_lane: str | None,
                      done_lane: str | None) -> bool:
    """Whether an open issue is ready to dispatch from the Todo lane."""
    if (
        content.get("__typename") != "Issue"
        or content.get("state") != "OPEN"
        or not todo_lane
        or status != todo_lane
        or not done_lane
    ):
        return False
    unsatisfied, _ = dep_gate(
        parse_dep_refs(content.get("body") or ""),
        number_lane,
        number_state,
        done_lane,
    )
    return not unsatisfied

def parse_verdict(bodies: list[str]) -> str | None:
    """Last `VERDICT: <approve|request-changes|block>` marker across bodies.

    The marker may sit anywhere in a line (markdown-wrapped or prose-prefixed)
    and is case-insensitive. Later comments win (a re-review supersedes the
    earlier verdict); a malformed or absent marker yields None (never merge
    on None).
    """
    verdict = None
    for body in bodies or []:
        for line in (body or "").splitlines():
            m = re.search(r"(?:^|[^\w])VERDICT:\s*([a-z-]+)", line, re.I)
            if m and m.group(1).lower() in VERDICT_VALUES:
                verdict = m.group(1).lower()
    return verdict

def markdown_section(body: str, heading: str) -> str | None:
    """Return the body of a Markdown section, including nested subsections."""
    lines = (body or "").splitlines()
    start = None
    level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{2,6})[ \t]+(.+?)[ \t]*$", line)
        if (match
                and match.group(2).strip().casefold() == heading.casefold()):
            start = index
            level = len(match.group(1))
            break
    if start is None or level is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{2,6})[ \t]+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    section = "\n".join(lines[start + 1:end]).strip()
    return section or None

def extract_test_guidance(comments: list[dict]) -> str | None:
    """Find human-test guidance in issue comments.

    New completion records use ``How to test``. Older records may only have a
    ``Validation`` section. For those legacy sections, prefer a recorded
    command over a newer summary that only reports pass/fail totals.
    """
    for comment in reversed(comments or []):
        body = comment.get("body") or ""
        for heading in READY_TEST_HEADINGS:
            section = markdown_section(body, heading)
            if section and not NONE_GUIDANCE_RE.fullmatch(section):
                return section

    validation_fallback = None
    for comment in reversed(comments or []):
        body = comment.get("body") or ""
        for heading in VALIDATION_HEADINGS:
            section = markdown_section(body, heading)
            if not section or NONE_GUIDANCE_RE.fullmatch(section):
                continue
            validation_fallback = validation_fallback or section
            if RUNNABLE_GUIDANCE_RE.search(section):
                return section
    return validation_fallback

def sanitize_test_guidance(guidance: str | None) -> str | None:
    """Make copied shell commands safe to paste in shells with comments off."""
    if not guidance:
        return None
    lines = []
    in_shell_fence = False
    for line in guidance.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_shell_fence:
                in_shell_fence = False
            else:
                parts = stripped[3:].strip().split(maxsplit=1)
                language = parts[0].casefold() if parts else ""
                in_shell_fence = language in SHELL_FENCE_LANGUAGES
            lines.append(line)
            continue
        if in_shell_fence or SHELL_COMMAND_RE.match(line):
            line = SHELL_INLINE_COMMENT_RE.sub("", line).rstrip()
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned or None

MACHINE_CHECK_HEADINGS = (
    "Machine checks",
    "Automated checks",
    "Automation checks",
)

HUMAN_CHECK_HEADINGS = (
    "Human checks",
    "Manual checks",
    "Manual testing",
)

EVIDENCE_MARKER_RE = re.compile(
    r"\b(?:pass(?:ed|ing)?|fail(?:ed|ing)?|error|clean|"
    r"exit\w*|success|verified|contain\w*|expected|"
    r"\d+\s+tests?|http\w?://)\b",
    re.I,
)

def _paragraphs(text: str) -> list[str]:
    """Split prose into paragraphs, keeping fenced code blocks intact."""
    paragraphs: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not stripped and not in_fence:
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        paragraphs.append("\n".join(buf))
    return paragraphs

def _paragraph_has_command(paragraph: str) -> bool:
    in_fence = False
    for line in paragraph.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or SHELL_COMMAND_RE.match(line):
            return True
    return False

def _section_by_prefix(text: str | None, heading: str) -> str | None:
    """Body of the first `##+ <heading>...` subsection, fences respected."""
    lines = (text or "").splitlines()
    start = None
    level = None
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,6})[ \t]+(.+?)[ \t]*$", line)
        if (match and match.group(2).strip().casefold().startswith(
                heading.casefold())):
            start = index
            level = len(match.group(1))
            break
    if start is None or level is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("```"):
            continue
        match = re.match(r"^(#{2,6})[ \t]+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    chunk = "\n".join(lines[start + 1:end]).strip()
    return chunk or None

def _remove_subsections(text: str | None, headings: tuple[str, ...]) -> str:
    """Drop heading-prefixed subsections (heading line + body) from text."""
    lines = (text or "").splitlines()
    drop: set[int] = set()
    for heading in headings:
        start = None
        level = None
        in_fence = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            match = re.match(r"^(#{2,6})[ \t]+(.+?)[ \t]*$", line)
            if (match and match.group(2).strip().casefold().startswith(
                    heading.casefold())):
                start = index
                level = len(match.group(1))
                break
        if start is None or level is None:
            continue
        end = len(lines)
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("```"):
                continue
            match = re.match(r"^(#{2,6})[ \t]+", lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        drop.update(range(start, end))
    kept = [line for index, line in enumerate(lines) if index not in drop]
    return "\n".join(kept).strip()

def _fallback_partition(guidance: str | None) -> tuple[str | None, str | None]:
    """Syntactic machine/human partition for unlabeled guidance."""
    if not guidance:
        return None, None
    paragraphs = _paragraphs(guidance)
    command_flags = [_paragraph_has_command(p) for p in paragraphs]
    machine_flags = list(command_flags)
    for index, paragraph in enumerate(paragraphs):
        if command_flags[index]:
            continue
        precedes_command = (
            index + 1 < len(paragraphs) and command_flags[index + 1])
        follows_command = index > 0 and command_flags[index - 1]
        if (precedes_command
                or (follows_command and EVIDENCE_MARKER_RE.search(paragraph))):
            machine_flags[index] = True
    machine = "\n\n".join(
        p for p, f in zip(paragraphs, machine_flags) if f).strip() or None
    human = "\n\n".join(
        p for p, f in zip(paragraphs, machine_flags) if not f).strip() or None
    return machine, human

def split_test_guidance(guidance: str | None) -> tuple[str | None, str | None]:
    """Partition ``How to test`` guidance into (machine_checks, human_steps).

    Machine checks are everything the machine can run on its own: shell
    commands with their expected output / recorded evidence. Human steps are
    the remainder — the prose that needs a person (product review, taste,
    decisions).

    Explicit subsections win when present: ``### Machine checks`` /
    ``### Automated checks`` (or synonyms) and ``### Human checks`` /
    ``### Manual checks``. Otherwise the section is partitioned
    syntactically: paragraphs containing shell commands, prose that leads
    into a command paragraph, and prose immediately after a command that
    records its result (counts, pass/fail, URLs to verify) are machine
    checks; everything else is a human step.
    """
    if not guidance:
        return None, None
    machine = None
    for heading in MACHINE_CHECK_HEADINGS:
        machine = _section_by_prefix(guidance, heading)
        if machine is not None:
            break
    human = None
    for heading in HUMAN_CHECK_HEADINGS:
        human = _section_by_prefix(guidance, heading)
        if human is not None:
            break
    if machine is None and human is None:
        return _fallback_partition(guidance)
    if human is None:
        _, human = _fallback_partition(
            _remove_subsections(guidance, MACHINE_CHECK_HEADINGS))
    if machine is None:
        machine, _ = _fallback_partition(
            _remove_subsections(guidance, HUMAN_CHECK_HEADINGS))
    return machine, human

def build_ready_for_review_comment(issue_number: int, base: str,
                                   pr_number: int | None,
                                   guidance: str | None) -> str:
    """Build the ready-for-review handoff posted when work reaches Ready.

    Machine-runnable checks (commands and their recorded evidence) are shown
    for the ready-review QA agent, which re-runs only a check that lacks
    recorded evidence. The part addressed to the human is limited to the
    steps that genuinely need a person (product review, taste, decisions).
    """
    guidance = sanitize_test_guidance(guidance)
    machine, human = split_test_guidance(guidance)
    if pr_number:
        status = f"Develop PR #{pr_number} was merged into `{base}`."
    else:
        status = (
            f"The implementation run completed, but no linked develop PR was "
            f"available to include here. Verify the current `{base}` checkout "
            "before testing."
        )
    lines = [
        "<!-- news:ready-for-review-test -->",
        "## Ready for Review — how to test",
        "",
        status,
        "",
        "### Machine checks",
    ]
    if machine:
        lines.extend([
            "The implementation run executed these checks and recorded the "
            "results below. The ready-review QA agent re-runs only a check "
            "that lacks recorded evidence.",
            "",
            machine,
        ])
    else:
        lines.append(
            "No runnable machine checks were recorded by the implementation "
            "workflow."
        )
    lines.extend(["", "### Human checks"])
    if human:
        lines.append(human)
    else:
        lines.append(
            "No manual steps are required — every recorded check is "
            "machine-runnable."
        )
    lines.extend([
        "",
        "After the machine checks pass, the ready-review QA agent moves this "
        "issue to In Review. To move it yourself instead:",
        f"`python3 automation/move_item.py {issue_number} \"In Review\"`",
    ])
    return "\n".join(lines)

def match_issue_pr(prs: list[dict], issue_number: int,
                   base: str | None = None) -> dict | None:
    """First PR whose body or title references the issue; optional base filter.

    An issue can have several PRs (develop PR, ship PR, re-review duplicates);
    the `base` filter pins the role (e.g. the develop PR vs the ship PR), so
    the develop-merge passes never grab the ship PR and retarget it.

    Fallback: the dispatch branch is `issue-<N>` and Archon's worktree PR
    branch is `archon/task-issue-<N>` — either one in the head ref links the
    PR to the issue even when the generated body has no keyword line.
    """
    pat = re.compile(
        rf"(?im)^[ \t]*(?:fix(?:es)?|clos(?:es|e)|resolv(?:es|e)|issue)"
        rf"\s*:?[ \t]*#{issue_number}\b(?=\s*(?:$|[.,:)\]]))"
    )
    for pr in prs:
        if base and pr.get("baseRefName") != base:
            continue
        if pat.search(pr.get("body") or ""):
            return pr
    for pr in prs:
        if base and pr.get("baseRefName") != base:
            continue
        if re.search(rf"\(#{issue_number}\)\s*$", pr.get("title") or ""):
            return pr
    branch_pat = re.compile(
        rf"(?:^|[^0-9])issue-{issue_number}(?:$|[^0-9])", re.I)
    for pr in prs:
        if base and pr.get("baseRefName") != base:
            continue
        if branch_pat.search(pr.get("headRefName") or ""):
            return pr
    return None

def is_decision_only(cfg: dict, labels: list[str]) -> bool:
    label = str((cfg.get("decision_only") or {}).get(
        "label", "decision-only")).casefold()
    return label in {name.casefold() for name in labels}

def pick_workflow(cfg: dict, labels: list[str]) -> str:
    todo_cfg = cfg["dispatch"]["todo"]
    for label in labels:
        wf = todo_cfg["label_overrides"].get(label.lower())
        if wf:
            return wf
    return todo_cfg.get("default", "archon-fix-github-issue")

def conflict_episode_action(mergeable: str, fix_msg: str | None,
                            fix_status: str | None, mech_failed: bool,
                            *, retried: bool = False) -> str:
    """Next step for a conflicting ship PR in the review lane.

    mergeable: GitHub's value (CONFLICTING / MERGEABLE / UNKNOWN).
    fix_msg: dispatch message of the dedicated fix run, or None.
    fix_status: run status for fix_msg, or None when fix_msg is None.
    (None with fix_msg set = run not yet registered or status lookup
    failed — treated as "active", never escalated.)
    mech_failed: the mechanical merge API already hit real conflicts.
    retried: the fix run already got one bounded re-dispatch this episode.

    Returns one of:
      "update"   try the mechanical base-into-head merge (no fix run yet)
      "dispatch" mechanical merge failed with real conflicts — start the fix run
      "active"   fix run in flight — wait
      "retry"    fix run finished terminal but the PR is still conflicting
                 and this episode has not retried yet — re-dispatch once
      "failed"   fix run terminal (or retried) and the PR is still
                 conflicting — human escalation
      "clear"    PR no longer conflicting — episode over, drop markers
      "none"     nothing to do (mergeable and no episode markers)
      "wait"     mergeability unknown — recheck next poll
    """
    if mergeable == "UNKNOWN":
        return "wait"
    if mergeable != "CONFLICTING":
        return "clear" if (fix_msg or mech_failed) else "none"
    if not fix_msg:
        return "dispatch" if mech_failed else "update"
    if fix_status is None:
        # Fix run dispatched but not yet registered in `archon workflow runs`
        # (async spawn), or the status lookup failed — never escalate on
        # unknown state; wait for a positive terminal status.
        return "active"
    if fix_status in ACTIVE_WORKFLOW_STATUSES:
        return "active"
    if not retried:
        return "retry"
    return "failed"

def develop_conflict_action(mech_tried: bool, fix_msg: str | None,
                            fix_status: str | None,
                            *, retried: bool = False) -> str:
    """Next step for a conflicting develop PR in the completion lane.

    mech_tried: the mechanical base-into-head merge already hit real conflicts.
    fix_msg: dispatch message of the develop resolver run, or None.
    fix_status: run status for fix_msg, or None when fix_msg is None.
    retried: the resolver run already got one bounded re-dispatch this episode.

    Returns one of:
      "mech"     try the mechanical base-into-head merge (no fix run yet)
      "dispatch" mechanical merge failed with real conflicts — start the resolver
      "active"   resolver run in flight — wait (the outer merge retries anyway)
      "retry"    resolver finished terminal but the PR is still conflicting
                 and this episode has not retried yet — re-dispatch once
      "failed"   resolver terminal (or retried) and the PR is still
                 conflicting — needs human
    """
    if not mech_tried:
        return "mech"
    if not fix_msg:
        return "dispatch"
    if fix_status in ACTIVE_WORKFLOW_STATUSES:
        return "active"
    if not retried:
        return "retry"
    return "failed"

def workflow_status_by_message(runs: list[dict]) -> dict[str, str]:
    """Map each run message to the newest run status."""
    best: dict[str, tuple[str, str]] = {}
    for run in runs:
        run_msg = run.get("user_message") or ""
        if not run_msg:
            continue
        started = run.get("started_at") or ""
        if started > best.get(run_msg, ("", ""))[1]:
            best[run_msg] = (run.get("status") or "", started)
    return {msg: status for msg, (status, _) in best.items()}

def run_status_for(runs_by_msg: dict[str, str], dispatch_msg: str) -> str | None:
    """Status of the newest run whose message contains the dispatch message."""
    for msg, status in runs_by_msg.items():
        if dispatch_msg in msg:
            return status
    return None
