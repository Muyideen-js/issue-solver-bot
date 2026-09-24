import sys
from pathlib import Path

import pytest

from app.services import claude_handoff
from app.services.claude_handoff import HandoffError
from app.services.workspace import WorkspaceError


NEWLINE = chr(10)


ISSUE = {
    "number": 42,
    "title": "Crash on empty input",
    "html_url": "https://github.com/owner/repo/issues/42",
    "body": "Passing an empty string raises IndexError.",
}


def test_workspace_path_is_filesystem_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    path = claude_handoff.workspace_path("Owner/My.Repo", 7, "octocat")

    assert path.parent == tmp_path.resolve()
    assert path.name == "octocat__Owner__My.Repo-issue-7"
    assert "/" not in path.name


def test_workspace_path_strips_characters_that_could_escape_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    path = claude_handoff.workspace_path("../../etc/pas swd", 1, "octocat")

    assert path.parent == tmp_path.resolve()
    assert ".." not in path.name.replace("-", "")
    assert " " not in path.name


def test_branch_name_matches_the_solver_convention():
    assert claude_handoff.branch_name(42) == "solver/issue-42"


def test_prompt_states_the_issue_branch_and_the_finishing_line():
    prompt = claude_handoff.build_prompt(
        ISSUE, "owner/repo", "solver/issue-42", "main", "forkowner"
    )

    assert "#42" in prompt
    assert "Crash on empty input" in prompt
    assert "https://github.com/owner/repo/issues/42" in prompt
    assert "solver/issue-42" in prompt
    assert "main" in prompt
    assert "Passing an empty string raises IndexError." in prompt
    # The session opens its own PR; the one thing the bot must guarantee is
    # the Closes line, since that is what links the PR to the issue.
    assert "Closes #42" in prompt
    assert "gh pr create" in prompt
    assert "--head forkowner:solver/issue-42" in prompt
    assert "git push fork solver/issue-42" in prompt
    # This path lands PRs ready for review, so the session must not draft one.
    assert "--draft" not in prompt
    assert "ready for review, not as a draft" in prompt


def test_prompt_truncates_a_huge_issue_body():
    issue = dict(ISSUE, body="x" * (claude_handoff.MAX_ISSUE_BODY_CHARS + 500))

    prompt = claude_handoff.build_prompt(
        issue, "owner/repo", "solver/issue-42", "main", "forkowner"
    )

    assert "[issue body truncated]" in prompt
    assert len(prompt) < claude_handoff.MAX_ISSUE_BODY_CHARS + 2_000


def test_prompt_handles_an_issue_with_no_description():
    prompt = claude_handoff.build_prompt(
        dict(ISSUE, body=None), "owner/repo", "solver/issue-42", "main", "forkowner"
    )

    assert "(no description provided)" in prompt


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch shapes")
def test_launch_command_opens_the_checkout_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handoff.shutil, "which", lambda name: None)

    command = claude_handoff.build_launch_command(tmp_path, "read the task file")

    # Without Windows Terminal the classic console is used, and `start` needs
    # its empty title argument or it treats the next quoted token as the title.
    assert command[:5] == ["cmd", "/c", "start", "", "cmd"]
    assert "claude" in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch shapes")
def test_launch_command_prefers_windows_terminal_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_handoff.shutil, "which", lambda name: "C:/wt.exe" if name == "wt" else None
    )

    command = claude_handoff.build_launch_command(tmp_path, "read the task file")

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
            account="octocat",
        )


@pytest.mark.asyncio
async def test_prepare_reuses_an_existing_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))
    target = claude_handoff.workspace_path("owner/repo", 42, "octocat")
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
        account="octocat",
    )

    assert result == target


def test_discard_checkout_reports_when_there_was_nothing_to_remove(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    assert claude_handoff.discard_checkout("owner/repo", 999, "octocat") is False


def test_session_env_authenticates_gh_as_the_issue_account(monkeypatch):
    """Each dashboard account has its own token; the machine's gh login is one
    unrelated account, so the session must override it."""
    monkeypatch.setenv("GH_TOKEN", "the-machines-global-account")

    env = claude_handoff.session_env("account-specific-token")

    assert env["GH_TOKEN"] == "account-specific-token"
    assert env["GITHUB_TOKEN"] == "account-specific-token"


def test_session_env_authenticates_git_pushes_from_inside_the_session():
    env = claude_handoff.session_env("account-specific-token")

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    # Basic auth is base64, so the raw token must not appear verbatim.
    assert "account-specific-token" not in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")


def test_session_env_keeps_the_parent_environment_claude_needs(monkeypatch):
    monkeypatch.setenv("APPDATA", "C:/Users/someone/AppData/Roaming")

    env = claude_handoff.session_env("token")

    assert env.get("PATH")
    assert env["APPDATA"] == "C:/Users/someone/AppData/Roaming"


def test_launch_passes_the_session_environment_to_the_terminal(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env")

    monkeypatch.setattr(claude_handoff, "claude_cli_path", lambda: "C:/claude.cmd")
    monkeypatch.setattr(claude_handoff.subprocess, "Popen", FakePopen)

    claude_handoff.launch_terminal(
        tmp_path, "fix it", env=claude_handoff.session_env("scoped-token")
    )

    assert captured["env"]["GH_TOKEN"] == "scoped-token"
    # The token must travel in the environment, never on the command line,
    # where it would show up in process listings.
    assert "scoped-token" not in " ".join(captured["command"])


def test_two_accounts_on_the_same_issue_get_separate_checkouts(monkeypatch, tmp_path):
    """Both accounts can be assigned one issue; a shared working tree would let
    two sessions collide and push with each other's credentials."""
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    alice = claude_handoff.workspace_path("owner/repo", 42, "alice")
    bob = claude_handoff.workspace_path("owner/repo", 42, "bob")

    assert alice != bob
    assert alice.parent == bob.parent == tmp_path.resolve()


def test_one_account_on_different_issues_gets_separate_checkouts(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    first = claude_handoff.workspace_path("owner/repo", 1, "alice")
    second = claude_handoff.workspace_path("owner/repo", 2, "alice")

    assert first != second


def test_account_names_cannot_escape_the_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))

    path = claude_handoff.workspace_path("owner/repo", 1, "../../evil")

    assert path.parent == tmp_path.resolve()
    assert ".." not in path.name.replace("-", "")


@pytest.mark.asyncio
async def test_simultaneous_prepares_clone_once(monkeypatch, tmp_path):
    """Four clicks at once must not all start cloning into one directory."""
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))
    claude_handoff._prepare_locks.clear()
    clones = []

    async def fake_git(*args, **kwargs):
        if args[0] == "clone":
            clones.append(args)
            # Mark the checkout as present, as a real clone would.
            Path(args[-1], ".git").mkdir(parents=True, exist_ok=True)
        return ""

    monkeypatch.setattr(claude_handoff, "_git", fake_git)

    async def prepare():
        return await claude_handoff.prepare_checkout(
            token="t",
            upstream_repo="owner/repo",
            upstream_clone_url="https://github.com/owner/repo.git",
            fork_clone_url="https://github.com/me/repo.git",
            base_branch="main",
            branch="solver/issue-9",
            issue_number=9,
            account="alice",
        )

    import asyncio as _asyncio
    results = await _asyncio.gather(*(prepare() for _ in range(4)))

    assert len(clones) == 1
    assert len(set(results)) == 1


def test_task_file_sits_beside_the_checkout_not_inside_it(monkeypatch, tmp_path):
    """A file in the working tree would show in git status and could be
    committed into the pull request."""
    monkeypatch.setenv("CLAUDE_HANDOFF_ROOT", str(tmp_path))
    checkout = claude_handoff.workspace_path("owner/repo", 319, "alice")

    task = claude_handoff.task_file_path(checkout)

    assert task.parent == checkout.parent
    assert checkout not in task.parents


def test_opening_line_is_a_single_line_pointing_at_the_task_file(tmp_path):
    task = tmp_path / "repo-issue-1.task.md"

    line = claude_handoff.build_opening_line(task)

    assert NEWLINE not in line
    assert str(task) in line


def test_launch_command_refuses_a_multi_line_opener(tmp_path):
    """Windows Terminal drops everything after the first newline and the rest
    spills into the shell, which is how a multi-line prompt broke a session."""
    with pytest.raises(HandoffError, match="single line"):
        claude_handoff.build_launch_command(tmp_path, "first" + NEWLINE + "second")


def test_launch_writes_the_full_prompt_to_the_task_file(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command

    monkeypatch.setattr(claude_handoff, "claude_cli_path", lambda: "C:/claude.cmd")
    monkeypatch.setattr(claude_handoff.subprocess, "Popen", FakePopen)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    prompt = "Fix issue #1" + NEWLINE + NEWLINE + "Please:" + NEWLINE + "1. Explore"

    claude_handoff.launch_terminal(checkout, prompt)

    task = claude_handoff.task_file_path(checkout)
    assert task.read_text(encoding="utf-8") == prompt
    # The multi-line prompt must never reach the command line.
    assert NEWLINE not in " ".join(captured["command"])
    assert "1. Explore" not in " ".join(captured["command"])


def test_prompt_tells_the_session_to_target_the_upstream_repository():
    """A fork clone can easily open the PR against the fork by mistake."""
    prompt = claude_handoff.build_prompt(
        ISSUE, "upstream/repo", "solver/issue-42", "main", "forkowner"
    )

    assert "--repo upstream/repo" in prompt
    assert "--base main" in prompt
