import pytest

from app.services import github


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo/issues/12", ("owner/repo", 12)),
        ("https://github.com/owner/repo/issues/12/", ("owner/repo", 12)),
        ("https://github.com/owner/repo/pull/12", None),
        ("not-a-url", None),
    ],
)
def test_parse_issue_url(url, expected):
    assert github.parse_issue_url(url) == expected


def test_assignment_requires_open_issue_and_exact_user():
    issue = {
        "state": "open",
        "assignees": [{"login": "Muyideen-js"}, {"login": "another"}],
    }
    assert github.is_open_and_assigned(issue, "muyideen-JS") is True
    assert github.is_open_and_assigned(issue, "someone-else") is False
    issue["state"] = "closed"
    assert github.is_open_and_assigned(issue, "Muyideen-js") is False


def test_a_configured_program_label_is_required(monkeypatch):
    monkeypatch.setenv("PROGRAM_LABELS", "GrantFox OSS,Stellar Wave")
    assert github.is_program_issue({"labels": [{"name": "GRANTFOX OSS"}]}) is True
    assert github.is_program_issue({"labels": [{"name": "Stellar Wave"}]}) is True
    assert github.is_program_issue({"labels": [{"name": "Mobile"}]}) is False


def test_configured_program_labels_are_parsed_and_deduplicated(monkeypatch):
    monkeypatch.setenv("PROGRAM_LABELS", " GrantFox OSS ,Stellar Wave,GrantFox OSS,")
    assert github.configured_program_labels() == ["GrantFox OSS", "Stellar Wave"]


class FakeResponse:
    status_code = 200

    def __init__(self, data, text=""):
        self._data = data
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class SearchClient:
    def __init__(self):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params):
        self.queries.append(params["q"])
        if "Stellar Wave" in params["q"]:
            return FakeResponse({
                "items": [{"id": 2, "number": 5, "title": "Stellar Wave issue"}]
            })
        return FakeResponse({
            "items": [
                {"id": 1, "number": 1, "title": "Issue"},
                {"id": 3, "number": 2, "title": "PR", "pull_request": {}},
            ]
        })


@pytest.mark.asyncio
async def test_assignment_search_merges_every_configured_program_label(monkeypatch):
    client = SearchClient()
    monkeypatch.setenv("PROGRAM_LABELS", "GrantFox OSS,Stellar Wave")
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    issues = await github.search_assigned_program_issues("token", "Muyideen-js")
    assert {issue["id"] for issue in issues} == {1, 2}
    assert any("assignee:Muyideen-js" in q and 'label:"GrantFox OSS"' in q for q in client.queries)
    assert any("assignee:Muyideen-js" in q and 'label:"Stellar Wave"' in q for q in client.queries)


class UnrestrictedSearchClient:
    def __init__(self):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params):
        self.queries.append(params["q"])
        return FakeResponse({
            "items": [
                {"id": 1, "number": 1, "title": "Any issue"},
                {"id": 2, "number": 2, "title": "PR", "pull_request": {}},
            ]
        })


@pytest.mark.asyncio
async def test_search_all_assigned_issues_ignores_program_labels(monkeypatch):
    client = UnrestrictedSearchClient()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    issues = await github.search_all_assigned_issues("token", "Muyideen-js")
    assert [issue["id"] for issue in issues] == [1]
    assert client.queries == ["is:issue is:open assignee:Muyideen-js"]
    assert "label:" not in client.queries[0]


def test_repo_from_issue_prefers_repository_url():
    assert github.repo_from_issue({
        "repository_url": "https://api.github.com/repos/owner/repo",
        "html_url": "https://github.com/other/other/issues/9",
    }) == "owner/repo"
    assert github.repo_from_issue({
        "html_url": "https://github.com/owner/repo/issues/9",
    }) == "owner/repo"
    assert github.repo_from_issue({}) is None


class CiDetailsClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params=None):
        if url.endswith("/check-runs"):
            return FakeResponse({
                "check_runs": [{
                    "id": 44,
                    "name": "test",
                    "conclusion": "failure",
                    "details_url": "https://github.com/owner/repo/actions/runs/12/job/99",
                    "output": {"title": "Tests failed", "summary": "One failure"},
                }]
            })
        if url.endswith("/status"):
            return FakeResponse({"statuses": []})
        if url.endswith("/check-runs/44/annotations"):
            return FakeResponse([{
                "path": "src/app.ts",
                "start_line": 7,
                "annotation_level": "failure",
                "message": "Type mismatch",
            }])
        if url.endswith("/actions/jobs/99/logs"):
            return FakeResponse({}, text="npm test\nExpected true but received false")
        raise AssertionError(f"Unexpected URL: {url}")


@pytest.mark.asyncio
async def test_ci_failure_details_include_annotations_and_action_logs(monkeypatch):
    client = CiDetailsClient()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    details = await github.get_ci_failure_details("token", "owner/repo", "abc")
    assert "src/app.ts:7" in details
    assert "Type mismatch" in details
    assert "Expected true but received false" in details


def test_actions_job_id_is_parsed_only_from_job_urls():
    assert github._actions_job_id(
        "https://github.com/owner/repo/actions/runs/12/job/99"
    ) == 99
    assert github._actions_job_id("https://example.com/build/99") is None


class PullFilesClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params):
        assert url.endswith("/pulls/153/files")
        return FakeResponse([{"filename": "src/app.ts"}, {"filename": "tests/app.test.ts"}])


@pytest.mark.asyncio
async def test_pr_changed_files_are_loaded_for_repair(monkeypatch):
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: PullFilesClient())
    paths = await github.get_pr_changed_files("token", "owner/repo", 153)
    assert paths == ["src/app.ts", "tests/app.test.ts"]


class CiStatusClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params=None):
        if url.endswith("/check-runs"):
            return FakeResponse({
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            })
        if url.endswith("/status"):
            return FakeResponse({
                "state": "failure",
                "statuses": [{
                    "context": "Vercel",
                    "state": "failure",
                    "description": "Authorization required to deploy.",
                }],
            })
        raise AssertionError(f"Unexpected URL: {url}")


@pytest.mark.asyncio
async def test_vercel_authorization_failure_does_not_block_passing_code_ci(monkeypatch):
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: CiStatusClient())
    assert await github.get_ci_status("token", "owner/repo", "abc") == "success"


def test_real_vercel_build_failure_is_not_ignored():
    assert github._is_noncode_authorization_failure({
        "context": "Vercel",
        "state": "failure",
        "description": "Build failed",
    }) is False
