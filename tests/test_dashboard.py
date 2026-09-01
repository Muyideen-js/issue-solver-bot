import asyncio
from datetime import datetime, timedelta

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
from app.services.crypto import decrypt_token, encrypt_token
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


def test_each_portal_user_stores_a_private_encrypted_deepseek_key(monkeypatch):
    admin = _login_admin(monkeypatch)
    raw_key = "sk-this-belongs-only-to-the-admin"

    saved = client.post(
        "/api/settings/deepseek",
        json={"api_key": raw_key, "model": "deepseek-chat"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "connected": True,
        "provider": "deepseek",
        "model": "deepseek-chat",
    }

    async def read_admin():
        async with AsyncSessionLocal() as db:
            return await db.get(PortalUser, admin.id)

    stored = asyncio.run(read_admin())
    assert stored.deepseek_api_key_encrypted != raw_key
    assert decrypt_token(stored.deepseek_api_key_encrypted) == raw_key

    settings = client.get("/api/settings/deepseek")
    assert settings.status_code == 200
    assert settings.json() == {
        "connected": True,
        "provider": "deepseek",
        "model": "deepseek-chat",
    }
    assert raw_key not in settings.text


def test_saving_a_key_resumes_only_that_users_jobs(monkeypatch):
    admin = _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "separatekey", "temporary_password": "temp-password-1"},
    ).json()
    admin_account = asyncio.run(_add_dashboard_account(admin.id, "admin-gh"))
    other_account = asyncio.run(_add_dashboard_account(created["id"], "other-gh"))
    admin_job = asyncio.run(
        _add_job(admin_account.telegram_id, "o/admin", 1, status="NEEDS_API_KEY")
    )
    other_job = asyncio.run(
        _add_job(other_account.telegram_id, "o/other", 2, status="NEEDS_API_KEY")
    )

    saved = client.post(
        "/api/settings/deepseek",
        json={"api_key": "sk-admin-private-key", "model": "deepseek-chat"},
    )
    assert saved.status_code == 200, saved.text

    async def read_jobs():
        async with AsyncSessionLocal() as db:
            return await db.get(IssueJob, admin_job.id), await db.get(IssueJob, other_job.id)

    resumed, untouched = asyncio.run(read_jobs())
    assert resumed.status == "QUEUED"
    assert untouched.status == "NEEDS_API_KEY"


def test_admin_view_as_cannot_replace_the_target_users_ai_key(monkeypatch):
    admin = _login_admin(monkeypatch)
    created = client.post(
        "/api/admin/users",
        json={"username": "privateowner", "temporary_password": "temp-password-1"},
    ).json()

    user_client = TestClient(app)
    _login(user_client, "privateowner", "temp-password-1")
    user_client.post(
        "/api/settings/deepseek",
        json={"api_key": "sk-target-private-key", "model": "deepseek-chat"},
    )

    assert client.post(f"/api/admin/users/{created['id']}/view-as").status_code == 200
    assert client.post(
        "/api/settings/deepseek",
        json={"api_key": "sk-admin-own-key", "model": "deepseek-reasoner"},
    ).status_code == 200

    async def read_users():
        async with AsyncSessionLocal() as db:
            target = await db.get(PortalUser, created["id"])
            admin_row = await db.get(PortalUser, admin.id)
            return target, admin_row

    target, admin_row = asyncio.run(read_users())
    assert decrypt_token(target.deepseek_api_key_encrypted) == "sk-target-private-key"
    assert decrypt_token(admin_row.deepseek_api_key_encrypted) == "sk-admin-own-key"


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


# --- Admin DeepSeek key recovery window ----------------------------------

def _save_deepseek_key(test_client, api_key="sk-user-secret-key"):
    return test_client.post(
        "/api/settings/deepseek", json={"api_key": api_key, "model": "deepseek-v4-flash"}
    )


def _create_user_with_key(monkeypatch, username="keyuser", api_key="sk-user-secret-key"):
    """Create a portal user, log in as them, and save a DeepSeek key."""
    created = client.post(
        "/api/admin/users",
        json={"username": username, "temporary_password": "temp-password-1"},
    ).json()
    user_client = TestClient(app)
    _login(user_client, username, "temp-password-1")
    assert _save_deepseek_key(user_client, api_key).status_code == 200
    return created


async def _set_key_saved_at(user_id, saved_at):
    async with AsyncSessionLocal() as db:
        user = await db.get(PortalUser, user_id)
        user.deepseek_key_saved_at = saved_at
        await db.commit()


def test_admin_can_reveal_a_recently_saved_key(monkeypatch):
    _login_admin(monkeypatch)
    created = _create_user_with_key(monkeypatch)

    listed = client.get("/api/admin/users").json()
    row = next(user for user in listed if user["id"] == created["id"])
    assert row["ai_connected"] is True
    assert row["deepseek_key_revealable"] is True

    response = client.get(f"/api/admin/users/{created['id']}/deepseek-key")
    assert response.status_code == 200, response.text
    assert response.json()["api_key"] == "sk-user-secret-key"


def test_reveal_window_closes_after_an_hour(monkeypatch):
    _login_admin(monkeypatch)
    created = _create_user_with_key(monkeypatch, username="expireduser")

    asyncio.run(_set_key_saved_at(
        created["id"], datetime.utcnow() - timedelta(hours=1, minutes=1)
    ))

    listed = client.get("/api/admin/users").json()
    row = next(user for user in listed if user["id"] == created["id"])
    assert row["ai_connected"] is True  # key still works for solving
    assert row["deepseek_key_revealable"] is False  # but is no longer readable

    response = client.get(f"/api/admin/users/{created['id']}/deepseek-key")
    assert response.status_code == 410


def test_saving_a_new_key_restarts_the_reveal_window(monkeypatch):
    _login_admin(monkeypatch)
    created = _create_user_with_key(monkeypatch, username="rotatinguser", api_key="sk-first-key")
    asyncio.run(_set_key_saved_at(
        created["id"], datetime.utcnow() - timedelta(hours=2)
    ))
    assert client.get(f"/api/admin/users/{created['id']}/deepseek-key").status_code == 410

    user_client = TestClient(app)
    _login(user_client, "rotatinguser", "temp-password-1")
    assert _save_deepseek_key(user_client, "sk-second-key").status_code == 200

    response = client.get(f"/api/admin/users/{created['id']}/deepseek-key")
    assert response.status_code == 200
    assert response.json()["api_key"] == "sk-second-key"


def test_clearing_a_key_ends_the_reveal_window(monkeypatch):
    _login_admin(monkeypatch)
    created = _create_user_with_key(monkeypatch, username="clearinguser")

    user_client = TestClient(app)
    _login(user_client, "clearinguser", "temp-password-1")
    cleared = user_client.post(
        "/api/settings/deepseek", json={"model": "deepseek-v4-flash", "clear": True}
    )
    assert cleared.status_code == 200

    response = client.get(f"/api/admin/users/{created['id']}/deepseek-key")
    assert response.status_code == 404


def test_ai_settings_supports_openai_and_gemini(monkeypatch):
    _login_admin(monkeypatch)

    providers = client.get("/api/providers")
    assert providers.status_code == 200
    ids = {item["id"] for item in providers.json()["providers"]}
    assert ids == {"deepseek", "openai", "gemini"}

    saved = client.post(
        "/api/settings/ai",
        json={"provider": "openai", "api_key": "sk-openai-user-key", "model": "gpt-4o-mini"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "connected": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
    }

    switched = client.post(
        "/api/settings/ai",
        json={"provider": "gemini", "api_key": "gemini-user-key-123", "model": "gemini-2.0-flash"},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["provider"] == "gemini"


def test_ai_connection_test_uses_entered_key_without_saving_it(monkeypatch):
    _login_admin(monkeypatch)
    observed = {}

    async def fake_test(provider, api_key, model):
        observed.update(provider=provider, api_key=api_key, model=model)
        return {"provider": provider, "model": model}

    monkeypatch.setattr(dashboard, "test_ai_connection", fake_test)
    response = client.post(
        "/api/settings/ai/test",
        json={
            "provider": "gemini",
            "api_key": "gemini-unsaved-key",
            "model": "gemini-3.5-flash-lite",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "connected": True,
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
    }
    assert observed == {
        "provider": "gemini",
        "api_key": "gemini-unsaved-key",
        "model": "gemini-3.5-flash-lite",
    }
    settings = client.get("/api/settings/ai").json()
    assert settings["connected"] is False


def test_reveal_key_requires_admin(monkeypatch):
    _login_admin(monkeypatch)
    created = _create_user_with_key(monkeypatch, username="victimuser")

    other = client.post(
        "/api/admin/users",
        json={"username": "nosyuser", "temporary_password": "temp-password-1"},
    ).json()
    assert other["id"]

    nosy_client = TestClient(app)
    _login(nosy_client, "nosyuser", "temp-password-1")
    response = nosy_client.get(f"/api/admin/users/{created['id']}/deepseek-key")
    assert response.status_code == 403


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


def test_dashboard_prefers_completed_telegram_job_over_stale_dashboard_duplicate(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id, username="shared-user"))

    async def add_telegram_owner_and_jobs():
        async with AsyncSessionLocal() as db:
            telegram_user = SolverUser(
                telegram_id="987654321",
                github_username="shared-user",
                github_token_encrypted=encrypt_token("telegram-token"),
            )
            db.add(telegram_user)
            await db.commit()
        await _add_job(
            account.telegram_id, "o/r", 6, status="NEEDS_TESTS",
            draft_pr_number=10, draft_pr_url="https://github.com/o/r/pull/10",
        )
        await _add_job(
            telegram_user.telegram_id, "o/r", 6, status="DONE",
            draft_pr_number=10, draft_pr_url="https://github.com/o/r/pull/10",
        )

    asyncio.run(add_telegram_owner_and_jobs())

    async def fake_search(token, username):
        return [{
            "id": 6, "number": 6, "title": "Solved through Telegram",
            "html_url": "https://github.com/o/r/issues/6",
            "repository_url": "https://api.github.com/repos/o/r",
            "labels": [],
        }]

    monkeypatch.setattr(gh, "search_all_assigned_issues", fake_search)

    issues = client.get(f"/api/accounts/{account.id}/issues").json()
    jobs = client.get(f"/api/accounts/{account.id}/jobs").json()

    assert issues[0]["status"] == "DONE"
    assert len(jobs) == 1
    assert jobs[0]["status"] == "DONE"


def test_dashboard_does_not_show_needs_ci_without_a_tracked_pr(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id, username="legacy-user"))
    asyncio.run(_add_job(account.telegram_id, "o/r", 8, status="NEEDS_TESTS"))

    async def fake_search(token, username):
        return [{
            "id": 8, "number": 8, "title": "Legacy inconsistent state",
            "html_url": "https://github.com/o/r/issues/8",
            "repository_url": "https://api.github.com/repos/o/r",
            "labels": [],
        }]

    monkeypatch.setattr(gh, "search_all_assigned_issues", fake_search)

    issue = client.get(f"/api/accounts/{account.id}/issues").json()[0]
    assert issue["status"] == "NEEDS_REVIEW"
    assert issue["pr_url"] is None


def test_fix_issue_enqueues_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))

    async def pause_dashboard_account():
        async with AsyncSessionLocal() as db:
            stored = await db.get(SolverUser, account.id)
            stored.paused = True
            await db.commit()

    asyncio.run(pause_dashboard_account())

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

    async def read_account():
        async with AsyncSessionLocal() as db:
            return await db.get(SolverUser, account.id)

    assert asyncio.run(read_account()).paused is False


def test_retry_now_releases_a_delayed_queued_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id, username="retry-user"))

    async def add_paused_telegram_sibling():
        async with AsyncSessionLocal() as db:
            db.add(SolverUser(
                telegram_id="123456789",
                github_username="retry-user",
                github_token_encrypted=encrypt_token("telegram-token"),
                paused=True,
            ))
            dashboard_account = await db.get(SolverUser, account.id)
            dashboard_account.paused = True
            await db.commit()

    asyncio.run(add_paused_telegram_sibling())
    job = asyncio.run(_add_job(
        "123456789",
        "o/r",
        18,
        status="QUEUED",
        attempts=2,
        next_attempt_at=datetime.utcnow() + timedelta(hours=1),
        last_error="Temporary provider rate limit",
    ))

    before = datetime.utcnow()
    response = client.post(
        f"/api/accounts/{account.id}/issues/retry-now",
        json={"repo": "o/r", "number": 18},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"queued": True, "status": "QUEUED"}

    async def read_job():
        async with AsyncSessionLocal() as db:
            return await db.get(IssueJob, job.id)

    refreshed = asyncio.run(read_job())
    assert refreshed.telegram_id == account.telegram_id
    assert refreshed.attempts == 0
    assert before <= refreshed.next_attempt_at <= datetime.utcnow()
    assert refreshed.last_error == (
        "Dashboard Force on site requested; detached from Telegram ownership"
    )

    async def read_account():
        async with AsyncSessionLocal() as db:
            return await db.get(SolverUser, account.id)

    assert asyncio.run(read_account()).paused is False


def test_force_on_site_consolidates_a_telegram_duplicate(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id, username="duplicate-user"))

    async def seed_duplicates():
        async with AsyncSessionLocal() as db:
            db.add(SolverUser(
                telegram_id="987654321",
                github_username="duplicate-user",
                github_token_encrypted=encrypt_token("telegram-token"),
                paused=True,
            ))
            await db.commit()
        dashboard_job = await _add_job(
            account.telegram_id,
            "o/r",
            22,
            status="FAILED",
            attempts=3,
            last_error="Old dashboard failure",
        )
        telegram_job = await _add_job(
            "987654321",
            "o/r",
            22,
            status="QUEUED",
            last_error="New issue solving is paused",
        )
        return dashboard_job.id, telegram_job.id

    dashboard_job_id, telegram_job_id = asyncio.run(seed_duplicates())
    response = client.post(
        f"/api/accounts/{account.id}/issues/retry-now",
        json={"repo": "o/r", "number": 22},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"queued": True, "status": "QUEUED"}

    async def read_jobs():
        async with AsyncSessionLocal() as db:
            return (
                await db.get(IssueJob, dashboard_job_id),
                await db.get(IssueJob, telegram_job_id),
            )

    dashboard_job, telegram_job = asyncio.run(read_jobs())
    assert dashboard_job.status == "QUEUED"
    assert dashboard_job.attempts == 0
    assert dashboard_job.last_error == (
        "Dashboard Force on site requested; detached from Telegram ownership"
    )
    assert telegram_job.status == "SKIPPED_BY_USER"
    assert "Superseded" in telegram_job.last_error

    listed = client.get(f"/api/accounts/{account.id}/jobs").json()
    assert listed[0]["next_attempt_at"] is not None


def test_retry_now_rejects_a_processing_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    asyncio.run(_add_job(account.telegram_id, "o/r", 19, status="PROCESSING"))

    response = client.post(
        f"/api/accounts/{account.id}/issues/retry-now",
        json={"repo": "o/r", "number": 19},
    )

    assert response.status_code == 409
    assert "already processing" in response.json()["detail"]


def test_mark_ready_marks_a_draft_pr_ready_and_completes_the_job(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 9, status="NEEDS_TESTS",
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


def test_recheck_pr_resets_a_completed_job_for_current_ci(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    job = asyncio.run(_add_job(
        account.telegram_id, "o/r", 11, status="DONE",
        draft_pr_number=44, draft_pr_url="https://github.com/o/r/pull/44",
        attempts=3, repair_attempts=2, ci_polls=8, head_sha="oldsha",
    ))

    async def fake_get_pr(token, repo, number):
        return {"state": "open", "draft": False, "head": {"sha": "currentsha"}}

    monkeypatch.setattr(gh, "get_pr", fake_get_pr)

    response = client.post(
        f"/api/accounts/{account.id}/issues/recheck",
        json={"repo": "o/r", "number": 11},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"queued": True, "pr_number": 44}

    async def read_job():
        async with AsyncSessionLocal() as db:
            return await db.get(IssueJob, job.id)

    refreshed = asyncio.run(read_job())
    assert refreshed.status == "WAITING_CI"
    assert refreshed.attempts == 0
    assert refreshed.repair_attempts == 0
    assert refreshed.ci_polls == 0
    assert refreshed.head_sha is None
    assert refreshed.last_error == "Dashboard requested a fresh PR and CI recheck"


def test_recheck_pr_rejects_active_or_missing_pr_jobs(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 12, status="WAITING_CI",
        draft_pr_number=45, draft_pr_url="https://github.com/o/r/pull/45",
    ))

    active = client.post(
        f"/api/accounts/{account.id}/issues/recheck",
        json={"repo": "o/r", "number": 12},
    )
    missing = client.post(
        f"/api/accounts/{account.id}/issues/recheck",
        json={"repo": "o/r", "number": 13},
    )

    assert active.status_code == 409
    assert missing.status_code == 404


def test_recheck_pr_rejects_closed_pull_request(monkeypatch):
    admin = _login_admin(monkeypatch)
    account = asyncio.run(_add_dashboard_account(admin.id))
    asyncio.run(_add_job(
        account.telegram_id, "o/r", 14, status="FAILED",
        draft_pr_number=46, draft_pr_url="https://github.com/o/r/pull/46",
    ))

    async def fake_get_pr(token, repo, number):
        return {"state": "closed", "draft": False, "head": {"sha": "sha"}}

    monkeypatch.setattr(gh, "get_pr", fake_get_pr)

    response = client.post(
        f"/api/accounts/{account.id}/issues/recheck",
        json={"repo": "o/r", "number": 14},
    )
    assert response.status_code == 409


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
