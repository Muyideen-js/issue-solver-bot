import pytest
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, DASHBOARD_ID_PREFIX, IssueJob, SolverUser
from app.services import solver_queue
from app.services.crypto import encrypt_token
from app.services.solver_queue import (
    _pr_body,
    _pr_title,
    _previous_changed_files,
    _previous_changed_paths,
    _ci_failure_fingerprint,
    _completed_pr_needs_retry,
    _prioritize_failure_paths,
    _reset_job_for_retry,
    _repo_from_issue,
)


async def _add_user(telegram_id: str, username: str = "octocat") -> SolverUser:
    async with AsyncSessionLocal() as db:
        user = SolverUser(
            telegram_id=telegram_id,
            github_username=username,
            github_token_encrypted=encrypt_token("token"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _add_job(telegram_id: str, repo: str, number: int, status: str = "QUEUED") -> int:
    async with AsyncSessionLocal() as db:
        job = IssueJob(
            telegram_id=telegram_id,
            repo_full_name=repo,
            issue_number=number,
            issue_title="Bug",
            issue_url=f"https://github.com/{repo}/issues/{number}",
            status=status,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job.id


def test_repo_is_extracted_from_search_result():
    issue = {"repository_url": "https://api.github.com/repos/owner/repo"}
    assert _repo_from_issue(issue) == "owner/repo"


def test_pr_body_links_issue_and_records_test_plan():
    job = type("Job", (), {"issue_number": 42})()
    body = _pr_body(job, {
        "summary": "Implemented the feature.",
        "test_plan": "Repository CI should run unit tests.",
        "changed_files": ["src/app.py", "tests/test_app.py"],
    })
    assert "Closes #42" in body
    assert "`src/app.py`" in body
    assert "Repository CI" in body


def test_pr_title_does_not_duplicate_conventional_prefix():
    assert _pr_title("fix: align amount units") == "fix: align amount units"
    assert _pr_title("feat(api): add validation") == "fix: add validation"
    assert _pr_title("Handle empty values") == "fix: Handle empty values"


def test_previous_changed_files_are_recovered_for_ci_repair():
    summary = '{"changed_files":["src/app.ts","tests/app.test.ts"]}'
    assert _previous_changed_files(summary) == "- src/app.ts\n- tests/app.test.ts"
    assert _previous_changed_files("not-json") == ""
    assert _previous_changed_paths(summary) == ["src/app.ts", "tests/app.test.ts"]


def test_retry_reset_preserves_draft_but_resets_failure_counters():
    job = type("Job", (), {})()
    job.draft_pr_number = 153
    job.status = "FAILED"
    job.attempts = 3
    job.repair_attempts = 2
    job.ci_polls = 10
    job.head_sha = "old-sha"
    job.next_attempt_at = None
    job.last_error = "failed"
    _reset_job_for_retry(job)
    assert job.status == "WAITING_CI"
    assert job.attempts == 0
    assert job.repair_attempts == 0
    assert job.ci_polls == 0
    assert job.head_sha is None


def test_retry_reset_can_reopen_a_completed_pr_after_its_head_changes():
    job = type("Job", (), {})()
    job.draft_pr_number = 154
    job.status = "DONE"
    job.attempts = 0
    job.repair_attempts = 2
    job.ci_polls = 0
    job.head_sha = "previous-successful-sha"
    job.next_attempt_at = None
    job.last_error = "CI passed; PR marked ready for review"

    _reset_job_for_retry(job, reason="PR head changed after completion")

    assert job.status == "WAITING_CI"
    assert job.repair_attempts == 0
    assert job.head_sha is None
    assert job.last_error == "PR head changed after completion"


def test_completed_pr_is_retried_when_head_changed_or_ci_failed():
    assert _completed_pr_needs_retry("old-sha", "new-sha", "success") is True
    assert _completed_pr_needs_retry("same-sha", "same-sha", "failure") is True
    assert _completed_pr_needs_retry("same-sha", "same-sha", "pending") is True
    assert _completed_pr_needs_retry("same-sha", "same-sha", "success") is False


def test_ci_failure_fingerprint_is_stable_across_log_prefixes():
    first = "step 2026-01-01 ##[error]src/app.ts(84,22): error TS2352: bad cast"
    second = "new-prefix ##[error]src/app.ts(84,22): error TS2352: bad cast"
    assert _ci_failure_fingerprint(first) == _ci_failure_fingerprint(second)


def test_compiler_named_file_is_preloaded_first():
    paths = ["apps/web/src/other.ts", "apps/web/src/components/OffRampSelector.test.tsx"]
    details = "src/components/OffRampSelector.test.tsx(84,22): error TS2352"
    assert _prioritize_failure_paths(paths, details)[0].endswith("OffRampSelector.test.tsx")


@pytest.mark.asyncio
async def test_queue_all_issues_for_account_ignores_program_labels(monkeypatch):
    async def fake_search(token, username):
        return [{
            "id": 1, "number": 5, "title": "Any issue",
            "html_url": "https://github.com/o/r/issues/5",
            "repository_url": "https://api.github.com/repos/o/r",
        }]

    monkeypatch.setattr(solver_queue.gh, "search_all_assigned_issues", fake_search)
    user = await _add_user(f"{DASHBOARD_ID_PREFIX}xyz")

    discovered, queued = await solver_queue.queue_all_issues_for_account(user)

    assert (discovered, queued) == (1, 1)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob).where(IssueJob.telegram_id == user.telegram_id)
        )
        jobs = result.scalars().all()
    assert len(jobs) == 1
    assert jobs[0].repo_full_name == "o/r"
    assert jobs[0].issue_number == 5


@pytest.mark.asyncio
async def test_process_job_skips_program_label_gate_for_dashboard_accounts(monkeypatch):
    monkeypatch.setenv("PROGRAM_LABELS", "GrantFox OSS")
    user = await _add_user(f"{DASHBOARD_ID_PREFIX}abc")
    job_id = await _add_job(user.telegram_id, "octo/repo", 1)

    async def fake_get_issue(token, repo, number):
        return {"state": "open", "assignees": [{"login": "octocat"}], "labels": []}

    async def fake_get_repository(token, repo):
        raise RuntimeError("reached implementation stage")

    monkeypatch.setattr(solver_queue.gh, "get_issue", fake_get_issue)
    monkeypatch.setattr(solver_queue.gh, "get_repository", fake_get_repository)

    await solver_queue._process_job(job_id)

    async with AsyncSessionLocal() as db:
        job = await db.get(IssueJob, job_id)
    assert job.status != "SKIPPED"
    assert "reached implementation stage" in job.last_error


@pytest.mark.asyncio
async def test_process_job_enforces_program_label_gate_for_telegram_accounts(monkeypatch):
    monkeypatch.setenv("PROGRAM_LABELS", "GrantFox OSS")
    user = await _add_user("123456789")
    job_id = await _add_job(user.telegram_id, "octo/repo", 1)

    async def fake_get_issue(token, repo, number):
        return {"state": "open", "assignees": [{"login": "octocat"}], "labels": []}

    monkeypatch.setattr(solver_queue.gh, "get_issue", fake_get_issue)

    await solver_queue._process_job(job_id)

    async with AsyncSessionLocal() as db:
        job = await db.get(IssueJob, job_id)
    assert job.status == "SKIPPED"


@pytest.mark.asyncio
async def test_enqueue_issue_recognizes_work_already_done_on_another_channel():
    """The same GitHub account connected via Telegram and a dashboard tab shares
    one job history, so a Telegram-solved issue isn't re-solved from the dashboard."""
    telegram_user = await _add_user("123456789", username="sharedgh")
    dashboard_user = await _add_user(f"{DASHBOARD_ID_PREFIX}abc", username="sharedgh")
    await _add_job(telegram_user.telegram_id, "octo/repo", 42, status="DONE")

    issue = {
        "number": 42, "title": "Already fixed via Telegram",
        "html_url": "https://github.com/octo/repo/issues/42",
        "repository_url": "https://api.github.com/repos/octo/repo",
    }
    queued = await solver_queue.enqueue_issue(dashboard_user, issue)

    assert queued is False
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob).where(IssueJob.repo_full_name == "octo/repo", IssueJob.issue_number == 42)
        )
        jobs = result.scalars().all()
    assert len(jobs) == 1  # no duplicate job created under the dashboard channel
    assert jobs[0].telegram_id == telegram_user.telegram_id
