"""Web dashboard: per-user login, unrestricted issue triage, and admin control."""
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.models.database import (
    AsyncSessionLocal,
    DASHBOARD_ID_PREFIX,
    IssueJob,
    PortalUser,
    SolverUser,
    is_dashboard_user,
    telegram_ids_sharing_username,
)
from app.services import github as gh
from app.services.coding_agent import CodingAgentError, test_ai_connection
from app.services.crypto import decrypt_token, encrypt_token
from app.services.password import hash_password, verify_password
from app.services.llm_providers import (
    DEFAULT_PROVIDER,
    default_model,
    normalize_provider,
    providers_for_api,
)
from app.services.solver_queue import (
    _reset_job_for_retry,
    enqueue_issue,
    queue_all_issues_for_account,
)

logger = logging.getLogger(__name__)
router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
ACTIVE_STATUSES = {"QUEUED", "PROCESSING", "WAITING_CI"}
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")
AI_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")

LOGIN_ATTEMPTS: dict[str, deque] = defaultdict(deque)
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW = timedelta(minutes=15)

# How long after saving a DeepSeek key an admin can still read it back, so a
# user who lost the key they pasted can recover it instead of reissuing one.
KEY_REVEAL_WINDOW = timedelta(hours=1)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    temporary_password: str


class ResetPasswordRequest(BaseModel):
    temporary_password: str


class AddAccountRequest(BaseModel):
    token: str


class DeepSeekSettingsRequest(BaseModel):
    api_key: str | None = None
    model: str = ""
    clear: bool = False


class AISettingsRequest(BaseModel):
    provider: str = DEFAULT_PROVIDER
    api_key: str | None = None
    model: str = ""
    clear: bool = False


class AIConnectionTestRequest(BaseModel):
    provider: str = DEFAULT_PROVIDER
    api_key: str | None = None
    model: str = ""


class IssueRef(BaseModel):
    repo: str
    number: int
    title: str | None = None
    url: str | None = None


async def require_login(request: Request) -> PortalUser:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    async with AsyncSessionLocal() as db:
        user = await db.get(PortalUser, user_id)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Login required")
    return user


async def require_admin(user: PortalUser = Depends(require_login)) -> PortalUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def current_scope_user(
    request: Request, user: PortalUser = Depends(require_login)
) -> PortalUser:
    """The portal user whose accounts should be shown/acted on.

    That's the impersonation target while an admin is "viewing as" someone,
    otherwise it's just the logged-in user themself.
    """
    view_as_id = request.session.get("view_as_id")
    if view_as_id and user.is_admin:
        async with AsyncSessionLocal() as db:
            target = await db.get(PortalUser, view_as_id)
        if target:
            return target
        request.session.pop("view_as_id", None)
    return user


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(key: str) -> bool:
    now = datetime.now(timezone.utc)
    attempts = LOGIN_ATTEMPTS[key]
    while attempts and attempts[0] < now - LOGIN_WINDOW:
        attempts.popleft()
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def _record_login_failure(key: str) -> None:
    LOGIN_ATTEMPTS[key].append(datetime.now(timezone.utc))


async def _safe_github_call(coro):
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400, detail=f"GitHub error: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub request failed: {exc}") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_repo(repo: str) -> None:
    if not REPO_PATTERN.fullmatch(repo):
        raise HTTPException(status_code=400, detail="Invalid repository name")


async def _get_dashboard_account(db, account_id: int, owner: PortalUser) -> SolverUser:
    account = await db.get(SolverUser, account_id)
    if (
        not account
        or not is_dashboard_user(account)
        or account.owner_portal_user_id != owner.id
    ):
        raise HTTPException(status_code=404, detail="Account not found")
    return account


async def _get_manageable_user(db, user_id: int) -> PortalUser:
    user = await db.get(PortalUser, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _account_summary(account: SolverUser) -> dict:
    return {
        "id": account.id,
        "github_username": account.github_username,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


def _key_reveal_expires_at(user: PortalUser) -> datetime | None:
    """When this user's saved DeepSeek key stops being readable by an admin."""
    if not user.deepseek_api_key_encrypted or not user.deepseek_key_saved_at:
        return None
    return user.deepseek_key_saved_at + KEY_REVEAL_WINDOW


def _key_is_revealable(user: PortalUser) -> bool:
    expires_at = _key_reveal_expires_at(user)
    return bool(expires_at and expires_at > datetime.utcnow())


def _ai_settings_response(user: PortalUser) -> dict:
    provider = user.ai_provider or DEFAULT_PROVIDER
    return {
        "connected": bool(user.deepseek_api_key_encrypted),
        "provider": provider,
        "model": user.deepseek_model or default_model(provider),
    }


def _user_summary(user: PortalUser) -> dict:
    expires_at = _key_reveal_expires_at(user)
    revealable = _key_is_revealable(user)
    provider = user.ai_provider or DEFAULT_PROVIDER
    return {
        "id": user.id,
        "username": user.username,
        "must_change_password": user.must_change_password,
        "ai_connected": bool(user.deepseek_api_key_encrypted),
        "ai_provider": provider,
        "ai_model": user.deepseek_model or default_model(provider),
        "deepseek_connected": bool(user.deepseek_api_key_encrypted),
        "deepseek_key_revealable": revealable,
        "deepseek_key_reveal_expires_at": (
            expires_at.isoformat() if expires_at and revealable else None
        ),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


JOB_STATUS_PRIORITY = {
    # A completed record is authoritative when older dashboard/Telegram rows
    # exist for the same GitHub issue. Active work wins over stale failures.
    "DONE": 100,
    "PROCESSING": 90,
    "WAITING_CI": 80,
    "QUEUED": 70,
    "NEEDS_REVIEW": 60,
    "NEEDS_API_KEY": 55,
    "NEEDS_TESTS": 50,
    "FAILED": 40,
    "SKIPPED_BY_USER": 20,
    "SKIPPED": 10,
}


def _job_sort_key(job: IssueJob) -> tuple[int, float, int]:
    changed_at = job.updated_at or job.created_at
    timestamp = changed_at.timestamp() if changed_at else 0.0
    return JOB_STATUS_PRIORITY.get(job.status, 0), timestamp, job.id or 0


def _coalesce_jobs(jobs: list[IssueJob]) -> list[IssueJob]:
    """Return one authoritative shared job per GitHub issue.

    Older deployments could create one row through Telegram and another
    through the dashboard. Keeping both in the response made a completed
    Telegram job look unfinished in the dashboard.
    """
    by_issue: dict[tuple[str, int], IssueJob] = {}
    for job in jobs:
        key = (job.repo_full_name.lower(), job.issue_number)
        current = by_issue.get(key)
        if current is None or _job_sort_key(job) > _job_sort_key(current):
            by_issue[key] = job
    return sorted(
        by_issue.values(),
        key=lambda job: job.updated_at or job.created_at or datetime.min,
        reverse=True,
    )


def _job_pr_url(job: IssueJob | None) -> str | None:
    if not job:
        return None
    if job.draft_pr_url:
        return job.draft_pr_url
    if job.draft_pr_number:
        return f"https://github.com/{job.repo_full_name}/pull/{job.draft_pr_number}"
    return None


def _display_status(job: IssueJob | None) -> str:
    if not job:
        return "NOT_QUEUED"
    # A CI state without a tracked PR is inconsistent legacy data. Do not tell
    # the operator that CI is missing when there is no PR on which CI could run.
    if job.status in {"WAITING_CI", "NEEDS_TESTS"} and not _job_pr_url(job):
        return "NEEDS_REVIEW"
    return job.status


def _issue_summary(issue: dict, jobs_by_key: dict[tuple[str, int], IssueJob]) -> dict:
    repo = gh.repo_from_issue(issue)
    number = issue.get("number")
    job = jobs_by_key.get((repo.lower(), number)) if repo and number else None
    return {
        "repo": repo,
        "number": number,
        "title": issue.get("title"),
        "url": issue.get("html_url"),
        "labels": [
            item.get("name") if isinstance(item, dict) else str(item)
            for item in issue.get("labels", [])
        ],
        "status": _display_status(job),
        "pr_url": _job_pr_url(job),
    }


def _job_summary(job: IssueJob) -> dict:
    return {
        "repo": job.repo_full_name,
        "number": job.issue_number,
        "title": job.issue_title,
        "issue_url": job.issue_url,
        "status": _display_status(job),
        "pr_url": _job_pr_url(job),
        "last_error": job.last_error,
        "next_attempt_at": (
            job.next_attempt_at.isoformat() if job.next_attempt_at else None
        ),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


# --- Pages -------------------------------------------------------------

@router.get("/dashboard")
async def dashboard_page(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/dashboard/login")
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/dashboard/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return FileResponse(STATIC_DIR / "login.html")


@router.get("/dashboard/admin")
async def admin_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/dashboard/login")
    async with AsyncSessionLocal() as db:
        user = await db.get(PortalUser, user_id)
    if not user or not user.is_admin:
        return RedirectResponse("/dashboard")
    return FileResponse(STATIC_DIR / "admin.html")


# --- Auth ----------------------------------------------------------------

@router.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    key = _client_key(request)
    if _rate_limited(key):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    username = payload.username.strip().lower()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PortalUser).where(PortalUser.username == username))
        user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_login_failure(key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    LOGIN_ATTEMPTS.pop(key, None)
    request.session.clear()
    request.session["user_id"] = user.id
    return {"must_change_password": user.must_change_password, "is_admin": user.is_admin}


@router.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"loggedOut": True}


@router.post("/api/change-password")
async def change_password(
    payload: ChangePasswordRequest, user: PortalUser = Depends(require_login)
):
    if len(payload.new_password) < 10:
        raise HTTPException(
            status_code=400, detail="New password must contain at least 10 characters"
        )
    async with AsyncSessionLocal() as db:
        fresh = await db.get(PortalUser, user.id)
        if not verify_password(payload.current_password, fresh.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        fresh.password_hash = hash_password(payload.new_password)
        fresh.must_change_password = False
        await db.commit()
    return {"changed": True}


@router.get("/api/providers")
async def list_providers(_: PortalUser = Depends(require_login)):
    return {"providers": providers_for_api()}


@router.get("/api/settings/ai")
async def get_ai_settings(user: PortalUser = Depends(require_login)):
    return _ai_settings_response(user)


@router.post("/api/settings/ai/test")
async def test_saved_ai_connection(
    payload: AIConnectionTestRequest,
    user: PortalUser = Depends(require_login),
):
    try:
        provider = normalize_provider(payload.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    model = payload.model.strip() or default_model(provider)
    if not AI_MODEL_PATTERN.fullmatch(model):
        raise HTTPException(status_code=400, detail="Invalid model name")

    api_key = (payload.api_key or "").strip()
    saved_provider = user.ai_provider or DEFAULT_PROVIDER
    if not api_key and provider == saved_provider and user.deepseek_api_key_encrypted:
        try:
            api_key = decrypt_token(user.deepseek_api_key_encrypted)
        except (InvalidToken, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail="Saved API key could not be read") from exc
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"Enter or save a {provider} API key before testing",
        )
    if len(api_key) < 10 or len(api_key) > 512:
        raise HTTPException(status_code=400, detail="Invalid API key")

    try:
        result = await test_ai_connection(provider, api_key, model)
    except CodingAgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connected": True, **result}


@router.post("/api/settings/ai")
async def save_ai_settings(
    payload: AISettingsRequest, user: PortalUser = Depends(require_login)
):
    try:
        provider = normalize_provider(payload.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model = payload.model.strip() or default_model(provider)
    if not AI_MODEL_PATTERN.fullmatch(model):
        raise HTTPException(status_code=400, detail="Invalid model name")
    api_key = (payload.api_key or "").strip()
    if api_key and (len(api_key) < 10 or len(api_key) > 512):
        raise HTTPException(status_code=400, detail="Invalid API key")

    async with AsyncSessionLocal() as db:
        fresh = await db.get(PortalUser, user.id)
        if payload.clear:
            fresh.deepseek_api_key_encrypted = ""
            fresh.deepseek_key_saved_at = None
        elif api_key:
            fresh.deepseek_api_key_encrypted = encrypt_token(api_key)
            # Restart the admin read-back window on every new key, so an
            # older key never stays readable after being replaced.
            fresh.deepseek_key_saved_at = datetime.utcnow()
        fresh.ai_provider = provider
        fresh.deepseek_model = model

        # Jobs stopped only because this user had no key can continue as soon
        # as their own key is saved. Other users' jobs are never touched.
        if fresh.deepseek_api_key_encrypted:
            account_ids = await db.execute(
                select(SolverUser.telegram_id).where(
                    SolverUser.owner_portal_user_id == fresh.id
                )
            )
            telegram_ids = [row[0] for row in account_ids.all()]
            if telegram_ids:
                jobs = await db.execute(
                    select(IssueJob).where(
                        IssueJob.telegram_id.in_(telegram_ids),
                        IssueJob.status == "NEEDS_API_KEY",
                    )
                )
                for job in jobs.scalars():
                    job.status = "WAITING_CI" if job.draft_pr_number else "QUEUED"
                    job.attempts = 0
                    job.next_attempt_at = datetime.utcnow()
                    job.last_error = "User AI key saved; solver resumed"
        await db.commit()
    return _ai_settings_response(fresh)


@router.get("/api/settings/deepseek")
async def get_deepseek_settings(user: PortalUser = Depends(require_login)):
    return _ai_settings_response(user)


@router.post("/api/settings/deepseek")
async def save_deepseek_settings(
    payload: DeepSeekSettingsRequest, user: PortalUser = Depends(require_login)
):
    return await save_ai_settings(
        AISettingsRequest(
            provider=DEFAULT_PROVIDER,
            api_key=payload.api_key,
            model=payload.model,
            clear=payload.clear,
        ),
        user,
    )


@router.get("/api/me")
async def me(request: Request, user: PortalUser = Depends(require_login)):
    impersonating = None
    view_as_id = request.session.get("view_as_id")
    if view_as_id and user.is_admin:
        async with AsyncSessionLocal() as db:
            target = await db.get(PortalUser, view_as_id)
        if target:
            impersonating = {"id": target.id, "username": target.username}
        else:
            request.session.pop("view_as_id", None)
    return {
        "username": user.username,
        "is_admin": user.is_admin,
        "must_change_password": user.must_change_password,
        "ai_connected": bool(user.deepseek_api_key_encrypted),
        "ai_provider": user.ai_provider or DEFAULT_PROVIDER,
        "ai_model": user.deepseek_model or default_model(user.ai_provider),
        "deepseek_connected": bool(user.deepseek_api_key_encrypted),
        "deepseek_model": user.deepseek_model or default_model(user.ai_provider),
        "impersonating": impersonating,
    }


# --- Admin: manage people --------------------------------------------------

@router.get("/api/admin/users")
async def list_users(_: PortalUser = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PortalUser).where(PortalUser.is_admin.is_(False)).order_by(PortalUser.username)
        )
        users = result.scalars().all()
    return [_user_summary(user) for user in users]


@router.post("/api/admin/users")
async def create_user(payload: CreateUserRequest, _: PortalUser = Depends(require_admin)):
    username = payload.username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-40 letters, numbers, dots, underscores, or hyphens",
        )
    if len(payload.temporary_password) < 10:
        raise HTTPException(
            status_code=400, detail="Temporary password must contain at least 10 characters"
        )
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PortalUser).where(PortalUser.username == username))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="That username already exists")
        user = PortalUser(
            username=username,
            password_hash=hash_password(payload.temporary_password),
            is_admin=False,
            must_change_password=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return _user_summary(user)


@router.post("/api/admin/users/{user_id}/reset-password")
async def reset_password(
    user_id: int, payload: ResetPasswordRequest, _: PortalUser = Depends(require_admin)
):
    if len(payload.temporary_password) < 10:
        raise HTTPException(
            status_code=400, detail="Temporary password must contain at least 10 characters"
        )
    async with AsyncSessionLocal() as db:
        user = await _get_manageable_user(db, user_id)
        user.password_hash = hash_password(payload.temporary_password)
        user.must_change_password = True
        await db.commit()
    return {"reset": True}


@router.get("/api/admin/users/{user_id}/ai-key")
async def reveal_ai_key(user_id: int, admin: PortalUser = Depends(require_admin)):
    """Read back a user's AI key within the short window after they saved it.

    Recovery aid for someone who pasted a key and no longer has a copy; the
    window closes on its own so a stored key isn't indefinitely readable.
    """
    async with AsyncSessionLocal() as db:
        user = await _get_manageable_user(db, user_id)
        if not user.deepseek_api_key_encrypted:
            raise HTTPException(status_code=404, detail="This user has no saved AI key")
        expires_at = _key_reveal_expires_at(user)
        if not _key_is_revealable(user):
            raise HTTPException(
                status_code=410,
                detail=(
                    "The recovery window for this key has closed. Ask the user to save "
                    "a new key if they need it back."
                ),
            )
        api_key = decrypt_token(user.deepseek_api_key_encrypted)

    logger.info(
        "Admin %s revealed the AI key for user %s", admin.username, user.username
    )
    return {
        "username": user.username,
        "provider": user.ai_provider or DEFAULT_PROVIDER,
        "api_key": api_key,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.get("/api/admin/users/{user_id}/deepseek-key")
async def reveal_deepseek_key(user_id: int, admin: PortalUser = Depends(require_admin)):
    return await reveal_ai_key(user_id, admin)


@router.delete("/api/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request, _: PortalUser = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        user = await _get_manageable_user(db, user_id)
        accounts_result = await db.execute(
            select(SolverUser.telegram_id).where(SolverUser.owner_portal_user_id == user.id)
        )
        telegram_ids = [row[0] for row in accounts_result.all()]
        for telegram_id in telegram_ids:
            await db.execute(delete(IssueJob).where(IssueJob.telegram_id == telegram_id))
        await db.execute(delete(SolverUser).where(SolverUser.owner_portal_user_id == user.id))
        await db.delete(user)
        await db.commit()
    if request.session.get("view_as_id") == user_id:
        request.session.pop("view_as_id", None)
    return {"deleted": True}


@router.post("/api/admin/users/{user_id}/view-as")
async def view_as(user_id: int, request: Request, _: PortalUser = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        user = await _get_manageable_user(db, user_id)
    request.session["view_as_id"] = user.id
    return {"viewing_as": user.username}


@router.post("/api/admin/exit-view-as")
async def exit_view_as(request: Request, _: PortalUser = Depends(require_login)):
    request.session.pop("view_as_id", None)
    return {"exited": True}


# --- Accounts and issues (scoped to the current/impersonated user) --------

@router.get("/api/accounts")
async def list_accounts(scope_user: PortalUser = Depends(current_scope_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(
                SolverUser.telegram_id.startswith(DASHBOARD_ID_PREFIX),
                SolverUser.owner_portal_user_id == scope_user.id,
            )
        )
        accounts = result.scalars().all()
    return [_account_summary(account) for account in accounts]


@router.post("/api/accounts")
async def add_account(
    payload: AddAccountRequest, scope_user: PortalUser = Depends(current_scope_user)
):
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="A GitHub token is required")
    username = await _safe_github_call(gh.validate_token(token))
    if not username:
        raise HTTPException(status_code=400, detail="GitHub rejected that token")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(
                SolverUser.telegram_id.startswith(DASHBOARD_ID_PREFIX),
                SolverUser.owner_portal_user_id == scope_user.id,
                func.lower(SolverUser.github_username) == username.lower(),
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"@{username} is already added")
        account = SolverUser(
            telegram_id=f"{DASHBOARD_ID_PREFIX}{uuid4()}",
            github_username=username,
            github_token_encrypted=encrypt_token(token),
            owner_portal_user_id=scope_user.id,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
    return _account_summary(account)


@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, scope_user: PortalUser = Depends(current_scope_user)):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        active = await db.execute(
            select(IssueJob.id)
            .where(
                IssueJob.telegram_id == account.telegram_id,
                IssueJob.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active.scalar_one_or_none():
            raise HTTPException(
                status_code=409, detail="This account has active jobs; wait for them to finish first"
            )
        await db.execute(delete(IssueJob).where(IssueJob.telegram_id == account.telegram_id))
        await db.delete(account)
        await db.commit()
    return {"deleted": True}


@router.get("/api/accounts/{account_id}/issues")
async def list_issues(account_id: int, scope_user: PortalUser = Depends(current_scope_user)):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        token = decrypt_token(account.github_token_encrypted)
        issues = await _safe_github_call(gh.search_all_assigned_issues(token, account.github_username))
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        jobs_result = await db.execute(
            select(IssueJob).where(IssueJob.telegram_id.in_(sibling_ids))
        )
        shared_jobs = _coalesce_jobs(list(jobs_result.scalars()))
        jobs_by_key = {
            (job.repo_full_name.lower(), job.issue_number): job for job in shared_jobs
        }
    return [_issue_summary(issue, jobs_by_key) for issue in issues]


@router.post("/api/accounts/{account_id}/issues/fix")
async def fix_issue(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
    token = decrypt_token(account.github_token_encrypted)
    issue = await _safe_github_call(gh.get_issue(token, payload.repo, payload.number))
    if not gh.is_open_and_assigned(issue, account.github_username):
        raise HTTPException(
            status_code=409, detail="Issue is no longer open and assigned to this account"
        )
    queued = await enqueue_issue(account, issue)
    return {"queued": queued}


@router.post("/api/accounts/{account_id}/issues/retry-now")
async def retry_issue_now(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    """Move a delayed queued job to the front of the due queue immediately."""
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        result = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id.in_(sibling_ids),
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
            )
        )
        shared_jobs = _coalesce_jobs(list(result.scalars()))
        job = shared_jobs[0] if shared_jobs else None
        if not job:
            raise HTTPException(status_code=404, detail="Solver job not found")
        if job.status == "PROCESSING":
            raise HTTPException(status_code=409, detail="This issue is already processing")
        if job.status not in {"QUEUED", "WAITING_CI"}:
            raise HTTPException(
                status_code=409,
                detail=f"Retry now is only available for queued jobs, not {job.status}",
            )

        job.next_attempt_at = datetime.utcnow()
        job.attempts = 0
        job.last_error = "Manual Retry now requested from the dashboard"
        await db.commit()
        status = job.status
    return {"queued": True, "status": status}


@router.post("/api/accounts/{account_id}/issues/mark-ready")
async def mark_issue_ready(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    """Force a draft PR ready for review without waiting on CI."""
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        job_result = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id.in_(sibling_ids),
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
            )
        )
        shared_jobs = _coalesce_jobs(list(job_result.scalars()))
        job = shared_jobs[0] if shared_jobs else None
        if not job or not job.draft_pr_number:
            raise HTTPException(status_code=404, detail="No draft PR is tracked for this issue")

        token = decrypt_token(account.github_token_encrypted)
        pull_request = await _safe_github_call(gh.get_pr(token, payload.repo, job.draft_pr_number))
        if pull_request.get("state") != "open":
            raise HTTPException(status_code=409, detail="That PR is no longer open")

        if pull_request.get("draft", False):
            await _safe_github_call(gh.mark_pr_ready(token, pull_request["node_id"]))

        job.status = "DONE"
        job.head_sha = pull_request["head"]["sha"]
        job.last_error = "Manually marked ready for review by operator"
        await db.commit()
    return {"ready": True}


@router.post("/api/accounts/{account_id}/issues/recheck")
async def recheck_issue_pr(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    """Recheck an existing solver PR and repair it if its current CI fails."""
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        job_result = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id.in_(sibling_ids),
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
            )
        )
        shared_jobs = _coalesce_jobs(list(job_result.scalars()))
        job = shared_jobs[0] if shared_jobs else None
        if not job or not job.draft_pr_number:
            raise HTTPException(status_code=404, detail="No solver PR is tracked for this issue")
        if job.status in ACTIVE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="This PR is already being checked by the solver",
            )

        token = decrypt_token(account.github_token_encrypted)
        pull_request = await _safe_github_call(
            gh.get_pr(token, payload.repo, job.draft_pr_number)
        )
        if pull_request.get("state") != "open":
            raise HTTPException(status_code=409, detail="That PR is no longer open")

        _reset_job_for_retry(
            job,
            reason="Dashboard requested a fresh PR and CI recheck",
        )
        await db.commit()
        pr_number = job.draft_pr_number
    return {"queued": True, "pr_number": pr_number}


@router.post("/api/accounts/{account_id}/issues/fix-all")
async def fix_all_issues(account_id: int, scope_user: PortalUser = Depends(current_scope_user)):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
    discovered, queued = await queue_all_issues_for_account(account)
    return {"discovered": discovered, "queued": queued}


@router.post("/api/accounts/{account_id}/issues/skip")
async def skip_issue(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        existing = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id.in_(sibling_ids),
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
            )
        )
        job = existing.scalars().first()
        if job:
            if job.status != "SKIPPED_BY_USER":
                raise HTTPException(
                    status_code=409, detail=f"Issue already has status {job.status}"
                )
            return {"skipped": True}
        db.add(IssueJob(
            telegram_id=account.telegram_id,
            repo_full_name=payload.repo,
            issue_number=payload.number,
            issue_title=(payload.title or f"Issue #{payload.number}")[:500],
            issue_url=payload.url or f"https://github.com/{payload.repo}/issues/{payload.number}",
            status="SKIPPED_BY_USER",
        ))
        await db.commit()
    return {"skipped": True}


@router.post("/api/accounts/{account_id}/issues/unskip")
async def unskip_issue(
    account_id: int, payload: IssueRef, scope_user: PortalUser = Depends(current_scope_user)
):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        await db.execute(
            delete(IssueJob).where(
                IssueJob.telegram_id == account.telegram_id,
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
                IssueJob.status == "SKIPPED_BY_USER",
            )
        )
        await db.commit()
    return {"skipped": False}


@router.get("/api/accounts/{account_id}/jobs")
async def list_jobs(account_id: int, scope_user: PortalUser = Depends(current_scope_user)):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id, scope_user)
        sibling_ids = await telegram_ids_sharing_username(db, account.github_username)
        result = await db.execute(
            select(IssueJob)
            .where(IssueJob.telegram_id.in_(sibling_ids))
            .order_by(IssueJob.updated_at.desc())
        )
        jobs = _coalesce_jobs(list(result.scalars()))
    return [_job_summary(job) for job in jobs]
