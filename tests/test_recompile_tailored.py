"""Unit tests for gitemployed.cli.recompile_tailored."""

import pathlib
from unittest import mock

import pytest

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
