"""Job scraper module containing core logic and helper functions for GitEmployed.

Uses python-jobspy to search LinkedIn, Indeed, and ZipRecruiter for roles
based on skills/titles in resume.yaml, or custom queries in settings.yaml.
"""

import logging
import math
import os
import random
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from gitemployed import (
    GitHubClient,
    GitHubClientError,
    Resume,
    load_resume,
    load_settings,
)
from gitemployed.job_description import (
    NO_DESCRIPTION_PLACEHOLDER,
    is_missing_description,
)
from gitemployed.status_model import LABEL_TO_STATUS

logger = logging.getLogger("job_scraper")


HYBRID_INDICATORS = (
    "hybrid schedule",
    "hybrid model",
    "hybrid role",
    "hybrid position",
    "hybrid work",
    "hybrid setup",
    "hybrid format",
    "hybrid environment",
    "3 days/week in office",
    "3 days a week in office",
    "2 days/week in office",
    "2 days a week in office",
    "4 days/week in office",
    "4 days a week in office",
    "days/week in office",
    "days a week in office",
    "days in office",
    "in-office days",
    "hybrid",
)

REMOTE_INDICATORS = (
    "remote position",
    "remote role",
    "remote job",
    "remote work",
    "work from home",
    "work-from-home",
    "telecommute",
    "100% remote",
    "fully remote",
    "remote option",
    "remote-first",
    "remote first",
)

ONSITE_INDICATORS = (
    "onsite required",
    "on-site required",
    "must be onsite",
    "must be on-site",
    "required to be onsite",
    "required to be on-site",
    "not a remote position",
    "not a remote role",
    "not remote",
    "no remote option",
    "onsite only",
    "on-site only",
    "100% onsite",
    "100% on-site",
    "no work from home",
)


@dataclass
class ScrapedJob:
    """Data Transfer Object representing a normalized scraped job posting."""

    company: str
    title: str
    location: str
    salary: str
    source: str
    apply_url: str
    description: str


def _clean_str(val: Any) -> str | None:
    """Helper to safely clean Pandas/Python variables to string or None.

    Checks for Pandas NaNs, None, Pandas Series, and empty/whitespace-only values.
    """
    if val is None:
        return None
    # Handle case where value is a Pandas Series due to duplicate columns
    if isinstance(val, pd.Series):
        val = val.iloc[0] if not val.empty else None
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return s


def _to_float(val: Any) -> float | None:
    """Helper to safely coerce a variable to float or None."""
    if val is None:
        return None
    if isinstance(val, pd.Series):
        val = val.iloc[0] if not val.empty else None
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except ValueError, TypeError:
        return None


def format_salary(
    min_amount: Any,
    max_amount: Any,
    currency: Any,
    interval: Any,
) -> str:
    """Format salary min/max amount, currency, and interval into a readable string.

    Args:
        min_amount: Minimum salary value.
        max_amount: Maximum salary value.
        currency: The currency of the salary (e.g., 'USD').
        interval: The pay interval (e.g., 'yearly', 'hourly').

    Returns:
        Formatted salary string.
    """
    min_val = _to_float(min_amount)
    max_val = _to_float(max_amount)

    if min_val is None and max_val is None:
        return "Not specified"

    curr = _clean_str(currency) or "USD"
    symbol = "$" if curr == "USD" else f"{curr} "

    parts = []
    if min_val is not None and max_val is not None:
        parts.append(f"{symbol}{min_val:,.0f} - {symbol}{max_val:,.0f}")
    elif min_val is not None:
        parts.append(f"From {symbol}{min_val:,.0f}")
    elif max_val is not None:
        parts.append(f"Up to {symbol}{max_val:,.0f}")

    interval_str = _clean_str(interval)
    if interval_str:
        parts.append(f"({interval_str})")

    return " ".join(parts)


def build_issue_body(
    company: str,
    title: str,
    location: str,
    salary: str,
    source: str,
    apply_url: str,
    description: str,
) -> str:
    """Construct a clean markdown body for the GitHub issue.

    Args:
        company: Company name.
        title: Job title.
        location: Job location.
        salary: Formatted salary string.
        source: Scraped job board source.
        apply_url: Original application URL.
        description: Job description text.

    Returns:
        Formatted markdown body.
    """
    return f"""# {title} at {company}

## Job Details
- **Company:** {company}
- **Role:** {title}
- **Location:** {location}
- **Salary:** {salary}
- **Source:** {source}
- **Apply URL:** {apply_url}

## Job Description
{description}
"""


def generate_queries(
    resume: Resume | None,
    settings_custom_queries: list[str] | None,
    combine_skills_with_or: bool = True,
) -> list[str]:
    """Generate job search queries using custom override or base resume data.

    Args:
        resume: Parsed Resume model (can be None if custom queries are set).
        settings_custom_queries: Optional list of custom queries override.
        combine_skills_with_or: If True, combines skills into a single Boolean
            OR query with the latest job title instead of generating multiple
            separate queries.

    Returns:
        List of query strings.
    """
    # 1. Custom Queries Override
    if settings_custom_queries:
        logger.info(
            "Using custom query override from settings: %s",
            settings_custom_queries,
        )
        return settings_custom_queries

    if not resume:
        raise ValueError(
            "Resume configuration is required when custom_queries are not set."
        )

    # 2. Resume-Driven Query Generation
    # Prefer applicant's most recent job title, falling back to summary label
    # if work history is empty.
    latest_title = resume.work[0].position if resume.work else resume.basics.label

    if not latest_title:
        latest_title = "Software Engineer"
    logger.info("Latest position title identified: %s", latest_title)

    # Extract top 3-5 unique skills/keywords
    # Cap at 5 unique keywords to keep total job search queries reasonable
    # and avoid triggering rate limits on job boards.
    unique_keywords = []
    for skill in resume.skills or []:
        for kw in getattr(skill, "keywords", None) or []:
            clean_kw = str(kw).strip() if kw else ""
            if clean_kw and clean_kw not in unique_keywords:
                unique_keywords.append(clean_kw)
                if len(unique_keywords) == 5:
                    break
        if len(unique_keywords) == 5:
            break

    # Consolidate skills into a single Boolean OR clause to minimize search
    # volume and avoid job board rate-limit backoffs, or generate individual
    # queries per skill in legacy mode.
    if not unique_keywords:
        logger.info("No skills found in resume, falling back to position title.")
        return [latest_title]

    if combine_skills_with_or:
        # Quote multi-word skills so job boards (LinkedIn, Indeed) preserve
        # Boolean precedence (e.g. "Distributed Systems" vs Distributed Systems).
        formatted_kws = []
        for kw in unique_keywords:
            raw_kw = kw.strip('"')
            formatted_kws.append(f'"{raw_kw}"' if " " in raw_kw else raw_kw)
        if len(formatted_kws) == 1:
            queries = [f"{latest_title} {formatted_kws[0]}"]
        else:
            skills_clause = " OR ".join(formatted_kws)
            queries = [f"{latest_title} ({skills_clause})"]
    else:
        # Pair latest job position with individual tech skills to target niche,
        # stack-specific job postings (legacy multi-query mode).
        queries = [f"{latest_title} {kw}" for kw in unique_keywords]

    logger.info("Auto-generated %d search queries: %s", len(queries), queries)
    return queries


def sanitize_company_for_search(company: str) -> str:
    """Sanitize company name for use in GitHub issue search queries.

    Strips quotes, brackets, backslashes, and extra whitespace to ensure safe query
    syntax in GitHub Search phrase queries. Preserves colons within the company
    name (e.g., 'Re:Build') so company tokenization is not corrupted.

    Args:
        company: Raw company name string.

    Returns:
        Sanitized company name.
    """
    if not company:
        return ""
    return re.sub(r'["\[\]\\]', "", company).strip()


def fetch_company_existing_records(
    github_client: GitHubClient,
    company: str,
    max_pages: int = 5,
    min_delay_seconds: float = 1.0,
) -> tuple[set[str], set[str]]:
    """Search and parse existing issue role titles and URLs for a specific company.

    Queries the GitHub Search API for issues with the company name in the title,
    extracting existing role titles and apply URLs.

    Args:
        github_client: Initialized GitHub client wrapper.
        company: The company name to search for.
        max_pages: Maximum number of search result pages to fetch.
        min_delay_seconds: Pacing delay between paginated search requests.

    Returns:
        Tuple of (set_of_lowercase_role_titles, set_of_canonical_job_identifiers).
    """
    clean_comp = sanitize_company_for_search(company)
    if not clean_comp:
        return set(), set()

    query = f'repo:{github_client.repo} is:issue in:title "[{clean_comp}]"'
    logger.info("Searching issues for company '%s': query='%s'", company, query)

    existing_roles: set[str] = set()
    existing_urls: set[str] = set()
    title_pattern = re.compile(r"^\[([^\]]+)\]\s+(.*)$")

    for page in range(1, max_pages + 1):
        try:
            issues = github_client.search_issues(query, per_page=100, page=page)
            if not issues:
                break
        except GitHubClientError as e:
            logger.warning(
                "Failed to search issues for company '%s' (page %d): %s",
                company,
                page,
                e,
            )
            break

        for issue in issues:
            title = issue.get("title") or ""
            match = title_pattern.match(title)
            if match:
                issue_comp = match.group(1).strip().lower()
                if (
                    issue_comp == company.strip().lower()
                    or clean_comp.lower() in issue_comp
                ):
                    role = match.group(2).strip().lower()
                    existing_roles.add(role)

            body = issue.get("body") or ""
            apply_url = extract_apply_url(body)
            if apply_url:
                identifier = extract_job_identifier(apply_url)
                if identifier:
                    existing_urls.add(identifier)
                existing_urls.add(apply_url.lower())

        if len(issues) < 100:
            break

        if page < max_pages and min_delay_seconds > 0:
            time.sleep(min_delay_seconds)

    logger.info(
        "Cached %d roles and %d URLs for company '%s'",
        len(existing_roles),
        len(existing_urls),
        company,
    )
    return existing_roles, existing_urls


class CompanyDeduplicationStore:
    """Run-local memoized deduplication store querying GitHub on demand per company."""

    def __init__(
        self,
        github_client: GitHubClient | None = None,
        min_delay_seconds: float = 1.0,
    ) -> None:
        """Initialize company deduplication store.

        Args:
            github_client: GitHub client instance, or None for dry-run/mock testing.
            min_delay_seconds: Minimum delay between search API requests for pacing.
        """
        self.github_client = github_client
        self.min_delay_seconds = min_delay_seconds
        self._cache: dict[str, tuple[set[str], set[str]]] = {}
        self._last_search_time: float = 0.0

    def _ensure_company_cached(self, company: str) -> None:
        comp_key = company.strip().lower()
        if comp_key in self._cache:
            return

        if self.github_client is None:
            self._cache[comp_key] = (set(), set())
            return

        # Pacing between search API requests across different companies
        elapsed = time.time() - self._last_search_time
        if self._last_search_time > 0 and elapsed < self.min_delay_seconds:
            time.sleep(self.min_delay_seconds - elapsed)

        roles, urls = fetch_company_existing_records(
            self.github_client,
            company,
            min_delay_seconds=self.min_delay_seconds,
        )
        self._last_search_time = time.time()
        self._cache[comp_key] = (roles, urls)

    def is_duplicate(self, job: ScrapedJob) -> tuple[bool, str]:
        """Check whether the scraped job is a duplicate via URL or company+role title.

        Args:
            job: ScrapedJob instance to evaluate.

        Returns:
            Tuple of (is_duplicate: bool, reason: str).
        """
        self._ensure_company_cached(job.company)
        comp_key = job.company.strip().lower()
        roles, urls = self._cache.get(comp_key, (set(), set()))

        # Tier 1: URL / Job ID Match
        if job.apply_url:
            job_id = extract_job_identifier(job.apply_url)
            norm_url = normalize_job_url(job.apply_url).lower()
            if job_id and job_id in urls:
                return True, f"matching job identifier '{job_id}'"
            if norm_url and norm_url in urls:
                return True, f"matching apply URL '{norm_url}'"

        # Tier 2: Company + Role Title Match
        role_key = job.title.strip().lower()
        if role_key in roles:
            return (
                True,
                f"matching role title '{job.title}' for company '{job.company}'",
            )

        return False, ""

    def record_job(self, job: ScrapedJob) -> None:
        """Record a newly published job in the in-memory cache.

        Args:
            job: ScrapedJob instance just published.
        """
        comp_key = job.company.strip().lower()
        if comp_key not in self._cache:
            self._cache[comp_key] = (set(), set())

        roles, urls = self._cache[comp_key]
        roles.add(job.title.strip().lower())
        if job.apply_url:
            job_id = extract_job_identifier(job.apply_url)
            if job_id:
                urls.add(job_id)
            norm_url = normalize_job_url(job.apply_url).lower()
            if norm_url:
                urls.add(norm_url)


def fetch_existing_jobs_cache(
    github_client: GitHubClient, max_pages: int = 5
) -> set[tuple[str, str]]:
    """Retrieve existing jobs from repository issues for deduplication.

    Args:
        github_client: Initialized GitHub client wrapper.
        max_pages: Maximum number of pages to fetch from the GitHub API.

    Returns:
        Set of lowercase (company, role) tuples.
    """
    # Fetch up to 100 recent issues per page to balance API response payload size
    # with deduplication freshness.
    logger.info("Fetching deduplication cache of recent issues...")
    existing = set()
    title_pattern = re.compile(r"^\[([^\]]+)\]\s+(.*)$")

    for page in range(1, max_pages + 1):
        try:
            issues = github_client.list_issues(state="all", per_page=100, page=page)
            if not issues:
                break
        except Exception as e:
            logger.error(
                "Failed to fetch issues (page %d) from GitHub: %s. Stopping cache.",
                page,
                e,
            )
            break

        for issue in issues:
            title = issue.get("title") or ""
            match = title_pattern.match(title)
            if match:
                company = match.group(1).strip().lower()
                role = match.group(2).strip().lower()
                existing.add((company, role))

        if len(issues) < 100:
            break

    logger.info("Successfully cached %d existing jobs.", len(existing))
    return existing


def classify_work_type(location: str, description: str) -> str:
    """Classify job location and description into 'remote', 'hybrid', or 'onsite'."""
    loc_lower = location.lower()
    desc_lower = description.lower()

    # Check location string first
    if "hybrid" in loc_lower:
        return "hybrid"
    if "remote" in loc_lower:
        # Check if description contradicts and makes it hybrid
        if any(ind in desc_lower for ind in HYBRID_INDICATORS):
            return "hybrid"
        return "remote"

    # Check description for hybrid indicators
    if any(ind in desc_lower for ind in HYBRID_INDICATORS):
        return "hybrid"

    # Check description for remote indicators
    has_remote = any(ind in desc_lower for ind in REMOTE_INDICATORS)
    has_onsite = any(ind in desc_lower for ind in ONSITE_INDICATORS)

    if has_remote and not has_onsite:
        return "remote"

    # Default is onsite
    return "onsite"


def is_local_proximity_match(
    job_location: str,
    candidate_city: str,
    candidate_state: str,
) -> bool:
    """Check if the job location matches the candidate's city/state.

    Enforces word boundary matching for state abbreviations to prevent false-positives
    (e.g., 'Austin, TX' matching state 'in').
    """
    job_loc_lower = job_location.lower().strip()
    city_lower = candidate_city.lower().strip()
    state_lower = candidate_state.lower().strip()

    # Check city match
    if city_lower and city_lower in job_loc_lower:
        return True

    # Check state match
    if state_lower:
        if len(state_lower) <= 2:
            # Enforce word boundaries for short state/region abbreviations
            if re.search(r"\b" + re.escape(state_lower) + r"\b", job_loc_lower):
                return True
        else:
            # For full names (e.g., 'Washington' or 'Ontario'), substring check is safe
            if state_lower in job_loc_lower:
                return True

    return False


TRACKING_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "refid",
        "trackingid",
        "position",
        "page",
        "trk",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "from",
        "vjs",
        "tk",
        "bb",
        "fccid",
        "cmp",
        "origin",
        "midtoken",
        "ek",
        "eid",
        "context",
        "currentjobid",
        "fbclid",
        "gclid",
        "msclkid",
        "yclid",
        "dclid",
        "ttclid",
        "li_fat_id",
    }
)

LINKEDIN_JOB_ID_PATTERN = re.compile(
    r"linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d+)", re.IGNORECASE
)
LINKEDIN_CURRENT_JOB_ID_PATTERN = re.compile(r"[?&]currentJobId=(\d+)", re.IGNORECASE)
INDEED_JK_PATTERN = re.compile(r"indeed\.com/.*[?&]jk=([a-zA-Z0-9]+)", re.IGNORECASE)

APPLY_URL_PATTERN = re.compile(
    r"-\s+\*\*Apply URL:\*\*\s+(https?://[^\s\)]+)", re.IGNORECASE
)


def _is_linkedin_url(url: Any) -> bool:
    """Check if a URL belongs to LinkedIn."""
    clean = _clean_str(url)
    if not clean:
        return False
    try:
        if "://" not in clean and not clean.startswith("//"):
            clean = f"//{clean}"
        parsed = urllib.parse.urlparse(clean)
        hostname = (parsed.hostname or "").lower()
        return hostname == "linkedin.com" or hostname.endswith(".linkedin.com")
    except ValueError, AttributeError:
        return False


def normalize_job_url(url: Any) -> str:
    """Normalize a job URL by removing tracking query params and standardizing host.

    Extracts canonical formats for major job boards (LinkedIn, Indeed) and strips
    tracking parameters and URL fragments for general URLs.

    Args:
        url: Raw URL string or object.

    Returns:
        Canonical normalized URL string, or empty string if invalid.
    """
    clean = _clean_str(url)
    if not clean or clean.lower() in ("not available", "none", "nan"):
        return ""

    # Check for LinkedIn job ID pattern
    # e.g., /jobs/view/4421986211, /jobs/view/slug-4421986211,
    # or currentJobId=4421986211
    if _is_linkedin_url(clean):
        li_match = LINKEDIN_JOB_ID_PATTERN.search(clean)
        if li_match:
            return f"https://linkedin.com/jobs/view/{li_match.group(1)}"

        li_param = LINKEDIN_CURRENT_JOB_ID_PATTERN.search(clean)
        if li_param:
            return f"https://linkedin.com/jobs/view/{li_param.group(1)}"

    # Check for Indeed job key jk=...
    indeed_match = INDEED_JK_PATTERN.search(clean)
    if indeed_match:
        return f"https://indeed.com/viewjob?jk={indeed_match.group(1)}"

    # General URL normalization
    try:
        parsed = urllib.parse.urlparse(clean)
        if not parsed.scheme or not parsed.netloc:
            return clean.rstrip("/")

        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        filtered_query = [
            (k, v) for k, v in query_pairs if k.lower() not in TRACKING_QUERY_PARAMS
        ]
        new_query = urllib.parse.urlencode(filtered_query)

        path = parsed.path.rstrip("/")
        normalized = urllib.parse.urlunparse((scheme, netloc, path, "", new_query, ""))
        return normalized.rstrip("/")
    except ValueError, AttributeError:
        return clean.rstrip("/")


def extract_job_identifier(url: Any) -> str:
    """Extract a canonical job identifier for deduplication lookups.

    Args:
        url: Raw or normalized job URL.

    Returns:
        Canonical job key (e.g. 'linkedin:4421986211' or normalized URL).
    """
    norm = normalize_job_url(url)
    if not norm:
        return ""

    li_match = LINKEDIN_JOB_ID_PATTERN.search(norm)
    if li_match:
        return f"linkedin:{li_match.group(1)}"

    indeed_match = re.search(r"indeed\.com/viewjob\?jk=([a-zA-Z0-9]+)", norm)
    if indeed_match:
        return f"indeed:{indeed_match.group(1)}"

    return norm


def extract_apply_url(body: str | None) -> str | None:
    """Extract and normalize the Apply URL from a markdown issue body.

    Args:
        body: Markdown issue body.

    Returns:
        Normalized apply URL, or None if not found.
    """
    if not body:
        return None
    match = APPLY_URL_PATTERN.search(body)
    if match:
        norm = normalize_job_url(match.group(1))
        return norm if norm else match.group(1).strip()
    return None


def parse_job_row(row: Any) -> ScrapedJob:
    """Extracts and sanitizes a ScrapedJob DTO from a pandas Series or dictionary.

    Args:
        row: The raw job row data (Series or dictionary).

    Returns:
        The parsed and sanitized ScrapedJob object.
    """
    company = _clean_str(row.get("company")) or "Unknown Company"
    title = _clean_str(row.get("title")) or "Unknown Role"
    location = _clean_str(row.get("location")) or "Unknown Location"
    description = _clean_str(row.get("description")) or NO_DESCRIPTION_PLACEHOLDER

    # Try job_url first, fallback to job_url_direct safely resolving empty strings
    raw_apply_url = _clean_str(row.get("job_url")) or _clean_str(
        row.get("job_url_direct")
    )
    apply_url = normalize_job_url(raw_apply_url) or raw_apply_url or "Not available"

    source = _clean_str(row.get("site")) or "Unknown"

    salary = format_salary(
        min_amount=row.get("min_amount"),
        max_amount=row.get("max_amount"),
        currency=row.get("currency"),
        interval=row.get("interval"),
    )

    return ScrapedJob(
        company=company,
        title=title,
        location=location,
        salary=salary,
        source=source,
        apply_url=apply_url,
        description=description,
    )


def publish_job(
    github_client: GitHubClient, job: ScrapedJob, dry_run: bool
) -> dict[str, Any] | None:
    """Publish a ScrapedJob to GitHub Issues and update Projects V2 status.

    Args:
        github_client: Initialized GitHubClient wrapper.
        job: The ScrapedJob to publish.
        dry_run: If True, log mock action without API requests.

    Returns:
        The created issue response dictionary, or None if dry_run.
    """
    issue_title = f"[{job.company}] {job.title}"
    issue_body = build_issue_body(
        company=job.company,
        title=job.title,
        location=job.location,
        salary=job.salary,
        source=job.source,
        apply_url=job.apply_url,
        description=job.description,
    )

    if dry_run:
        logger.info(
            "[DRY RUN] Would create issue: %s. Body length: %d chars",
            issue_title,
            len(issue_body),
        )
        return None

    logger.info("Creating issue for: %s", issue_title)
    issue = github_client.create_issue(
        title=issue_title,
        body=issue_body,
        labels=["triage-pending"],
    )

    # Update Projects V2 if configured
    if github_client.project_id:
        node_id = issue.get("node_id")
        if node_id:
            status = LABEL_TO_STATUS["triage-pending"]
            logger.info(
                "Adding node %s to status '%s'",
                node_id,
                status,
            )
            try:
                github_client.update_project_status(node_id, status)
            except Exception as pe:
                logger.warning("Failed to update Projects V2 status: %s", pe)
    return issue


# LinkedIn throttles shared CI egress IPs with HTTP 429. A couple of short,
# bounded retries recovers transient throttling without stalling the scrape.
LINKEDIN_MAX_RETRIES = 2
LINKEDIN_BACKOFF_BASE_SECONDS = 2.0
LINKEDIN_MAX_BACKOFF_SECONDS = 30.0


class LinkedInThrottle:
    """Per-run circuit breaker for LinkedIn description fetches.

    When throttling is sustained, retrying every candidate just burns minutes
    of CI time on sleeps. After a few jobs in a row exhaust their 429 retries
    the rest of the run skips LinkedIn fetches and uses the basic listing,
    which triage can still recover from the Apply URL.
    """

    def __init__(self, max_consecutive_exhausted: int = 2) -> None:
        self.max_consecutive_exhausted = max_consecutive_exhausted
        self.consecutive_exhausted = 0

    @property
    def tripped(self) -> bool:
        return self.consecutive_exhausted >= self.max_consecutive_exhausted

    def record_exhausted(self) -> None:
        self.consecutive_exhausted += 1
        # Only the transition is worth a warning; later calls are no-ops that
        # would otherwise repeat the message for every remaining job.
        if self.consecutive_exhausted == self.max_consecutive_exhausted:
            logger.warning(
                "LinkedIn throttled %d jobs in a row; skipping description "
                "fetches for the rest of this run.",
                self.consecutive_exhausted,
            )

    def record_success(self) -> None:
        self.consecutive_exhausted = 0


def _retry_delay_seconds(resp: requests.Response, attempt: int) -> float:
    """Return the wait before retrying a 429, honoring a numeric Retry-After."""
    retry_after = resp.headers.get("Retry-After", "")
    try:
        delay = float(retry_after)
    except TypeError, ValueError:
        delay = LINKEDIN_BACKOFF_BASE_SECONDS * (2**attempt)
    return max(0.0, min(delay, LINKEDIN_MAX_BACKOFF_SECONDS))


def _get_with_429_retry(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    timeout: float,
    sleep_fn: Callable[[float], None],
) -> requests.Response:
    """GET ``url``, retrying HTTP 429 with backoff up to LINKEDIN_MAX_RETRIES."""
    resp = session.get(url, headers=headers, timeout=timeout)
    for attempt in range(LINKEDIN_MAX_RETRIES):
        if resp.status_code != 429:
            break
        delay = _retry_delay_seconds(resp, attempt)
        logger.info(
            "LinkedIn rate-limited %s; retrying in %.1fs (%d/%d)",
            url,
            delay,
            attempt + 1,
            LINKEDIN_MAX_RETRIES,
        )
        sleep_fn(delay)
        resp = session.get(url, headers=headers, timeout=timeout)
    return resp


def fetch_linkedin_description(
    job_url: str,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    throttle: LinkedInThrottle | None = None,
) -> str | None:
    """Fetch and parse full description from a LinkedIn job posting URL.

    Args:
        job_url: Raw or normalized LinkedIn job URL.
        session: Optional injected requests.Session instance.
        timeout: Network timeout in seconds.
        sleep_fn: Sleep function used between 429 retries (injectable for tests).
        throttle: Optional run-wide breaker; once tripped, no request is made.

    Returns:
        Clean markdown description string, or None if extraction fails.
    """
    if throttle is not None and throttle.tripped:
        return None

    clean_url = _clean_str(job_url)
    if not clean_url or not _is_linkedin_url(clean_url):
        return None

    norm_url = normalize_job_url(clean_url)
    li_match = LINKEDIN_JOB_ID_PATTERN.search(norm_url)
    if li_match:
        job_id = li_match.group(1)
        target_url = f"https://www.linkedin.com/jobs/view/{job_id}"
    elif _is_linkedin_url(norm_url):
        target_url = norm_url
    else:
        return None

    req_session = session or requests.Session()
    close_session = session is None
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = _get_with_429_retry(req_session, target_url, headers, timeout, sleep_fn)
        if throttle is not None:
            if resp.status_code == 429:
                throttle.record_exhausted()
            else:
                throttle.record_success()
        if resp.status_code != 200:
            logger.warning(
                "LinkedIn description fetch failed for %s: HTTP %d",
                target_url,
                resp.status_code,
            )
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        markup = soup.find(
            "div", class_=lambda x: x and "show-more-less-html__markup" in x
        )
        if not markup:
            markup = soup.find("div", class_=lambda x: x and "description__text" in x)

        if markup:
            desc = md(str(markup)).strip()
            return desc if desc else None

        logger.warning(
            "LinkedIn description container not found in response for %s",
            target_url,
        )
    except Exception as e:
        logger.warning(
            "Error fetching LinkedIn description for %s: %s",
            target_url,
            e,
        )
        return None
    finally:
        if close_session:
            req_session.close()

    return None


def fetch_job_description(
    job: ScrapedJob,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    throttle: LinkedInThrottle | None = None,
) -> str | None:
    """Fetch description on demand for candidate jobs missing one.

    If the job already has a non-empty description, returns it directly.
    If the job is from LinkedIn and missing a description, fetches it lazily.

    Args:
        job: ScrapedJob instance.
        session: Optional injected requests.Session.
        timeout: Network timeout in seconds.
        throttle: Optional run-wide LinkedIn circuit breaker.

    Returns:
        Full description string or None.
    """
    if not is_missing_description(job.description):
        return job.description

    if job.apply_url and _is_linkedin_url(job.apply_url):
        return fetch_linkedin_description(
            job.apply_url, session=session, timeout=timeout, throttle=throttle
        )

    return None


def run_scraper(
    settings_path: str = "config/settings.yaml",
    resume_path: str = "resumes/resume.yaml",
    dry_run: bool = False,
    github_client: GitHubClient | None = None,
    scrape_fn: Callable | None = None,
    work_preference_override: str | None = None,
    job_type_override: str | None = None,
    hours_old_override: int | None = None,
    results_wanted_override: int | None = None,
    combine_skills_override: bool | None = None,
    fetch_description_fn: Callable[[ScrapedJob], str | None] | None = None,
) -> None:
    """Main execution function for the job scraper bot.

    Args:
        settings_path: Filepath of the system settings.yaml.
        resume_path: Filepath of the base resume.yaml.
        dry_run: If True, skip all remote GitHub write/read operations.
        github_client: Optional injected GitHubClient instance.
        scrape_fn: Optional injected scraping function to override jobspy.
        work_preference_override: Optional work preference override to ignore settings.
        job_type_override: Optional job type override to ignore settings.
        hours_old_override: Optional hours old override to ignore settings.
        results_wanted_override: Optional results wanted override to ignore settings.
        combine_skills_override: Optional combine skills override to ignore settings.
        fetch_description_fn: Optional injected function to fetch job descriptions.
    """
    logger.info("Loading settings from: %s", settings_path)
    settings = load_settings(settings_path)

    if not settings.search.enabled:
        logger.info("Scraper is disabled in settings.yaml. Skipping execution.")
        return

    # Always load resume to derive location for country/local filtering
    resume = load_resume(resume_path)

    # 1. Resolve GitHub client (with dependency injection fallback)
    if github_client is None:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")

        if dry_run:
            logger.info(
                "Running in DRY RUN mode. GitHub API operations will be skipped."
            )
            token = token or "dummy_token"
            repo = repo or "dummy/repo"
        elif not token or not repo:
            raise ValueError(
                "Missing GITHUB_TOKEN or GITHUB_REPOSITORY environment variables."
            )

        project_id = None
        status_field = "Status"
        if settings.projects_v2:
            project_id = settings.projects_v2.project_id
            status_field = settings.projects_v2.status_field_name

        github_client = GitHubClient(
            token=token,
            repo=repo,
            project_id=project_id,
            status_field_name=status_field,
        )

    # 2. Resolve scraping method (dependency injection fallback)
    if scrape_fn is None:
        from jobspy import scrape_jobs

        scrape_fn = scrape_jobs

    # 3. Initialize run-local company deduplication store
    dedup_store = CompanyDeduplicationStore(
        github_client=None if dry_run else github_client
    )

    # 4. Initialize description fetcher
    desc_session: requests.Session | None = None
    if fetch_description_fn is None:
        desc_session = requests.Session()
        linkedin_throttle = LinkedInThrottle()

        def _default_fetch_desc(j: ScrapedJob) -> str | None:
            return fetch_job_description(
                j, session=desc_session, throttle=linkedin_throttle
            )

        fetch_description_fn = _default_fetch_desc

    # 5. Generate queries by passing the loaded Resume object
    combine_skills = (
        combine_skills_override
        if combine_skills_override is not None
        else settings.search.combine_skills_with_or
    )
    queries = generate_queries(
        resume,
        settings.custom_queries,
        combine_skills_with_or=combine_skills,
    )

    # 6. Extract scraper settings
    platforms = settings.search.platforms
    work_preference = (
        work_preference_override
        if work_preference_override is not None
        else settings.search.work_preference
    )
    work_preference = work_preference.lower().strip()
    if work_preference not in ("remote", "onsite", "hybrid"):
        raise ValueError(
            f"Invalid work preference '{work_preference}'. "
            "Must be one of: remote, onsite, hybrid."
        )

    job_type = (
        job_type_override if job_type_override is not None else settings.search.job_type
    )
    hours_old = (
        hours_old_override
        if hours_old_override is not None
        else settings.search.hours_old
    )
    if hours_old <= 0:
        raise ValueError(f"Invalid hours_old '{hours_old}'. Must be greater than zero.")

    results_wanted = (
        results_wanted_override
        if results_wanted_override is not None
        else settings.search.results_wanted
    )
    if results_wanted <= 0:
        raise ValueError(
            f"Invalid results_wanted '{results_wanted}'. Must be greater than zero."
        )

    # Map country code to search country name
    user_country_code = (
        resume.basics.location.country_code.upper() if resume.basics.location else "US"
    )

    # Derive python-jobspy query parameters from work preference and candidate location
    if work_preference == "remote":
        search_location = user_country_code
        is_remote = True
    else:
        is_remote = False
        city = resume.basics.location.city if resume.basics.location else ""
        state = resume.basics.location.state if resume.basics.location else ""
        if city and state:
            search_location = f"{city}, {state}"
        elif city:
            search_location = city
        else:
            search_location = user_country_code

    # Pre-resolve and lowercase candidate location attributes once outside the loop
    # (null-safe)
    cand_city = ""
    cand_state = ""
    if resume.basics.location:
        if resume.basics.location.city:
            cand_city = resume.basics.location.city.lower().strip()
        if resume.basics.location.state:
            cand_state = resume.basics.location.state.lower().strip()

    logger.info(
        "Scraper config: platforms=%s, work_preference='%s', "
        "search_location='%s', job_type=%s, hours_old=%d, results_wanted=%d",
        platforms,
        work_preference,
        search_location,
        job_type,
        hours_old,
        results_wanted,
    )

    # 7. Sequential search loop
    for index, query in enumerate(queries):
        logger.info(
            "Executing query (%d/%d): '%s'",
            index + 1,
            len(queries),
            query,
        )
        try:
            extra_kwargs: dict[str, Any] = {}
            if "linkedin" in platforms:
                extra_kwargs["linkedin_fetch_description"] = False

            jobs_df = scrape_fn(
                site_name=platforms,
                search_term=query,
                location=search_location,
                is_remote=is_remote,
                job_type=job_type,
                hours_old=hours_old,
                results_wanted=results_wanted,
                **extra_kwargs,
            )

            if jobs_df is None or jobs_df.empty:
                logger.info("No job postings found for query: '%s'", query)
            else:
                logger.info("Found %d jobs for query: '%s'", len(jobs_df), query)
                new_listings_count = 0

                # Iterate over native dicts to avoid iterrows() Series allocation
                # overhead as recommended in review audits.
                for row in jobs_df.to_dict(orient="records"):
                    job = parse_job_row(row)

                    # Local proximity check for onsite/hybrid (uses location only)
                    if (
                        work_preference in ("onsite", "hybrid")
                        and resume.basics.location
                        and not is_local_proximity_match(
                            job.location, cand_city, cand_state
                        )
                    ):
                        logger.info(
                            "Skipping local role outside candidate's "
                            "city/region: [%s] %s - Location: %s "
                            "(User location: %s, %s)",
                            job.company,
                            job.title,
                            job.location,
                            resume.basics.location.city or "",
                            resume.basics.location.state or "",
                        )
                        continue

                    # Check for duplicates via CompanyDeduplicationStore
                    # (uses URL, company, and title only)
                    is_dup, dup_reason = dedup_store.is_duplicate(job)
                    if is_dup:
                        logger.debug(
                            "Skipping duplicate role (%s): [%s] %s",
                            dup_reason,
                            job.company,
                            job.title,
                        )
                        continue

                    # Lazy fetch description only for candidate jobs
                    if is_missing_description(job.description):
                        logger.info(
                            "Fetching description for candidate role: [%s] %s",
                            job.company,
                            job.title,
                        )
                        fetched_desc = fetch_description_fn(job)
                        if fetched_desc:
                            job.description = fetched_desc
                        # Polite jitter delay between successive on-demand fetches
                        time.sleep(random.uniform(0.5, 1.0))

                        # Without a description triage has nothing to score, so
                        # don't file the issue. The job is not recorded in the
                        # dedup store, which lets a later run retry the fetch
                        # (LinkedIn 429s are usually transient).
                        if is_missing_description(job.description):
                            logger.info(
                                "No description retrieved for [%s] %s; "
                                "skipping so a later run can retry.",
                                job.company,
                                job.title,
                            )
                            continue

                    # Filter by work preference type (now with full description).
                    # Onsite and hybrid are mutually compatible as both are local
                    # office-based.
                    job_work_type = classify_work_type(job.location, job.description)
                    is_match = job_work_type == work_preference or (
                        work_preference in ("onsite", "hybrid")
                        and job_work_type in ("onsite", "hybrid")
                    )

                    if not is_match:
                        logger.info(
                            "Skipping role with mismatching work type "
                            "(got '%s', want '%s'): [%s] %s - Location: %s",
                            job_work_type,
                            work_preference,
                            job.company,
                            job.title,
                            job.location,
                        )
                        continue

                    try:
                        publish_job(github_client, job, dry_run)
                        new_listings_count += 1
                        dedup_store.record_job(job)
                    except Exception as ie:
                        logger.error(
                            "Failed to process job '%s' by '%s': %s",
                            job.title,
                            job.company,
                            ie,
                        )

                logger.info(
                    "Processed %d new roles for query: '%s'",
                    new_listings_count,
                    query,
                )

        except Exception as e:
            logger.error("Scraper failed for query '%s': %s. Continuing...", query, e)

        # Sleep/Throttling between queries (except for the last one)
        if index < len(queries) - 1:
            delay = random.uniform(2.0, 5.0)
            logger.info("Throttling: sleeping for %.2fs before next query...", delay)
            time.sleep(delay)

    logger.info("Scraper execution completed successfully.")
    if desc_session is not None:
        desc_session.close()
