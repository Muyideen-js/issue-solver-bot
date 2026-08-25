"""Shared test setup: an isolated in-memory database for the whole test session."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
# TestClient talks to the app over plain http://testserver; a Secure-flagged
# session cookie would never be sent back, so relax it for the test session.
os.environ.setdefault("DASHBOARD_COOKIE_SECURE", "false")

import pytest_asyncio

from app.models.database import AsyncSessionLocal, IssueJob, PortalUser, SolverUser, init_db


@pytest_asyncio.fixture(autouse=True)
async def _isolated_database():
    await init_db()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(IssueJob.__table__.delete())
        await db.execute(SolverUser.__table__.delete())
        await db.execute(PortalUser.__table__.delete())
        await db.commit()
