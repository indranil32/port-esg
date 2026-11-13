"""Source-trace explorer: from a reported number down to the raw records, the people who entered and approved
them, and the evidence (photo or file) behind them."""
from fastapi import APIRouter, Depends, HTTPException, Query

from . import calc, db
from .auth import require

router = APIRouter(prefix="/api/trace")
READERS = require("esg_admin", "data_owner", "auditor")
TRACEABLE = {a[4] for a in calc.ACTIVITIES}
SHOW_MAX = 100


@router.get("/runs/{run_id}/activities")
def activities(run_id: str, scope: int | None = None, entity: str | None = None, q: str | None = None,
               limit: int = Query(200, le=1000), user: dict = Depends(READERS)):
    with db.conn() as c:
        run = db.row(c, "SELECT r.*, l.label AS lock_label FROM calc_run r LEFT JOIN reporting_period_lock l "
                        "ON l.run_id = r.run_id AND l.unlocked_at IS NULL WHERE r.run_id::text = %s", (run_id,))
        if not run:
            raise HTTPException(404, "Run not found")
        rows = db.rows(c, """
            SELECT a.activity_id, a.scope, a.scope3_category, a.activity_type, a.quantity, a.uom, a.period_start, a.period_end,
                   a.data_quality, a.source_table, cardinality(a.source_record_ids) AS source_rows, en.code AS entity,
                   round(sum(e.co2e_tonnes)::numeric, 3) AS tco2e
            FROM ghg_activity_data a JOIN ghg_emission e ON e.activity_id = a.activity_id
            LEFT JOIN entity en ON en.entity_id = a.entity_id
            WHERE a.run_id = %s AND (%s::int IS NULL OR a.scope = %s) AND (%s::text IS NULL OR a.entity_id::text = %s)
              AND (%s::text IS NULL OR a.activity_type ILIKE '%%' || %s || '%%')
            GROUP BY a.activity_id, en.code ORDER BY tco2e DESC LIMIT %s""", (run["run_id"], scope, scope, entity, entity, q, q, limit))
    return {"run": run, "rows": rows}


@router.get("/activity/{activity_id}")
def activity(activity_id: str, user: dict = Depends(READERS)):
    with db.conn() as c:
        a = db.row(c, "SELECT a.*, en.code AS entity FROM ghg_activity_data a LEFT JOIN entity en ON en.entity_id = a.entity_id "
                      "WHERE a.activity_id::text = %s", (activity_id,))
        if not a:
            raise HTTPException(404, "Activity not found")
        run = db.row(c, "SELECT r.run_id, r.period_start, r.period_end, r.factor_set_version, r.triggered_by, r.started_at, l.label AS lock_label "
                        "FROM calc_run r LEFT JOIN reporting_period_lock l ON l.run_id = r.run_id AND l.unlocked_at IS NULL WHERE r.run_id = %s",
                     (a["run_id"],))
        em = db.rows(c, "SELECT e.emission_id, e.gas, e.co2e_tonnes, e.method, f.factor_id, f.value AS factor_value, f.uom AS factor_uom, "
                        "f.source AS factor_source, f.valid_from, f.valid_to FROM ghg_emission e JOIN emission_factor f ON f.factor_id = e.factor_id "
                        "WHERE e.activity_id = %s", (a["activity_id"],))
        table, ids = a["source_table"], list(a["source_record_ids"] or [])
        sources, note = [], None
        if table in TRACEABLE and ids:
            pk = db.primary_keys(c)[table][0]
            rows = db.rows(c, f"SELECT t.*, ss.code AS source_system FROM esg.{table} t "
                              f"LEFT JOIN esg.source_system ss ON ss.source_system_id = t.source_system_id "
                              f"WHERE t.{pk}::text = ANY(%s) ORDER BY 1 LIMIT %s", (ids[:SHOW_MAX], SHOW_MAX))
            for r in rows:
                rid = str(r[pk])
                caps = db.rows(c, "SELECT capture_id, captured_by, exif_taken_at, entered_value, candidates, flags FROM evidence_capture "
                                  "WHERE target_table = %s AND target_record_id = %s", (table, rid))
                derived_from = None
                if table == "energy_meter_reading":          # a derived interval row: follow it back to the hand-read register
                    g = db.row(c, "SELECT register_reading_id, register_value, read_at, created_by, approved_by FROM meter_register_reading "
                                  "WHERE derived_reading_id = %s", (r["reading_id"],))
                    if g:
                        derived_from = {"table": "meter_register_reading", **g}
                        caps = db.rows(c, "SELECT capture_id, captured_by, exif_taken_at, entered_value, candidates, flags FROM evidence_capture "
                                          "WHERE target_table = 'meter_register_reading' AND target_record_id = %s", (str(g["register_reading_id"]),))
                doc = None
                if r.get("evidence_doc_id"):
                    doc = db.row(c, "SELECT doc_id, mime_type, description, sha256 FROM evidence_document WHERE doc_id = %s", (r["evidence_doc_id"],))
                sources.append({"id": rid, "row": r, "captures": caps, "document": doc, "derived_from": derived_from})
            if len(ids) >= 500:
                note = "Lineage lists the first 500 source records for this activity; the rest are included in the total but not listed."
            elif len(ids) > SHOW_MAX:
                note = f"Showing the first {SHOW_MAX} of {len(ids)} source records."
        return {"activity": a, "run": run, "emissions": em, "source_table": table, "source_count": len(ids), "sources": sources, "note": note}
