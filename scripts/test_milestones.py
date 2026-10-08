"""Tests for scripts/milestones.py.

Run: python3 -m unittest discover -s scripts -p 'test_milestones.py' -v
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

import milestones as ms

DOC = """\
# Milestones

## At a glance

| # | Milestone | Version | Status |
| --- | --- | --- | --- |
| M1 | Deterministic replay | in 0.2.0 | ✅ Done 2026-10-05 |
| **M4** | **Pay down debt: a single node you'd trust** | **0.3.0** | **⏭ Next** |
| M8 | **Maintenance gate** | **1.0.0** | Finish line |
| — | Sharding | 2.x | Parked: [decision D1](#decisions) |

## Part 1

### M1 — Deterministic replay (PRs #1–#3, 2026-10-05)

### M4 — Pay down debt: a single node you'd trust (`0.3.0`) ⏭ next

## Part 3 — The maintenance gate (M8, `1.0.0`)
"""

REPO_URL = "https://github.com/o/r"


class FakeGitHub(ms.GitHub):
    def __init__(self, milestones=None, pulls=None):
        super().__init__("o/r", "token")
        self.milestones = {m["number"]: dict(m) for m in milestones or []}
        self.pulls = {p["number"]: dict(p) for p in pulls or []}
        self.writes: list[tuple] = []

    def list_milestones(self):
        return [dict(m) for m in self.milestones.values()]

    def create_milestone(self, fields):
        number = max(self.milestones, default=0) + 1
        self.milestones[number] = {"number": number, **fields}
        self.writes.append(("create", fields["title"]))
        return dict(self.milestones[number])

    def update_milestone(self, number, fields):
        self.milestones[number].update(fields)
        self.writes.append(("update", number, tuple(sorted(fields))))
        return dict(self.milestones[number])

    def get_pull(self, number):
        return self.pulls.get(number)

    def set_milestone(self, issue_number, milestone_number):
        self.pulls.setdefault(issue_number, {"number": issue_number})["milestone"] = {
            "number": milestone_number
        }
        self.writes.append(("file", issue_number, milestone_number))


def quiet(fn, *args):
    with redirect_stdout(io.StringIO()):
        return fn(*args)


def event(title="", body="", labels=(), milestone=None, number=42):
    return {
        "pull_request": {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
            "milestone": milestone,
        }
    }


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.parsed = {m.code: m for m in ms.parse_milestones(DOC)}

    def test_rows_with_codes_become_milestones(self):
        self.assertEqual(list(self.parsed), ["M1", "M4", "M8"])

    def test_markup_is_stripped(self):
        m4 = self.parsed["M4"]
        self.assertEqual(m4.name, "Pay down debt: a single node you'd trust")
        self.assertEqual(m4.version, "0.3.0")
        self.assertEqual(m4.title, "M4 · Pay down debt: a single node you'd trust")

    def test_done_rows_close(self):
        self.assertEqual(self.parsed["M1"].state(), "closed")
        self.assertEqual(self.parsed["M4"].state(), "open")
        self.assertEqual(self.parsed["M8"].state(), "open")

    def test_anchor_prefers_own_heading_then_mention(self):
        self.assertEqual(self.parsed["M4"].anchor, "m4--pay-down-debt-a-single-node-youd-trust-030--next")
        self.assertEqual(self.parsed["M8"].anchor, "part-3--the-maintenance-gate-m8-100")

    def test_pr_ranges_from_headings(self):
        self.assertEqual(self.parsed["M1"].prs, (1, 2, 3))
        self.assertEqual(self.parsed["M4"].prs, ())

    def test_missing_table_is_an_error(self):
        with self.assertRaises(ValueError):
            ms.parse_milestones("# Nothing here\n")

    def test_duplicate_code_is_an_error(self):
        doc = DOC.replace("| M8 |", "| M4 |")
        with self.assertRaises(ValueError):
            ms.parse_milestones(doc)

    def test_anchor_matches_github_slugs(self):
        self.assertEqual(ms.github_anchor("Parking lot (post-1.0 or undecided)"), "parking-lot-post-10-or-undecided")
        self.assertEqual(ms.github_anchor("Decisions only the owner can make"), "decisions-only-the-owner-can-make")

    def test_duplicate_headings_get_suffixes(self):
        anchors = [a for _, a in ms.headings("## Notes\n## Notes\n```\n## not a heading\n```\n")]
        self.assertEqual(anchors, ["notes", "notes-1"])

    def test_pr_numbers_lists_and_ranges(self):
        self.assertEqual(ms.pr_numbers("Foo (PRs #4–#6, #9)"), (4, 5, 6, 9))
        self.assertEqual(ms.pr_numbers("Foo (PR #7)"), (7,))
        self.assertEqual(ms.pr_numbers("Issue #7 mentioned"), ())

    def test_real_milestones_md_parses(self):
        parsed = ms.parse_milestones(ms.MILESTONES_MD.read_text(encoding="utf-8"))
        codes = [m.code for m in parsed]
        self.assertIn("M4", codes)
        self.assertTrue(all(m.anchor for m in parsed), "every milestone needs a heading to link to")


class TagTests(unittest.TestCase):
    def test_title_bracket(self):
        self.assertEqual(ms.tags_in("[M4] wire code_refs", "", []), {"M4"})
        self.assertEqual(ms.tags_in("fix [ m5 ] sync", "", []), {"M5"})

    def test_bare_mentions_do_not_count(self):
        self.assertEqual(ms.tags_in("M4 exit criteria", "This is part of M4 and M5.", []), set())

    def test_body_line_variants(self):
        for line in ["Milestone: M4", "**Milestone:** M4", "- milestone: m4", "> Milestone: M04 (debt)"]:
            self.assertEqual(ms.tags_in("", f"Intro\n{line}\nmore", []), {"M4"}, line)

    def test_comments_and_code_fences_are_ignored(self):
        body = "Milestone: <!-- e.g. M4 -->\n```\nMilestone: M5\n```\n<!--\nMilestone: M6\n-->\n"
        self.assertEqual(ms.tags_in("", body, []), set())

    def test_tags_outside_closed_comments_and_fences_count(self):
        self.assertEqual(ms.tags_in("", "<!-- note -->\nMilestone: M4", []), {"M4"})
        self.assertEqual(ms.tags_in("", "```\ncode\n```\nMilestone: M4", []), {"M4"})

    def test_unterminated_comment_hides_the_rest(self):
        self.assertEqual(ms.tags_in("", "<!-- draft\nMilestone: M4", []), set())

    def test_labels(self):
        self.assertEqual(ms.tags_in("", "", ["M6"]), {"M6"})
        self.assertEqual(ms.tags_in("", "", ["milestone: M6"]), {"M6"})
        self.assertEqual(ms.tags_in("", "", ["milestone/M6", "bug"]), {"M6"})
        self.assertEqual(ms.tags_in("", "", ["M6-followup"]), set())

    def test_same_milestone_twice_is_one_tag(self):
        self.assertEqual(ms.tags_in("[M4] x", "Milestone: M4", ["M4"]), {"M4"})


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.milestones = ms.parse_milestones(DOC)

    def test_creates_missing_and_closes_done(self):
        gh = FakeGitHub()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        by_title = {m["title"]: m for m in gh.milestones.values()}
        self.assertEqual(by_title["M1 · Deterministic replay"]["state"], "closed")
        self.assertEqual(by_title["M4 · Pay down debt: a single node you'd trust"]["state"], "open")
        self.assertIn("MILESTONES.md#m4--pay-down-debt", by_title["M4 · Pay down debt: a single node you'd trust"]["description"])

    def test_second_sync_is_a_no_op(self):
        gh = FakeGitHub()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        gh.writes.clear()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        self.assertEqual(gh.writes, [])

    def test_updates_only_what_drifted(self):
        gh = FakeGitHub()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        m4 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M4 "))
        gh.milestones[m4]["state"] = "closed"
        gh.writes.clear()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        self.assertEqual(gh.writes, [("update", m4, ("state",))])

    def test_renamed_row_updates_title_in_place(self):
        gh = FakeGitHub([{"number": 9, "title": "M4 · Old name", "description": None, "state": "open"}])
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        self.assertEqual(gh.milestones[9]["title"], "M4 · Pay down debt: a single node you'd trust")

    def test_unknown_github_milestones_are_left_alone(self):
        gh = FakeGitHub([{"number": 5, "title": "Q4 planning", "description": "", "state": "open"}])
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        self.assertEqual(gh.milestones[5]["title"], "Q4 planning")

    def test_backfill_files_merged_unfiled_prs_only(self):
        gh = FakeGitHub(
            pulls=[
                {"number": 1, "merged_at": "2026-10-05T00:00:00Z", "milestone": None},
                {"number": 2, "merged_at": None, "milestone": None},  # closed unmerged
                {"number": 3, "merged_at": "2026-10-05T00:00:00Z", "milestone": {"number": 77}},
            ]
        )
        quiet(ms.sync, gh, self.milestones, REPO_URL, True)
        m1 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M1 "))
        filed = [w for w in gh.writes if w[0] == "file"]
        self.assertEqual(filed, [("file", 1, m1)])


class AssignTests(unittest.TestCase):
    def setUp(self):
        self.milestones = ms.parse_milestones(DOC)

    def test_files_tagged_pr_creating_the_milestone(self):
        gh = FakeGitHub()
        rc = quiet(ms.assign, gh, self.milestones, REPO_URL, event(title="[M4] fix"))
        self.assertEqual(rc, 0)
        m4 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M4 "))
        self.assertEqual(gh.pulls[42]["milestone"]["number"], m4)

    def test_untagged_pr_is_left_alone(self):
        gh = FakeGitHub()
        quiet(ms.assign, gh, self.milestones, REPO_URL, event(title="docs", milestone={"number": 3}))
        self.assertEqual(gh.writes, [])

    def test_conflicting_tags_change_nothing(self):
        gh = FakeGitHub()
        rc = quiet(ms.assign, gh, self.milestones, REPO_URL, event(title="[M4] x", labels=["M8"]))
        self.assertEqual(rc, 0)
        self.assertEqual(gh.writes, [])

    def test_unknown_code_changes_nothing(self):
        gh = FakeGitHub()
        quiet(ms.assign, gh, self.milestones, REPO_URL, event(body="Milestone: M12"))
        self.assertEqual(gh.writes, [])

    def test_already_filed_is_a_no_op(self):
        gh = FakeGitHub()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        m4 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M4 "))
        gh.writes.clear()
        quiet(ms.assign, gh, self.milestones, REPO_URL, event(labels=["M4"], milestone={"number": m4}))
        self.assertEqual(gh.writes, [])

    def test_tag_overrides_a_different_milestone(self):
        gh = FakeGitHub()
        quiet(ms.sync, gh, self.milestones, REPO_URL, False)
        m4 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M4 "))
        m8 = next(n for n, m in gh.milestones.items() if m["title"].startswith("M8 "))
        quiet(ms.assign, gh, self.milestones, REPO_URL, event(body="Milestone: M8", milestone={"number": m4}))
        self.assertEqual(gh.pulls[42]["milestone"]["number"], m8)

    def test_event_without_pull_request_fails(self):
        self.assertEqual(quiet(ms.assign, FakeGitHub(), self.milestones, REPO_URL, {}), 1)


if __name__ == "__main__":
    unittest.main()
