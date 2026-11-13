"""ESG system of record: FastAPI application entry point."""
import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerts, auth, entra, batches, compliance, imports, ems, capture, collect, db, evidence, periods, reports, seed, trace
from .auth import current_user, require

log = logging.getLogger("esg")


async def _init_data(app: FastAPI):
    """Schema and seed run in the background so the port opens at once. Over a slow database
    link the first seed can outlast the platform's startup probe and the container would be
    restarted in a loop."""
    for attempt in range(40):  # the database may still be starting
        try:
            mode = await run_in_threadpool(seed.ensure_initial_data)
            log.warning("Initial data: %s", mode)
            app.state.ready = True
            return
        except Exception as e:
            log.warning("Database not ready (%s); retrying", e)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    init = asyncio.create_task(_init_data(app))
    task = asyncio.create_task(alerts.scheduler())
    yield
    init.cancel()
    task.cancel()
    db.close_pool()


app = FastAPI(title="ESG System of Record", version="1.0", lifespan=lifespan)
@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag the request so audit rows carry its id and client address; echo the id to the caller."""
    rid = uuid.uuid4().hex[:16]
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()      # behind a trusted reverse proxy only
    db.REQUEST_CTX.set({"request_id": rid, "ip": fwd or (request.client.host if request.client else None)})
    resp = await call_next(request)
    resp.headers["X-Request-ID"] = rid
    return resp


app.include_router(capture.router)
app.include_router(evidence.router)
app.include_router(periods.router)
app.include_router(batches.router)
app.include_router(ems.router)
app.include_router(compliance.router)
app.include_router(imports.router)
app.include_router(imports.imp)
app.include_router(trace.router)
app.include_router(collect.admin)
app.include_router(collect.public)
app.include_router(reports.router)
app.include_router(alerts.router)
app.include_router(entra.router)


@app.get("/api/health")
def health():
    if not getattr(app.state, "ready", False):
        return {"status": "starting"}
    with db.conn() as c:
        db.row(c, "SELECT 1")
    return {"status": "ok"}


# ------------------------------------------------------------------ auth
class Login(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: Login):
    if not entra.local_login_enabled():
        raise HTTPException(403, "Password sign-in is turned off. Use Microsoft sign-in.")
    with db.conn() as c:
        u = db.row(c, "SELECT * FROM app_user WHERE lower(email) = lower(%s)", (body.email.strip(),))
        if not u or not u["active"] or not auth.verify_password(body.password, u["password_hash"]):
            db.audit(c, body.email[:100], "login_failed")
            c.commit()
            raise HTTPException(401, "Invalid email or password")
        c.execute("UPDATE app_user SET last_login = now() WHERE user_id = %s", (u["user_id"],))
        db.audit(c, u["email"], "login")
    return {"token": auth.make_token(u["user_id"]),
            "user": {k: u[k] for k in ("user_id", "email", "full_name", "role", "tenant_id")}}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return user


# ------------------------------------------------------------------ admin
@app.get("/api/sources")
def sources(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        return db.rows(c, "SELECT code, name, source_class, ingestion_mode, refresh_frequency, owner, active FROM source_system ORDER BY source_class, code")


@app.get("/api/admin/users")
def users(user: dict = Depends(require("esg_admin"))):
    with db.conn() as c:
        return db.rows(c, "SELECT u.user_id, u.email, u.full_name, u.role, u.active, u.last_login, t.name AS tenant "
                          "FROM app_user u LEFT JOIN tenant t ON t.tenant_id = u.tenant_id ORDER BY u.role, u.email")


class NewUser(BaseModel):
    email: str
    full_name: str = ""
    role: str
    password: str
    tenant_id: str | None = None


@app.post("/api/admin/users")
def create_user(body: NewUser, user: dict = Depends(require("esg_admin"))):
    if body.role not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {auth.ROLES}")
    if len(body.password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    if body.role == "tenant" and not body.tenant_id:
        raise HTTPException(400, "tenant users need a tenant_id")
    with db.conn() as c:
        if db.row(c, "SELECT 1 FROM app_user WHERE lower(email) = lower(%s)", (body.email,)):
            raise HTTPException(409, "Email already registered")
        r = db.row(c, "INSERT INTO app_user (email, full_name, role, password_hash, tenant_id) VALUES (%s,%s,%s,%s,%s) "
                      "RETURNING user_id, email, role", (body.email.strip(), body.full_name, body.role,
                                                         auth.hash_password(body.password), body.tenant_id))
        db.audit(c, user["email"], "insert", "app_user", r["user_id"], after=r)
        return r


class UserPatch(BaseModel):
    active: bool | None = None
    role: str | None = None
    password: str | None = None


@app.patch("/api/admin/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, user: dict = Depends(require("esg_admin"))):
    with db.conn() as c:
        if body.role is not None and body.role not in auth.ROLES:
            raise HTTPException(400, f"role must be one of {auth.ROLES}")
        if str(user["user_id"]) == user_id and (body.active is False or (body.role and body.role != "esg_admin")):
            raise HTTPException(400, "You cannot demote or disable your own account")
        sets, params = [], []
        if body.active is not None:
            sets.append("active = %s"); params.append(body.active)
        if body.role is not None:
            sets.append("role = %s"); params.append(body.role)
        if body.password is not None:
            if len(body.password) < 10:
                raise HTTPException(400, "Password must be at least 10 characters")
            sets.append("password_hash = %s"); params.append(auth.hash_password(body.password))
        if not sets:
            raise HTTPException(400, "Nothing to change")
        r = db.row(c, f"UPDATE app_user SET {', '.join(sets)} WHERE user_id = %s RETURNING user_id, email, role, active", params + [user_id])
        if not r:
            raise HTTPException(404, "User not found")
        db.audit(c, user["email"], "update", "app_user", user_id, after={k: v for k, v in r.items()}, detail="password changed" if body.password else None)
        return r


@app.get("/api/audit-log")
def audit_log(actor: str | None = None, table: str | None = None, action: str | None = None, limit: int = Query(200, le=1000),
              user: dict = Depends(require("esg_admin", "auditor"))):
    with db.conn() as c:
        return db.rows(c, """
            SELECT audit_id, ts, actor, action, table_name, record_id, detail, ip, request_id, before, after FROM audit_log
            WHERE (%s::text IS NULL OR actor ILIKE '%%' || %s || '%%') AND (%s::text IS NULL OR table_name = %s)
              AND (%s::text IS NULL OR action = %s) ORDER BY audit_id DESC LIMIT %s""",
                       (actor, actor, table, table, action, action, limit))


# ------------------------------------------------------------------ UI (must be mounted last)
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="ui")
