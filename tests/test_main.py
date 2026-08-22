from app.main import app


def test_root_supports_head_for_uptime_monitoring():
    root_methods = {
        method
        for route in app.routes
        if getattr(route, "path", None) == "/"
        for method in getattr(route, "methods", set())
    }
    assert {"GET", "HEAD"}.issubset(root_methods)
