"""Unit tests for the job posting liveness detection engine."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
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

    resp_404 = MagicMock(status_code=404, url="https://example.com/job/1")
    session.get.return_value = resp_404
    res = check_job_liveness("https://example.com/job/1", session=session)
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 404
    assert "404" in res.reason

    resp_410 = MagicMock(status_code=410, url="https://example.com/job/2")
    session.get.return_value = resp_410
    res = check_job_liveness("https://example.com/job/2", session=session)
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 410
    assert "410" in res.reason


def test_http_429_and_403_return_inconclusive() -> None:
    session = MagicMock(spec=requests.Session)

    for code in (429, 403, 500, 503, 301):
        resp = MagicMock(status_code=code, url="https://example.com/job")
        session.get.return_value = resp
        res = check_job_liveness(
            "https://example.com/job", session=session, max_retries_on_rate_limit=0
        )
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
    # LinkedIn expired JD redirect parameter
    assert is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/4461786754",
        "https://www.linkedin.com/jobs/senior-software-engineer-contract-jobs?trk=expired_jd_redirect",
    )
    # LinkedIn redirect to category / directory page away from job view
    assert is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/4461744897",
        "https://www.linkedin.com/jobs/j2ee-developer-jobs",
    )
    # LinkedIn redirect from non-LinkedIn domain ending up on LinkedIn search
    assert is_redirect_to_search_or_index(
        "https://custom-domain.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/search?keywords=test",
    )
    # LinkedIn normal job view is not a search redirect (including slugged URLs)
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/view/12345",
    )
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/view/senior-software-architect-12345",
    )
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/jobs/view/12345?refId=abc",
    )
    # LinkedIn redirecting to external ATS is not a search redirect
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://boards.greenhouse.io/acme/jobs/12345",
    )
    # Generic aggregator landing on LinkedIn /jobs
    assert is_redirect_to_search_or_index(
        "https://aggregator.com/apply/123",
        "https://www.linkedin.com/jobs",
    )
    # LinkedIn authwall is not a search redirect
    assert not is_redirect_to_search_or_index(
        "https://www.linkedin.com/jobs/view/12345",
        "https://www.linkedin.com/authwall?trk=rip",
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


def test_check_job_liveness_authwall_redirect() -> None:
    session = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.linkedin.com/authwall?trk=rip"
    resp.text = "<html><body>Please sign in</body></html>"
    session.get.return_value = resp

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/12345", session=session
    )
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "login or authentication wall" in res.reason


def test_check_job_liveness_expired_jd_redirect() -> None:
    session = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.linkedin.com/jobs/senior-software-engineer-contract-jobs?trk=expired_jd_redirect"
    resp.text = "<html><body><h1>11,000+ Open Roles</h1></body></html>"
    session.get.return_value = resp

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/4461786754", session=session
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert "Redirected from job view" in res.reason


def test_check_job_liveness_rate_limit_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    session = MagicMock(spec=requests.Session)

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.url = "https://www.linkedin.com/jobs/view/12345"

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.url = "https://www.linkedin.com/jobs/view/12345"
    resp_200.text = "<html><body><h1>Software Engineer</h1></body></html>"

    session.get.side_effect = [resp_429, resp_200]

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/12345",
        session=session,
        max_retries_on_rate_limit=1,
        retry_delay=0.01,
    )
    assert res.status == JobLivenessStatus.OPEN
    assert session.get.call_count == 2


def test_check_job_liveness_rate_limit_retry_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    session = MagicMock(spec=requests.Session)

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.url = "https://www.linkedin.com/jobs/view/12345"
    session.get.return_value = resp_429

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/12345",
        session=session,
        max_retries_on_rate_limit=1,
        retry_delay=0.01,
    )
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "429" in res.reason
    assert session.get.call_count == 2


def test_check_job_liveness_processing_exception() -> None:
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = ValueError("Broken parser")

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/12345", session=session
    )
    assert res.status == JobLivenessStatus.INCONCLUSIVE
    assert "Processing error" in res.reason


def test_linkedin_missing_apply_cta_detected_as_closed() -> None:
    # Simulates LinkedIn job posting (e.g. #4469917527) where topcard renders
    # but CTA container is empty (comments only, no apply button or link).
    html = """
    <html>
      <body>
        <section class="top-card-layout container-lined">
          <div class="top-card-layout__entity-info">
            <h1>Senior AI Engineer</h1>
          </div>
          <div class="top-card-layout__cta-container">
            <!-- -->
            <!-- -->
          </div>
        </section>
        <div>Job description details...</div>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/4469917527/",
        "https://www.linkedin.com/jobs/view/4469917527/",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("no apply CTA in topcard" in sig for sig in res.signals)


def test_linkedin_topcard_with_only_secondary_save_detected_as_closed() -> None:
    # Topcard has a Save button but no primary apply CTA or apply action
    html = """
    <html>
      <body>
        <section class="top-card-layout">
          <h1>Staff Infrastructure Engineer</h1>
          <div class="top-card-layout__cta-container">
            <button class="top-card-layout__cta top-card-layout__cta--secondary">
              Save
            </button>
          </div>
        </section>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/senior-staff-999999",
        "https://www.linkedin.com/jobs/view/senior-staff-999999",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("no apply CTA in topcard" in sig for sig in res.signals)


def test_linkedin_missing_apply_cta_with_applied_company_link_detected_as_closed() -> (
    None
):
    # Closed posting at a company whose name starts with 'Applied'
    # (e.g. Applied Materials) must not be falsely treated as active.
    html = """
    <html>
      <body>
        <section class="top-card-layout">
          <a class="topcard__org-name-link" href="https://linkedin.com/company/applied-materials">
            Applied Materials
          </a>
          <div class="top-card-layout__cta-container">
            <!-- -->
          </div>
        </section>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/123456789",
        "https://www.linkedin.com/jobs/view/123456789",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("no apply CTA in topcard" in sig for sig in res.signals)


def test_linkedin_disabled_primary_cta_detected_as_closed() -> None:
    # Primary CTA rendered in disabled state indicates closed applications
    html = """
    <html>
      <body>
        <section class="top-card-layout">
          <div class="top-card-layout__cta-container">
            <button class="top-card-layout__cta top-card-layout__cta--primary" disabled>
              Applications Closed
            </button>
          </div>
        </section>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/5551234",
        "https://www.linkedin.com/jobs/view/5551234",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("no apply CTA in topcard" in sig for sig in res.signals)


def test_linkedin_aria_disabled_primary_cta_detected_as_closed() -> None:
    # Primary CTA with aria-disabled="true" indicates closed applications
    html = """
    <html>
      <body>
        <section class="top-card-layout">
          <div class="top-card-layout__cta-container">
            <a class="top-card-layout__cta top-card-layout__cta--primary"
               aria-disabled="true">
              Apply
            </a>
          </div>
        </section>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/5559876",
        "https://www.linkedin.com/jobs/view/5559876",
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert any("no apply CTA in topcard" in sig for sig in res.signals)


def test_linkedin_with_primary_apply_cta_evaluated_as_open() -> None:
    html = """
    <html>
      <body>
        <section class="top-card-layout">
          <h1>Applied AI Engineer</h1>
          <div class="top-card-layout__cta-container">
            <button class="top-card-layout__cta top-card-layout__cta--primary">
              Apply
            </button>
            <button class="top-card-layout__cta top-card-layout__cta--secondary">
              Save
            </button>
          </div>
        </section>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/4419969671",
        "https://www.linkedin.com/jobs/view/4419969671",
    )
    assert res.status == JobLivenessStatus.OPEN
    assert "active" in res.reason.lower()


def test_linkedin_with_easy_apply_or_tracking_evaluated_as_open() -> None:
    html = """
    <html>
      <body>
        <div class="top-card-layout__card">
          <h1>Platform Architect</h1>
          <div class="top-card-layout__cta-container">
            <button data-tracking-control-name="public_jobs_apply-button">
              Easy Apply
            </button>
          </div>
        </div>
      </body>
    </html>
    """
    res = evaluate_page_content(
        html,
        "https://www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com/jobs/view/123456",
    )
    assert res.status == JobLivenessStatus.OPEN


def test_linkedin_missing_apply_cta_ignored_for_non_job_view_or_non_linkedin() -> None:
    # Non-LinkedIn ATS with a topcard-like class name should not be closed by this rule
    html = """
    <html>
      <body>
        <div class="top-card-layout">
          <h1>Careers at Acme</h1>
        </div>
      </body>
    </html>
    """
    res_ats = evaluate_page_content(
        html,
        "https://boards.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/123",
    )
    assert res_ats.status == JobLivenessStatus.OPEN

    # LinkedIn non-view URL (e.g. feed/search) should not trigger this rule
    res_non_view = evaluate_page_content(
        html,
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/feed/",
    )
    assert res_non_view.status == JobLivenessStatus.OPEN

    # LinkedIn job view without topcard DOM elements should not assume closed
    plain_html = "<html><body><h1>Minimal Posting</h1></body></html>"
    res_plain = evaluate_page_content(
        plain_html,
        "https://www.linkedin.com/jobs/view/123",
        "https://www.linkedin.com/jobs/view/123",
    )
    assert res_plain.status == JobLivenessStatus.OPEN


def test_check_job_liveness_linkedin_closed_no_cta_e2e() -> None:
    session = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://www.linkedin.com/jobs/view/4469917527/"
    resp.text = """
    <html>
      <body>
        <section class="top-card-layout">
          <h1>Senior Software Engineer II, Applied AI</h1>
          <div class="top-card-layout__cta-container">
            <!-- -->
          </div>
        </section>
      </body>
    </html>
    """
    session.get.return_value = resp

    res = check_job_liveness(
        "https://www.linkedin.com/jobs/view/4469917527/", session=session
    )
    assert res.status == JobLivenessStatus.CLOSED
    assert res.http_status == 200
    assert any("no apply CTA in topcard" in sig for sig in res.signals)
