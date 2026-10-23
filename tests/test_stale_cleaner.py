"""Unit tests for the stale job search issue cleaner."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from gitemployed.github_client import GitHubClient
from gitemployed.liveness import JobLivenessStatus, LivenessResult
from gitemployed.stale_cleaner import (
    calculate_cutoff_date,
    clean_stale_issues,
    fetch_stale_candidate_issues,
    has_human_activity,
    is_bot_comment,
)


def test_is_bot_comment() -> None:
    # GitHub App / Bot type
    assert is_bot_comment({"user": {"login": "some-app", "type": "Bot"}})
    # Known bot login
    assert is_bot_comment({"user": {"login": "github-actions[bot]", "type": "User"}})
    assert is_bot_comment({"user": {"login": "github-actions", "type": "User"}})
    # Automation comment markers
    assert is_bot_comment(
        {
            "user": {"login": "regular-user", "type": "User"},
            "body": "<!-- gitemployed:status-update --> Status updated to Applied",
        }
    )
    assert is_bot_comment(
        {
            "user": {"login": "regular-user", "type": "User"},
            "body": "<!-- gitemployed:gmail-notice --> Ambiguous match",
        }
    )
    # Real human comment
    assert not is_bot_comment(
        {
            "user": {"login": "regular-user", "type": "User"},
            "body": "I sent an email to the recruiter today.",
        }
    )


def test_has_human_activity() -> None:
    bot_comments = [
        {
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": "Triage fit: A",
        },
        {
            "user": {"login": "user", "type": "User"},
            "body": "<!-- gitemployed:status-update --> applied",
        },
    ]
    assert not has_human_activity(bot_comments)

    mixed_comments = [
        *bot_comments,
        {"user": {"login": "candidate", "type": "User"}, "body": "Need to follow up"},
    ]
    assert has_human_activity(mixed_comments)


def test_calculate_cutoff_date() -> None:
    now = datetime.datetime(2026, 10, 22, 12, 0, 0, tzinfo=datetime.UTC)
    cutoff = calculate_cutoff_date(30, now=now)
    assert cutoff == datetime.date(2026, 9, 22)


def test_fetch_stale_candidate_issues_with_limit() -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"

    page1 = [{"number": i, "title": f"Job {i}"} for i in range(1, 101)]
    page2 = [{"number": i, "title": f"Job {i}"} for i in range(101, 151)]
    client.search_issues.side_effect = [page1, page2]

    # Test limit 120
    issues = fetch_stale_candidate_issues(client, days_inactive=30, limit=120)
    assert len(issues) == 120
    assert issues[0]["number"] == 1
    assert issues[119]["number"] == 120

    # Test query format
    call_args = client.search_issues.call_args_list[0]
    query = call_args[0][0]
    assert "repo:owner/repo is:issue is:open label:ready-to-apply updated:<" in query


def test_clean_stale_issues_skips_bookmarked_by_default() -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 10,
            "title": "Staff Engineer",
            "labels": [{"name": "ready-to-apply"}, {"name": "want-to-apply"}],
            "body": "## Job Details\n- **Apply URL:** https://example.com/job/10",
        }
    ]

    summary = clean_stale_issues(client, days_inactive=30, include_bookmarked=False)
    assert summary.inspected_count == 1
    assert summary.skipped_bookmarked_count == 1
    assert summary.closed_count == 0


def test_clean_stale_issues_skips_human_activity() -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 11,
            "title": "Backend Dev",
            "labels": [{"name": "ready-to-apply"}],
            "body": "## Job Details\n- **Apply URL:** https://example.com/job/11",
        }
    ]
    client.list_comments.return_value = [
        {
            "user": {"login": "human", "type": "User"},
            "body": "I had a chat with the hiring manager.",
        }
    ]

    summary = clean_stale_issues(client, days_inactive=30)
    assert summary.inspected_count == 1
    assert summary.skipped_human_activity_count == 1
    assert summary.closed_count == 0


def test_clean_stale_issues_skips_missing_url() -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 12,
            "title": "DevOps Engineer",
            "labels": [{"name": "ready-to-apply"}],
            "body": "Missing apply url",
        }
    ]
    client.list_comments.return_value = []

    summary = clean_stale_issues(client, days_inactive=30)
    assert summary.inspected_count == 1
    assert summary.skipped_no_url_count == 1
    assert summary.closed_count == 0


@patch("gitemployed.stale_cleaner.check_job_liveness")
def test_clean_stale_issues_skips_active_and_inconclusive(
    mock_liveness: MagicMock,
) -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 20,
            "title": "Open Role",
            "labels": [{"name": "ready-to-apply"}],
            "body": "- **Apply URL:** https://example.com/job/20",
        },
        {
            "number": 21,
            "title": "Rate Limited Role",
            "labels": [{"name": "ready-to-apply"}],
            "body": "- **Apply URL:** https://example.com/job/21",
        },
    ]
    client.list_comments.return_value = []

    mock_liveness.side_effect = [
        LivenessResult(status=JobLivenessStatus.OPEN, reason="Page is active"),
        LivenessResult(status=JobLivenessStatus.INCONCLUSIVE, reason="HTTP 429"),
    ]

    summary = clean_stale_issues(client, days_inactive=30, request_delay=0.0)
    assert summary.inspected_count == 2
    assert summary.skipped_active_count == 1
    assert summary.skipped_inconclusive_count == 1
    assert summary.closed_count == 0
    client.close_issue.assert_not_called()


@patch("gitemployed.stale_cleaner.check_job_liveness")
def test_clean_stale_issues_liveness_exception_handled_gracefully(
    mock_liveness: MagicMock,
) -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 25,
            "title": "Exception Role",
            "labels": [{"name": "ready-to-apply"}],
            "body": "- **Apply URL:** https://example.com/job/25",
        }
    ]
    client.list_comments.return_value = []
    mock_liveness.side_effect = RuntimeError("Parser crashed")

    summary = clean_stale_issues(client, days_inactive=30, request_delay=0.0)
    assert summary.inspected_count == 1
    assert summary.skipped_inconclusive_count == 1
    assert summary.closed_count == 0
    assert summary.evaluations[0].action_taken == "skipped_inconclusive"


@patch("gitemployed.stale_cleaner.check_job_liveness")
def test_clean_stale_issues_dry_run(mock_liveness: MagicMock) -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 30,
            "title": "Closed Role",
            "labels": [{"name": "ready-to-apply"}],
            "body": "- **Apply URL:** https://example.com/job/30",
        }
    ]
    client.list_comments.return_value = []
    mock_liveness.return_value = LivenessResult(
        status=JobLivenessStatus.CLOSED, reason="HTTP 404"
    )

    summary = clean_stale_issues(client, days_inactive=30, dry_run=True)
    assert summary.inspected_count == 1
    assert summary.closed_count == 1
    assert summary.evaluations[0].action_taken == "dry_run_closed"
    client.close_issue.assert_not_called()
    client.post_comment.assert_not_called()
    client.add_labels.assert_not_called()


@patch("gitemployed.stale_cleaner.check_job_liveness")
def test_clean_stale_issues_executes_closure(mock_liveness: MagicMock) -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.project_id = "PVT_123"
    client.search_issues.return_value = [
        {
            "number": 40,
            "title": "Expired Job",
            "node_id": "I_kwDOULLkss40",
            "labels": [{"name": "ready-to-apply"}, {"name": "fit:A"}],
            "body": "- **Apply URL:** https://example.com/job/40",
        }
    ]
    client.list_comments.return_value = []
    mock_liveness.return_value = LivenessResult(
        status=JobLivenessStatus.CLOSED, reason="No longer accepting applications"
    )

    summary = clean_stale_issues(client, days_inactive=30, dry_run=False)
    assert summary.inspected_count == 1
    assert summary.closed_count == 1
    assert summary.evaluations[0].action_taken == "closed"

    # Verify side-effects (label added, no comment, board updated, closed)
    client.add_labels.assert_any_call(40, ["expired"])
    client.remove_label.assert_called_with(40, "ready-to-apply")
    client.post_comment.assert_not_called()
    client.update_project_status.assert_called_with(
        "I_kwDOULLkss40", "Mismatched/Closed"
    )
    client.close_issue.assert_called_with(40)


@patch("gitemployed.stale_cleaner.time.sleep")
@patch("gitemployed.stale_cleaner.check_job_liveness")
def test_clean_stale_issues_paces_requests(
    mock_liveness: MagicMock, mock_sleep: MagicMock
) -> None:
    client = MagicMock(spec=GitHubClient)
    client.repo = "owner/repo"
    client.search_issues.return_value = [
        {
            "number": 1,
            "title": "Job 1",
            "body": "- **Apply URL:** https://example.com/job/1",
            "labels": [{"name": "ready-to-apply"}],
        },
        {
            "number": 2,
            "title": "Job 2",
            "body": "- **Apply URL:** https://example.com/job/2",
            "labels": [{"name": "ready-to-apply"}],
        },
    ]
    client.list_comments.return_value = []
    mock_liveness.return_value = LivenessResult(
        status=JobLivenessStatus.OPEN, reason="Active"
    )

    summary = clean_stale_issues(
        client, days_inactive=30, request_delay=2.5, dry_run=True
    )
    assert summary.inspected_count == 2
    mock_sleep.assert_called_once_with(2.5)
