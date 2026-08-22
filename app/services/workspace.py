"""Restricted repository workspace: file tools plus Git, never project execution."""
import asyncio
import base64
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath


IGNORED_PARTS = {
    ".git", "node_modules", "target", "dist", "build", ".next", "vendor",
    "coverage", "__pycache__",
}
FORBIDDEN_PREFIXES = ((".github", "workflows"),)
MAX_FILE_CHARS = 200_000


class WorkspaceError(Exception):
    pass


class SolverWorkspace:
    def __init__(self, token: str):
        self._token = token
        self._temp = None
        self.root: Path | None = None

    async def __aenter__(self):
        self._temp = tempfile.TemporaryDirectory(prefix="issue-solver-")
        self.root = Path(self._temp.name).resolve()
        return self

    async def __aexit__(self, *args):
        if self._temp:
            self._temp.cleanup()

    async def clone(self, clone_url: str, base_branch: str, solver_branch: str) -> None:
        _validate_clone_url(clone_url)
        _validate_branch(base_branch)
        _validate_branch(solver_branch)
        await self._git(
            "clone", "--no-tags", "--single-branch", "--branch", base_branch,
            clone_url, str(self._root()),
            cwd=self._root().parent,
        )
        await self._git("checkout", "-b", solver_branch)
        await self._configure_identity()

    async def clone_existing_branch(self, clone_url: str, branch: str) -> None:
        _validate_clone_url(clone_url)
        _validate_branch(branch)
        await self._git(
            "clone", "--no-tags", "--single-branch", "--branch", branch,
            clone_url, str(self._root()),
            cwd=self._root().parent,
        )
        await self._configure_identity()

    async def _configure_identity(self) -> None:
        await self._git("config", "user.name", "grantfox-issue-solver")
        await self._git(
            "config", "user.email", "grantfox-issue-solver@users.noreply.github.com"
        )

    def list_files(self, path: str = ".") -> list[str]:
        start = self._safe_path(path, allow_directory=True)
        if not start.exists() or not start.is_dir():
            raise WorkspaceError(f"Directory does not exist: {path}")
        files = []
        for current, dirs, names in os.walk(start):
            dirs[:] = sorted(directory for directory in dirs if directory not in IGNORED_PARTS)
            for name in sorted(names):
                candidate = Path(current) / name
                relative = candidate.relative_to(self._root()).as_posix()
                if not self._is_forbidden(relative):
                    files.append(relative)
                    if len(files) >= 1000:
                        return files
        return files

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        file_path = self._safe_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"File does not exist: {path}")
        if file_path.stat().st_size > MAX_FILE_CHARS * 4:
            raise WorkspaceError(f"File is too large: {path}")
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"File is not UTF-8 text: {path}") from exc
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)))
        selected = lines[start - 1:end]
        return "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start))

    def search(self, query: str, path: str = ".") -> list[str]:
        if not query or len(query) > 300:
            raise WorkspaceError("Search query must be between 1 and 300 characters")
        start = self._safe_path(path, allow_directory=True)
        results = []
        for relative in self.list_files(start.relative_to(self._root()).as_posix() or "."):
            candidate = self._root() / PurePosixPath(relative)
            try:
                if candidate.stat().st_size > MAX_FILE_CHARS * 4:
                    continue
                for number, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(), 1):
                    if query.lower() in line.lower():
                        results.append(f"{relative}:{number}: {line[:500]}")
                        if len(results) >= 200:
                            return results
            except (OSError, UnicodeDecodeError):
                continue
        return results

    def write_file(self, path: str, content: str) -> str:
        if len(content) > MAX_FILE_CHARS:
            raise WorkspaceError(f"Generated file exceeds {MAX_FILE_CHARS} characters")
        file_path = self._safe_path(path, allow_missing=True)
        relative = file_path.relative_to(self._root()).as_posix()
        if self._is_forbidden(relative):
            raise WorkspaceError(f"The solver may not modify sensitive path: {relative}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8", newline="")
        return f"Wrote {relative} ({len(content)} characters)"

    def replace_text(
        self, path: str, old_text: str, new_text: str, expected_replacements: int = 1
    ) -> str:
        file_path = self._safe_path(path)
        relative = file_path.relative_to(self._root()).as_posix()
        if self._is_forbidden(relative):
            raise WorkspaceError(f"The solver may not modify sensitive path: {relative}")
        if not old_text:
            raise WorkspaceError("old_text must not be empty")
        content = file_path.read_text(encoding="utf-8")
        actual = content.count(old_text)
        expected = max(1, int(expected_replacements))
        if actual != expected:
            raise WorkspaceError(
                f"Expected {expected} exact occurrence(s), found {actual} in {relative}"
            )
        updated = content.replace(old_text, new_text)
        if len(updated) > MAX_FILE_CHARS:
            raise WorkspaceError(f"Edited file exceeds {MAX_FILE_CHARS} characters")
        file_path.write_text(updated, encoding="utf-8", newline="")
        return f"Replaced {actual} occurrence(s) in {relative}"

    async def diff(self) -> str:
        # Intent-to-add makes new files visible without staging their contents.
        await self._git("add", "-N", "--", ".")
        result = await self._git("diff", "--no-ext-diff", "--", ".")
        return result.stdout[:200_000]

    async def changed_files(self) -> list[str]:
        result = await self._git("status", "--porcelain", "-z")
        paths = []
        for entry in result.stdout.split("\0"):
            if not entry:
                continue
            paths.append(entry[3:] if len(entry) > 3 else entry)
        return paths

    async def commit_and_push(
        self,
        fork_clone_url: str,
        branch: str,
        commit_message: str,
    ) -> str:
        _validate_clone_url(fork_clone_url)
        _validate_branch(branch)
        changed = await self.changed_files()
        if not changed:
            raise WorkspaceError("The solver produced no code changes")
        forbidden = [path for path in changed if self._is_forbidden(path)]
        if forbidden:
            raise WorkspaceError(f"Refusing sensitive changes: {', '.join(forbidden)}")
        await self._git("add", "-N", "--", ".")
        check = await self._git("diff", "--check", check=False)
        if check.returncode != 0:
            raise WorkspaceError(f"Generated diff failed validation: {_short(check.stdout)}")
        await self._git("add", "--", ".")
        await self._git("commit", "-m", commit_message[:200])
        await self._git("remote", "add", "fork", fork_clone_url)
        push = await self._git(
            "push", "fork", f"HEAD:refs/heads/{branch}", check=False
        )
        if push.returncode != 0:
            raise WorkspaceError(f"GitHub rejected solver branch: {_short(push.stderr)}")
        return (await self._git("rev-parse", "HEAD")).stdout.strip()

    def _safe_path(
        self, path: str, allow_missing: bool = False, allow_directory: bool = False
    ) -> Path:
        relative = PurePosixPath((path or ".").replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise WorkspaceError("Unsafe repository path")
        candidate = self._root().joinpath(*relative.parts).resolve()
        if candidate != self._root() and self._root() not in candidate.parents:
            raise WorkspaceError("Repository path escaped the workspace")
        if not allow_missing and not allow_directory and not candidate.exists():
            raise WorkspaceError(f"Path does not exist: {path}")
        return candidate

    def _is_forbidden(self, path: str) -> bool:
        parts = tuple(part.lower() for part in PurePosixPath(path).parts)
        name = parts[-1].lower() if parts else ""
        return (
            any(parts[:len(prefix)] == prefix for prefix in FORBIDDEN_PREFIXES)
            or (name.startswith(".env") and name != ".env.example")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        )

    def _root(self) -> Path:
        if self.root is None:
            raise WorkspaceError("Workspace is not open")
        return self.root

    async def _git(self, *args: str, cwd: Path | None = None, check: bool = True):
        if not shutil.which("git"):
            raise WorkspaceError("Git is not installed")
        env = _git_auth_env(self._token)
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd or self._root()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise WorkspaceError(f"Git command timed out: {args[0]}") from exc
        result = GitResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise WorkspaceError(
                f"Git {args[0]} failed: {_short(result.stderr or result.stdout)}"
            )
        return result


class GitResult:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_auth_env(token: str) -> dict:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
    }
    return {key: value for key, value in env.items() if value}


def _validate_clone_url(url: str) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", url):
        raise WorkspaceError("Unexpected GitHub clone URL")


def _validate_branch(branch: str) -> None:
    if (
        not branch
        or branch.startswith("-")
        or ".." in branch
        or any(character in branch for character in (" ", "\n", "\r", "~", "^", ":"))
    ):
        raise WorkspaceError("Unsafe Git branch name")


def _short(value: str, limit: int = 800) -> str:
    return " ".join((value or "").split())[:limit]
