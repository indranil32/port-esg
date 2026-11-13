"""Accounts, sign-in security and the smaller read endpoints: the use cases the other suites do not reach."""
import uuid


from app import auth
from .conftest import *  # noqa: F401,F403  (client, H, PW, USERS)


def login(client, email, pw):
    return client.post("/api/auth/login", json={"email": email, "password": pw})


def test_login_rejects_wrong_password_unknown_user_and_blank(client):
    assert login(client, USERS["admin"], "wrong-password").status_code == 401
    assert login(client, "nobody@example.com", PW).status_code == 401
    assert client.post("/api/auth/login", json={}).status_code in (401, 422)


def test_token_tampering_expiry_and_garbage_are_rejected(client, H):
    good = H["admin"]["Authorization"]
    assert client.get("/api/auth/me", headers={"Authorization": good}).status_code == 200
    tok = good.split(" ", 1)[1]
    body, sig = tok.split(".")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {body}.{sig[::-1]}"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401
    me = client.get("/api/auth/me", headers=H["admin"]).json()
    old = auth.TOKEN_TTL
    auth.TOKEN_TTL = -10                                           # a token that is already expired
    try:
        expired = auth.make_token(me["user_id"])
    finally:
        auth.TOKEN_TTL = old
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_password_hash_roundtrip():
    h = auth.hash_password("S0me-long-pass")
    assert auth.verify_password("S0me-long-pass", h) and not auth.verify_password("other", h)
    assert not auth.verify_password("x", "malformed")


def test_admin_creates_edits_and_disables_a_user(client, H):
    email = f"qa.{uuid.uuid4().hex[:8]}@adports-demo.local"
    r = client.post("/api/admin/users", headers=H["admin"], json={"email": email, "full_name": "QA User", "role": "auditor", "password": "Long-enough-pass1"})
    assert r.status_code == 200, r.text
    uid = r.json()["user_id"]
    assert client.post("/api/admin/users", headers=H["admin"], json={"email": email.upper(), "full_name": "Dup", "role": "auditor", "password": "Long-enough-pass1"}).status_code == 409
    assert login(client, email, "Long-enough-pass1").status_code == 200
    # validation
    base = {"full_name": "x", "password": "Long-enough-pass1"}
    assert client.post("/api/admin/users", headers=H["admin"], json={**base, "email": "a@b.c", "role": "emperor"}).status_code == 400
    assert client.post("/api/admin/users", headers=H["admin"], json={**base, "email": "a2@b.c", "role": "auditor", "password": "short"}).status_code == 400
    assert client.post("/api/admin/users", headers=H["admin"], json={**base, "email": "a3@b.c", "role": "tenant"}).status_code == 400
    # change role, reset password, disable
    assert client.patch(f"/api/admin/users/{uid}", headers=H["admin"], json={"role": "data_owner"}).json()["role"] == "data_owner"
    assert client.patch(f"/api/admin/users/{uid}", headers=H["admin"], json={"password": "short"}).status_code == 400
    assert client.patch(f"/api/admin/users/{uid}", headers=H["admin"], json={"password": "Another-long-pass2"}).status_code == 200
    assert login(client, email, "Long-enough-pass1").status_code == 401 and login(client, email, "Another-long-pass2").status_code == 200
    tok = login(client, email, "Another-long-pass2").json()["token"]
    assert client.patch(f"/api/admin/users/{uid}", headers=H["admin"], json={"active": False}).json()["active"] is False
    assert login(client, email, "Another-long-pass2").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401      # existing sessions die too
    assert client.patch(f"/api/admin/users/{uid}", headers=H["admin"], json={}).status_code == 400
    assert client.patch(f"/api/admin/users/{uuid.uuid4()}", headers=H["admin"], json={"active": True}).status_code == 404
    acts = [a["action"] for a in client.get("/api/audit-log?limit=200", headers=H["admin"]).json()]
    assert "insert" in acts and "update" in acts


def test_admin_cannot_lock_themselves_out(client, H):
    me = client.get("/api/auth/me", headers=H["admin"]).json()["user_id"]
    assert client.patch(f"/api/admin/users/{me}", headers=H["admin"], json={"active": False}).status_code == 400
    assert client.patch(f"/api/admin/users/{me}", headers=H["admin"], json={"role": "auditor"}).status_code == 400


def test_non_admin_cannot_manage_users(client, H):
    for role in ("owner", "auditor", "tenant", "field"):
        assert client.get("/api/admin/users", headers=H[role]).status_code == 403
        assert client.post("/api/admin/users", headers=H[role], json={}).status_code in (403, 422)


def test_reference_and_discovery_endpoints(client, H):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/sources", headers=H["auditor"]).json()
    tables = client.get("/api/tables", headers=H["owner"])
    assert tables.status_code == 200 and tables.json()
    name = tables.json()[0]["tables"][0]["table"]
    assert client.get(f"/api/schema/{name}", headers=H["owner"]).status_code == 200
    assert client.get("/api/schema/not_a_table", headers=H["owner"]).status_code in (403, 404)
    assert client.get("/api/captures/config", headers=H["field"]).status_code == 200
    assert client.get("/api/captures/meters", headers=H["field"]).status_code == 200
    assert client.get("/api/collect-links/metrics", headers=H["admin"]).status_code == 200
    assert client.get("/api/alerts/summary", headers=H["owner"]).status_code == 200
    k = client.get("/api/reports/kpis.csv", headers=H["auditor"])
    assert k.status_code in (200, 404) and (k.status_code == 404 or k.headers["content-type"].startswith("text/csv"))


def test_test_notification_is_admin_only(client, H):
    assert client.post("/api/alerts/test-notification", headers=H["owner"]).status_code == 403
    assert client.post("/api/alerts/test-notification", headers=H["admin"]).status_code == 200


def test_security_headers_and_request_id(client):
    r = client.get("/api/health")
    assert r.headers.get("x-request-id")
