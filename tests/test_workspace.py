from pathlib import Path

import pytest

from app.services.workspace import SolverWorkspace, WorkspaceError


def workspace_at(path: Path) -> SolverWorkspace:
    workspace = SolverWorkspace("token")
    workspace.root = path.resolve()
    return workspace


def test_workspace_targeted_replace(tmp_path):
    source = tmp_path / "src" / "lib.rs"
    source.parent.mkdir()
    source.write_text("fn old() {}\n", encoding="utf-8")
    workspace = workspace_at(tmp_path)
    result = workspace.replace_text("src/lib.rs", "old", "new", 1)
    assert result.startswith("Replaced 1")
    assert source.read_text(encoding="utf-8") == "fn new() {}\n"


def test_workspace_replace_fails_when_match_is_ambiguous(tmp_path):
    source = tmp_path / "file.ts"
    source.write_text("same\nsame\n", encoding="utf-8")
    workspace = workspace_at(tmp_path)
    with pytest.raises(WorkspaceError, match="found 2"):
        workspace.replace_text("file.ts", "same", "new", 1)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".GITHUB/WORKFLOWS/ci.yml",
        ".env",
        ".env.local",
        "secret.pem",
    ],
)
def test_workspace_refuses_sensitive_writes(tmp_path, path):
    workspace = workspace_at(tmp_path)
    with pytest.raises(WorkspaceError, match="sensitive path"):
        workspace.write_file(path, "unsafe")


def test_workspace_allows_env_example(tmp_path):
    workspace = workspace_at(tmp_path)
    workspace.write_file(".env.example", "SAFE=value\n")
    assert (tmp_path / ".env.example").read_text() == "SAFE=value\n"


def test_workspace_rejects_path_traversal(tmp_path):
    workspace = workspace_at(tmp_path)
    with pytest.raises(WorkspaceError, match="Unsafe repository path"):
        workspace.write_file("../outside.txt", "no")
