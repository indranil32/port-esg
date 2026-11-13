"""Password hashing, signed bearer tokens and role checks (standard library only)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Depends, Header, HTTPException

from . import db

SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me").encode()
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_SECONDS", 8 * 3600))
ROLES = ("esg_admin", "data_owner", "field_operator", "tenant", "auditor")


def hash_password(pw: str, salt: str | None = None, iters: int = 120_000) -> str:
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), iters).hex()
    return f"pbkdf2${iters}${salt}${dk}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, dk = stored.split("$")
        cand = hash_password(pw, salt, int(iters)).split("$")[3]
        return hmac.compare_digest(cand, dk)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign(body: str) -> str:
    return _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())


def make_token(user_id: str) -> str:
    body = _b64(json.dumps({"sub": str(user_id), "exp": int(time.time()) + TOKEN_TTL}).encode())
    return f"{body}.{_sign(body)}"


def read_token(token: str) -> str | None:
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return payload["sub"] if payload["exp"] > time.time() else None
    except Exception:
        return None


def current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not authenticated")
    uid = read_token(authorization[7:])
    if not uid:
        raise HTTPException(401, "Invalid or expired token")
    with db.conn() as c:
        u = db.row(c, "SELECT user_id, email, full_name, role, tenant_id, active FROM app_user WHERE user_id = %s", (uid,))
    if not u or not u["active"]:
        raise HTTPException(401, "User disabled or unknown")
    return u


def require(*roles):
    """Dependency factory: user must hold one of the roles."""
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, f"Requires role: {', '.join(roles)}")
        return user
    return dep
