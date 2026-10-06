# AI Decision Record: Fast Early Exit for Gmail Sync Workflow

## Context & Goal
The GitEmployed repository includes an hourly cron workflow (`template/.github/workflows/gmail-sync.yml`) that polls Gmail for lifecycle application updates when enabled. By default in template installations, the Gmail integration is disabled (`# gmail:` commented out in `config/settings.yaml`). Previously, the workflow executed a single containerized job declaring `container: image: ghcr.io/menil/gitemployed:latest`, forcing GitHub Actions runners to pull the entire multi-gigabyte container image, perform a full checkout, and execute status badge updates before running `gitemployed.cli.gmail_sync` and discovering that Gmail integration is disabled. Running this hourly (720 times/month) consumed significant runner minutes and compute resources on disabled installations.

The goal was to allow the workflow to exit in seconds when Gmail integration is disabled, bypassing runner image downloads and badge updates.

## Architecture & Key Decisions
1. **Two-Stage DAG Pattern (`check` -> `sync`)**:
   - Introduced a lightweight, uncontainerized `check` job running directly on `ubuntu-latest`.
   - Used `actions/checkout@v4` with `sparse-checkout: config/settings.yaml` to fetch only the configuration file.
   - Evaluated `.gmail.enabled // false` via preinstalled `yq`.
   - Gated the downstream containerized `sync` job with `needs: check` and `if: needs.check.outputs.enabled == 'true'`.
   - Matches the existing architectural pattern established in `template/.github/workflows/scrape-jobs.yml`.

2. **Defense-in-Depth & Fail-Safe Fallbacks**:
   - Handled non-existent config, missing `gmail` section, commented YAML, and YAML syntax errors safely via `$(yq '.gmail.enabled // false' config/settings.yaml 2>/dev/null || echo "false")`.
   - Preserved internal checks inside `gitemployed.cli.gmail_sync:main()` to ensure standalone/manual invocations remain safe.

3. **Workflow Drift Testing**:
   - Added unit test `test_gmail_sync_workflow_file_valid` in `tests/test_gmail_sync.py` to assert YAML schema validity, job dependencies (`needs: check`), and condition guards.

## Alternatives Considered & Rejected
- **In-Job Conditional Container Execution**:
  - GitHub Actions does not allow conditional evaluation of top-level `container:` keys within a single job; declaring `container:` unconditionally forces runner image pulls during job initialization.
- **Workflow Dispatch Override**:
  - Considered bypassing the check on `workflow_dispatch` (as in `scrape-jobs.yml`). Rejected because `gmail_sync.py` strictly requires Google OAuth credentials and configuration; running when disabled without credentials would produce fatal configuration errors instead of a useful test run.
