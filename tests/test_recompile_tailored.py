"""Unit tests for gitemployed.cli.recompile_tailored."""

import pathlib
from unittest import mock

import pytest

from gitemployed.assistant import STATUS_CONFIRMATION_MARKER
from gitemployed.cli.recompile_tailored import (
    _load_base_resume,
    main,
    recompile_and_commit,
)
from gitemployed.git_ops import GitOpsError
from gitemployed.schema import Basics, Resume


@pytest.fixture
def sample_resume() -> Resume:
    """Fixture returning a valid Resume object."""
    return Resume(
        basics=Basics(
            name="Jane Doe",
            email="jane@example.com",
            summary="Experienced Engineer",
        )
    )


def test_load_base_resume_success(tmp_path: pathlib.Path) -> None:
    """Test loading base resume succeeds with valid git show output."""
    yaml_text = "basics:\n  name: Jane Doe\n"
    with mock.patch(
        "gitemployed.cli.recompile_tailored.run_git", return_value=yaml_text
    ) as mock_git:
        res = _load_base_resume(tmp_path, base_branch="main")
        assert res is not None
        assert res.basics is not None
        assert res.basics.name == "Jane Doe"
        mock_git.assert_called_once_with(
            ["show", "main:resumes/resume.yaml"], cwd=tmp_path
        )


def test_load_base_resume_fallback_to_origin(tmp_path: pathlib.Path) -> None:
    """Test loading base resume falls back to origin/main when main fails."""
    yaml_text = "basics:\n  name: Origin Jane\n"

    def fake_run_git(args: list[str], cwd: pathlib.Path) -> str:
        if args[1] == "main:resumes/resume.yaml":
            raise GitOpsError("unknown revision main")
        if args[1] == "origin/main:resumes/resume.yaml":
            return yaml_text
        return ""

    with mock.patch(
        "gitemployed.cli.recompile_tailored.run_git", side_effect=fake_run_git
    ):
        res = _load_base_resume(tmp_path, base_branch="main")
        assert res is not None
        assert res.basics is not None
        assert res.basics.name == "Origin Jane"


def test_load_base_resume_failure(tmp_path: pathlib.Path) -> None:
    """Test loading base resume returns None when git show fails."""
    with mock.patch(
        "gitemployed.cli.recompile_tailored.run_git",
        side_effect=GitOpsError("file not found"),
    ):
        res = _load_base_resume(tmp_path, base_branch="main")
        assert res is None


@mock.patch("gitemployed.cli.recompile_tailored.push_branch")
@mock.patch("gitemployed.cli.recompile_tailored.generate_pdf_diff")
@mock.patch("gitemployed.cli.recompile_tailored.compile_resume")
@mock.patch(
    "gitemployed.cli.recompile_tailored.ensure_theme_installed", return_value="theme"
)
@mock.patch("gitemployed.cli.recompile_tailored.run_git")
def test_recompile_and_commit_success(
    mock_run_git: mock.MagicMock,
    mock_theme: mock.MagicMock,
    mock_compile: mock.MagicMock,
    mock_pdf_diff: mock.MagicMock,
    mock_push: mock.MagicMock,
    tmp_path: pathlib.Path,
    sample_resume: Resume,
) -> None:
    """Test recompile_and_commit compiles artifacts, commits and pushes."""
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir(parents=True)
    yaml_path = resumes_dir / "resume.yaml"
    yaml_path.write_text("basics:\n  name: Jane Doe\n", encoding="utf-8")

    # Mock git calls
    def fake_run_git(args: list[str], cwd: pathlib.Path) -> str:
        if args[0] == "show":
            return "basics:\n  name: Base Jane\n"
        if args[0] == "diff":
            return "resumes/jane_doe_resume.pdf\n"
        return ""

    mock_run_git.side_effect = fake_run_git

    res = recompile_and_commit(
        repo_path=tmp_path,
        branch_name="applications/jane-swe-12345",
        base_branch="main",
        dry_run=False,
    )

    assert res.diff_generated is True
    assert res.candidate_name == "Jane Doe"
    assert res.pdf_filename == "jane_doe_resume.pdf"
    assert res.diff_filename == "jane_doe_resume_diff.pdf"
    assert res.committed is True

    mock_compile.assert_called()
    mock_pdf_diff.assert_called_once()
    mock_push.assert_called_once_with(tmp_path, "applications/jane-swe-12345")


@mock.patch("gitemployed.cli.recompile_tailored.push_branch")
@mock.patch("gitemployed.cli.recompile_tailored.generate_pdf_diff")
@mock.patch("gitemployed.cli.recompile_tailored.compile_resume")
@mock.patch(
    "gitemployed.cli.recompile_tailored.ensure_theme_installed", return_value="theme"
)
@mock.patch("gitemployed.cli.recompile_tailored.run_git")
def test_recompile_and_commit_dry_run(
    mock_run_git: mock.MagicMock,
    mock_theme: mock.MagicMock,
    mock_compile: mock.MagicMock,
    mock_pdf_diff: mock.MagicMock,
    mock_push: mock.MagicMock,
    tmp_path: pathlib.Path,
) -> None:
    """Test recompile_and_commit skips commit and push in dry_run mode."""
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir(parents=True)
    yaml_path = resumes_dir / "resume.yaml"
    yaml_path.write_text("basics:\n  name: Jane Doe\n", encoding="utf-8")

    mock_run_git.side_effect = lambda args, cwd: (
        "basics:\n  name: Base Jane\n" if args[0] == "show" else ""
    )

    res = recompile_and_commit(
        repo_path=tmp_path,
        branch_name="applications/jane-swe-12345",
        base_branch="main",
        dry_run=True,
    )

    assert res.committed is False
    mock_push.assert_not_called()


def test_main_cli_success(tmp_path: pathlib.Path) -> None:
    """Test CLI main entrypoint invocation."""
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir(parents=True)
    yaml_path = resumes_dir / "resume.yaml"
    yaml_path.write_text("basics:\n  name: Jane Doe\n", encoding="utf-8")

    with (
        mock.patch(
            "gitemployed.cli.recompile_tailored.recompile_and_commit"
        ) as mock_recompile,
    ):
        mock_recompile.return_value = mock.MagicMock(
            diff_generated=True, committed=True
        )
        ret = main(
            [
                "--repo-path",
                str(tmp_path),
                "--branch",
                "applications/test-branch",
                "--dry-run",
            ]
        )
        assert ret == 0
        mock_recompile.assert_called_once_with(
            repo_path=tmp_path,
            branch_name="applications/test-branch",
            base_branch="main",
            dry_run=True,
        )


def test_build_update_comment() -> None:
    """Test building markdown update comment."""
    from gitemployed.cli.recompile_tailored import build_update_comment

    comment = build_update_comment(
        repo="owner/repo",
        branch_name="applications/swe-google-12345",
        yaml_rel_path="resumes/resume.yaml",
        pdf_filename="jane_doe_resume.pdf",
        diff_filename="jane_doe_resume_diff.pdf",
        diff_generated=True,
        apply_url="https://jobs.example.com/apply",
        inline_diff="<details><summary>Diff</summary></details>",
    )
    assert STATUS_CONFIRMATION_MARKER in comment
    assert "### Tailored Resume Updated" in comment
    assert (
        "[applications/swe-google-12345](https://github.com/owner/repo/tree/applications/swe-google-12345)"
        in comment
    )
    assert (
        "[View/Download PDF](https://github.com/owner/repo/blob/applications/swe-google-12345/resumes/jane_doe_resume.pdf)"
        in comment
    )
    assert (
        "[View Visual Diff PDF](https://github.com/owner/repo/blob/applications/swe-google-12345/resumes/jane_doe_resume_diff.pdf)"
        in comment
    )
    assert (
        '<a href="https://jobs.example.com/apply" target="_blank">Link to Posting</a>'
        in comment
    )
    assert "<details><summary>Diff</summary></details>" in comment


def test_post_recompile_comment_found(tmp_path: pathlib.Path) -> None:
    """Test post_recompile_comment posts comment when issue is found."""
    from gitemployed.cli.recompile_tailored import (
        RecompileResult,
        post_recompile_comment,
    )
    from gitemployed.github_client import GitHubClient

    mock_gh = mock.MagicMock(spec=GitHubClient)
    mock_gh.repo = "owner/repo"
    mock_gh.find_issue_by_branch.return_value = {
        "number": 101,
        "title": "[Google] SWE",
        "body": "**Apply URL:** https://example.com/job",
    }

    res = RecompileResult(
        diff_generated=True,
        candidate_name="Jane Doe",
        yaml_filename="resume.yaml",
        json_filename="jane_doe_resume.json",
        pdf_filename="jane_doe_resume.pdf",
        diff_filename="jane_doe_resume_diff.pdf",
        committed=True,
    )

    with mock.patch(
        "gitemployed.cli.recompile_tailored._build_inline_diff_section",
        return_value="<diff-section>",
    ):
        posted = post_recompile_comment(
            repo_path=tmp_path,
            branch_name="applications/swe-google-12345",
            recompile_result=res,
            gh_client=mock_gh,
        )

    assert posted is True
    mock_gh.post_comment.assert_called_once()
    args, _ = mock_gh.post_comment.call_args
    assert args[0] == 101
    assert "### Tailored Resume Updated" in args[1]


def test_post_recompile_comment_post_error(tmp_path: pathlib.Path) -> None:
    """Test post_recompile_comment handles post_comment exception gracefully."""
    from gitemployed.cli.recompile_tailored import (
        RecompileResult,
        post_recompile_comment,
    )
    from gitemployed.github_client import GitHubClient

    mock_gh = mock.MagicMock(spec=GitHubClient)
    mock_gh.repo = "owner/repo"
    mock_gh.find_issue_by_branch.return_value = {
        "number": 101,
        "title": "[Google] SWE",
        "body": "**Apply URL:** https://example.com/job",
    }
    mock_gh.post_comment.side_effect = RuntimeError("network timeout")

    res = RecompileResult(
        diff_generated=True,
        candidate_name="Jane Doe",
        yaml_filename="resume.yaml",
        json_filename="jane_doe_resume.json",
        pdf_filename="jane_doe_resume.pdf",
        diff_filename="jane_doe_resume_diff.pdf",
        committed=True,
    )

    with mock.patch(
        "gitemployed.cli.recompile_tailored._build_inline_diff_section",
        return_value="",
    ):
        posted = post_recompile_comment(
            repo_path=tmp_path,
            branch_name="applications/swe-google-12345",
            recompile_result=res,
            gh_client=mock_gh,
        )
        assert posted is False


def test_post_recompile_comment_not_found(tmp_path: pathlib.Path) -> None:
    """Test post_recompile_comment returns False when no issue matches branch."""
    from gitemployed.cli.recompile_tailored import (
        RecompileResult,
        post_recompile_comment,
    )
    from gitemployed.github_client import GitHubClient

    mock_gh = mock.MagicMock(spec=GitHubClient)
    mock_gh.find_issue_by_branch.return_value = None

    res = RecompileResult(
        diff_generated=False,
        candidate_name=None,
        yaml_filename="resume.yaml",
        json_filename="resume.json",
        pdf_filename="resume.pdf",
        diff_filename="resume_diff.pdf",
        committed=False,
    )

    posted = post_recompile_comment(
        repo_path=tmp_path,
        branch_name="applications/unknown-12345",
        recompile_result=res,
        gh_client=mock_gh,
    )
    assert posted is False
    mock_gh.post_comment.assert_not_called()


def test_recompile_tailored_workflow_file_valid() -> None:
    """Verify that template workflow file is valid and configured correctly."""
    import yaml

    workflow_path = (
        pathlib.Path(__file__).parent.parent
        / "template"
        / ".github"
        / "workflows"
        / "recompile-tailored-resume.yml"
    )
    assert workflow_path.is_file(), f"Workflow file not found: {workflow_path}"

    with workflow_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert data["name"] == "Recompile Tailored Resume"
    # PyYAML parses unquoted 'on:' as boolean True (YAML 1.1 spec).
    # Check string 'on' key first, falling back to boolean True for
    # PyYAML compatibility.
    triggers = data.get("on") if "on" in data else data.get(True, {})
    assert triggers is not None
    assert "push" in triggers
    assert "applications/**" in triggers["push"]["branches"]
    assert "resumes/resume.yaml" in triggers["push"]["paths"]
    assert data["permissions"]["contents"] == "write"
    assert data["permissions"]["issues"] == "write"
    assert "recompile" in data["jobs"]
    job = data["jobs"]["recompile"]
    assert "ghcr.io/menil/gitemployed" in job["container"]["image"]
    steps = job["steps"]
    step_runs = [s.get("run", "") for s in steps if "run" in s]
    assert any("gitemployed.cli.recompile_tailored" in r for r in step_runs)


def test_main_cli_inferred_branch_failure(tmp_path: pathlib.Path) -> None:
    """Test CLI fails when branch is omitted and git rev-parse fails."""
    with mock.patch(
        "gitemployed.cli.recompile_tailored.run_git",
        side_effect=GitOpsError("rev-parse failed"),
    ):
        ret = main(["--repo-path", str(tmp_path)])
        assert ret == 1


def test_main_cli_recompile_exception(tmp_path: pathlib.Path) -> None:
    """Test CLI handles recompile exception gracefully returning EXIT_ERROR."""
    with (
        mock.patch(
            "gitemployed.cli.recompile_tailored.recompile_and_commit",
            side_effect=RuntimeError("compilation boom"),
        ),
    ):
        ret = main(["--repo-path", str(tmp_path), "--branch", "applications/b"])
        assert ret == 1


def test_main_cli_comment_posting_with_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test CLI posts comment when GITHUB_TOKEN and GITHUB_REPOSITORY are set."""
    monkeypatch.setenv("GITHUB_TOKEN", "mock-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    mock_res = mock.MagicMock(diff_generated=True, committed=True)
    with (
        mock.patch(
            "gitemployed.cli.recompile_tailored.recompile_and_commit",
            return_value=mock_res,
        ),
        mock.patch(
            "gitemployed.cli.recompile_tailored.post_recompile_comment"
        ) as mock_post,
    ):
        ret = main(["--repo-path", str(tmp_path), "--branch", "applications/b"])
        assert ret == 0
        mock_post.assert_called_once()


def test_main_cli_head_detached_branch_failure(tmp_path: pathlib.Path) -> None:
    """Test CLI fails when branch is omitted and HEAD is detached ('HEAD')."""
    with mock.patch(
        "gitemployed.cli.recompile_tailored.run_git",
        return_value="HEAD",
    ):
        ret = main(["--repo-path", str(tmp_path)])
        assert ret == 1


def test_main_cli_comment_skipped_when_not_committed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test CLI skips posting comment when nothing was committed."""
    monkeypatch.setenv("GITHUB_TOKEN", "mock-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    mock_res = mock.MagicMock(diff_generated=True, committed=False)
    with (
        mock.patch(
            "gitemployed.cli.recompile_tailored.recompile_and_commit",
            return_value=mock_res,
        ),
        mock.patch(
            "gitemployed.cli.recompile_tailored.post_recompile_comment"
        ) as mock_post,
    ):
        ret = main(["--repo-path", str(tmp_path), "--branch", "applications/b"])
        assert ret == 0
        mock_post.assert_not_called()


@mock.patch("gitemployed.cli.recompile_tailored.push_branch")
@mock.patch("gitemployed.cli.recompile_tailored.generate_pdf_diff")
@mock.patch("gitemployed.cli.recompile_tailored.compile_resume")
@mock.patch(
    "gitemployed.cli.recompile_tailored.ensure_theme_installed", return_value="theme"
)
@mock.patch("gitemployed.cli.recompile_tailored.run_git")
def test_recompile_and_commit_legacy_cleanup(
    mock_run_git: mock.MagicMock,
    mock_theme: mock.MagicMock,
    mock_compile: mock.MagicMock,
    mock_pdf_diff: mock.MagicMock,
    mock_push: mock.MagicMock,
    tmp_path: pathlib.Path,
) -> None:
    """Test legacy files are removed when candidate prefix is present."""
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir(parents=True)
    yaml_path = resumes_dir / "resume.yaml"
    yaml_path.write_text("basics:\n  name: Jane Doe\n", encoding="utf-8")

    # Create legacy files
    (resumes_dir / "resume.json").write_text("{}", encoding="utf-8")
    (resumes_dir / "resume.pdf").write_text("dummy pdf", encoding="utf-8")
    (resumes_dir / "jane_doe_resume.yaml").write_text("dummy", encoding="utf-8")

    def fake_run_git(args: list[str], cwd: pathlib.Path) -> str:
        if args[0] == "show":
            return "basics:\n  name: Base\n"
        if args[0] == "diff":
            return "resumes/resume.json\n"
        return ""

    mock_run_git.side_effect = fake_run_git

    res = recompile_and_commit(
        repo_path=tmp_path,
        branch_name="applications/jane-swe-12345",
        base_branch="main",
        dry_run=False,
    )

    assert not (resumes_dir / "resume.json").exists()
    assert not (resumes_dir / "resume.pdf").exists()
    assert not (resumes_dir / "jane_doe_resume.yaml").exists()
    assert res.candidate_name == "Jane Doe"
    assert res.diff_generated is True
    assert res.committed is True
    mock_compile.assert_called()
    mock_pdf_diff.assert_called_once()
    mock_push.assert_called_once_with(tmp_path, "applications/jane-swe-12345")
