"""Ingest batches: every API or CSV load is remembered as a batch, so a bad file can be withdrawn in one step.
Withdrawing rejects the batch's records (they stay in the database, excluded from every calculation) and is audited.
Records in a locked reporting period cannot be withdrawn until the period is unlocked."""
import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require

router = APIRouter(prefix="/api/batches")
NO_WITHDRAW = {"meter_register_reading"}      # derived readings: correct these through the approval flow


def record(c, user, res, source_code, filename):
    """Store a batch for a finished ingest and strip the internal id list from the response."""
    ids = res.pop("_all", [])
    if ids:
        b = db.row(c, "INSERT INTO ingest_batch (table_name, source_code, filename, created_by, received, inserted, duplicates, errors) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING batch_id",
                   (res["table"], source_code, filename, user["email"], res["received"], res["inserted"], res["duplicates"], len(res["errors"])))
        with c.cursor() as cur:
            cur.executemany("INSERT INTO ingest_batch_record (batch_id, record_id) VALUES (%s,%s)", [(b["batch_id"], i) for i in ids])
        res["batch_id"] = str(b["batch_id"])
    return res


@router.get("")
def list_batches(user: dict = Depends(require("esg_admin", "data_owner", "auditor"))):
    with db.conn() as c:
        return db.rows(c, "SELECT *, (withdrawn_at IS NULL) AS active FROM ingest_batch ORDER BY created_at DESC LIMIT 200")


class Withdraw(BaseModel):
    reason: str


@router.post("/{batch_id}/withdraw")
def withdraw(batch_id: str, body: Withdraw, user: dict = Depends(require("esg_admin", "data_owner"))):
    if not body.reason.strip():
        raise HTTPException(400, "A reason is required")
    from .capture import pk_cols
    with db.conn() as c:
        b = db.row(c, "SELECT * FROM ingest_batch WHERE batch_id::text = %s FOR UPDATE", (batch_id,))
        if not b:
            raise HTTPException(404, "Batch not found")
        if b["withdrawn_at"]:
            raise HTTPException(409, "Batch already withdrawn")
        t = b["table_name"]
        if t in NO_WITHDRAW:
            raise HTTPException(400, f"{t} batches cannot be withdrawn this way")
        pk = pk_cols(c, t)[0]
        ids = [r["record_id"] for r in db.rows(c, "SELECT record_id FROM ingest_batch_record WHERE batch_id = %s", (b["batch_id"],))]
        n = 0
        try:
            with c.transaction():
                sets, extra = "validation_status = 'rejected', approved_by = %s", ""
                if t == "tenant_esg_submission":
                    extra = ", status = 'rejected', reviewer = %s, review_date = current_date"
                for rid in ids:
                    params = [user["email"]] + ([user["email"]] if extra else []) + [rid]
                    after = db.row(c, f"UPDATE esg.{t} SET {sets}{extra} WHERE {pk}::text = %s AND validation_status <> 'rejected' RETURNING {pk}", params)
                    if after:
                        n += 1
                        db.audit(c, user["email"], "withdraw", t, rid, after={"validation_status": "rejected"}, detail=f"batch {batch_id[:8]}: {body.reason.strip()}")
        except psycopg.Error as e:
            raise HTTPException(409, "Part of this batch is in a locked reporting period; unlock it first. " + str(e).splitlines()[0])
        db.row(c, "UPDATE ingest_batch SET withdrawn_by=%s, withdrawn_at=now(), withdraw_reason=%s, withdrawn_count=%s WHERE batch_id = %s RETURNING 1",
               (user["email"], body.reason.strip(), n, b["batch_id"]))
        db.audit(c, user["email"], "withdraw_batch", t, batch_id, detail=f"{n} records: {body.reason.strip()}")
        return {"batch_id": batch_id, "withdrawn": n}
