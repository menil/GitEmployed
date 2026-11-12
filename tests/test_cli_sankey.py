"""Unit tests for the Sankey CLI entry point (gitemployed.cli.sankey)."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from unittest.mock import MagicMock, patch

import pytest

from gitemployed.cli.sankey import (
    _parse_date,
    _parse_positive_int,
    _write_step_summary,
    main,
)
from gitemployed.github_client import GitHubClientError
from gitemployed.sankey import SankeyFlow

DEFAULT_ENV = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
DEFAULT_ARGV = ["sankey.py"]


def run_main(
    env: dict[str, str] | None = None,
    argv: list[str] | None = None,
    expected_code: int | None = None,
) -> int | None:
    with (
        patch.dict(os.environ, DEFAULT_ENV if env is None else env, clear=True),
        patch("sys.argv", DEFAULT_ARGV if argv is None else argv),
    ):
        if expected_code is None:
            main()
            return None
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == expected_code
        return exc_info.value.code


def test_parse_helpers() -> None:
    assert _parse_positive_int("10") == 10
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_positive_int("-5")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_positive_int("invalid")

    assert _parse_date("2026-01-01") == dt.date(2026, 1, 1)
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_date("bad-date")


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_missing_env_vars(
    mock_client: MagicMock,
    mock_flows: MagicMock,
    caplog,
) -> None:
    run_main(env={"GITHUB_REPOSITORY": "owner/repo"}, expected_code=1)
    assert "GITHUB_TOKEN environment variable is missing" in caplog.text

    run_main(env={"GITHUB_TOKEN": "token"}, expected_code=1)
    assert "GITHUB_REPOSITORY environment variable is missing" in caplog.text


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_successful_stdout_run(
    mock_client: MagicMock,
    mock_generate: MagicMock,
    capsys,
) -> None:
    mock_generate.return_value = [SankeyFlow("Applied", "In Loop", 5)]
    run_main(argv=["sankey.py", "--format", "sankeymatic"])

    captured = capsys.readouterr()
    assert "Applied [5] In Loop" in captured.out


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_output_file_and_step_summary(
    mock_client: MagicMock,
    mock_generate: MagicMock,
    tmp_path,
) -> None:
    mock_generate.return_value = [SankeyFlow("Applied", "In Loop", 5)]
    out_file = tmp_path / "out.txt"
    summary_file = tmp_path / "summary.md"

    run_main(
        argv=[
            "sankey.py",
            "--format",
            "mermaid",
            "--output",
            str(out_file),
            "--step-summary",
            str(summary_file),
        ]
    )

    assert out_file.exists()
    assert "sankey-beta" in out_file.read_text(encoding="utf-8")

    assert summary_file.exists()
    summary_text = summary_file.read_text(encoding="utf-8")
    assert "Job Search Sankey Diagram" in summary_text
    assert "sankey-beta" in summary_text
    assert "SankeyMATIC Code" in summary_text


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_step_summary_from_env_var(
    mock_client: MagicMock,
    mock_generate: MagicMock,
    tmp_path,
) -> None:
    mock_generate.return_value = [SankeyFlow("Applied", "In Loop", 5)]
    summary_file = tmp_path / "env_summary.md"
    env = {**DEFAULT_ENV, "GITHUB_STEP_SUMMARY": str(summary_file)}

    run_main(env=env, argv=["sankey.py"])

    assert summary_file.exists()
    summary_text = summary_file.read_text(encoding="utf-8")
    assert "Job Search Sankey Diagram" in summary_text


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_date_range_validation(
    mock_client: MagicMock,
    mock_generate: MagicMock,
) -> None:
    run_main(
        argv=[
            "sankey.py",
            "--start-date",
            "2026-02-01",
            "--end-date",
            "2026-01-01",
        ],
        expected_code=2,
    )


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_client_error_handling(
    mock_client: MagicMock,
    mock_generate: MagicMock,
    caplog,
) -> None:
    mock_generate.side_effect = GitHubClientError("API down")
    run_main(argv=["sankey.py"], expected_code=1)
    assert "Failed to build Sankey flows" in caplog.text


def test_write_step_summary_failure(caplog) -> None:
    # Non-writable path should log a warning instead of crashing
    _write_step_summary("/nonexistent_dir/summary.md", [], "applied")
    assert "Could not write to GitHub step summary" in caplog.text


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
@patch("pathlib.Path.write_text")
def test_output_file_write_error(
    mock_write: MagicMock,
    mock_client: MagicMock,
    mock_generate: MagicMock,
    caplog,
) -> None:
    mock_generate.return_value = []
    mock_write.side_effect = OSError("Disk full")
    run_main(argv=["sankey.py", "--output", "out.txt"], expected_code=1)
    assert "Failed to write output to out.txt" in caplog.text


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_output_dir_generates_all_formats(
    mock_client: MagicMock,
    mock_generate: MagicMock,
    tmp_path,
) -> None:
    mock_generate.return_value = [SankeyFlow("Applied", "In Loop", 5)]
    out_dir = tmp_path / "sankey_out"

    run_main(argv=["sankey.py", "--output-dir", str(out_dir)])

    assert (out_dir / "sankey.txt").exists()
    assert (out_dir / "sankey.mmd").exists()
    assert (out_dir / "sankey.json").exists()
    assert (out_dir / "sankey.csv").exists()

    assert "Applied [5] In Loop" in (out_dir / "sankey.txt").read_text(encoding="utf-8")
    assert "sankey-beta" in (out_dir / "sankey.mmd").read_text(encoding="utf-8")
    assert '"source": "Applied"' in (out_dir / "sankey.json").read_text(
        encoding="utf-8"
    )
    assert "Applied,In Loop,5" in (out_dir / "sankey.csv").read_text(encoding="utf-8")


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_output_and_output_dir_mutually_exclusive(
    mock_client: MagicMock,
    mock_generate: MagicMock,
) -> None:
    run_main(
        argv=["sankey.py", "--output", "out.txt", "--output-dir", "out_dir"],
        expected_code=2,
    )


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
def test_format_and_output_dir_mutually_exclusive(
    mock_client: MagicMock,
    mock_generate: MagicMock,
) -> None:
    run_main(
        argv=["sankey.py", "--format", "csv", "--output-dir", "out_dir"],
        expected_code=2,
    )


@patch("gitemployed.cli.sankey.generate_sankey_flows")
@patch("gitemployed.cli.sankey.GitHubClient")
@patch("pathlib.Path.write_text")
def test_output_dir_write_error(
    mock_write: MagicMock,
    mock_client: MagicMock,
    mock_generate: MagicMock,
    caplog,
) -> None:
    mock_generate.return_value = [SankeyFlow("Applied", "In Loop", 5)]
    mock_write.side_effect = OSError("Disk full")
    run_main(argv=["sankey.py", "--output-dir", "out_dir"], expected_code=1)
    assert "Failed to write output to out_dir" in caplog.text
