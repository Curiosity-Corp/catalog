"""Main pipelines run to the end: a later merge never cancels the one still proving the previous merge."""

from __future__ import annotations

from pathlib import Path

CI = (Path(__file__).resolve().parent.parent / ".gitlab-ci.yml").read_text()


def workflow_block() -> str:
    start = CI.index("\nworkflow:\n")
    end = CI.index("\n\n", start + 1)
    return CI[start:end]


def test_main_pipelines_are_never_auto_cancelled() -> None:
    block = workflow_block()
    assert "if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in block
    assert "on_new_commit: none" in block


def test_every_other_pipeline_is_still_created() -> None:
    assert workflow_block().rstrip().endswith("- when: always")
