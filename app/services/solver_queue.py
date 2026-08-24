"""Durable assignment discovery and issue-solving worker."""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError

from app.models.database import AsyncSessionLocal, IssueJob, SolverUser, is_dashboard_user
from app.services import github as gh
from app.services.coding_agent import CodingAgentError, solve_issue
from app.services.crypto import decrypt_token
from app.services.notifications import notify
from app.services.workspace import SolverWorkspace, WorkspaceError

logger = logging.getLogger(__name__)


async def enqueue_issue(user: SolverUser, issue: dict) -> bool:
    repo = _repo_from_issue(issue)
    number = issue.get("number")
    if not repo or not number:
        return False
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id == user.telegram_id,
                IssueJob.repo_full_name == repo,
                IssueJob.issue_number == int(number),
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.status == "FAILED":
                _reset_job_for_retry(existing, reason="Failed issue requeued")
                await db.commit()
                return True
            return False
        db.add(IssueJob(
            telegram_id=user.telegram_id,
            repo_full_name=repo,
            issue_number=int(number),
            issue_title=(issue.get("title") or f"Issue #{number}")[:500],
            issue_url=issue.get("html_url") or f"https://github.com/{repo}/issues/{number}",
        ))
        try:
            await db.commit()
            return True
        except IntegrityError:
            await db.rollback()
            return False


async def discover_for_user(user: SolverUser) -> tuple[int, int]:
    token = decrypt_token(user.github_token_encrypted)
    issues = await gh.search_assigned_program_issues(token, user.github_username)
    queued = 0
    for issue in issues:
        queued += int(await enqueue_issue(user, issue))
    return len(issues), queued


async def queue_all_issues_for_account(user: SolverUser) -> tuple[int, int]:
    """Dashboard "Fix all": queue every open assigned issue, ignoring program labels."""
    token = decrypt_token(user.github_token_encrypted)
    issues = await gh.search_all_assigned_issues(token, user.github_username)
    queued = 0
    for issue in issues:
        queued += int(await enqueue_issue(user, issue))
    return len(issues), queued


async def retry_draft_pr(telegram_id: str, pr_number: int) -> str:
    """Reset a stopped solver PR so its current head and CI can be checked again."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob).where(
                IssueJob.telegram_id == telegram_id,
                IssueJob.draft_pr_number == pr_number,
            )
        )
        jobs = result.scalars().all()
        if not jobs:
            return f"I cannot find solver PR #{pr_number}."
        if len(jobs) > 1:
            return f"More than one repository has solver PR #{pr_number}; retry by issue URL."
        job = jobs[0]
        if job.status in {"QUEUED", "WAITING_CI", "PROCESSING"}:
            return f"PR #{pr_number} is already active ({job.status})."
        if job.status == "DONE":
            user_result = await db.execute(
                select(SolverUser).where(SolverUser.telegram_id == telegram_id)
            )
            user = user_result.scalar_one_or_none()
            if not user:
                return "Run /setup again before retrying this PR."
            token = decrypt_token(user.github_token_encrypted)
            pull_request = await gh.get_pr(token, job.repo_full_name, pr_number)
            if pull_request.get("state") != "open":
                return f"PR #{pr_number} is no longer open."
            current_sha = pull_request["head"]["sha"]
            ci_status = await gh.get_ci_status(token, job.repo_full_name, current_sha)
            if not _completed_pr_needs_retry(job.head_sha, current_sha, ci_status):
                return f"PR #{pr_number} already passed CI and is complete."
            reason = (
                "Manual retry requested after the PR head changed"
                if current_sha != job.head_sha
                else f"Manual retry requested because current CI is {ci_status}"
            )
            _reset_job_for_retry(job, reason=reason)
            await db.commit()
            return (
                f"PR #{pr_number} changed after completion or no longer passes CI; "
                "queued a fresh CI check with repair counters reset."
            )
        if job.status not in {"FAILED", "NEEDS_TESTS"}:
            return f"PR #{pr_number} stopped as {job.status}; it was not reset automatically."
        _reset_job_for_retry(job)
        await db.commit()
        return (
            f"PR #{pr_number} queued for a fresh CI check with automatic repair counters reset."
        )


async def assignment_poller(stop_event: asyncio.Event) -> None:
    interval = max(60, int(os.getenv("ASSIGNMENT_POLL_SECONDS", "300")))
    while not stop_event.is_set():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SolverUser).where(
                    SolverUser.auto_solve.is_(True), SolverUser.paused.is_(False)
                )
            )
            users = result.scalars().all()
        for user in users:
            try:
                discovered, queued = await discover_for_user(user)
                if queued:
                    await notify(
                        user.telegram_id,
                        f"Found {discovered} assigned issue(s); queued {queued} new job(s).",
                    )
            except Exception:
                logger.exception("Assignment discovery failed for %s", user.github_username)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def solver_worker(stop_event: asyncio.Event) -> None:
    await _recover_interrupted_jobs()
    while not stop_event.is_set():
        job_id = await _claim_next_job()
        if job_id is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            continue
        await _process_job(job_id)


async def _claim_next_job() -> int | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob)
            .where(
                IssueJob.status.in_(["QUEUED", "WAITING_CI"]),
                IssueJob.next_attempt_at <= datetime.utcnow(),
            )
            .order_by(
                case((IssueJob.draft_pr_number.is_not(None), 0), else_=1),
                IssueJob.next_attempt_at,
                IssueJob.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if not job:
            return None
        job.status = "PROCESSING"
        await db.commit()
        return job.id


async def _process_job(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(IssueJob, job_id)
        if not job:
            return
        user_result = await db.execute(
            select(SolverUser).where(SolverUser.telegram_id == job.telegram_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await _reschedule(job, db, 300, "Solver user is missing")
            return
        # Pause blocks new issues, but existing drafts must still be monitored and repaired.
        if user.paused and not job.draft_pr_number:
            await _reschedule(job, db, 300, "New issue solving is paused")
            return
        token = decrypt_token(user.github_token_encrypted)
        try:
            issue = await gh.get_issue(token, job.repo_full_name, job.issue_number)
            label_gated = not is_dashboard_user(user)
            if not gh.is_open_and_assigned(issue, user.github_username) or (
                label_gated and not gh.is_program_issue(issue)
            ):
                await _finish(
                    job, db, "SKIPPED", "Issue is no longer assigned or labeled for a supported program"
                )
                return
            if job.draft_pr_number:
                await _process_existing_draft(job, user, token, issue, db)
            else:
                await _implement_issue(job, user, token, issue, db)
        except Exception as exc:
            logger.exception("Solver job %s failed", job.id)
            await _retry_or_fail(job, db, str(exc))
            if job.status == "FAILED":
                await notify(
                    user.telegram_id,
                    f"Issue solver failed for {job.issue_url}:\n{str(exc)[:1000]}",
                )
            else:
                await notify(
                    user.telegram_id,
                    f"Attempt {job.attempts} failed for {job.issue_url}. It remains queued "
                    f"for retry.\n{str(exc)[:700]}",
                )


async def _implement_issue(job, user, token: str, issue: dict, db) -> None:
    await notify(
        user.telegram_id,
        f"Starting {job.repo_full_name} issue #{job.issue_number}: {job.issue_title}",
    )
    repository = await gh.get_repository(token, job.repo_full_name)
    base_branch = repository["default_branch"]
    fork = await gh.ensure_personal_fork(
        token, user.github_username, job.repo_full_name, repository["name"]
    )
    branch = job.branch_name or f"solver/issue-{job.issue_number}-{job.id}-a{job.attempts}"
    job.branch_name = branch
    await db.commit()

    if job.head_sha and job.result_summary:
        result = json.loads(job.result_summary)
        existing_pr = await gh.find_open_pr_by_head(
            token, job.repo_full_name, f"{user.github_username}:{branch}"
        )
        if existing_pr:
            await _attach_draft(job, existing_pr, db)
            return
    else:
        async with SolverWorkspace(token) as workspace:
            await workspace.clone(repository["clone_url"], base_branch, branch)
            result = await solve_issue(
                workspace,
                job.repo_full_name,
                job.issue_number,
                issue.get("title", ""),
                issue.get("body") or "",
            )
            head_sha = await workspace.commit_and_push(
                fork["clone_url"], branch, f"fix: resolve issue #{job.issue_number}"
            )
        job.head_sha = head_sha
        job.result_summary = json.dumps(result)
        await db.commit()

    pr_body = _pr_body(job, result)
    pull_request = await gh.create_draft_pr(
        token=token,
        repo=job.repo_full_name,
        head=f"{user.github_username}:{branch}",
        base=base_branch,
        title=_pr_title(issue.get("title", job.issue_title)),
        body=pr_body,
    )
    await _attach_draft(job, pull_request, db)
    await notify(
        user.telegram_id,
        f"Draft PR created for issue #{job.issue_number}. Waiting for CI:\n"
        f"{job.draft_pr_url}",
    )


async def _process_existing_draft(job, user, token: str, issue: dict, db) -> None:
    pull_request = await gh.get_pr(token, job.repo_full_name, job.draft_pr_number)
    if pull_request.get("state") != "open":
        await _finish(job, db, "SKIPPED", "Draft PR is no longer open")
        return
    current_sha = pull_request["head"]["sha"]
    if job.head_sha and current_sha != job.head_sha:
        await _finish(job, db, "NEEDS_REVIEW", "Solver branch changed outside this job")
        await notify(
            user.telegram_id,
            f"The solver branch changed externally, so automation stopped safely:\n"
            f"{job.draft_pr_url}",
        )
        return
    job.head_sha = current_sha
    ci_status = await gh.get_ci_status(token, job.repo_full_name, current_sha)
    if ci_status == "pending":
        job.ci_polls += 1
        await _reschedule(job, db, 60, "WAITING_CI")
        return
    if ci_status == "none":
        job.ci_polls += 1
        if job.ci_polls < 10:
            await _reschedule(job, db, 60, "WAITING_FOR_CI_TO_APPEAR")
        else:
            await _finish(job, db, "NEEDS_TESTS", "No CI checks appeared for the draft PR")
            await notify(
                user.telegram_id,
                f"Draft PR has no CI checks and remains draft:\n{job.draft_pr_url}",
            )
        return
    if ci_status == "success":
        if pull_request.get("draft", False):
            await gh.mark_pr_ready(token, pull_request["node_id"])
            detail = "CI passed; PR marked ready for review"
        else:
            detail = "CI passed; PR was already ready for review"
        await _finish(job, db, "DONE", detail)
        await notify(
            user.telegram_id,
            f"Solved issue #{job.issue_number}. CI passed and the PR is ready:\n"
            f"{job.draft_pr_url}",
        )
        return

    max_repairs = max(0, int(os.getenv("SOLVER_MAX_REPAIR_ATTEMPTS", "2")))
    if job.repair_attempts >= max_repairs:
        await _finish(job, db, "FAILED", "CI still fails after automatic repairs")
        await notify(
            user.telegram_id,
            f"CI still fails after {job.repair_attempts} repair attempt(s). PR remains draft:\n"
            f"{job.draft_pr_url}",
        )
        return
    failure_details = await gh.get_ci_failure_details(token, job.repo_full_name, current_sha)
    previous_result = _previous_result(job.result_summary)
    failure_fingerprint = _ci_failure_fingerprint(failure_details)
    repeated_failure = (
        bool(failure_fingerprint)
        and previous_result.get("ci_failure_fingerprint") == failure_fingerprint
    )
    previous_paths = await gh.get_pr_changed_files(
        token, job.repo_full_name, job.draft_pr_number
    )
    if not previous_paths:
        previous_paths = _previous_changed_paths(job.result_summary)
    previous_paths = _prioritize_failure_paths(previous_paths, failure_details)
    await notify(
        user.telegram_id,
        f"CI failed for issue #{job.issue_number}. Starting repair attempt "
        f"{job.repair_attempts + 1}/{max_repairs}:\n{job.draft_pr_url}",
    )
    fork_url = pull_request["head"]["repo"]["clone_url"]
    async with SolverWorkspace(token) as workspace:
        await workspace.clone_existing_branch(fork_url, job.branch_name)
        result = await solve_issue(
            workspace,
            job.repo_full_name,
            job.issue_number,
            issue.get("title", ""),
            issue.get("body") or "",
            mode="repair",
            focus_files=previous_paths,
            ci_failure_details=failure_details,
            repeated_ci_failure=repeated_failure,
        )
        new_sha = await workspace.commit_and_push(
            fork_url,
            job.branch_name,
            f"fix: repair CI for issue #{job.issue_number}",
        )
    result["changed_files"] = list(dict.fromkeys(previous_paths + result["changed_files"]))
    result["ci_failure_fingerprint"] = failure_fingerprint
    job.repair_attempts += 1
    job.head_sha = new_sha
    job.result_summary = json.dumps(result)
    job.ci_polls = 0
    await _reschedule(job, db, 60, "WAITING_CI_AFTER_REPAIR")
    await notify(
        user.telegram_id,
        f"Repair commit pushed for issue #{job.issue_number}. Waiting for CI again:\n"
        f"{job.draft_pr_url}",
    )


async def _retry_or_fail(job: IssueJob, db, error: str) -> None:
    job.attempts += 1
    if job.attempts >= 3:
        await _finish(job, db, "FAILED", error[:2000])
    else:
        await _reschedule(job, db, min(900, 30 * (2 ** (job.attempts - 1))), error)


async def _attach_draft(job: IssueJob, pull_request: dict, db) -> None:
    job.draft_pr_number = pull_request["number"]
    job.draft_pr_url = pull_request["html_url"]
    job.head_sha = pull_request["head"]["sha"]
    job.ci_polls = 0
    await _reschedule(job, db, 60, "WAITING_CI")


async def _reschedule(job: IssueJob, db, seconds: int, reason: str) -> None:
    job.status = "WAITING_CI" if job.draft_pr_number else "QUEUED"
    job.next_attempt_at = datetime.utcnow() + timedelta(seconds=seconds)
    job.last_error = reason[:2000]
    await db.commit()


async def _finish(job: IssueJob, db, status: str, detail: str) -> None:
    job.status = status
    job.last_error = detail[:2000]
    await db.commit()


async def _recover_interrupted_jobs() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob).where(IssueJob.status == "PROCESSING")
        )
        for job in result.scalars():
            job.status = "WAITING_CI" if job.draft_pr_number else "QUEUED"
            job.next_attempt_at = datetime.utcnow()
        await db.commit()


def _repo_from_issue(issue: dict) -> str | None:
    return gh.repo_from_issue(issue)


def _pr_body(job: IssueJob, result: dict) -> str:
    changed = "\n".join(f"- `{path}`" for path in result["changed_files"])
    return f"""## Summary
{result['summary']}

## Changed files
{changed}

## Test plan
{result['test_plan']}

This PR was created as a draft by the issue solver bot. It will remain a
draft until repository CI passes.

Closes #{job.issue_number}
"""


def _pr_title(issue_title: str) -> str:
    title = (issue_title or "Resolve assigned issue").strip()
    title = re.sub(
        r"^(?:fix|feat|chore|refactor|test|docs)(?:\([^)]*\))?:\s*",
        "",
        title,
        count=1,
        flags=re.IGNORECASE,
    )
    return f"fix: {title}"[:240]


def _previous_changed_files(result_summary: str | None) -> str:
    return "\n".join(f"- {path}" for path in _previous_changed_paths(result_summary))


def _previous_changed_paths(result_summary: str | None) -> list[str]:
    paths = _previous_result(result_summary).get("changed_files", [])
    return [path for path in paths[:50] if isinstance(path, str)]


def _previous_result(result_summary: str | None) -> dict:
    if not result_summary:
        return {}
    try:
        result = json.loads(result_summary)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _ci_failure_fingerprint(details: str) -> str:
    diagnostic_lines = []
    for line in (details or "").splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        lowered = clean.lower()
        if "##[error]" in lowered or re.search(r"\berror\s+(?:ts|[a-z]\d{3,})", lowered):
            clean = re.sub(r"^.*?(##\[error\])", r"\1", clean)
            diagnostic_lines.append(clean[-1200:])
    if not diagnostic_lines:
        return ""
    value = "\n".join(dict.fromkeys(diagnostic_lines[:20]))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prioritize_failure_paths(paths: list[str], details: str) -> list[str]:
    """Put files named by the compiler first so their contents survive context bounds."""
    normalized_details = (details or "").replace("\\", "/")
    scored = []
    for index, path in enumerate(paths):
        normalized = path.replace("\\", "/")
        suffixes = [normalized]
        parts = normalized.split("/")
        suffixes.extend("/".join(parts[start:]) for start in range(1, len(parts)))
        mentioned = any(suffix and suffix in normalized_details for suffix in suffixes)
        scored.append((0 if mentioned else 1, index, path))
    return [path for _, _, path in sorted(scored)]


def _reset_job_for_retry(job: IssueJob, reason: str = "Manual retry requested") -> None:
    job.status = "WAITING_CI" if job.draft_pr_number else "QUEUED"
    job.attempts = 0
    job.repair_attempts = 0
    job.ci_polls = 0
    if job.draft_pr_number:
        # An explicit retry authorizes adopting the PR's current head after a user/manual fix.
        job.head_sha = None
    job.next_attempt_at = datetime.utcnow()
    job.last_error = reason


def _completed_pr_needs_retry(
    recorded_sha: str | None, current_sha: str, ci_status: str
) -> bool:
    """Return true when a formerly successful PR needs fresh CI processing."""
    return current_sha != recorded_sha or ci_status != "success"
