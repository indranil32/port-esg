"""Microsoft Entra ID (Azure AD) single sign-on, OpenID Connect authorization-code flow with PKCE.

Everything is configured by environment variables, so pointing the app at a different tenant
(your own now, the company's later) needs no code change:

  ENTRA_TENANT_ID        directory (tenant) id, required to enable SSO
  ENTRA_CLIENT_ID        app registration (client) id
  ENTRA_CLIENT_SECRET    client secret (omit for a public client)
  ENTRA_REDIRECT_URI     https://<host>/api/auth/entra/callback   (default: derived from the request)
  ENTRA_AUTHORITY_HOST   login.microsoftonline.com (sovereign clouds differ)
  ENTRA_ROLE_MAP         "AdPorts.Admin=esg_admin,AdPorts.Owner=data_owner,..." maps Entra app roles or group ids to roles
  ENTRA_DEFAULT_ROLE     role for a signed-in user with no mapped role (blank = refuse sign-in)
  ENTRA_AUTO_PROVISION   true to create the user on first sign-in; false = user must already exist
  ENTRA_ALLOWED_DOMAINS  comma list of email domains accepted (blank = any in the tenant)
  LOCAL_LOGIN_ENABLED    keep the email+password form (default true; set false once SSO is live)

MFA and conditional access are enforced by Entra before the browser comes back here.
Tenant-role users (tenant portal) are never created through SSO; they keep local accounts.
"""
import base64
import hashlib
import os
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import auth, db

router = APIRouter(prefix="/api/auth")
COOKIE = "esg_sso"
_jwks_cache: dict = {}


def cfg() -> dict:
    return {
        "tenant": os.environ.get("ENTRA_TENANT_ID", ""),
        "client_id": os.environ.get("ENTRA_CLIENT_ID", ""),
        "secret": os.environ.get("ENTRA_CLIENT_SECRET", ""),
        "host": os.environ.get("ENTRA_AUTHORITY_HOST", "login.microsoftonline.com"),
        "redirect": os.environ.get("ENTRA_REDIRECT_URI", ""),
        "default_role": os.environ.get("ENTRA_DEFAULT_ROLE", ""),
        "auto": os.environ.get("ENTRA_AUTO_PROVISION", "false").lower() == "true",
        "domains": [d.strip().lower() for d in os.environ.get("ENTRA_ALLOWED_DOMAINS", "").split(",") if d.strip()],
        "role_map": dict(p.split("=", 1) for p in os.environ.get("ENTRA_ROLE_MAP", "").split(",") if "=" in p),
    }


def enabled() -> bool:
    c = cfg()
    return bool(c["tenant"] and c["client_id"])


def local_login_enabled() -> bool:
    return os.environ.get("LOCAL_LOGIN_ENABLED", "true").lower() != "false"


def _authority(c) -> str:
    return f"https://{c['host']}/{c['tenant']}"


def _redirect_uri(request: Request, c) -> str:
    return c["redirect"] or str(request.url_for("entra_callback"))


def _signing_key(c, token: str):
    """Public key for the id_token, from the tenant's published JWKS (cached for an hour)."""
    kid = jwt.get_unverified_header(token).get("kid")
    hit = _jwks_cache.get(kid)
    if hit and hit[0] > time.time():
        return hit[1]
    keys = httpx.get(f"{_authority(c)}/discovery/v2.0/keys", timeout=10).json()["keys"]
    for k in keys:
        _jwks_cache[k["kid"]] = (time.time() + 3600, jwt.PyJWK(k).key)
    if kid not in _jwks_cache:
        raise HTTPException(401, "Sign-in token signed by an unknown key")
    return _jwks_cache[kid][1]


def _exchange(c, code: str, verifier: str, redirect: str) -> dict:
    data = {"client_id": c["client_id"], "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect, "code_verifier": verifier, "scope": "openid profile email"}
    if c["secret"]:
        data["client_secret"] = c["secret"]
    r = httpx.post(f"{_authority(c)}/oauth2/v2.0/token", data=data, timeout=15)
    if r.status_code != 200:
        raise HTTPException(401, "Microsoft sign-in was refused: " + r.json().get("error_description", "token exchange failed")[:200])
    return r.json()


def pick_role(c, claims: dict) -> str | None:
    wanted = set(claims.get("roles") or []) | set(claims.get("groups") or [])
    for k, role in c["role_map"].items():
        if k in wanted and role in auth.ROLES and role != "tenant":
            return role
    return c["default_role"] if c["default_role"] in auth.ROLES and c["default_role"] != "tenant" else None


@router.get("/config")
def auth_config():
    return {"entra": enabled(), "local": local_login_enabled(), "entra_login_url": "/api/auth/entra/login"}


@router.get("/entra/login")
def entra_login(request: Request):
    if not enabled():
        raise HTTPException(404, "Single sign-on is not configured")
    c = cfg()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    q = urlencode({"client_id": c["client_id"], "response_type": "code", "redirect_uri": _redirect_uri(request, c),
                   "response_mode": "query", "scope": "openid profile email", "state": state, "nonce": nonce,
                   "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(f"{_authority(c)}/oauth2/v2.0/authorize?{q}", status_code=302)
    resp.set_cookie(COOKIE, f"{state}.{nonce}.{verifier}", max_age=600, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


@router.get("/entra/callback", name="entra_callback")
def entra_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    """Browser lands here from Microsoft; failures go back to the login page with a message."""
    try:
        return _callback(request, code, state, error, error_description)
    except HTTPException as e:
        r = RedirectResponse("/#sso_error=" + quote(str(e.detail)), status_code=302)
        r.delete_cookie(COOKIE)
        return r


def _callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if not enabled():
        raise HTTPException(404, "Single sign-on is not configured")
    if error:
        raise HTTPException(401, f"Microsoft sign-in failed: {error_description or error}"[:300])
    parts = (request.cookies.get(COOKIE) or "").split(".")
    if len(parts) != 3 or not secrets.compare_digest(parts[0], state or "-"):
        raise HTTPException(400, "Sign-in session expired or did not start here. Start again.")
    _, nonce, verifier = parts
    c = cfg()
    tok = _exchange(c, code, verifier, _redirect_uri(request, c))
    try:
        claims = jwt.decode(tok["id_token"], _signing_key(c, tok["id_token"]), algorithms=["RS256"],
                            audience=c["client_id"], issuer=f"{_authority(c)}/v2.0")
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Sign-in token rejected: {e}")
    if claims.get("nonce") != nonce:
        raise HTTPException(401, "Sign-in token rejected: nonce mismatch")
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(401, "Your Microsoft account did not provide an email address")
    if c["domains"] and email.split("@")[1] not in c["domains"]:
        raise HTTPException(403, "This email domain is not allowed")
    role = pick_role(c, claims)
    with db.conn() as c2:
        u = db.row(c2, "SELECT * FROM app_user WHERE lower(email) = %s", (email,))
        if u and u["role"] == "tenant":
            raise HTTPException(403, "Tenant accounts sign in with their portal password")
        if not u:
            if not (c["auto"] and role):
                db.audit(c2, email, "sso_denied")
                c2.commit()
                raise HTTPException(403, "No account for this user. Ask an ESG admin to add you.")
            u = db.row(c2, "INSERT INTO app_user (email, full_name, role, password_hash) VALUES (%s,%s,%s,'sso$') RETURNING *",
                       (email, claims.get("name"), role))
            db.audit(c2, email, "insert", "app_user", u["user_id"], after={"email": email, "role": role, "via": "sso"})
        elif not u["active"]:
            raise HTTPException(403, "This account is disabled")
        elif role and role != u["role"] and c["role_map"]:
            # Entra is the source of truth for roles whenever a role map is configured
            c2.execute("UPDATE app_user SET role = %s WHERE user_id = %s", (role, u["user_id"]))
            db.audit(c2, email, "update", "app_user", u["user_id"], before={"role": u["role"]}, after={"role": role, "via": "sso"})
            u["role"] = role
        c2.execute("UPDATE app_user SET last_login = now() WHERE user_id = %s", (u["user_id"],))
        db.audit(c2, email, "login_sso")
    resp = RedirectResponse(f"/#sso={auth.make_token(u['user_id'])}", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp
