"""Reporting API: run calculations, GHG inventory views, KPI scorecard, disclosure coverage,
CSV exports and a printable annual report (HTML; use the browser's Print > Save as PDF)."""
import csv
import html
import io
from datetime import date

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from . import calc, db
from .auth import require

router = APIRouter(prefix="/api")
READERS = require("esg_admin", "data_owner", "auditor")

ENTITY_CTE = """WITH RECURSIVE d AS (
    SELECT entity_id FROM entity WHERE entity_id = %(entity)s
    UNION ALL SELECT e.entity_id FROM entity e JOIN d ON e.parent_entity_id = d.entity_id)"""


def resolve_run(c, run_id):
    if run_id:
        r = db.row(c, "SELECT * FROM calc_run WHERE run_id = %s", (run_id,))
    else:
        r = db.row(c, "SELECT * FROM calc_run WHERE status = 'succeeded' ORDER BY finished_at DESC LIMIT 1")
    if not r:
        raise HTTPException(404, "No calculation run found. Run a calculation first.")
    return r


class CalcBody(BaseModel):
    period_start: date
    period_end: date


@router.post("/calc/run")
def run_calc(body: CalcBody, user: dict = Depends(require("esg_admin", "data_owner"))):
    if body.period_end < body.period_start or (body.period_end - body.period_start).days > 800:
        raise HTTPException(400, "Invalid period")
    try:
        with db.conn() as c:
            return calc.run_calculation(c, user["email"], body.period_start, body.period_end)
    except ValueError as e:  # e.g. no root entity yet: a setup problem, not a server fault
        raise HTTPException(409, str(e))
    except psycopg.Error as e:
        with db.conn() as c:  # the failed run's own transaction was rolled back; keep a record of the failure
            db.row(c, "INSERT INTO calc_run (period_start, period_end, triggered_by, status, notes, finished_at) "
                      "VALUES (%s,%s,%s,'failed',%s, now()) RETURNING run_id",
                   (body.period_start, body.period_end, user["email"], str(e)[:500]))
        raise HTTPException(500, f"Calculation failed: {e}")


@router.get("/runs")
def runs(user: dict = Depends(READERS)):
    with db.conn() as c:
        return db.rows(c, "SELECT r.*, EXISTS (SELECT 1 FROM reporting_period_lock l WHERE l.run_id = r.run_id AND l.unlocked_at IS NULL) AS locked "
                          "FROM calc_run r ORDER BY started_at DESC LIMIT 50")


@router.get("/entities")
def entities(user: dict = Depends(READERS)):
    with db.conn() as c:
        return db.rows(c, "SELECT entity_id, parent_entity_id, entity_type, code, name FROM entity ORDER BY entity_type, code")


APPROACHES = {   # GHG Protocol consolidation: share of each entity's emissions that is counted
    "all": "1.0",
    "equity": "(en.equity_share_pct / 100.0)",
    "operational": "(CASE WHEN en.operational_control THEN 1.0 ELSE 0.0 END)",
}


def ghg_rows(c, run_id, group, entity=None, approach="all"):
    cols = {
        "month": "a.period_start", "scope": "a.scope", "entity": "en.name",
        "category": "coalesce('Cat ' || a.scope3_category::text, 'Scope ' || a.scope::text)",
        "activity": "a.activity_type",
    }
    if group not in cols:
        raise HTTPException(400, f"group must be one of {list(cols)}")
    if approach not in APPROACHES:
        raise HTTPException(400, f"approach must be one of {list(APPROACHES)}")
    w = APPROACHES[approach]
    cte = ENTITY_CTE if entity else ""
    flt = "AND a.entity_id IN (SELECT entity_id FROM d)" if entity else ""
    extra = ", a.scope" if group in ("month", "category", "activity") else ""
    return db.rows(c, f"""{cte}
        SELECT {cols[group]} AS key{extra}, round(sum(e.co2e_tonnes * {w})::numeric, 2) AS tco2e, count(*) FILTER (WHERE {w} > 0) AS activities
        FROM ghg_activity_data a JOIN ghg_emission e ON e.activity_id = a.activity_id
        JOIN entity en ON en.entity_id = a.entity_id
        WHERE a.run_id = %(run)s {flt} GROUP BY 1{', 2' if extra else ''} ORDER BY 1{', 2' if extra else ''}""",
        {"run": run_id, "entity": entity})


@router.get("/reports/summary")
def summary(run_id: str | None = None, entity: str | None = None, user: dict = Depends(READERS)):
    with db.conn() as c:
        run = resolve_run(c, run_id)
        rid = run["run_id"]
        monthly = ghg_rows(c, rid, "month", entity)
        by_scope = ghg_rows(c, rid, "scope", entity)
        by_entity = ghg_rows(c, rid, "entity", entity)
        by_cat = ghg_rows(c, rid, "category", entity)
        quality = db.rows(c, """
            SELECT a.data_quality, round(sum(e.co2e_tonnes)::numeric, 2) AS tco2e FROM ghg_activity_data a
            JOIN ghg_emission e ON e.activity_id = a.activity_id WHERE a.run_id = %s GROUP BY 1""", (rid,))
        open_alerts = db.row(c, "SELECT count(*) FILTER (WHERE severity='critical') AS critical, "
                                "count(*) AS total FROM alert WHERE status <> 'resolved'")
    return {"run": run, "monthly": monthly, "by_scope": by_scope, "by_entity": by_entity,
            "by_category": by_cat, "data_quality": quality, "open_alerts": open_alerts}


@router.get("/reports/ghg")
def ghg(group: str = "activity", run_id: str | None = None, entity: str | None = None, approach: str = "all", user: dict = Depends(READERS)):
    with db.conn() as c:
        run = resolve_run(c, run_id)
        return {"run": run, "approach": approach, "rows": ghg_rows(c, run["run_id"], group, entity, approach)}


def kpi_rows(c, rid, entity=None):
    return db.rows(c, """
        SELECT m.metric_code, m.name, m.pillar, m.unit, m.direction, en.name AS entity, en.entity_type, en.code AS entity_code,
               k.value, t.target_value, t.target_year, k.status, k.period_start, k.period_end
        FROM kpi_value k JOIN metric m ON m.metric_id = k.metric_id JOIN entity en ON en.entity_id = k.entity_id
        LEFT JOIN kpi_target t ON t.target_id = k.target_id
        WHERE k.run_id = %s AND (%s::uuid IS NULL OR k.entity_id = %s::uuid)
        ORDER BY m.pillar, m.metric_code, en.entity_type DESC, en.code""", (rid, entity, entity))


@router.get("/reports/kpis")
def kpis_report(run_id: str | None = None, entity: str | None = None, user: dict = Depends(READERS)):
    with db.conn() as c:
        run = resolve_run(c, run_id)
        return {"run": run, "rows": kpi_rows(c, run["run_id"], entity)}


def to_csv(rows, cols):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(k) for k in cols])
    return buf.getvalue()


@router.get("/reports/ghg.csv")
def ghg_csv(run_id: str | None = None, user: dict = Depends(READERS)):
    with db.conn() as c:
        run = resolve_run(c, run_id)
        data = db.rows(c, """
            SELECT en.code AS entity, a.scope, a.scope3_category, a.activity_type, a.period_start, a.period_end,
                   a.quantity, a.uom, a.data_quality, f.source AS factor_source, f.value AS factor_value, f.uom AS factor_uom,
                   e.co2e_tonnes, a.source_table, coalesce(cardinality(a.source_record_ids), 0) AS source_records
            FROM ghg_activity_data a JOIN ghg_emission e ON e.activity_id = a.activity_id
            JOIN emission_factor f ON f.factor_id = e.factor_id JOIN entity en ON en.entity_id = a.entity_id
            WHERE a.run_id = %s ORDER BY a.scope, en.code, a.activity_type, a.period_start""", (run["run_id"],))
    cols = list(data[0].keys()) if data else ["entity"]
    return Response(to_csv(data, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="ghg_inventory_{run["period_start"]}_{run["period_end"]}.csv"'})


@router.get("/reports/kpis.csv")
def kpis_csv(run_id: str | None = None, user: dict = Depends(READERS)):
    with db.conn() as c:
        run = resolve_run(c, run_id)
        data = kpi_rows(c, run["run_id"])
    cols = ["metric_code", "name", "pillar", "unit", "entity_code", "value", "target_value", "target_year", "status", "period_start", "period_end"]
    return Response(to_csv(data, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="kpis_{run["period_start"]}_{run["period_end"]}.csv"'})


@router.get("/reports/disclosures")
def disclosures(run_id: str | None = None, user: dict = Depends(READERS)):
    """Framework coverage: which disclosures have a mapped metric with a computed value in the run."""
    with db.conn() as c:
        run = resolve_run(c, run_id)
        data = db.rows(c, """
            SELECT f.code AS framework, f.name AS framework_name, r.disclosure_code, r.title,
                   coalesce(array_agg(DISTINCT m.metric_code) FILTER (WHERE m.metric_code IS NOT NULL), '{}') AS metrics,
                   max(k.value) FILTER (WHERE en.entity_type = 'port') AS value,
                   max(k.status) FILTER (WHERE en.entity_type = 'port') AS status,
                   bool_or(k.kpi_value_id IS NOT NULL) AS has_data
            FROM framework f JOIN framework_requirement r ON r.framework_id = f.framework_id
            LEFT JOIN metric_requirement_map mp ON mp.requirement_id = r.requirement_id
            LEFT JOIN metric m ON m.metric_id = mp.metric_id
            LEFT JOIN kpi_value k ON k.metric_id = m.metric_id AND k.run_id = %s
            LEFT JOIN entity en ON en.entity_id = k.entity_id
            GROUP BY f.code, f.name, r.disclosure_code, r.title ORDER BY f.code, r.disclosure_code""", (run["run_id"],))
    for d in data:
        d["coverage"] = "data" if d["has_data"] else ("mapped_no_data" if d["metrics"] else "not_mapped")
    return {"run": run, "rows": data}


@router.get("/reports/annual.html", response_class=HTMLResponse)
def annual_html(run_id: str | None = None, user: dict = Depends(READERS)):
    esc = html.escape
    with db.conn() as c:
        run = resolve_run(c, run_id)
        rid = run["run_id"]
        scope = ghg_rows(c, rid, "scope")
        cat = ghg_rows(c, rid, "category")
        ent = ghg_rows(c, rid, "entity")
        kp = [k for k in kpi_rows(c, rid) if k["entity_type"] == "port"]
        facs = db.rows(c, """SELECT DISTINCT f.activity_type, f.fuel_or_grid, f.value, f.uom, f.source FROM ghg_emission e
                             JOIN emission_factor f ON f.factor_id = e.factor_id WHERE e.run_id = %s ORDER BY 1, 2""", (rid,))
        quality = db.rows(c, """SELECT a.data_quality, round(sum(e.co2e_tonnes)::numeric,2) AS t FROM ghg_activity_data a
                                JOIN ghg_emission e ON e.activity_id=a.activity_id WHERE a.run_id=%s GROUP BY 1""", (rid,))

    def table(head, body):
        return "<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>" + "".join(
            "<tr>" + "".join(f"<td>{esc(str(x)) if x is not None else ''}</td>" for x in r) + "</tr>" for r in body) + "</table>"

    total = sum(float(r["tco2e"]) for r in scope)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>ESG report {run['period_start']} to {run['period_end']}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1c1d21}}
h1{{font-size:24px}}h2{{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;margin:8px 0}}th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #e5e5e5;font-size:13px}}
th{{background:#f4f4f2}}.note{{color:#555;font-size:12px}}@media print{{body{{margin:0}}}}</style></head><body>
<h1>ESG performance report</h1>
<p>Reporting period <b>{run['period_start']}</b> to <b>{run['period_end']}</b> · calculation run <code>{str(rid)[:8]}</code>
 · factor set <code>{esc(str(run['factor_set_version']))}</code> · generated {date.today()}</p>
<h2>Greenhouse gas inventory</h2><p>Total: <b>{total:,.1f} tCO2e</b></p>
{table(['Scope', 'tCO2e', 'Activity rows'], [(f"Scope {r['key']}", r['tco2e'], r['activities']) for r in scope])}
<h3>By category</h3>{table(['Category', 'tCO2e'], [(r['key'], r['tco2e']) for r in cat])}
<h3>By entity</h3>{table(['Entity', 'tCO2e'], [(r['key'], r['tco2e']) for r in ent])}
<h2>Key performance indicators</h2>
{table(['Pillar', 'Metric', 'Value', 'Unit', 'Target', 'Status'], [(k['pillar'], k['name'], round(float(k['value']), 2), k['unit'], k['target_value'], (k['status'] or '').replace('_', ' ')) for k in kp])}
<h2>Method and data quality</h2>
<p class="note">Emissions = activity quantity x emission factor. Data-quality split (tCO2e): {esc(', '.join(f"{q['data_quality']}: {q['t']}" for q in quality))}.</p>
{table(['Activity', 'Key', 'Factor', 'Unit', 'Source'], [(f['activity_type'], f['fuel_or_grid'], f['value'], f['uom'], f['source']) for f in facs])}
<p class="note">Notes from the run: {esc(run['notes'] or 'none')}</p></body></html>"""
    return HTMLResponse(page)
