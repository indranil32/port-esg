"""Environmental (ISO 14001) and energy (ISO 50001) management workflows: objectives tied to KPIs, action plans,
internal audits (findings are the existing audit_finding records, linked by audit reference) and management reviews
that snapshot where the system stands. This supports running the management system; it does not certify it."""
from datetime import date

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require

router = APIRouter(prefix="/api/ems")
READ = require("esg_admin", "data_owner", "auditor")
WRITE = require("esg_admin", "data_owner")
STD = ("ISO14001", "ISO50001")


def need_std(s):
    if s not in STD:
        raise HTTPException(400, f"standard must be one of {list(STD)}")


def progress(base, target, current):
    """Share of the distance from baseline to target covered so far (works for decreases and increases)."""
    if None in (base, target, current) or float(base) == float(target):
        return None
    return round(100.0 * (float(current) - float(base)) / (float(target) - float(base)), 1)


def latest_kpi(c, metric_id, entity_id):
    return db.row(c, """
        SELECT k.value, k.period_end FROM kpi_value k JOIN calc_run r ON r.run_id = k.run_id
        JOIN entity en ON en.entity_id = k.entity_id
        WHERE r.status = 'succeeded' AND k.metric_id = %s AND (k.entity_id = %s OR (%s::uuid IS NULL AND en.entity_type = 'port'))
        ORDER BY r.finished_at DESC LIMIT 1""", (metric_id, entity_id, entity_id))


# ---------------------------------------------------------------- objectives and actions
class Objective(BaseModel):
    standard: str
    title: str
    entity_id: str | None = None
    metric_id: str | None = None
    baseline_value: float | None = None
    target_value: float | None = None
    target_date: date | None = None
    owner: str | None = None


@router.get("/objectives")
def objectives(standard: str | None = None, user: dict = Depends(READ)):
    with db.conn() as c:
        rows = db.rows(c, """
            SELECT o.*, m.metric_code, m.unit, en.name AS entity,
              (SELECT count(*) FROM ems_action a WHERE a.objective_id = o.objective_id) AS actions,
              (SELECT count(*) FROM ems_action a WHERE a.objective_id = o.objective_id AND a.status = 'done') AS actions_done,
              (SELECT count(*) FROM ems_action a WHERE a.objective_id = o.objective_id AND a.status <> 'done' AND a.due_date < current_date) AS actions_overdue
            FROM ems_objective o LEFT JOIN metric m ON m.metric_id = o.metric_id LEFT JOIN entity en ON en.entity_id = o.entity_id
            WHERE (%s::text IS NULL OR o.standard = %s) ORDER BY o.created_at DESC""", (standard, standard))
        for r in rows:
            k = latest_kpi(c, r["metric_id"], r["entity_id"]) if r["metric_id"] else None
            r["current_value"] = k["value"] if k else None
            r["progress_pct"] = progress(r["baseline_value"], r["target_value"], r["current_value"])
        return rows


@router.post("/objectives")
def add_objective(body: Objective, user: dict = Depends(WRITE)):
    need_std(body.standard)
    if not body.title.strip():
        raise HTTPException(400, "A title is required")
    try:
        with db.conn() as c:
            r = db.row(c, """INSERT INTO ems_objective (standard, title, entity_id, metric_id, baseline_value, target_value, target_date, owner, created_by)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                       (body.standard, body.title.strip(), body.entity_id, body.metric_id, body.baseline_value, body.target_value, body.target_date, body.owner, user["email"]))
            db.audit(c, user["email"], "insert", "ems_objective", r["objective_id"], after=r)
            return r
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(400, "Unknown entity or metric")


class Status(BaseModel):
    status: str
    note: str | None = None


@router.patch("/objectives/{oid}")
def set_objective(oid: str, body: Status, user: dict = Depends(WRITE)):
    if body.status not in ("active", "achieved", "cancelled"):
        raise HTTPException(400, "status must be active, achieved or cancelled")
    with db.conn() as c:
        r = db.row(c, "UPDATE ems_objective SET status = %s WHERE objective_id::text = %s RETURNING *", (body.status, oid))
        if not r:
            raise HTTPException(404, "Objective not found")
        db.audit(c, user["email"], "update", "ems_objective", oid, after={"status": body.status}, detail=body.note)
        return r


class Action(BaseModel):
    title: str
    owner: str | None = None
    due_date: date | None = None


@router.post("/objectives/{oid}/actions")
def add_action(oid: str, body: Action, user: dict = Depends(WRITE)):
    if not body.title.strip():
        raise HTTPException(400, "A title is required")
    with db.conn() as c:
        if not db.row(c, "SELECT 1 FROM ems_objective WHERE objective_id::text = %s", (oid,)):
            raise HTTPException(404, "Objective not found")
        r = db.row(c, "INSERT INTO ems_action (objective_id, title, owner, due_date, created_by) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                   (oid, body.title.strip(), body.owner, body.due_date, user["email"]))
        db.audit(c, user["email"], "insert", "ems_action", r["action_id"], after=r)
        return r


@router.get("/actions")
def actions(overdue: bool = False, user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, """SELECT a.*, o.title AS objective, o.standard FROM ems_action a JOIN ems_objective o USING (objective_id)
                             WHERE (NOT %s OR (a.status <> 'done' AND a.due_date < current_date)) ORDER BY a.due_date NULLS LAST""", (overdue,))


@router.patch("/actions/{aid}")
def set_action(aid: str, body: Status, user: dict = Depends(WRITE)):
    if body.status not in ("open", "in_progress", "done"):
        raise HTTPException(400, "status must be open, in_progress or done")
    with db.conn() as c:
        r = db.row(c, "UPDATE ems_action SET status = %s, note = coalesce(%s, note), completed_at = CASE WHEN %s = 'done' THEN now() END "
                      "WHERE action_id::text = %s RETURNING *", (body.status, body.note, body.status, aid))
        if not r:
            raise HTTPException(404, "Action not found")
        db.audit(c, user["email"], "update", "ems_action", aid, after={"status": body.status}, detail=body.note)
        return r


# ---------------------------------------------------------------- internal audits
class AuditPlan(BaseModel):
    audit_ref: str
    standard: str
    scope: str | None = None
    planned_date: date
    auditor: str | None = None


@router.get("/audits")
def audits(user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, """SELECT a.*, (a.status = 'planned' AND a.planned_date < current_date) AS overdue,
                 (SELECT count(*) FROM audit_finding f WHERE f.audit_ref = a.audit_ref) AS findings,
                 (SELECT count(*) FROM audit_finding f WHERE f.audit_ref = a.audit_ref AND f.status <> 'closed') AS open_findings
                 FROM ems_audit a ORDER BY a.planned_date DESC""")


@router.post("/audits")
def plan_audit(body: AuditPlan, user: dict = Depends(WRITE)):
    need_std(body.standard)
    if not body.audit_ref.strip():
        raise HTTPException(400, "An audit reference is required")
    try:
        with db.conn() as c:
            r = db.row(c, "INSERT INTO ems_audit (audit_ref, standard, scope, planned_date, auditor, created_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                       (body.audit_ref.strip(), body.standard, body.scope, body.planned_date, body.auditor, user["email"]))
            db.audit(c, user["email"], "insert", "ems_audit", r["audit_id"], after=r)
            return r
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "That audit reference already exists")


@router.post("/audits/{aid}/complete")
def complete_audit(aid: str, performed_date: date | None = None, user: dict = Depends(WRITE)):
    with db.conn() as c:
        r = db.row(c, "UPDATE ems_audit SET status = 'done', performed_date = coalesce(%s, current_date) WHERE audit_id::text = %s AND status = 'planned' RETURNING *",
                   (performed_date, aid))
        if not r:
            raise HTTPException(404, "Planned audit not found")
        db.audit(c, user["email"], "update", "ems_audit", aid, after={"status": "done"})
        return r


# ---------------------------------------------------------------- management review
def review_snapshot(c, standard):
    objs = db.rows(c, "SELECT objective_id, title, metric_id, entity_id, baseline_value, target_value, target_date, status FROM ems_objective WHERE standard = %s AND status <> 'cancelled'", (standard,))
    for o in objs:
        k = latest_kpi(c, o["metric_id"], o["entity_id"]) if o["metric_id"] else None
        o["current_value"] = k["value"] if k else None
        o["progress_pct"] = progress(o["baseline_value"], o["target_value"], o["current_value"])
        o.pop("metric_id"); o.pop("entity_id")
    kp = db.row(c, """SELECT count(*) FILTER (WHERE status = 'on_track') AS on_track, count(*) FILTER (WHERE status = 'at_risk') AS at_risk,
                             count(*) FILTER (WHERE status = 'off_track') AS off_track
                      FROM kpi_value WHERE run_id = (SELECT run_id FROM calc_run WHERE status = 'succeeded' ORDER BY finished_at DESC LIMIT 1)""")
    return {
        "objectives": objs, "kpi_status": kp,
        "open_findings": db.row(c, "SELECT count(*) AS n, count(*) FILTER (WHERE severity = 'high') AS high FROM audit_finding WHERE status <> 'closed'"),
        "overdue_actions": db.row(c, "SELECT count(*) AS n FROM ems_action a JOIN ems_objective o USING (objective_id) WHERE o.standard = %s AND a.status <> 'done' AND a.due_date < current_date", (standard,))["n"],
        "audits_done_12m": db.row(c, "SELECT count(*) AS n FROM ems_audit WHERE standard = %s AND status = 'done' AND performed_date >= current_date - 365", (standard,))["n"],
        "certifications_expiring_90d": db.rows(c, "SELECT standard, expiry_date FROM certification WHERE expiry_date <= current_date + 90 ORDER BY expiry_date"),
        "environmental_incidents_12m": db.row(c, "SELECT count(*) AS n FROM environmental_incident WHERE occurred_at >= now() - interval '365 days'")["n"],
    }


class Review(BaseModel):
    standard: str
    review_date: date
    attendees: str | None = None
    decisions: str | None = None


@router.get("/reviews")
def reviews(user: dict = Depends(READ)):
    with db.conn() as c:
        return db.rows(c, "SELECT * FROM ems_review ORDER BY review_date DESC")


@router.post("/reviews")
def add_review(body: Review, user: dict = Depends(WRITE)):
    need_std(body.standard)
    with db.conn() as c:
        from psycopg.types.json import Jsonb
        snap = review_snapshot(c, body.standard)
        r = db.row(c, "INSERT INTO ems_review (standard, review_date, attendees, decisions, snapshot, created_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                   (body.standard, body.review_date, body.attendees, body.decisions, Jsonb(db.jsonable(snap)), user["email"]))
        db.audit(c, user["email"], "insert", "ems_review", r["review_id"], detail=f"{body.standard} management review")
        return r


# ---------------------------------------------------------------- alert evaluator
def f_ems_overdue(c, p):
    out = []
    for r in db.rows(c, """SELECT a.action_id, a.title, a.owner, a.due_date, o.title AS objective FROM ems_action a JOIN ems_objective o USING (objective_id)
                           WHERE a.status <> 'done' AND o.status = 'active' AND a.due_date < current_date"""):
        out.append(dict(dedupe_key=f"action:{r['action_id']}", title=f"Overdue EMS action: {r['title']}",
                        message=f"Due {r['due_date']} ({(date.today() - r['due_date']).days} days ago), owner {r['owner'] or 'unassigned'}; objective: {r['objective']}.",
                        context={"action_id": str(r["action_id"])}))
    for r in db.rows(c, "SELECT audit_id, audit_ref, standard, planned_date FROM ems_audit WHERE status = 'planned' AND planned_date < current_date"):
        out.append(dict(dedupe_key=f"audit:{r['audit_id']}", title=f"Internal audit not completed: {r['audit_ref']}",
                        message=f"{r['standard']} audit was planned for {r['planned_date']}.", context={"audit_id": str(r["audit_id"])}))
    return out
