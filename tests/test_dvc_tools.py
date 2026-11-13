"""DVC tools: provenance lookups and the command allowlist."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.schemas import NoArgs
from agent.tools import dvc_tools
from agent.tools.base import PermissionDeniedError, ToolContext, ToolError
from tests.conftest import COMMIT_V1, COMMIT_V2, MD5_V1, MD5_V2

CTX = ToolContext(request_id="test")


@pytest.mark.parametrize(
    "command",
    [
        ["git", "push"],
        ["git", "checkout", "main"],
        ["dvc", "checkout"],
        ["dvc", "push"],
        ["dvc", "gc", "-f"],
        ["rm", "-rf", "/"],
        ["git"],
    ],
)
def test_only_allowlisted_commands_can_run(command):
    with pytest.raises(PermissionDeniedError):
        dvc_tools._run(command)


@pytest.mark.parametrize("rev", ["--force", "-x", "HEAD; rm -rf .", "$(id)", "a b", ""])
def test_revision_arguments_cannot_inject_options(rev):
    with pytest.raises(ValidationError):
        dvc_tools.CompareArgs(rev_a=rev, rev_b="HEAD")


def test_list_tracked_datasets(fake_dvc):
    result = dvc_tools.list_tracked_datasets(NoArgs(), CTX)
    assert result["datasets"] == [
        {
            "path": "data/raw/dataset.csv",
            "dvc_file": "data/raw/dataset.csv.dvc",
            "md5": MD5_V2,
            "size_bytes": 2000,
            "hash": "md5",
        }
    ]
    assert result["remotes"] == [{"name": "localremote", "url": "/tmp/remote"}]


def test_current_version_reports_tag_commit_and_missing_workspace_file(fake_dvc):
    result = dvc_tools.current_version(dvc_tools.DatasetArgs(), CTX)
    assert result["md5"] == MD5_V2 and result["version_tags"] == ["data-v2.0"]
    assert result["git_head_commit"] == COMMIT_V2
    assert result["workspace_file_present"] is False
    assert result["workspace_matches_recorded_version"] is False


def test_current_version_detects_modified_data(fake_dvc, settings):
    (settings.repo_root / "data/raw/dataset.csv").write_text("tampered")
    result = dvc_tools.current_version(dvc_tools.DatasetArgs(), CTX)
    assert result["workspace_file_present"] is True
    assert result["workspace_matches_recorded_version"] is False


def test_find_version_resolves_recorded_md5_to_tag_and_commit(fake_dvc):
    result = dvc_tools.find_version(dvc_tools.FindArgs(md5=MD5_V1), CTX)
    assert result["found_in_history"] is True
    assert result["version_tags"] == ["data-v1.0"] and result["pointer_commits"] == [COMMIT_V1]
    assert result["is_current_workspace_version"] is False


def test_find_version_flags_inconsistent_or_unknown_references(fake_dvc):
    mismatch = dvc_tools.find_version(dvc_tools.FindArgs(md5=MD5_V1, rev="data-v2.0"), CTX)
    assert mismatch["rev_and_md5_consistent"] is False
    unknown = dvc_tools.find_version(dvc_tools.FindArgs(md5="f" * 32), CTX)
    assert unknown["found_in_history"] is False and unknown["version_tags"] == []


def test_untracked_paths_are_refused(fake_dvc):
    with pytest.raises(ToolError, match="not a DVC-tracked dataset"):
        dvc_tools.current_version(dvc_tools.DatasetArgs(path="/etc/passwd"), CTX)


def test_reproduce_instructions_reference_the_requested_version(fake_dvc):
    result = dvc_tools.reproduce_instructions(dvc_tools.ReproduceArgs(rev="data-v1.0", verify=False), CTX)
    assert result["md5"] == MD5_V1 and result["git_commit"] == COMMIT_V1
    assert result["commands_restore_in_place"][0] == ("git checkout data-v1.0 -- data/raw/dataset.csv.dvc")
    assert "verification" not in result


def test_status_reports_clean_workspace(fake_dvc):
    result = dvc_tools.status(NoArgs(), CTX)
    assert result["tracked_data_changed"] is False and result["remote_in_sync"] is True
