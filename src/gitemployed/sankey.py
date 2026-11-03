"""Sankey diagram flow extraction and data modeling for job search tracking.

Reconstructs application lifecycle transitions from GitHub Issues and timeline
label events, supporting both 'applied' (traditional job search funnel) and
'full' (discovery, AI triage, and application funnel) scopes.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from gitemployed.github_client import GitHubClient
from gitemployed.status_model import (
    DUPLICATE_LABEL,
    EXPIRED_LABEL,
    MISMATCH_REASON_LABELS,
)

logger = logging.getLogger("sankey")

_ISSUE_PAGE_SIZE = 100

TIMELINE_CANDIDATE_LABELS: frozenset[str] = frozenset(
    {"applied", "in-loop", "offer-received", "rejected"}
)


@dataclass(frozen=True)
class SankeyFlow:
    """A directed edge in the Sankey diagram representing a transition count."""

    source: str
    target: str
    count: int


def _parse_iso(raw: Any) -> dt.datetime | None:
    """Safely parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    if not isinstance(raw, str):
        return None
    try:
        cleaned = raw.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(cleaned)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError, TypeError:
        return None


def _get_issue_labels(issue: dict[str, Any]) -> set[str]:
    """Extract label names from an issue object."""
    labels = issue.get("labels", [])
    result = set()
    for lbl in labels:
        if isinstance(lbl, dict) and "name" in lbl:
            result.add(lbl["name"])
        elif isinstance(lbl, str):
            result.add(lbl)
    return result


def _resolve_primary_mismatch(labels: set[str]) -> str:
    """Determine a single representative mismatch category to maintain flow balance."""
    mismatches = labels & MISMATCH_REASON_LABELS
    if len(mismatches) == 1:
        name = next(iter(mismatches)).replace("-mismatch", "").replace("-", " ").title()
        return f"Mismatch ({name})"
    if len(mismatches) > 1:
        return "Mismatch (Multiple)"
    if DUPLICATE_LABEL in labels:
        return "Duplicate"
    if EXPIRED_LABEL in labels:
        return "Expired"
    return "Mismatched / Filtered"


def _extract_issue_journey(
    issue: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    stale_days: int,
    now: dt.datetime,
) -> tuple[dt.datetime | None, list[str]]:
    """Determine an issue's path through the lifecycle stages.

    Returns:
        (applied_date, stages_path) where stages_path is the ordered sequence
        of nodes traversed by this issue.
    """
    current_labels = _get_issue_labels(issue)
    is_open = issue.get("state") == "open"
    created_at = _parse_iso(issue.get("created_at")) or now
    updated_at = _parse_iso(issue.get("updated_at")) or created_at

    # Track which milestone labels were ever attached and earliest appearance
    milestone_dates: dict[str, dt.datetime] = {}
    for ev in events:
        if ev.get("event") == "labeled":
            lbl = ev.get("label")
            ev_time = _parse_iso(ev.get("created_at")) or now
            if lbl and (lbl not in milestone_dates or ev_time < milestone_dates[lbl]):
                milestone_dates[lbl] = ev_time

    # Also check current labels if events were empty
    for lbl in current_labels:
        if lbl not in milestone_dates:
            milestone_dates[lbl] = created_at

    reached_in_loop = "in-loop" in milestone_dates
    has_offer = "offer-received" in milestone_dates
    has_rejected = "rejected" in milestone_dates
    reached_applied = "applied" in milestone_dates or reached_in_loop or has_offer
    is_mismatched = (
        "triage-mismatched" in milestone_dates
        or bool(current_labels & MISMATCH_REASON_LABELS)
        or DUPLICATE_LABEL in current_labels
    )
    reached_ready = "ready-to-apply" in milestone_dates or reached_applied

    applied_date = (
        milestone_dates.get("applied")
        or milestone_dates.get("in-loop")
        or milestone_dates.get("offer-received")
    )

    # Determine ghosted / inactive status for open roles
    days_inactive = (now - updated_at).total_seconds() / 86400.0
    is_stale = EXPIRED_LABEL in current_labels or (days_inactive >= stale_days)

    path: list[str] = ["Discovered Roles"]

    if not reached_ready and is_mismatched:
        reason = _resolve_primary_mismatch(current_labels | set(milestone_dates.keys()))
        path.append(reason)
        return applied_date, path

    if not reached_ready:
        if is_open and "triage-pending" in current_labels:
            path.append("Triage Pending (Active)")
        elif not is_open:
            path.append("Closed Unprocessed")
        else:
            path.append("Backlog")
        return applied_date, path

    path.append("Ready to Apply")

    if not reached_applied:
        if not is_open:
            path.append("Archived / Skipped")
        else:
            path.append("To Apply (Backlog)")
        return applied_date, path

    # The application funnel
    path.append("Applied")

    if reached_in_loop:
        path.append("In Loop")
        if has_offer:
            path.append("Offer Received")
        elif has_rejected:
            path.append("Rejected")
        elif is_open:
            if is_stale:
                path.append("Loop Stale")
            else:
                path.append("Interviewing (Active)")
        else:
            path.append("Withdrawn / Closed")
    else:
        if has_offer:
            path.append("Offer Received")
        elif has_rejected:
            path.append("Rejected")
        elif is_open:
            if is_stale:
                path.append("Ghosted / Stale")
            else:
                path.append("Awaiting Response (Active)")
        else:
            path.append("Ghosted / Stale")

    return applied_date, path


def build_sankey_flows(
    issues: list[dict[str, Any]],
    events_by_issue: dict[int, list[dict[str, Any]]],
    *,
    scope: str = "applied",
    stale_days: int = 30,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    now: dt.datetime | None = None,
) -> list[SankeyFlow]:
    """Construct Sankey flow links from issues and event history.

    Args:
        issues: List of GitHub issue dictionaries.
        events_by_issue: Mapping from issue number to list of timeline events.
        scope: 'applied' (traditional funnel) or 'full' (discovery & triage).
        stale_days: Days of inactivity before an issue is marked 'Ghosted'.
        start_date: Optional inclusive lower bound on date.
        end_date: Optional inclusive upper bound on date.
        now: Optional current timestamp (defaults to UTC now).

    Returns:
        List of SankeyFlow objects representing transitions and their counts.
    """
    if scope not in ("applied", "full"):
        raise ValueError(f"scope must be 'applied' or 'full', got {scope!r}")
    if stale_days <= 0:
        raise ValueError(f"stale_days must be positive, got {stale_days}")

    effective_now = now or dt.datetime.now(dt.UTC)
    resolved_end_date = end_date or effective_now.date()

    transitions: Counter[tuple[str, str]] = Counter()

    for issue in issues:
        number = issue["number"]
        events = events_by_issue.get(number, [])
        created_at = _parse_iso(issue.get("created_at")) or effective_now
        applied_date, path = _extract_issue_journey(
            issue, events, stale_days=stale_days, now=effective_now
        )

        # Date filtering depends on scope
        if scope == "applied":
            if "Applied" not in path or applied_date is None:
                continue
            event_date = applied_date.astimezone(dt.UTC).date()
            if start_date is not None and event_date < start_date:
                continue
            if event_date > resolved_end_date:
                continue

            # Slice path starting from 'Applied'
            applied_idx = path.index("Applied")
            subpath = path[applied_idx:]
        else:
            event_date = created_at.astimezone(dt.UTC).date()
            if start_date is not None and event_date < start_date:
                continue
            if event_date > resolved_end_date:
                continue
            subpath = path

        # Record each consecutive step in the path
        for src, dst in zip(subpath, subpath[1:], strict=False):
            transitions[(src, dst)] += 1

    return [
        SankeyFlow(source=src, target=dst, count=count)
        for (src, dst), count in transitions.items()
    ]


def list_all_issues(gh_client: GitHubClient) -> list[dict[str, Any]]:
    """Fetch all issues (open and closed), filtering out pull requests."""
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_client.list_issues(state="all", per_page=_ISSUE_PAGE_SIZE, page=page)
        if not batch:
            break
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < _ISSUE_PAGE_SIZE:
            break
        page += 1
    return issues


def _should_fetch_timeline(issue: dict[str, Any], scope: str) -> bool:
    """Determine if an issue requires historical timeline event queries."""
    labels = _get_issue_labels(issue)
    if not labels.isdisjoint(TIMELINE_CANDIDATE_LABELS):
        return True
    if scope == "applied":
        return False
    is_mismatched = (
        "triage-mismatched" in labels
        or bool(labels & MISMATCH_REASON_LABELS)
        or DUPLICATE_LABEL in labels
    )
    if is_mismatched or "triage-pending" in labels:
        return False
    return issue.get("state") != "open"


def _fetch_timeline_events_graphql(
    gh_client: GitHubClient,
    candidate_numbers: list[int],
    batch_size: int = 30,
) -> dict[int, list[dict[str, Any]]]:
    """Fetch timeline events for issues in batches using GraphQL field aliasing."""
    owner, repo_name = gh_client.repo.split("/")
    events_by_issue: dict[int, list[dict[str, Any]]] = {}

    for i in range(0, len(candidate_numbers), batch_size):
        chunk = candidate_numbers[i : i + batch_size]
        fields = []
        for num in chunk:
            fields.append(
                f"""
                i{num}: issue(number: {num}) {{
                  number
                  timelineItems(
                    first: 50, itemTypes: [LABELED_EVENT, UNLABELED_EVENT]
                  ) {{
                    nodes {{
                      __typename
                      ... on LabeledEvent {{
                        createdAt
                        label {{ name }}
                      }}
                      ... on UnlabeledEvent {{
                        createdAt
                        label {{ name }}
                      }}
                    }}
                  }}
                }}
                """
            )
        query = (
            "query($owner: String!, $name: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            + "\n".join(fields)
            + "\n  }\n}"
        )
        res = gh_client._graphql(query, {"owner": owner, "name": repo_name})
        repo_data = (res.get("data") or {}).get("repository") or {}
        for issue_data in repo_data.values():
            if not isinstance(issue_data, dict):
                continue
            num = issue_data.get("number")
            if num is None:
                continue
            nodes = (issue_data.get("timelineItems") or {}).get("nodes") or []
            events: list[dict[str, Any]] = []
            for node in nodes:
                typename = node.get("__typename")
                if typename == "LabeledEvent":
                    ev_type = "labeled"
                elif typename == "UnlabeledEvent":
                    ev_type = "unlabeled"
                else:
                    continue
                lbl = node.get("label") or {}
                lbl_name = lbl.get("name")
                created_at = node.get("createdAt")
                if lbl_name and created_at:
                    events.append(
                        {
                            "event": ev_type,
                            "label": lbl_name,
                            "created_at": created_at,
                        }
                    )
            events_by_issue[num] = events

    return events_by_issue


def fetch_all_timeline_events(
    gh_client: GitHubClient,
    issues: list[dict[str, Any]],
    *,
    scope: str = "applied",
) -> dict[int, list[dict[str, Any]]]:
    """Fetch timeline label events for candidate issues, batching via GraphQL."""
    candidates = [
        issue["number"] for issue in issues if _should_fetch_timeline(issue, scope)
    ]
    logger.info(
        "Found %d total issue(s); pre-filtered to %d candidate(s) for timeline events",
        len(issues),
        len(candidates),
    )
    if not candidates:
        return {}

    events_by_issue: dict[int, list[dict[str, Any]]] = {}

    try:
        events_by_issue = _fetch_timeline_events_graphql(gh_client, candidates)
    except Exception as e:
        logger.warning(
            "GraphQL batch timeline fetch failed (%s); falling back to REST", e
        )
        events_by_issue = {}

    missing = [num for num in candidates if num not in events_by_issue]
    if missing:
        for num in missing:
            events_by_issue[num] = gh_client.list_issue_label_events(num)

    return events_by_issue


def generate_sankey_flows(
    gh_client: GitHubClient,
    *,
    scope: str = "applied",
    stale_days: int = 30,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[SankeyFlow]:
    """Fetch issues and timeline events via GitHubClient and compute flows."""
    issues = list_all_issues(gh_client)
    events_by_issue = fetch_all_timeline_events(gh_client, issues, scope=scope)
    return build_sankey_flows(
        issues,
        events_by_issue,
        scope=scope,
        stale_days=stale_days,
        start_date=start_date,
        end_date=end_date,
    )
