"""Entra ID sign-in (against a fake identity provider) and the pluggable evidence storage."""
import time
import uuid
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import db, entra, storage

TENANT, CLIENT = "11111111-2222-3333-4444-555555555555", "cccccccc-0000-0000-0000-000000000001"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sso(monkeypatch):
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setenv("ENTRA_REDIRECT_URI", "http://testserver/api/auth/entra/callback")
    monkeypatch.setenv("ENTRA_ROLE_MAP", "AdPorts.Admin=esg_admin,AdPorts.Owner=data_owner")
    monkeypatch.setenv("ENTRA_ALLOWED_DOMAINS", "adports-demo.local")
    monkeypatch.setattr(entra, "_signing_key", lambda c, t: KEY.public_key())
    sent = {}

    def fake_exchange(c, code, verifier, redirect):
        sent.update(code=code, verifier=verifier)
        claims = dict(sent["claims"], iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", aud=CLIENT,
                      exp=int(time.time()) + 300, nonce=sent["nonce"])
        return {"id_token": jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})}
    monkeypatch.setattr(entra, "_exchange", fake_exchange)
    return sent


def start(client, sso, **claims):
    r = client.get("/api/auth/entra/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [CLIENT]
    sso["nonce"] = q["nonce"][0]
    sso["claims"] = claims
    return q["state"][0]


def test_config_and_disabled_by_default(client):
    assert client.get("/api/auth/config").json() == {"entra": False, "local": True, "entra_login_url": "/api/auth/entra/login"}
    assert client.get("/api/auth/entra/login", follow_redirects=False).status_code == 404


def test_sso_existing_user_signs_in(client, sso):
    assert client.get("/api/auth/config").json()["entra"] is True
    state = start(client, sso, email="esg.admin@adports-demo.local", name="Admin", roles=["AdPorts.Admin"])
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/#sso=")
    token = r.headers["location"].split("=", 1)[1]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["role"] == "esg_admin"
    assert sso["verifier"]


def test_sso_refusals(client, sso, monkeypatch):
    # unknown user, no auto-provision
    state = start(client, sso, email="new.person@adports-demo.local", roles=["AdPorts.Owner"])
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"].startswith("/#sso_error=")
    # wrong state
    start(client, sso, email="esg.admin@adports-demo.local")
    r = client.get("/api/auth/entra/callback?code=abc&state=forged", follow_redirects=False)
    assert "sso_error" in r.headers["location"]
    # domain not allowed
    state = start(client, sso, email="someone@gmail.com")
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert "sso_error" in r.headers["location"]
    # tenant-portal accounts cannot use SSO
    state = start(client, sso, email="tenant1@adports-demo.local")
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert "sso_error" in r.headers["location"]


def test_sso_bad_nonce_and_audience(client, sso, monkeypatch):
    state = start(client, sso, email="esg.admin@adports-demo.local")
    sso["nonce"] = "someone-elses-nonce"
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert "nonce" in r.headers["location"]


def test_sso_auto_provision_and_role_sync(client, sso, monkeypatch):
    monkeypatch.setenv("ENTRA_AUTO_PROVISION", "true")
    email = f"auto.{uuid.uuid4().hex[:6]}@adports-demo.local"
    state = start(client, sso, email=email, name="Auto User", roles=["AdPorts.Owner"])
    r = client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"].startswith("/#sso=")
    with db.conn() as c:
        assert db.row(c, "SELECT role FROM app_user WHERE email = %s", (email,))["role"] == "data_owner"
    # promoted in Entra -> role follows at next sign-in
    state = start(client, sso, email=email, roles=["AdPorts.Admin"])
    client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False)
    with db.conn() as c:
        assert db.row(c, "SELECT role FROM app_user WHERE email = %s", (email,))["role"] == "esg_admin"
        assert db.row(c, "SELECT 1 AS x FROM audit_log WHERE actor = %s AND action = 'login_sso'", (email,))
    # no mapped role and no default -> refused even with auto-provision
    state = start(client, sso, email=f"x.{uuid.uuid4().hex[:6]}@adports-demo.local", roles=[])
    assert "sso_error" in client.get(f"/api/auth/entra/callback?code=abc&state={state}", follow_redirects=False).headers["location"]


def test_local_login_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("LOCAL_LOGIN_ENABLED", "false")
    assert client.get("/api/auth/config").json()["local"] is False
    r = client.post("/api/auth/login", json={"email": "esg.admin@adports-demo.local", "password": "Demo#2026"})
    assert r.status_code == 403


# ------------------------------------------------------------------ storage
class FakeBlob:
    def __init__(self):
        self.blobs, self.container_name = {}, "evidence"

    def upload_blob(self, name, data, overwrite=True, content_settings=None):
        self.blobs[name] = data

    def download_blob(self, name):
        class D:
            def readall(s, _d=self.blobs[name]): return _d
        return D()


def test_azure_backend_roundtrip_and_mixed_uris(client, H, monkeypatch, tmp_path):
    fake = FakeBlob()
    monkeypatch.setattr(storage, "_client", fake)
    monkeypatch.setenv("STORAGE_BACKEND", "azure")
    r = client.post("/api/evidence", headers=H["owner"], files={"file": ("a.txt", b"hello azure", "text/plain")},
                    data={"description": "azure test"})
    assert r.status_code == 200, r.text
    doc = r.json()["doc_id"]
    with db.conn() as c:
        uri = db.row(c, "SELECT file_uri FROM evidence_document WHERE doc_id = %s", (doc,))["file_uri"]
    assert uri.startswith("az://evidence/") and fake.blobs
    got = client.get(f"/api/evidence/{doc}", headers=H["owner"])
    assert got.status_code == 200 and got.content == b"hello azure"
    # a document stored locally earlier still downloads after the switch
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    p = tmp_path / "old.txt"
    p.write_bytes(b"legacy")
    assert storage.respond(str(p), "text/plain").path == str(p)
    with pytest.raises(Exception):
        storage.respond(str(tmp_path / "missing.txt"))
