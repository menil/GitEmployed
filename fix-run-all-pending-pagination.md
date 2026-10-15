# AI Decision Record: Fix run_all_pending pagination skipping issues

## Context & Goal
`run_all_pending` paged through open issues filtered by `triage-pending`
while triaging each one. Triage removes that label, so the filtered
result set shrank during iteration and page N+1 skipped issues that had
shifted into earlier pages. Goal: a backlog larger than `BATCH_PAGE_SIZE`
is fully triaged in one run, with a regression test (Beads JobGitOps-dgu).

## Architecture & Key Decisions
- `_list_all_pending_issues` paginates the full list up front, so page
  offsets are stable because nothing is mutated while listing.
- `run_all_pending` then triages the snapshot. A listing failure on any
  page aborts with `EXIT_ERROR` before anything is triaged
  (all-or-nothing), and the next run retries.
- A snapshot can go stale during a long LLM-bound run. Each issue is
  refetched via `get_issue` just before triage; closed issues or ones
  that lost `triage-pending` are skipped, and fresh title/body/labels
  are passed to `run_triage`. A refetch failure counts as that issue's
  failure and the batch continues.
- The workflow's `concurrency: group: triage` serializes webhook runs
  behind the batch, so the remaining staleness risk is human edits.

## Alternatives Considered & Rejected
- **Re-fetch page 1 until empty**: also fixes skipping, but any path
  where `run_triage` leaves the label (failure, quota) would be
  re-fetched forever, needing extra bookkeeping to avoid a loop.
- **Snapshot only, no refetch**: simpler, but widens the staleness
  window from one page to the whole backlog (risk of duplicate triage
  or acting on closed issues). One cheap GET per issue is small next
  to the LLM calls.
- **Page cap and removing the redundant client-side label filter**:
  raised in self-review; left as optional follow-ups to keep this bug
  fix focused.
