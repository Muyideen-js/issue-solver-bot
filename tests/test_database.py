from app.models.database import DASHBOARD_ID_PREFIX, is_dashboard_user


def test_is_dashboard_user_matches_synthetic_prefix():
    dashboard_user = type("User", (), {"telegram_id": f"{DASHBOARD_ID_PREFIX}abc-123"})()
    telegram_user = type("User", (), {"telegram_id": "987654321"})()
    assert is_dashboard_user(dashboard_user) is True
    assert is_dashboard_user(telegram_user) is False


def test_dashboard_id_prefix_is_not_a_valid_telegram_id():
    assert not DASHBOARD_ID_PREFIX.isdigit()
