"""Durable SQLAlchemy models for users and issue-solving jobs."""
import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./solver.db")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_database_url(), echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class SolverUser(Base):
    __tablename__ = "solver_users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    github_username = Column(String, nullable=False)
    github_token_encrypted = Column(String, nullable=False)
    auto_solve = Column(Boolean, nullable=False, default=False)
    paused = Column(Boolean, nullable=False, default=False)
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
