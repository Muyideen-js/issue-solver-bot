"""Web dashboard: unrestricted per-account issue triage alongside Telegram."""
import logging
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.models.database import (
    AsyncSessionLocal,
    DASHBOARD_ID_PREFIX,
    IssueJob,
    SolverUser,
    is_dashboard_user,
)
from app.services import github as gh
from app.services.crypto import decrypt_token, encrypt_token
from app.services.solver_queue import enqueue_issue, queue_all_issues_for_account

logger = logging.getLogger(__name__)
router = APIRouter()
security = HTTPBasic()

STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
ACTIVE_STATUSES = {"QUEUED", "PROCESSING", "WAITING_CI"}
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class AddAccountRequest(BaseModel):
    token: str


class IssueRef(BaseModel):
    repo: str
    number: int
    title: str | None = None
    url: str | None = None


def require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    configured_password = os.getenv("DASHBOARD_PASSWORD")
    if not configured_password:
        raise HTTPException(status_code=503, detail="Dashboard is not configured")
    expected_username = os.getenv("DASHBOARD_USERNAME", "admin")
    valid_username = secrets.compare_digest(credentials.username, expected_username)
    valid_password = secrets.compare_digest(credentials.password, configured_password)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"}
        )


async def _safe_github_call(coro):
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400, detail=f"GitHub error: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_repo(repo: str) -> None:
    if not REPO_PATTERN.fullmatch(repo):
        raise HTTPException(status_code=400, detail="Invalid repository name")


async def _get_dashboard_account(db, account_id: int) -> SolverUser:
    account = await db.get(SolverUser, account_id)
    if not account or not is_dashboard_user(account):
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _account_summary(account: SolverUser) -> dict:
    return {
        "id": account.id,
        "github_username": account.github_username,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


def _issue_summary(issue: dict, jobs_by_key: dict[tuple[str, int], IssueJob]) -> dict:
    repo = gh.repo_from_issue(issue)
    number = issue.get("number")
    job = jobs_by_key.get((repo, number)) if repo and number else None
    return {
        "repo": repo,
        "number": number,
        "title": issue.get("title"),
        "url": issue.get("html_url"),
        "labels": [
            item.get("name") if isinstance(item, dict) else str(item)
            for item in issue.get("labels", [])
        ],
        "status": job.status if job else "NOT_QUEUED",
        "pr_url": job.draft_pr_url if job else None,
    }


def _job_summary(job: IssueJob) -> dict:
    return {
        "repo": job.repo_full_name,
        "number": job.issue_number,
        "title": job.issue_title,
        "issue_url": job.issue_url,
        "status": job.status,
        "pr_url": job.draft_pr_url,
        "last_error": job.last_error,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.get("/dashboard", dependencies=[Depends(require_dashboard_auth)])
async def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/api/accounts", dependencies=[Depends(require_dashboard_auth)])
async def list_accounts():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(SolverUser.telegram_id.startswith(DASHBOARD_ID_PREFIX))
        )
        accounts = result.scalars().all()
    return [_account_summary(account) for account in accounts]


@router.post("/api/accounts", dependencies=[Depends(require_dashboard_auth)])
async def add_account(payload: AddAccountRequest):
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
                func.lower(SolverUser.github_username) == username.lower(),
            )
        )
        if result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"@{username} is already added")
        account = SolverUser(
            telegram_id=f"{DASHBOARD_ID_PREFIX}{uuid4()}",
            github_username=username,
            github_token_encrypted=encrypt_token(token),
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
    return _account_summary(account)


@router.delete("/api/accounts/{account_id}", dependencies=[Depends(require_dashboard_auth)])
async def delete_account(account_id: int):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
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


@router.get("/api/accounts/{account_id}/issues", dependencies=[Depends(require_dashboard_auth)])
async def list_issues(account_id: int):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
        token = decrypt_token(account.github_token_encrypted)
        issues = await _safe_github_call(gh.search_all_assigned_issues(token, account.github_username))
        jobs_result = await db.execute(
            select(IssueJob).where(IssueJob.telegram_id == account.telegram_id)
        )
        jobs_by_key = {
            (job.repo_full_name, job.issue_number): job for job in jobs_result.scalars()
        }
    return [_issue_summary(issue, jobs_by_key) for issue in issues]


@router.post("/api/accounts/{account_id}/issues/fix", dependencies=[Depends(require_dashboard_auth)])
async def fix_issue(account_id: int, payload: IssueRef):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
    token = decrypt_token(account.github_token_encrypted)
    issue = await _safe_github_call(gh.get_issue(token, payload.repo, payload.number))
    if not gh.is_open_and_assigned(issue, account.github_username):
        raise HTTPException(
            status_code=409, detail="Issue is no longer open and assigned to this account"
        )
    queued = await enqueue_issue(account, issue)
    return {"queued": queued}


@router.post("/api/accounts/{account_id}/issues/fix-all", dependencies=[Depends(require_dashboard_auth)])
async def fix_all_issues(account_id: int):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
    discovered, queued = await queue_all_issues_for_account(account)
    return {"discovered": discovered, "queued": queued}


@router.post("/api/accounts/{account_id}/issues/skip", dependencies=[Depends(require_dashboard_auth)])
async def skip_issue(account_id: int, payload: IssueRef):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
        existing = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id == account.telegram_id,
                IssueJob.repo_full_name == payload.repo,
                IssueJob.issue_number == payload.number,
            )
        )
        job = existing.scalar_one_or_none()
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


@router.post("/api/accounts/{account_id}/issues/unskip", dependencies=[Depends(require_dashboard_auth)])
async def unskip_issue(account_id: int, payload: IssueRef):
    _validate_repo(payload.repo)
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
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


@router.get("/api/accounts/{account_id}/jobs", dependencies=[Depends(require_dashboard_auth)])
async def list_jobs(account_id: int):
    async with AsyncSessionLocal() as db:
        account = await _get_dashboard_account(db, account_id)
        result = await db.execute(
            select(IssueJob)
            .where(IssueJob.telegram_id == account.telegram_id)
            .order_by(IssueJob.updated_at.desc())
        )
        jobs = result.scalars().all()
    return [_job_summary(job) for job in jobs]
