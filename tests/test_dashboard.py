import asyncio

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from app.main import app
from app.models.database import AsyncSessionLocal, DASHBOARD_ID_PREFIX, IssueJob, SolverUser
from app.services import github as gh
from app.services.crypto import encrypt_token

client = TestClient(app)


async def _add_dashboard_account(username: str = "octocat") -> SolverUser:
    async with AsyncSessionLocal() as db:
        account = SolverUser(
            telegram_id=f"{DASHBOARD_ID_PREFIX}test-{username}",
            github_username=username,
            github_token_encrypted=encrypt_token("token"),
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
    return account


async def _add_job(telegram_id: str, repo: str, number: int, **fields) -> IssueJob:
    async with AsyncSessionLocal() as db:
        job = IssueJob(
            telegram_id=telegram_id,
            repo_full_name=repo,
            issue_number=number,
            issue_title=fields.pop("issue_title", "Bug"),
            issue_url=fields.pop("issue_url", f"https://github.com/{repo}/issues/{number}"),
            status=fields.pop("status", "QUEUED"),
            **fields,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


def test_dashboard_disabled_without_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    response = client.get("/api/accounts", auth=("admin", "whatever"))
    assert response.status_code == 503


def test_dashboard_rejects_wrong_credentials(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    response = client.get("/api/accounts", auth=("admin", "wrong-password"))
    assert response.status_code == 401


def test_accounts_endpoint_excludes_telegram_accounts(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")

    async def add_telegram_user():
        async with AsyncSessionLocal() as db:
            db.add(SolverUser(
                telegram_id="123456789",
                github_username="telegram-owner",
                github_token_encrypted=encrypt_token("token"),
            ))
            await db.commit()

    asyncio.run(add_telegram_user())

    listed = client.get("/api/accounts", auth=("admin", "secret"))
    assert listed.json() == []


def test_add_account_rejects_duplicate_username(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")

    async def fake_validate_token(token):
        return "octocat"

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    first = client.post("/api/accounts", json={"token": "a"}, auth=("admin", "secret"))
    assert first.status_code == 200

    second = client.post("/api/accounts", json={"token": "b"}, auth=("admin", "secret"))
    assert second.status_code == 409


def test_add_account_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")

    async def fake_validate_token(token):
        return None

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    response = client.post("/api/accounts", json={"token": "bad"}, auth=("admin", "secret"))
    assert response.status_code == 400


def test_delete_account_blocked_with_active_job(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())
    asyncio.run(_add_job(account.telegram_id, "o/r", 1, status="QUEUED"))

    response = client.delete(f"/api/accounts/{account.id}", auth=("admin", "secret"))
    assert response.status_code == 409


def test_issues_endpoint_joins_job_status(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 5, status="DONE", draft_pr_url="https://github.com/o/r/pull/9"
    ))

    async def fake_search(token, username):
        return [{
            "id": 1, "number": 5, "title": "Fixed already",
            "html_url": "https://github.com/o/r/issues/5",
            "repository_url": "https://api.github.com/repos/o/r",
            "labels": [{"name": "bug"}],
        }]

    monkeypatch.setattr(gh, "search_all_assigned_issues", fake_search)

    response = client.get(f"/api/accounts/{account.id}/issues", auth=("admin", "secret"))
    assert response.status_code == 200
    issues = response.json()
    assert len(issues) == 1
    assert issues[0]["status"] == "DONE"
    assert issues[0]["pr_url"] == "https://github.com/o/r/pull/9"
    assert issues[0]["labels"] == ["bug"]


def test_fix_issue_enqueues_job(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())

    async def fake_get_issue(token, repo, number):
        return {
            "number": number, "title": "Needs a fix",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "state": "open", "assignees": [{"login": account.github_username}],
        }

    monkeypatch.setattr(gh, "get_issue", fake_get_issue)

    response = client.post(
        f"/api/accounts/{account.id}/issues/fix",
        json={"repo": "o/r", "number": 7},
        auth=("admin", "secret"),
    )
    assert response.status_code == 200
    assert response.json() == {"queued": True}

    jobs = client.get(f"/api/accounts/{account.id}/jobs", auth=("admin", "secret")).json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "QUEUED"


def test_mark_ready_marks_a_draft_pr_ready_and_completes_the_job(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 9, status="WAITING_CI",
        draft_pr_number=42, draft_pr_url="https://github.com/o/r/pull/42",
    ))

    async def fake_get_pr(token, repo, number):
        return {
            "state": "open", "draft": True, "node_id": "PR_kwabc",
            "head": {"sha": "newsha"},
        }

    marked = []

    async def fake_mark_pr_ready(token, node_id):
        marked.append(node_id)

    monkeypatch.setattr(gh, "get_pr", fake_get_pr)
    monkeypatch.setattr(gh, "mark_pr_ready", fake_mark_pr_ready)

    response = client.post(
        f"/api/accounts/{account.id}/issues/mark-ready",
        json={"repo": "o/r", "number": 9},
        auth=("admin", "secret"),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ready": True}
    assert marked == ["PR_kwabc"]

    jobs = client.get(f"/api/accounts/{account.id}/jobs", auth=("admin", "secret")).json()
    assert jobs[0]["status"] == "DONE"


def test_mark_ready_skips_the_mutation_when_already_ready(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 10, status="WAITING_CI",
        draft_pr_number=43, draft_pr_url="https://github.com/o/r/pull/43",
    ))

    async def fake_get_pr(token, repo, number):
        return {"state": "open", "draft": False, "node_id": "PR_x", "head": {"sha": "sha"}}

    async def fail_if_called(token, node_id):
        raise AssertionError("mark_pr_ready should not be called when already ready")

    monkeypatch.setattr(gh, "get_pr", fake_get_pr)
    monkeypatch.setattr(gh, "mark_pr_ready", fail_if_called)

    response = client.post(
        f"/api/accounts/{account.id}/issues/mark-ready",
        json={"repo": "o/r", "number": 10},
        auth=("admin", "secret"),
    )
    assert response.status_code == 200


def test_mark_ready_requires_a_tracked_draft_pr(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())

    response = client.post(
        f"/api/accounts/{account.id}/issues/mark-ready",
        json={"repo": "o/r", "number": 99},
        auth=("admin", "secret"),
    )
    assert response.status_code == 404


def test_fix_all_queues_every_unrestricted_issue(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())

    async def fake_search(token, username):
        return [
            {"id": 1, "number": 1, "title": "A", "html_url": "https://github.com/o/r/issues/1",
             "repository_url": "https://api.github.com/repos/o/r"},
            {"id": 2, "number": 2, "title": "B", "html_url": "https://github.com/o/r/issues/2",
             "repository_url": "https://api.github.com/repos/o/r"},
        ]

    monkeypatch.setattr(gh, "search_all_assigned_issues", fake_search)

    response = client.post(f"/api/accounts/{account.id}/issues/fix-all", auth=("admin", "secret"))
    assert response.status_code == 200
    assert response.json() == {"discovered": 2, "queued": 2}


def test_skip_then_unskip_issue(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    account = asyncio.run(_add_dashboard_account())

    skip = client.post(
        f"/api/accounts/{account.id}/issues/skip",
        json={"repo": "o/r", "number": 3, "title": "Not worth it"},
        auth=("admin", "secret"),
    )
    assert skip.status_code == 200

    jobs = client.get(f"/api/accounts/{account.id}/jobs", auth=("admin", "secret")).json()
    assert jobs[0]["status"] == "SKIPPED_BY_USER"

    unskip = client.post(
        f"/api/accounts/{account.id}/issues/unskip",
        json={"repo": "o/r", "number": 3},
        auth=("admin", "secret"),
    )
    assert unskip.status_code == 200

    jobs_after = client.get(f"/api/accounts/{account.id}/jobs", auth=("admin", "secret")).json()
    assert jobs_after == []


def test_add_list_delete_account_flow(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")

    async def fake_validate_token(token):
        return "octocat"

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    response = client.post(
        "/api/accounts", json={"token": "ghp_fake"}, auth=("admin", "secret")
    )
    assert response.status_code == 200, response.text
    account = response.json()
    assert account["github_username"] == "octocat"

    listed = client.get("/api/accounts", auth=("admin", "secret"))
    assert listed.status_code == 200
    assert [item["github_username"] for item in listed.json()] == ["octocat"]

    deleted = client.delete(f"/api/accounts/{account['id']}", auth=("admin", "secret"))
    assert deleted.status_code == 200

    listed_again = client.get("/api/accounts", auth=("admin", "secret"))
    assert listed_again.json() == []
