"""Job posting liveness and closure detection engine.

Evaluates whether an existing job listing URL is active, closed/expired,
or inconclusive (due to rate limits, login walls, or network errors).
"""

from __future__ import annotations

import datetime
import enum
import json
import logging
import re
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests
from bs4 import BeautifulSoup

from gitemployed.job_description import looks_like_blocked_page

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class JobLivenessStatus(enum.StrEnum):
    """Categorical status of a job posting's availability."""

    OPEN = "open"
    CLOSED = "closed"
    INCONCLUSIVE = "inconclusive"


@dataclass
class LivenessResult:
    """Outcome of evaluating a job listing's availability."""

    status: JobLivenessStatus
    reason: str
    http_status: int | None = None
    signals: list[str] = field(default_factory=list)


# Text phrases that explicitly indicate a role is no longer available.
CLOSED_PHRASES: tuple[str, ...] = (
    "no longer accepting applications",
    "this job is no longer available",
    "this job is closed",
    "position has been filled",
    "this position has been filled",
    "job posting has expired",
    "this job posting has expired",
    "this position is no longer accepting applications",
    "we're sorry, this job has expired",
    "job is no longer open",
    "this requisition has been closed",
    "applications are now closed",
    "the role you are looking for has been closed",
    "this vacancy has expired",
)

# Regex to detect LinkedIn job view paths (including slugged URLs) vs search/index paths
LINKEDIN_VIEW_RE = re.compile(r"^/jobs/view/(?:[^/?#]*-)?\d+", re.IGNORECASE)

# Regex to match 'apply' as an isolated word (preventing false matches on 'Applied')
_APPLY_WORD_RE = re.compile(r"\bapply\b", re.IGNORECASE)

_ATS_HOSTS: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
)

_CLOSED_BADGE_CLASSES: tuple[str, ...] = ("closed-job", "no-longer-accepting")

AUTH_WALL_PATHS: tuple[str, ...] = (
    "/authwall",
    "/login",
    "/checkpoint",
    "/uas/login",
)


def _is_auth_wall_path(path: str) -> bool:
    """Check whether a URL path represents a login wall or checkpoint."""
    clean_path = path.lower().rstrip("/")
    return any(
        clean_path == p or clean_path.startswith(f"{p}/") for p in AUTH_WALL_PATHS
    )


def _is_linkedin_host(host: str) -> bool:
    """Check if hostname is linkedin.com or a subdomain of linkedin.com."""
    clean = host.lower().removeprefix("www.")
    return clean == "linkedin.com" or clean.endswith(".linkedin.com")


def _is_ats_host(host: str) -> bool:
    """Check if hostname belongs to a known Applicant Tracking System."""
    clean = host.lower().removeprefix("www.")
    return any(clean == ats or clean.endswith(f".{ats}") for ats in _ATS_HOSTS)


def _is_closed_badge_class(class_val: Any) -> bool:
    """Check whether an HTML class attribute contains closure badge markers."""
    if not class_val:
        return False
    classes = class_val if isinstance(class_val, list) else [str(class_val)]
    return any(
        any(badge in str(c).lower() for badge in _CLOSED_BADGE_CLASSES) for c in classes
    )


def is_redirect_to_search_or_index(original_url: str, final_url: str) -> bool:
    """Check if the response redirected away from a job view to a search or index page.

    Args:
        original_url: Initial job listing URL.
        final_url: Resolved destination URL after redirects.

    Returns:
        True if the redirect landed on a generic search/index instead of the job.
    """
    orig_parsed = urllib.parse.urlsplit(original_url)
    final_parsed = urllib.parse.urlsplit(final_url)

    final_host = final_parsed.netloc.lower().removeprefix("www.")

    # Do not treat authwalls/login challenges as search/index redirects
    if _is_auth_wall_path(final_parsed.path):
        return False

    # 1. Any LinkedIn redirect carrying LinkedIn's explicit expired JD tracking marker
    if (
        _is_linkedin_host(final_host)
        and "expired_jd_redirect" in final_parsed.query.lower()
    ):
        return True

    # 2. LinkedIn job view redirect away to a non-view LinkedIn page
    # (e.g. directory/search)
    if (
        _is_linkedin_host(final_host)
        and LINKEDIN_VIEW_RE.search(orig_parsed.path)
        and not LINKEDIN_VIEW_RE.search(final_parsed.path)
    ):
        return True

    # 3. Any redirect ending up on a generic LinkedIn search or landing page
    if _is_linkedin_host(final_host) and (
        "/jobs/search" in final_parsed.path
        or final_parsed.path.rstrip("/") in ("/jobs", "")
    ):
        return True

    # 4. Common ATS redirects when job is removed (e.g. Greenhouse, Lever, Ashby)
    if _is_ats_host(final_host):
        orig_segments = [s for s in orig_parsed.path.strip("/").split("/") if s]
        final_segments = [s for s in final_parsed.path.strip("/").split("/") if s]
        if len(orig_segments) > len(final_segments):
            return True

    return False


def _check_json_ld_expiration(soup: BeautifulSoup) -> tuple[bool, str | None]:
    """Check JSON-LD JobPosting blocks for past validThrough date."""
    for script in soup.find_all("script", type="application/ld+json"):
        content = script.string or ""
        if "validThrough" not in content:
            continue
        try:
            doc = json.loads(content)
            nodes = (
                [doc]
                if isinstance(doc, dict)
                else (doc if isinstance(doc, list) else [])
            )
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                valid_through = node.get("validThrough")
                if isinstance(valid_through, str) and valid_through.strip():
                    vt_str = valid_through.strip().replace("Z", "+00:00")
                    vt_dt = datetime.datetime.fromisoformat(vt_str)
                    now = (
                        datetime.datetime.now(datetime.UTC)
                        if vt_dt.tzinfo
                        else datetime.datetime.now()
                    )
                    if vt_dt < now:
                        return True, f"JSON-LD validThrough expired ({valid_through})"
        except Exception:
            continue
    return False, None


def _check_linkedin_missing_apply_cta(
    soup: BeautifulSoup,
    final_url: str,
) -> tuple[bool, str | None]:
    """Check if a LinkedIn job view page lacks an apply CTA in its topcard layout.

    When a LinkedIn job stops accepting applications without redirecting or
    displaying an explicit 'closed' text phrase, LinkedIn renders the job view
    topcard layout but strips all apply call-to-actions (leaving an empty
    'top-card-layout__cta-container' or omitting primary apply buttons).
    """
    final_parsed = urllib.parse.urlsplit(final_url)
    final_host = final_parsed.netloc.lower().removeprefix("www.")

    if not _is_linkedin_host(final_host) or not LINKEDIN_VIEW_RE.search(
        final_parsed.path
    ):
        return False, None

    # Resolve topcard and CTA containers via CSS selectors
    top_card = soup.select_one(
        ".top-card-layout, .topcard, [class*='top-card-layout__card']"
    )
    cta_container = (
        top_card.select_one(
            ".top-card-layout__cta-container, [class*='top-card-layout__cta-container']"
        )
        if top_card
        else soup.select_one(
            ".top-card-layout__cta-container, [class*='top-card-layout__cta-container']"
        )
    )

    if not top_card and not cta_container:
        # Not a recognizable LinkedIn topcard DOM; do not assume closed
        return False, None

    # An active, enabled primary apply button indicates an open posting
    has_primary_cta = soup.select_one(
        "button.top-card-layout__cta--primary, "
        "a.top-card-layout__cta--primary, "
        "[class*='top-card-layout__cta--primary']"
    )
    if (
        has_primary_cta
        and not has_primary_cta.has_attr("disabled")
        and has_primary_cta.get("aria-disabled") != "true"
    ):
        return False, None

    # Prioritize inspecting cta_container when present to avoid matching
    # unrelated topcard anchor links (e.g., 'Applied Materials' organization link)
    target_container = cta_container if cta_container is not None else top_card
    for elem in target_container.find_all(["button", "a"]):
        if elem.has_attr("disabled") or elem.get("aria-disabled") == "true":
            continue
        text = elem.get_text(" ", strip=True)
        aria = elem.get("aria-label") or ""
        tracking = (elem.get("data-tracking-control-name") or "").lower()
        if (
            _APPLY_WORD_RE.search(text)
            or _APPLY_WORD_RE.search(aria)
            or "apply" in tracking
        ):
            return False, None

    return True, "LinkedIn job posting has no apply CTA in topcard"


def evaluate_page_content(
    html: str,
    original_url: str,
    final_url: str,
) -> LivenessResult:
    """Analyze page HTML for closure or active signals.

    Args:
        html: Raw HTML content.
        original_url: Initial request URL.
        final_url: Final response URL after any redirects.

    Returns:
        LivenessResult indicating OPEN, CLOSED, or INCONCLUSIVE.
    """
    if not html or not html.strip():
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason="Empty response body",
            http_status=200,
        )

    # 1. Anti-scraping / Bot wall guard: NEVER close on a bot challenge
    if looks_like_blocked_page(html):
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason="Blocked by login, CAPTCHA, or anti-bot challenge",
            http_status=200,
        )

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()

    signals: list[str] = []

    # 2. Redirect to general search/index check
    if is_redirect_to_search_or_index(original_url, final_url):
        signals.append(f"Redirected from job view to '{final_url}'")

    # 3. Known text phrases in page body
    for phrase in CLOSED_PHRASES:
        if phrase in page_text:
            signals.append(f"Found closure text: '{phrase}'")

    # 4. DOM element badges (e.g. LinkedIn closed badge)
    closed_elem = soup.find(class_=_is_closed_badge_class)
    if closed_elem:
        badge_text = closed_elem.get_text(strip=True)
        signals.append(f"Found closed DOM badge: '{badge_text}'")

    # 5. JSON-LD validThrough check
    is_expired_ld, ld_reason = _check_json_ld_expiration(soup)
    if is_expired_ld and ld_reason:
        signals.append(ld_reason)

    # 6. LinkedIn topcard missing apply CTA check
    is_missing_cta, cta_reason = _check_linkedin_missing_apply_cta(soup, final_url)
    if is_missing_cta and cta_reason:
        signals.append(cta_reason)

    if signals:
        return LivenessResult(
            status=JobLivenessStatus.CLOSED,
            reason="; ".join(signals),
            http_status=200,
            signals=signals,
        )

    return LivenessResult(
        status=JobLivenessStatus.OPEN,
        reason="Posting appears active and accepting applications",
        http_status=200,
    )


def check_job_liveness(
    url: str,
    session: requests.Session | None = None,
    timeout: float = 12.0,
    headers: dict[str, str] | None = None,
    max_retries_on_rate_limit: int = 1,
    retry_delay: float = 2.0,
) -> LivenessResult:
    """Inspect a job posting URL and determine its availability status.

    Args:
        url: The job posting URL to check.
        session: Optional injected requests.Session for connection reuse/mocking.
        timeout: Network timeout in seconds.
        headers: Optional custom HTTP headers.
        max_retries_on_rate_limit: Retries to attempt if HTTP 429 is encountered.
        retry_delay: Delay in seconds before retrying on HTTP 429.

    Returns:
        LivenessResult with status (OPEN, CLOSED, INCONCLUSIVE) and explanation.
    """
    clean_url = (url or "").strip()
    if not clean_url or not clean_url.startswith(("http://", "https://")):
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason=f"Invalid URL: '{url}'",
        )

    req_session = session or requests.Session()
    close_session = session is None
    req_headers = headers or DEFAULT_HEADERS

    try:
        resp = req_session.get(
            clean_url,
            headers=req_headers,
            timeout=timeout,
            allow_redirects=True,
        )

        # Check if redirected to authwall or login page
        resp_url = resp.url or clean_url
        final_parsed = urllib.parse.urlsplit(resp_url)
        if _is_auth_wall_path(final_parsed.path):
            return LivenessResult(
                status=JobLivenessStatus.INCONCLUSIVE,
                reason="Redirected to login or authentication wall",
                http_status=resp.status_code,
            )

        # HTTP status code inspection
        if resp.status_code in (404, 410):
            status_desc = "Not Found" if resp.status_code == 404 else "Gone"
            return LivenessResult(
                status=JobLivenessStatus.CLOSED,
                reason=f"HTTP {resp.status_code} ({status_desc})",
                http_status=resp.status_code,
                signals=[f"HTTP {resp.status_code}"],
            )

        if resp.status_code in (403, 429):
            if resp.status_code == 429 and max_retries_on_rate_limit > 0:
                logger.warning(
                    "HTTP 429 Rate Limited on %s. Retrying in %.1fs...",
                    clean_url,
                    retry_delay,
                )
                time.sleep(retry_delay)
                return check_job_liveness(
                    clean_url,
                    session=req_session,
                    timeout=timeout,
                    headers=req_headers,
                    max_retries_on_rate_limit=max_retries_on_rate_limit - 1,
                    retry_delay=retry_delay * 1.5,
                )
            return LivenessResult(
                status=JobLivenessStatus.INCONCLUSIVE,
                reason=f"HTTP {resp.status_code} (Rate limited or access denied)",
                http_status=resp.status_code,
            )

        if resp.status_code >= 500:
            return LivenessResult(
                status=JobLivenessStatus.INCONCLUSIVE,
                reason=f"HTTP {resp.status_code} (Server error)",
                http_status=resp.status_code,
            )

        if resp.status_code != 200:
            return LivenessResult(
                status=JobLivenessStatus.INCONCLUSIVE,
                reason=f"Unexpected HTTP {resp.status_code}",
                http_status=resp.status_code,
            )

        # Evaluate HTTP 200 content
        res = evaluate_page_content(resp.text, clean_url, resp_url)
        res.http_status = 200
        return res

    except requests.Timeout:
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason=f"Connection timed out after {timeout}s",
        )
    except requests.RequestException as e:
        logger.debug("Error checking liveness for %s: %s", clean_url, e)
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason=f"Request error: {e}",
        )
    except (ValueError, urllib.error.URLError) as e:
        logger.warning("Error checking liveness for %s: %s", clean_url, e)
        return LivenessResult(
            status=JobLivenessStatus.INCONCLUSIVE,
            reason=f"Processing error: {e}",
        )
    finally:
        if close_session:
            req_session.close()
