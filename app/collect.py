"""Third-party data collection by link. An administrator creates an expiring single-use link for one tenant or
contractor and a list of requested metrics; the recipient opens it without an account, sees their last submitted
values as pre-fill, confirms or edits, optionally attaches a file, and submits. Submissions land as pending rows in
tenant_esg_submission and go through the same approval as everything else.

Only a SHA-256 of the token is stored. The token travels in a header, never in a URL path, so it stays out of access
logs. Transport security (HTTPS) is the deployment's job."""
import hashlib
import json
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from . import db
from .auth import require
from . import storage
from .capture import ingest_records

admin = APIRouter(prefix="/api/collect-links")
public = APIRouter(prefix="/api/collect")
MAX_FILE = 10 * 1024 * 1024
_hits: dict[str, deque] = defaultdict(deque)


def _limit(request: Request, per_minute=30):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?"))
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Too many requests; try again in a minute")
    q.append(now)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- admin side
class LinkBody(BaseModel):
    tenant_id: str
    metric_ids: list[str]
    period_start: date
    period_end: date
    days_valid: int = 14
    message: str | None = None


@admin.post("")
def create_link(body: LinkBody, user: dict = Depends(require("esg_admin", "data_owner"))):
    if body.period_end < body.period_start:
        raise HTTPException(400, "Period end is before its start")
    if not 1 <= body.days_valid <= 90 or not 1 <= len(body.metric_ids) <= 50:
        raise HTTPException(400, "Valid for 1-90 days, and 1-50 metrics")
    token = secrets.token_urlsafe(32)
    with db.conn() as c:
        if not db.row(c, "SELECT 1 FROM tenant WHERE tenant_id::text = %s", (body.tenant_id,)):
            raise HTTPException(404, "Unknown tenant")
        ok = db.rows(c, "SELECT metric_id FROM metric WHERE metric_id::text = ANY(%s) AND 'E_third_party' = ANY (source_classes)", (body.metric_ids,))
        if len(ok) != len(set(body.metric_ids)):
            raise HTTPException(400, "Every metric must exist and be one that third parties can submit")
        lk = db.row(c, "INSERT INTO collection_link (token_hash, tenant_id, metric_ids, period_start, period_end, message, created_by, expires_at) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING link_id, expires_at",
                    (_hash(token), body.tenant_id, [m["metric_id"] for m in ok], body.period_start, body.period_end, body.message,
                     user["email"], datetime.now(timezone.utc) + timedelta(days=body.days_valid)))
        db.audit(c, user["email"], "create_collection_link", "collection_link", lk["link_id"],
                 after={"tenant_id": body.tenant_id, "metrics": len(ok), "expires_at": lk["expires_at"]})
    return {"link_id": lk["link_id"], "token": token, "path": f"/collect.html#{token}", "expires_at": lk["expires_at"]}


@admin.get("/metrics")
def requestable_metrics(user: dict = Depends(require("esg_admin", "data_owner"))):
    with db.conn() as c:
        return db.rows(c, "SELECT metric_id AS id, metric_code || ' - ' || name AS label FROM metric WHERE 'E_third_party' = ANY (source_classes) ORDER BY metric_code")


@admin.get("")
def list_links(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        return db.rows(c, """
            SELECT l.link_id, t.name AS tenant, l.period_start, l.period_end, cardinality(l.metric_ids) AS metrics, l.created_by,
                   l.created_at, l.expires_at, l.used_at, l.revoked_at, l.submission_count,
                   CASE WHEN l.revoked_at IS NOT NULL THEN 'revoked' WHEN l.used_at IS NOT NULL THEN 'submitted'
                        WHEN l.expires_at < now() THEN 'expired' ELSE 'open' END AS status
            FROM collection_link l JOIN tenant t ON t.tenant_id = l.tenant_id ORDER BY l.created_at DESC LIMIT 200""")


@admin.post("/{link_id}/revoke")
def revoke(link_id: str, user: dict = Depends(require("esg_admin", "data_owner"))):
    with db.conn() as c:
        r = db.row(c, "UPDATE collection_link SET revoked_at = now() WHERE link_id::text = %s AND revoked_at IS NULL AND used_at IS NULL "
                      "RETURNING link_id", (link_id,))
        if not r:
            raise HTTPException(404, "No open link with that id")
        db.audit(c, user["email"], "revoke_collection_link", "collection_link", r["link_id"])
    return {"ok": True}


# --------------------------------------------------------------------------- recipient side (no login)
def _open_link(c, token, lock=False):
    lk = db.row(c, f"SELECT * FROM collection_link WHERE token_hash = %s {'FOR UPDATE' if lock else ''}", (_hash(token or ""),))
    # one message for every failure so the endpoint does not reveal which tokens exist
    if not lk or lk["revoked_at"] or lk["used_at"] or lk["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(404, "This link is not valid. It may have expired or already been used; ask the sender for a new one.")
    return lk


@public.get("")
def view(request: Request, response: Response, x_collect_token: str | None = Header(None)):
    _limit(request)
    response.headers["Cache-Control"] = "no-store"
    with db.conn() as c:
        lk = _open_link(c, x_collect_token)
        tenant = db.row(c, "SELECT name FROM tenant WHERE tenant_id = %s", (lk["tenant_id"],))
        metrics = db.rows(c, "SELECT metric_id, metric_code, name, unit, frequency FROM metric WHERE metric_id = ANY(%s) ORDER BY metric_code",
                          (lk["metric_ids"],))
        for m in metrics:       # pre-fill: the most recent value this tenant gave for the metric, to confirm or change
            last = db.row(c, "SELECT value, uom, period_end FROM tenant_esg_submission WHERE tenant_id = %s AND metric_id = %s "
                             "AND validation_status <> 'rejected' ORDER BY period_end DESC, ingested_at DESC LIMIT 1",
                          (lk["tenant_id"], m["metric_id"]))
            m["prefill"] = last
    return {"tenant": tenant["name"], "message": lk["message"], "period_start": lk["period_start"], "period_end": lk["period_end"],
            "expires_at": lk["expires_at"], "metrics": metrics}


@public.post("")
def submit(request: Request, response: Response, entries: str = Form(...), file: UploadFile | None = File(None),
           x_collect_token: str | None = Header(None)):
    _limit(request, 10)
    response.headers["Cache-Control"] = "no-store"
    try:
        items = json.loads(entries)
        assert isinstance(items, list) and items
    except Exception:
        raise HTTPException(400, "entries must be a JSON list of {metric_id, value, uom}")
    data = file.file.read(MAX_FILE + 1) if file else None
    if data is not None and len(data) > MAX_FILE:
        raise HTTPException(413, "Attachment too large (10 MB max)")
    with db.conn() as c:
        lk = _open_link(c, x_collect_token, lock=True)
        allowed = {str(m) for m in lk["metric_ids"]}
        recs = []
        for it in items:
            if str(it.get("metric_id")) not in allowed:
                raise HTTPException(400, "A value was sent for a metric that was not requested")
            if it.get("value") in (None, ""):
                continue                                        # skipped rows are simply not submitted
            rec = {"tenant_id": str(lk["tenant_id"]), "metric_id": str(it["metric_id"]), "period_start": str(lk["period_start"]),
                   "period_end": str(lk["period_end"]), "value": it["value"], "status": "submitted"}
            if it.get("uom"):
                rec["uom"] = str(it["uom"])[:20]
            recs.append(rec)
        if not recs:
            raise HTTPException(400, "Enter at least one value")
        label = f"link:{str(lk['link_id'])[:8]}"
        if data:
            import hashlib as h
            sha = h.sha256(data).hexdigest()
            name = "".join(ch for ch in (file.filename or "file") if ch.isalnum() or ch in "._-")[:60] or "file"
            path = storage.save(f"{sha[:16]}_{name}", data, file.content_type)
            doc = db.row(c, "INSERT INTO evidence_document (file_uri, sha256, mime_type, description, uploaded_by) VALUES (%s,%s,%s,%s,%s) RETURNING doc_id",
                         (str(path), sha, file.content_type, f"Attachment sent through collection link {label}", label))
            for r in recs:
                r["evidence_doc_id"] = str(doc["doc_id"])
        res = ingest_records(c, {"email": label, "role": "tenant", "tenant_id": lk["tenant_id"]}, "tenant_esg_submission", recs, "TENANT_PORTAL")
        if res["errors"]:
            # nothing is consumed: the recipient can correct and resend with the same link
            raise HTTPException(422, "; ".join(f"row {e['index'] + 1}: {e['error']}" for e in res["errors"][:5]))
        c.execute("UPDATE collection_link SET used_at = now(), submission_count = %s WHERE link_id = %s", (res["inserted"], lk["link_id"]))
        db.audit(c, label, "link_submission", "collection_link", lk["link_id"], after={"inserted": res["inserted"], "duplicates": res["duplicates"]})
    return {"received": len(recs), "saved": res["inserted"], "duplicates": res["duplicates"]}
