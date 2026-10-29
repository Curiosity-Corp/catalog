"""release-verify runs `check-tag --require-main-ancestor`, which shells out to git, so its image must have git.

v0.0.17's tag pipeline failed with "No such file or directory: 'git'" because the job ran on the uv slim image. A tag
pipeline only runs after the tag exists, so nothing else would have caught it: this reads the CI file instead.
"""

from __future__ import annotations

import re
from pathlib import Path

CI = (Path(__file__).resolve().parent.parent / ".gitlab-ci.yml").read_text()

# Images known to ship git. The uv "slim" images (${PYTHON_IMAGE}) and python:*-slim do not.
GIT_IMAGES = re.compile(r"^python:\d+(\.\d+)*(-bookworm|-trixie)?$")


def job_block(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\n((?:  .*\n|\n)+)", CI, re.MULTILINE)
    assert match, f"job {name} not found in .gitlab-ci.yml"
    return match[1]


def job_image(name: str) -> str:
    match = re.search(r"^  image: (\S+)$", job_block(name), re.MULTILINE)
    assert match, f"job {name} has no image"
    return match[1]


def test_release_verify_runs_on_an_image_with_git() -> None:
    assert "--require-main-ancestor" in job_block("release-verify")
    image = job_image("release-verify")
    assert GIT_IMAGES.match(image), f"release-verify runs on {image}, which may have no git"


def test_every_job_that_shells_out_to_git_has_a_git_image() -> None:
    for name in ("changelog-check", "release-verify"):
        assert GIT_IMAGES.match(job_image(name)), name


def test_the_release_mr_runs_the_tag_gate_before_the_tag_exists() -> None:
    """v0.0.14 and v0.0.17 were burned by tag-only failures; the release MR must run the same job's checks."""
    block = job_block("release-verify-mr")
    assert "extends: release-verify" in block, "it must use the tag job's image"
    assert "merge_request_event" in block and r"chore\(release\)" in block
    assert "check-tag" in block
    assert "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA" in block, "check the commit that will be tagged, not the merged result"
