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


def test_grantfox_label_is_required(monkeypatch):
    monkeypatch.setenv("GRANTFOX_LABEL", "GrantFox OSS")
    assert github.is_grantfox_issue({"labels": [{"name": "GRANTFOX OSS"}]}) is True
    assert github.is_grantfox_issue({"labels": [{"name": "Stellar Wave"}]}) is False


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
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers, params):
        self.params = params
        return FakeResponse({
            "items": [
                {"number": 1, "title": "Issue"},
                {"number": 2, "title": "PR", "pull_request": {}},
            ]
        })


@pytest.mark.asyncio
async def test_assignment_search_uses_connected_username_and_grantfox_label(monkeypatch):
    client = SearchClient()
    monkeypatch.setenv("GRANTFOX_LABEL", "GrantFox OSS")
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda **kwargs: client)
    issues = await github.search_assigned_grantfox_issues("token", "Muyideen-js")
    assert len(issues) == 1
    assert "assignee:Muyideen-js" in client.params["q"]
    assert 'label:"GrantFox OSS"' in client.params["q"]


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
