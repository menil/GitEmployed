import pytest

from gitemployed.job_description import (
    NO_DESCRIPTION_PLACEHOLDER,
    is_missing_description,
    looks_like_blocked_page,
)


@pytest.mark.parametrize(
    "description",
    [None, "", "   ", NO_DESCRIPTION_PLACEHOLDER, f"\n{NO_DESCRIPTION_PLACEHOLDER}\n"],
)
def test_is_missing_description_true(description: str | None) -> None:
    assert is_missing_description(description)


def test_is_missing_description_false_for_real_text() -> None:
    assert not is_missing_description("Build backend services in Python.")


@pytest.mark.parametrize(
    "text",
    [
        "Sign in to view this job",
        "Access Denied. You don't have permission to access this page.",
        "Please verify you are a human to continue.",
        # Real text returned for a LinkedIn job URL from a GitHub runner.
        "Checking your browser - reCAPTCHA Checking your browser before "
        "accessing linkedin.com ... Click here if you are not automatically "
        "redirected after 5 seconds.",
        "Just a moment...",
        "Attention Required! | Cloudflare",
    ],
)
def test_looks_like_blocked_page_true(text: str) -> None:
    assert looks_like_blocked_page(text)


def test_looks_like_blocked_page_false_for_real_or_long_text() -> None:
    assert not looks_like_blocked_page("Build backend services in Python.")
    assert not looks_like_blocked_page(None)
    # A long genuine posting that merely mentions signing in is not a wall.
    assert not looks_like_blocked_page("Sign in to view " + "x" * 2000)


@pytest.mark.parametrize(
    "text",
    [
        "Our captcha service team needs an engineer; be there in just a moment.",
        "You will enable JavaScript features and be attentive to detail.",
        "Help customers by checking your browser compatibility matrix.",
    ],
)
def test_looks_like_blocked_page_false_for_short_legit_text(text: str) -> None:
    """Single generic words must not park a genuine (short) posting."""
    assert not looks_like_blocked_page(text)
