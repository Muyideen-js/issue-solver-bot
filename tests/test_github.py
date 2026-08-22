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

    def __init__(self, data):
        self._data = data

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
