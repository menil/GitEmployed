"""Coordinator for recompiling tailored resumes on manual branch commits."""

import argparse
import hashlib
import logging
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass

from gitemployed.assistant import STATUS_CONFIRMATION_MARKER
from gitemployed.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from gitemployed.cli.triage import (
    _DEFAULT_RESUME_THEME,
    _build_inline_diff_section,
    _sanitize_apply_url,
    get_resume_filenames,
    get_resume_prefix,
    parse_job_details,
)
from gitemployed.git_ops import (
    GitOpsError,
    mask_value,
    push_branch,
    run_git,
)
from gitemployed.github_client import GitHubClient
from gitemployed.loader import (
    load_resume,
    load_settings,
    parse_resume_yaml,
    render_resume_yaml,
)
from gitemployed.renderer import (
    compile_resume,
    ensure_theme_installed,
    generate_pdf_diff,
)
from gitemployed.schema import Resume

logger = logging.getLogger("gitemployed.recompile_tailored")

EXIT_SUCCESS = 0
EXIT_ERROR = 1

COMMIT_MSG = "chore(resume): update compiled artifacts [skip ci]"


@dataclass
class RecompileResult:
    """Result of recompiling tailored resume artifacts."""

    diff_generated: bool
    candidate_name: str | None
    yaml_filename: str
    json_filename: str
    pdf_filename: str
    diff_filename: str
    committed: bool


def _load_base_resume(
    repo_path: pathlib.Path, base_branch: str = "main"
) -> Resume | None:
    """Load the base resume from the base branch via git show.

    Args:
        repo_path: Path to the local git repository.
        base_branch: Base branch name (default 'main').

    Returns:
        Parsed base Resume object, or None if unavailable.
    """
    candidates = [base_branch]
    if not base_branch.startswith("origin/"):
        candidates.append(f"origin/{base_branch}")

    for ref in candidates:
        try:
            base_yaml_str = run_git(
                ["show", f"{ref}:resumes/resume.yaml"],
                cwd=repo_path,
            )
            return parse_resume_yaml(base_yaml_str)
        except Exception as e:
            logger.debug(
                "Could not load base resume from '%s': %s",
                ref,
                e,
            )
    logger.warning("Could not load base resume from '%s'", base_branch)
    return None


def recompile_and_commit(
    repo_path: pathlib.Path,
    branch_name: str,
    base_branch: str = "main",
    dry_run: bool = False,
) -> RecompileResult:
    """Recompile resume artifacts and commit changes to the branch.

    Args:
        repo_path: Path to the git repository.
        branch_name: Target branch name being recompiled.
        base_branch: Base branch containing the master resume.
        dry_run: If True, skips git commit and push operations.

    Returns:
        RecompileResult containing compilation metadata and status.
    """
    resumes_dir = repo_path / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)

    tailored_yaml_path = resumes_dir / "resume.yaml"
    tailored_resume = load_resume(tailored_yaml_path)

    settings = load_settings(repo_path / "config" / "settings.yaml")
    theme_name = ensure_theme_installed(settings.theme or _DEFAULT_RESUME_THEME)

    candidate_name = tailored_resume.basics.name if tailored_resume.basics else None
    yaml_filename, json_filename, pdf_filename = get_resume_filenames(candidate_name)
    prefix = get_resume_prefix(candidate_name)
    diff_filename = f"{prefix}_diff.pdf"

    # 1. Overwrite with canonical YAML formatting
    canonical_yaml = render_resume_yaml(tailored_resume)
    with (resumes_dir / yaml_filename).open("w", encoding="utf-8") as f:
        f.write(canonical_yaml)

    # 2. Compile JSON and PDF
    compile_resume(
        tailored_resume,
        theme_name,
        resumes_dir / pdf_filename,
        resumes_dir / json_filename,
    )

    # 3. Generate Visual Diff PDF against base resume
    diff_generated = False
    base_resume = _load_base_resume(repo_path, base_branch=base_branch)
    if base_resume:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = pathlib.Path(tmpdir)
                base_pdf_path = tmp_path / "base.pdf"
                base_json_path = tmp_path / "base.json"
                compile_resume(
                    base_resume,
                    theme_name,
                    base_pdf_path,
                    base_json_path,
                )
                generate_pdf_diff(
                    base_pdf_path=base_pdf_path,
                    tailored_pdf_path=resumes_dir / pdf_filename,
                    output_diff_pdf_path=resumes_dir / diff_filename,
                )
                diff_generated = True
        except Exception as e:
            logger.warning("Could not generate visual PDF diff: %s", e)

    # 4. Stage and commit changes
    files_to_commit = [
        f"resumes/{yaml_filename}",
        f"resumes/{json_filename}",
        f"resumes/{pdf_filename}",
    ]
    if diff_generated:
        files_to_commit.append(f"resumes/{diff_filename}")

    if prefix != "resume":
        legacy_files = ["resume.json", "resume.pdf", f"{prefix}.yaml"]
        for legacy in legacy_files:
            legacy_path = resumes_dir / legacy
            if legacy_path.exists():
                legacy_path.unlink()
                files_to_commit.append(f"resumes/{legacy}")

    committed = False
    if not dry_run:
        try:
            name = run_git(["config", "user.name"], cwd=repo_path).strip()
            if not name:
                run_git(["config", "user.name", "github-actions[bot]"], cwd=repo_path)
        except GitOpsError:
            run_git(["config", "user.name", "github-actions[bot]"], cwd=repo_path)

        try:
            email = run_git(["config", "user.email"], cwd=repo_path).strip()
            if not email:
                run_git(
                    [
                        "config",
                        "user.email",
                        "github-actions[bot]@users.noreply.github.com",
                    ],
                    cwd=repo_path,
                )
        except GitOpsError:
            run_git(
                [
                    "config",
                    "user.email",
                    "github-actions[bot]@users.noreply.github.com",
                ],
                cwd=repo_path,
            )

        run_git(["add", "--force", "--"] + files_to_commit, cwd=repo_path)

        # Check for staged changes
        has_staged = False
        try:
            diff_out = run_git(["diff", "--cached", "--name-only"], cwd=repo_path)
            has_staged = bool(diff_out.strip())
        except GitOpsError:
            has_staged = True

        if has_staged:
            run_git(["commit", "--no-verify", "-m", COMMIT_MSG], cwd=repo_path)
            push_branch(repo_path, branch_name)
            committed = True
        else:
            logger.info("No modified compiled artifacts to commit.")

    return RecompileResult(
        diff_generated=diff_generated,
        candidate_name=candidate_name,
        yaml_filename=yaml_filename,
        json_filename=json_filename,
        pdf_filename=pdf_filename,
        diff_filename=diff_filename,
        committed=committed,
    )


def build_update_comment(
    repo: str,
    branch_name: str,
    yaml_rel_path: str,
    pdf_filename: str,
    diff_filename: str,
    diff_generated: bool,
    apply_url: str = "",
    inline_diff: str = "",
) -> str:
    """Build the markdown body for the update comment posted to the issue.

    Args:
        repo: Repository name (e.g. 'owner/repo').
        branch_name: Target application branch name.
        yaml_rel_path: Relative path of the resume YAML file.
        pdf_filename: Filename of the compiled PDF.
        diff_filename: Filename of the visual diff PDF.
        diff_generated: Whether the visual diff PDF was generated.
        apply_url: Optional apply URL for the job posting.
        inline_diff: Optional collapsible markdown diff section.

    Returns:
        Formatted markdown comment string.
    """
    yaml_hash = hashlib.sha256(yaml_rel_path.encode("utf-8")).hexdigest()
    pdf_blob_url = (
        f"https://github.com/{repo}/blob/{branch_name}/resumes/{pdf_filename}"
    )
    diff_blob_url = (
        f"https://github.com/{repo}/blob/{branch_name}/resumes/{diff_filename}"
    )

    lines = [
        STATUS_CONFIRMATION_MARKER,
        "",
        "### Tailored Resume Updated",
        f"- **Application Branch:** [{branch_name}](https://github.com/{repo}/tree/{branch_name})",
        (
            f"- **Resume YAML Diff:** [Compare Changes]"
            f"(https://github.com/{repo}/compare/main...{branch_name}#diff-{yaml_hash})"
        ),
        f"- **Tailored Resume PDF:** [View/Download PDF]({pdf_blob_url})",
    ]
    if diff_generated:
        lines.append(
            f"- **Visual Resume Diff:** [View Visual Diff PDF]({diff_blob_url})"
        )

    sanitized_url = _sanitize_apply_url(apply_url)
    if sanitized_url:
        lines.append(
            f'- **Apply URL:** <a href="{sanitized_url}" '
            f'target="_blank">Link to Posting</a>'
        )

    body = "\n".join(lines)
    if inline_diff:
        body += f"\n\n{inline_diff}"
    return body


def post_recompile_comment(
    repo_path: pathlib.Path,
    branch_name: str,
    recompile_result: RecompileResult,
    gh_client: GitHubClient,
) -> bool:
    """Find the associated issue and post a comment with updated artifact links.

    Args:
        repo_path: Path to the git repository.
        branch_name: The branch name being recompiled.
        recompile_result: Metadata result from recompile_and_commit.
        gh_client: GitHub client instance.

    Returns:
        True if comment was posted to a matching issue, False otherwise.
    """
    logger.info(
        "Searching for issue associated with branch: %s", mask_value(branch_name)
    )
    issue = gh_client.find_issue_by_branch(branch_name)
    if not issue:
        logger.warning(
            "No issue found matching branch '%s'; skipping comment posting.",
            mask_value(branch_name),
        )
        return False

    issue_number = issue.get("number")
    if not issue_number:
        logger.warning("Matched issue payload missing 'number'; skipping comment.")
        return False

    details = parse_job_details(issue.get("body", ""), issue.get("title", ""))
    apply_url = details.get("apply_url", "")

    yaml_rel_path = f"resumes/{recompile_result.yaml_filename}"
    inline_diff = _build_inline_diff_section(repo_path, branch_name, yaml_rel_path)

    comment_body = build_update_comment(
        repo=gh_client.repo,
        branch_name=branch_name,
        yaml_rel_path=yaml_rel_path,
        pdf_filename=recompile_result.pdf_filename,
        diff_filename=recompile_result.diff_filename,
        diff_generated=recompile_result.diff_generated,
        apply_url=apply_url,
        inline_diff=inline_diff,
    )

    try:
        gh_client.post_comment(issue_number, comment_body)
        logger.info("Posted update comment to issue #%d", issue_number)
        return True
    except Exception as e:
        logger.warning(
            "Failed to post update comment to issue #%d: %s",
            issue_number,
            e,
        )
        return False


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for recompiling tailored resume on commit."""
    parser = argparse.ArgumentParser(
        description="Recompile tailored resume PDF and diffs on branch commit."
    )
    add_repo_path_argument(parser)
    parser.add_argument(
        "--branch",
        type=str,
        default=None,
        help="Target branch name. Inferred from HEAD if omitted.",
    )
    parser.add_argument(
        "--base-branch",
        type=str,
        default="main",
        help="Base branch to compare against (default: main).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run compilation without committing or pushing changes.",
    )

    args = parser.parse_args(argv)
    setup_logging()

    repo_path = resolve_repo_path(args.repo_path)

    branch_name = args.branch
    if not branch_name:
        try:
            branch_name = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        except GitOpsError as e:
            logger.error("Could not determine current git branch: %s", e)
            return EXIT_ERROR

    if not branch_name or branch_name == "HEAD":
        logger.error(
            "Could not determine current branch name (HEAD is detached). "
            "Please specify --branch explicitly."
        )
        return EXIT_ERROR

    logger.info("Recompiling tailored resume on branch: %s", mask_value(branch_name))

    try:
        res = recompile_and_commit(
            repo_path=repo_path,
            branch_name=branch_name,
            base_branch=args.base_branch,
            dry_run=args.dry_run,
        )
        logger.info(
            "Recompile finished. diff_generated=%s, committed=%s",
            res.diff_generated,
            res.committed,
        )

        if not args.dry_run and res.committed:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")
            repo = os.environ.get("GITHUB_REPOSITORY")
            if token and repo:
                try:
                    gh_client = GitHubClient(token=token, repo=repo)
                    post_recompile_comment(
                        repo_path=repo_path,
                        branch_name=branch_name,
                        recompile_result=res,
                        gh_client=gh_client,
                    )
                except Exception as comment_err:
                    logger.warning("Failed to post comment to issue: %s", comment_err)
            else:
                logger.info(
                    "GITHUB_TOKEN or GITHUB_REPOSITORY unset; skipping issue comment."
                )

        return EXIT_SUCCESS
    except Exception as e:
        logger.error("Failed to recompile tailored resume: %s", e)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
