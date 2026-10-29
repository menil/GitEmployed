# AI Decision Record: Prevent Gmail Sync Cursor Advance When Messages Fail

## Context & Goal
In GitHub Actions run `40759022134` (`menil/job-search`), Gmail sync failed to classify 5 eligible emails because Gemini prepayment credits were depleted (HTTP 402 `RESOURCE_EXHAUSTED`), yet the workflow still advanced `last_synced_at`, pruned older `processed` entries, pushed a new cursor commit (`chore(gmail): advance sync cursor`) to `gmail-sync-state`, and exited `0` (green badge).

Two root causes combined:
1. LiteLLM wraps Gemini's HTTP 402 `RESOURCE_EXHAUSTED` response ("Your prepayment credits are depleted.") in `litellm.BadRequestError` rather than `RateLimitError` or `BudgetExceededError`. `LiteLLMClient._call_litellm` only caught `(RateLimitError, BudgetExceededError, ContextWindowExceededError)` as `QuotaExceededError`, turning 402 credit depletion into a generic `ValidationError` that `gmail_sync.process_message` logged and swallowed as a per-message failure instead of exiting `75` (`EXIT_QUOTA_EXCEEDED`).
2. `gmail_sync.run_sync` precomputed `caught_up = unprocessed_total <= MAX_MESSAGES_PER_RUN` *before* running `process_message` across `batch_ids`. Even when messages in `batch_ids` failed processing and were omitted from `processed`, `caught_up` remained `True`, causing `run_sync` to advance `cursor["last_synced_at"]` to `now`, prune old `processed` entries, and commit/push the cursor.

## Architecture & Key Decisions
1. **Centralized Quota/Credit Exhaustion Detection (`_is_quota_exceeded_error` in `src/gitemployed/llm.py`)**:
   - Extracted `_is_quota_exceeded_error(exc)` to inspect LiteLLM exception classes (`RateLimitError`, `BudgetExceededError`, `ContextWindowExceededError`), HTTP status codes (`402`, `429`), and normalized error markers (`resource_exhausted`, `insufficient_quota`, `credits are depleted`, `insufficient credits`, `credit balance is too low`).
   - Ensures all callers (`gmail_sync`, `respond`, `triage`, `scraper`) consistently treat HTTP 402 credit depletion as `QuotaExceededError` (exit `75`).
2. **Post-Batch Cursor & Pruning Gating (`run_sync` in `src/gitemployed/cli/gmail_sync.py`)**:
   - Computed `batch_had_failures = any(mid not in processed for mid in batch_ids)` *after* iterating over `batch_ids`.
   - When `batch_ids` is non-empty and zero messages in `batch_ids` were added to `processed`, `run_sync` logs a warning and returns early without touching `last_synced_at`, pruning `processed`, or checking out `gmail-sync-state`.
   - When a batch partially succeeds (`batch_had_failures` is `True`, at least one message succeeded), `caught_up` is `False` (holding back `last_synced_at`) and `_prune_processed` is skipped (preventing premature eviction of older processed IDs while the sync window has unresolved failures), while still persisting the newly processed message IDs.

## Alternatives Considered & Rejected
- **Catching `BadRequestError` indiscriminately as `QuotaExceededError`**: Rejected because genuine 400 Bad Request errors (e.g., malformed request parameters) are not quota/credit failures and should remain `ValidationError`.
- **Aborting the entire batch on a single `ValidationError`**: Rejected for non-quota errors because `specs/gmail-integration.md` §9.6 specifies that a single message's malformed LLM JSON output should leave that message unprocessed for retry without aborting other valid messages in the batch, while still holding back `last_synced_at` until all eligible messages are processed.
