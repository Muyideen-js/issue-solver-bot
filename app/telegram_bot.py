"""Telegram command interface for the standalone issue solver."""
import logging
import os
from functools import wraps

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.models.database import AsyncSessionLocal, IssueJob, SolverUser
from app.services import github as gh
from app.services.crypto import decrypt_token, encrypt_token
from app.services.solver_queue import discover_for_user, enqueue_issue

logger = logging.getLogger(__name__)
WAITING_FOR_TOKEN = 1


def owner_only(handler):
    """Keep the personal solver and its shared AI budget private."""
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        expected = int(os.environ["TELEGRAM_OWNER_ID"])
        actual = update.effective_user.id if update.effective_user else None
        if actual != expected:
            if update.effective_message:
                await update.effective_message.reply_text("This is a private bot.")
            return ConversationHandler.END
        return await handler(update, context)

    return wrapped


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setup", setup)],
        states={
            WAITING_FOR_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_token)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("assigned", assigned))
    application.add_handler(CommandHandler("solve", solve))
    application.add_handler(CommandHandler("solveall", solve_all))
    application.add_handler(CommandHandler("solverstatus", status))
    application.add_handler(CommandHandler("autoon", auto_on))
    application.add_handler(CommandHandler("autooff", auto_off))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))
    application.add_handler(CommandHandler("help", help_command))
    return application


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "GrantFox Issue Solver\n\n"
        "Use /setup to connect GitHub, then /assigned to see GrantFox issues "
        "assigned to your GitHub username."
    )


@owner_only
async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Send a GitHub token with read access to issues and permission to create "
        "fork branches and pull requests. The message will be deleted after validation.\n\n"
        "Use /cancel to stop."
    )
    return WAITING_FOR_TOKEN


@owner_only
async def save_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        logger.warning("Could not delete the GitHub token message")
    username = await gh.validate_token(token)
    if not username:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="GitHub rejected that token. Run /setup and try again.",
        )
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(SolverUser.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.github_username = username
            user.github_token_encrypted = encrypt_token(token)
        else:
            db.add(SolverUser(
                telegram_id=telegram_id,
                github_username=username,
                github_token_encrypted=encrypt_token(token),
            ))
        await db.commit()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Connected to GitHub as @{username}. Use /assigned or /solveall.",
    )
    return ConversationHandler.END


@owner_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


@owner_only
async def assigned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    if not user:
        await update.message.reply_text("Run /setup first.")
        return
    issues = await gh.search_assigned_grantfox_issues(
        decrypt_token(user.github_token_encrypted), user.github_username
    )
    if not issues:
        await update.message.reply_text("No open GrantFox issues are assigned to you.")
        return
    lines = [f"Assigned GrantFox issues ({len(issues)}):"]
    for issue in issues[:25]:
        lines.append(f"- #{issue['number']} {issue['title']}\n  {issue['html_url']}")
    if len(issues) > 25:
        lines.append(f"...and {len(issues) - 25} more")
    await update.message.reply_text("\n".join(lines)[:4000])


@owner_only
async def solve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    if not user:
        await update.message.reply_text("Run /setup first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /solve https://github.com/owner/repo/issues/123")
        return
    parsed = gh.parse_issue_url(context.args[0])
    if not parsed:
        await update.message.reply_text("That is not a valid GitHub issue URL.")
        return
    repo, number = parsed
    token = decrypt_token(user.github_token_encrypted)
    issue = await gh.get_issue(token, repo, number)
    if (
        not gh.is_open_and_assigned(issue, user.github_username)
        or not gh.is_grantfox_issue(issue)
    ):
        await update.message.reply_text(
            f"Issue #{number} is not an open GrantFox issue assigned to "
            f"@{user.github_username}."
        )
        return
    queued = await enqueue_issue(user, issue)
    await update.message.reply_text(
        "Queued." if queued else "That issue is already queued or completed."
    )


@owner_only
async def solve_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    if not user:
        await update.message.reply_text("Run /setup first.")
        return
    discovered, queued = await discover_for_user(user)
    await update.message.reply_text(
        f"Found {discovered} assigned GrantFox issue(s); queued {queued} new job(s)."
    )


@owner_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _user(update)
    if not user:
        await update.message.reply_text("Run /setup first.")
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(IssueJob.status, func.count())
            .where(IssueJob.telegram_id == user.telegram_id)
            .group_by(IssueJob.status)
        )
        counts = dict(result.all())
    breakdown = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
    await update.message.reply_text(
        f"GitHub: @{user.github_username}\n"
        f"Automatic discovery: {'on' if user.auto_solve else 'off'}\n"
        f"Paused: {'yes' if user.paused else 'no'}\n"
        f"Jobs: {breakdown or 'none'}"
    )


@owner_only
async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_flag(update, auto_solve=True, message="Automatic assignment discovery enabled.")


@owner_only
async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_flag(update, auto_solve=False, message="Automatic assignment discovery disabled.")


@owner_only
async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_flag(update, paused=True, message="Solver paused. Existing draft PRs are preserved.")


@owner_only
async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_flag(update, paused=False, message="Solver resumed.")


@owner_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/setup - connect or rotate GitHub token\n"
        "/assigned - list assigned GrantFox issues\n"
        "/solve <issue URL> - solve one assigned issue\n"
        "/solveall - queue every assigned GrantFox issue\n"
        "/autoon and /autooff - control automatic discovery\n"
        "/solverstatus - job progress\n"
        "/pause and /resume - control the worker"
    )


async def _user(update: Update) -> SolverUser | None:
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(SolverUser.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()


async def _set_flag(update: Update, message: str, **flags) -> None:
    telegram_id = str(update.effective_user.id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SolverUser).where(SolverUser.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await update.message.reply_text("Run /setup first.")
            return
        for name, value in flags.items():
            setattr(user, name, value)
        await db.commit()
    await update.message.reply_text(message)
