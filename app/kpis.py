"""KPI engine: computes metric values for a period (port level, plus GHG metrics per terminal)
and scores each against its active target.

Status rule: for 'lower is better' metrics, value <= target is on_track, up to 10 % above is at_risk,
beyond that off_track (mirrored for 'higher is better'). Cumulative metrics (emissions, spills,
fatalities) have their annual target pro-rated to the days elapsed in the period.
"""
from . import db

CUMULATIVE = {"GHG_S1", "GHG_S2", "GHG_S3", "GHG_TOTAL", "SPILLS", "FATALITIES"}
TERMINAL_LEVEL = {"GHG_S1", "GHG_S2", "GHG_S3", "GHG_TOTAL", "GHG_INT_TEU"}

LATEST_SNAP = """(SELECT max(period_end) FROM employee_snapshot WHERE period_end BETWEEN %(s)s AND %(e)s)"""
HOURS = """(SELECT sum(employee_hours + coalesce(contractor_hours, 0)) FROM hours_worked
            WHERE period_start >= %(s)s AND period_end <= %(e)s)"""

# code -> SQL returning one column "v"
SQL_KPIS = {
    "RENEW_SHARE": """
        SELECT 100.0 * sum(r.energy_kwh) FILTER (WHERE r.energy_type = 'solar')
               / nullif(sum(r.energy_kwh) FILTER (WHERE r.energy_type IN ('solar','grid')), 0) AS v
        FROM energy_meter_reading r JOIN meter m ON m.meter_id = r.meter_id
        WHERE m.scope_boundary IN ('own','shared') AND r.ts >= %(s)s::date AND r.ts < %(e)s::date + 1""",
    "SHORE_POWER_SHARE": """
        SELECT 100.0 * sum(hours_at_berth) FILTER (WHERE shore_power_connected) / nullif(sum(hours_at_berth), 0) AS v
        FROM vessel_call WHERE atb >= %(s)s::date AND atb < %(e)s::date + 1""",
    "WASTE_DIVERSION": """
        SELECT 100.0 * sum(quantity_kg) FILTER (WHERE disposal_method IN ('recycle','recover')) / nullif(sum(quantity_kg), 0) AS v
        FROM waste_manifest WHERE manifest_date BETWEEN %(s)s AND %(e)s""",
    "AQ_PM25_MEAN": """
        SELECT avg(pm25) AS v FROM air_quality_reading WHERE ts >= %(s)s::date AND ts < %(e)s::date + 1""",
    "SPILLS": """
        SELECT count(*)::numeric AS v FROM environmental_incident
        WHERE incident_type = 'spill' AND occurred_at >= %(s)s::date AND occurred_at < %(e)s::date + 1""",
    "LTIFR": f"""
        SELECT 1000000.0 * (SELECT count(*) FROM safety_incident WHERE incident_type IN ('lost_time','fatality')
               AND occurred_at >= %(s)s::date AND occurred_at < %(e)s::date + 1) / nullif({HOURS}, 0) AS v""",
    "TRIR": f"""
        SELECT 1000000.0 * (SELECT count(*) FROM safety_incident WHERE incident_type IN ('lost_time','recordable','fatality')
               AND occurred_at >= %(s)s::date AND occurred_at < %(e)s::date + 1) / nullif({HOURS}, 0) AS v""",
    "FATALITIES": """
        SELECT count(*)::numeric AS v FROM safety_incident
        WHERE incident_type = 'fatality' AND occurred_at >= %(s)s::date AND occurred_at < %(e)s::date + 1""",
    "TRAINING_HRS_PER_EMP": f"""
        SELECT (SELECT sum(training_hours) FROM training_record WHERE period_start >= %(s)s AND period_end <= %(e)s)
               / nullif((SELECT sum(headcount) FROM employee_snapshot WHERE period_end = {LATEST_SNAP}), 0) AS v""",
    "FEMALE_SHARE": f"""
        SELECT 100.0 * sum(headcount) FILTER (WHERE gender = 'F') / nullif(sum(headcount), 0) AS v
        FROM employee_snapshot WHERE period_end = {LATEST_SNAP}""",
    "NATIONAL_SHARE": f"""
        SELECT 100.0 * sum(headcount) FILTER (WHERE nationality_group = 'UAE national') / nullif(sum(headcount), 0) AS v
        FROM employee_snapshot WHERE period_end = {LATEST_SNAP}""",
    "GRIEVANCE_AVG_DAYS": """
        SELECT avg(resolution_days) AS v FROM community_grievance
        WHERE received_date BETWEEN %(s)s AND %(e)s AND resolution_days IS NOT NULL""",
    "OPEN_AUDIT_FINDINGS": """
        SELECT count(*)::numeric AS v FROM audit_finding WHERE status <> 'closed'""",
    "CERT_VALID_SHARE": """
        SELECT 100.0 * count(*) FILTER (WHERE expiry_date >= %(e)s) / nullif(count(*), 0) AS v FROM certification""",
    "POLICY_REVIEW_OVERDUE": """
        SELECT count(*)::numeric AS v FROM policy WHERE next_review_date < %(e)s""",
    "TENANT_ESG_COVERAGE": """
        SELECT 100.0 * count(DISTINCT s.tenant_id) FILTER (WHERE s.status = 'approved')
               / nullif((SELECT count(*) FROM tenant), 0) AS v
        FROM tenant_esg_submission s WHERE s.period_end >= %(s)s AND s.period_start <= %(e)s""",
    "DATA_VALIDATED_SHARE": """
        SELECT 100.0 * sum(v) / nullif(sum(n), 0) AS v FROM (
          SELECT count(*) FILTER (WHERE validation_status='validated') v, count(*) n FROM vessel_call
          UNION ALL SELECT count(*) FILTER (WHERE validation_status='validated'), count(*) FROM fuel_transaction
          UNION ALL SELECT count(*) FILTER (WHERE validation_status='validated'), count(*) FROM safety_incident
          UNION ALL SELECT count(*) FILTER (WHERE validation_status='validated'), count(*) FROM waste_manifest
          UNION ALL SELECT count(*) FILTER (WHERE validation_status='validated'), count(*) FROM spend_record) t""",
}


def entity_terminals(c):
    """entity_id -> id of its terminal ancestor (or itself / root when none)."""
    ents = {e["entity_id"]: e for e in db.rows(c, "SELECT entity_id, parent_entity_id, entity_type FROM entity")}
    out = {}
    for eid in ents:
        cur, seen = eid, set()
        while cur and cur not in seen:
            seen.add(cur)
            if ents[cur]["entity_type"] == "terminal":
                break
            cur = ents[cur]["parent_entity_id"]
        out[eid] = cur if cur and ents[cur]["entity_type"] == "terminal" else None
    return out


def evaluate(value, target, direction, cumulative, days):
    if target is None:
        return "no_target", None
    t = float(target)
    if cumulative and days < 365:
        t = t * days / 365.0
    v = float(value)
    if direction == "lower":
        return ("on_track" if v <= t else "at_risk" if v <= t * 1.1 else "off_track"), t
    return ("on_track" if v >= t else "at_risk" if v >= t * 0.9 else "off_track"), t


def compute(c, run_id, start, end, root):
    metrics = {m["metric_code"]: m for m in db.rows(c, "SELECT metric_id, metric_code, direction FROM metric")}
    days = (end - start).days + 1
    terms = entity_terminals(c)
    params = {"s": start, "e": end}
    values = {}  # (code, entity_id) -> value

    # --- GHG-derived metrics, port level and per terminal
    g = db.rows(c, """
        SELECT a.scope, a.entity_id, sum(e.co2e_tonnes) AS t FROM ghg_activity_data a
        JOIN ghg_emission e ON e.activity_id = a.activity_id WHERE a.run_id = %s GROUP BY 1, 2""", (run_id,))
    teu = {r["entity_id"]: float(r["teu"]) for r in db.rows(c, """
        SELECT coalesce(terminal_id, %s) AS entity_id, sum(teu_moved) AS teu FROM vessel_call
        WHERE atb >= %s::date AND atb < %s::date + 1 GROUP BY 1""", (root, start, end))}
    by_scope, by_term = {1: 0.0, 2: 0.0, 3: 0.0}, {}
    for r in g:
        t = float(r["t"])
        by_scope[r["scope"]] += t
        tid = terms.get(r["entity_id"])
        if tid:
            by_term.setdefault(tid, {1: 0.0, 2: 0.0, 3: 0.0})[r["scope"]] += t
    values[("GHG_S1", root)], values[("GHG_S2", root)], values[("GHG_S3", root)] = by_scope[1], by_scope[2], by_scope[3]
    values[("GHG_TOTAL", root)] = sum(by_scope.values())
    total_teu = sum(teu.values())
    if total_teu:
        values[("GHG_INT_TEU", root)] = (by_scope[1] + by_scope[2]) * 1000.0 / total_teu
    for tid, sc in by_term.items():
        values[("GHG_S1", tid)], values[("GHG_S2", tid)], values[("GHG_S3", tid)] = sc[1], sc[2], sc[3]
        values[("GHG_TOTAL", tid)] = sum(sc.values())
        if teu.get(tid):
            values[("GHG_INT_TEU", tid)] = (sc[1] + sc[2]) * 1000.0 / teu[tid]

    # --- SQL metrics, port level
    for code, sql in SQL_KPIS.items():
        v = db.row(c, sql, params)["v"]
        if v is not None:
            values[(code, root)] = float(v)

    # --- targets and status
    n = 0
    for (code, eid), v in values.items():
        m = metrics.get(code)
        if not m:
            continue
        tgt = db.row(c, """
            SELECT target_id, target_value FROM kpi_target
            WHERE metric_id = %s AND entity_id = %s AND status = 'active' AND target_year >= %s
            ORDER BY target_year LIMIT 1""", (m["metric_id"], eid, end.year))
        status, _ = evaluate(v, tgt["target_value"] if tgt else None, m["direction"], code in CUMULATIVE, days)
        db.row(c, """
            INSERT INTO kpi_value (metric_id, entity_id, period_start, period_end, value, target_id, status, run_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING kpi_value_id""",
            (m["metric_id"], eid, start, end, round(v, 4), tgt["target_id"] if tgt else None, status, run_id))
        n += 1
    return n
