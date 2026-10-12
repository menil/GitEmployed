# AI Decision Record: Configurable Results Wanted & Increased Hours Old

## Context & Goal
On daily LinkedIn job scraping runs, users noticed that the scraper was finding fewer and fewer jobs over time, suspecting deduplication.
Investigation revealed:
1. `results_wanted=15` was hardcoded in `src/gitemployed/scraper.py`.
2. LinkedIn's default guest search sorts results by relevance and sponsored status rather than purely chronological order.
3. The top 15 results frequently contain the same sponsored/bumped postings day after day.
4. GitEmployed's deduplication system correctly identifies these repeated roles and skips them, starving the pipeline while fresh postings reside at ranks 16–100+.
5. `hours_old: 24` combined with a 24-hour GitHub Actions schedule led to slippage windows whenever runner queues experienced scheduling delays.

The goal is to make `results_wanted` fully configurable in `settings.yaml` and CLI/workflow overrides, increase its default to 50, and widen `hours_old` to 48.

## Architecture & Key Decisions
- **Configurable `results_wanted`**: Exposed in `SearchConfig` (`src/gitemployed/schema.py`) with positive integer validation, defaulting to 50.
- **CLI & Workflow Overrides**: Added `--results-wanted` (`-n`) to `src/gitemployed/cli/scrape.py` and workflow dispatch inputs in `template/.github/workflows/scrape-jobs.yml`.
- **Precedence Hierarchy**: `CLI / Workflow Dispatch` > `run_scraper(results_wanted_override=...)` > `settings.search.results_wanted` > `SearchConfig default (50)`.
- **48-Hour Lookback Window**: Updated `hours_old` default to 48 across `SearchConfig`, `template/config/settings.yaml`, and `scrape-jobs.yml` to close runner latency gaps.
- **Defensive Runtime Validation**: Added checks in `run_scraper` for `results_wanted <= 0` and `hours_old <= 0` while using `isinstance(..., (int, float))` to preserve `MagicMock` compatibility during tests.

## Alternatives Considered & Rejected
- **Disabling or relaxing Tier-1 deduplication**: Rejected because creating duplicate GitHub issues for the same apply URL or job ID causes severe notification spam and confusion.
- **Bumping hardcoded limit without configuration**: Rejected because different users have different query volume and rate-limit tolerances; making it a first-class setting empowers users to tune it to their needs.
