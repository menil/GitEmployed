"""Coordinator for recompiling tailored resumes on manual branch commits."""

import argparse
import logging
import pathlib
import sys
import tempfile
from dataclasses import dataclass

from gitemployed.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from gitemployed.cli.triage import (
    _DEFAULT_RESUME_THEME,
    get_resume_filenames,
    get_resume_prefix,
)
from gitemployed.git_ops import (
    GitOpsError,
    mask_value,
    push_branch,
    run_git,
)
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
        return EXIT_SUCCESS
    except Exception as e:
        logger.error("Failed to recompile tailored resume: %s", e)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
