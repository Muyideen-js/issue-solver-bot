# GrantFox Issue Solver Bot

A separate Telegram-controlled coding agent that discovers open GitHub issues
assigned to the connected contributor and labeled for a configured program
(by default `GrantFox OSS` or `Stellar Wave`), implements them with DeepSeek,
pushes branches to the contributor's fork, and creates draft pull requests.
Drafts become ready for review only after repository CI passes.

## How assignment detection works

`/setup` validates the stored GitHub token through `GET /user`; the returned
login is the source of truth. For each label in `PROGRAM_LABELS` the bot
searches GitHub for:

```text
is:issue is:open assignee:<connected-login> label:"<program label>"
```

and merges the results, since GitHub search qualifiers AND together rather
than OR. It rechecks the issue immediately before coding. An issue removed
from the user, closed, or missing every configured label before work starts
is skipped. This search spans every participating repository, not just
repositories owned by GrantChain.

## Solver lifecycle

1. Discover or manually queue an assigned issue.
2. Clone the upstream default branch into a temporary workspace.
3. Let DeepSeek inspect, search, and edit repository files through restricted
   tools. The model cannot run shell commands.
4. Refuse workflow, credential-file, path-traversal, and oversized-file edits.
5. Push the commit to a branch in the connected user's personal fork.
6. Open a draft PR containing `Closes #N`, the summary, changed files, and test
   plan.
7. Use the upstream repository's CI as the execution boundary. If CI fails,
   download failed check annotations and GitHub Actions job logs, preload the
   PR's changed files, and run a focused DeepSeek repair for up to two commits.
8. Mark the PR ready for review only after CI succeeds. If no CI appears, keep
   the PR draft and notify the user.

The service never executes downloaded repository code while it holds GitHub or
DeepSeek credentials. This is intentional: generated and third-party repository
code must not share a process environment with long-lived secrets.

Failed draft PR checks are prioritized ahead of newly queued issues. If the AI
turn budget ends after producing code changes, those changes are preserved in a
draft PR for CI validation instead of being discarded. HTTP client request logs
are suppressed because Telegram embeds the bot credential in request URLs.

Repair prompts promote exact compiler diagnostics and their named files ahead
of general context. Repeated failures are fingerprinted so the agent is told to
use a materially different correction. A Vercel `Authorization required to
deploy` status is treated as repository configuration—not a source-code failure—
and does not block a successful GitHub Actions result.

Every Telegram command is restricted to `TELEGRAM_OWNER_ID`, so another user
cannot consume the shared DeepSeek balance.

## Telegram commands

- `/setup` — connect or rotate the GitHub token
- `/assigned` — list open assigned issues for configured programs
- `/solve <issue-url>` — queue one assigned issue
- `/solveall` — queue all assigned issues for configured programs
- `/retrypr <PR-number>` — retry a stopped solver PR and reset its repair counters
  while explicitly adopting any manual fixes already pushed to that PR branch
- `/autoon` and `/autooff` — control five-minute automatic discovery
- `/solverstatus` — show durable job states
- `/pause` and `/resume` — pause or resume new issue implementations; existing
  draft PRs continue CI monitoring and automatic repair
- `/help`

## Web dashboard

An optional dashboard at `/dashboard` runs alongside Telegram, sharing the same
queue, workers, and database. It's for a different workflow than the
label-gated auto-solve above: paste any GitHub token into a tab and see
**every** open issue assigned to that account, with no `PROGRAM_LABELS`
filter. Click **Fix** on an issue (or **Fix all**) to queue it through the
exact same clone → implement → draft PR → CI-repair pipeline Telegram uses.
**Skip** marks an issue as intentionally not being worked, without ever
writing to GitHub (no auto-close) — the bot never takes action on an issue
it decided not to fix.

A dashboard account is created directly on `/dashboard`, independent of the
Telegram-connected owner; it never participates in the 5-minute label-gated
poller or Telegram commands.

The dashboard is protected by a single HTTP Basic Auth login (your browser
will prompt) and is disabled (503) until `DASHBOARD_PASSWORD` is set:

```env
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=
```

## Environment

Copy `.env.example` to `.env` locally. Never commit `.env`.

```env
TELEGRAM_SOLVER_BOT_TOKEN=...
TELEGRAM_OWNER_ID=123456789
ENCRYPTION_KEY=...
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
PROGRAM_LABELS=GrantFox OSS,Stellar Wave
ASSIGNMENT_POLL_SECONDS=300
SOLVER_MAX_TURNS=30
SOLVER_REPAIR_MAX_TURNS=16
SOLVER_MAX_REPAIR_ATTEMPTS=2
DATABASE_URL=postgresql://user:password@host/database
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=
```

The GitHub token needs issue/metadata read access, Checks read access, Actions
read access for job logs, permission to create and push branches in the user's
fork, and pull-request write access on target repositories.
A numeric `TELEGRAM_OWNER_ID` makes every command private to one Telegram account.
You can obtain your numeric ID from Telegram's `@userinfobot`; do not use your
`@username` in this setting.
A GitHub App with short-lived credentials is preferable for a later multi-user
production release; this first version encrypts the contributor PAT at rest.

## Local verification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --reload --port 8010
```

## Render

Create a separate Render web service and PostgreSQL database for this project.
Set the environment values above, use `pip install -r requirements.txt` as the
build command, and use the included `Procfile` start command. Run exactly one
instance because Telegram long polling and the durable worker are single-instance.
The root URL supports both `GET` and lightweight `HEAD` requests so free uptime
monitors can check the service without downloading a response body.
