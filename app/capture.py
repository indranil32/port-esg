"""Data capture: schema-validated ingestion (JSON, CSV, UI forms), record browsing and
corrections, evidence upload, and the four-eyes validation workflow. Every write is audited."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from . import db, evidence
from .auth import current_user, require

router = APIRouter(prefix="/api")

SCHEMAS = json.load(open(Path(__file__).parent / "esg_schemas.json"))["$defs"]
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", "./data/evidence"))

GROUPS = [
    ("Operations", ["vessel", "berth", "vessel_call", "asset", "container_move", "gate_transaction", "ais_position"]),
    ("Energy", ["meter", "meter_register_reading", "energy_meter_reading", "fuel_transaction", "shore_power_session", "refrigerant_event"]),
    ("Environment", ["sensor", "air_quality_reading", "water_quality_reading", "noise_reading",
                     "weather_observation", "environmental_incident"]),
    ("People & safety", ["employee_snapshot", "training_record", "hours_worked", "safety_incident"]),
    ("Governance & finance", ["spend_record", "utility_invoice", "risk", "policy", "audit_finding", "certification"]),
    ("Third parties & community", ["tenant_esg_submission", "shipping_line_data", "waste_manifest",
                                   "community_grievance", "survey_response"]),
    ("Reference & setup", ["entity", "tenant", "emission_factor", "kpi_target"]),
]
ADMIN_ONLY = {"entity", "tenant", "emission_factor", "kpi_target"}
INGESTIBLE = {t for _, ts in GROUPS for t in ts}
GROUP_OF = {t: g for g, ts in GROUPS for t in ts}
CLIENT_FORBIDDEN = {"validation_status", "approved_by", "ingested_at", "created_by"}
SUBMISSION_FORBIDDEN = {"reviewer", "review_date"}
# What a field operator may record from the plant floor, and which reference lists they may pick from.
FIELD_TABLES = {"meter_register_reading", "fuel_transaction", "refrigerant_event", "waste_manifest", "environmental_incident"}
FIELD_LOOKUPS = {"meter", "asset", "entity", "vessel", "berth", "sensor", "evidence_document"}

LABELS = {  # SQL expression giving a human label for dropdowns
    "entity": "code || ' - ' || name", "tenant": "name", "vessel": "imo_number || ' ' || vessel_name",
    "berth": "berth_code", "asset": "asset_code", "meter": "meter_code", "sensor": "sensor_code",
    "metric": "metric_code || ' - ' || name", "evidence_document": "coalesce(description, file_uri)",
    "vessel_call": "imo_number || ' / ' || coalesce(voyage_ref, call_id::text)",
    "framework": "code", "framework_requirement": "disclosure_code || ' ' || coalesce(title, '')",
    "kpi_target": "target_id::text", "calc_run": "run_id::text", "emission_factor": "activity_type || ' / ' || coalesce(fuel_or_grid,'')",
    "ghg_activity_data": "activity_type",
}


def can_write(user, table):
    if table not in INGESTIBLE:
        return False
    r = user["role"]
    if r == "esg_admin":
        return True
    if r == "data_owner":
        return table not in ADMIN_ONLY
    if r == "tenant":
        return table == "tenant_esg_submission"
    if r == "field_operator":
        return table in FIELD_TABLES
    return False


def can_read(user, table):
    if table not in INGESTIBLE:
        return False
    if user["role"] == "field_operator":
        return table in FIELD_TABLES
    return user["role"] != "tenant" or table == "tenant_esg_submission"


def check_table(table):
    if table not in SCHEMAS or table not in INGESTIBLE:
        raise HTTPException(404, f"Unknown table: {table}")


def pk_cols(c, table):
    return db.primary_keys(c)[table]


def id_expr(c, table):
    return "concat_ws('|', " + ", ".join(f"{k}::text" for k in pk_cols(c, table)) + ")"


_validators = {}


def validator(table):
    if table not in _validators:
        _validators[table] = Draft202012Validator(SCHEMAS[table], format_checker=FormatChecker())
    return _validators[table]


def pg_message(e: psycopg.Error) -> str:
    return (getattr(e.diag, "message_primary", None) or str(e)).strip() + \
           (f" ({e.diag.constraint_name})" if getattr(e.diag, "constraint_name", None) else "")


# --------------------------------------------------------------------------- catalog
@router.get("/tables")
def tables(user: dict = Depends(current_user)):
    out = []
    for g, ts in GROUPS:
        items = [{"table": t, "writable": can_write(user, t)} for t in ts if can_read(user, t) or can_write(user, t)]
        if items:
            out.append({"group": g, "tables": items})
    return out


@router.get("/schema/{table}")
def schema(table: str, user: dict = Depends(current_user)):
    check_table(table)
    if not (can_read(user, table) or can_write(user, table)):
        raise HTTPException(403, "No access to this table")
    with db.conn() as c:
        return {"table": table, "group": GROUP_OF[table], "pk": pk_cols(c, table),
                "writable": can_write(user, table), "schema": SCHEMAS[table]}


@router.get("/lookup/{table}")
def lookup(table: str, user: dict = Depends(current_user)):
    if table not in SCHEMAS and table != "evidence_document":
        raise HTTPException(404)
    with db.conn() as c:
        pk = db.primary_keys(c)[table][0]
        label = LABELS.get(table, f"{pk}::text")
        where = ""
        if user["role"] == "field_operator" and table not in FIELD_LOOKUPS:
            raise HTTPException(403, "Field operators can only look up meters, assets, entities and similar site lists")
        if user["role"] == "tenant":
            if table != "metric":
                raise HTTPException(403, "Tenants can only look up submittable metrics")
            where = "WHERE 'E_third_party' = ANY (source_classes)"
        data = db.rows(c, f"SELECT {pk}::text AS id, {label} AS label FROM esg.{table} {where} ORDER BY 2 LIMIT 2000")
    return data


# --------------------------------------------------------------------------- ingestion
def coerce(prop: dict, text: str):
    t = prop.get("type")
    t = [x for x in t if x != "null"][0] if isinstance(t, list) else t
    text = text.strip()
    if t == "integer":
        return int(float(text))
    if t == "number":
        return float(text)
    if t == "boolean":
        low = text.lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
        raise ValueError(f"not a boolean: {text}")
    if t == "array":
        return [x.strip() for x in text.split("|") if x.strip()]
    return text


def ingest_records(c, user, table, records, source_code):
    props = SCHEMAS[table]["properties"]
    ss = None
    if "source_system_id" in props:
        ss = db.row(c, "SELECT source_system_id FROM source_system WHERE code = %s", (source_code,))
        if not ss:
            raise HTTPException(400, f"Unknown source_system_code '{source_code}'")
    pks = pk_cols(c, table)
    inserted, errors, dups = [], [], 0
    for i, raw in enumerate(records):
        rec = {k: v for k, v in dict(raw).items() if k not in CLIENT_FORBIDDEN}
        if table == "tenant_esg_submission":
            for k in SUBMISSION_FORBIDDEN:
                rec.pop(k, None)
            if rec.get("status") not in (None, "draft", "submitted"):
                errors.append({"index": i, "error": "status can only be draft or submitted; approval is a separate step"})
                continue
            if user["role"] == "tenant":
                rec["tenant_id"] = str(user["tenant_id"])
        errs = sorted(validator(table).iter_errors(rec), key=lambda e: list(e.path))
        # server-side defaults are applied by the database, so 'required' only lists true inputs
        if errs:
            errors.append({"index": i, "error": "; ".join(
                f"{'.'.join(map(str, e.path)) or 'record'}: {e.message}" for e in errs[:5])})
            continue
        if ss:
            rec["source_system_id"] = str(ss["source_system_id"])
        if "created_by" in props:
            rec["created_by"] = user["email"]
        cols = list(rec)
        try:
            with c.transaction():  # savepoint: one bad record does not sink the batch
                res = db.row(
                    c, f"INSERT INTO esg.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                       f"ON CONFLICT DO NOTHING RETURNING *", [rec[k] for k in cols])
                if res is None:
                    dups += 1
                    continue
                rid = "|".join(str(res[k]) for k in pks)
                db.audit(c, user["email"], "insert", table, rid, after=res, detail=f"via {source_code or 'api'}")
                inserted.append(rid)
        except psycopg.Error as e:
            errors.append({"index": i, "error": pg_message(e)})
    return {"table": table, "received": len(records), "inserted": len(inserted), "duplicates": dups,
            "errors": errors, "ids": inserted[:200]}


class IngestBody(BaseModel):
    source_system_code: str = "API"
    records: list[dict]


@router.post("/ingest/{table}")
def ingest(table: str, body: IngestBody, user: dict = Depends(current_user)):
    check_table(table)
    if not can_write(user, table):
        raise HTTPException(403, f"Your role cannot write to {table}")
    if not body.records or len(body.records) > 20000:
        raise HTTPException(400, "Send between 1 and 20000 records per call")
    with db.conn() as c:
        return ingest_records(c, user, table, body.records, body.source_system_code)


@router.post("/ingest/{table}/csv")
async def ingest_csv(table: str, file: UploadFile = File(...), user: dict = Depends(current_user)):
    check_table(table)
    if not can_write(user, table):
        raise HTTPException(403, f"Your role cannot write to {table}")
    text = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    props = SCHEMAS[table]["properties"]
    unknown = [h for h in (reader.fieldnames or []) if h not in props]
    if unknown:
        raise HTTPException(400, f"Unknown columns: {', '.join(unknown)}. Allowed: {', '.join(props)}")
    records, errors = [], []
    for i, line in enumerate(reader):
        rec = {}
        try:
            for k, v in line.items():
                if v is not None and v.strip() != "":
                    rec[k] = coerce(props[k], v)
            records.append(rec)
        except ValueError as e:
            errors.append({"index": i, "error": f"row {i + 2}: {e}"})
            records.append({})  # keep indexes aligned; empty record fails validation below
    if not records or len(records) > 50000:
        raise HTTPException(400, "CSV must contain between 1 and 50000 rows")
    with db.conn() as c:
        res = ingest_records(c, user, table, records, "CSV")
    res["errors"] = errors + [e for e in res["errors"] if e["index"] not in {x["index"] for x in errors}]
    return res


# --------------------------------------------------------------------------- browse / correct
@router.get("/records/{table}")
def records(table: str, request: Request, limit: int = Query(50, le=500), offset: int = 0,
            order: str | None = None, user: dict = Depends(current_user)):
    check_table(table)
    if not can_read(user, table):
        raise HTTPException(403, "No access to this table")
    props = SCHEMAS[table]["properties"]
    reserved = {"limit", "offset", "order"}
    where, params = [], []
    for k, v in request.query_params.items():
        if k in reserved:
            continue
        if k not in props:
            raise HTTPException(400, f"Cannot filter on unknown column {k}")
        where.append(f"{k}::text = %s")
        params.append(v)
    if user["role"] == "tenant":
        where.append("tenant_id = %s")
        params.append(str(user["tenant_id"]))
    if user["role"] == "field_operator":          # operators see only what they captured themselves
        where.append("created_by = %s")
        params.append(user["email"])
    with db.conn() as c:
        pks = pk_cols(c, table)
        default = "ingested_at DESC" if "ingested_at" in props else ", ".join(pks)
        if order:
            col = order.lstrip("-")
            if col not in props:
                raise HTTPException(400, f"Cannot order by {col}")
            default = f"{col} {'DESC' if order.startswith('-') else 'ASC'}"
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = db.row(c, f"SELECT count(*) AS n FROM esg.{table} {w}", params)["n"]
        data = db.rows(c, f"SELECT *, {id_expr(c, table)} AS _id FROM esg.{table} {w} ORDER BY {default} LIMIT %s OFFSET %s",
                       params + [limit, offset])
    return {"total": total, "rows": data}


@router.patch("/records/{table}/{rid}")
def correct(table: str, rid: str, body: dict, user: dict = Depends(current_user)):
    """Correct a record. The before/after images go to the audit log and the row returns to 'pending'."""
    check_table(table)
    if not can_write(user, table):
        raise HTTPException(403, f"Your role cannot write to {table}")
    props = SCHEMAS[table]["properties"]
    with db.conn() as c:
        pks = pk_cols(c, table)
        if len(pks) != 1:
            raise HTTPException(400, "Composite-key rows cannot be edited")
        changes = {k: v for k, v in body.items() if k not in CLIENT_FORBIDDEN and k not in pks}
        bad = [k for k in changes if k not in props]
        if bad or not changes:
            raise HTTPException(400, f"Unknown or empty fields: {bad}")
        if table == "tenant_esg_submission":
            changes = {k: v for k, v in changes.items() if k not in SUBMISSION_FORBIDDEN}
        for k, v in changes.items():
            sub = {"type": "object", "properties": {k: props[k]}}
            errs = list(Draft202012Validator(sub, format_checker=FormatChecker()).iter_errors({k: v}))
            if errs:
                raise HTTPException(422, f"{k}: {errs[0].message}")
        before = db.row(c, f"SELECT * FROM esg.{table} WHERE {pks[0]}::text = %s FOR UPDATE", (rid,))
        if not before:
            raise HTTPException(404, "Record not found")
        if user["role"] == "tenant":
            if str(before["tenant_id"]) != str(user["tenant_id"]) or before["status"] not in ("draft", "rejected"):
                raise HTTPException(403, "Only your own draft or rejected submissions can be edited")
            if changes.get("status") not in (None, "draft", "submitted"):
                raise HTTPException(403, "Invalid status")
            changes["status"] = changes.get("status", "submitted")
        if user["role"] == "field_operator" and (before.get("created_by") != user["email"]
                                                 or before.get("validation_status") not in ("pending", "rejected")):
            raise HTTPException(403, "You can only correct your own readings that are pending or were rejected")
        if "validation_status" in props:
            changes["validation_status"] = "pending"
            changes["approved_by"] = None
        sets = ", ".join(f"{k} = %s" for k in changes)
        try:
            after = db.row(c, f"UPDATE esg.{table} SET {sets} WHERE {pks[0]}::text = %s RETURNING *",
                           list(changes.values()) + [rid])
        except psycopg.Error as e:
            raise HTTPException(422, pg_message(e))
        db.audit(c, user["email"], "update", table, rid, before=before, after=after)
        evidence.after_correction(c, table, before, user["email"])
        return after


# --------------------------------------------------------------------------- evidence
@router.post("/evidence")
async def upload_evidence(file: UploadFile = File(...), description: str = "", user: dict = Depends(current_user)):
    if user["role"] == "auditor":
        raise HTTPException(403, "Auditors are read-only")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "File too large (25 MB max)")
    sha = hashlib.sha256(data).hexdigest()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in (file.filename or "file") if ch.isalnum() or ch in "._-") or "file"
    path = EVIDENCE_DIR / f"{sha[:16]}_{safe}"
    path.write_bytes(data)
    with db.conn() as c:
        res = db.row(c, "INSERT INTO evidence_document (file_uri, sha256, mime_type, description, uploaded_by) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING doc_id", (str(path), sha, file.content_type, description, user["email"]))
        db.audit(c, user["email"], "insert", "evidence_document", res["doc_id"], after={"sha256": sha, "name": safe})
    return {"doc_id": res["doc_id"], "sha256": sha}


@router.get("/evidence/{doc_id}")
def download_evidence(doc_id: str, user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        d = db.row(c, "SELECT file_uri FROM evidence_document WHERE doc_id = %s", (doc_id,))
    if not d or not Path(d["file_uri"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(d["file_uri"])


# --------------------------------------------------------------------------- validation workflow
_VALIDATABLE = None


def validatable(c):
    global _VALIDATABLE
    if _VALIDATABLE is None:
        _VALIDATABLE = [r["table_name"] for r in db.rows(
            c, "SELECT table_name FROM information_schema.columns WHERE table_schema='esg' "
               "AND column_name='validation_status' ORDER BY 1")]
    return _VALIDATABLE


@router.get("/validation/summary")
def validation_summary(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        tabs = [t for t in validatable(c) if t in INGESTIBLE]
        sql = " UNION ALL ".join(
            f"SELECT '{t}' AS table_name, count(*) FILTER (WHERE validation_status='pending') AS pending, "
            f"count(*) FILTER (WHERE validation_status='validated') AS validated, "
            f"count(*) FILTER (WHERE validation_status='rejected') AS rejected, "
            f"min(ingested_at) FILTER (WHERE validation_status='pending') AS oldest_pending FROM esg.{t}" for t in tabs)
        data = db.rows(c, sql)
    return sorted(data, key=lambda r: -r["pending"])


@router.get("/validation/{table}")
def validation_queue(table: str, request: Request, status: str = "pending", limit: int = Query(100, le=500),
                     user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        if table not in validatable(c) or table not in INGESTIBLE:
            raise HTTPException(404, "Table has no validation workflow")
        where, params = ["validation_status = %s"], [status]
        for k, v in request.query_params.items():         # optional column filters, e.g. ?meter_id=...
            if k in ("status", "limit"):
                continue
            if k not in SCHEMAS[table]["properties"]:
                raise HTTPException(400, f"Cannot filter on unknown column {k}")
            where.append(f"{k}::text = %s")
            params.append(v)
        data = db.rows(c, f"SELECT *, {id_expr(c, table)} AS _id FROM esg.{table} WHERE {' AND '.join(where)} "
                          f"ORDER BY ingested_at ASC LIMIT %s", params + [limit])
        ev = evidence.evidence_summary(c, table, [r["_id"] for r in data])
        codes = {str(m["meter_id"]): m["meter_code"] for m in db.rows(c, "SELECT meter_id, meter_code FROM meter")} \
            if table == "meter_register_reading" else {}
        for r in data:
            r["_evidence"] = ev.get(r["_id"], [])
            if codes:
                r["_meter"] = codes.get(str(r["meter_id"]))
            if table == "meter_register_reading" and status == "pending":   # arithmetic checks are live, with or without a photo
                r["_checks"] = [f for f in evidence.sequence_flags(c, r["meter_id"], r["register_value"], r["read_at"],
                                                                   r["_id"], r["rollover"]) if f["code"] != "first_reading"] \
                               + ([evidence.flag("no_evidence", "warning", "No photo attached.")] if not r["_evidence"] else [])
    return data


class Decision(BaseModel):
    action: str  # validate | reject
    comment: str = ""


@router.post("/validation/{table}/{rid}")
def decide(table: str, rid: str, body: Decision, user: dict = Depends(require("esg_admin", "data_owner"))):
    if body.action not in ("validate", "reject"):
        raise HTTPException(400, "action must be validate or reject")
    if body.action == "reject" and not body.comment.strip():
        raise HTTPException(400, "A comment is required when rejecting")
    with db.conn() as c:
        if table not in validatable(c) or table not in INGESTIBLE:
            raise HTTPException(404, "Table has no validation workflow")
        pk = pk_cols(c, table)[0]
        r = db.row(c, f"SELECT * FROM esg.{table} WHERE {pk}::text = %s FOR UPDATE", (rid,))
        if not r:
            raise HTTPException(404, "Record not found")
        if r["validation_status"] != "pending":
            raise HTTPException(409, f"Record is already {r['validation_status']}")
        if r.get("created_by") == user["email"]:
            raise HTTPException(403, "Four-eyes rule: you cannot approve a record you created")
        new = "validated" if body.action == "validate" else "rejected"
        sets = "validation_status = %s, approved_by = %s"
        params = [new, user["email"]]
        if table == "tenant_esg_submission":
            sets += ", status = %s, reviewer = %s, review_date = current_date"
            params += ["approved" if new == "validated" else "rejected", user["email"]]
        try:
            after = db.row(c, f"UPDATE esg.{table} SET {sets} WHERE {pk}::text = %s RETURNING *", params + [rid])
        except psycopg.errors.CheckViolation as e:
            raise HTTPException(409, pg_message(e))
        db.audit(c, user["email"], body.action, table, rid, before={"validation_status": "pending"},
                 after={"validation_status": new}, detail=body.comment or None)
        if new == "validated":
            try:
                evidence.before_decision(c, table, after, user["email"])   # may veto, e.g. impossible meter sequence
            except psycopg.errors.CheckViolation as e:
                raise HTTPException(409, pg_message(e))
            after = db.row(c, f"SELECT * FROM esg.{table} WHERE {pk}::text = %s", (rid,))
    return after
