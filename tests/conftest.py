"""Shared test setup: an isolated in-memory database for the whole test session."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest_asyncio

from app.models.database import AsyncSessionLocal, IssueJob, SolverUser, init_db


@pytest_asyncio.fixture(autouse=True)
async def _isolated_database():
    await init_db()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(IssueJob.__table__.delete())
        await db.execute(SolverUser.__table__.delete())
        await db.commit()
