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
    r"(?:python3?|pytest|uv|news|npm|pnpm|yarn|curl|make)\b",
    re.M,
)


SHELL_FENCE_LANGUAGES = frozenset(
    {"", "bash", "sh", "shell", "zsh", "console", "terminal"}
)


SHELL_COMMAND_RE = re.compile(
    r"^[ \t]*(?:\$[ \t]*)?(?:[\w.-]+/)*"
    r"(?:python3?|pytest|uv|news|npm|pnpm|yarn|curl|make)\b"
)


SHELL_INLINE_COMMENT_RE = re.compile(r"[ \t]+#[ \t].*$")


DEFERRED_SECTION_RE = re.compile(r"^##\s+deferred\s+work\s*$", re.M | re.I)


DEFERRED_NONE_RE = re.compile(r"^\s*\*none\*\.?\s*$", re.I)


DEFERRAL_HINT_RE = re.compile(
    r"\b(defer\w*|out\s+of\s+scope|not\s+in\s+scope|follow-?up|for\s+later"
    r"|later\s+(?:phase|release|work|iteration|pass))\b",
    re.I)


UNCHECKED_CRITERION_RE = re.compile(r"^\s*-\s*\[\s*\]\s+(.+)$", re.M)


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


def parse_deferred_work(body: str) -> list[dict] | None:
    """Items from the `## Deferred work` section of a completion comment.

    Format (one bullet per item; indented fields attach to the current item):
      - **Title:** <imperative title, <=72 chars>
        **Description:** <1-2 sentences>
        **Reason:** <why deferred now>
        **Label:** <optional; must already exist in the repo>
        **Links to:** #N       (already tracked — the model judged it covered)
        **Supersedes:** #N     (closed issue — create a new one referencing it)
        **Skip:** <reason>     (never-to-be-done — do not create)
    Returns None when the section is absent; [] when the section is present
    but empty or `*None.*` (the contract's explicit "nothing deferred" form).
    """
    m = DEFERRED_SECTION_RE.search(body or "")
    if not m:
        return None
    chunk = (body or "")[m.end():]
    end = re.search(r"^##\s+", chunk, re.M)
    if end:
        chunk = chunk[:end.start()]
    if not chunk.strip() or DEFERRED_NONE_RE.match(chunk):
        return []
    items: list[dict] = []
    for block in re.split(r"^-\s*\*\*Title:\*\*\s*", chunk, flags=re.M)[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        item = {"title": lines[0].strip(),
                "description": "", "reason": "", "label": "",
                "links_to": None, "supersedes": None, "skip": "",
                "out_of_scope": ""}
        for line in lines[1:]:
            fm = re.match(r"^\s*\*\*([\w ]+?):\*\*\s*(.*)$", line)
            if not fm:
                continue
            key = fm.group(1).strip().lower()
            val = fm.group(2).strip()
            if key == "links to":
                m = re.search(r"#?(\d+)", val)
                item["links_to"] = int(m.group(1)) if m else None
            elif key == "supersedes":
                m = re.search(r"#?(\d+)", val)
                item["supersedes"] = int(m.group(1)) if m else None
            elif key == "skip":
                item["skip"] = val
            elif key == "out of scope":
                item["out_of_scope"] = val
            elif key in item and val:
                item[key] = val
        if item["title"]:
            items.append(item)
    return items


def normalize_title(title: str) -> str:
    """Dedupe key: casefold, drop non-alphanumerics."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").casefold())


def dedupe_deferred(item: dict, open_issues: list[dict],
                    closed_issues: list[dict]) -> tuple[str, int | None]:
    """Safety net for deferred items the model did not link itself.

    The completion-comment node is the real judge (it consults issue titles +
    initial bodies and repo context and stamps each item with `Links to:` /
    `Supersedes:` / `Skip:`). This exact-title check only ever LINKS or adds a
    reference — it can never create an issue the model would not have wanted.

    Returns:
      ("link", n)        an OPEN issue with the same normalized title exists
      ("create-ref", n)  only a CLOSED issue matches — create a new one
                          referencing #n
      ("create", None)   no exact match — create
    """
    target = normalize_title(item.get("title") or "")
    if target:
        for iss in open_issues:
            if normalize_title(iss.get("title") or "") == target:
                return "link", iss["number"]
        for iss in closed_issues:
            if normalize_title(iss.get("title") or "") == target:
                return "create-ref", iss["number"]
    return "create", None


def has_deferral_language(body: str) -> bool:
    """True when free text plausibly mentions deferred/out-of-scope work.

    Used only for the fallback warning — never for auto-creating issues.
    """
    return bool(DEFERRAL_HINT_RE.search(body or ""))


def find_unchecked_criteria(body: str) -> list[str]:
    """Unchecked `- [ ]` checklist lines in a completion record.

    An acceptance criterion the run did not mark done is a second deferral
    signal, independent of the model's word choice.
    """
    return [m.group(1).strip()
            for m in UNCHECKED_CRITERION_RE.finditer(body or "")]


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


def build_ready_for_review_comment(issue_number: int, base: str,
                                   pr_number: int | None,
                                   guidance: str | None) -> str:
    """Build the human-facing test handoff posted when work reaches Ready."""
    guidance = sanitize_test_guidance(guidance)
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
        "### Steps",
        "1. In a clean checkout, update the integration branch:",
        "   ```bash",
        f"   git switch {base}",
        f"   git pull --ff-only origin {base}",
        "   ```",
        "2. Run the issue-specific checks below and exercise every acceptance "
        "criterion in this issue.",
        "",
        "### Issue-specific checks",
    ]
    if guidance:
        lines.append(guidance)
    else:
        lines.extend([
            "No runnable issue-specific instructions were recorded by the "
            "implementation workflow.",
            f"Test the acceptance criteria in this issue against `{base}`. If "
            "there is no manual path, review the focused automated checks in "
            f"the implementation PR for issue #{issue_number}.",
        ])
    lines.extend([
        "",
        "When it works, move the issue to In Review:",
        f"`python3 automation/move_item.py {issue_number} \"In Review\"`",
    ])
    return "\n".join(lines)


def match_issue_pr(prs: list[dict], issue_number: int,
                   base: str | None = None) -> dict | None:
    """First PR whose body or title references the issue; optional base filter.

    An issue can have several PRs (develop PR, ship PR, re-review duplicates);
    the `base` filter pins the role (e.g. the develop PR vs the ship PR), so
    the develop-merge passes never grab the ship PR and retarget it.
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
                            fix_status: str | None, mech_failed: bool) -> str:
    """Next step for a conflicting ship PR in the review lane.

    mergeable: GitHub's value (CONFLICTING / MERGEABLE / UNKNOWN).
    fix_msg: dispatch message of the dedicated fix run, or None.
    fix_status: run status for fix_msg, or None when fix_msg is None.
    (None with fix_msg set = run not yet registered or status lookup
    failed — treated as "active", never escalated.)
    mech_failed: the mechanical merge API already hit real conflicts.

    Returns one of:
      "update"   try the mechanical base-into-head merge (no fix run yet)
      "dispatch" mechanical merge failed with real conflicts — start the fix run
      "active"   fix run in flight — wait
      "failed"   fix run finished but the PR is still conflicting
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
    return "failed"


def develop_conflict_action(mech_tried: bool, fix_msg: str | None,
                            fix_status: str | None) -> str:
    """Next step for a conflicting develop PR in the completion lane.

    mech_tried: the mechanical base-into-head merge already hit real conflicts.
    fix_msg: dispatch message of the develop resolver run, or None.
    fix_status: run status for fix_msg, or None when fix_msg is None.

    Returns one of:
      "mech"     try the mechanical base-into-head merge (no fix run yet)
      "dispatch" mechanical merge failed with real conflicts — start the resolver
      "active"   resolver run in flight — wait (the outer merge retries anyway)
      "failed"   resolver finished but the PR is still conflicting — needs human
    """
    if not mech_tried:
        return "mech"
    if not fix_msg:
        return "dispatch"
    if fix_status in ACTIVE_WORKFLOW_STATUSES:
        return "active"
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
