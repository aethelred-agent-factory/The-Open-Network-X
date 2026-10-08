#!/usr/bin/env python3
"""Keep GitHub milestones in step with MILESTONES.md and file PRs under them.

MILESTONES.md is the source of truth. Every row of its "At a glance" table
whose first cell is an `M<n>` id becomes a GitHub milestone titled
"M<n> · <name>". Rows whose status says Done are closed; the rest stay open.
Edit the table, not the milestone: the next sync overwrites hand edits to
the title, description, or open/closed state.

A pull request is filed under a milestone when it is tagged in any of these
ways (HTML comments and fenced code blocks in the description are ignored):

    title         contains [M4]
    description   has a line "Milestone: M4" (bold markup allowed)
    label         "M4" or "milestone: M4"

A PR that carries no tag is left alone. A PR tagged with two different
milestones is left alone with a warning, since GitHub allows only one.

Usage:
    scripts/milestones.py table [--json]
    scripts/milestones.py sync [--backfill] [--dry-run]
    scripts/milestones.py assign [--event PATH] [--dry-run]

`sync` and `assign` call the GitHub REST API with GITHUB_TOKEN for the repo
in GITHUB_REPOSITORY (both set automatically in GitHub Actions). `--backfill`
also files the merged PRs that each milestone's MILESTONES.md heading lists
("PRs #4–#11") and that have no milestone yet. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MILESTONES_MD = ROOT / "MILESTONES.md"
TABLE_HEADING = "## At a glance"
WORKFLOW = ".github/workflows/milestones.yml"

CODE_RE = re.compile(r"^M(\d+)$", re.I)
TITLE_CODE_RE = re.compile(r"^(M\d+)\b")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
PR_RANGE_RE = re.compile(r"#(\d+)(?:\s*[–-]\s*#?(\d+))?")
TITLE_TAG_RE = re.compile(r"\[\s*(M\d+)\s*\]", re.I)
BODY_TAG_RE = re.compile(r"^milestone\s*:\s*\[?\s*(M\d+)\b", re.I)
LABEL_TAG_RE = re.compile(r"^(?:milestone\s*[:/]\s*)?(M\d+)$", re.I)


@dataclass(frozen=True)
class Milestone:
    code: str
    name: str
    version: str
    status: str
    done: bool
    anchor: str
    prs: tuple[int, ...]

    @property
    def title(self) -> str:
        return f"{self.code} · {self.name}"

    def description(self, repo_url: str) -> str:
        link = f"{repo_url}/blob/main/MILESTONES.md"
        if self.anchor:
            link += f"#{self.anchor}"
        return (
            f"Target version: {self.version}. Status: {self.status}.\n\n"
            f"Exit criteria and evidence: {link}\n\n"
            f"Managed by {WORKFLOW} from MILESTONES.md; edit the file, not this milestone."
        )

    def state(self) -> str:
        return "closed" if self.done else "open"


# --- MILESTONES.md parsing ------------------------------------------------


def plain(cell: str) -> str:
    """Markdown cell text without links, bold markers, or backticks."""
    cell = LINK_RE.sub(r"\1", cell)
    return re.sub(r"\s+", " ", cell.replace("**", "").replace("`", "")).strip()


def normalize_code(raw: str) -> str | None:
    m = CODE_RE.match(raw.strip())
    return f"M{int(m[1])}" if m else None


def github_anchor(heading: str) -> str:
    """The slug GitHub gives a Markdown heading (without duplicate suffix)."""
    text = LINK_RE.sub(r"\1", heading).lower()
    kept = "".join(c for c in text if c.isalnum() or c in " -_")
    return kept.replace(" ", "-")


def headings(text: str) -> list[tuple[str, str]]:
    """(heading text, anchor) for every heading outside code fences."""
    out: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if not m:
            continue
        slug = github_anchor(m[2])
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.append((m[2], slug if n == 0 else f"{slug}-{n}"))
    return out


def pr_numbers(heading: str) -> tuple[int, ...]:
    """PR numbers a heading lists, e.g. "(PRs #4–#11, ...)" -> 4..11."""
    if not re.search(r"\bPRs?\b", heading):
        return ()
    found: list[int] = []
    for m in PR_RANGE_RE.finditer(heading):
        lo = int(m[1])
        hi = int(m[2]) if m[2] else lo
        if hi < lo:
            raise ValueError(f"backwards PR range #{lo}–#{hi} in heading {heading!r}")
        found.extend(range(lo, hi + 1))
    return tuple(dict.fromkeys(found))


def parse_milestones(text: str) -> list[Milestone]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == TABLE_HEADING)
    except StopIteration:
        raise ValueError(f"MILESTONES.md has no {TABLE_HEADING!r} section") from None

    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.lstrip().startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    if len(rows) < 3:
        raise ValueError("the At a glance table has no data rows")

    all_headings = headings(text)
    milestones: list[Milestone] = []
    seen: set[str] = set()
    for cells in rows[2:]:  # skip the header row and the |---| separator
        if len(cells) < 4:
            raise ValueError(f"table row has {len(cells)} cells, expected 4: {cells}")
        code = normalize_code(plain(cells[0]))
        if code is None:
            continue  # e.g. the parked "—" row
        if code in seen:
            raise ValueError(f"{code} appears twice in the At a glance table")
        seen.add(code)
        anchor, prs = "", ()
        own = re.compile(rf"^{code}\b")
        mention = re.compile(rf"\b{code}\b")
        match = next((h for h in all_headings if own.match(plain(h[0]))), None) or next(
            (h for h in all_headings if mention.search(h[0])), None
        )
        if match:
            anchor = match[1]
            prs = pr_numbers(match[0])
        status = plain(cells[3])
        milestones.append(
            Milestone(
                code=code,
                name=plain(cells[1]),
                version=plain(cells[2]),
                status=status,
                done=bool(re.search(r"\bdone\b", status, re.I)),
                anchor=anchor,
                prs=prs,
            )
        )
    if not milestones:
        raise ValueError("the At a glance table has no M<n> rows")
    return milestones


# --- PR tags --------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Drop <!-- ... --> comments. An unterminated one hides the rest, as in
    rendered Markdown. A linear scan, not a regex: PR descriptions are
    untrusted, and a lazy regex goes quadratic on many unclosed openers."""
    out: list[str] = []
    i = 0
    while (start := text.find("<!--", i)) != -1:
        out.append(text[i:start])
        end = text.find("-->", start + 4)
        if end == -1:
            return "".join(out)
        i = end + 3
    out.append(text[i:])
    return "".join(out)


def body_lines(body: str) -> list[str]:
    """Description lines outside HTML comments and fenced code blocks."""
    lines: list[str] = []
    fence = ""
    for line in strip_comments(body).splitlines():
        marker = line.lstrip()[:3]
        if fence:
            if marker == fence:
                fence = ""
        elif marker in ("```", "~~~"):
            fence = marker
        else:
            lines.append(line)
    return lines


def tags_in(title: str, body: str, labels: list[str]) -> set[str]:
    """Milestone codes a PR is explicitly tagged with."""
    codes: set[str] = set()
    for m in TITLE_TAG_RE.finditer(title or ""):
        codes.add(normalize_code(m[1]) or "")
    for line in body_lines(body or ""):
        s = line.replace("*", "").replace("_", "").strip().lstrip("->").strip()
        m = BODY_TAG_RE.match(s)
        if m:
            codes.add(normalize_code(m[1]) or "")
    for label in labels:
        m = LABEL_TAG_RE.match(label.strip())
        if m:
            codes.add(normalize_code(m[1]) or "")
    codes.discard("")
    return codes


# --- GitHub API -----------------------------------------------------------


class GitHub:
    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com"):
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.api_url}/repos/{self.repo}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "onx-milestones",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            if err.code == 404 and method == "GET":
                return None
            detail = err.read().decode(errors="replace")[:500]
            raise SystemExit(f"error: {method} {path} -> HTTP {err.code}: {detail}") from None
        return json.loads(raw) if raw else None

    def list_milestones(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = self.request("GET", f"/milestones?state=all&per_page=100&page={page}") or []
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def create_milestone(self, fields: dict) -> dict:
        return self.request("POST", "/milestones", fields)

    def update_milestone(self, number: int, fields: dict) -> dict:
        return self.request("PATCH", f"/milestones/{number}", fields)

    def get_pull(self, number: int) -> dict | None:
        return self.request("GET", f"/pulls/{number}")

    def set_milestone(self, issue_number: int, milestone_number: int) -> None:
        # PRs are issues: the issues endpoint is how a PR's milestone is set.
        self.request("PATCH", f"/issues/{issue_number}", {"milestone": milestone_number})


class DryRun(GitHub):
    """Reads for real, prints writes instead of making them."""

    def create_milestone(self, fields: dict) -> dict:
        print(f"[dry-run] create milestone {fields['title']!r} ({fields['state']})")
        return {"number": 0, **fields}

    def update_milestone(self, number: int, fields: dict) -> dict:
        print(f"[dry-run] update milestone #{number}: {sorted(fields)}")
        return {"number": number, **fields}

    def set_milestone(self, issue_number: int, milestone_number: int) -> None:
        print(f"[dry-run] file #{issue_number} under milestone #{milestone_number}")


# --- Actions output -------------------------------------------------------


def log(level: str, msg: str) -> None:
    """A GitHub Actions annotation in CI, a plain line elsewhere."""
    prefix = f"::{level}::" if os.environ.get("GITHUB_ACTIONS") == "true" else f"{level}: "
    print(prefix + msg)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --- Commands -------------------------------------------------------------


def existing_by_code(gh: GitHub) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for m in gh.list_milestones():
        tm = TITLE_CODE_RE.match(m.get("title") or "")
        code = normalize_code(tm[1]) if tm else None
        if code is None:
            continue
        if code in found:
            log("warning", f"two GitHub milestones start with {code}; using #{found[code]['number']}")
            continue
        found[code] = m
    return found


def ensure(gh: GitHub, ms: Milestone, current: dict | None, repo_url: str) -> dict:
    """Create or update one GitHub milestone so it matches the table row."""
    want = {"title": ms.title, "description": ms.description(repo_url), "state": ms.state()}
    if current is None:
        created = gh.create_milestone(want)
        log("notice", f"created milestone {ms.code} ({ms.state()})")
        summary(f"- created **{ms.code}** ({ms.state()})")
        return created
    changed = {k: v for k, v in want.items() if (current.get(k) or "") != v}
    if not changed:
        return current
    updated = gh.update_milestone(current["number"], changed)
    log("notice", f"updated milestone {ms.code}: {', '.join(sorted(changed))}")
    summary(f"- updated **{ms.code}**: {', '.join(sorted(changed))}")
    return updated


def sync(gh: GitHub, milestones: list[Milestone], repo_url: str, backfill: bool) -> int:
    summary("### Milestone sync")
    existing = existing_by_code(gh)
    synced = {ms.code: ensure(gh, ms, existing.get(ms.code), repo_url) for ms in milestones}
    for code in sorted(set(existing) - set(synced)):
        log("notice", f"GitHub milestone {code} is not in MILESTONES.md; left unchanged")
    if not backfill:
        return 0

    filed = 0
    for ms in milestones:
        for number in ms.prs:
            pr = gh.get_pull(number)
            if pr is None or not pr.get("merged_at") or pr.get("milestone"):
                continue  # missing, closed unmerged, or already filed
            gh.set_milestone(number, synced[ms.code]["number"])
            filed += 1
            summary(f"- filed #{number} under **{ms.code}**")
    log("notice", f"backfill filed {filed} merged PR(s)")
    return 0


def assign(gh: GitHub, milestones: list[Milestone], repo_url: str, event: dict) -> int:
    pr = event.get("pull_request")
    if not pr:
        log("error", "event payload has no pull_request")
        return 1
    number = pr["number"]
    labels = [label.get("name", "") for label in pr.get("labels") or []]
    codes = tags_in(pr.get("title") or "", pr.get("body") or "", labels)

    if not codes:
        log("notice", f"#{number} has no milestone tag; nothing to do")
        return 0
    if len(codes) > 1:
        tagged = ", ".join(sorted(codes, key=lambda c: int(c[1:])))
        log("warning", f"#{number} is tagged {tagged}; a PR belongs to one milestone, so it was left as is")
        summary(f"#{number} is tagged {tagged}: pick one.")
        return 0

    code = codes.pop()
    ms = next((m for m in milestones if m.code == code), None)
    if ms is None:
        known = ", ".join(m.code for m in milestones)
        log("warning", f"#{number} is tagged {code}, which is not in MILESTONES.md ({known})")
        summary(f"#{number} is tagged {code}, which is not in MILESTONES.md.")
        return 0

    target = ensure(gh, ms, existing_by_code(gh).get(code), repo_url)
    current = (pr.get("milestone") or {}).get("number")
    if current == target["number"]:
        log("notice", f"#{number} is already filed under {code}")
        return 0
    gh.set_milestone(number, target["number"])
    log("notice", f"filed #{number} under {code}")
    summary(f"Filed #{number} under **{ms.title}**.")
    return 0


def client(dry_run: bool) -> tuple[GitHub, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not repo or not token:
        raise SystemExit("error: GITHUB_REPOSITORY and GITHUB_TOKEN must be set")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    cls = DryRun if dry_run else GitHub
    return cls(repo, token, api), f"{server}/{repo}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_table = sub.add_parser("table", help="print the parsed At a glance table")
    p_table.add_argument("--json", action="store_true")
    p_sync = sub.add_parser("sync", help="create/update GitHub milestones from the table")
    p_sync.add_argument("--backfill", action="store_true", help="also file merged PRs listed in headings")
    p_sync.add_argument("--dry-run", action="store_true")
    p_assign = sub.add_parser("assign", help="file one PR under the milestone it is tagged with")
    p_assign.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    p_assign.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        milestones = parse_milestones(MILESTONES_MD.read_text(encoding="utf-8"))
    except ValueError as err:
        log("error", f"MILESTONES.md: {err}")
        return 1

    if args.command == "table":
        if args.json:
            print(json.dumps([asdict(m) | {"title": m.title} for m in milestones], indent=2, ensure_ascii=False))
        else:
            for m in milestones:
                prs = f"  PRs {m.prs[0]}–{m.prs[-1]}" if m.prs else ""
                print(f"{m.code:<4} {m.state():<6} {m.version:<16} {m.name}  #{m.anchor or '-'}{prs}")
        return 0

    gh, repo_url = client(args.dry_run)
    if args.command == "sync":
        return sync(gh, milestones, repo_url, args.backfill)
    if not args.event:
        log("error", "assign needs --event or GITHUB_EVENT_PATH")
        return 1
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    return assign(gh, milestones, repo_url, event)


if __name__ == "__main__":
    sys.exit(main())
