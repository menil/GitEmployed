"""Unit tests for the job scraper bot (gitemployed/scraper.py)."""

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from gitemployed.github_client import GitHubClientError
from gitemployed.scraper import (
    CompanyDeduplicationStore,
    ScrapedJob,
    build_issue_body,
    extract_apply_url,
    extract_job_identifier,
    fetch_company_existing_records,
    fetch_existing_jobs_cache,
    format_salary,
    generate_queries,
    normalize_job_url,
    parse_job_row,
    publish_job,
    run_scraper,
    sanitize_company_for_search,
)


def test_format_salary() -> None:
    """Verify salary formatting handles various inputs and edge cases."""
    # Both min and max provided
    val1 = format_salary(100000, 150000, "USD", "yearly")
    assert val1 == "$100,000 - $150,000 (yearly)"

    # Only min provided
    assert format_salary(80000, None, "USD", "yearly") == "From $80,000 (yearly)"

    # Only max provided
    assert format_salary(None, 120000, "USD", "yearly") == "Up to $120,000 (yearly)"

    # Different currency
    val2 = format_salary(5000, 8000, "EUR", "monthly")
    assert val2 == "EUR 5,000 - EUR 8,000 (monthly)"

    # No salary info
    assert format_salary(None, None, "USD", "yearly") == "Not specified"

    # String coercion
    assert format_salary("90000", "110000", None, None) == "$90,000 - $110,000"

    # Bad values fallback
    assert format_salary("invalid", None, "USD", None) == "Not specified"

    # NaN handling
    assert format_salary(float("nan"), None, "USD", "yearly") == "Not specified"
    assert format_salary(None, float("nan"), "USD", None) == "Not specified"
    assert format_salary(float("nan"), float("nan"), "USD", None) == "Not specified"
    assert (
        format_salary(100000, 150000, float("nan"), float("nan"))
        == "$100,000 - $150,000"
    )

    # Pandas Series handling (duplicate columns)
    assert (
        format_salary(
            pd.Series([100000, 120000]),
            pd.Series([150000]),
            "USD",
            "yearly",
        )
        == "$100,000 - $150,000 (yearly)"
    )


def test_build_issue_body() -> None:
    """Verify markdown issue body construction and fallback values."""
    body = build_issue_body(
        company="Acme Corp",
        title="Python Engineer",
        location="Remote",
        salary="$100k - $120k",
        source="Indeed",
        apply_url="https://acme.com/apply",
        description="Write python code.",
    )
    assert "# Python Engineer at Acme Corp" in body
    assert "- **Company:** Acme Corp" in body
    assert "- **Role:** Python Engineer" in body
    assert "- **Location:** Remote" in body
    assert "- **Salary:** $100k - $120k" in body
    assert "- **Source:** Indeed" in body
    assert "- **Apply URL:** https://acme.com/apply" in body
    assert "Write python code." in body


def test_generate_queries_resume_driven() -> None:
    """Verify query generation from Resume object."""
    mock_resume = MagicMock()
    mock_resume.work = [MagicMock(position="Senior Python Developer")]
    mock_resume.skills = [
        MagicMock(keywords=["FastAPI", "Docker", "Git"]),
        # Duplicate keyword 'Docker', and whitespace padded keyword
        MagicMock(keywords=["Kubernetes", "AWS", " FastAPI "]),
    ]

    queries = generate_queries(mock_resume, None)

    expected = [
        "Senior Python Developer FastAPI",
        "Senior Python Developer Docker",
        "Senior Python Developer Git",
        "Senior Python Developer Kubernetes",
        "Senior Python Developer AWS",
    ]
    assert queries == expected


def test_generate_queries_resume_driven_fallback() -> None:
    """Verify query generation fallback when work and skills are sparse."""
    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = "Staff Engineer"
    mock_resume.skills = []

    queries = generate_queries(mock_resume, None)
    assert queries == ["Staff Engineer"]


def test_generate_queries_resume_driven_ultimate_fallback() -> None:
    """Verify query generation ultimate fallback to Software Engineer."""
    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = None
    mock_resume.skills = []

    queries = generate_queries(mock_resume, None)
    assert queries == ["Software Engineer"]


def test_generate_queries_custom_override() -> None:
    """Verify custom queries override resume parsing entirely."""
    mock_resume = MagicMock()
    custom = ["Remote React Developer", "Data Scientist Seattle"]
    queries = generate_queries(mock_resume, custom)
    assert queries == custom


def test_fetch_existing_jobs_cache() -> None:
    """Verify issue titles are fetched and parsed correctly for duplicate check."""
    mock_client = MagicMock()
    # Construct 100 items for page 1 to trigger loading page 2
    page1 = [{"title": f"[Company {i}] Role {i}"} for i in range(100)]
    # Wayne Enterprises on page 2
    page2 = [{"title": "[Wayne Enterprises] CEO"}]
    mock_client.list_issues.side_effect = [page1, page2, []]

    cache = fetch_existing_jobs_cache(mock_client, max_pages=3)
    assert len(cache) == 101
    assert ("company 0", "role 0") in cache
    assert ("wayne enterprises", "ceo") in cache


def test_fetch_existing_jobs_cache_failure() -> None:
    """Verify cache fetching handles GitHub API failure gracefully."""
    mock_client = MagicMock()
    mock_client.list_issues.side_effect = Exception("API rate limit exceeded")

    cache = fetch_existing_jobs_cache(mock_client)
    assert cache == set()


def test_parse_job_row() -> None:
    """Verify parse_job_row handles normal values, NaNs, and whitespace fallbacks."""
    # Test valid row
    row1 = {
        "company": "Acme",
        "title": "Engineer",
        "location": "Boston",
        "description": "Short desc",
        "job_url": "https://acme.com",
        "site": "linkedin",
    }
    job1 = parse_job_row(row1)
    assert job1.company == "Acme"
    assert job1.apply_url == "https://acme.com"
    assert job1.salary == "Not specified"

    # Test NaN / Null row
    row2 = {
        "company": float("nan"),
        "title": float("nan"),
        "location": float("nan"),
        "description": float("nan"),
        "job_url": float("nan"),
        "job_url_direct": float("nan"),
        "site": float("nan"),
    }
    job2 = parse_job_row(row2)
    assert job2.company == "Unknown Company"
    assert job2.title == "Unknown Role"
    assert job2.location == "Unknown Location"
    assert job2.description == "No job description provided."
    assert job2.apply_url == "Not available"
    assert job2.source == "Unknown"

    # Test whitespace / empty string URL selection fallback
    row3 = {
        "company": "Google",
        "title": "Developer",
        "job_url": "   ",
        "job_url_direct": "https://google.com/apply",
    }
    job3 = parse_job_row(row3)
    assert job3.apply_url == "https://google.com/apply"


def test_publish_job_dry_run() -> None:
    """Verify publish_job skips API operations in dry-run mode."""
    mock_client = MagicMock()
    job = ScrapedJob(
        company="Acme",
        title="Engineer",
        location="Remote",
        salary="Not specified",
        source="indeed",
        apply_url="Not available",
        description="Write code.",
    )
    res = publish_job(mock_client, job, dry_run=True)
    assert res is None
    mock_client.create_issue.assert_not_called()


def test_publish_job_success() -> None:
    """Verify publish_job issues API requests and Project status update."""
    mock_client = MagicMock()
    mock_client.project_id = "PROJ_123"
    mock_client.create_issue.return_value = {
        "number": 101,
        "node_id": "ND101",
    }
    job = ScrapedJob(
        company="Acme",
        title="Engineer",
        location="Remote",
        salary="Not specified",
        source="indeed",
        apply_url="Not available",
        description="Write code.",
    )
    res = publish_job(mock_client, job, dry_run=False)
    assert res == {"number": 101, "node_id": "ND101"}
    mock_client.create_issue.assert_called_once()
    mock_client.update_project_status.assert_called_once_with("ND101", "Triage Pending")


@patch("gitemployed.scraper.time.sleep")
@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_success(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Test successful scraper execution flow using dependency injection."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Python Developer"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = MagicMock(
        project_id="PROJ123", status_field_name="Status"
    )
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = "Software Engineer"
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_resume.skills = []
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_github_client.repo = "owner/repo"
    mock_github_client.project_id = "PROJ123"
    mock_github_client.search_issues.side_effect = lambda query, **kwargs: (
        [
            {
                "title": "[Existing Company] Existing Role",
                "body": "- **Apply URL:** https://skip.com",
            }
        ]
        if "existing company" in query.lower()
        else []
    )
    mock_github_client.create_issue.return_value = {
        "number": 42,
        "node_id": "ISSUE_NODE_ID",
    }

    mock_scrape_jobs = MagicMock()
    # 3 mock postings: 1 duplicate, 1 valid new job, 1 NaN-sanitized job
    jobs_df = pd.DataFrame(
        [
            {
                "company": "Existing Company",
                "title": "Existing Role",
                "location": "Remote",
                "description": "Skip me",
                "job_url": "https://skip.com",
                "site": "linkedin",
            },
            {
                "company": "New Company",
                "title": "New Role",
                "location": "Remote",
                "description": "Scrape me",
                "job_url": "https://scrape.com",
                "site": "linkedin",
                "min_amount": 120000,
                "max_amount": 160000,
                "currency": "USD",
                "interval": "yearly",
            },
            {
                "company": float("nan"),
                "title": float("nan"),
                "location": "Remote",
                "description": float("nan"),
                "job_url": "   ",
                "job_url_direct": "https://direct.com",
                "site": "linkedin",
            },
        ]
    )
    mock_scrape_jobs.return_value = jobs_df

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}

    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Verify search_issues was called for company deduplication with expected queries
    assert mock_github_client.search_issues.call_count == 3
    search_queries = [
        call[0][0] for call in mock_github_client.search_issues.call_args_list
    ]
    assert 'repo:owner/repo is:issue in:title "[Existing Company]"' in search_queries
    assert 'repo:owner/repo is:issue in:title "[New Company]"' in search_queries

    # Verify only the new job and the NaN-sanitized job were created as issues
    assert mock_github_client.create_issue.call_count == 2
    created_titles = [
        call[1]["title"] for call in mock_github_client.create_issue.call_args_list
    ]
    assert not any("[Existing Company]" in t for t in created_titles)
    assert any("[New Company]" in t for t in created_titles)

    # Verify Projects V2 field update was called
    mock_github_client.update_project_status.assert_any_call(
        "ISSUE_NODE_ID", "Triage Pending"
    )


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_missing_env(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify scraper raises ValueError if env vars are missing."""
    mock_load_settings.return_value = MagicMock()

    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(ValueError) as exc_info,
    ):
        run_scraper()

    assert "Missing GITHUB_TOKEN" in str(exc_info.value)


@patch("gitemployed.scraper.time.sleep")
@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_robustness(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Verify scraper robustness when query fails and issue creation fails."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query 1", "Query 2"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "onsite"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_github_client.repo = "owner/repo"
    mock_github_client.project_id = None
    mock_github_client.search_issues.return_value = []
    # simulate create_issue raising exception for Query 2
    mock_github_client.create_issue.side_effect = Exception("API rate limit exceeded")

    mock_scrape_jobs = MagicMock()
    # Query 1 fails; Query 2 returns a valid job
    mock_scrape_jobs.side_effect = [
        Exception("Rate limit on LinkedIn"),
        pd.DataFrame(
            [
                {
                    "company": "Failed Company",
                    "title": "Failed Role",
                    "location": "Seattle, WA",
                    "description": "Try me",
                    "job_url": "https://failed.com",
                    "site": "linkedin",
                }
            ]
        ),
    ]

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Both scrape jobs should be attempted
    assert mock_scrape_jobs.call_count == 2
    mock_sleep.assert_called_once()


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_dry_run(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify scraper dry-run mode skips API calls."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Dry Run Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()

    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "Dry Run Company",
                "title": "Dry Run Role",
                "location": "Remote",
                "description": "Don't create me",
                "job_url": "https://dryrun.com",
                "site": "linkedin",
            }
        ]
    )

    # No environment variables set (should pass in dry-run mode)
    with patch.dict(os.environ, {}, clear=True):
        run_scraper(
            dry_run=True,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Verify existing issues were NOT listed (cache skipped)
    mock_github_client.list_issues.assert_not_called()

    # Verify scrape_jobs was still called
    mock_scrape_jobs.assert_called_once()

    # Verify create_issue was NOT called
    mock_github_client.create_issue.assert_not_called()


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_overrides(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper respects optional parameter overrides."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()
    mock_scrape_jobs.return_value = pd.DataFrame([])

    run_scraper(
        dry_run=True,
        github_client=mock_github_client,
        scrape_fn=mock_scrape_jobs,
        work_preference_override="onsite",
        job_type_override="contract",
        hours_old_override=48,
    )

    # Check that mock_scrape_jobs was called with overridden parameters
    mock_scrape_jobs.assert_called_once_with(
        site_name=["linkedin"],
        search_term="Query",
        location="Seattle, WA",
        is_remote=False,
        job_type="contract",
        hours_old=48,
        results_wanted=15,
        linkedin_fetch_description=True,
    )


@patch("gitemployed.cli.scrape.run_scraper")
@patch(
    "sys.argv",
    [
        "scrape.py",
        "--work-preference",
        "onsite",
        "--job-type",
        "parttime",
        "--hours-old",
        "72",
        "--dry-run",
    ],
)
def test_scrape_cli_args(mock_run_scraper) -> None:
    """Verify scrape.py CLI entry point parses overrides correctly."""
    from gitemployed.cli.scrape import main as cli_main

    cli_main()
    mock_run_scraper.assert_called_once_with(
        settings_path="config/settings.yaml",
        resume_path="resumes/resume.yaml",
        dry_run=True,
        work_preference_override="onsite",
        job_type_override="parttime",
        hours_old_override=72,
    )


def test_classify_work_type() -> None:
    """Verify classify_work_type correctly classifies jobs."""
    from gitemployed.scraper import classify_work_type

    assert classify_work_type("Remote", "This is hybrid") == "hybrid"
    assert classify_work_type("San Francisco (Remote)", "hybrid work") == "hybrid"
    assert (
        classify_work_type("San Francisco, CA", "This is a hybrid model role.")
        == "hybrid"
    )
    assert (
        classify_work_type("Seattle, WA", "Requires 3 days/week in office.") == "hybrid"
    )
    assert (
        classify_work_type("Dallas, TX", "This is an onsite required position.")
        == "onsite"
    )
    assert (
        classify_work_type("New York, NY", "This is a fully remote position.")
        == "remote"
    )
    assert classify_work_type("Remote", "Plain text") == "remote"
    assert classify_work_type("Chicago, IL", "Plain text") == "onsite"


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_skips_hybrid(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper filters out hybrid/onsite listings when location is Remote."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()
    # Return two jobs: one fully remote, one hybrid
    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "Remote Co",
                "title": "Remote Engineer",
                "location": "San Francisco, CA",  # City name but description is remote
                "description": "This is a fully remote role.",
                "job_url": "https://remote.com",
                "site": "linkedin",
            },
            {
                "company": "Hybrid Co",
                "title": "Hybrid Engineer",
                "location": "San Francisco, CA",
                "description": "Requires 3 days/week in office.",
                "job_url": "https://hybrid.com",
                "site": "linkedin",
            },
        ]
    )

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Only Remote Engineer should be published, Hybrid Engineer should be skipped
    assert mock_github_client.create_issue.call_count == 1
    _, kwargs = mock_github_client.create_issue.call_args
    assert "Remote Co" in kwargs["title"]
    assert "Remote Engineer" in kwargs["title"]


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_disabled(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper exits early and does nothing if search is disabled."""
    mock_settings = MagicMock()
    mock_settings.search.enabled = False
    mock_load_settings.return_value = mock_settings

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()

    run_scraper(
        dry_run=False,
        github_client=mock_github_client,
        scrape_fn=mock_scrape_jobs,
    )

    # Scraper functions and GitHub client should never have been invoked
    assert mock_load_resume.call_count == 0
    assert mock_scrape_jobs.call_count == 0
    assert mock_github_client.create_issue.call_count == 0


def test_is_local_proximity_match() -> None:
    """Verify is_local_proximity_match maps state names and enforces word boundaries."""
    from gitemployed.scraper import is_local_proximity_match

    # State matching
    assert is_local_proximity_match("Seattle, WA", "Seattle", "WA")
    assert is_local_proximity_match("Seattle, Washington", "Seattle", "Washington")
    assert is_local_proximity_match("Bellevue, WA", "Seattle", "wa")

    # Word boundary checks to prevent false positives
    assert not is_local_proximity_match("Austin, TX", "Seattle", "in")
    assert not is_local_proximity_match("Cambridge, MA", "Seattle", "ca")

    # City match fallback
    assert is_local_proximity_match("Seattle, FL", "Seattle", "wa")


@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_proximity_filtering(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper filters out local roles outside candidate's city/region."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "onsite"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.search.enabled = True
    mock_load_settings.return_value = mock_settings

    # Candidate in Seattle, WA
    mock_resume = MagicMock()
    mock_resume.basics.location.city = "Seattle"
    mock_resume.basics.location.state = "WA"
    mock_resume.basics.location.country_code = "US"
    mock_resume.to_dict.return_value = {}
    mock_load_resume.return_value = mock_resume

    # Scraped jobs: one in Seattle (match), one in Boston (mismatch)
    mock_scrape_jobs = MagicMock()
    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "Match Co",
                "title": "Onsite Engineer Seattle",
                "location": "Seattle, WA",
                "salary": "",
                "source": "linkedin",
                "apply_url": "http://match",
                "description": "Onsite position in Seattle",
            },
            {
                "company": "Mismatch Co",
                "title": "Onsite Engineer Boston",
                "location": "Boston, MA",
                "salary": "",
                "source": "linkedin",
                "apply_url": "http://mismatch",
                "description": "Onsite position in Boston",
            },
        ]
    )

    mock_github_client = MagicMock()
    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Only Match Co should be published, Mismatch Co should be skipped
    assert mock_github_client.create_issue.call_count == 1
    _, kwargs = mock_github_client.create_issue.call_args
    assert "Match Co" in kwargs["title"]


def test_normalize_job_url() -> None:
    """Verify URL normalization handles LinkedIn, Indeed, and tracking params."""
    # None and empty
    assert normalize_job_url(None) == ""
    assert normalize_job_url("") == ""
    assert normalize_job_url("   ") == ""
    assert normalize_job_url("Not available") == ""
    assert normalize_job_url("nan") == ""

    # LinkedIn with tracking parameters
    li_url = "https://www.linkedin.com/jobs/view/4421986211/?refId=abc&trackingId=xyz&position=1"
    assert normalize_job_url(li_url) == "https://linkedin.com/jobs/view/4421986211"

    # LinkedIn with role slug in URL
    li_slug = "https://linkedin.com/jobs/view/staff-software-architect-4421986211"
    assert normalize_job_url(li_slug) == "https://linkedin.com/jobs/view/4421986211"

    # LinkedIn with currentJobId query parameter
    li_param = (
        "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4421986211"
    )
    assert normalize_job_url(li_param) == "https://linkedin.com/jobs/view/4421986211"

    # Indeed with jk param
    indeed_url = "https://www.indeed.com/viewjob?jk=1234567890abcdef&from=vj&pos=top"
    assert (
        normalize_job_url(indeed_url)
        == "https://indeed.com/viewjob?jk=1234567890abcdef"
    )

    # Indeed redirect link with jk
    indeed_clk = "https://www.indeed.com/rc/clk?jk=1234567890abcdef&from=vj"
    assert (
        normalize_job_url(indeed_clk)
        == "https://indeed.com/viewjob?jk=1234567890abcdef"
    )

    # General company career URL with UTM params and trailing slash/fragment
    gen_url = "https://www.acme.com/careers/jobs/123/?utm_source=linkedin&utm_medium=job_post#apply"
    assert normalize_job_url(gen_url) == "https://acme.com/careers/jobs/123"

    # General URL keeping legitimate search params
    param_url = "https://jobs.example.com/apply?req_id=9876&utm_campaign=winter"
    assert normalize_job_url(param_url) == "https://jobs.example.com/apply?req_id=9876"

    # URL with additional ad/social tracking params stripped
    ad_url = (
        "https://jobs.example.com/apply?req_id=9876&gclid=123&fbclid=456&li_fat_id=789"
    )
    assert normalize_job_url(ad_url) == "https://jobs.example.com/apply?req_id=9876"


def test_extract_job_identifier() -> None:
    """Verify extraction of canonical job identifiers."""
    assert extract_job_identifier(None) == ""
    assert extract_job_identifier("") == ""

    # LinkedIn
    assert (
        extract_job_identifier(
            "https://www.linkedin.com/jobs/view/4421986211?refId=123"
        )
        == "linkedin:4421986211"
    )

    # Indeed
    assert (
        extract_job_identifier("https://www.indeed.com/viewjob?jk=abc123xyz&from=vj")
        == "indeed:abc123xyz"
    )

    # Other domain
    assert (
        extract_job_identifier("https://careers.google.com/jobs/results/12345")
        == "https://careers.google.com/jobs/results/12345"
    )


def test_extract_apply_url() -> None:
    """Verify Apply URL extraction from markdown issue bodies."""
    assert extract_apply_url(None) is None
    assert extract_apply_url("") is None
    assert extract_apply_url("Random body with no URL") is None

    body = """# Software Architect at PitchBook

## Job Details
- **Company:** PitchBook
- **Role:** Software Architect
- **Location:** Seattle, WA
- **Salary:** Not specified
- **Source:** linkedin
- **Apply URL:** https://www.linkedin.com/jobs/view/4421986211?refId=xyz

## Job Description
Some description here.
"""
    assert extract_apply_url(body) == "https://linkedin.com/jobs/view/4421986211"


def test_sanitize_company_for_search() -> None:
    """Verify company name sanitization for GitHub search syntax."""
    assert sanitize_company_for_search("") == ""
    assert sanitize_company_for_search('PitchBook, "Inc."') == "PitchBook, Inc."
    assert sanitize_company_for_search("[Acme] Corp:") == "Acme Corp"
    assert sanitize_company_for_search("  Amazon.com  ") == "Amazon.com"


def test_fetch_company_existing_records() -> None:
    """Verify fetching existing issue records scoped to a single company."""
    mock_client = MagicMock()
    mock_client.repo = "owner/repo"
    mock_client.search_issues.return_value = [
        {
            "title": "[PitchBook] Staff Software Architect",
            "body": "- **Apply URL:** https://www.linkedin.com/jobs/view/4421986211",
        },
        {
            "title": "[PitchBook] Lead Software Engineer",
            "body": "- **Apply URL:** https://www.indeed.com/viewjob?jk=abcdef12345",
        },
    ]

    roles, urls = fetch_company_existing_records(
        mock_client, "PitchBook", min_delay_seconds=0
    )
    assert "staff software architect" in roles
    assert "lead software engineer" in roles
    assert "linkedin:4421986211" in urls
    assert "indeed:abcdef12345" in urls


def test_fetch_company_existing_records_error_handling() -> None:
    """Verify fetch_company_existing_records handles GitHubClientError gracefully."""
    mock_client = MagicMock()
    mock_client.repo = "owner/repo"
    mock_client.search_issues.side_effect = GitHubClientError("Rate limit exceeded")

    roles, urls = fetch_company_existing_records(
        mock_client, "FaultyCo", min_delay_seconds=0
    )
    assert roles == set()
    assert urls == set()


def test_company_deduplication_store() -> None:
    """Verify CompanyDeduplicationStore two-tier deduplication and caching."""
    mock_client = MagicMock()
    mock_client.repo = "owner/repo"
    mock_client.search_issues.return_value = [
        {
            "title": "[PitchBook] Staff Software Architect",
            "body": "- **Apply URL:** https://www.linkedin.com/jobs/view/4421986211",
        },
    ]

    store = CompanyDeduplicationStore(mock_client, min_delay_seconds=0)

    # 1. Exact URL duplicate (even if title differs slightly)
    dup_job_url = ScrapedJob(
        company="PitchBook",
        title="Staff Software Architect (Hybrid)",
        location="Seattle, WA",
        salary="Not specified",
        source="linkedin",
        apply_url="https://www.linkedin.com/jobs/view/4421986211?refId=123",
        description="...",
    )
    is_dup, reason = store.is_duplicate(dup_job_url)
    assert is_dup is True
    assert "matching job identifier 'linkedin:4421986211'" in reason

    # 2. Company + Role title duplicate (even if URL is different/reposted)
    dup_job_title = ScrapedJob(
        company="PitchBook",
        title="Staff Software Architect",
        location="Seattle, WA",
        salary="Not specified",
        source="linkedin",
        apply_url="https://www.linkedin.com/jobs/view/9999999999",
        description="...",
    )
    is_dup, reason = store.is_duplicate(dup_job_title)
    assert is_dup is True
    assert "matching role title" in reason

    # 3. New job from same company
    new_job = ScrapedJob(
        company="PitchBook",
        title="Engineering Manager",
        location="Seattle, WA",
        salary="Not specified",
        source="linkedin",
        apply_url="https://www.linkedin.com/jobs/view/8888888888",
        description="...",
    )
    is_dup, _ = store.is_duplicate(new_job)
    assert is_dup is False

    # 4. Record new job and verify it's now recognized as duplicate
    store.record_job(new_job)
    is_dup, _ = store.is_duplicate(new_job)
    assert is_dup is True

    # 5. Verify search_issues was only called once for PitchBook (memoized)
    assert mock_client.search_issues.call_count == 1


def test_company_deduplication_store_none_client_dry_run() -> None:
    """Verify CompanyDeduplicationStore operates safely with None client in dry-run."""
    store = CompanyDeduplicationStore(github_client=None)
    job = ScrapedJob(
        company="TestCorp",
        title="Software Engineer",
        location="Remote",
        salary="Not specified",
        source="linkedin",
        apply_url="https://linkedin.com/jobs/view/123",
        description="...",
    )
    is_dup, reason = store.is_duplicate(job)
    assert is_dup is False
    assert reason == ""

    # Recording the job in dry-run should cache it locally
    store.record_job(job)
    is_dup, reason = store.is_duplicate(job)
    assert is_dup is True
    assert "matching job identifier" in reason


@patch("gitemployed.scraper.time.sleep")
@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_company_dedup_exact_and_repost_skipped(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Verify exact URL and reposted title variants are skipped."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Staff Software Architect"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    # Simulate issue #49 for PitchBook in the repository history
    mock_github_client = MagicMock()
    mock_github_client.repo = "menil/job-search"
    mock_github_client.project_id = None

    def mock_search(query: str, **kwargs):
        if "pitchbook" in query.lower():
            return [
                {
                    "number": 49,
                    "title": "[PitchBook] Staff Software Architect",
                    "body": (
                        "# Staff Software Architect at PitchBook\n\n"
                        "## Job Details\n"
                        "- **Company:** PitchBook\n"
                        "- **Role:** Staff Software Architect\n"
                        "- **Apply URL:** https://www.linkedin.com/jobs/view/4421986211\n"
                    ),
                }
            ]
        return []

    mock_github_client.search_issues.side_effect = mock_search

    # Both scraped jobs belong to PitchBook with matching job IDs:
    # 1. Exact match with URL query params
    # 2. Title variation ("- US") with same job ID
    mock_scrape_jobs = MagicMock()
    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "PitchBook",
                "title": "Staff Software Architect",
                "location": "Remote",
                "description": "Remote architect role",
                "job_url": "https://www.linkedin.com/jobs/view/4421986211?refId=xyz",
                "site": "linkedin",
            },
            {
                "company": "PitchBook",
                "title": "Staff Software Architect - US",
                "location": "Remote",
                "description": "Remote architect role",
                "job_url": "https://www.linkedin.com/jobs/view/4421986211?trackingId=123",
                "site": "linkedin",
            },
        ]
    )

    environ_mock = {
        "GITHUB_TOKEN": "test_token",
        "GITHUB_REPOSITORY": "menil/job-search",
    }
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Both jobs should be identified as duplicates; no issue created
    assert mock_github_client.create_issue.call_count == 0


@patch("gitemployed.scraper.time.sleep")
@patch("gitemployed.scraper.load_settings")
@patch("gitemployed.scraper.load_resume")
def test_run_scraper_company_dedup_distinct_company_creates_issue(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Verify distinct company creates an issue without false deduplication."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Staff Software Architect"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.work_preference = "remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_github_client.repo = "menil/job-search"
    mock_github_client.project_id = None

    def mock_search(query: str, **kwargs):
        if "pitchbook" in query.lower():
            return [
                {
                    "number": 49,
                    "title": "[PitchBook] Staff Software Architect",
                    "body": (
                        "# Staff Software Architect at PitchBook\n\n"
                        "## Job Details\n"
                        "- **Company:** PitchBook\n"
                        "- **Role:** Staff Software Architect\n"
                        "- **Apply URL:** https://www.linkedin.com/jobs/view/4421986211\n"
                    ),
                }
            ]
        return []

    mock_github_client.search_issues.side_effect = mock_search

    # Scraped job has identical role title as PitchBook, but is at OtherTech Corp
    mock_scrape_jobs = MagicMock()
    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "OtherTech Corp",
                "title": "Staff Software Architect",
                "location": "Remote",
                "description": "Remote architect role",
                "job_url": "https://www.linkedin.com/jobs/view/9999999999",
                "site": "linkedin",
            },
        ]
    )

    environ_mock = {
        "GITHUB_TOKEN": "test_token",
        "GITHUB_REPOSITORY": "menil/job-search",
    }
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Issue must be created for OtherTech Corp
    assert mock_github_client.create_issue.call_count == 1
    _, kwargs = mock_github_client.create_issue.call_args
    assert kwargs["title"] == "[OtherTech Corp] Staff Software Architect"
