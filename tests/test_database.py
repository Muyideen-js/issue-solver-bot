from app.models import database
from app.models.database import DASHBOARD_ID_PREFIX, is_dashboard_user


def test_is_dashboard_user_matches_synthetic_prefix():
    dashboard_user = type("User", (), {"telegram_id": f"{DASHBOARD_ID_PREFIX}abc-123"})()
    telegram_user = type("User", (), {"telegram_id": "987654321"})()
    assert is_dashboard_user(dashboard_user) is True
    assert is_dashboard_user(telegram_user) is False


def test_dashboard_id_prefix_is_not_a_valid_telegram_id():
    assert not DASHBOARD_ID_PREFIX.isdigit()


def test_neon_style_url_is_translated_for_asyncpg(monkeypatch):
    """asyncpg rejects libpq's sslmode/channel_binding as connect kwargs."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require",
    )

    url = database._database_url()

    assert url == "postgresql+asyncpg://u:p@ep-x.neon.tech/db"
    assert database._engine_kwargs(url) == {"connect_args": {"ssl": True}}


def test_postgres_url_without_sslmode_does_not_force_tls(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@internal-host/db")

    url = database._database_url()

    assert url == "postgresql+asyncpg://u:p@internal-host/db"
    assert database._engine_kwargs(url) == {}


def test_unrelated_query_parameters_survive_translation(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@host/db?sslmode=require&application_name=solver",
    )

    assert database._database_url() == (
        "postgresql+asyncpg://u:p@host/db?application_name=solver"
    )


def test_sqlite_url_is_left_alone(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./solver.db")

    assert database._database_url() == "sqlite+aiosqlite:///./solver.db"
