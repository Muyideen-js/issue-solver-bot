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

    async def fake_request(messages, **kwargs):
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


def test_old_tool_outputs_are_compacted_to_context_budget():
    messages = [
        {"role": "tool", "content": str(index) * 12_000}
        for index in range(8)
    ]
    coding_agent._compact_tool_history(messages)
    retained = [
        message["content"]
        for message in messages
        if not message["content"].startswith(coding_agent.COMPACTED_TOOL_PREFIX)
    ]
    assert sum(len(content) for content in retained) <= (
        coding_agent.MAX_RETAINED_TOOL_OUTPUT_CHARS
    )
    assert any(
        message["content"].startswith(coding_agent.COMPACTED_TOOL_PREFIX)
        for message in messages
    )


def test_empty_tool_calls_are_omitted_from_assistant_history():
    history = coding_agent._assistant_history_message({
        "content": "I need to inspect another file.",
        "tool_calls": [],
    })
    assert history == {
        "role": "assistant",
        "content": "I need to inspect another file.",
    }


def test_nonempty_tool_calls_are_preserved_in_assistant_history():
    tool_calls = [{"id": "call-1", "function": {"name": "list_files"}}]
    history = coding_agent._assistant_history_message({
        "content": None,
        "tool_calls": tool_calls,
    })
    assert history["tool_calls"] == tool_calls


@pytest.mark.asyncio
async def test_agent_preserves_changed_draft_at_turn_limit(monkeypatch):
    monkeypatch.setenv("SOLVER_MAX_TURNS", "1")

    async def fake_request(messages, **kwargs):
        return {
            "content": None,
            "tool_calls": [{
                "id": "1",
                "function": {
                    "name": "replace_text",
                    "arguments": (
                        '{"path":"src/app.py","old_text":"old","new_text":"new",'
                        '"expected_replacements":1}'
                    ),
                },
            }],
        }

    monkeypatch.setattr(coding_agent, "_request_agent", fake_request)
    workspace = FakeWorkspace()
    result = await coding_agent.solve_issue(
        workspace, "owner/repo", 9, "Fix it", "Requirements"
    )
    assert result["budget_exhausted"] is True
    assert result["changed_files"] == ["src/app.py"]
    assert workspace.diff_seen is True


@pytest.mark.asyncio
async def test_implementation_agent_is_forced_from_discovery_into_editing(monkeypatch):
    monkeypatch.setenv("SOLVER_MAX_TURNS", "8")
    tool_sets = []

    async def fake_request(messages, **kwargs):
        names = {item["function"]["name"] for item in kwargs["tools"]}
        tool_sets.append(names)
        call_number = len(tool_sets)
        if call_number <= 6:
            return {"content": "Still investigating.", "tool_calls": []}
        if call_number == 7:
            return {
                "content": None,
                "tool_calls": [{
                    "id": "edit-1",
                    "function": {
                        "name": "replace_text",
                        "arguments": (
                            '{"path":"src/app.py","old_text":"old","new_text":"new",'
                            '"expected_replacements":1}'
                        ),
                    },
                }],
            }
        return {
            "content": None,
            "tool_calls": [
                {"id": "diff-1", "function": {"name": "inspect_diff", "arguments": "{}"}},
                {
                    "id": "finish-1",
                    "function": {
                        "name": "finish",
                        "arguments": '{"summary":"implemented","test_plan":"run CI"}',
                    },
                },
            ],
        }

    monkeypatch.setattr(coding_agent, "_request_agent", fake_request)
    result = await coding_agent.solve_issue(
        FakeWorkspace(), "owner/repo", 10, "Implement feature", "Requirements"
    )

    assert {"list_files", "search"}.issubset(tool_sets[0])
    assert "list_files" not in tool_sets[4]
    assert "search" not in tool_sets[4]
    assert {"read_file", "read_files"}.issubset(tool_sets[4])
    assert tool_sets[6] == {"write_file", "replace_text", "inspect_diff", "finish"}
    assert result["summary"] == "implemented"


@pytest.mark.asyncio
async def test_repair_mode_preloads_changed_files_and_ci_details(monkeypatch):
    monkeypatch.setenv("SOLVER_REPAIR_MAX_TURNS", "4")
    requests = []
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "1",
                "function": {
                    "name": "replace_text",
                    "arguments": (
                        '{"path":"src/app.py","old_text":"old","new_text":"new",'
                        '"expected_replacements":1}'
                    ),
                },
            }],
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "2",
                "function": {"name": "inspect_diff", "arguments": "{}"},
            }],
        },
        {
            "content": None,
            "tool_calls": [{
                "id": "3",
                "function": {
                    "name": "finish",
                    "arguments": '{"summary":"fixed CI","test_plan":"rerun CI"}',
                },
            }],
        },
    ])

    async def fake_request(messages, **kwargs):
        requests.append((list(messages), kwargs["tools"]))
        return next(responses)

    monkeypatch.setattr(coding_agent, "_request_agent", fake_request)
    result = await coding_agent.solve_issue(
        FakeWorkspace(),
        "owner/repo",
        9,
        "Fix it",
        "Requirements",
        mode="repair",
        focus_files=["src/app.py"],
        ci_failure_details=(
            "##[error]src/app.py(1,2): error TS2352: unsafe tuple conversion"
        ),
        repeated_ci_failure=True,
    )
    first_prompt = requests[0][0][1]["content"]
    first_tool_names = {item["function"]["name"] for item in requests[0][1]}
    assert "1: old = True" in first_prompt
    assert "error TS2352" in first_prompt
    assert "Exact compiler/test diagnostics" in first_prompt
    assert "previous repair did not clear" in requests[0][0][0]["content"]
    assert "list_files" not in first_tool_names
    assert result["summary"] == "fixed CI"


def test_exact_diagnostics_extracts_compiler_errors_from_action_log():
    details = (
        "2026-01-01 normal output\n"
        "job Type-check ##[error]src/app.ts(84,22): error TS2352: bad cast\n"
        "Process completed with exit code 2"
    )
    extracted = coding_agent._extract_exact_diagnostics(details)
    assert "src/app.ts(84,22)" in extracted
    assert "normal output" not in extracted


@pytest.mark.asyncio
async def test_gemini_connection_test_makes_a_tiny_real_request(monkeypatch):
    observed = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

    class FakeClient:
        def __init__(self, timeout):
            observed["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers, json):
            observed.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(coding_agent.httpx, "AsyncClient", FakeClient)
    result = await coding_agent.test_ai_connection(
        "gemini", "gemini-test-key", "gemini-3.5-flash-lite"
    )

    assert result == {
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
    }
    assert observed["url"].endswith("/v1beta/openai/chat/completions")
    assert observed["headers"] == {"Authorization": "Bearer gemini-test-key"}
    assert observed["payload"]["max_tokens"] == 8
    assert "tools" not in observed["payload"]
