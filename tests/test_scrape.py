"""Unit tests for the job scraper bot (gitemployed/scraper.py)."""

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from gitemployed.github_client import GitHubClientError
from gitemployed.scraper import (
    CompanyDeduplicationStore,
    LinkedInThrottle,
    ScrapedJob,
    _is_linkedin_url,
    build_issue_body,
    extract_apply_url,
    extract_job_identifier,
    fetch_company_existing_records,
    fetch_existing_jobs_cache,
    fetch_job_description,
    fetch_linkedin_description,
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

    # Test combine_skills_with_or=True (default)
    queries = generate_queries(mock_resume, None)
    assert queries == [
        "Senior Python Developer (FastAPI OR Docker OR Git OR Kubernetes OR AWS)"
    ]

    # Test single keyword combined without parentheses
    single_skill_resume = MagicMock()
    single_skill_resume.work = [MagicMock(position="Senior Python Developer")]
    single_skill_resume.skills = [MagicMock(keywords=["FastAPI"])]
    assert generate_queries(single_skill_resume, None) == [
        "Senior Python Developer FastAPI"
    ]

    # Test multi-word skills double-quoted in Boolean OR clause
    # (preventing double-quoting)
    multi_word_resume = MagicMock()
    multi_word_resume.work = [MagicMock(position="Staff Engineer")]
    multi_word_resume.skills = [
        MagicMock(keywords=['"Distributed Systems"', "Python", "Cloud Architecture"])
    ]
    assert generate_queries(multi_word_resume, None) == [
        'Staff Engineer ("Distributed Systems" OR Python OR "Cloud Architecture")'
    ]

    single_multi_word_resume = MagicMock()
    single_multi_word_resume.work = [MagicMock(position="Staff Engineer")]
    single_multi_word_resume.skills = [MagicMock(keywords=['"Machine Learning"'])]
    assert generate_queries(single_multi_word_resume, None) == [
        'Staff Engineer "Machine Learning"'
    ]

    # Test combine_skills_with_or=False (legacy individual queries)
    legacy_queries = generate_queries(mock_resume, None, combine_skills_with_or=False)
    expected_legacy = [
        "Senior Python Developer FastAPI",
        "Senior Python Developer Docker",
        "Senior Python Developer Git",
        "Senior Python Developer Kubernetes",
        "Senior Python Developer AWS",
    ]
    assert legacy_queries == expected_legacy


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
    mock_settings.search.results_wanted = 50
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

    # Only the new job is created; the NaN-sanitized row has no description to
    # fetch, so it is skipped for a later run to retry.
    assert mock_github_client.create_issue.call_count == 1
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
    mock_settings.search.results_wanted = 50
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
    mock_settings.search.results_wanted = 50
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
    mock_settings.search.results_wanted = 50
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
        results_wanted_override=30,
        combine_skills_override=False,
    )

    # Check that mock_scrape_jobs was called with overridden parameters
    mock_scrape_jobs.assert_called_once_with(
        site_name=["linkedin"],
        search_term="Query",
        location="Seattle, WA",
        is_remote=False,
        job_type="contract",
        hours_old=48,
        results_wanted=30,
        linkedin_fetch_description=False,
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
        "--results-wanted",
        "60",
        "--combine-skills",
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
        results_wanted_override=60,
        combine_skills_override=True,
    )


@patch("gitemployed.cli.scrape.run_scraper")
@patch(
    "sys.argv",
    [
        "scrape.py",
        "--no-combine-skills",
    ],
)
def test_scrape_cli_no_combine_skills(mock_run_scraper) -> None:
    """Verify scrape.py CLI parses --no-combine-skills correctly."""
    from gitemployed.cli.scrape import main as cli_main

    cli_main()
    mock_run_scraper.assert_called_once_with(
        settings_path="config/settings.yaml",
        resume_path="resumes/resume.yaml",
        dry_run=False,
        work_preference_override=None,
        job_type_override=None,
        hours_old_override=None,
        results_wanted_override=None,
        combine_skills_override=False,
    )


@patch("gitemployed.cli.scrape.run_scraper")
@patch(
    "sys.argv",
    [
        "scrape.py",
        "-n",
        "40",
    ],
)
def test_scrape_cli_short_args(mock_run_scraper) -> None:
    """Verify scrape.py CLI entry point parses short options correctly."""
    from gitemployed.cli.scrape import main as cli_main

    cli_main()
    mock_run_scraper.assert_called_once_with(
        settings_path="config/settings.yaml",
        resume_path="resumes/resume.yaml",
        dry_run=False,
        work_preference_override=None,
        job_type_override=None,
        hours_old_override=None,
        results_wanted_override=40,
        combine_skills_override=None,
    )


def test_run_scraper_invalid_bounds() -> None:
    """Verify run_scraper raises ValueError when hours_old or results_wanted <= 0."""
    with (
        patch("gitemployed.scraper.load_settings") as mock_settings,
        patch("gitemployed.scraper.load_resume"),
    ):
        mock_settings.return_value.search.enabled = True
        mock_settings.return_value.search.work_preference = "hybrid"
        mock_settings.return_value.search.hours_old = 48
        mock_settings.return_value.search.results_wanted = 50
        with pytest.raises(ValueError, match="Invalid hours_old"):
            run_scraper(dry_run=True, hours_old_override=0)
        with pytest.raises(ValueError, match="Invalid results_wanted"):
            run_scraper(dry_run=True, results_wanted_override=-1)


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
    mock_settings.search.results_wanted = 50
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
    mock_settings.search.results_wanted = 50
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
    assert sanitize_company_for_search("[Acme] Corp") == "Acme Corp"
    assert sanitize_company_for_search("  Amazon.com  ") == "Amazon.com"
    assert (
        sanitize_company_for_search("Re:Build Manufacturing")
        == "Re:Build Manufacturing"
    )
    assert sanitize_company_for_search(r"Company\Name") == "CompanyName"


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


def test_fetch_company_existing_records_with_colon() -> None:
    """Verify fetching existing records for a company with an internal colon."""
    mock_client = MagicMock()
    mock_client.repo = "owner/repo"
    mock_client.search_issues.return_value = [
        {
            "title": "[Re:Build Manufacturing] Senior Backend Engineer",
            "body": "- **Apply URL:** https://www.linkedin.com/jobs/view/4461744897",
        },
    ]

    roles, urls = fetch_company_existing_records(
        mock_client, "Re:Build Manufacturing", min_delay_seconds=0
    )
    mock_client.search_issues.assert_called_once_with(
        'repo:owner/repo is:issue in:title "[Re:Build Manufacturing]"',
        per_page=100,
        page=1,
    )
    assert "senior backend engineer" in roles
    assert "linkedin:4461744897" in urls


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
    mock_settings.search.results_wanted = 50
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
    mock_settings.search.results_wanted = 50
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


def test_fetch_linkedin_description_success() -> None:
    """Verify fetch_linkedin_description extracts HTML markup into markdown."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """
    <html>
      <body>
        <div class="show-more-less-html__markup">
          <p>We are looking for a <strong>Senior Engineer</strong>.</p>
          <ul>
            <li>Python</li>
            <li>Distributed Systems</li>
          </ul>
        </div>
      </body>
    </html>
    """
    mock_session.get.return_value = mock_resp

    desc = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
    )
    assert desc is not None
    assert "Senior Engineer" in desc
    assert "Python" in desc


def test_fetch_linkedin_description_http_error() -> None:
    """Verify fetch_linkedin_description handles non-200 responses gracefully."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_session.get.return_value = mock_resp

    desc = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
    )
    assert desc is None
    assert mock_session.get.call_count == 1


def _linkedin_resp(status: int, text: str = "", retry_after: str | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {"Retry-After": retry_after} if retry_after else {}
    return resp


def test_fetch_linkedin_description_retries_429_then_succeeds() -> None:
    """A throttled request is retried, honoring Retry-After."""
    ok_html = '<div class="show-more-less-html__markup"><p>Real JD</p></div>'
    mock_session = MagicMock()
    mock_session.get.side_effect = [
        _linkedin_resp(429, retry_after="3"),
        _linkedin_resp(429),
        _linkedin_resp(200, ok_html),
    ]
    sleeps: list[float] = []

    desc = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
        sleep_fn=sleeps.append,
    )

    assert desc is not None and "Real JD" in desc
    # Retry-After on the first 429, exponential backoff (2s * 2^1) on the second.
    assert sleeps == [3.0, 4.0]


def test_fetch_linkedin_description_gives_up_after_max_429_retries() -> None:
    """Persistent throttling stops after the bounded retries and returns None."""
    mock_session = MagicMock()
    mock_session.get.return_value = _linkedin_resp(429, retry_after="999")
    sleeps: list[float] = []

    desc = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
        sleep_fn=sleeps.append,
    )

    assert desc is None
    assert mock_session.get.call_count == 3
    # An oversized Retry-After is capped so the scrape is never stalled.
    assert sleeps == [30.0, 30.0]


def test_fetch_linkedin_description_fallback_and_missing_markup() -> None:
    """Verify fallback selector and missing markup handling."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # Fallback to description__text selector
    mock_resp.text = """
    <html>
      <body>
        <div class="description__text">
          <p>Role responsibilities and qualifications.</p>
        </div>
      </body>
    </html>
    """
    mock_session.get.return_value = mock_resp

    desc = fetch_linkedin_description(
        "https://www.linkedin.com/jobs/search/?currentJobId=9876543210",
        session=mock_session,
    )
    assert desc is not None
    assert "Role responsibilities and qualifications." in desc
    # Target URL should be canonicalized to /jobs/view/9876543210
    called_url = mock_session.get.call_args[0][0]
    assert called_url == "https://www.linkedin.com/jobs/view/9876543210"

    # Missing markup container (e.g. login wall) returns None
    mock_resp.text = "<html><body><div>Sign In to LinkedIn</div></body></html>"
    desc_missing = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
    )
    assert desc_missing is None


def test_fetch_linkedin_description_network_exception() -> None:
    """Verify fetch_linkedin_description catches network exceptions safely."""
    import requests

    mock_session = MagicMock()
    mock_session.get.side_effect = requests.RequestException("Connection reset by peer")

    desc = fetch_linkedin_description(
        "https://linkedin.com/jobs/view/1234567890",
        session=mock_session,
    )
    assert desc is None


def test_is_linkedin_url() -> None:
    """Verify LinkedIn URL validation prevents spoofing and handles subdomains."""
    assert _is_linkedin_url("https://www.linkedin.com/jobs/view/12345") is True
    assert _is_linkedin_url("https://linkedin.com/jobs/view/12345") is True
    assert _is_linkedin_url("https://uk.linkedin.com/jobs/view/12345") is True
    assert _is_linkedin_url("linkedin.com/jobs/view/12345") is True
    assert _is_linkedin_url("https://spoofed-linkedin.com/jobs/view/12345") is False
    assert _is_linkedin_url("https://attacker.com?ref=linkedin.com") is False
    assert _is_linkedin_url("https://attacker.com/linkedin.com") is False
    assert _is_linkedin_url("https://linkedin.com.attacker.com") is False
    assert _is_linkedin_url("https://indeed.com/viewjob?jk=123") is False
    assert _is_linkedin_url("") is False
    assert _is_linkedin_url(None) is False


def test_fetch_linkedin_description_invalid_or_missing() -> None:
    """Verify fetch_linkedin_description returns None for invalid or spoofed URLs."""
    assert fetch_linkedin_description(None) is None
    assert fetch_linkedin_description("") is None
    assert fetch_linkedin_description("https://indeed.com/viewjob?jk=123") is None
    assert (
        fetch_linkedin_description("https://spoofed-linkedin.com/jobs/view/1234567890")
        is None
    )
    assert fetch_linkedin_description("https://attacker.com?ref=linkedin.com") is None


def test_fetch_job_description_already_present() -> None:
    """Verify fetch_job_description skips network call when description exists."""
    job = ScrapedJob(
        company="Acme Corp",
        title="Engineer",
        location="Seattle, WA",
        salary="N/A",
        source="indeed",
        apply_url="https://acme.com/apply",
        description="Existing detailed description.",
    )
    mock_session = MagicMock()
    res = fetch_job_description(job, session=mock_session)
    assert res == "Existing detailed description."
    mock_session.get.assert_not_called()


def test_fetch_job_description_skips_non_linkedin() -> None:
    """Verify fetch_job_description skips network fetch for non-LinkedIn URLs."""
    job = ScrapedJob(
        company="Acme Corp",
        title="Engineer",
        location="Seattle, WA",
        salary="N/A",
        source="linkedin",
        apply_url="https://spoofed-linkedin.com/jobs/view/12345",
        description="",
    )
    mock_session = MagicMock()
    res = fetch_job_description(job, session=mock_session)
    assert res is None
    mock_session.get.assert_not_called()


def test_run_scraper_lazy_fetch_skips_duplicates_and_proximity() -> None:
    """Verify run_scraper only fetches descriptions for candidate jobs."""
    mock_github_client = MagicMock()
    mock_github_client.repo = "owner/repo"
    mock_github_client.project_id = None

    def mock_search(query: str, **kwargs):
        if "dupcompany" in query.lower():
            return [
                {
                    "title": "[DupCompany] Staff Engineer",
                    "body": "- **Apply URL:** https://linkedin.com/jobs/view/11111",
                }
            ]
        return []

    mock_github_client.search_issues.side_effect = mock_search

    # 3 mock postings without descriptions:
    # 1. Duplicate job at DupCompany
    # 2. Out-of-region job in Dallas, TX (candidate is in Seattle, WA)
    # 3. Valid candidate job at FreshCompany in Seattle, WA
    jobs_df = pd.DataFrame(
        [
            {
                "company": "DupCompany",
                "title": "Staff Engineer",
                "location": "Seattle, WA",
                "description": None,
                "job_url": "https://linkedin.com/jobs/view/11111",
                "site": "linkedin",
            },
            {
                "company": "FarCompany",
                "title": "Staff Engineer",
                "location": "Dallas, TX",
                "description": None,
                "job_url": "https://linkedin.com/jobs/view/22222",
                "site": "linkedin",
            },
            {
                "company": "FreshCompany",
                "title": "Staff Engineer",
                "location": "Seattle, WA",
                "description": None,
                "job_url": "https://linkedin.com/jobs/view/33333",
                "site": "linkedin",
            },
        ]
    )

    mock_scrape_jobs = MagicMock(return_value=jobs_df)
    mock_fetch_desc = MagicMock(
        return_value="Detailed job description for FreshCompany"
    )

    mock_settings = MagicMock()
    mock_settings.search.enabled = True
    mock_settings.search.work_preference = "hybrid"
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 48
    mock_settings.search.results_wanted = 50
    mock_settings.search.combine_skills_with_or = True
    mock_settings.custom_queries = ["Staff Engineer"]
    mock_settings.projects_v2 = None

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_resume.skills = []

    with (
        patch("gitemployed.scraper.load_settings", return_value=mock_settings),
        patch("gitemployed.scraper.load_resume", return_value=mock_resume),
        patch("gitemployed.scraper.time.sleep"),
    ):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
            fetch_description_fn=mock_fetch_desc,
        )

    # Scrape called with linkedin_fetch_description=False
    mock_scrape_jobs.assert_called_once()
    assert mock_scrape_jobs.call_args[1]["linkedin_fetch_description"] is False

    # fetch_description_fn MUST ONLY be called for FreshCompany (Job 3)!
    assert mock_fetch_desc.call_count == 1
    called_job = mock_fetch_desc.call_args[0][0]
    assert called_job.company == "FreshCompany"
    assert called_job.apply_url == "https://linkedin.com/jobs/view/33333"

    # Only 1 issue created for FreshCompany
    assert mock_github_client.create_issue.call_count == 1
    created_title = mock_github_client.create_issue.call_args[1]["title"]
    assert created_title == "[FreshCompany] Staff Engineer"


def test_run_scraper_skips_job_when_description_missing_then_retries() -> None:
    """A job whose description cannot be fetched is skipped, not filed.

    It must not be recorded as seen, so a later run that does retrieve a
    description files it normally.
    """
    mock_github_client = MagicMock()
    mock_github_client.repo = "owner/repo"
    mock_github_client.project_id = None
    mock_github_client.search_issues.return_value = []

    jobs_df = pd.DataFrame(
        [
            {
                "company": "RemoteTech Inc",
                "title": "Backend Engineer",
                "location": "United States",
                "description": None,
                "job_url": "https://linkedin.com/jobs/view/99999",
                "site": "linkedin",
            }
        ]
    )

    mock_scrape_jobs = MagicMock(return_value=jobs_df)
    # Simulate failed description fetch
    mock_fetch_desc = MagicMock(return_value=None)

    mock_settings = MagicMock()
    mock_settings.search.enabled = True
    mock_settings.search.work_preference = "remote"
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 48
    mock_settings.search.results_wanted = 50
    mock_settings.search.combine_skills_with_or = True
    mock_settings.custom_queries = ["Backend Engineer"]
    mock_settings.projects_v2 = None

    mock_resume = MagicMock()
    mock_resume.basics.location = MagicMock(
        city="Seattle", state="WA", country_code="US"
    )
    mock_resume.skills = []

    with (
        patch("gitemployed.scraper.load_settings", return_value=mock_settings),
        patch("gitemployed.scraper.load_resume", return_value=mock_resume),
        patch("gitemployed.scraper.time.sleep"),
    ):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
            fetch_description_fn=mock_fetch_desc,
        )

    # No description -> nothing to triage -> no issue filed this run.
    mock_github_client.create_issue.assert_not_called()

    # A later run (the fetch now succeeds) files the same job.
    mock_fetch_desc.return_value = "Fully remote backend role."
    with (
        patch("gitemployed.scraper.load_settings", return_value=mock_settings),
        patch("gitemployed.scraper.load_resume", return_value=mock_resume),
        patch("gitemployed.scraper.time.sleep"),
    ):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
            fetch_description_fn=mock_fetch_desc,
        )

    assert mock_github_client.create_issue.call_count == 1
    assert (
        mock_github_client.create_issue.call_args[1]["title"]
        == "[RemoteTech Inc] Backend Engineer"
    )


def test_linkedin_throttle_trips_and_skips_further_requests() -> None:
    """After consecutive exhausted 429s the rest of the run makes no requests."""
    mock_session = MagicMock()
    mock_session.get.return_value = _linkedin_resp(429)
    throttle = LinkedInThrottle(max_consecutive_exhausted=2)
    url = "https://linkedin.com/jobs/view/1234567890"

    for _ in range(2):
        assert (
            fetch_linkedin_description(
                url, session=mock_session, sleep_fn=lambda _s: None, throttle=throttle
            )
            is None
        )
    assert throttle.tripped
    calls_before = mock_session.get.call_count

    assert (
        fetch_linkedin_description(
            url, session=mock_session, sleep_fn=lambda _s: None, throttle=throttle
        )
        is None
    )
    assert mock_session.get.call_count == calls_before


def test_linkedin_throttle_resets_after_a_successful_fetch() -> None:
    ok_html = '<div class="show-more-less-html__markup"><p>Real JD</p></div>'
    mock_session = MagicMock()
    mock_session.get.side_effect = [
        _linkedin_resp(429),
        _linkedin_resp(429),
        _linkedin_resp(429),  # first job exhausts its retries
        _linkedin_resp(200, ok_html),  # second job succeeds
    ]
    throttle = LinkedInThrottle(max_consecutive_exhausted=2)
    url = "https://linkedin.com/jobs/view/1234567890"

    fetch_linkedin_description(
        url, session=mock_session, sleep_fn=lambda _s: None, throttle=throttle
    )
    assert throttle.consecutive_exhausted == 1
    desc = fetch_linkedin_description(
        url, session=mock_session, sleep_fn=lambda _s: None, throttle=throttle
    )

    assert desc is not None
    assert throttle.consecutive_exhausted == 0
    assert not throttle.tripped


def test_linkedin_throttle_warns_once_when_tripped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    throttle = LinkedInThrottle(max_consecutive_exhausted=2)

    with caplog.at_level("WARNING", logger="job_scraper"):
        for _ in range(5):
            throttle.record_exhausted()

    assert len([r for r in caplog.records if "throttled" in r.message]) == 1


def test_scrape_kwargs_match_real_jobspy_signature() -> None:
    """The kwargs scrape_listings passes must exist in the installed jobspy.

    Every other scrape test injects a mock scrape_fn, so a jobspy release that
    renames or drops a parameter would otherwise only surface in production.
    """
    import inspect

    from jobspy import scrape_jobs

    params = inspect.signature(scrape_jobs).parameters
    passed = {
        "site_name",
        "search_term",
        "location",
        "is_remote",
        "job_type",
        "hours_old",
        "results_wanted",
        "linkedin_fetch_description",
    }
    assert not passed - params.keys()
