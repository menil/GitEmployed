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
# the markers only count on short pages. They are also kept specific (full
# phrases from the walls we have seen, not single words like "captcha"), because
# a false positive parks a genuine posting. Unknown walls are caught later by
# the JSON-LD preference in the web client and the model's description_usable.
_BLOCKED_PAGE_MARKERS = (
    # Generic login / bot walls.
    "sign in to view",
    "authwall",
    "access denied",
    "verify you are a human",
    "unusual traffic",
    "enable javascript and cookies to continue",
    # LinkedIn's interstitial: "Checking your browser - reCAPTCHA ... Click
    # here if you are not automatically redirected after 5 seconds."
    "checking your browser before accessing",
    "if you are not automatically redirected",
    # Cloudflare challenge pages.
    "just a moment...",
    "attention required! | cloudflare",
)
_BLOCKED_PAGE_MAX_CHARS = 1500


def looks_like_blocked_page(text: str | None) -> bool:
    """Return True when short fetched text is a login/bot wall, not a posting."""
    lowered = (text or "").lower()
    if len(lowered) > _BLOCKED_PAGE_MAX_CHARS:
        return False
    return any(marker in lowered for marker in _BLOCKED_PAGE_MARKERS)
