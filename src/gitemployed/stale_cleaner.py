"""Stale job search issue sweeper and cleaner.

Identifies open, inactive 'ready-to-apply' issues that have had no user
activity since triage, evaluates whether their job posting has closed,
and archives them by transitioning to 'Mismatched/Closed' with an 'expired'
reason label.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from gitemployed.github_client import GitHubClient, extract_label_names
from gitemployed.liveness import JobLivenessStatus, LivenessResult, check_job_liveness
from gitemployed.scraper import extract_apply_url
from gitemployed.status_model import (
    EXPIRED_LABEL,
    LABEL_TO_STATUS,
    WANT_TO_APPLY_LABEL,
    sync_lifecycle_label,
)

logger = logging.getLogger(__name__)

DEFAULT_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "github-actions[bot]",
        "github-actions",
    }
)


def is_bot_comment(
    comment: dict[str, Any], bot_logins: set[str] | frozenset[str] | None = None
) -> bool:
    """Return True if a comment was authored by a bot or automated workflow."""
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return True
    login = (user.get("login") or "").strip().lower()
    active_bot_logins = bot_logins if bot_logins is not None else DEFAULT_BOT_LOGINS
    if login in active_bot_logins:
        return True
    body = comment.get("body") or ""
    # Automation comment markers used in GitEmployed
    return (
        "<!-- gitemployed:status-update -->" in body
        or "<!-- gitemployed:gmail-notice -->" in body
    )


def has_human_activity(
    comments: list[dict[str, Any]],
    bot_logins: set[str] | frozenset[str] | None = None,
) -> bool:
    """Check if any comments were posted by a non-bot human user."""
    return any(not is_bot_comment(c, bot_logins=bot_logins) for c in comments)


@dataclass
class StaleJobEvaluation:
    """Per-issue evaluation record during a stale cleanup sweep."""

    issue_number: int
    title: str
    apply_url: str | None
    liveness: LivenessResult | None
    action_taken: str
    details: str = ""


@dataclass
class StaleCleanupSummary:
    """Aggregated results from a stale cleanup execution."""

    inspected_count: int = 0
    closed_count: int = 0
    skipped_active_count: int = 0
    skipped_inconclusive_count: int = 0
    skipped_human_activity_count: int = 0
    skipped_bookmarked_count: int = 0
    skipped_no_url_count: int = 0
    evaluations: list[StaleJobEvaluation] = field(default_factory=list)


def calculate_cutoff_date(
    days: int, now: datetime.datetime | None = None
) -> datetime.date:
    """Calculate the ISO cutoff date (YYYY-MM-DD) for inactivity."""
    current_time = now or datetime.datetime.now(datetime.UTC)
    cutoff = current_time - datetime.timedelta(days=max(0, days))
    return cutoff.date()


def fetch_stale_candidate_issues(
    gh_client: GitHubClient,
    days_inactive: int = 30,
    limit: int | None = None,
    now: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    """Query GitHub API for open ready-to-apply issues updated before cutoff date.

    Args:
        gh_client: Initialized GitHub client.
        days_inactive: Number of days without updates required to qualify.
        limit: Maximum number of issues to return (None for all).
        now: Optional injected current datetime.

    Returns:
        List of matching issue dictionaries.
    """
    cutoff_date = calculate_cutoff_date(days_inactive, now=now).isoformat()
    query = (
        f"repo:{gh_client.repo} is:issue is:open label:ready-to-apply "
        f"updated:<{cutoff_date}"
    )
    logger.info("Searching stale issues with query: '%s'", query)

    candidates: list[dict[str, Any]] = []
    page = 1
    per_page = 100

    while True:
        issues = gh_client.search_issues(query, per_page=per_page, page=page)
        if not issues:
            break

        for issue in issues:
            candidates.append(issue)
            if limit is not None and len(candidates) >= limit:
                return candidates

        if len(issues) < per_page:
            break
        page += 1

    return candidates


def clean_stale_issues(
    gh_client: GitHubClient,
    days_inactive: int = 30,
    dry_run: bool = False,
    limit: int | None = None,
    include_bookmarked: bool = False,
    session: requests.Session | None = None,
    now: datetime.datetime | None = None,
) -> StaleCleanupSummary:
    """Find and clean up stale, expired job search issues.

    Args:
        gh_client: Initialized GitHubClient instance.
        days_inactive: Days of inactivity before an issue is eligible.
        dry_run: If True, evaluate without mutating GitHub issues or board.
        limit: Maximum number of issues to evaluate.
        include_bookmarked: Whether to inspect 'want-to-apply' bookmarked roles.
        session: Optional injected requests.Session for URL inspection.
        now: Optional injected current datetime for testing.

    Returns:
        StaleCleanupSummary object.
    """
    candidates = fetch_stale_candidate_issues(
        gh_client, days_inactive=days_inactive, limit=limit, now=now
    )
    summary = StaleCleanupSummary(inspected_count=len(candidates))

    req_session = session or requests.Session()
    close_session = session is None

    try:
        for issue in candidates:
            num = issue.get("number") or 0
            title = issue.get("title") or ""
            node_id = issue.get("node_id") or ""
            body = issue.get("body") or ""
            labels = set(extract_label_names(issue.get("labels", [])))

            # Guard 1: Skip bookmarked 'want-to-apply' unless explicitly included
            if WANT_TO_APPLY_LABEL in labels and not include_bookmarked:
                logger.info(
                    "Skipping issue #%d '%s': bookmarked with '%s'",
                    num,
                    title,
                    WANT_TO_APPLY_LABEL,
                )
                summary.skipped_bookmarked_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=None,
                        liveness=None,
                        action_taken="skipped_bookmarked",
                        details=f"Tagged with {WANT_TO_APPLY_LABEL}",
                    )
                )
                continue

            # Guard 2: Skip issues with human interaction/comments
            try:
                comments = gh_client.list_comments(num)
                if has_human_activity(comments):
                    logger.info(
                        "Skipping issue #%d '%s': contains human activity/comments",
                        num,
                        title,
                    )
                    summary.skipped_human_activity_count += 1
                    summary.evaluations.append(
                        StaleJobEvaluation(
                            issue_number=num,
                            title=title,
                            apply_url=None,
                            liveness=None,
                            action_taken="skipped_human_activity",
                            details="Contains non-bot user comments",
                        )
                    )
                    continue
            except Exception as ce:
                logger.warning("Failed to fetch comments for issue #%d: %s", num, ce)

            # Guard 3: Extract Apply URL
            apply_url = extract_apply_url(body)
            if not apply_url:
                logger.warning(
                    "Skipping issue #%d '%s': no Apply URL found in body",
                    num,
                    title,
                )
                summary.skipped_no_url_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=None,
                        liveness=None,
                        action_taken="skipped_no_url",
                        details="No apply URL found in body",
                    )
                )
                continue

            # Evaluate Liveness
            logger.info("Checking liveness for issue #%d: %s", num, apply_url)
            liveness = check_job_liveness(apply_url, session=req_session)

            if liveness.status == JobLivenessStatus.OPEN:
                logger.info("Issue #%d is still open: %s", num, liveness.reason)
                summary.skipped_active_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=apply_url,
                        liveness=liveness,
                        action_taken="skipped_active",
                        details=liveness.reason,
                    )
                )
                continue

            if liveness.status == JobLivenessStatus.INCONCLUSIVE:
                logger.info(
                    "Issue #%d liveness is inconclusive: %s",
                    num,
                    liveness.reason,
                )
                summary.skipped_inconclusive_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=apply_url,
                        liveness=liveness,
                        action_taken="skipped_inconclusive",
                        details=liveness.reason,
                    )
                )
                continue

            # Liveness is CLOSED -> Execute closure action
            if dry_run:
                logger.info(
                    "[DRY RUN] Would close issue #%d '%s': %s",
                    num,
                    title,
                    liveness.reason,
                )
                summary.closed_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=apply_url,
                        liveness=liveness,
                        action_taken="dry_run_closed",
                        details=liveness.reason,
                    )
                )
                continue

            logger.info(
                "Closing expired issue #%d '%s': %s", num, title, liveness.reason
            )
            try:
                # 1. Add expired label and sync lifecycle to triage-mismatched
                if EXPIRED_LABEL not in labels:
                    gh_client.add_labels(num, [EXPIRED_LABEL])
                    labels.add(EXPIRED_LABEL)
                sync_lifecycle_label(gh_client, num, "triage-mismatched", labels)

                # 2. Update Projects V2 column if configured
                if gh_client.project_id and node_id:
                    status_name = LABEL_TO_STATUS["triage-mismatched"]
                    try:
                        gh_client.update_project_status(node_id, status_name)
                    except Exception as pe:
                        logger.warning(
                            "Failed to update Projects V2 status for #%d: %s",
                            num,
                            pe,
                        )

                # 3. Close the GitHub issue
                gh_client.close_issue(num)

                summary.closed_count += 1
                summary.evaluations.append(
                    StaleJobEvaluation(
                        issue_number=num,
                        title=title,
                        apply_url=apply_url,
                        liveness=liveness,
                        action_taken="closed",
                        details=liveness.reason,
                    )
                )
            except Exception as ie:
                logger.error("Failed to close issue #%d: %s", num, ie)

    finally:
        if close_session:
            req_session.close()

    logger.info(
        "Stale cleanup completed: %d inspected, %d closed, %d active, %d inconclusive",
        summary.inspected_count,
        summary.closed_count,
        summary.skipped_active_count,
        summary.skipped_inconclusive_count,
    )
    return summary
