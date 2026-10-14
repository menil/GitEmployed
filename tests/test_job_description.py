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
        "Please enable JavaScript and verify you are a human.",
    ],
)
def test_looks_like_blocked_page_true(text: str) -> None:
    assert looks_like_blocked_page(text)


def test_looks_like_blocked_page_false_for_real_or_long_text() -> None:
    assert not looks_like_blocked_page("Build backend services in Python.")
    assert not looks_like_blocked_page(None)
    # A long genuine posting that merely mentions signing in is not a wall.
    assert not looks_like_blocked_page("Sign in to view " + "x" * 2000)
