# Scripts

`check-spec-citations.py` verifies that `WHITEPAPER.md §N.N` (and ranges) in
`docs/specification/` and Rust source cite headings that exist in `WHITEPAPER.md`.
Run locally with `python3 scripts/check-spec-citations.py`.

`milestones.py` keeps GitHub's Milestones page in step with `MILESTONES.md`:
`sync` creates or updates one GitHub milestone per row of the "At a glance"
table (Done rows are closed), and `assign` files a PR under the milestone it
is tagged with (`[M4]` in the title, `Milestone: M4` in the description, or
the label `M4`). `.github/workflows/milestones.yml` runs both. Locally,
`python3 scripts/milestones.py table` shows how the table parses, and
`python3 -m unittest discover -s scripts -p 'test_milestones.py'` runs the
tests.

`fix-whitepaper-page-headers.py` finds PDF page headers/footers (a bare page
number followed by the running chapter/section title) that got interleaved
into `WHITEPAPER.md`'s flowing text during the original PDF-to-Markdown
conversion. It removes the ones that fall cleanly on a paragraph/list-item
boundary and reports the rest (mid-sentence or mid-word breaks) to
`docs/reference/whitepaper-page-header-review.md` for manual reconstruction, since
restoring the original wording there requires human judgment. Run with
`python3 scripts/fix-whitepaper-page-headers.py [--apply] [--report PATH]`
(omit `--apply` for a dry run).
