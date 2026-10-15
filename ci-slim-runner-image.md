# AI Decision Record: Run light workflows on the :slim runner image

## Context & Goal
`status-transition.yml` took ~60s per run, ~40s of it pulling the full
`ghcr.io/menil/gitemployed:latest` image (Chromium, Bun, resumed,
pdf-vdiff) to run an ~11s Python script. Runners are ephemeral, so the
image is re-pulled every run, and triage/label batches fire many of
these. A `:slim` image (Python 3.12 + git + gh + curl + the CLI, no PDF
tooling) already exists and only `respond-issue.yml` used it
(Beads JobGitOps-6q9).

## Architecture & Key Decisions
- Move the workflows that only call the GitHub API or parse data to
  `:slim`: status-transition, project-status-sync, gmail-sync,
  esd-export, check-setup, scrape-jobs.
- Each was audited statically before switching: the workflow's steps
  and the CLI module's import chain were checked for
  resumed/bun/chromium/pdf-vdiff/just use. Only stdlib, yaml, pydantic,
  pandas, bs4, jobspy, google client libs and internal modules appear,
  all in the main project dependencies that `uv sync --frozen` installs
  in the slim stage.
- gmail-sync imports `respond.execute_action`, which has bun-gated paths
  (`_has_bun_runtime`). gmail-sync only issues STATUS_UPDATE actions, so
  it never reaches them; slim also degrades safely by delegating to
  triage when bun is missing.
- `yq` appears in gmail-sync and scrape-jobs, but only in their
  host-runner `check` jobs, not the container jobs.
- A guardrail test (`tests/test_workflow_images.py`) pins slim vs full
  workflows, so a rendering workflow cannot be moved to slim by mistake.
- Kept on `:latest`: triage-issue, recompile-tailored-resume,
  format-resume, and the triage-opened job in respond-issue.

## Alternatives Considered & Rejected
- **One PR per workflow (stacked)**: the task suggested this for easy
  bisecting. Rejected by the maintainer: each change is a one-line image
  swap, so a single commit and PR is simpler to review and revert.
- **Only migrate status-transition**: lowest risk, but leaves the other
  API-only workflows paying the same ~40s per run.
- **Slim down the full image instead**: would not help; the cost is the
  pull size, and rendering workflows need that tooling.

## Verification gaps
Audited statically only. Workflows have not been run on the slim image,
and the "Initialize containers" drop has not been measured; both need a
run in job-search after the sync release.
