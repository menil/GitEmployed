"""Unit tests for the clean-stale CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gitemployed.cli.clean_stale import main
from gitemployed.stale_cleaner import StaleCleanupSummary


def test_cli_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["gitemployed-clean-stale", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_cli_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["gitemployed-clean-stale"])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_PAT", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


@patch("gitemployed.cli.clean_stale.clean_stale_issues")
@patch("gitemployed.cli.clean_stale.load_settings")
def test_cli_disabled_in_settings(
    mock_load_settings: MagicMock,
    mock_clean_stale: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("dummy")

    mock_settings = MagicMock()
    mock_settings.stale_cleanup.enabled = False
    mock_load_settings.return_value = mock_settings

    monkeypatch.setattr(
        "sys.argv", ["gitemployed-clean-stale", "--settings", str(settings_file)]
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    main()
    mock_clean_stale.assert_not_called()


@patch("gitemployed.cli.clean_stale.clean_stale_issues")
@patch("gitemployed.cli.clean_stale.load_settings")
def test_cli_flag_overrides_passed_to_cleaner(
    mock_load_settings: MagicMock,
    mock_clean_stale: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("dummy")

    mock_settings = MagicMock()
    mock_settings.stale_cleanup.enabled = True
    mock_settings.stale_cleanup.days_inactive = 45
    mock_settings.stale_cleanup.limit = 10
    mock_settings.stale_cleanup.include_bookmarked = False
    mock_settings.projects_v2.project_id = "PVT_XYZ"
    mock_settings.projects_v2.status_field_name = "Status"
    mock_load_settings.return_value = mock_settings

    mock_clean_stale.return_value = StaleCleanupSummary(
        inspected_count=5, closed_count=2, skipped_active_count=3
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "gitemployed-clean-stale",
            "--settings",
            str(settings_file),
            "--days-inactive",
            "14",
            "--limit",
            "20",
            "--dry-run",
            "--include-bookmarked",
        ],
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    main()

    mock_clean_stale.assert_called_once()
    _, kwargs = mock_clean_stale.call_args
    assert kwargs["days_inactive"] == 14
    assert kwargs["limit"] == 20
    assert kwargs["dry_run"] is True
    assert kwargs["include_bookmarked"] is True


@patch("gitemployed.cli.clean_stale.clean_stale_issues")
def test_cli_cleaner_exception_exits_nonzero(
    mock_clean_stale: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_clean_stale.side_effect = RuntimeError("Network partition")

    monkeypatch.setattr("sys.argv", ["gitemployed-clean-stale", "--dry-run"])
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


@patch("gitemployed.cli.clean_stale.clean_stale_issues")
def test_cli_nonexistent_settings_file_falls_back_to_defaults(
    mock_clean_stale: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    non_existent = tmp_path / "does_not_exist.yaml"
    mock_clean_stale.return_value = StaleCleanupSummary()

    monkeypatch.setattr(
        "sys.argv",
        ["gitemployed-clean-stale", "--settings", str(non_existent), "--dry-run"],
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    main()

    mock_clean_stale.assert_called_once()
    _, kwargs = mock_clean_stale.call_args
    assert kwargs["days_inactive"] == 30
    assert kwargs["limit"] is None
    assert kwargs["include_bookmarked"] is False


@patch("gitemployed.cli.clean_stale.clean_stale_issues")
@patch("gitemployed.cli.clean_stale.load_settings")
def test_cli_corrupt_settings_file_warns_and_continues(
    mock_load_settings: MagicMock,
    mock_clean_stale: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    corrupt = tmp_path / "corrupt.yaml"
    corrupt.write_text("invalid: yaml: [")
    mock_load_settings.side_effect = ValueError("Invalid YAML")
    mock_clean_stale.return_value = StaleCleanupSummary()

    monkeypatch.setattr(
        "sys.argv",
        ["gitemployed-clean-stale", "--settings", str(corrupt), "--dry-run"],
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    main()

    mock_clean_stale.assert_called_once()
