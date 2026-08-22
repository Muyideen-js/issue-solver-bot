import pytest

from app.services import coding_agent


class FakeWorkspace:
    def __init__(self):
        self.writes = []
        self.diff_seen = False

    def list_files(self, path):
        return ["src/app.py"]

    def read_file(self, path, start, end):
        return "1: old = True"

    def search(self, query, path):
        return ["src/app.py:1: old = True"]

    def write_file(self, path, content):
        self.writes.append((path, content))
        return "written"

    def replace_text(self, path, old, new, expected):
        self.writes.append((path, new))
        return "replaced"

    async def diff(self):
        self.diff_seen = True
        return "+new = True"

    async def changed_files(self):
        return ["src/app.py"] if self.writes else []


@pytest.mark.asyncio
async def test_agent_requires_diff_before_finish(monkeypatch):
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "1",
                "function": {
                    "name": "replace_text",
                    "arguments": '{"path":"src/app.py","old_text":"old","new_text":"new","expected_replacements":1}',
                },
            }],
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "2",
                "function": {
                    "name": "finish",
                    "arguments": '{"summary":"done","test_plan":"run tests"}',
                },
            }],
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "3",
                "function": {"name": "inspect_diff", "arguments": "{}"},
            }],
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "4",
                "function": {
                    "name": "finish",
                    "arguments": '{"summary":"done","test_plan":"run tests"}',
                },
            }],
        },
    ])

    async def fake_request(messages):
        return next(responses)

    monkeypatch.setattr(coding_agent, "_request_agent", fake_request)
    workspace = FakeWorkspace()
    result = await coding_agent.solve_issue(
        workspace, "owner/repo", 1, "Fix it", "Requirements"
    )
    assert workspace.diff_seen is True
    assert result["summary"] == "done"


def test_tool_output_is_bounded():
    oversized = "x" * (coding_agent.MAX_TOOL_OUTPUT_CHARS + 100)
    bounded = coding_agent._bounded_tool_output(oversized)
    assert len(bounded) < len(oversized)
    assert bounded.endswith("...[output truncated; narrow the request]")
