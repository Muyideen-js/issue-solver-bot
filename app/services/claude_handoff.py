"""Hand an issue to an interactive Claude Code session on this machine.

The solver's own agent loop runs headlessly against an API key. This is the
opposite, and deliberately so: it prepares a real checkout and opens a terminal
window the person drives themselves. The bot never holds Claude credentials and
never runs Claude unattended -- it does the tedious setup (fork, clone, branch,
prompt) and hands over.

The terminal only appears on the machine running this process, so a dashboard
reached through a tunnel would pop the window on the host, not the viewer's
screen.
"""
import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.services.workspace import _git_auth_env, _validate_branch

logger = logging.getLogger(__name__)

MAX_ISSUE_BODY_CHARS = 4_000
GIT_TIMEOUT_SECONDS = 300


class HandoffError(Exception):
    pass


def workspace_root() -> Path:
    """Where checkouts live. Under .solver-work, which the repo already ignores."""
    configured = os.getenv("CLAUDE_HANDOFF_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".solver-work" / "claude").resolve()


def _slug(value: str, fallback: str) -> str:
    """Filesystem-safe single path component.

    Inputs are validated before they reach here, so this is belt and braces:
    run-together dots are collapsed and leading dots stripped so a component
    can never be a relative-path element or a hidden folder.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", value.replace("/", "__"))
    return re.sub(r"\.{2,}", "-", slug).lstrip(".-") or fallback


def workspace_path(repo: str, issue_number: int, account: str) -> Path:
    """A stable checkout directory for one issue, per account.

    The account belongs in the key: two dashboard accounts can both be assigned
    the same issue in the same repo, and sharing one checkout would put two
    sessions in one working tree, pushing with each other's credentials.
    """
    return workspace_root() / (
        f"{_slug(account, 'account')}__{_slug(repo, 'repo')}-issue-{int(issue_number)}"
    )


def branch_name(issue_number: int) -> str:
    return f"solver/issue-{int(issue_number)}"


def build_prompt(issue: dict, repo: str, branch: str, base_branch: str) -> str:
    """The opening instruction Claude Code receives.

    It states the finishing line explicitly -- commit on this branch, do not
    open the PR -- because the dashboard opens the pull request afterwards with
    the same body and CI tracking the automated path uses.
    """
    body = (issue.get("body") or "").strip()
    if len(body) > MAX_ISSUE_BODY_CHARS:
        body = body[:MAX_ISSUE_BODY_CHARS] + "\n[issue body truncated]"
    number = issue.get("number")
    title = (issue.get("title") or "").strip()
    return "\n".join([
        f"Fix issue #{number} in {repo}: {title}",
        "",
        f"Issue URL: {issue.get('html_url', '')}",
        f"You are on branch {branch}, cut from {base_branch}.",
        "",
        "Issue description:",
        body or "(no description provided)",
        "",
        "Please:",
        "1. Explore the repository and work out the smallest correct fix.",
        "2. Make the change and follow the conventions already in this codebase.",
        "3. Run the project's tests if it has any.",
        f"4. Commit to {branch} with a clear message.",
        "",
        "Do not open the pull request and do not push -- the dashboard does that,"
        " so the PR gets the right 'Closes' link and CI tracking. Just commit.",
        "",
        "The GitHub CLI in this terminal is already signed in as the account this"
        " issue is assigned to, so `gh issue view` and similar work as-is.",
    ])


def _quote_for_shell(value: str) -> str:
    """Quote a prompt for a Windows command line without a shell interpreting it."""
    return '"' + value.replace('"', '""') + '"'


def build_launch_command(cwd: Path, prompt: str) -> list[str]:
    """Command that opens a visible terminal already running Claude Code.

    Windows Terminal is preferred because it takes the working directory
    directly; the classic console is the fallback on machines without it.
    """
    if sys.platform != "win32":
        # Best effort elsewhere; the dashboard reports what it ran either way.
        return ["x-terminal-emulator", "-e", f"claude {prompt}"]
    if shutil.which("wt"):
        return ["wt", "-d", str(cwd), "cmd", "/k", "claude", prompt]
    return ["cmd", "/c", "start", "", "cmd", "/k", "claude", _quote_for_shell(prompt)]


def claude_cli_path() -> str | None:
    return shutil.which("claude")


def session_env(token: str) -> dict[str, str]:
    """Environment for one session, scoped to the account that owns the issue.

    Each dashboard account has its own GitHub token, and the machine's global
    `gh auth login` is a single unrelated account -- so without this a push or
    `gh` call inside the session would act as the wrong identity, or none.

    GH_TOKEN takes precedence over gh's stored credentials, and the git
    extraheader does the same for git, so this authenticates the session
    without touching the machine's global login and without writing the token
    to disk. It lives only in that terminal's environment and dies with it.

    The full parent environment is inherited deliberately: Claude Code needs
    PATH, APPDATA and its own config to start at all.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    # Some tooling reads the other spelling; set both so neither falls back to
    # the machine's global account.
    env["GITHUB_TOKEN"] = token
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
    return env


async def _git(*args: str, cwd: Path | None = None, token: str | None = None) -> str:
    env = _git_auth_env(token or "")
    process = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=GIT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise HandoffError(f"git {args[0]} timed out") from exc
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise HandoffError(f"git {args[0]} failed: {detail[:400]}")
    return (stdout or b"").decode("utf-8", "replace")


# One lock per checkout path. Two clicks on the same issue would otherwise both
# see no .git, both start cloning into one directory, and the second would fail
# on a non-empty target.
_prepare_locks: dict[str, asyncio.Lock] = {}


def _prepare_lock(target: Path) -> asyncio.Lock:
    return _prepare_locks.setdefault(str(target), asyncio.Lock())


async def prepare_checkout(
    token: str,
    upstream_repo: str,
    upstream_clone_url: str,
    fork_clone_url: str,
    base_branch: str,
    branch: str,
    issue_number: int,
    account: str,
) -> Path:
    """Clone upstream, add the fork as a push remote, and cut the work branch.

    Re-running for the same issue reuses the existing checkout so a session can
    be reopened without losing uncommitted work.
    """
    _validate_branch(branch)
    _validate_branch(base_branch)
    target = workspace_path(upstream_repo, issue_number, account)

    async with _prepare_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        if (target / ".git").exists():
            logger.info("Reusing existing Claude Code checkout at %s", target)
            return target

        await _git(
            "clone", "--no-tags", "--single-branch", "--branch", base_branch,
            upstream_clone_url, str(target), token=token,
        )
        await _git("remote", "add", "fork", fork_clone_url, cwd=target, token=token)
        await _git("checkout", "-b", branch, cwd=target, token=token)
    return target


def launch_terminal(cwd: Path, prompt: str, env: dict[str, str] | None = None) -> list[str]:
    """Open the terminal and return the command used, for the dashboard to show.

    Spawned detached so the HTTP request does not wait on a session the person
    may keep open for an hour. Verified that both Windows Terminal and the
    classic console inherit the environment passed here, which is what carries
    the account's credentials into the session.
    """
    if not claude_cli_path():
        raise HandoffError(
            "The Claude Code CLI is not installed or not on PATH. "
            "Install it with: npm install -g @anthropic-ai/claude-code"
        )
    command = build_launch_command(cwd, prompt)
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise HandoffError(f"Could not open a terminal: {exc}") from exc
    return command


async def commits_ahead(token: str, checkout: Path, base_branch: str) -> list[str]:
    """Subjects of the commits this session added on top of the base branch."""
    output = await _git(
        "log", "--oneline", f"origin/{base_branch}..HEAD", cwd=checkout, token=token
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


async def push_branch(token: str, checkout: Path, branch: str, base_branch: str) -> str:
    """Push the person's commits to their fork and return the head SHA.

    Refuses an empty branch so the dashboard cannot open a pull request with no
    changes in it.
    """
    _validate_branch(branch)
    _validate_branch(base_branch)
    if not (checkout / ".git").exists():
        raise HandoffError(f"No checkout at {checkout}; start the session again")
    if not await commits_ahead(token, checkout, base_branch):
        raise HandoffError(
            "Nothing has been committed on this branch yet -- commit in the "
            "Claude Code window first, then open the PR."
        )
    await _git("push", "--force-with-lease", "fork", f"{branch}:{branch}", cwd=checkout, token=token)
    return (await _git("rev-parse", "HEAD", cwd=checkout, token=token)).strip()


def discard_checkout(repo: str, issue_number: int, account: str) -> bool:
    """Remove a finished checkout. Returns whether anything was deleted."""
    target = workspace_path(repo, issue_number, account)
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()
