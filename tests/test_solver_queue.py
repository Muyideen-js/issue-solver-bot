from app.services.solver_queue import (
    _pr_body,
    _pr_title,
    _previous_changed_files,
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
