"""Reporting-period locks. Locking a calculation run fixes the figures reported for its period: source data dated
inside it can no longer change (enforced by database triggers, so direct SQL is covered too), the period cannot be
recalculated, and the emission factors behind it cannot be edited in place. An administrator can unlock with a reason;
both steps are audited. Corrections for later years use a new factor with a later valid_from."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require

router = APIRouter(prefix="/api/periods")


def locked_overlap(c, start, end):
    return db.row(c, "SELECT label, period_start, period_end FROM reporting_period_lock WHERE unlocked_at IS NULL "
                     "AND period_start <= %s AND period_end >= %s LIMIT 1", (end, start))


@router.get("")
def list_locks(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        return db.rows(c, "SELECT l.*, (l.unlocked_at IS NULL) AS active, r.factor_set_version FROM reporting_period_lock l "
                          "LEFT JOIN calc_run r ON r.run_id = l.run_id ORDER BY l.locked_at DESC")


class LockBody(BaseModel):
    run_id: str
    label: str
    reason: str


@router.post("/lock")
def lock(body: LockBody, user: dict = Depends(require("esg_admin"))):
    if not body.label.strip() or not body.reason.strip():
        raise HTTPException(400, "A label and a reason are required")
    with db.conn() as c:
        run = db.row(c, "SELECT * FROM calc_run WHERE run_id::text = %s", (body.run_id,))
        if not run or run["status"] != "succeeded":
            raise HTTPException(404, "Choose a successful calculation run")
        if locked_overlap(c, run["period_start"], run["period_end"]):
            raise HTTPException(409, "That period overlaps one that is already locked")
        pending = db.row(c, "SELECT count(*) AS n FROM (SELECT 1 FROM fuel_transaction WHERE validation_status = 'pending' "
                            "AND ts >= %s::date AND ts < %s::date + 1 UNION ALL SELECT 1 FROM refrigerant_event "
                            "WHERE validation_status = 'pending' AND event_date BETWEEN %s AND %s UNION ALL SELECT 1 FROM spend_record "
                            "WHERE validation_status = 'pending' AND spend_date BETWEEN %s AND %s UNION ALL SELECT 1 FROM meter_register_reading "
                            "WHERE validation_status = 'pending' AND read_at >= %s::date AND read_at < %s::date + 1) x",
                         (run["period_start"], run["period_end"]) * 4)["n"]
        lk = db.row(c, "INSERT INTO reporting_period_lock (label, period_start, period_end, run_id, locked_by, reason) "
                       "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                    (body.label.strip(), run["period_start"], run["period_end"], run["run_id"], user["email"], body.reason.strip()))
        db.audit(c, user["email"], "lock_period", "reporting_period_lock", lk["lock_id"], after=lk, detail=body.reason)
        lk["pending_records_in_period"] = pending
        return lk


class UnlockBody(BaseModel):
    reason: str


@router.post("/{lock_id}/unlock")
def unlock(lock_id: str, body: UnlockBody, user: dict = Depends(require("esg_admin"))):
    if not body.reason.strip():
        raise HTTPException(400, "A reason is required to unlock a reported period")
    with db.conn() as c:
        lk = db.row(c, "UPDATE reporting_period_lock SET unlocked_by = %s, unlocked_at = now(), unlock_reason = %s "
                       "WHERE lock_id::text = %s AND unlocked_at IS NULL RETURNING *", (user["email"], body.reason.strip(), lock_id))
        if not lk:
            raise HTTPException(404, "No active lock with that id")
        db.audit(c, user["email"], "unlock_period", "reporting_period_lock", lk["lock_id"], after=lk, detail=body.reason)
        return lk


@router.get("/factors")
def factors(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    """Emission factors with the years they apply to and whether a locked period relies on them."""
    with db.conn() as c:
        return db.rows(c, """
            SELECT f.factor_id, f.activity_type, f.fuel_or_grid, f.value, f.uom, f.source, f.valid_from, f.valid_to,
                   EXISTS (SELECT 1 FROM ghg_emission e JOIN reporting_period_lock l ON l.run_id = e.run_id AND l.unlocked_at IS NULL
                           WHERE e.factor_id = f.factor_id) AS in_locked_period
            FROM emission_factor f ORDER BY f.activity_type, f.fuel_or_grid, f.valid_from DESC""")
