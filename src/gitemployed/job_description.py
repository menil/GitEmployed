"""Shared handling for job issues that lack a usable description."""

# Written by the scraper when a listing has no description, and recognized by
# triage so the placeholder is never mistaken for real posting text.
NO_DESCRIPTION_PLACEHOLDER = "No job description provided."


def is_missing_description(description: str | None) -> bool:
    """Return True when ``description`` is empty or only the placeholder."""
    stripped = (description or "").strip()
    return not stripped or stripped == NO_DESCRIPTION_PLACEHOLDER


# Fetched pages behind a login or bot wall are non-empty, so without this check
# they would be scored as if they were the posting. Real postings are long, so
# the markers only count on short pages to avoid rejecting a genuine JD that
# merely mentions "sign in".
_BLOCKED_PAGE_MARKERS = (
    "sign in to view",
    "authwall",
    "access denied",
    "enable javascript",
    "verify you are a human",
    "unusual traffic",
)
_BLOCKED_PAGE_MAX_CHARS = 1500


def looks_like_blocked_page(text: str | None) -> bool:
    """Return True when short fetched text is a login/bot wall, not a posting."""
    lowered = (text or "").lower()
    if len(lowered) > _BLOCKED_PAGE_MAX_CHARS:
        return False
    return any(marker in lowered for marker in _BLOCKED_PAGE_MARKERS)
