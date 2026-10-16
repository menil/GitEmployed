# AI Decision Record: Add want-to-apply Label and Update needs-description Color

## Context & Goal
GitEmployed tracks job applications using GitHub issues and Projects V2 board columns. When a job issue is approved by AI triage, it receives a fit tier label (e.g. `fit:A`) and the `ready-to-apply` lifecycle label. Users requested a bookmarking/TODO mechanism to flag roles they want to apply to later when they are not ready to apply immediately (`want-to-apply`).
Furthermore, whenever the system automation removes the `ready-to-apply` label (e.g. when an application is submitted or the role is rejected/closed), the `want-to-apply` label should be cleaned up automatically.
Additionally, the `needs-description` label previously used yellow (`#fbca04`), identical to `fit:B`, causing visual confusion on the board; it needed to be updated to brown (`#8b4513`).

## Architecture & Key Decisions
1. **Separation from `LIFECYCLE_LABELS`**:
   - `want-to-apply` is deliberately **excluded** from `LIFECYCLE_LABELS`. Lifecycle labels represent mutually exclusive pipeline columns in Projects V2 (`Triage Pending`, `Ready to Apply`, `Applied`, `In Loop`, `Offer Received`, `Rejected`, `Mismatched/Closed`).
   - `want-to-apply` is an auxiliary user bookmark that coexists alongside `ready-to-apply`. Making it a lifecycle label would erroneously require an extra Projects V2 column and disrupt automated column-sync mapping.
2. **Centralized Eviction via `sync_lifecycle_label` and `get_updated_lifecycle_labels`**:
   - All status transitions across GitEmployed (webhook status transitions, reverse Projects V2 column sync, conversational assistant status updates, and Gmail application sync) route through `sync_lifecycle_label` in `src/gitemployed/status_model.py`.
   - When `ready-to-apply` is identified as stale during a lifecycle transition, `want-to-apply` is automatically appended to the eviction list if present.
   - `get_updated_lifecycle_labels` mirrors this logic for in-memory status projections during backfills, discarding `want-to-apply` whenever leaving `ready-to-apply`.
3. **Color Selection**:
   - `want-to-apply` uses `#a2eeef` (GitHub's standard default color for the `enhancement` label, light cyan/teal).
   - `needs-description` uses `#8b4513` (SaddleBrown), providing high contrast with white text and distinguishing missing descriptions from `fit:B` evaluations.

## Alternatives Considered & Rejected
- **Adding `want-to-apply` to `LIFECYCLE_LABELS`**: Rejected because an issue represents one pipeline stage on Projects V2. A dedicated column would clutter the board and complicate Kanban workflows for what is fundamentally a lightweight personal bookmark.
- **Requiring manual removal by the user**: Rejected per requirements. Once a candidate applies or the role is rejected, the "want to apply" intention is fulfilled or moot; leaving stale TODO tags creates clutter.
- **Handling removal at each individual caller**: Rejected to prevent logic fragmentation across `status_transition.py`, `project_sync.py`, and `respond.py`. Centralizing inside `status_model.py` guarantees uniform behavior everywhere.
