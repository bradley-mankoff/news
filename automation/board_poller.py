#!/usr/bin/env python3
"""Board poller for the Daily News GitHub project.

Watches the project board and dispatches Archon workflows when items move
between lanes. Contract:

- New issues land in the FIRST lane (Backlog). Work never starts on creation —
  it starts only when an item is MOVED INTO the todo lane (e.g. "Todo").
- The first poll after (re)start is a snapshot: state is recorded, nothing is
  dispatched. This prevents backlog bursts after the poller was down.
- Every real transition into a dispatch lane fires one run: move into "Todo"
  starts implementation, move into "In Review" starts a PR review. Moving back
  out and in again fires again (re-work / re-review).
- Dependency gate: an issue whose body has a `Depends on: #N` line does not
  dispatch until every referenced issue is in the Done lane. It moves to
  Blocked with a comment; when the deps ship it returns to Todo automatically
  (and dispatches on the next poll). A dependency that closes without shipping
  posts a re-scope notice on each dependent, once per episode.
- Runnable label: the configured `runnable` label marks open issues in Todo
  whose dependencies are all in Done; it is removed as soon as eligibility
  ends.
- Verdict gate: the ship PR is merged (and the issue closed) only when the
  review run completed AND its final comment carries `VERDICT: approve`.
  Any other verdict (or none) holds the ship in In Review with a notice.
- Ship-conflict auto-fix: a CONFLICTING ship PR blocks the merge even after an
  approving verdict. The poller first merges the base into the head via the
  GitHub merge API (free, instant); on real conflicts it dispatches
  `archon-fix-ship-conflicts` once per episode (state markers), which resolves,
  validates, and pushes. Episodes end when the PR is mergeable; a fix run that
  finishes with the PR still conflicting posts a human-help comment once.
- Deferred-work guard: implementation completion records carry a `## Deferred
  work` section (one bullet per deferred item; contract enforced by the
  completion-comment nodes). The completion-comment node is the dedupe judge:
  it consults open/closed issue titles + initial bodies and repo context
  (HANDOFF.md, ADRs) and stamps each item `**Links to:** #N` (already tracked),
  `**Supersedes:** #N` (closed — create a new one referencing it), `**Skip:**`
  (never-to-be-done), or leaves it bare (create). The poller executes
  mechanically: links, creates (boarded in the default lane), skips, and
  comments the linkage on the source issue; an exact-title safety check links
  but never creates. Deferral language or unchecked acceptance criteria
  without the section post a verification comment instead (never auto-create
  from prose). Idempotent via per-run state markers; retried next poll on
  failure.
- Ready-for-Review handoff: after the completed implementation PR is merged into
  `develop`, the poller posts a bottom-of-issue comment with the workflow's
  `## How to test` guidance (or an explicit fallback when none was recorded).
  Failed comment posts are retried for issues already in Ready for Review.
- Dispatch = `archon workflow run <wf> --branch <branch> "<msg>"` executed in
  the repo root as a detached child (`subprocess.Popen` + `start_new_session`).
  `--detach` is NOT used — the archon-pi build's detached-child spawn is broken
  (see `dispatch()`).

Config: automation/config.json (repo, committed).
State:  automation/state.json (gitignored, machine-local).
Log:    stdout; the launchd agent redirects to automation/board_poller.log.

Requires: gh CLI (authenticated), archon CLI on PATH. Python stdlib only.

Flags: --once runs a single poll and exits; --dry-run runs one poll with no
mutations (no dispatch, no lane moves, no comments, no merges, no state write).
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DRY_RUN = "--dry-run" in sys.argv
ACTIVE_WORKFLOW_STATUSES = frozenset(
    {"running", "pending", "queued", "scheduled", "paused"}
)
_DISPATCH_BUDGET: int | None = None

QUERY = """
query($login: String!, $number: Int!, $statusField: String!, $cursor: String) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
      items(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          statusValue: fieldValueByName(name: $statusField) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue {
              number title url state body
              repository { nameWithOwner }
              labels(first: 20) { nodes { name } }
            }
            ... on PullRequest {
              number title url
              repository { nameWithOwner }
            }
          }
        }
      }
    }
  }
}
"""

MOVE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: { singleSelectOptionId: $optionId }
  }) { projectV2Item { id } }
}
"""

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


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_config() -> dict:
    cfg = json.loads((ROOT / "automation" / "config.json").read_text())
    cfg.setdefault("poll_interval_seconds", 45)
    return cfg


def gh(args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, env=env
    )


# --- Local develop sync (keep the dev UI on the latest integration code) --
#
# Develop merges happen server-side via the GitHub API, so the local checkout
# never learns about them on its own. After every successful develop merge the
# poller refreshes the local checkout (fetch + fast-forward only) and restarts
# the control-panel UI if it is running, so the dev loop is: merge lands ->
# local code + UI are already current when the issue moves to Ready for Review.
# Never destructive: dirty trees, non-develop branches, and unpushed local
# commits all skip with a logged reason instead of forcing.

UI_HOST = "127.0.0.1"
UI_PORT = 8766
UI_LOG_PATH = "/tmp/news-ui.log"


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ui_running() -> bool:
    return _port_open(UI_HOST, UI_PORT)


def _restart_ui() -> bool:
    """Kill a running news UI and relaunch it; True when the port comes back."""
    subprocess.run(["pkill", "-f", "news ui"], capture_output=True, text=True)
    time.sleep(1)
    news_bin = ROOT / ".venv" / "bin" / "news"
    if not news_bin.exists():
        log(f"LOCAL SYNC: cannot restart UI - missing {news_bin}")
        return False
    with open(UI_LOG_PATH, "a") as out:
        subprocess.Popen(
            [str(news_bin), "ui", "--host", UI_HOST, "--port", str(UI_PORT)],
            stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(ROOT),
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _port_open(UI_HOST, UI_PORT):
            return True
        time.sleep(1)
    return False


def sync_local_develop() -> str:
    """Refresh the local develop checkout and UI after a remote merge.

    Fast-forward only: skips (never forces) when the tree is dirty, the
    checked-out branch is not develop, or local develop has unpushed
    commits. Restarts the control-panel UI only when it is running.
    Returns a one-line summary for the poller log.
    """
    if DRY_RUN:
        return "LOCAL SYNC: dry-run (no fetch/merge/restart)"

    def run_git(args: list[str], timeout: int):
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, text=True,
                timeout=timeout, cwd=str(ROOT),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, str(exc)
        if result.returncode != 0:
            detail = result.stderr.strip()[:200] or f"exit status {result.returncode}"
            return None, detail
        return result, None

    fetch, error = run_git(["fetch", "origin"], 90)
    if fetch is None:
        return f"LOCAL SYNC FAILED: git fetch: {error}"
    branch, error = run_git(["rev-parse", "--abbrev-ref", "HEAD"], 30)
    if branch is None:
        return f"LOCAL SYNC FAILED: git branch check: {error}"
    if branch.stdout.strip() != "develop":
        return (f"LOCAL SYNC SKIP: not on develop "
                f"(branch={branch.stdout.strip() or '?'!r})")
    dirty, error = run_git(["status", "--porcelain"], 30)
    if dirty is None:
        return f"LOCAL SYNC FAILED: git status: {error}"
    dirty_files = [ln for ln in dirty.stdout.splitlines() if ln.strip()]
    if dirty_files:
        return f"LOCAL SYNC SKIP: working tree dirty ({len(dirty_files)} file(s))"
    ahead, error = run_git(["rev-list", "--count", "origin/develop..HEAD"], 30)
    if ahead is None:
        return f"LOCAL SYNC FAILED: git ahead check: {error}"
    if ahead.stdout.strip() != "0":
        n = ahead.stdout.strip()
        return (f"LOCAL SYNC SKIP: local develop has {n} unpushed commit(s); "
                "sync blocked until they are pushed (fast-forward only)")
    behind, error = run_git(["rev-list", "--count", "HEAD..origin/develop"], 30)
    if behind is None:
        return f"LOCAL SYNC FAILED: git behind check: {error}"
    if behind.stdout.strip() == "0":
        return "LOCAL SYNC: develop already up to date"
    merge, error = run_git(["merge", "--ff-only", "origin/develop"], 90)
    if merge is None:
        return f"LOCAL SYNC FAILED: fast-forward merge: {error}"
    merged = (merge.stdout.strip() or f"{behind.stdout.strip()} commit(s)").splitlines()[-1]
    if not _ui_running():
        return f"LOCAL SYNC: develop updated ({merged}); UI not running, left as is"
    if _restart_ui():
        return f"LOCAL SYNC: develop updated ({merged}); UI restarted"
    return (f"LOCAL SYNC WARNING: develop updated ({merged}) but UI restart "
            f"failed - see {UI_LOG_PATH}")


def graphql(cfg: dict, env: dict, cursor: str | None) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={QUERY}",
           "-F", f"login={cfg['project_owner']}",
           "-F", f"number={cfg['project_number']}",
           "-F", f"statusField={cfg['status_field']}"]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"graphql failed: {r.stderr.strip()[:500]}")
    return json.loads(r.stdout)


def fetch_project(cfg: dict, env: dict) -> tuple[str, str, dict, list[dict]]:
    """Returns (project_id, status_field_id, status_options: name->id, items).

    Each item is {"id", "status", "content"}; status is the lane name
    (or "No status" when the field is unset).
    """
    data = graphql(cfg, env, None)
    project = data["data"]["user"]["projectV2"]
    if project is None:
        raise RuntimeError(
            f"project {cfg['project_number']} not found for owner {cfg['project_owner']}"
        )
    field = next(
        (f for f in project["fields"]["nodes"]
         if f.get("name") == cfg["status_field"]), None)
    if field is None:
        raise RuntimeError(f"status field '{cfg['status_field']}' not found on project")
    options = {o["name"]: o["id"] for o in field["options"]}
    items: list[dict] = []
    page = project["items"]
    while True:
        for node in page["nodes"]:
            items.append({
                "id": node["id"],
                "status": ((node.get("statusValue") or {}).get("name"))
                          or "No status",
                "content": node.get("content") or {},
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        page = graphql(cfg, env, page["pageInfo"]["endCursor"])["data"]["user"]["projectV2"]["items"]
    return project["id"], field["id"], options, items


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


def sync_runnable_labels(cfg: dict, env: dict, items: list[dict],
                         number_lane: dict[int, str],
                         number_state: dict[int, str],
                         todo_lane: str | None,
                         done_lane: str | None) -> None:
    """Keep the dispatch-eligible label aligned with board state."""
    label = cfg.get("runnable_label")
    if not label:
        return
    if DRY_RUN:
        log(f"[dry-run] ensure label {label!r} exists")
    else:
        try:
            created = gh(
                ["label", "create", label, "-R", cfg["repo"],
                 "--color", "0e8a16",
                 "--description", "Todo issue with satisfied dependencies",
                 "--force"],
                env,
            )
        except Exception as exc:
            log(f"RUNNABLE LABEL ENSURE FAILED: {exc}")
            return
        if created.returncode != 0:
            log(f"RUNNABLE LABEL ENSURE FAILED: "
                f"{created.stderr.strip()[:200]}")
            return

    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        number = content["number"]
        present = label in {
            node["name"] for node in (content.get("labels") or {}).get("nodes", [])
        }
        desired = issue_is_runnable(
            content,
            item["status"],
            number_lane,
            number_state,
            todo_lane,
            done_lane,
        )
        if present == desired:
            continue
        action = "--add-label" if desired else "--remove-label"
        if DRY_RUN:
            log(f"[dry-run] {action} {label} issue={number}")
            continue
        try:
            updated = gh(
                ["issue", "edit", str(number), "-R", cfg["repo"],
                 action, label],
                env,
            )
        except Exception as exc:
            log(f"RUNNABLE LABEL UPDATE FAILED issue={number}: {exc}")
            continue
        if updated.returncode != 0:
            log(f"RUNNABLE LABEL UPDATE FAILED issue={number}: "
                f"{updated.stderr.strip()[:200]}")
            continue
        log(f"RUNNABLE LABEL {'ADDED' if desired else 'REMOVED'} issue={number}")


# ─── Deferred-work guard ────────────────────────────────────────────────
# Implementation workflows must list deferred work in the completion record
# under a `## Deferred work` section (contract: README, Project Automation →
# "Deferred work is auto-tracked" bullet; enforced by the completion-comment
# nodes in apply_workflow_edits.py).
# When a run completes the poller parses that section and guarantees each item
# is tracked as a board issue: dedupe against existing issues, create when
# missing, put it in the default lane, and comment the linkage on the source
# issue. Deferral language without the section posts a verification comment
# instead of auto-creating (prose is not a reliable title source).

DEFERRED_SECTION_RE = re.compile(r"^##\s+deferred\s+work\s*$", re.M | re.I)
DEFERRED_NONE_RE = re.compile(r"^\s*\*none\*\.?\s*$", re.I)
DEFERRAL_HINT_RE = re.compile(
    r"\b(defer\w*|out\s+of\s+scope|not\s+in\s+scope|follow-?up|for\s+later"
    r"|later\s+(?:phase|release|work|iteration|pass))\b",
    re.I)
UNCHECKED_CRITERION_RE = re.compile(r"^\s*-\s*\[\s*\]\s+(.+)$", re.M)


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
    Indented fields: `**Links to:** #N` (already tracked — the model judged it
    covered), `**Supersedes:** #N` (closed issue — create a new one referencing
    it), `**Skip:** <reason>` (never-to-be-done — do not create).
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


def find_issue_pr(cfg: dict, env: dict, issue_number: int,
                  state: str = "all", base: str | None = None) -> tuple[dict | None, bool]:
    """(pr, ok) for the PR linked to an issue (any state).

    Pass `base` (e.g. "develop") when a specific PR role is required — an
    issue accumulates several PRs over its life and the newest is not
    necessarily the one a pass wants. ok=False means the gh lookup itself
    failed (network/auth/rate limit) — callers must NOT treat None as
    "no PR exists" and advance; they should retry on the next poll.
    """
    r = gh(["pr", "list", "-R", cfg["repo"], "--state", state,
            "--limit", "200",
            "--json", "number,title,body,headRefName,baseRefName,state"], env)
    if r.returncode != 0:
        log(f"find_issue_pr: gh pr list failed for issue #{issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None, False
    try:
        prs = json.loads(r.stdout)
    except ValueError as exc:
        log(f"find_issue_pr: unparseable gh output for issue #{issue_number}: {exc}")
        return None, False
    return match_issue_pr(prs, issue_number, base), True


def merge_pr_to_base(cfg: dict, env: dict, pr: dict, base: str,
                     issue_number: int | None = None) -> tuple[bool, str]:
    """Retarget a PR to `base`, mark it ready, merge with a merge commit.

    Callers pass `issue_number` for develop merges so the issue can be
    reopened if GitHub's keyword auto-close (`Fixes #N` in the PR body or
    commit message) closes it early — the issue must stay open until the
    ship PR merges into main. Without an issue number the merge proceeds
    and the reopen check is skipped; callers should not merge develop PRs
    that are not linked to an issue. A PR already merged short-circuits as
    success; a PR closed WITHOUT merging is a loud failure (never treat it
    as merged — the code is not in `base`).
    """
    if DRY_RUN:
        log(f"[dry-run] MERGE PR #{pr.get('number')} -> {base}")
        return True, "dry-run (no merge)"
    if pr.get("state") == "MERGED":
        return True, "already merged"
    if pr.get("state") == "CLOSED":
        return False, "PR closed without merging; re-drag the issue to Todo to re-run"
    r = gh(["pr", "edit", str(pr["number"]), "-R", cfg["repo"], "--base", base], env)
    if r.returncode != 0:
        return False, f"retarget failed: {r.stderr.strip()[:200]}"
    gh(["pr", "ready", str(pr["number"]), "-R", cfg["repo"]], env)
    rr = gh(["pr", "merge", str(pr["number"]), "-R", cfg["repo"], "--merge"], env)
    if rr.returncode != 0:
        return False, f"merge failed: {rr.stderr.strip()[:200]}"
    if issue_number:
        # GitHub applies keyword-based auto-close asynchronously after the
        # merge (PR-body keywords AND commit-message keywords like "Fix #N"),
        # so wait for it to land before checking whether we must reopen.
        time.sleep(6)
        q = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
                "--json", "state"], env)
        if q.returncode == 0 and json.loads(q.stdout).get("state") == "CLOSED":
            gh(["issue", "reopen", str(issue_number), "-R", cfg["repo"]], env)
            return True, f"merged into {base} (issue #{issue_number} reopened)"
    return True, f"merged into {base}"


def find_or_create_ship_pr(cfg: dict, env: dict, head: str, title: str,
                           issue_number: int, base: str) -> dict | None:
    """Reuse the open ship PR for this head/base, or create one."""
    if DRY_RUN:
        log(f"[dry-run] SHIP PR for head={head} base={base} (reuse or create)")
        return {"number": 0, "headRefName": head}
    r = gh(["pr", "list", "-R", cfg["repo"], "--head", head, "--state", "open",
            "--json", "number,baseRefName"], env)
    if r.returncode != 0:
        log(f"SHIP PR LIST FAILED head={head}: {r.stderr.strip()[:200]}")
    elif r.returncode == 0:
        for pr in json.loads(r.stdout):
            if pr.get("baseRefName") == base:
                return pr
    body = (f"Issue #{issue_number}. Shipped from develop after human testing. "
            "Reviewed by archon-smart-pr-review before merge.")
    r = gh(["pr", "create", "-R", cfg["repo"], "--base", base, "--head", head,
            "--title", title, "--body", body], env)
    if r.returncode != 0:
        log(f"SHIP PR CREATE FAILED head={head} issue={issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None
    m = re.search(r"pull/(\d+)", r.stdout or "")
    if not m:
        log(f"find_or_create_ship_pr: cannot parse PR number from: "
            f"{r.stdout[:200]!r}")
        return None
    return {"number": int(m.group(1)), "headRefName": head}


def branch_empty_vs_main(cfg: dict, env: dict, head: str, base: str) -> bool:
    """True when `head` has no commits beyond `base` — its work already
    reached base via another ship PR's develop merge (the #24 case), so a
    ship PR would be empty and a review would be meaningless."""
    r = gh(["api", f"repos/{cfg['repo']}/compare/{base}...{head}"], env)
    if r.returncode != 0:
        return False  # unknown — treat as shippable (safe default)
    try:
        return int(json.loads(r.stdout).get("ahead_by", 1)) == 0
    except (ValueError, TypeError):
        return False


def ensure_ship_review(cfg: dict, env: dict, item_id: str, issue_number: int,
                       title: str, project_id: str, field_id: str,
                       status_options: dict, done_name: str | None,
                       rec: dict) -> tuple[str, str, int] | str | None:
    """Open/verify the ship PR for an issue in the review lane and dispatch
    the review workflow.

    Returns ("ok", msg, ship_num) when a review run was dispatched;
    "shipped" when the branch has no commits beyond main (issue closed and
    moved to Done — nothing left to review); None when nothing could be done
    this poll (logged; the recheck pass retries).
    """
    merge_base = cfg["dispatch"]["todo"].get("merge_develop_base", "develop")
    pr, pr_ok = find_issue_pr(cfg, env, issue_number, base=merge_base)
    if not pr_ok:
        # A gh failure must never be misread as "no PR": the develop merge
        # would be skipped and the ship PR built from a guessed head branch.
        # Keep the item in place (status not recorded -> lane re-entered next
        # poll) and retry then.
        log(f"REVIEW PREP DEFERRED item={item_id} "
            f"issue={issue_number}: PR lookup failed (gh error); retrying next poll")
        return None
    if pr:
        ok, note = merge_pr_to_base(cfg, env, pr, merge_base, issue_number)
        log(f"DEVELOP MERGE issue={issue_number} PR=#{pr['number']}: {note}"
            if ok else
            f"DEVELOP MERGE FAILED issue={issue_number}: {note}")
    head = (pr or {}).get("headRefName") or f"archon/task-issue-{issue_number}"
    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
    if branch_empty_vs_main(cfg, env, head, ship_to):
        if DRY_RUN:
            log(f"[dry-run] ALREADY SHIPPED issue={issue_number} head={head}")
        else:
            comment_issue(
                cfg, env, issue_number,
                "Closing as shipped: this branch has no commits beyond main — "
                "its work already reached main via an earlier ship PR's develop "
                "merge. No review needed.")
            gh(["issue", "close", str(issue_number), "-R", cfg["repo"]], env)
            log(f"ALREADY SHIPPED issue={issue_number} head={head} -> {done_name}")
        if done_name:
            option_id = status_options.get(done_name)
            if option_id:
                move_to_lane(cfg, env, project_id, item_id, field_id, option_id)
        rec.pop("review_msg", None)
        rec.pop("ship_pr", None)
        rec.pop("review_held", None)
        return "shipped"
    ship = find_or_create_ship_pr(
        cfg, env, head, f"Ship: {title} (#{issue_number})", issue_number, ship_to)
    if not ship:
        log(f"SHIP PR UNAVAILABLE issue={issue_number} head={head} — retrying next poll")
        return None
    msg = (f"Review PR #{ship['number']} (ship to {ship_to} for issue "
           f"#{issue_number}: {title}).")
    branch = f"review/issue-{issue_number}"
    if not dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"],
                    branch, msg, item_id, issue_number):
        log(f"SHIP REVIEW DISPATCH FAILED issue={issue_number} — retrying next poll")
        return None
    rec.pop("review_held", None)
    return "ok", msg, ship["number"]


def pick_workflow(cfg: dict, labels: list[str]) -> str:
    todo_cfg = cfg["dispatch"]["todo"]
    for label in labels:
        wf = todo_cfg["label_overrides"].get(label.lower())
        if wf:
            return wf
    return todo_cfg.get("default", "archon-fix-github-issue")


def move_to_lane(cfg: dict, env: dict, project_id: str, item_id: str,
                 field_id: str, option_id: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] MOVE item={item_id} -> option {option_id}")
        return True
    r = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={MOVE_MUTATION}",
         "-F", f"projectId={project_id}",
         "-F", f"itemId={item_id}",
         "-F", f"fieldId={field_id}",
         "-F", f"optionId={option_id}"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return r.returncode == 0


def comment_issue(cfg: dict, env: dict, issue_number: int, body: str) -> bool:
    if DRY_RUN:
        log(f"[dry-run] COMMENT issue={issue_number}: {body[:100]}")
        return True
    r = gh(["issue", "comment", str(issue_number), "-R", cfg["repo"],
            "--body", body], env)
    return r.returncode == 0

def fetch_issue_comments(cfg: dict, env: dict,
                         issue_number: int) -> list[dict] | None:
    """Fetch issue comments, returning None only when GitHub lookup fails."""
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
            "--json", "comments"], env)
    if r.returncode != 0:
        log(f"READY TEST FETCH FAILED issue #{issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError as exc:
        log(f"READY TEST PARSE FAILED issue #{issue_number}: {exc}")
        return None
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        log(f"READY TEST PARSE FAILED issue #{issue_number}: comments is not a list")
        return None
    return comments


def post_ready_for_review_comment(cfg: dict, env: dict, issue_number: int,
                                  base: str, pr_number: int | None = None) -> bool:
    """Post the test handoff after an issue reaches Ready for Review."""
    if DRY_RUN:
        return comment_issue(
            cfg, env, issue_number,
            build_ready_for_review_comment(issue_number, base, pr_number, None),
        )
    comments = fetch_issue_comments(cfg, env, issue_number)
    if comments is None:
        return False
    guidance = extract_test_guidance(comments)
    body = build_ready_for_review_comment(issue_number, base, pr_number, guidance)
    ok = comment_issue(cfg, env, issue_number, body)
    if not ok:
        log(f"READY TEST COMMENT FAILED issue #{issue_number}")
    return ok


def issue_has_label(cfg: dict, env: dict, issue_number: int, label: str) -> bool | None:
    """True/False, or None when the label state could not be determined.

    A gh failure must never be misread as "label absent": the caller gates the
    Blocked-vs-complete decision on this, and a blocked issue that falls through
    to the normal completion path could ship past the human's decision.
    """
    try:
        r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
                "--json", "labels", "-q", ".labels[].name"], env)
    except subprocess.TimeoutExpired as exc:
        log(f"LABEL CHECK TIMEOUT issue={issue_number}: {exc}")
        return None
    if r.returncode != 0:
        log(f"LABEL CHECK FAILED issue={issue_number}: {r.stderr.strip()[:200]}")
        return None
    return label in r.stdout.split()


def resolve_worktree_branch(env: dict, issue_number: int, repo: str) -> str | None:
    """Find the archon worktree branch for an issue (e.g. archon/task-issue-12).

    `archon continue` needs the full namespaced branch, not the shorthand the
    poller passes to `workflow run --branch`. The parse is scoped to this
    repo's section of `archon isolation list` so a same-named worktree of
    another repository can never be resumed.
    """
    r = subprocess.run(["archon", "isolation", "list"], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(ROOT))
    if r.returncode != 0:
        log(f"WORKTREE LIST FAILED: {r.stderr.strip()[:200]}")
        return None
    pat = re.compile(rf"task-issue-{issue_number}\b")
    in_repo = False
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.endswith(":") and "github.com" in line:   # repo section header
            in_repo = repo in line
            continue
        if in_repo and line.lstrip().startswith("{"):  # archon JSON log line
            continue
        if in_repo and pat.search(line) and not line.startswith(("Path", "Type")):
            return line
    return None


def resume_issue(cfg: dict, env: dict, branch: str, wf: str,
                 issue_number: int) -> tuple[bool, str, str | None]:
    """Resume a blocked issue in its existing worktree after human input.

    Uses `archon continue` so the workflow picks up in the same worktree with
    prior context; the human's latest comment is passed as the message.

    Returns (ok, msg, full_branch). ok=False means the resume was NOT started
    (or the spawned process died immediately); the caller falls back to a
    fresh dispatch. Every False path leaves NO `archon continue` child
    running, so the fallback can never double-run the issue: the needs-input
    label is removed BEFORE the spawn, so a failed label edit means no child
    was ever created. A lingering label stays as the recovery signal only on
    the defer path (worktree branch unresolvable).
    """
    if DRY_RUN:
        log(f"[dry-run] RESUME issue={issue_number} branch={branch} wf={wf}")
        return True, "dry-run", branch
    if not _dispatch_slot_available(wf, issue_number):
        return False, "Archon workflow capacity is full", None
    full_branch = resolve_worktree_branch(env, issue_number, cfg["repo"])
    if full_branch is None:
        log(f"RESUME DEFERRED issue={issue_number}: worktree branch not found "
            f"(needs-input label kept; retrying next poll)")
        return False, "", None
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"], "--json", "comments",
            "-q", ".comments[-1].body"], env)
    if r.returncode != 0:
        log(f"COMMENT FETCH FAILED issue={issue_number}: {r.stderr.strip()[:200]} "
            f"(resuming without the answer in context)")
    answer = (r.stdout or "").strip()[:600] if r.returncode == 0 else ""
    msg = (f"Resuming issue #{issue_number} after human input."
           + (f" Latest comment from the human: {answer}" if answer else ""))
    # Remove the needs-input label FIRST: if this fails, no child has been
    # spawned, so the caller's fresh-dispatch fallback cannot start a SECOND
    # concurrent run for the same issue/worktree. The label edit is the gate;
    # only a verified removal proceeds to spawn.
    r = gh(["issue", "edit", str(issue_number), "-R", cfg["repo"],
            "--remove-label", "needs-input"], env)
    if r.returncode != 0:
        log(f"RESUME LABEL REMOVE FAILED issue={issue_number}: "
            f"{r.stderr.strip()[:200]} (label kept; not spawning)")
        return False, msg, None
    log_path = ROOT / "automation" / "archon-runs.log"
    try:
        with open(log_path, "a") as out:
            proc = subprocess.Popen(
                ["archon", "continue", full_branch, "--workflow", wf, msg],
                cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        log(f"RESUME FAILED issue={issue_number}: {exc}")
        return False, msg, None
    # A short grace period catches immediate non-zero exits (bad branch or
    # workflow name): resuming must not be reported as success when no run
    # will actually run. The label is already removed, so the caller's fresh
    # dispatch replaces the dead run without a re-block cycle.
    time.sleep(2)
    if proc.poll() is not None:
        log(f"RESUME FAILED issue={issue_number}: archon continue exited "
            f"immediately with code {proc.returncode}")
        return False, msg, None
    _consume_dispatch_slot()
    log(f"RESUMED issue={issue_number} branch={full_branch} wf={wf} pid={proc.pid}")
    return True, msg, full_branch


def fetch_active_workflow_count(env: dict) -> int | None:
    """Return active Archon runs, or None when the status lookup is unusable."""
    try:
        result = subprocess.run(
            ["archon", "workflow", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    runs = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        return None
    return sum(
        1 for run in runs
        if isinstance(run, dict)
        and (run.get("status") or "").lower() in ACTIVE_WORKFLOW_STATUSES
    )


def prepare_dispatch_budget(cfg: dict, env: dict) -> None:
    """Reserve this poll's dispatch slots from Archon's conversation cap."""
    global _DISPATCH_BUDGET
    if DRY_RUN:
        _DISPATCH_BUDGET = None
        return
    try:
        limit = max(0, int(cfg.get("max_concurrent_workflows", 10)))
    except (TypeError, ValueError):
        limit = 10
    active = fetch_active_workflow_count(env)
    if active is None:
        _DISPATCH_BUDGET = 0
        log("DISPATCH HOLD: active Archon workflow count unavailable")
        return
    _DISPATCH_BUDGET = max(0, limit - active)
    if _DISPATCH_BUDGET == 0:
        log(f"DISPATCH HOLD: Archon workflow capacity reached ({limit})")


def _dispatch_slot_available(wf: str, number: int) -> bool:
    if _DISPATCH_BUDGET is not None and _DISPATCH_BUDGET <= 0:
        log(f"DISPATCH DEFERRED issue={number} wf={wf}: "
            "Archon workflow capacity is full")
        return False
    return True


def _consume_dispatch_slot() -> None:
    global _DISPATCH_BUDGET
    if _DISPATCH_BUDGET is not None:
        _DISPATCH_BUDGET -= 1


def dispatch(cfg: dict, env: dict, wf: str, branch: str, message: str,
             item_id: str, number: int) -> bool:
    """Start an Archon workflow run in a detached child process.

    Deliberately does NOT use `archon ... --detach`: the archon-pi build's
    detached-child spawn is broken (it passes the binary path as the command).
    The child is put in its own session so it survives the poller (and
    launchd restarts of it). Output appends to automation/archon-runs.log.
    Returns True when the process spawned.
    """
    if DRY_RUN:
        log(f"[dry-run] DISPATCH wf={wf} branch={branch} issue={number}")
        return True
    if not _dispatch_slot_available(wf, number):
        return False
    log_path = ROOT / "automation" / "archon-runs.log"
    try:
        with open(log_path, "a") as out:
            proc = subprocess.Popen(
                ["archon", "workflow", "run", wf, "--branch", branch, message],
                cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        log(f"DISPATCH FAILED item={item_id} wf={wf}: {exc}")
        return False
    _consume_dispatch_slot()
    log(f"DISPATCHED item={item_id} issue={number} wf={wf} branch={branch} pid={proc.pid}")
    return True


def fetch_verdict(cfg: dict, env: dict, pr_number: int) -> tuple[str | None, bool]:
    """(verdict, ok). ok=False means the lookup failed (gh error or
    unparseable output) — the caller must NOT treat the verdict as a real
    non-approve and must NOT pop retry markers; retry next poll instead."""
    r = gh(["pr", "view", str(pr_number), "-R", cfg["repo"],
            "--json", "comments"], env)
    if r.returncode != 0:
        log(f"VERDICT FETCH FAILED PR #{pr_number}: {r.stderr.strip()[:200]}")
        return None, False
    try:
        data = json.loads(r.stdout)
    except ValueError as exc:
        log(f"VERDICT PARSE FAILED PR #{pr_number}: {exc}")
        return None, False
    return parse_verdict([c.get("body") or "" for c in data.get("comments", [])]), True


def find_ship_pr(cfg: dict, env: dict, issue_number: int, base: str) -> dict | None:
    """Open ship PR (head -> base) linked to an issue; None when absent.

    The ship PR shares its head branch with the issue's develop PR, so this
    resolves the issue's PR first, then looks for an open PR with that head
    targeting `base`.
    """
    pr, pr_ok = find_issue_pr(cfg, env, issue_number)
    if not pr_ok or not pr:
        return None
    r = gh(["pr", "list", "-R", cfg["repo"], "--head", pr.get("headRefName") or "",
            "--state", "open", "--json", "number,baseRefName,mergeable,headRefName"],
           env)
    if r.returncode != 0:
        return None
    for p in json.loads(r.stdout or "[]"):
        if p.get("baseRefName") == base:
            return p
    return None


def try_merge_base_into_head(cfg: dict, env: dict, pr_number: int,
                             head: str, base: str) -> tuple[bool, str]:
    """Merge `base` into the PR head via the GitHub merge API.

    Same operation as the "Update branch" button. Returns (True, note) when
    the merge applied or was a no-op; (False, "conflict") when the branches
    genuinely conflict; (False, note) on transient errors (caller retries
    next poll without escalating).
    """
    if DRY_RUN:
        log(f"[dry-run] SHIP CONFLICT UPDATE PR #{pr_number}: merge {base} -> {head}")
        return True, "dry-run (no merge)"
    r = gh(["api", "-X", "POST", f"repos/{cfg['repo']}/merges",
            "-f", f"base={head}", "-f", f"head={base}"], env)
    if r.returncode == 0:
        return True, "base merged into head"
    err = (r.stderr or "") + (r.stdout or "")
    low = err.lower()
    if "conflict" in low:
        return False, "conflict"
    if "no commits between" in low or "already up to date" in low:
        return True, "no-op (head already contains base)"
    return False, f"transient: {err.strip()[:200]}"


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


def fetch_issue_titles(cfg: dict, env: dict, state: str) -> list[dict] | None:
    """Open/closed issues as [{number, title}] for the dedupe search."""
    r = gh(["issue", "list", "-R", cfg["repo"], "--state", state,
            "--limit", "200", "--json", "number,title"], env)
    if r.returncode != 0:
        log(f"DEFERRED: issue list ({state}) failed: {r.stderr.strip()[:200]}")
        return None
    return json.loads(r.stdout)


def label_exists(cfg: dict, env: dict, label: str) -> bool:
    """True when `label` already exists in the repo (gh fails on unknown
    labels, so never pass one through unchecked)."""
    r = gh(["label", "list", "-R", cfg["repo"], "--json", "name"], env)
    return r.returncode == 0 and any(
        l.get("name") == label for l in json.loads(r.stdout))


def add_to_board(cfg: dict, env: dict, issue_number: int, lane: str,
                 project_id: str, field_id: str,
                 status_options: dict) -> bool:
    """Add a new issue to the project and move it to `lane` (default Backlog)."""
    url = f"https://github.com/{cfg['repo']}/issues/{issue_number}"
    if DRY_RUN:
        log(f"[dry-run] DEFERRED BOARD issue={issue_number} -> {lane}")
        return True
    r = gh(["project", "item-add", str(cfg["project_number"]),
            "--owner", cfg["project_owner"], "--url", url,
            "--format", "json"], env)
    if r.returncode != 0:
        log(f"DEFERRED BOARD ADD FAILED issue={issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return False
    try:
        item_id = json.loads(r.stdout)["itemId"]
    except (ValueError, KeyError):
        m = re.search(r"(PVTI_[A-Za-z0-9_]+)", r.stdout or "")
        if not m:
            log(f"DEFERRED BOARD ADD: unexpected output: {r.stdout[:200]!r}")
            return False
        item_id = m.group(1)
    option_id = status_options.get(lane)
    if not option_id:
        log(f"DEFERRED BOARD: lane '{lane}' not on board")
        return False
    return move_to_lane(cfg, env, project_id, item_id, field_id, option_id)


def create_deferred_issue(cfg: dict, env: dict, issue_number: int,
                          pr_number: int | None, source_title: str,
                          item: dict, lane: str, project_id: str,
                          field_id: str, status_options: dict,
                          supersedes: int | None = None) -> int | None:
    """Create + board the tracking issue for one deferred item.

    Returns the new issue number, 0 in dry-run (simulated), or None on failure.
    """
    title = item["title"]
    body = (
        f"Deferred from #{issue_number} during implementation"
        + (f" (PR #{pr_number})" if pr_number else "") + ".\n\n"
        + f"**What and why:** {item['description'] or 'TBD.'}\n\n"
        + (f"**Reason deferred:** {item['reason']}\n\n" if item["reason"] else "")
        + f"Context: source issue #{issue_number} ({source_title}).\n\n"
        + "## Ownership\n\n"
        + "Files/areas: declare before moving this issue to Todo.\n\n"
        + "## Depends on\n\n"
        + "None.\n\n"
        + "Acceptance criteria to be filled when this is planned."
    )
    if supersedes is not None:
        body += f"\n\nSupersedes: #{supersedes}"
    cmd = ["issue", "create", "-R", cfg["repo"], "--title", title,
           "--body", body]
    label = item.get("label") or ""
    if label and label_exists(cfg, env, label):
        cmd += ["--label", label]
    if DRY_RUN:
        log(f"[dry-run] DEFERRED CREATE issue={issue_number}: {title}"
            + (f" (label {label})" if label else ""))
        return 0
    r = gh(cmd, env)
    if r.returncode != 0:
        log(f"DEFERRED CREATE FAILED issue={issue_number}: "
            f"{r.stderr.strip()[:200]}")
        return None
    m = re.search(r"issues/(\d+)\s*$", (r.stdout or "").strip())
    if not m:
        log(f"DEFERRED CREATE: cannot parse issue number from: {r.stdout[:200]!r}")
        return None
    new_num = int(m.group(1))
    if not add_to_board(cfg, env, new_num, lane, project_id, field_id,
                        status_options):
        log(f"DEFERRED BOARD FAILED issue={new_num}; created but not boarded")
        return None
    return new_num


def record_out_of_scope(cfg: dict, slug: str, item: dict,
                        source_number: int, source_title: str) -> bool:
    """Record a durable rejection in .out-of-scope/<slug>.md (Matt Pocock KB).

    Creates or appends the concept file and commits it (a dirty tree is fine —
    only the KB path is staged). Returns False on failure (logged, non-fatal:
    the skip stands; a future run re-stamping the concept will retry the write).
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", (slug or "").lower()).strip("-")
    if not slug:
        slug = re.sub(r"[^a-z0-9-]+", "-", (item.get("title") or "x").lower())[:40]
    path = ROOT / ".out-of-scope" / f"{slug}.md"
    if DRY_RUN:
        log(f"[dry-run] OUT-OF-SCOPE {path.relative_to(ROOT)}")
        return True
    heading = slug.replace("-", " ").strip().title()
    request_line = f'- #{source_number} — "{source_title}"'
    if path.exists():
        text = path.read_text()
        if request_line in text:
            return True
        if "## Prior requests" in text:
            text = text.replace("## Prior requests",
                                "## Prior requests\n" + request_line, 1)
        else:
            text += f"\n## Prior requests\n\n{request_line}\n"
    else:
        why = item.get("reason") or item.get("description") or ""
        text = (f"# {heading}\n\n{why}\n\n"
                f"## Prior requests\n\n{request_line}\n")
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        subprocess.run(["git", "add", "--", str(path)], capture_output=True,
                       text=True, timeout=60, cwd=str(ROOT))
        r = subprocess.run(["git", "commit", "-m", f"out-of-scope: {slug}",
                            "--", str(path)], capture_output=True, text=True,
                           timeout=60, cwd=str(ROOT))
        if r.returncode != 0:
            log(f"OUT-OF-SCOPE COMMIT FAILED {slug}: {r.stderr.strip()[:200]}")
            return False
    except OSError as exc:
        log(f"OUT-OF-SCOPE WRITE FAILED {slug}: {exc}")
        return False
    return True


def reconcile_deferred_work(cfg: dict, env: dict, issue_number: int,
                            pr_number: int | None, rec: dict, runs_msg: str,
                            project_id: str, field_id: str,
                            status_options: dict) -> bool:
    """Guarantee every deferred item in the run's completion record is tracked.

    Idempotent: skips when `runs_msg` was already handled (state marker) and
    dedupes against existing issue titles before creating. Returns False on
    any failure so the caller retries on the next poll (marker not set); the
    dedupe makes retries safe.
    """
    if rec.get("deferred_handled") == runs_msg:
        return True
    r = gh(["issue", "view", str(issue_number), "-R", cfg["repo"],
            "--json", "title,comments"], env)
    if r.returncode != 0:
        log(f"DEFERRED SKIP issue={issue_number}: cannot read comments")
        return False
    data = json.loads(r.stdout)
    source_title = data.get("title") or ""
    bodies = [c.get("body") or "" for c in data.get("comments") or []]

    items: list[dict] | None = None
    for body in reversed(bodies):  # newest comment WITH a section wins, even empty
        parsed = parse_deferred_work(body)
        if parsed is not None:
            items = parsed
            break
    if items is None:  # no `## Deferred work` section anywhere
        newest = bodies[-1] if bodies else ""
        unmet = find_unchecked_criteria(newest)
        if (cfg.get("deferred_work", {}).get("fallback_warn", True)
                and (has_deferral_language(newest) or unmet)
                and not rec.get("deferred_warned")):
            note = ""
            if unmet:
                note = ("\nAcceptance criteria left unchecked in the completion "
                        "record (likely deferred work):\n"
                        + "\n".join(f"- {c[:160]}" for c in unmet[:8]))
                if len(unmet) > 8:
                    note += f"\n- … and {len(unmet) - 8} more"
            comment_issue(
                cfg, env, issue_number,
                "This run's completion record shows deferred or unfinished work "
                "but has no `## Deferred work` section. If any deferred item "
                "needs an issue, create one (or drag it to Todo)." + note)
            rec["deferred_warned"] = True
        rec["deferred_handled"] = runs_msg
        return True

    open_issues = fetch_issue_titles(cfg, env, "open")
    closed_issues = fetch_issue_titles(cfg, env, "closed")
    if open_issues is None or closed_issues is None:
        return False

    lane = cfg.get("default_lane", "Backlog")
    lines: list[str] = []
    for item in items:
        if item.get("out_of_scope"):
            record_out_of_scope(cfg, item["out_of_scope"], item,
                                issue_number, source_title)
            lines.append(f"- **{item['title']}** \u2014 out of scope, recorded in "
                         f".out-of-scope/{item['out_of_scope'].strip('-')}.md")
            continue
        if item.get("skip"):
            lines.append(f"- **{item['title']}** \u2014 skipped ({item['skip']})")
            continue
        if item.get("links_to") is not None:
            target = item["links_to"]
            if any(i.get("number") == target for i in open_issues):
                lines.append(f"- **{item['title']}** \u2192 already tracked in #{target}")
            else:
                log(f"DEFERRED: '{item['title']}' links to #{target}, which is not "
                    "an open issue; creating fresh")
                created = create_deferred_issue(
                    cfg, env, issue_number, pr_number, source_title, item, lane,
                    project_id, field_id, status_options,
                    supersedes=item.get("supersedes"))
                if created is None:
                    log(f"DEFERRED RETRY issue={issue_number}: create failed for "
                        f"'{item['title']}'")
                    return False
                if created:
                    lines.append(f"- **{item['title']}** \u2192 #{created} (created, {lane})")
            continue
        action, ref = dedupe_deferred(item, open_issues, closed_issues)
        if action == "link":
            lines.append(f"- **{item['title']}** \u2192 already tracked in #{ref}")
            continue
        supersedes = item.get("supersedes")
        if supersedes is None and action == "create-ref":
            supersedes = ref
        created = create_deferred_issue(
            cfg, env, issue_number, pr_number, source_title, item, lane,
            project_id, field_id, status_options, supersedes=supersedes)
        if created is None:
            log(f"DEFERRED RETRY issue={issue_number}: create failed for "
                f"'{item['title']}'")
            return False
        if created == 0:  # dry-run simulated
            continue
        if supersedes is not None:
            lines.append(f"- **{item['title']}** \u2192 #{created} "
                         f"(created; supersedes closed #{supersedes})")
        else:
            lines.append(f"- **{item['title']}** \u2192 #{created} (created, {lane})")
    if lines:
        comment_issue(cfg, env, issue_number,
                      "Deferred work from this run:\n" + "\n".join(lines))
    rec["deferred_handled"] = runs_msg
    return True


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


def poll(cfg: dict, env: dict, state: dict) -> None:
    project_id, field_id, status_options, items = fetch_project(cfg, env)
    prepare_dispatch_budget(cfg, env)
    first_run = not state.get("_meta", {}).get("snapshot_done")

    lane_names = {v: k for k, v in cfg["lanes"].items()}
    done_lane_name = lane_names.get("done")
    todo_lane_name = lane_names.get("todo")
    blocked_lane_name = lane_names.get("blocked")
    ready_lane_name = lane_names.get("ready")

    # Issue number -> lane / open-state on this board, for the dep gate.
    number_lane: dict[int, str] = {}
    number_state: dict[int, str] = {}
    for item in items:
        content = item.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
            continue
        number_lane[content["number"]] = item["status"]
        number_state[content["number"]] = content.get("state") or ""

    sync_runnable_labels(
        cfg,
        env,
        items,
        number_lane,
        number_state,
        todo_lane_name,
        done_lane_name,
    )

    seen: set[str] = set()
    # Items dispatched in THIS poll: their run rows appear in `archon workflow
    # runs` asynchronously, so the completion passes must not read a stale run
    # with the same message (re-dispatches reuse the message) and pop the
    # marker before the new run registers.
    fresh_dispatched: set[str] = set()
    for item in items:
        item_id = item["id"]
        seen.add(item_id)
        content = item.get("content")
        if not content or content["__typename"] not in ("Issue", "PullRequest"):
            continue
        repo = content["repository"]["nameWithOwner"]
        if repo != cfg["repo"]:
            continue
        status_val = item["status"]
        # Convention: items on the board without a status land in the default lane.
        default_lane = cfg.get("default_lane", "Backlog")
        if status_val == "No status":
            option_id = status_options.get(default_lane)
            if option_id and move_to_lane(
                    cfg, env, project_id, item_id, field_id, option_id):
                status_val = default_lane
                log(f"NORMALIZED item={item_id} -> {default_lane}")
        rec = state.get(item_id, {})
        prev = rec.get("status")
        dispatched_msg = None
        review_msg = None
        ship_pr_num = None
        dispatched_wf = None
        dispatched_branch = None
        dep_gate_ran = False
        dep_blocked_marker = None
        dep_cancelled_noted = None

        if not first_run and prev != status_val:
            lane = cfg["lanes"].get(status_val)
            if lane == "todo" and content["__typename"] == "Issue":
                # Dependency gate: an issue whose Depends-on refs are not all
                # in the Done lane does not dispatch; it moves to the Blocked
                # lane and the unblock pass returns it to Todo (which
                # dispatches) when the deps ship. Needs Input stays exclusive
                # to NEEDS INPUT questions.
                deps = parse_dep_refs(content.get("body") or "")
                if deps and done_lane_name:
                    dep_gate_ran = True
                    unsatisfied, cancelled = dep_gate(
                        deps, number_lane, number_state, done_lane_name)
                    if unsatisfied or cancelled:
                        noted = set(rec.get("dep_cancelled_noted") or [])
                        fresh = [n for n in cancelled if n not in noted]
                        for n in fresh:
                            comment_issue(
                                cfg, env, content["number"],
                                f"#{n} closed without shipping, but this issue depends on it. "
                                "Re-scope, rewire the Depends on line, or close this issue.")
                            noted.add(n)
                        if not rec.get("dep_blocked"):
                            comment_issue(
                                cfg, env, content["number"],
                                f"Moved to Blocked: depends on {fmt_deps(unsatisfied)} "
                                "— not in the Done lane. Will move to Todo automatically "
                                "when they ship. If a dependency is abandoned, re-scope or "
                                "close this issue.")
                        if blocked_lane_name:
                            option_id = status_options.get(blocked_lane_name)
                            if option_id and move_to_lane(
                                    cfg, env, project_id, item_id, field_id, option_id):
                                log(f"DEP-BLOCKED item={item_id} "
                                    f"issue={content['number']} -> {blocked_lane_name}")
                                status_val = blocked_lane_name
                        dep_blocked_marker = deps
                        dep_cancelled_noted = sorted(noted)
                    else:
                        dep_blocked_marker = None
                if not dep_gate_ran or dep_blocked_marker is None:
                    labels = [n["name"] for n in content["labels"]["nodes"]]
                    wf = pick_workflow(cfg, labels)
                    branch = f"issue-{content['number']}"
                    ok = False
                    resumed_branch = None
                    if "needs-input" in labels and rec.get("branch") and rec.get("wf"):
                        ok, msg, resumed_branch = resume_issue(
                            cfg, env, rec["branch"], rec["wf"], content["number"])
                        if ok:
                            wf = rec["wf"]
                    if not ok:
                        msg = (
                            f"Implement GitHub issue #{content['number']}: {content['title']} "
                            f"({repo}). Full issue: {content['url']}"
                        )
                        if wf == "archon-idea-to-pr":
                            msg = (
                                f"Build feature from issue #{content['number']}: {content['title']} "
                                f"({repo}). Full issue: {content['url']}"
                            )
                        ok = dispatch(cfg, env, wf, branch, msg,
                                      item_id, content["number"])
                    if ok:
                        dispatched_msg = msg
                        dispatched_wf = wf
                        dispatched_branch = resumed_branch or branch
                        fresh_dispatched.add(item_id)
                    target = cfg["dispatch"]["todo"].get("move_to")
                    if ok and target:
                        option_id = status_options.get(target)
                        if option_id and move_to_lane(
                                cfg, env, project_id, item_id, field_id, option_id):
                            log(f"MOVED item={item_id} issue={content['number']} -> {target}")
                        elif option_id is None:
                            log(f"MOVE SKIPPED item={item_id}: lane '{target}' not on board")
                        else:
                            log(f"MOVE FAILED item={item_id} -> {target}")
            elif lane == "review":
                if content["__typename"] == "PullRequest":
                    pr_number = content["number"]
                    msg = f"Review PR #{pr_number} ({content['title']})"
                    branch = f"review/pr-{pr_number}"
                    dispatch(cfg, env, cfg["dispatch"]["review"]["workflow"], branch,
                             msg, item_id, content["number"])
                else:
                    # Ensure the feature is in develop, then review the ship PR
                    # (feature -> main); on an approving review the poller merges
                    # it. The helper also closes issues whose work already
                    # reached main (empty ship PR) and logs failures so the
                    # recheck pass can retry.
                    rec = state.get(item_id, {})
                    result = ensure_ship_review(
                        cfg, env, item_id, content["number"], content["title"],
                        project_id, field_id, status_options, done_lane_name, rec)
                    if isinstance(result, tuple):
                        review_msg = result[1]
                        ship_pr_num = result[2]
                        fresh_dispatched.add(item_id)
                    elif result == "shipped":
                        if done_lane_name:
                            status_val = done_lane_name

        rec = state.get(item_id, {})
        rec["status"] = status_val
        if prev != status_val and status_val != ready_lane_name:
            rec.pop("ready_test_comment", None)
        if dispatched_msg:
            rec.pop("ready_test_comment", None)
            rec.pop("develop_pr", None)
            rec["dispatch_msg"] = dispatched_msg
            rec["issue_number"] = content["number"]
            rec["wf"] = dispatched_wf
            rec["branch"] = dispatched_branch
        if review_msg:
            rec["review_msg"] = review_msg
            rec["ship_pr"] = ship_pr_num
            rec["issue_number"] = content["number"]
        if dep_gate_ran:
            if dep_blocked_marker:
                rec["dep_blocked"] = dep_blocked_marker
            else:
                rec.pop("dep_blocked", None)
            if dep_cancelled_noted:
                rec["dep_cancelled_noted"] = dep_cancelled_noted
            else:
                rec.pop("dep_cancelled_noted", None)
        state[item_id] = rec

    # Review-lane recheck: an issue sitting in In Review with no review run
    # on record (a transition consumed by a transient failure, or a ship-PR
    # creation that failed silently) retries the ship flow every poll.
    # review_held marks verdict-holds and failed runs so they do NOT
    # auto-redispatch — the human re-drags those after fixing findings.
    review_lane_name = next(
        (k for k, v in cfg["lanes"].items() if v == "review"), None)
    if review_lane_name:
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != review_lane_name:
                continue
            item_id = item["id"]
            if item_id in fresh_dispatched:
                continue
            rec = state.get(item_id, {})
            if rec.get("review_msg") or rec.get("review_held"):
                continue
            number = content["number"]
            result = ensure_ship_review(
                cfg, env, item_id, number, content["title"],
                project_id, field_id, status_options, done_lane_name, rec)
            if isinstance(result, tuple):
                rec["review_msg"] = result[1]
                rec["ship_pr"] = result[2]
                fresh_dispatched.add(item_id)
            state[item_id] = rec

    # Dep-unblock pass: dep-marked items in the Blocked lane re-check every
    # poll, so a shipped dependency releases them (moves them to Todo, which
    # dispatches on the next poll) without a manual drag.
    if todo_lane_name and blocked_lane_name and done_lane_name:
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != blocked_lane_name:
                continue
            item_id = item["id"]
            rec = state.get(item_id, {})
            if not rec.get("dep_blocked"):
                continue
            # The current body is authoritative: clearing the Depends-on line
            # (or rewiring it) must release or re-block the item immediately.
            deps = parse_dep_refs(content.get("body") or "")
            unsatisfied, cancelled = dep_gate(
                deps, number_lane, number_state, done_lane_name)
            if unsatisfied:
                noted = set(rec.get("dep_cancelled_noted") or [])
                fresh = [n for n in cancelled if n not in noted]
                for n in fresh:
                    comment_issue(
                        cfg, env, content["number"],
                        f"#{n} closed without shipping, but this issue depends on it. "
                        "Re-scope, rewire the Depends on line, or close this issue.")
                    noted.add(n)
                if fresh:
                    rec["dep_cancelled_noted"] = sorted(noted)
                    state[item_id] = rec
                continue
            option_id = status_options.get(todo_lane_name)
            if option_id and move_to_lane(
                    cfg, env, project_id, item_id, field_id, option_id):
                if deps:
                    comment_issue(
                        cfg, env, content["number"],
                        f"Dependencies satisfied ({fmt_deps(deps)} in Done). Moved to Todo.")
                rec.pop("dep_blocked", None)
                rec.pop("dep_cancelled_noted", None)
                state[item_id] = rec
                log(f"DEP-UNBLOCKED item={item_id} issue={content['number']} -> {todo_lane_name}")

    # Completion reconciliation: when a dispatched run finishes, merge the
    # feature PR into develop and move the item to Ready for Review.
    complete_move_to = cfg["dispatch"]["todo"].get("complete_move_to")
    in_progress_name = next(
        (k for k, v in cfg["lanes"].items() if v == "in_progress"), None)
    runs_by_msg = None
    if complete_move_to and in_progress_name:
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or item_id in fresh_dispatched:
                continue
            msg = rec.get("dispatch_msg")
            if not msg or rec.get("status") != in_progress_name:
                continue
            if runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            run_status = run_status_for(runs_by_msg, msg)
            if run_status == "completed":
                issue_number = rec.get("issue_number")
                needs_input_name = next(
                    (k for k, v in cfg["lanes"].items() if v == "needs_input"), None)
                if issue_number and needs_input_name:
                    label_state = issue_has_label(
                        cfg, env, issue_number, "needs-input")
                    if label_state is None:
                        # gh failure — must not be misread as "label absent":
                        # the run may be awaiting human input. Keep the marker
                        # and retry next poll (never advance on uncertainty).
                        log(f"LABEL CHECK UNREADABLE issue={issue_number}; holding "
                            "completion until the needs-input state is known")
                        continue
                    if label_state:
                        option_id = status_options.get(needs_input_name)
                        if option_id and move_to_lane(
                                cfg, env, project_id, item_id, field_id, option_id):
                            log(f"NEEDS INPUT item={item_id} issue={issue_number} -> "
                                f"{needs_input_name} (awaiting human input)")
                        rec.pop("dispatch_msg", None)
                        continue
                merge_base = cfg["dispatch"]["todo"].get("merge_develop_base")
                merge_ok = True
                pr_num = None
                if issue_number and merge_base:
                    pr, pr_ok = find_issue_pr(cfg, env, issue_number, base=merge_base)
                    if not pr_ok:
                        # gh lookup failed — cannot positively confirm the merge
                        # state; leave the item In Progress with its marker and
                        # retry next poll (never advance without confirmation).
                        log(f"DEVELOP MERGE DEFERRED issue={issue_number}: PR lookup "
                            "failed (gh error); retrying next poll")
                        continue
                    if pr:
                        pr_num = pr.get("number")
                        merge_ok, note = merge_pr_to_base(cfg, env, pr, merge_base,
                                                          issue_number)
                        log(f"DEVELOP MERGE issue={issue_number} PR=#{pr['number']}: {note}"
                            if merge_ok else
                            f"DEVELOP MERGE FAILED issue={issue_number}: {note}")
                    else:
                        log(f"no PR found for issue #{issue_number}; skipping develop merge")
                if not merge_ok:
                    # Develop-merge conflict gate: mirror the ship-lane state
                    # machine. Episode markers: dev_conflict_mech /
                    # dev_conflict_fix_msg / dev_conflict_noted. The outer
                    # merge retries every poll; this block only escalates.
                    dev_wf = cfg["dispatch"]["todo"].get("conflict_fix_workflow")
                    head = (pr or {}).get("headRefName")
                    if (issue_number and pr_num and head and dev_wf
                            and runs_by_msg is not None):
                        fix_msg = rec.get("dev_conflict_fix_msg")
                        act = develop_conflict_action(
                            bool(rec.get("dev_conflict_mech")), fix_msg,
                            (run_status_for(runs_by_msg, fix_msg)
                             if fix_msg else None))
                        if act == "mech":
                            ok, note = try_merge_base_into_head(
                                cfg, env, pr_num, head, merge_base)
                            log(f"DEVELOP CONFLICT MECHANICAL PR #{pr_num} "
                                f"issue={issue_number}: {note}")
                            if not ok and note == "conflict":
                                rec["dev_conflict_mech"] = True
                        elif act == "dispatch":
                            dmsg = (f"Resolve merge conflicts on develop PR #{pr_num} "
                                    f"(issue #{issue_number}).")
                            if dispatch(cfg, env, dev_wf,
                                        f"fix-develop-issue-{issue_number}", dmsg,
                                        item_id, issue_number):
                                rec["dev_conflict_fix_msg"] = dmsg
                                rec["dev_conflict_noted"] = False
                                comment_issue(
                                    cfg, env, issue_number,
                                    f"Develop PR #{pr_num} has merge conflicts. "
                                    f"Resolving automatically (merging develop into "
                                    f"the branch, then {dev_wf} if needed).")
                                log(f"DEVELOP CONFLICT DISPATCH PR #{pr_num} "
                                    f"issue={issue_number} wf={dev_wf}")
                        elif act == "failed":
                            if not rec.get("dev_conflict_noted"):
                                comment_issue(
                                    cfg, env, issue_number,
                                    f"Could not resolve the merge conflicts on develop "
                                    f"PR #{pr_num} automatically — the fix run finished "
                                    "but the PR is still conflicting. Merge develop into "
                                    "the branch manually (or rewrite the conflicting "
                                    "lines); the poller will merge automatically once the "
                                    "branch is mergeable.")
                                rec["dev_conflict_noted"] = True
                            log(f"DEVELOP CONFLICT UNRESOLVED PR #{pr_num} "
                                f"issue={issue_number} — needs human")
                        # "active" falls through: keep the markers, retry next poll.
                        state[item_id] = rec
                        log(f"left item={item_id} in {in_progress_name} "
                            "(develop merge conflict episode active)")
                        continue
                    log(f"left item={item_id} in {in_progress_name} (develop merge failed)")
                    rec.pop("dispatch_msg", None)
                    continue
                if merge_ok:
                    for m in ("dev_conflict_mech", "dev_conflict_fix_msg",
                              "dev_conflict_noted"):
                        rec.pop(m, None)
                    log(sync_local_develop())
                # Deferred-work guard: anything deferred by this run must be
                # tracked as an issue before the item moves to Ready for Review.
                dw_cfg = cfg.get("deferred_work") or {}
                if (issue_number and dw_cfg.get("enabled", True)
                        and not reconcile_deferred_work(
                            cfg, env, issue_number, pr_num, rec, msg,
                            project_id, field_id, status_options)):
                    log(f"DEFERRED RETRY item={item_id} issue={issue_number} "
                        "(guard incomplete; will retry next poll)")
                    continue
                option_id = status_options.get(complete_move_to)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    rec.pop("dispatch_msg", None)
                    if complete_move_to == ready_lane_name and issue_number:
                        if pr_num is not None:
                            rec["develop_pr"] = pr_num
                        posted = post_ready_for_review_comment(
                            cfg, env, issue_number, merge_base or "develop", pr_num)
                        if posted:
                            rec["ready_test_comment"] = True
                            log(f"READY TEST COMMENTED issue={issue_number}")
                        else:
                            rec.pop("ready_test_comment", None)
                            log(f"READY TEST COMMENT DEFERRED issue={issue_number}")
                    log(f"MOVED item={item_id} -> {complete_move_to} (run completed)")
                elif option_id is None:
                    log(f"MOVE SKIPPED item={item_id}: lane '{complete_move_to}' not on board")
                else:
                    log(f"MOVE FAILED item={item_id} -> {complete_move_to}")
            elif run_status in ("failed", "cancelled"):
                log(f"RUN {run_status.upper()} item={item_id}; left in {in_progress_name}")
                rec.pop("dispatch_msg", None)

    # Ready-lane recheck: a comment failure must not strand the issue without
    # its handoff. This also backfills issues that reached Ready before this
    # behavior was deployed.
    if ready_lane_name and not first_run:
        ready_base = cfg["dispatch"]["todo"].get("merge_develop_base", "develop")
        for item in items:
            content = item.get("content") or {}
            if content.get("__typename") != "Issue":
                continue
            if content.get("repository", {}).get("nameWithOwner") != cfg["repo"]:
                continue
            if item["status"] != ready_lane_name:
                continue
            item_id = item["id"]
            rec = state.get(item_id, {})
            if rec.get("ready_test_comment"):
                continue
            issue_number = content["number"]
            pr_number = rec.get("develop_pr")
            if pr_number is None:
                pr, _ = find_issue_pr(cfg, env, issue_number, base=ready_base)
                if pr:
                    pr_number = pr.get("number")
            if post_ready_for_review_comment(
                    cfg, env, issue_number, ready_base, pr_number):
                rec["issue_number"] = issue_number
                if pr_number is not None:
                    rec["develop_pr"] = pr_number
                rec["ready_test_comment"] = True
                state[item_id] = rec
                log(f"READY TEST COMMENTED issue={issue_number} (recheck)")

    # Review completion: merge the ship PR to its base (main) and move the
    # item to Done ONLY when the review run finished AND its verdict approves.
    ship_to = cfg["dispatch"]["review"].get("ship_to", "main")
    review_lane_name = next(
        (k for k, v in cfg["lanes"].items() if v == "review"), None)
    done_name = cfg["dispatch"]["review"].get("done_lane", "Done")
    if (review_lane_name and done_name
            and cfg["dispatch"]["review"].get("merge_ship_on_approve")):
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or item_id in fresh_dispatched:
                continue
            rmsg = rec.get("review_msg")
            if not rmsg or rec.get("status") != review_lane_name:
                continue
            if runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            rstatus = run_status_for(runs_by_msg, rmsg)
            if rstatus == "completed":
                ship_num = rec.get("ship_pr")
                ship = None
                if ship_num:
                    r = gh(["pr", "view", str(ship_num), "-R", cfg["repo"],
                            "--json", "number,state,baseRefName"], env)
                    if r.returncode == 0:
                        ship = json.loads(r.stdout)
                if ship and ship.get("state") != "MERGED":
                    verdict, verdict_ok = fetch_verdict(cfg, env, ship_num)
                    if not verdict_ok:
                        # Lookup failure, not a review outcome: do NOT accuse the
                        # reviewer and do NOT pop the retry markers.
                        log(f"SHIP VERDICT UNREADABLE PR #{ship_num}; retrying next poll")
                        continue
                    if verdict != "approve":
                        log(f"SHIP HELD PR #{ship_num}: verdict={verdict or 'none'} — not merging")
                        issue_number = rec.get("issue_number")
                        if issue_number and not comment_issue(
                                cfg, env, issue_number,
                                f"Ship review did not approve (VERDICT: {verdict or 'none'}). "
                                "Fix the findings, then drag the issue back to In Review to re-review."):
                            log(f"SHIP HELD NOTICE FAILED issue={issue_number}; "
                                "keeping markers for retry")
                            continue
                        # Held verdicts do NOT auto-redispatch (the recheck pass
                        # skips review_held items): the human re-drags after
                        # fixing findings, which clears the marker on dispatch.
                        rec["review_held"] = True
                        rec.pop("review_msg", None)
                        rec.pop("ship_pr", None)
                        continue
                    if DRY_RUN:
                        log(f"[dry-run] MERGE PR #{ship_num} -> {ship_to}")
                    else:
                        rr = gh(["pr", "merge", str(ship_num), "-R", cfg["repo"],
                                 "--merge"], env)
                        if rr.returncode == 0:
                            log(f"SHIPPED PR #{ship_num} -> {ship_to} (approved)")
                            issue_number = rec.get("issue_number")
                            if issue_number:
                                gh(["issue", "close", str(issue_number), "-R", cfg["repo"]],
                                   env)
                                log(f"CLOSED issue #{issue_number} (shipped)")
                        else:
                            log(f"SHIP MERGE FAILED PR #{ship_num}: {rr.stderr.strip()[:300]}")
                            continue
                elif not ship:
                    log(f"SHIP PR #{ship_num} not found for item={item_id}")
                    continue
                option_id = status_options.get(done_name)
                if option_id and move_to_lane(
                        cfg, env, project_id, item_id, field_id, option_id):
                    log(f"MOVED item={item_id} -> {done_name} (shipped)")
                rec.pop("review_msg", None)
                rec.pop("ship_pr", None)
            elif rstatus in ("failed", "cancelled"):
                log(f"REVIEW {rstatus.upper()} item={item_id}; left in {review_lane_name}")
                # Do not auto-redispatch failed runs every poll (the recheck
                # pass skips review_held items) — the human re-drags to retry.
                rec["review_held"] = True
                rec.pop("review_msg", None)

    # Ship-conflict remediation: a CONFLICTING ship PR blocks the verdict-gated
    # merge. Try a mechanical base-into-head merge first (free, instant); on
    # real conflicts dispatch the dedicated fix workflow once per episode
    # (markers: conflict_fix_msg / conflict_mech_failed / conflict_fix_noted).
    # Episodes end when the PR is mergeable; a fix run that finishes with the
    # PR still conflicting posts a human-help comment once.
    fix_wf = cfg["dispatch"]["review"].get("conflict_fix_workflow")
    if review_lane_name and fix_wf:
        for item_id, rec in list(state.items()):
            if item_id == "_meta" or rec.get("status") != review_lane_name:
                continue
            issue_number = rec.get("issue_number")
            if not issue_number:
                continue
            ship = find_ship_pr(cfg, env, issue_number, ship_to)
            if not ship:
                continue
            ship_num = ship.get("number")
            mergeable = ship.get("mergeable") or "UNKNOWN"
            # Never remediate while a review run is active — its sync/fix
            # nodes also write to the branch; concurrent writers race.
            rmsg = rec.get("review_msg")
            if rmsg and runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            review_active = bool(
                rmsg and runs_by_msg
                and run_status_for(runs_by_msg, rmsg)
                in ACTIVE_WORKFLOW_STATUSES)
            if review_active:
                continue
            fix_msg = rec.get("conflict_fix_msg")
            mech_failed = bool(rec.get("conflict_mech_failed"))
            if fix_msg and runs_by_msg is None:
                runs_by_msg = fetch_runs_by_message(env)
            fix_status = (run_status_for(runs_by_msg, fix_msg)
                          if fix_msg and runs_by_msg else None)
            action = conflict_episode_action(mergeable, fix_msg, fix_status, mech_failed)
            if action == "update":
                ok, note = try_merge_base_into_head(
                    cfg, env, ship_num, ship.get("headRefName") or "", ship_to)
                log(f"SHIP CONFLICT UPDATE PR #{ship_num}: {note}")
                if not ok and note == "conflict":
                    rec["conflict_mech_failed"] = True
                    action = "dispatch"
                elif not ok:
                    action = "wait"  # transient error — retry next poll
            if action == "dispatch":
                msg = (f"Resolve merge conflicts on ship PR #{ship_num} "
                       f"(issue #{issue_number}).")
                branch = f"fix-ship-issue-{issue_number}"
                if dispatch(cfg, env, fix_wf, branch, msg, item_id, issue_number):
                    rec["conflict_fix_msg"] = msg
                    rec["conflict_fix_noted"] = False
                    comment_issue(
                        cfg, env, issue_number,
                        f"Ship PR #{ship_num} has merge conflicts. Resolving automatically "
                        f"(merging main into the branch, then {fix_wf} if needed).")
                    log(f"SHIP CONFLICT DISPATCH PR #{ship_num} issue={issue_number} "
                        f"wf={fix_wf}")
            elif action == "failed":
                if not rec.get("conflict_fix_noted"):
                    if comment_issue(
                            cfg, env, issue_number,
                            f"Could not resolve the merge conflicts on ship PR #{ship_num} "
                            "automatically — the fix run finished but the PR is still "
                            "conflicting. Merge main into the branch manually (or rewrite "
                            "the conflicting lines), then drag the issue back to In Review."):
                        rec["conflict_fix_noted"] = True
                    else:
                        log(f"SHIP CONFLICT HELP NOTICE FAILED issue={issue_number}; "
                            "will retry next poll")
                log(f"SHIP CONFLICT UNRESOLVED PR #{ship_num} issue={issue_number} "
                    "— needs human")
            elif action == "clear":
                log(f"SHIP CONFLICT RESOLVED PR #{ship_num} issue={issue_number}")
                rec.pop("conflict_fix_msg", None)
                rec.pop("conflict_mech_failed", None)
                rec.pop("conflict_fix_noted", None)
            state[item_id] = rec

    # Prune items that left the board.
    for item_id in list(state):
        if item_id != "_meta" and item_id not in seen:
            del state[item_id]

    if first_run:
        state["_meta"] = {"snapshot_done": True, "project_id": project_id,
                          "snapshot_at": datetime.now(timezone.utc).isoformat()}
        log(f"snapshot taken: {len(seen)} items on board, dispatch armed")
    save_state(cfg, state)


def fetch_runs_by_message(env: dict) -> dict[str, str]:
    """Map exact run user_message -> status of the NEWEST run with it.

    Re-dispatches reuse the same message for the same issue, so multiple runs
    can share it; the newest run's status is the one that counts. Callers do
    substring lookup because `archon continue` prepends a "Prior Context"
    preamble to the message.
    """
    r = subprocess.run(["archon", "workflow", "runs", "--json"],
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(ROOT))
    if r.returncode != 0:
        return {}
    data = json.loads(r.stdout)
    runs = data.get("runs") if isinstance(data, dict) else data
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


def save_state(cfg: dict, state: dict) -> None:
    if DRY_RUN:
        log("[dry-run] state not saved")
        return
    path = ROOT / cfg["state_file"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def main() -> int:
    cfg = load_config()
    env = os.environ.copy()
    token = gh(["auth", "token"], env)
    if token.returncode == 0 and token.stdout.strip():
        env["GH_TOKEN"] = token.stdout.strip()

    state_path = ROOT / cfg["state_file"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    if not DRY_RUN:
        log(sync_local_develop())

    once = "--once" in sys.argv or DRY_RUN
    consecutive_failures = 0
    while True:
        try:
            poll(cfg, env, state)
            consecutive_failures = 0
        except Exception:  # keep the loop alive on transient failures
            consecutive_failures += 1
            log(f"poll error (attempt {consecutive_failures}):\n"
                + traceback.format_exc().rstrip())
            if consecutive_failures > 3:
                log("POLLER STUCK: repeated poll failures — check gh/archon auth "
                    "and board config")
        if once:
            return 0
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    sys.exit(main())

