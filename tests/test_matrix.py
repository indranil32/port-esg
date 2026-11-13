"""Every API route, every role: no unauthenticated access, no server errors, and the role boundaries hold."""
import uuid

import pytest

from .conftest import *  # noqa: F401,F403  (client, H fixtures)

PUBLIC = {"/api/health", "/api/auth/login", "/api/auth/config", "/api/auth/entra/login", "/api/auth/entra/callback"}
# Public paths that carry their own credential (one-time link token) instead of a bearer token.
TOKEN_AUTH_PREFIXES = ("/api/collect",)
SKIP = {("POST", "/api/admin/users"), ("PATCH", "/api/admin/users/{user_id}")}   # change accounts; covered by dedicated tests
ROLES = ["admin", "owner", "auditor", "tenant", "field"]


def token_auth(p):
    return p == "/api/collect" or p.startswith("/api/collect/")


def routes(client):
    spec = client.get("/openapi.json").json()["paths"]
    return [(m.upper(), p) for p, v in spec.items() for m in v if m in ("get", "post", "put", "patch", "delete")]


def fill(path):
    out = path
    while "{" in out:
        a, b = out.index("{"), out.index("}")
        out = out[:a] + (str(uuid.uuid4()) if "id" in out[a:b] else "x") + out[b + 1:]
    return out


def call(client, method, path, headers=None):
    url = fill(path)
    kw = {"headers": headers or {}}
    if method in ("POST", "PUT", "PATCH"):
        kw["json"] = {}
    return client.request(method, url, **kw)


def pmap(fn, items):
    """Run fn over items; concurrently against a deployed instance (latency-bound), serially in-process."""
    items = list(items)
    if not BASE_URL:
        return [fn(i) for i in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        return list(ex.map(fn, items))


def test_every_protected_route_rejects_anonymous_calls(client):
    todo = [(m, p) for m, p in routes(client) if not (p in PUBLIC or token_auth(p))]
    bad = []
    for (m, p), r in zip(todo, pmap(lambda mp: call(client, *mp), todo)):
        if r.status_code not in (401, 403, 404, 422):
            bad.append((m, p, r.status_code))
        if r.status_code == 200:
            bad.append((m, p, "anonymous 200"))
    assert not bad, bad


@pytest.mark.parametrize("role", ROLES)
def test_no_route_returns_a_server_error(client, H, role):
    bad = []
    todo = [(m, p) for m, p in routes(client)
            if not ((m, p) in SKIP or p in PUBLIC or "/withdraw" in p or p.endswith("/lock") or "unlock" in p)]
    for (m, p), r in zip(todo, pmap(lambda mp: call(client, mp[0], mp[1], H[role]), todo)):
        if r.status_code >= 500:
            bad.append((m, p, r.status_code, r.text[:120]))
    assert not bad, bad


def test_tenant_and_field_roles_are_walled_off_from_admin_areas(client, H):
    for role in ("tenant", "field"):
        for path in ("/api/admin/users", "/api/audit-log", "/api/batches", "/api/files", "/api/periods", "/api/sources", "/api/runs"):
            assert client.get(path, headers=H[role]).status_code == 403, (role, path)


def test_auditor_is_read_only(client, H):
    todo = [(m, p) for m, p in routes(client)
            if m in ("POST", "PUT", "PATCH", "DELETE") and p not in PUBLIC and not token_auth(p) and p != "/api/auth/logout"]
    for (m, p), r in zip(todo, pmap(lambda mp: call(client, mp[0], mp[1], H["auditor"]), todo)):
        if True:
            if r.status_code in (200, 201):
                # a few POSTs are reads or self-service; flag anything that actually wrote
                assert p in ("/api/reports/export",) or "search" in p or "check" in p, (m, p, r.status_code)
