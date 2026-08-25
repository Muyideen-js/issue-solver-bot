import asyncio

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from app.main import app
from app.models.database import (
    AsyncSessionLocal,
    DASHBOARD_ID_PREFIX,
    IssueJob,
    PortalUser,
    SolverUser,
    bootstrap_admin,
)
from app.services import github as gh
from app.services.crypto import encrypt_token
from app.services.password import hash_password

client = TestClient(app)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password-123"


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    dashboard.LOGIN_ATTEMPTS.clear()
    yield


def _bootstrap_admin(monkeypatch, username=ADMIN_USERNAME, password=ADMIN_PASSWORD):
    monkeypatch.setenv("DASHBOARD_USERNAME", username)
    monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    return asyncio.run(bootstrap_admin())


def _login(test_client, username, password):
    return test_client.post("/api/login", json={"username": username, "password": password})


def _login_admin(monkeypatch, test_client=None):
    admin = _bootstrap_admin(monkeypatch)
    response = _login(test_client or client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert response.status_code == 200, response.text
    return admin


async def _add_dashboard_account(owner_id: int, username: str = "octocat") -> SolverUser:
    async with AsyncSessionLocal() as db:
        account = SolverUser(
            telegram_id=f"{DASHBOARD_ID_PREFIX}test-{owner_id}-{username}",
            github_username=username,
            github_token_encrypted=encrypt_token("token"),
            owner_portal_user_id=owner_id,
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


# --- Login / session basics ------------------------------------------------

def test_login_fails_when_no_admin_bootstrapped():
    response = _login(client, "admin", "whatever-password")
    assert response.status_code == 401


def test_login_rejects_wrong_password(monkeypatch):
    _bootstrap_admin(monkeypatch)
    response = _login(client, ADMIN_USERNAME, "wrong-password")
    assert response.status_code == 401


def test_login_rate_limited_after_too_many_failures(monkeypatch):
    _bootstrap_admin(monkeypatch)
    for _ in range(8):
        _login(client, ADMIN_USERNAME, "wrong-password")
    response = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert response.status_code == 429


def test_unauthenticated_api_call_is_rejected():
    response = client.get("/api/accounts")
    assert response.status_code == 401


def test_bootstrap_admin_backfills_orphaned_dashboard_accounts(monkeypatch):
    async def seed_orphan():
        async with AsyncSessionLocal() as db:
            db.add(SolverUser(
                telegram_id=f"{DASHBOARD_ID_PREFIX}orphan",
                github_username="orphaned",
                github_token_encrypted=encrypt_token("token"),
            ))
            await db.commit()

    asyncio.run(seed_orphan())
    admin = _bootstrap_admin(monkeypatch)

    async def read_owner():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                SolverUser.__table__.select().where(SolverUser.github_username == "orphaned")
            )
            return result.first()

    row = asyncio.run(read_owner())
    assert row.owner_portal_user_id == admin.id


def test_created_user_must_change_password_then_can_change_it(monkeypatch):
    _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "newperson", "temporary_password": "temp-password-1"},
    )
    assert created.status_code == 200, created.text

    user_client = TestClient(app)
    login = _login(user_client, "newperson", "temp-password-1")
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True

    me = user_client.get("/api/me").json()
    assert me["must_change_password"] is True

    changed = user_client.post(
        "/api/change-password",
        json={"current_password": "temp-password-1", "new_password": "a-new-real-password"},
    )
    assert changed.status_code == 200, changed.text

    me_after = user_client.get("/api/me").json()
    assert me_after["must_change_password"] is False

    relogin_client = TestClient(app)
    relogin = _login(relogin_client, "newperson", "a-new-real-password")
    assert relogin.status_code == 200


# --- Ownership isolation -----------------------------------------------

def test_two_portal_users_accounts_are_isolated(monkeypatch):
    admin = _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "isolateduser", "temporary_password": "temp-password-1"},
    ).json()

    other_client = TestClient(app)
    _login(other_client, "isolateduser", "temp-password-1")
    other_account = asyncio.run(_add_dashboard_account(created["id"], "otherpersonsgh"))

    admin_account = asyncio.run(_add_dashboard_account(admin.id, "adminsgh"))

    admin_accounts = client.get("/api/accounts").json()
    assert [a["github_username"] for a in admin_accounts] == ["adminsgh"]

    other_accounts = other_client.get("/api/accounts").json()
    assert [a["github_username"] for a in other_accounts] == ["otherpersonsgh"]

    # Admin (not impersonating) can't reach the other user's account by ID.
    cross_access = client.get(f"/api/accounts/{other_account.id}/issues")
    assert cross_access.status_code == 404

    cross_access_reverse = other_client.get(f"/api/accounts/{admin_account.id}/issues")
    assert cross_access_reverse.status_code == 404


# --- Admin impersonation -------------------------------------------------

def test_admin_view_as_sees_and_acts_on_target_accounts(monkeypatch):
    _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "targetuser", "temporary_password": "temp-password-1"},
    ).json()
    target_account = asyncio.run(_add_dashboard_account(created["id"], "targetgh"))

    view_as = client.post(f"/api/admin/users/{created['id']}/view-as")
    assert view_as.status_code == 200

    me = client.get("/api/me").json()
    assert me["impersonating"] == {"id": created["id"], "username": "targetuser"}

    accounts = client.get("/api/accounts").json()
    assert [a["github_username"] for a in accounts] == ["targetgh"]

    async def fake_get_issue(token, repo, number):
        return {
            "number": number, "title": "Needs a fix",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "state": "open", "assignees": [{"login": "targetgh"}],
        }

    monkeypatch.setattr(gh, "get_issue", fake_get_issue)
    fix = client.post(
        f"/api/accounts/{target_account.id}/issues/fix", json={"repo": "o/r", "number": 1}
    )
    assert fix.status_code == 200
    assert fix.json() == {"queued": True}

    exited = client.post("/api/admin/exit-view-as")
    assert exited.status_code == 200

    accounts_after = client.get("/api/accounts").json()
    assert accounts_after == []


def test_admin_cannot_manage_another_admin(monkeypatch):
    _login_admin(monkeypatch)

    async def add_second_admin():
        async with AsyncSessionLocal() as db:
            second = PortalUser(
                username="secondadmin",
                password_hash=hash_password("whatever-password-1"),
                is_admin=True,
                must_change_password=False,
            )
            db.add(second)
            await db.commit()
            await db.refresh(second)
        return second

    second = asyncio.run(add_second_admin())

    assert client.post(f"/api/admin/users/{second.id}/view-as").status_code == 404
    assert client.delete(f"/api/admin/users/{second.id}").status_code == 404
    assert client.post(
        f"/api/admin/users/{second.id}/reset-password",
        json={"temporary_password": "another-password-1"},
    ).status_code == 404


def test_admin_delete_cascades_accounts_and_jobs(monkeypatch):
    _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "doomeduser", "temporary_password": "temp-password-1"},
    ).json()
    account = asyncio.run(_add_dashboard_account(created["id"], "doomedgh"))
    asyncio.run(_add_job(account.telegram_id, "o/r", 1))

    response = client.delete(f"/api/admin/users/{created['id']}")
    assert response.status_code == 200

    async def counts():
        async with AsyncSessionLocal() as db:
            user = await db.get(PortalUser, created["id"])
            solver = await db.get(SolverUser, account.id)
            jobs = await db.execute(
                IssueJob.__table__.select().where(IssueJob.telegram_id == account.telegram_id)
            )
            return user, solver, jobs.first()

    user, solver, job_row = asyncio.run(counts())
    assert user is None
    assert solver is None
    assert job_row is None


# --- Existing account/issue behavior, now behind a real login -------------

def test_accounts_endpoint_excludes_telegram_accounts(monkeypatch):
    _login_admin(monkeypatch)

    async def add_telegram_user():
        async with AsyncSessionLocal() as db:
            db.add(SolverUser(
                telegram_id="123456789",
                github_username="telegram-owner",
                github_token_encrypted=encrypt_token("token"),
            ))
            await db.commit()

    asyncio.run(add_telegram_user())

    listed = client.get("/api/accounts")
    assert listed.json() == []


def test_add_account_rejects_duplicate_username(monkeypatch):
    _login_admin(monkeypatch)

    async def fake_validate_token(token):
        return "octocat"

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    first = client.post("/api/accounts", json={"token": "a"})
    assert first.status_code == 200

    second = client.post("/api/accounts", json={"token": "b"})
    assert second.status_code == 409


def test_add_account_rejects_invalid_token(monkeypatch):
    _login_admin(monkeypatch)

    async def fake_validate_token(token):
        return None

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    response = client.post("/api/accounts", json={"token": "bad"})
    assert response.status_code == 400


def test_delete_account_blocked_with_active_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    asyncio.run(_add_job(account.telegram_id, "o/r", 1, status="QUEUED"))

    response = client.delete(f"/api/accounts/{account.id}")
    assert response.status_code == 409


def test_issues_endpoint_joins_job_status(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
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

    response = client.get(f"/api/accounts/{account.id}/issues")
    assert response.status_code == 200
    issues = response.json()
    assert len(issues) == 1
    assert issues[0]["status"] == "DONE"
    assert issues[0]["pr_url"] == "https://github.com/o/r/pull/9"
    assert issues[0]["labels"] == ["bug"]


def test_fix_issue_enqueues_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))

    async def fake_get_issue(token, repo, number):
        return {
            "number": number, "title": "Needs a fix",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "state": "open", "assignees": [{"login": account.github_username}],
        }

    monkeypatch.setattr(gh, "get_issue", fake_get_issue)

    response = client.post(
        f"/api/accounts/{account.id}/issues/fix", json={"repo": "o/r", "number": 7}
    )
    assert response.status_code == 200
    assert response.json() == {"queued": True}

    jobs = client.get(f"/api/accounts/{account.id}/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "QUEUED"


def test_mark_ready_marks_a_draft_pr_ready_and_completes_the_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
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
        f"/api/accounts/{account.id}/issues/mark-ready", json={"repo": "o/r", "number": 9}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ready": True}
    assert marked == ["PR_kwabc"]

    jobs = client.get(f"/api/accounts/{account.id}/jobs").json()
    assert jobs[0]["status"] == "DONE"


def test_mark_ready_skips_the_mutation_when_already_ready(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
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
        f"/api/accounts/{account.id}/issues/mark-ready", json={"repo": "o/r", "number": 10}
    )
    assert response.status_code == 200


def test_mark_ready_requires_a_tracked_draft_pr(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))

    response = client.post(
        f"/api/accounts/{account.id}/issues/mark-ready", json={"repo": "o/r", "number": 99}
    )
    assert response.status_code == 404


def test_fix_all_queues_every_unrestricted_issue(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))

    async def fake_search(token, username):
        return [
            {"id": 1, "number": 1, "title": "A", "html_url": "https://github.com/o/r/issues/1",
             "repository_url": "https://api.github.com/repos/o/r"},
            {"id": 2, "number": 2, "title": "B", "html_url": "https://github.com/o/r/issues/2",
             "repository_url": "https://api.github.com/repos/o/r"},
        ]

    monkeypatch.setattr(gh, "search_all_assigned_issues", fake_search)

    response = client.post(f"/api/accounts/{account.id}/issues/fix-all")
    assert response.status_code == 200
    assert response.json() == {"discovered": 2, "queued": 2}


def test_skip_then_unskip_issue(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))

    skip = client.post(
        f"/api/accounts/{account.id}/issues/skip",
        json={"repo": "o/r", "number": 3, "title": "Not worth it"},
    )
    assert skip.status_code == 200

    jobs = client.get(f"/api/accounts/{account.id}/jobs").json()
    assert jobs[0]["status"] == "SKIPPED_BY_USER"

    unskip = client.post(
        f"/api/accounts/{account.id}/issues/unskip", json={"repo": "o/r", "number": 3}
    )
    assert unskip.status_code == 200

    jobs_after = client.get(f"/api/accounts/{account.id}/jobs").json()
    assert jobs_after == []


def test_add_list_delete_account_flow(monkeypatch):
    _login_admin(monkeypatch)

    async def fake_validate_token(token):
        return "octocat"

    monkeypatch.setattr(gh, "validate_token", fake_validate_token)

    response = client.post("/api/accounts", json={"token": "ghp_fake"})
    assert response.status_code == 200, response.text
    account = response.json()
    assert account["github_username"] == "octocat"

    listed = client.get("/api/accounts")
    assert listed.status_code == 200
    assert [item["github_username"] for item in listed.json()] == ["octocat"]

    deleted = client.delete(f"/api/accounts/{account['id']}")
    assert deleted.status_code == 200

    listed_again = client.get("/api/accounts")
    assert listed_again.json() == []
