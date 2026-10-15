"""Guardrails for which runner image each template workflow job uses.

The :slim image has no Chromium/Bun/resumed/pdf-vdiff, so workflows that only
call the GitHub API or parse data should use it (the full image costs ~40s of
container init per run), while anything that renders or compiles resumes must
stay on :latest.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).parent.parent / "template/.github/workflows"
SLIM_IMAGE = "ghcr.io/menil/gitemployed:slim"
FULL_IMAGE = "ghcr.io/menil/gitemployed:latest"

# Every container job in these workflows must use the slim image. Add a
# workflow here only after auditing its CLI import chain and steps for
# Chromium/Bun/resumed/pdf-vdiff use.
SLIM_WORKFLOWS = [
    "status-transition.yml",
    "project-status-sync.yml",
    "gmail-sync.yml",
    "esd-export.yml",
    "check-setup.yml",
    "scrape-jobs.yml",
]
# These render or compile resumes (Chromium/resumed/pdf-vdiff) and need :latest.
FULL_WORKFLOWS = [
    "triage-issue.yml",
    "recompile-tailored-resume.yml",
    "format-resume.yml",
]
# Not a single-image workflow: respond-issue runs one job on each image (its
# split is asserted in test_respond.py) and sync-labels has no container job.
MIXED_OR_HOST_WORKFLOWS = [
    "respond-issue.yml",
    "sync-labels.yml",
]


def _job_images(workflow_name: str) -> dict[str, str]:
    data = yaml.safe_load((WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8"))
    return {
        name: job["container"]["image"]
        for name, job in data["jobs"].items()
        if "container" in job
    }


@pytest.mark.parametrize("workflow_name", SLIM_WORKFLOWS)
def test_light_workflows_use_slim_image(workflow_name: str) -> None:
    """Light workflows avoid the heavy PDF-tooling image."""
    images = _job_images(workflow_name)
    assert images, f"{workflow_name} has no container jobs"
    assert set(images.values()) == {SLIM_IMAGE}


@pytest.mark.parametrize("workflow_name", FULL_WORKFLOWS)
def test_rendering_workflows_keep_full_image(workflow_name: str) -> None:
    """Resume rendering/compiling workflows must keep the full image."""
    images = _job_images(workflow_name)
    assert images, f"{workflow_name} has no container jobs"
    assert set(images.values()) == {FULL_IMAGE}


def test_every_workflow_is_classified() -> None:
    """A new workflow must be deliberately classified before it can merge.

    Without this, a workflow added on :latest silently pays the ~40s image
    pull even if it only calls the GitHub API.
    """
    classified = [*SLIM_WORKFLOWS, *FULL_WORKFLOWS, *MIXED_OR_HOST_WORKFLOWS]
    assert len(classified) == len(set(classified)), "workflow listed twice"
    on_disk = {path.name for path in WORKFLOWS_DIR.glob("*.yml")}
    assert on_disk == set(classified)
