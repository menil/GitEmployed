"""CLI entry point for stale job search issue cleanup.

Finds open, inactive 'ready-to-apply' issues, checks if their job links
are closed, and archives them with an 'expired' reason label.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from gitemployed.github_client import GitHubClient
from gitemployed.loader import load_settings
from gitemployed.stale_cleaner import clean_stale_issues

logger = logging.getLogger("clean_stale")


def main() -> None:
    """Parse CLI arguments and run stale issue cleanup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Clean up inactive, expired job search issues."
    )
    parser.add_argument(
        "--settings",
        "-s",
        type=str,
        default="config/settings.yaml",
        help="Path to settings YAML file.",
    )
    parser.add_argument(
        "--days-inactive",
        "-d",
        type=int,
        default=None,
        help="Number of days without activity required (overrides settings).",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum issues to evaluate (overrides settings, default unlimited).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate without modifying GitHub issues or board.",
    )
    parser.add_argument(
        "--include-bookmarked",
        action="store_true",
        help="Include issues tagged with 'want-to-apply'.",
    )
    parser.add_argument(
        "--delay",
        "-w",
        type=float,
        default=None,
        help=(
            "Delay in seconds between liveness checks "
            "(overrides settings, default 0.0)."
        ),
    )

    args = parser.parse_args()

    is_dry = args.dry_run or os.environ.get("DRY_RUN", "").lower() in (
        "1",
        "true",
        "yes",
    )

    # 1. Load settings if available
    settings = None
    if Path(args.settings).exists():
        try:
            settings = load_settings(args.settings)
        except Exception as se:
            logger.warning("Could not load settings from %s: %s", args.settings, se)

    # Honor settings.stale_cleanup.enabled unless overridden
    if settings and not settings.stale_cleanup.enabled:
        logger.info("Stale cleanup is disabled in settings.yaml. Skipping.")
        return

    # Resolve parameters from CLI args or settings
    cfg = settings.stale_cleanup if settings else None
    days_inactive = (
        args.days_inactive
        if args.days_inactive is not None
        else (cfg.days_inactive if cfg else 30)
    )
    limit = args.limit if args.limit is not None else (cfg.limit if cfg else None)
    include_bookmarked = (
        args.include_bookmarked
        if args.include_bookmarked
        else (cfg.include_bookmarked if cfg else False)
    )
    request_delay = (
        args.delay if args.delay is not None else (cfg.request_delay if cfg else 0.0)
    )

    # 2. Resolve GitHub credentials
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if is_dry and (not token or not repo):
        token = token or "dummy_token"
        repo = repo or "dummy/repo"
    elif not token or not repo:
        logger.error("Missing GITHUB_TOKEN or GITHUB_REPOSITORY environment variable.")
        sys.exit(1)

    project_id = None
    status_field = "Status"
    if settings and settings.projects_v2:
        project_id = settings.projects_v2.project_id
        status_field = settings.projects_v2.status_field_name

    gh_client = GitHubClient(
        token=token,
        repo=repo,
        project_id=project_id,
        status_field_name=status_field,
    )

    try:
        summary = clean_stale_issues(
            gh_client=gh_client,
            days_inactive=days_inactive,
            dry_run=is_dry,
            limit=limit,
            include_bookmarked=include_bookmarked,
            request_delay=request_delay,
        )
        logger.info(
            "Summary: %d inspected, %d closed, %d active, %d inconclusive",
            summary.inspected_count,
            summary.closed_count,
            summary.skipped_active_count,
            summary.skipped_inconclusive_count,
        )
    except Exception as e:
        logger.error("Failed to run stale issue cleanup: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
