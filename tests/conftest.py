import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ALERT_INTERVAL_SEC", "0")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("APP_SECRET", "test-secret")
# Two ways to run the same suite:
#   local (default)  in-process against a disposable Postgres:   DATABASE_URL=postgresql://...
#   remote           over HTTPS against a deployed instance:      BASE_URL=https://<host>  DEMO_PASSWORD=...
# Remote mode skips tests that need the database or the app internals (they are covered by the local run).
# SMOKE_MODE=readonly (remote only) limits the run to tests that never write, for use against real environments.
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
if not BASE_URL and "DATABASE_URL" not in os.environ:
    pytest.skip("Set DATABASE_URL to a disposable Postgres (local run) or BASE_URL to a deployed instance (remote run)", allow_module_level=True)

if not BASE_URL:
    from app.main import app  # noqa: E402

READONLY_TESTS = {
    "test_login_and_me", "test_role_boundaries", "test_every_protected_route_rejects_anonymous_calls", "test_no_route_returns_a_server_error",
    "test_tenant_and_field_roles_are_walled_off_from_admin_areas", "test_login_rejects_wrong_password_unknown_user_and_blank",
    "test_token_tampering_expiry_and_garbage_are_rejected", "test_reference_and_discovery_endpoints", "test_non_admin_cannot_manage_users",
    "test_security_headers_and_request_id", "test_config_and_disabled_by_default",
}
LOCAL_ONLY_MARKERS = ("db.conn", "db.row", "psycopg", "monkeypatch", "from app import", "import app", "alerts.", "auth.TOKEN_TTL", "auth.make_token",
                      "os.environ[\"DATABASE_URL\"]")


def pytest_collection_modifyitems(config, items):
    if not BASE_URL:
        return
    import inspect
    readonly = os.environ.get("SMOKE_MODE", "full") == "readonly"
    for it in items:
        try:
            src = inspect.getsource(it.function)
        except (OSError, TypeError):
            src = ""
        if any(m in src for m in LOCAL_ONLY_MARKERS) or "sso" in it.fixturenames or "test_azure" in it.nodeid:
            it.add_marker(pytest.mark.skip(reason="needs the database or app internals; runs in the local suite"))
        elif readonly and it.originalname not in READONLY_TESTS:
            it.add_marker(pytest.mark.skip(reason="SMOKE_MODE=readonly"))

PW = os.environ.get("DEMO_PASSWORD", "Demo#2026")
USERS = {"admin": "esg.admin@adports-demo.local", "owner": "data.owner@adports-demo.local",
         "auditor": "auditor@adports-demo.local", "tenant": "tenant1@adports-demo.local",
         "field": "field.operator@adports-demo.local"}


@pytest.fixture(scope="session")
def client():
    import time
    if BASE_URL:
        import httpx
        with httpx.Client(base_url=BASE_URL, timeout=60, follow_redirects=False) as c:
            for _ in range(120):            # a fresh instance seeds in the background
                try:
                    if c.get("/api/health").json().get("status") == "ok":
                        break
                except Exception:
                    pass
                time.sleep(5)
            yield c
        return
    with TestClient(app) as c:
        for _ in range(600):  # seeding runs in the background after startup
            if c.get("/api/health").json().get("status") == "ok":
                break
            time.sleep(0.5)
        yield c


@pytest.fixture(scope="session")
def H(client):
    out = {}
    for k, email in USERS.items():
        r = client.post("/api/auth/login", json={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        out[k] = {"Authorization": "Bearer " + r.json()["token"]}
    return out
