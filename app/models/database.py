"""Durable SQLAlchemy models for users and issue-solving jobs."""
import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.services.password import hash_password


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./solver.db")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _engine_kwargs(url: str) -> dict:
    # An in-memory SQLite DB (used by tests) is per-connection; StaticPool keeps
    # every session on the same connection so data doesn't vanish between them.
    if ":memory:" in url:
        return {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    return {}


engine = create_async_engine(_database_url(), echo=False, **_engine_kwargs(_database_url()))
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

DASHBOARD_ID_PREFIX = "dash:"


class SolverUser(Base):
    __tablename__ = "solver_users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    github_username = Column(String, nullable=False)
    github_token_encrypted = Column(String, nullable=False)
    auto_solve = Column(Boolean, nullable=False, default=False)
    paused = Column(Boolean, nullable=False, default=False)
    # Nullable, no FK constraint — matches this table's existing convention of
    # associating rows by plain value rather than ORM relationships. Only
    # meaningful for dash:-prefixed (dashboard) accounts.
    owner_portal_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PortalUser(Base):
    __tablename__ = "portal_users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    must_change_password = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class IssueJob(Base):
    __tablename__ = "issue_jobs"
    __table_args__ = (
        UniqueConstraint(
            "telegram_id", "repo_full_name", "issue_number", name="uq_solver_issue"
        ),
    )

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, nullable=False, index=True)
    repo_full_name = Column(String, nullable=False, index=True)
    issue_number = Column(Integer, nullable=False)
    issue_title = Column(String, nullable=False)
    issue_url = Column(String, nullable=False)
    status = Column(String, nullable=False, default="QUEUED", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    repair_attempts = Column(Integer, nullable=False, default=0)
    ci_polls = Column(Integer, nullable=False, default=0)
    branch_name = Column(String, nullable=True)
    draft_pr_number = Column(Integer, nullable=True)
    draft_pr_url = Column(String, nullable=True)
    head_sha = Column(String, nullable=True)
    result_summary = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await _add_missing_columns()


async def _add_missing_columns() -> None:
    """Idempotently add new columns to tables that already existed in production.

    create_all only creates missing tables, never alters existing ones, so a
    column added to a model here needs a matching entry below to reach an
    already-deployed database.
    """
    additions = {
        "solver_users": {"owner_portal_user_id": "INTEGER"},
    }

    def _migrate(sync_connection) -> None:
        inspector = inspect(sync_connection)
        table_names = inspector.get_table_names()
        for table_name, columns in additions.items():
            if table_name not in table_names:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, definition in columns.items():
                if column_name in existing:
                    continue
                sync_connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}')
                )

    async with engine.begin() as connection:
        await connection.run_sync(_migrate)


def is_dashboard_user(user: SolverUser) -> bool:
    """Dashboard-added accounts carry a synthetic telegram_id, not a real Telegram ID."""
    return user.telegram_id.startswith(DASHBOARD_ID_PREFIX)


async def telegram_ids_sharing_username(db, github_username: str) -> list[str]:
    """Every telegram_id (Telegram owner and/or any dashboard tab) connected to
    the same GitHub account, so a job solved through one channel is recognized
    by the others instead of being solved again."""
    result = await db.execute(
        select(SolverUser.telegram_id).where(
            func.lower(SolverUser.github_username) == github_username.lower()
        )
    )
    return [row[0] for row in result.all()]


async def bootstrap_admin() -> PortalUser | None:
    """Create or promote the initial admin from env vars.

    Any dash: dashboard account created before this feature shipped has no
    owner yet; the first time the admin is created (not merely promoted),
    those orphaned accounts are adopted so they don't disappear from view.
    """
    username = os.getenv("DASHBOARD_USERNAME", "").strip().lower()
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if not username or not password:
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PortalUser).where(PortalUser.username == username))
        admin = result.scalar_one_or_none()
        created = False
        if admin:
            if not admin.is_admin:
                admin.is_admin = True
        else:
            admin = PortalUser(
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
                must_change_password=False,
            )
            db.add(admin)
            created = True
        await db.commit()
        await db.refresh(admin)

        if created:
            orphaned = await db.execute(
                select(SolverUser).where(
                    SolverUser.telegram_id.startswith(DASHBOARD_ID_PREFIX),
                    SolverUser.owner_portal_user_id.is_(None),
                )
            )
            adopted_any = False
            for account in orphaned.scalars():
                account.owner_portal_user_id = admin.id
                adopted_any = True
            if adopted_any:
                await db.commit()

        return admin
