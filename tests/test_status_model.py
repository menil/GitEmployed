"""Unit tests for the canonical status/label model."""

from gitemployed.status_model import (
    ACTIVITY_LABELS,
    CLOSED_MISMATCH_REASON_LABELS,
    CLOSURE_LABELS,
    DUPLICATE_LABEL,
    EXPIRED_LABEL,
    LABEL_TO_STATUS,
    LIFECYCLE_LABELS,
    READY_TO_APPLY_LABEL,
    REVERSE_SYNC_STATUSES,
    STATUS_TO_LABEL,
    WANT_TO_APPLY_LABEL,
    get_updated_lifecycle_labels,
    is_lifecycle_label_satisfied,
    resolve_closed_lifecycle_label,
    sync_lifecycle_label,
)


def test_activity_labels_are_claimant_actions_only() -> None:
    """ACTIVITY_LABELS covers only labels the claimant themselves caused."""
    assert {"applied", "in-loop"} == ACTIVITY_LABELS
    assert ACTIVITY_LABELS <= LIFECYCLE_LABELS
    # Employer-driven outcomes are deliberately excluded (specs/esd-export.md §4).
    assert "rejected" not in ACTIVITY_LABELS
    assert "offer-received" not in ACTIVITY_LABELS


def test_lifecycle_labels_are_complete() -> None:
    assert {
        "triage-pending",
        "ready-to-apply",
        "applied",
        "in-loop",
        "offer-received",
        "rejected",
        "triage-mismatched",
    } == LIFECYCLE_LABELS


def test_label_to_status_mapping() -> None:
    assert LABEL_TO_STATUS == {
        "triage-pending": "Triage Pending",
        "ready-to-apply": "Ready to Apply",
        "applied": "Applied",
        "in-loop": "In Loop",
        "offer-received": "Offer Received",
        "rejected": "Rejected",
        "triage-mismatched": "Mismatched/Closed",
    }


def test_status_to_label_is_exact_inverse() -> None:
    assert {
        status: label for label, status in LABEL_TO_STATUS.items()
    } == STATUS_TO_LABEL
    assert len(STATUS_TO_LABEL) == len(LABEL_TO_STATUS)


def test_reverse_sync_excludes_triage_pending() -> None:
    assert "Triage Pending" not in REVERSE_SYNC_STATUSES
    assert set(STATUS_TO_LABEL) - {"Triage Pending"} == REVERSE_SYNC_STATUSES


class FakeClient:
    """In-memory GitHub client double tracking lifecycle-label side effects."""

    def __init__(self, labels: list[str] | None = None) -> None:
        self.labels = set(labels or [])
        self.removed: list[str] = []
        self.added: list[str] = []

    def get_labels(self, issue_number: int) -> list[str]:
        return list(self.labels)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.labels.discard(label)
        self.removed.append(label)

    def add_labels(self, issue_number: int, labels: list[str]) -> list[str]:
        self.labels.update(labels)
        self.added.extend(labels)
        return list(self.labels)


def test_sync_lifecycle_label_adds_target_and_removes_siblings() -> None:
    client = FakeClient(["triage-pending", "fit:A"])
    sync_lifecycle_label(client, 7, "applied")
    assert client.labels == {"fit:A", "applied"}
    assert client.removed == ["triage-pending"]
    assert client.added == ["applied"]


def test_sync_lifecycle_label_preserves_non_lifecycle_labels() -> None:
    client = FakeClient(["fit:A+", "salary-mismatch"])
    sync_lifecycle_label(client, 3, "ready-to-apply")
    assert client.labels == {"fit:A+", "salary-mismatch", "ready-to-apply"}
    assert client.removed == []


def test_sync_lifecycle_label_noop_when_only_target_present() -> None:
    client = FakeClient(["applied"])
    sync_lifecycle_label(client, 9, "applied")
    assert client.labels == {"applied"}
    assert client.removed == []
    assert client.added == []


def test_sync_lifecycle_label_unknown_target_still_cleans_siblings() -> None:
    client = FakeClient(["applied", "fit:B"])
    sync_lifecycle_label(client, 11, "triage-mismatched")
    assert client.labels == {"fit:B", "triage-mismatched"}


def test_sync_lifecycle_label_accepts_prefetched_labels() -> None:
    """Verify a caller-supplied label set skips the get_labels round-trip."""
    client = FakeClient(["in-loop", "fit:A"])
    fetched = client.get_labels(11)
    client.get_labels = None  # type: ignore[assignment]

    sync_lifecycle_label(client, 11, "applied", set(fetched))

    assert client.labels == {"fit:A", "applied"}
    assert client.removed == ["in-loop"]
    assert client.added == ["applied"]


def test_resolve_closed_lifecycle_label_with_mismatch_reasons() -> None:
    assert resolve_closed_lifecycle_label({"triage-mismatched"}) == "triage-mismatched"
    assert resolve_closed_lifecycle_label({"location-mismatch"}) == "triage-mismatched"
    assert (
        resolve_closed_lifecycle_label({"salary-mismatch", "experience-mismatch"})
        == "triage-mismatched"
    )
    assert resolve_closed_lifecycle_label({"applied"}) == "rejected"
    assert resolve_closed_lifecycle_label(set()) == "rejected"


def test_closure_labels_match_closed_resolution_domain() -> None:
    """CLOSURE_LABELS is exactly the value domain of resolve_closed_lifecycle_label.

    Locks the coupling between the terminal-label set and the closed-issue
    resolver so a change to one without the other fails here instead of
    drifting silently across pipeline modules.
    """
    outcomes = {
        resolve_closed_lifecycle_label({"triage-mismatched"}),
        resolve_closed_lifecycle_label({"location-mismatch"}),
        resolve_closed_lifecycle_label({"applied"}),
        resolve_closed_lifecycle_label(set()),
    }
    assert outcomes == CLOSURE_LABELS


def test_sync_lifecycle_label_skips_generic_mismatch() -> None:
    # Skip adding triage-mismatched if a specific mismatch reason is present.
    client = FakeClient(["location-mismatch"])
    sync_lifecycle_label(client, 12, "triage-mismatched")
    assert client.labels == {"location-mismatch"}
    assert client.added == []
    assert client.removed == []

    # Add triage-mismatched if no specific reason is present.
    client = FakeClient([])
    sync_lifecycle_label(client, 12, "triage-mismatched")
    assert client.labels == {"triage-mismatched"}
    assert client.added == ["triage-mismatched"]
    assert client.removed == []

    # Verify that stale lifecycle labels are removed even when adding the target
    # label is skipped.
    client = FakeClient(["triage-pending", "location-mismatch"])
    sync_lifecycle_label(client, 12, "triage-mismatched")
    assert client.labels == {"location-mismatch"}
    assert "triage-pending" in client.removed
    assert "triage-mismatched" not in client.added


def test_get_updated_lifecycle_labels() -> None:
    # Adding triage-mismatched when specific mismatch label is present
    assert get_updated_lifecycle_labels("triage-mismatched", {"location-mismatch"}) == {
        "location-mismatch"
    }

    # Adding triage-mismatched when no specific mismatch label is present
    assert get_updated_lifecycle_labels("triage-mismatched", set()) == {
        "triage-mismatched"
    }

    # Adding active lifecycle label, removing stale ones
    assert get_updated_lifecycle_labels("applied", {"triage-pending", "fit:A"}) == {
        "applied",
        "fit:A",
    }


def test_duplicate_label_in_closed_mismatch_reasons() -> None:
    assert DUPLICATE_LABEL == "duplicate"
    assert DUPLICATE_LABEL in CLOSED_MISMATCH_REASON_LABELS
    assert DUPLICATE_LABEL not in LIFECYCLE_LABELS


def test_resolve_closed_lifecycle_label_with_duplicate() -> None:
    assert resolve_closed_lifecycle_label({"duplicate"}) == "triage-mismatched"
    assert resolve_closed_lifecycle_label({"duplicate", "fit:B"}) == "triage-mismatched"
    assert (
        resolve_closed_lifecycle_label({"duplicate", "ready-to-apply"})
        == "triage-mismatched"
    )


def test_is_lifecycle_label_satisfied_with_duplicate() -> None:
    assert is_lifecycle_label_satisfied("triage-mismatched", {"duplicate"}) is True
    assert (
        is_lifecycle_label_satisfied("triage-mismatched", {"duplicate", "fit:A"})
        is True
    )
    # Siblings from LIFECYCLE_LABELS violate satisfaction
    assert (
        is_lifecycle_label_satisfied(
            "triage-mismatched", {"duplicate", "ready-to-apply"}
        )
        is False
    )


def test_sync_lifecycle_label_skips_generic_mismatch_for_duplicate() -> None:
    client = FakeClient(["duplicate"])
    sync_lifecycle_label(client, 15, "triage-mismatched")
    assert client.labels == {"duplicate"}
    assert client.added == []
    assert client.removed == []

    # Stale lifecycle label is removed while duplicate is preserved
    client = FakeClient(["ready-to-apply", "duplicate"])
    sync_lifecycle_label(client, 15, "triage-mismatched")
    assert client.labels == {"duplicate"}
    assert "ready-to-apply" in client.removed
    assert "triage-mismatched" not in client.added


def test_get_updated_lifecycle_labels_preserves_duplicate() -> None:
    assert get_updated_lifecycle_labels("triage-mismatched", {"duplicate"}) == {
        "duplicate"
    }
    assert get_updated_lifecycle_labels(
        "triage-mismatched", {"ready-to-apply", "duplicate"}
    ) == {"duplicate"}


def test_expired_label_in_closed_mismatch_reasons() -> None:
    assert EXPIRED_LABEL == "expired"
    assert EXPIRED_LABEL in CLOSED_MISMATCH_REASON_LABELS
    assert EXPIRED_LABEL not in LIFECYCLE_LABELS


def test_resolve_closed_lifecycle_label_with_expired() -> None:
    assert resolve_closed_lifecycle_label({"expired"}) == "triage-mismatched"
    assert resolve_closed_lifecycle_label({"expired", "fit:B"}) == "triage-mismatched"
    assert (
        resolve_closed_lifecycle_label({"expired", "ready-to-apply"})
        == "triage-mismatched"
    )


def test_is_lifecycle_label_satisfied_with_expired() -> None:
    assert is_lifecycle_label_satisfied("triage-mismatched", {"expired"}) is True
    assert (
        is_lifecycle_label_satisfied("triage-mismatched", {"expired", "fit:A"}) is True
    )
    # Siblings from LIFECYCLE_LABELS violate satisfaction
    assert (
        is_lifecycle_label_satisfied("triage-mismatched", {"expired", "ready-to-apply"})
        is False
    )


def test_sync_lifecycle_label_skips_generic_mismatch_for_expired() -> None:
    client = FakeClient(["expired"])
    sync_lifecycle_label(client, 16, "triage-mismatched")
    assert client.labels == {"expired"}
    assert client.added == []
    assert client.removed == []

    # Stale lifecycle label is removed while expired is preserved
    client = FakeClient(["ready-to-apply", "expired"])
    sync_lifecycle_label(client, 16, "triage-mismatched")
    assert client.labels == {"expired"}
    assert "ready-to-apply" in client.removed
    assert "triage-mismatched" not in client.added


def test_get_updated_lifecycle_labels_preserves_expired() -> None:
    assert get_updated_lifecycle_labels("triage-mismatched", {"expired"}) == {"expired"}
    assert get_updated_lifecycle_labels(
        "triage-mismatched", {"ready-to-apply", "expired"}
    ) == {"expired"}


def test_want_to_apply_label_constant() -> None:
    """Verify want-to-apply label constant and non-lifecycle status."""
    assert WANT_TO_APPLY_LABEL == "want-to-apply"
    assert READY_TO_APPLY_LABEL == "ready-to-apply"
    assert WANT_TO_APPLY_LABEL not in LIFECYCLE_LABELS


def test_sync_lifecycle_label_removes_want_to_apply() -> None:
    """Verify want-to-apply is removed whenever ready-to-apply is removed."""
    client = FakeClient(["ready-to-apply", "want-to-apply", "fit:A"])
    sync_lifecycle_label(client, 20, "applied")
    assert client.labels == {"fit:A", "applied"}
    assert "ready-to-apply" in client.removed
    assert "want-to-apply" in client.removed
    assert "applied" in client.added


def test_sync_lifecycle_label_preserves_want_to_apply_on_ready_target() -> None:
    """Verify want-to-apply is preserved when target is ready-to-apply."""
    client = FakeClient(["triage-pending", "want-to-apply", "fit:A"])
    sync_lifecycle_label(client, 21, "ready-to-apply")
    assert client.labels == {"fit:A", "want-to-apply", "ready-to-apply"}
    assert client.removed == ["triage-pending"]
    assert "ready-to-apply" in client.added


def test_sync_lifecycle_label_preserves_want_to_apply_when_ready_absent() -> None:
    """Verify want-to-apply is preserved when ready-to-apply is absent."""
    client = FakeClient(["in-loop", "want-to-apply", "fit:A"])
    sync_lifecycle_label(client, 22, "applied")
    assert client.labels == {"fit:A", "want-to-apply", "applied"}
    assert client.removed == ["in-loop"]


def test_get_updated_lifecycle_labels_with_want_to_apply() -> None:
    """Verify get_updated_lifecycle_labels evicts or preserves want-to-apply."""
    # When ready-to-apply is removed, want-to-apply is discarded
    assert get_updated_lifecycle_labels(
        "applied", {"ready-to-apply", "want-to-apply", "fit:A"}
    ) == {"applied", "fit:A"}

    # When target is ready-to-apply, want-to-apply is preserved
    assert get_updated_lifecycle_labels(
        "ready-to-apply", {"triage-pending", "want-to-apply", "fit:A"}
    ) == {"ready-to-apply", "want-to-apply", "fit:A"}

    # When ready-to-apply was not present, want-to-apply is preserved
    assert get_updated_lifecycle_labels(
        "applied", {"in-loop", "want-to-apply", "fit:A"}
    ) == {"applied", "want-to-apply", "fit:A"}
