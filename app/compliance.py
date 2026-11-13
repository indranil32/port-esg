"""Legal and permit register, nonconformities with corrective action, and ISO 50001 energy baselines (EnPI)."""
from datetime import date

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require

router = APIRouter(prefix="/api/ems")
READ = require("esg_admin", "data_owner", "auditor")
WRITE = require("esg_admin", "data_owner")


# ---------------------------------------------------------------- legal and permit register
class Legal(BaseModel):
    ref: str
    title: str
    kind: str
    authority: str | None = None
    entity_id: str | None = None
    obligation: str | None = None
    owner: str | None = None
    permit_expiry: date | None = None
    review_due: date | None = None


@router.get("/legal")
def legal(user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, """SELECT l.*, en.name AS entity,
              (l.permit_expiry IS NOT NULL AND l.permit_expiry <= current_date + 60) AS permit_soon,
              (l.review_due IS NOT NULL AND l.review_due < current_date) AS review_overdue
            FROM legal_requirement l LEFT JOIN entity en ON en.entity_id = l.entity_id ORDER BY l.status = 'non_compliant' DESC, l.review_due NULLS LAST""")


@router.post("/legal")
def add_legal(body: Legal, user: dict = Depends(WRITE)):
    if not body.ref.strip() or not body.title.strip():
        raise HTTPException(400, "Reference and title are required")
    try:
        with db.conn() as c:
            r = db.row(c, """INSERT INTO legal_requirement (ref, title, kind, authority, entity_id, obligation, owner, permit_expiry, review_due, created_by)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                       (body.ref.strip(), body.title.strip(), body.kind, body.authority, body.entity_id, body.obligation, body.owner,
                        body.permit_expiry, body.review_due, user["email"]))
            db.audit(c, user["email"], "insert", "legal_requirement", r["requirement_id"], after=r)
            return r
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "That reference already exists")
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation):
        raise HTTPException(400, "kind must be law, permit, standard, customer or other, and the entity must exist")


class Evaluation(BaseModel):
    status: str
    note: str | None = None
    next_review: date | None = None


@router.post("/legal/{rid}/evaluate")
def evaluate_legal(rid: str, body: Evaluation, user: dict = Depends(WRITE)):
    if body.status not in ("compliant", "non_compliant", "under_review"):
        raise HTTPException(400, "status must be compliant, non_compliant or under_review")
    if body.status == "non_compliant" and not (body.note or "").strip():
        raise HTTPException(400, "Say what is not compliant")
    with db.conn() as c:
        r = db.row(c, """UPDATE legal_requirement SET status = %s, last_evaluated = current_date, evaluated_by = %s, evaluation_note = %s,
                         review_due = coalesce(%s, review_due) WHERE requirement_id::text = %s RETURNING *""",
                   (body.status, user["email"], body.note, body.next_review, rid))
        if not r:
            raise HTTPException(404, "Not found")
        db.audit(c, user["email"], "evaluate", "legal_requirement", rid, after={"status": body.status}, detail=body.note)
        return r


# ---------------------------------------------------------------- nonconformities
class NC(BaseModel):
    nc_ref: str
    source: str
    standard: str | None = None
    entity_id: str | None = None
    description: str
    severity: str = "minor"
    due_date: date | None = None


@router.get("/nonconformities")
def ncs(user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, """SELECT n.*, en.name AS entity, (n.status <> 'closed' AND n.due_date < current_date) AS overdue,
              (SELECT count(*) FROM ems_action a WHERE a.nc_id = n.nc_id) AS actions,
              (SELECT count(*) FROM ems_action a WHERE a.nc_id = n.nc_id AND a.status = 'done') AS actions_done
            FROM nonconformity n LEFT JOIN entity en ON en.entity_id = n.entity_id ORDER BY n.status = 'closed', n.raised_at DESC""")


@router.post("/nonconformities")
def raise_nc(body: NC, user: dict = Depends(WRITE)):
    if not body.nc_ref.strip() or not body.description.strip():
        raise HTTPException(400, "Reference and description are required")
    try:
        with db.conn() as c:
            r = db.row(c, """INSERT INTO nonconformity (nc_ref, source, standard, entity_id, description, severity, due_date, raised_by)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                       (body.nc_ref.strip(), body.source, body.standard, body.entity_id, body.description.strip(), body.severity, body.due_date, user["email"]))
            db.audit(c, user["email"], "insert", "nonconformity", r["nc_id"], after=r)
            return r
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "That reference already exists")
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation):
        raise HTTPException(400, "Invalid source, standard, severity or entity")


class RootCause(BaseModel):
    root_cause: str


@router.post("/nonconformities/{nid}/root-cause")
def root_cause(nid: str, body: RootCause, user: dict = Depends(WRITE)):
    if not body.root_cause.strip():
        raise HTTPException(400, "Describe the root cause")
    with db.conn() as c:
        r = db.row(c, "UPDATE nonconformity SET root_cause = %s, status = 'corrective_action' WHERE nc_id::text = %s AND status IN ('open', 'corrective_action') RETURNING *",
                   (body.root_cause.strip(), nid))
        if not r:
            raise HTTPException(409, "Nonconformity not found or already past this step")
        db.audit(c, user["email"], "update", "nonconformity", nid, after={"status": "corrective_action"}, detail=body.root_cause)
        return r


class NCAction(BaseModel):
    title: str
    owner: str | None = None
    due_date: date | None = None


@router.post("/nonconformities/{nid}/actions")
def nc_action(nid: str, body: NCAction, user: dict = Depends(WRITE)):
    with db.conn() as c:
        n = db.row(c, "SELECT status FROM nonconformity WHERE nc_id::text = %s", (nid,))
        if not n:
            raise HTTPException(404, "Not found")
        if n["status"] not in ("corrective_action", "verification"):
            raise HTTPException(409, "Record the root cause before adding corrective actions")
        r = db.row(c, "INSERT INTO ems_action (nc_id, title, owner, due_date, created_by) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                   (nid, body.title.strip(), body.owner, body.due_date, user["email"]))
        db.audit(c, user["email"], "insert", "ems_action", r["action_id"], after=r)
        return r


@router.post("/nonconformities/{nid}/verify")
def to_verification(nid: str, user: dict = Depends(WRITE)):
    with db.conn() as c:
        acts = db.row(c, "SELECT count(*) AS n, count(*) FILTER (WHERE status <> 'done') AS open FROM ems_action WHERE nc_id::text = %s", (nid,))
        if not acts["n"]:
            raise HTTPException(409, "Add at least one corrective action first")
        if acts["open"]:
            raise HTTPException(409, f"{acts['open']} corrective action(s) are not done yet")
        r = db.row(c, "UPDATE nonconformity SET status = 'verification' WHERE nc_id::text = %s AND status = 'corrective_action' RETURNING *", (nid,))
        if not r:
            raise HTTPException(409, "Nonconformity is not at the corrective-action step")
        db.audit(c, user["email"], "update", "nonconformity", nid, after={"status": "verification"})
        return r


class Closure(BaseModel):
    effectiveness_note: str


@router.post("/nonconformities/{nid}/close")
def close_nc(nid: str, body: Closure, user: dict = Depends(WRITE)):
    if not body.effectiveness_note.strip():
        raise HTTPException(400, "Say how you checked the fix worked")
    with db.conn() as c:
        n = db.row(c, "SELECT * FROM nonconformity WHERE nc_id::text = %s FOR UPDATE", (nid,))
        if not n or n["status"] != "verification":
            raise HTTPException(409, "Only a nonconformity in verification can be closed")
        if n["severity"] == "major" and n["raised_by"] == user["email"]:
            raise HTTPException(403, "A major nonconformity must be closed by someone other than the person who raised it")
        r = db.row(c, "UPDATE nonconformity SET status = 'closed', effectiveness_note = %s, closed_by = %s, closed_at = now() WHERE nc_id = %s RETURNING *",
                   (body.effectiveness_note.strip(), user["email"], n["nc_id"]))
        db.audit(c, user["email"], "close", "nonconformity", nid, after={"status": "closed"}, detail=body.effectiveness_note)
        return r


# ---------------------------------------------------------------- energy baselines (EnPI)
def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = 0.0 if sxx == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (icpt + slope * x)) ** 2 for x, y in zip(xs, ys))
    return icpt, slope, (None if ss_tot == 0 else 1 - ss_res / ss_tot)


def monthly(c, entity_id, driver, start, end):
    """Month -> (kWh, driver value) for the entity and everything below it."""
    sub = """WITH RECURSIVE d AS (SELECT entity_id FROM entity WHERE entity_id = %(e)s
             UNION ALL SELECT x.entity_id FROM entity x JOIN d ON x.parent_entity_id = d.entity_id)"""
    energy = {r["m"]: float(r["kwh"]) for r in db.rows(c, sub + """
        SELECT date_trunc('month', r.ts)::date AS m, sum(r.energy_kwh) AS kwh FROM energy_meter_reading r JOIN meter m ON m.meter_id = r.meter_id
        WHERE m.entity_id IN (SELECT entity_id FROM d) AND m.scope_boundary IN ('own', 'shared')
          AND r.ts >= %(s)s::date AND r.ts < %(en)s::date + 1 GROUP BY 1""", {"e": entity_id, "s": start, "en": end})}
    drv = {}
    if driver != "none":
        col = "sum(teu_moved)" if driver == "teu" else "count(*)"
        drv = {r["m"]: float(r["v"] or 0) for r in db.rows(c, sub + f"""
            SELECT date_trunc('month', atb)::date AS m, {col} AS v FROM vessel_call WHERE terminal_id IN (SELECT entity_id FROM d)
              AND atb >= %(s)s::date AND atb < %(en)s::date + 1 GROUP BY 1""", {"e": entity_id, "s": start, "en": end})}
    this_month = date.today().replace(day=1)      # a month still in progress would look like a saving, so leave it out
    return [(m, energy[m], drv.get(m, 0.0)) for m in sorted(energy) if m < this_month]


class Baseline(BaseModel):
    name: str
    entity_id: str
    driver: str = "teu"
    baseline_start: date
    baseline_end: date


@router.post("/enpi/baselines")
def make_baseline(body: Baseline, user: dict = Depends(WRITE)):
    if body.driver not in ("teu", "vessel_calls", "none"):
        raise HTTPException(400, "driver must be teu, vessel_calls or none")
    if body.baseline_end <= body.baseline_start:
        raise HTTPException(400, "Baseline end must be after start")
    with db.conn() as c:
        rows = monthly(c, body.entity_id, body.driver, body.baseline_start, body.baseline_end)
        if len(rows) < 6:
            raise HTTPException(422, f"Need at least 6 months of energy data in the baseline period; found {len(rows)}")
        icpt, slope, r2 = ols([r[2] for r in rows], [r[1] for r in rows]) if body.driver != "none" else (sum(r[1] for r in rows) / len(rows), 0.0, None)
        r = db.row(c, """INSERT INTO enpi_baseline (name, entity_id, driver, baseline_start, baseline_end, intercept, slope, r2, n_months, created_by)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                   (body.name.strip() or "Baseline", body.entity_id, body.driver, body.baseline_start, body.baseline_end, icpt, slope, r2, len(rows), user["email"]))
        db.audit(c, user["email"], "insert", "enpi_baseline", r["baseline_id"], after=r)
        r["weak_fit"] = r2 is not None and r2 < 0.5
        return r


@router.get("/enpi/baselines")
def baselines(user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, "SELECT b.*, en.name AS entity FROM enpi_baseline b JOIN entity en USING (entity_id) ORDER BY b.created_at DESC")


@router.get("/enpi/baselines/{bid}/performance")
def performance(bid: str, start: date, end: date, user: dict = Depends(READ)):
    with db.conn() as c:
        b = db.row(c, "SELECT * FROM enpi_baseline WHERE baseline_id::text = %s", (bid,))
        if not b:
            raise HTTPException(404, "Baseline not found")
        rows = monthly(c, b["entity_id"], b["driver"], start, end)
    out, cum = [], 0.0
    for m, kwh, drv in rows:
        exp = float(b["intercept"]) + float(b["slope"]) * drv
        cum += exp - kwh
        out.append({"month": m, "actual_kwh": round(kwh), "expected_kwh": round(exp), "saving_kwh": round(exp - kwh), "cumulative_saving_kwh": round(cum), "driver": drv})
    tot_a, tot_e = sum(r["actual_kwh"] for r in out), sum(r["expected_kwh"] for r in out)
    return {"baseline": b, "months": out, "actual_kwh": tot_a, "expected_kwh": tot_e, "saving_kwh": tot_e - tot_a,
            "saving_pct": round(100.0 * (tot_e - tot_a) / tot_e, 1) if tot_e else None,
            "note": "Savings are against what the baseline model expected for the same activity. They are not verified savings until reviewed."}
