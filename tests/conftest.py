import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ALERT_INTERVAL_SEC", "0")
os.environ.setdefault("SEED_DEMO_DATA", "true")
os.environ.setdefault("APP_SECRET", "test-secret")
if "DATABASE_URL" not in os.environ:
    pytest.skip("Set DATABASE_URL to a disposable Postgres with db/01_schema.sql and db/02_schema_ext.sql loaded", allow_module_level=True)

from app.main import app  # noqa: E402

PW = os.environ.get("DEMO_PASSWORD", "Demo#2026")
USERS = {"admin": "esg.admin@adports-demo.local", "owner": "data.owner@adports-demo.local",
         "auditor": "auditor@adports-demo.local", "tenant": "tenant1@adports-demo.local",
         "field": "field.operator@adports-demo.local"}


@pytest.fixture(scope="session")
def client():
    import time
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
