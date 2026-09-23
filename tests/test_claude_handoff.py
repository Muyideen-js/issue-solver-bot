import sys
from pathlib import Path

import pytest

from app.services import claude_handoff
from app.services.claude_handoff import HandoffError
from app.services.workspace import WorkspaceError


ISSUE = {
    "number": 42,
    "title": "Crash on empty input",
    "html_url": "https://github.com/owner/repo/issues/42",
    "body": "Passing an empty string raises IndexError.",
}


def test_workspace_path_is_filesystem_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    path = claude_handoff.workspace_path("Owner/My.Repo", 7)

    assert path.parent == tmp_path.resolve()
    assert path.name == "Owner__My.Repo-issue-7"
    assert "/" not in path.name


def test_workspace_path_strips_characters_that_could_escape_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    path = claude_handoff.workspace_path("../../etc/pas swd", 1)

    assert path.parent == tmp_path.resolve()
    assert ".." not in path.name.replace("-", "")
    assert " " not in path.name


def test_branch_name_matches_the_solver_convention():
    assert claude_handoff.branch_name(42) == "solver/issue-42"


def test_prompt_states_the_issue_branch_and_the_finishing_line():
    prompt = claude_handoff.build_prompt(ISSUE, "owner/repo", "solver/issue-42", "main")

    assert "#42" in prompt
    assert "Crash on empty input" in prompt
    assert "https://github.com/owner/repo/issues/42" in prompt
    assert "solver/issue-42" in prompt
    assert "main" in prompt
    assert "Passing an empty string raises IndexError." in prompt
    # The dashboard opens the PR so it carries the Closes link and CI tracking.
    assert "Do not open the pull request" in prompt


def test_prompt_truncates_a_huge_issue_body():
    issue = dict(ISSUE, body="x" * (claude_handoff.MAX_ISSUE_BODY_CHARS + 500))

    prompt = claude_handoff.build_prompt(issue, "owner/repo", "solver/issue-42", "main")

    assert "[issue body truncated]" in prompt
    assert len(prompt) < claude_handoff.MAX_ISSUE_BODY_CHARS + 2_000


def test_prompt_handles_an_issue_with_no_description():
    prompt = claude_handoff.build_prompt(
        dict(ISSUE, body=None), "owner/repo", "solver/issue-42", "main"
    )

    assert "(no description provided)" in prompt


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch shapes")
def test_launch_command_opens_the_checkout_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handoff.shutil, "which", lambda name: None)

    command = claude_handoff.build_launch_command(tmp_path, "fix it")

    # Without Windows Terminal the classic console is used, and `start` needs
    # its empty title argument or it treats the next quoted token as the title.
    assert command[:5] == ["cmd", "/c", "start", "", "cmd"]
    assert "claude" in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch shapes")
def test_launch_command_prefers_windows_terminal_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_handoff.shutil, "which", lambda name: "C:/wt.exe" if name == "wt" else None
    )

    command = claude_handoff.build_launch_command(tmp_path, "fix it")

    assert command[0] == "wt"
    assert str(tmp_path) in command


def test_quoting_escapes_embedded_quotes():
    assert claude_handoff._quote_for_shell('say "hi"') == '"say ""hi"""'


def test_launch_refuses_when_the_cli_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handoff, "claude_cli_path", lambda: None)

    with pytest.raises(HandoffError, match="not installed"):
        claude_handoff.launch_terminal(tmp_path, "fix it")


@pytest.mark.asyncio
async def test_push_refuses_without_a_checkout(tmp_path):
    with pytest.raises(HandoffError, match="No checkout"):
        await claude_handoff.push_branch("token", tmp_path / "missing", "solver/issue-1", "main")


@pytest.mark.asyncio
async def test_push_refuses_when_nothing_was_committed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    async def no_commits(token, checkout, base_branch):
        return []

    monkeypatch.setattr(claude_handoff, "commits_ahead", no_commits)

    with pytest.raises(HandoffError, match="Nothing has been committed"):
        await claude_handoff.push_branch("token", tmp_path, "solver/issue-1", "main")


@pytest.mark.asyncio
async def test_prepare_rejects_a_branch_name_that_could_inject_git_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    with pytest.raises(WorkspaceError):
        await claude_handoff.prepare_checkout(
            token="t",
            upstream_repo="owner/repo",
            upstream_clone_url="https://github.com/owner/repo.git",
            fork_clone_url="https://github.com/me/repo.git",
            base_branch="main",
            branch="--upload-pack=evil",
            issue_number=1,
        )


@pytest.mark.asyncio
async def test_prepare_reuses_an_existing_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))
    target = claude_handoff.workspace_path("owner/repo", 42)
    (target / ".git").mkdir(parents=True)

    async def fail_git(*args, **kwargs):
        raise AssertionError("existing checkout must not be re-cloned")

    monkeypatch.setattr(claude_handoff, "_git", fail_git)

    result = await claude_handoff.prepare_checkout(
        token="t",
        upstream_repo="owner/repo",
        upstream_clone_url="https://github.com/owner/repo.git",
        fork_clone_url="https://github.com/me/repo.git",
        base_branch="main",
        branch="solver/issue-42",
        issue_number=42,
    )

    assert result == target


def test_discard_checkout_reports_when_there_was_nothing_to_remove(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    assert claude_handoff.discard_checkout("owner/repo", 999) is False
