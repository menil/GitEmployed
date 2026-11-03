"""Unit tests for the Sankey flow extraction engine."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from gitemployed.sankey import (
    SankeyFlow,
    _fetch_timeline_events_graphql,
    _parse_iso,
    _resolve_primary_mismatch,
    _should_fetch_timeline,
    build_sankey_flows,
    format_csv,
    format_flows,
    format_json,
    format_mermaid,
    format_sankeymatic,
    generate_sankey_flows,
)


def _issue(
    number: int,
    *,
    labels: list[str] | None = None,
    state: str = "open",
    created_at: str = "2026-01-01T00:00:00Z",
    updated_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    label_objs = [{"name": lbl} for lbl in (labels or [])]
    return {
        "number": number,
        "title": f"Job {number}",
        "state": state,
        "labels": label_objs,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _labeled_event(label: str, created_at: str) -> dict:
    return {"event": "labeled", "label": label, "created_at": created_at}


def test_parse_iso() -> None:
    assert _parse_iso(None) is None
    assert _parse_iso(123) is None
    assert _parse_iso("invalid-date") is None

    parsed = _parse_iso("2026-01-15T12:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo == dt.UTC
    assert parsed.year == 2026

    # Naive timestamp gets assigned UTC
    parsed_naive = _parse_iso("2026-01-15T12:00:00")
    assert parsed_naive is not None
    assert parsed_naive.tzinfo == dt.UTC


def test_resolve_primary_mismatch() -> None:
    assert _resolve_primary_mismatch({"salary-mismatch"}) == "Mismatch (Salary)"
    assert (
        _resolve_primary_mismatch({"salary-mismatch", "tech-stack-mismatch"})
        == "Mismatch (Multiple)"
    )
    assert _resolve_primary_mismatch({"duplicate"}) == "Duplicate"
    assert _resolve_primary_mismatch({"expired"}) == "Expired"
    assert _resolve_primary_mismatch({"triage-mismatched"}) == "Mismatched / Filtered"


def test_validation_errors() -> None:
    with pytest.raises(ValueError, match="scope must be 'applied' or 'full'"):
        build_sankey_flows([], {}, scope="unknown")

    with pytest.raises(ValueError, match="stale_days must be positive"):
        build_sankey_flows([], {}, stale_days=0)


def test_applied_funnel_offer_and_rejection() -> None:
    issues = [
        _issue(1, labels=["offer-received"]),
        _issue(2, labels=["rejected"]),
        # Direct offer without in-loop
        _issue(3, labels=["offer-received"]),
    ]
    events = {
        1: [
            _labeled_event("applied", "2026-01-02T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-10T00:00:00Z"),
            _labeled_event("offer-received", "2026-01-20T00:00:00Z"),
        ],
        2: [
            _labeled_event("applied", "2026-01-02T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-05T00:00:00Z"),
            _labeled_event("rejected", "2026-01-12T00:00:00Z"),
        ],
        3: [
            _labeled_event("applied", "2026-01-03T00:00:00Z"),
            _labeled_event("offer-received", "2026-01-07T00:00:00Z"),
        ],
    }

    flows = build_sankey_flows(issues, events, scope="applied")
    flow_map = {(f.source, f.target): f.count for f in flows}

    assert flow_map.get(("Applied", "In Loop")) == 2
    assert flow_map.get(("In Loop", "Offer Received")) == 1
    assert flow_map.get(("In Loop", "Rejected")) == 1
    assert flow_map.get(("Applied", "Offer Received")) == 1


def test_applied_funnel_direct_rejection_and_active() -> None:
    now = dt.datetime(2026, 1, 15, 0, 0, tzinfo=dt.UTC)
    issues = [
        # Rejected with no interview
        _issue(1, labels=["rejected"], created_at="2026-01-01T00:00:00Z"),
        # Active awaiting response (updated 2 days ago)
        _issue(
            2,
            labels=["applied"],
            state="open",
            created_at="2026-01-10T00:00:00Z",
            updated_at="2026-01-13T00:00:00Z",
        ),
        # Ghosted (updated 40 days ago, default stale_days=30)
        _issue(
            3,
            labels=["applied"],
            state="open",
            created_at="2025-11-01T00:00:00Z",
            updated_at="2025-11-15T00:00:00Z",
        ),
    ]
    events = {
        1: [
            _labeled_event("applied", "2026-01-02T00:00:00Z"),
            _labeled_event("rejected", "2026-01-08T00:00:00Z"),
        ],
        2: [_labeled_event("applied", "2026-01-10T00:00:00Z")],
        3: [_labeled_event("applied", "2025-11-01T00:00:00Z")],
    }

    flows = build_sankey_flows(issues, events, scope="applied", now=now)
    flow_map = {(f.source, f.target): f.count for f in flows}

    assert flow_map.get(("Applied", "Rejected")) == 1
    assert flow_map.get(("Applied", "Awaiting Response (Active)")) == 1
    assert flow_map.get(("Applied", "Ghosted / Stale")) == 1


def test_applied_funnel_implied_applied_from_in_loop() -> None:
    """Issue reaching in-loop without explicit applied label implies applied."""
    now = dt.datetime(2026, 1, 20, 0, 0, tzinfo=dt.UTC)
    issues = [
        _issue(
            1,
            labels=["in-loop"],
            state="open",
            updated_at="2026-01-18T00:00:00Z",
        ),
    ]
    events = {
        1: [
            _labeled_event("ready-to-apply", "2026-01-05T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-10T00:00:00Z"),
        ],
    }
    flows = build_sankey_flows(issues, events, scope="applied", now=now)
    flow_map = {(f.source, f.target): f.count for f in flows}
    assert flow_map.get(("Applied", "In Loop")) == 1
    assert flow_map.get(("In Loop", "Interviewing (Active)")) == 1


def test_applied_funnel_in_loop_active_and_stale() -> None:
    now = dt.datetime(2026, 1, 20, 0, 0, tzinfo=dt.UTC)
    issues = [
        # In loop, active
        _issue(
            1,
            labels=["in-loop"],
            state="open",
            updated_at="2026-01-18T00:00:00Z",
        ),
        # In loop, stale
        _issue(
            2,
            labels=["in-loop"],
            state="open",
            updated_at="2025-12-01T00:00:00Z",
        ),
        # In loop, closed without offer or reject (withdrawn)
        _issue(
            3,
            labels=["in-loop"],
            state="closed",
            updated_at="2026-01-05T00:00:00Z",
        ),
    ]
    events = {
        1: [
            _labeled_event("applied", "2026-01-02T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-10T00:00:00Z"),
        ],
        2: [
            _labeled_event("applied", "2025-11-01T00:00:00Z"),
            _labeled_event("in-loop", "2025-11-10T00:00:00Z"),
        ],
        3: [
            _labeled_event("applied", "2026-01-01T00:00:00Z"),
            _labeled_event("in-loop", "2026-01-03T00:00:00Z"),
        ],
    }

    flows = build_sankey_flows(issues, events, scope="applied", now=now)
    flow_map = {(f.source, f.target): f.count for f in flows}

    assert flow_map.get(("In Loop", "Interviewing (Active)")) == 1
    assert flow_map.get(("In Loop", "Loop Stale")) == 1
    assert flow_map.get(("In Loop", "Withdrawn / Closed")) == 1


def test_date_filtering() -> None:
    issues = [
        _issue(1, labels=["applied"]),
        _issue(2, labels=["applied"]),
        _issue(3, labels=["applied"]),
    ]
    events = {
        1: [_labeled_event("applied", "2026-01-05T00:00:00Z")],
        2: [_labeled_event("applied", "2026-01-15T00:00:00Z")],
        3: [_labeled_event("applied", "2026-01-25T00:00:00Z")],
    }

    flows = build_sankey_flows(
        issues,
        events,
        scope="applied",
        start_date=dt.date(2026, 1, 10),
        end_date=dt.date(2026, 1, 20),
    )
    # Only issue 2 falls inside the window
    total_applied = sum(f.count for f in flows if f.source == "Applied")
    assert total_applied == 1


def test_full_funnel_transitions() -> None:
    issues = [
        # Mismatched role
        _issue(
            1,
            labels=["salary-mismatch", "triage-mismatched"],
            created_at="2026-01-01T00:00:00Z",
        ),
        # Triage pending
        _issue(
            2,
            labels=["triage-pending"],
            state="open",
            created_at="2026-01-02T00:00:00Z",
        ),
        # Ready to apply, skipped
        _issue(
            3,
            labels=["ready-to-apply"],
            state="closed",
            created_at="2026-01-03T00:00:00Z",
        ),
        # Ready to apply, in backlog
        _issue(
            4,
            labels=["ready-to-apply"],
            state="open",
            created_at="2026-01-04T00:00:00Z",
        ),
        # Applied
        _issue(
            5,
            labels=["applied"],
            created_at="2026-01-05T00:00:00Z",
        ),
        # Closed unprocessed
        _issue(
            6,
            labels=[],
            state="closed",
            created_at="2026-01-06T00:00:00Z",
        ),
        # Open backlog without triage-pending
        _issue(
            7,
            labels=[],
            state="open",
            created_at="2026-01-07T00:00:00Z",
        ),
    ]
    events = {
        5: [
            _labeled_event("ready-to-apply", "2026-01-05T00:00:00Z"),
            _labeled_event("applied", "2026-01-06T00:00:00Z"),
        ],
    }

    flows = build_sankey_flows(issues, events, scope="full")
    flow_map = {(f.source, f.target): f.count for f in flows}

    assert flow_map.get(("Discovered Roles", "Mismatch (Salary)")) == 1
    assert flow_map.get(("Discovered Roles", "Triage Pending (Active)")) == 1
    assert flow_map.get(("Discovered Roles", "Ready to Apply")) == 3
    assert flow_map.get(("Discovered Roles", "Closed Unprocessed")) == 1
    assert flow_map.get(("Discovered Roles", "Backlog")) == 1
    assert flow_map.get(("Ready to Apply", "Archived / Skipped")) == 1
    assert flow_map.get(("Ready to Apply", "To Apply (Backlog)")) == 1
    assert flow_map.get(("Ready to Apply", "Applied")) == 1


def test_should_fetch_timeline() -> None:
    # Applied candidate
    assert _should_fetch_timeline(_issue(1, labels=["applied"]), "applied") is True
    assert _should_fetch_timeline(_issue(2, labels=["in-loop"]), "applied") is True
    assert _should_fetch_timeline(_issue(3, labels=["rejected"]), "applied") is True
    assert (
        _should_fetch_timeline(_issue(4, labels=["offer-received"]), "applied") is True
    )

    # Non-applied in applied scope
    assert (
        _should_fetch_timeline(_issue(5, labels=["triage-mismatched"]), "applied")
        is False
    )
    assert (
        _should_fetch_timeline(_issue(6, labels=["triage-pending"]), "applied") is False
    )
    assert (
        _should_fetch_timeline(_issue(7, labels=["ready-to-apply"]), "applied") is False
    )

    # Full scope
    assert (
        _should_fetch_timeline(_issue(8, labels=["salary-mismatch"]), "full") is False
    )
    assert _should_fetch_timeline(_issue(9, labels=["triage-pending"]), "full") is False
    assert (
        _should_fetch_timeline(
            _issue(10, labels=["ready-to-apply"], state="open"), "full"
        )
        is False
    )
    assert _should_fetch_timeline(_issue(11, labels=[], state="closed"), "full") is True


def test_fetch_timeline_events_graphql() -> None:
    client = MagicMock()
    client.repo = "owner/repo"
    client._graphql.return_value = {
        "data": {
            "repository": {
                "i1": {
                    "number": 1,
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-01-01T00:00:00Z",
                                "label": {"name": "applied"},
                            },
                            {
                                "__typename": "UnlabeledEvent",
                                "createdAt": "2026-01-02T00:00:00Z",
                                "label": {"name": "applied"},
                            },
                            {"__typename": "IssueComment"},
                        ]
                    },
                }
            }
        }
    }

    events = _fetch_timeline_events_graphql(client, [1])
    assert 1 in events
    assert len(events[1]) == 2
    assert events[1][0]["label"] == "applied"
    assert events[1][0]["event"] == "labeled"
    assert events[1][1]["event"] == "unlabeled"


def test_generate_sankey_flows_with_client() -> None:
    client = MagicMock()
    client.repo = "owner/repo"
    client._graphql.side_effect = Exception("GraphQL unavailable")
    client.list_issues.side_effect = [
        [
            {
                "number": 1,
                "title": "Role 1",
                "state": "open",
                "labels": [{"name": "applied"}],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            # pull request should be filtered out
            {"number": 2, "pull_request": {}},
        ],
        [],
    ]
    client.list_issue_label_events.return_value = [
        {"event": "labeled", "label": "applied", "created_at": "2026-01-01T00:00:00Z"}
    ]

    flows = generate_sankey_flows(client, scope="applied")
    assert isinstance(flows, list)
    assert len(flows) > 0
    assert flows[0].source == "Applied"


def test_format_sankeymatic() -> None:
    assert format_sankeymatic([]) == "// No matching job applications found"

    flows = [
        SankeyFlow("Applied", "In Loop", 3),
        SankeyFlow("In Loop", "Offer Received", 1),
    ]
    out = format_sankeymatic(flows)
    assert "Applied [3] In Loop" in out
    assert "In Loop [1] Offer Received" in out


def test_format_mermaid() -> None:
    empty_out = format_mermaid([])
    assert "nodeAlignment: left" in empty_out
    assert "%% No matching job applications found" in empty_out

    flows = [
        SankeyFlow("Applied", "In Loop", 3),
        SankeyFlow("In Loop", "Offer Received", 1),
    ]
    out = format_mermaid(flows)
    assert "nodeAlignment: left" in out
    assert "sankey-beta" in out
    assert "Applied, In Loop, 3" in out
    assert "In Loop, Offer Received, 1" in out

    # Node names with commas or quotes are properly escaped
    special_flows = [
        SankeyFlow("O'Reilly", 'Role "Lead", Senior', 2),
    ]
    special_out = format_mermaid(special_flows)
    assert "O'Reilly" in special_out
    assert '"Role ""Lead"", Senior"' in special_out


def test_format_json_and_csv() -> None:
    flows = [
        SankeyFlow("Applied", "In Loop", 3),
    ]
    json_out = format_json(flows)
    assert '"source": "Applied"' in json_out
    assert '"count": 3' in json_out

    csv_out = format_csv(flows)
    assert "source,target,count" in csv_out
    assert "Applied,In Loop,3" in csv_out


def test_format_flows_dispatch() -> None:
    flows = [SankeyFlow("Applied", "In Loop", 1)]
    assert "[" in format_flows(flows, "sankeymatic")
    assert "sankey-beta" in format_flows(flows, "mermaid")
    assert '"source"' in format_flows(flows, "json")
    assert "source,target,count" in format_flows(flows, "csv")

    with pytest.raises(ValueError, match="Unsupported format 'xml'"):
        format_flows(flows, "xml")
