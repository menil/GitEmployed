"""CLI entry point for the job search Sankey diagram generator.

Supports on-demand invocation via GitHub Actions (workflow_dispatch) or local CLI.
Fetches issue lifecycle events from GitHub and outputs flows in SankeyMATIC,
Mermaid, JSON, or CSV formats, optionally updating $GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from gitemployed.cli import setup_logging
from gitemployed.github_client import GitHubClient, GitHubClientError
from gitemployed.sankey import (
    format_all_flows,
    format_flows,
    format_mermaid,
    format_sankeymatic,
    generate_sankey_flows,
)

logger = logging.getLogger("sankey")


def _write_step_summary(
    summary_path: str,
    flows: list,
    scope: str,
) -> None:
    """Append a Mermaid diagram and SankeyMATIC block to GITHUB_STEP_SUMMARY."""
    mermaid_block = format_mermaid(flows)
    sankeymatic_block = format_sankeymatic(flows)

    content = [
        "## 📊 Job Search Sankey Diagram",
        f"**Scope:** `{scope}` | **Total Transitions:** {len(flows)}",
        "",
        "```mermaid",
        mermaid_block,
        "```",
        "",
        "<details><summary><b>📋 SankeyMATIC Code</b></summary>",
        "",
        "Paste into [SankeyMATIC](https://sankeymatic.com/) to customize:",
        "",
        "```text",
        sankeymatic_block,
        "```",
        "</details>",
        "",
    ]
    try:
        with Path(summary_path).open("a", encoding="utf-8") as f:
            f.write("\n".join(content) + "\n")
        logger.info("Appended diagram to GitHub step summary: %s", summary_path)
    except OSError as e:
        logger.warning(
            "Could not write to GitHub step summary at %s: %s", summary_path, e
        )


def main() -> None:
    """Parse arguments, compute Sankey flows, and write output."""
    setup_logging()
    args = _parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)
    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

    gh_client = GitHubClient(token=token, repo=repo)

    try:
        flows = generate_sankey_flows(
            gh_client,
            scope=args.scope,
            stale_days=args.stale_days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (ValueError, GitHubClientError) as e:
        logger.error("Failed to build Sankey flows: %s", e)
        sys.exit(1)

    if args.output_dir:
        try:
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in format_all_flows(flows).items():
                file_path = output_dir / filename
                file_path.write_text(content, encoding="utf-8")
                logger.info("Wrote %s output to %s", filename, file_path)
        except OSError as e:
            logger.error("Failed to write output to %s: %s", args.output_dir, e)
            sys.exit(1)
    else:
        fmt = args.format or "sankeymatic"
        formatted = format_flows(flows, fmt)
        if args.output:
            try:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(formatted, encoding="utf-8")
                logger.info("Wrote %s output to %s", fmt, args.output)
            except OSError as e:
                logger.error("Failed to write output to %s: %s", args.output, e)
                sys.exit(1)
        else:
            print(formatted)

    # If --step-summary was requested or GITHUB_STEP_SUMMARY is present
    summary_dest = args.step_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_dest:
        _write_step_summary(summary_dest, flows, args.scope)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the Sankey generator."""
    parser = argparse.ArgumentParser(
        description="Generate Sankey diagram flows for job search tracking."
    )
    parser.add_argument(
        "--scope",
        choices=("applied", "full"),
        default="applied",
        help="Funnel scope: 'applied' (traditional) or 'full' (discovery & triage).",
    )
    parser.add_argument(
        "--format",
        choices=("sankeymatic", "mermaid", "json", "csv"),
        default=None,
        help="Output format (default: sankeymatic when writing single file or stdout).",
    )
    parser.add_argument(
        "--stale-days",
        type=_parse_positive_int,
        default=30,
        help="Days of inactivity before an open role is marked ghosted.",
    )
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=None,
        help="Inclusive lower bound, YYYY-MM-DD. Defaults to unbounded.",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        help="Inclusive upper bound, YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: print to stdout).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help=(
            "Directory to write all format outputs (sankey.txt, sankey.mmd, "
            "sankey.json, sankey.csv)."
        ),
    )
    parser.add_argument(
        "--step-summary",
        type=str,
        default=None,
        help="Path to GITHUB_STEP_SUMMARY file to append Markdown diagram.",
    )

    args = parser.parse_args()
    if args.output and args.output_dir:
        parser.error("Cannot specify both --output and --output-dir.")
    if args.format and args.output_dir:
        parser.error("Cannot specify both --format and --output-dir.")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date.")

    return args


def _parse_positive_int(value: str) -> int:
    """Parse positive integer argument."""
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer, got {value!r}"
        ) from e
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return parsed


def _parse_date(value: str) -> dt.date:
    """Parse ISO YYYY-MM-DD date argument."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {value!r}") from e


if __name__ == "__main__":
    main()
