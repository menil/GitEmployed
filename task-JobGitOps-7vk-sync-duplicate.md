# AI Decision Record: Map Duplicate Label to Triage-Mismatched Lifecycle

## Context & Goal
When job issues are identified as duplicate postings (e.g. redundant LinkedIn job IDs) and closed with the `duplicate` label, the previous status lifecycle model did not recognize `duplicate` as a terminal mismatch reason. As a result:
1. `resolve_closed_lifecycle_label(labels)` defaulted unrecognized closed issues to `rejected`.
2. When synchronizing lifecycle labels, `sync_lifecycle_label()` attempted to re-add `triage-mismatched` or reverted status unexpectedly.
3. In GitHub Projects V2 sync (`project_sync.py`), closed duplicate issues were not mapped to the `Mismatched/Closed` column.

The objective was to treat `duplicate` consistently across the lifecycle state machine, status transition workflow, and Projects V2 board synchronization, ensuring duplicate job issues transition to and remain in `Mismatched/Closed` (`triage-mismatched`) without redundant labels or workflow loops.

## Architecture & Key Decisions
1. **Centralized Definition in `status_model.py`**:
   - Defined `DUPLICATE_LABEL = "duplicate"`.
   - Defined `CLOSED_MISMATCH_REASON_LABELS = MISMATCH_REASON_LABELS | frozenset({DUPLICATE_LABEL})`.
   - `resolve_closed_lifecycle_label(labels)`: Checks if any label in `CLOSED_MISMATCH_REASON_LABELS` is present. If so, resolves directly to `TRIAGE_MISMATCHED_LABEL` (`"triage-mismatched"`).
2. **Idempotent Label Sync**:
   - Updated `sync_lifecycle_label()`, `is_lifecycle_label_satisfied()`, and `get_updated_lifecycle_labels()`: When target label is `TRIAGE_MISMATCHED_LABEL` and any label in `CLOSED_MISMATCH_REASON_LABELS` (such as `duplicate`) is already present on the issue, we avoid redundantly adding `triage-mismatched` while stripping stale non-terminal lifecycle labels (e.g., `ready-to-apply`).
3. **End-to-End Alignment**:
   - Updated `src/gitemployed/cli/status_transition.py` (`_resolve_label`) to check `CLOSED_MISMATCH_REASON_LABELS` on closed issues.
   - Updated `src/gitemployed/cli/project_sync.py` (`_target_status`) to map closed issues with `CLOSED_MISMATCH_REASON_LABELS` to `STATUS_CLOSED_MISMATCHED` (`Mismatched/Closed`).

## Alternatives Considered & Rejected
1. **Adding `duplicate` directly to `MISMATCH_REASON_LABELS`**:
   - Rejected because `MISMATCH_REASON_LABELS` represents triage-level qualification mismatch reasons (e.g., location, skills, salary). Triage qualification reasons might be displayed or filtered differently in triage reporting, whereas `duplicate` is a post-triage administrative closure reason. Creating `CLOSED_MISMATCH_REASON_LABELS = MISMATCH_REASON_LABELS | frozenset({DUPLICATE_LABEL})` cleanly decouples domain triage reasons from closure classification.
2. **Creating a dedicated `STATUS_CLOSED_DUPLICATE` project column**:
   - Rejected to avoid complicating user Kanban boards and project configurations. Folding duplicates under `Mismatched/Closed` matches existing repository specifications and simplifies column mappings.
