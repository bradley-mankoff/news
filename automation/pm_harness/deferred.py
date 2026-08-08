"""Deferred-work reconciliation and durable rejection records."""

from __future__ import annotations

import json
import re
import subprocess

from .github import (
    comment_issue,
    create_deferred_issue,
    fetch_issue_titles,
)
from .policy import (
    dedupe_deferred,
    find_unchecked_criteria,
    has_deferral_language,
    parse_deferred_work,
)
from .runtime import DRY_RUN, ROOT, gh, log

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
                    project_id, field_id, status_options)
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
        created = create_deferred_issue(
            cfg, env, issue_number, pr_number, source_title, item, lane,
            project_id, field_id, status_options)
        if created is None:
            log(f"DEFERRED RETRY issue={issue_number}: create failed for "
                f"'{item['title']}'")
            return False
        if created == 0:  # dry-run simulated
            continue
        if action == "create-ref":
            lines.append(f"- **{item['title']}** \u2192 #{created} "
                         f"(created; supersedes closed #{ref})")
        else:
            lines.append(f"- **{item['title']}** \u2192 #{created} (created, {lane})")
    if lines:
        comment_issue(cfg, env, issue_number,
                      "Deferred work from this run:\n" + "\n".join(lines))
    rec["deferred_handled"] = runs_msg
    return True
