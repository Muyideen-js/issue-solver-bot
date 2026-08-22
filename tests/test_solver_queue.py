from app.services.solver_queue import (
    _pr_body,
    _pr_title,
    _previous_changed_files,
    _previous_changed_paths,
    _ci_failure_fingerprint,
    _prioritize_failure_paths,
    _reset_job_for_retry,
    _repo_from_issue,
)


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


def test_ci_failure_fingerprint_is_stable_across_log_prefixes():
    first = "step 2026-01-01 ##[error]src/app.ts(84,22): error TS2352: bad cast"
    second = "new-prefix ##[error]src/app.ts(84,22): error TS2352: bad cast"
    assert _ci_failure_fingerprint(first) == _ci_failure_fingerprint(second)


def test_compiler_named_file_is_preloaded_first():
    paths = ["apps/web/src/other.ts", "apps/web/src/components/OffRampSelector.test.tsx"]
    details = "src/components/OffRampSelector.test.tsx(84,22): error TS2352"
    assert _prioritize_failure_paths(paths, details)[0].endswith("OffRampSelector.test.tsx")
