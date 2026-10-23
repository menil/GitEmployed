"""Unit tests for the job posting liveness detection engine."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import requests

from gitemployed.liveness import (
    JobLivenessStatus,
    check_job_liveness,
    evaluate_page_content,
    is_redirect_to_search_or_index,
)


def test_invalid_urls_return_inconclusive() -> None:
    for invalid in ("", "   ", "not-a-url", "ftp://foo.com"):
        res = check_job_liveness(invalid)
        assert res.status == JobLivenessStatus.INCONCLUSIVE
        assert "Invalid URL" in res.reason


def test_http_404_and_410_detected_as_closed() -> None:
    session = MagicMock(spec=requests.Session)

    resp_404 = MagicMock(status_code=404)
    session.get.return_value = resp_404
    res = check_job_liveness("https://example.com/job/1", session=session)
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 404
    assert "404" in res.reason

    resp_410 = MagicMock(status_code=410)
    session.get.return_value = resp_410
    res = check_job_liveness("https://example.com/job/2", session=session)
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 410
    assert "410" in res.reason


def test_http_429_and_403_return_inconclusive() -> None:
    session = MagicMock(spec=requests.Session)

    for code in (429, 403, 500, 503, 301):
        resp = MagicMock(status_code=code)
        session.get.return_value = resp
        res = check_job_liveness("https://example.com/job", session=session)
        assert res.status == JobLivenessStatus.INCONCLUSIVE
        assert res.http_status == code


def test_request_exception_and_timeout_return_inconclusive() -> None:
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.Timeout("Connection timed out")

    res = check_job_liveness("https://example.com/job", session=session)
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "timed out" in res.reason

    session.get.side_effect = requests.ConnectionError("DNS failure")
    res = check_job_liveness("https://example.com/job", session=session)
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "DNS failure" in res.reason


def test_empty_content_returns_inconclusive() -> None:
    res = evaluate_page_content(
        "", "https://example.com/job", "https://example.com/job"
    )
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "Empty" in res.reason


def test_blocked_page_returns_inconclusive() -> None:
    blocked_html = (
        "<html><body><h1>Just a moment...</h1>"
        "<p>Checking your browser before accessing</p></body></html>"
    )
    res = evaluate_page_content(
        blocked_html, "https://example.com/job", "https://example.com/job"
    )
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "Blocked" in res.reason


def test_active_open_posting() -> None:
    html = """
    <html>
      <head><title>Senior Engineer at Acme</title></head>
      <body>
        <h1>Senior Engineer</h1>
        <p>Join our engineering team to build scalable services.</p>
        <button>Apply Now</button>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://jobs.lever.co/acme/123",
        "https://jobs.lever.co/acme/123",
    )
    assert res.status == JobLivenessStatus.OPEN
    assert "active" in res.reason.lower()


def test_closure_text_phrases() -> None:
    phrases = [
        "No longer accepting applications",
        "This job is no longer available",
        "This position has been filled",
        "Job posting has expired",
        "This requisition has been closed",
    ]
    for phrase in phrases:
        html = f"""
        <html>
          <body>
            <h1>Engineering Role</h1>
            <div class="alert">{phrase}</div>
          </body>
        </html>
        """
        res = evaluate_page_content(
            html,
            "https://linkedin.com/jobs/view/12345",
            "https://linkedin.com/jobs/view/12345",
        )
        assert res.status == JobLivenessStatus.CLOSED
        assert any(phrase.lower() in sig.lower() for sig in res.signals)


def test_closure_dom_badge() -> None:
    html = """
    <html>
      <body>
        <h1>Staff Engineer</h1>
        <div class="closed-job__header">Closed</div>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://linkedin.com/jobs/view/999",
        "https://linkedin.com/jobs/view/999",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("DOM badge" in sig for sig in res.signals)


def test_json_ld_valid_through_expired() -> None:
    past_date = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
    ).isoformat()
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Backend Dev",
          "validThrough": "{past_date}"
        }}
        </script>
      </head>
      <body><h1>Backend Dev</h1></body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://boards.greenhouse.io/acme/jobs/1",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("validThrough expired" in sig for sig in res.signals)


def test_json_ld_valid_through_future() -> None:
    future_date = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)
    ).isoformat()
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Backend Dev",
          "validThrough": "{future_date}"
        }}
        </script>
      </head>
      <body><h1>Backend Dev</h1></body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://boards.greenhouse.io/acme/jobs/1",
    )
    assert res.status == JobLivenessStatus.OPEN


def test_redirect_to_search_or_index() -> None:
    # LinkedIn redirect to search
    assert is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/search?keywords=test",
    )
    assert is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/",
    )
    # LinkedIn redirect from non-LinkedIn domain ending up on LinkedIn search
    assert is_redirect_to_search_or_index(
        "https://custom-domain.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/search?keywords=test",
    )
    # LinkedIn normal job view is not a search redirect
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/view/12345",
    )

    # ATS redirects where original URL has more path segments than final URL
    assert is_redirect_to_search_or_index(
        "https://boards.greenhouse.io/acme/jobs/12345",
        "https://boards.greenhouse.io/acme",
    )
    assert is_redirect_to_search_or_index(
        "https://jobs.lever.co/acme/abc-123",
        "https://jobs.lever.co/acme",
    )
    assert is_redirect_to_search_or_index(
        "https://jobs.ashbyhq.com/acme/xyz-456",
        "https://jobs.ashbyhq.com/acme",
    )
    assert is_redirect_to_search_or_index(
        "https://apply.workable.com/acme/j/123456",
        "https://apply.workable.com/acme",
    )

    # ATS same or deeper path is not considered a search/index redirect
    assert not is_redirect_to_search_or_index(
        "https://boards.greenhouse.io/acme/jobs/12345",
        "https://boards.greenhouse.io/acme/jobs/12345",
    )
    assert not is_redirect_to_search_or_index(
        "https://jobs.lever.co/acme/abc-123",
        "https://jobs.lever.co/acme/abc-123/apply",
    )


def test_check_job_liveness_e2e_mocked() -> None:
    session = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.linkedin.com/jobs/view/4469806904"
    resp.text = (
        "<html><body><span class='closed-job'>"
        "No longer accepting applications</span></body></html>"
    )
    session.get.return_value = resp

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/4469806904", session=session
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 200
    assert len(res.signals) > 0
