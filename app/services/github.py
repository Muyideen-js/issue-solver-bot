"""GitHub API operations for assignment discovery and draft PR creation."""
import asyncio
import os
import re
from typing import Optional

import httpx

GITHUB_API = "https://api.github.com"
ISSUE_URL_PATTERN = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?$"
)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def validate_token(token: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{GITHUB_API}/user", headers=_headers(token))
    if response.status_code != 200:
        return None
    return response.json().get("login")


def configured_program_labels() -> list[str]:
    raw = os.getenv("PROGRAM_LABELS", "GrantFox OSS,Stellar Wave")
    labels = []
    for part in raw.split(","):
        label = part.strip()
        if label and label not in labels:
            labels.append(label)
    return labels


async def _search_issues(client: httpx.AsyncClient, token: str, query: str) -> list[dict]:
    issues = []
    page = 1
    while True:
        response = await client.get(
            f"{GITHUB_API}/search/issues",
            headers=_headers(token),
            params={"q": query, "per_page": 100, "page": page, "sort": "updated"},
        )
        response.raise_for_status()
        batch = response.json().get("items", [])
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < 100 or page >= 10:
            return issues
        page += 1


async def search_assigned_program_issues(token: str, username: str) -> list[dict]:
    """Search all GitHub repositories for every configured program label, not just GrantChain's.

    GitHub's search qualifiers AND together, so an OR across program labels
    requires one query per label; results are merged and deduplicated by id.
    """
    issues: dict[int, dict] = {}
    async with httpx.AsyncClient(timeout=45) as client:
        for label in configured_program_labels():
            query = f'is:issue is:open assignee:{username} label:"{label}"'
            for item in await _search_issues(client, token, query):
                issues[item["id"]] = item
    return list(issues.values())


async def search_all_assigned_issues(token: str, username: str) -> list[dict]:
    """List every open issue assigned to this account, regardless of label."""
    async with httpx.AsyncClient(timeout=45) as client:
        items = await _search_issues(client, token, f"is:issue is:open assignee:{username}")
    issues: dict[int, dict] = {item["id"]: item for item in items}
    return list(issues.values())


def repo_from_issue(issue: dict) -> str | None:
    """Extract "owner/repo" from a GitHub search-issues or issues API result."""
    repository_url = issue.get("repository_url") or ""
    prefix = f"{GITHUB_API}/repos/"
    if repository_url.startswith(prefix):
        return repository_url[len(prefix):]
    html_url = issue.get("html_url") or ""
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/\d+", html_url)
    return match.group(1) if match else None


def parse_issue_url(url: str) -> tuple[str, int] | None:
    match = ISSUE_URL_PATTERN.fullmatch((url or "").strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


async def get_issue(token: str, repo: str, issue_number: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}", headers=_headers(token)
        )
    response.raise_for_status()
    issue = response.json()
    if "pull_request" in issue:
        raise ValueError("The supplied URL points to a pull request, not an issue")
    return issue


def is_open_and_assigned(issue: dict, username: str) -> bool:
    assignees = {
        item.get("login", "").lower() for item in issue.get("assignees", [])
    }
    return issue.get("state") == "open" and username.lower() in assignees


def is_program_issue(issue: dict) -> bool:
    expected = {label.lower() for label in configured_program_labels()}
    labels = {
        (item.get("name", "") if isinstance(item, dict) else str(item)).lower()
        for item in issue.get("labels", [])
    }
    return bool(expected & labels)


async def get_repository(token: str, repo: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{GITHUB_API}/repos/{repo}", headers=_headers(token))
    response.raise_for_status()
    return response.json()


async def ensure_personal_fork(
    token: str, username: str, upstream_repo: str, repo_name: str
) -> dict:
    """Return the user's fork, creating it and waiting for GitHub when necessary."""
    candidate = f"{username}/{repo_name}"
    async with httpx.AsyncClient(timeout=45) as client:
        existing = await client.get(
            f"{GITHUB_API}/repos/{candidate}", headers=_headers(token)
        )
        if existing.status_code == 200:
            data = existing.json()
            parent_name = (data.get("parent") or {}).get("full_name", "")
            if parent_name.lower() != upstream_repo.lower():
                raise RuntimeError(
                    f"{candidate} already exists but is not a fork of {upstream_repo}"
                )
            return data
        if existing.status_code != 404:
            existing.raise_for_status()

        created = await client.post(
            f"{GITHUB_API}/repos/{upstream_repo}/forks",
            headers=_headers(token),
            json={"default_branch_only": True},
        )
        created.raise_for_status()

        for _ in range(20):
            await asyncio.sleep(3)
            response = await client.get(
                f"{GITHUB_API}/repos/{candidate}", headers=_headers(token)
            )
            if response.status_code == 200:
                return response.json()
    raise RuntimeError(f"GitHub did not finish creating fork {candidate}")


async def create_draft_pr(
    token: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> dict:
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=_headers(token),
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": True,
            },
        )
    response.raise_for_status()
    return response.json()


async def find_open_pr_by_head(token: str, repo: str, head: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=_headers(token),
            params={"state": "open", "head": head, "per_page": 10},
        )
    response.raise_for_status()
    pulls = response.json()
    return pulls[0] if pulls else None


async def get_pr(token: str, repo: str, pr_number: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}", headers=_headers(token)
        )
    response.raise_for_status()
    return response.json()


async def mark_pr_ready(token: str, pull_request_node_id: str) -> None:
    query = """
    mutation($id: ID!) {
      markPullRequestReadyForReview(input: {pullRequestId: $id}) {
        pullRequest { id isDraft }
      }
    }
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.github.com/graphql",
            headers=_headers(token),
            json={"query": query, "variables": {"id": pull_request_node_id}},
        )
    response.raise_for_status()
    errors = response.json().get("errors")
    if errors:
        raise RuntimeError(f"GitHub could not mark PR ready: {errors[0].get('message')}")


async def get_ci_status(token: str, repo: str, sha: str) -> str:
    """Return success, failure, pending, or none for one exact commit."""
    async with httpx.AsyncClient(timeout=45) as client:
        checks, statuses = await asyncio.gather(
            client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
                headers=_headers(token),
                params={"per_page": 100},
            ),
            client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{sha}/status",
                headers=_headers(token),
            ),
        )
    runs = checks.json().get("check_runs", []) if checks.status_code == 200 else []
    status_data = statuses.json() if statuses.status_code == 200 else {}
    commit_statuses = [
        status for status in status_data.get("statuses", [])
        if not _is_noncode_authorization_failure(status)
    ]
    if any(run.get("status") != "completed" for run in runs):
        return "pending"
    failed = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    if any(run.get("conclusion") in failed for run in runs):
        return "failure"
    if any(status.get("state") in {"failure", "error"} for status in commit_statuses):
        return "failure"
    if any(status.get("state") == "pending" for status in commit_statuses):
        return "pending"
    if runs or any(status.get("state") == "success" for status in commit_statuses):
        return "success"
    return "none"


async def get_pr_changed_files(token: str, repo: str, pr_number: int) -> list[str]:
    """Return every filename currently changed by a pull request."""
    paths = []
    async with httpx.AsyncClient(timeout=45) as client:
        for page in range(1, 31):
            response = await client.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
                headers=_headers(token),
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            batch = response.json()
            paths.extend(
                item["filename"] for item in batch if isinstance(item.get("filename"), str)
            )
            if len(batch) < 100:
                break
    return paths


async def get_ci_failure_details(token: str, repo: str, sha: str) -> str:
    """Collect check output, annotations, and GitHub Actions logs for repair prompts."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        checks, statuses = await asyncio.gather(
            client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
                headers=_headers(token),
                params={"per_page": 100},
            ),
            client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{sha}/status",
                headers=_headers(token),
            ),
        )
        sections = []
        if checks.status_code == 200:
            for run in checks.json().get("check_runs", []):
                if run.get("conclusion") not in {
                    "failure", "cancelled", "timed_out", "action_required", "startup_failure"
                }:
                    continue
                output = run.get("output") or {}
                check_sections = [
                    f"CHECK: {run.get('name')}",
                    f"Conclusion: {run.get('conclusion')}",
                    f"Title: {output.get('title') or ''}",
                    f"Summary: {output.get('summary') or ''}",
                    f"Details: {output.get('text') or ''}",
                ]
                check_id = run.get("id")
                if check_id:
                    try:
                        annotations = await client.get(
                            f"{GITHUB_API}/repos/{repo}/check-runs/{check_id}/annotations",
                            headers=_headers(token),
                            params={"per_page": 100},
                        )
                        if annotations.status_code == 200:
                            rendered = [_format_annotation(item) for item in annotations.json()]
                            if rendered:
                                check_sections.append("Annotations:\n" + "\n".join(rendered))
                    except (httpx.HTTPError, TypeError, ValueError):
                        pass
                job_id = _actions_job_id(run.get("details_url") or "")
                if job_id:
                    try:
                        logs = await client.get(
                            f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs",
                            headers=_headers(token),
                        )
                        if logs.status_code == 200 and logs.text:
                            check_sections.append(
                                "GitHub Actions job log (failure tail):\n"
                                + _tail_text(logs.text, 60_000)
                            )
                    except httpx.HTTPError:
                        pass
                sections.append("\n".join(check_sections))
        if statuses.status_code == 200:
            for status in statuses.json().get("statuses", []):
                if (
                    status.get("state") in {"failure", "error"}
                    and not _is_noncode_authorization_failure(status)
                ):
                    sections.append(
                        f"STATUS: {status.get('context')}\n"
                        f"Description: {status.get('description') or ''}"
                    )
    combined = "\n\n".join(sections)
    return _tail_text(combined, 100_000) if combined else "CI failed without inline diagnostics."


def _actions_job_id(details_url: str) -> int | None:
    match = re.search(r"/job/(\d+)(?:[/?#]|$)", details_url)
    return int(match.group(1)) if match else None


def _is_noncode_authorization_failure(status: dict) -> bool:
    """Ignore deployment statuses that cannot be repaired by changing source code."""
    if status.get("state") not in {"failure", "error"}:
        return False
    context = (status.get("context") or "").lower()
    description = (status.get("description") or "").lower()
    return (
        context == "vercel"
        and any(
            phrase in description
            for phrase in ("authorization required", "authorisation required", "not authorized")
        )
    )


def _format_annotation(annotation: dict) -> str:
    location = annotation.get("path") or "unknown file"
    if annotation.get("start_line"):
        location += f":{annotation['start_line']}"
    title = annotation.get("title") or annotation.get("annotation_level") or "diagnostic"
    message = annotation.get("message") or ""
    details = annotation.get("raw_details") or ""
    return f"- {location} [{title}] {message} {details}".strip()[:4000]


def _tail_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return "[earlier log output omitted]\n" + value[-limit:]
