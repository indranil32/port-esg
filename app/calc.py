"""GHG inventory engine: turns captured activity data into tCO2e using versioned emission factors.

Every run creates a new calc_run plus its own activity and emission rows, so earlier results stay
reproducible. Each activity row keeps lineage (source table + up to 500 source record ids).

Scope mapping (documented assumptions, adjust to your reporting policy):
  Scope 1  fuel_transaction (fuel combustion), refrigerant_event (fugitive)
  Scope 2  grid electricity on own/shared meters, plus shore-power electricity the port purchases
  Scope 3  cat 1  purchased goods and services (spend-based, AED)
           cat 9  vessel auxiliary engines at berth when shore power is NOT connected
           cat 13 electricity consumed on tenant-controlled meters
"""
import os

from . import db, kpis

LOAD_FACTOR = float(os.environ.get("VESSEL_AUX_LOAD_FACTOR", 0.4))
RANGE = "{col} >= %(s)s::date AND {col} < %(e)s::date + 1"


def _ids(col):
    return f"(array_agg({col}::text))[1:500]"


# (activity_type, scope, scope3_category, uom, source_table, factor_activity, factor_key_is_row_key, sql)
ACTIVITIES = [
    ("fuel_combustion", 1, None, "L", "fuel_transaction", "fuel_combustion", True, f"""
        SELECT coalesce(f.entity_id, %(root)s) AS entity_id, date_trunc('month', f.ts)::date AS m,
               lower(f.fuel_type) AS k, sum(f.litres) AS qty, {_ids('f.txn_id')} AS ids
        FROM fuel_transaction f
        WHERE {RANGE.format(col='f.ts')} AND f.validation_status <> 'rejected' GROUP BY 1,2,3"""),
    ("refrigerant_leakage", 1, None, "kg", "refrigerant_event", "refrigerant_leakage", True, f"""
        SELECT coalesce(a.entity_id, %(root)s) AS entity_id, date_trunc('month', r.event_date)::date AS m,
               r.refrigerant_type AS k, sum(r.kg_added) AS qty, {_ids('r.event_id')} AS ids
        FROM refrigerant_event r LEFT JOIN asset a ON a.asset_id = r.asset_id
        WHERE r.event_date BETWEEN %(s)s AND %(e)s AND r.validation_status <> 'rejected' GROUP BY 1,2,3"""),
    ("purchased_electricity", 2, None, "kWh", "energy_meter_reading", "purchased_electricity", False, f"""
        SELECT mt.entity_id, date_trunc('month', r.ts)::date AS m, 'UAE_grid' AS k,
               sum(r.energy_kwh) AS qty, {_ids('r.reading_id')} AS ids
        FROM energy_meter_reading r JOIN meter mt ON mt.meter_id = r.meter_id
        WHERE r.energy_type = 'grid' AND mt.scope_boundary IN ('own','shared')
          AND {RANGE.format(col='r.ts')} GROUP BY 1,2,3"""),
    ("shore_power_electricity", 2, None, "kWh", "energy_meter_reading", "purchased_electricity", False, f"""
        SELECT mt.entity_id, date_trunc('month', r.ts)::date AS m, 'UAE_grid' AS k,
               sum(r.energy_kwh) AS qty, {_ids('r.reading_id')} AS ids
        FROM energy_meter_reading r JOIN meter mt ON mt.meter_id = r.meter_id
        WHERE r.energy_type = 'shore_power' AND {RANGE.format(col='r.ts')} GROUP BY 1,2,3"""),
    ("tenant_electricity", 3, 13, "kWh", "energy_meter_reading", "purchased_electricity", False, f"""
        SELECT mt.entity_id, date_trunc('month', r.ts)::date AS m, 'UAE_grid' AS k,
               sum(r.energy_kwh) AS qty, {_ids('r.reading_id')} AS ids
        FROM energy_meter_reading r JOIN meter mt ON mt.meter_id = r.meter_id
        WHERE r.energy_type = 'grid' AND mt.scope_boundary = 'tenant'
          AND {RANGE.format(col='r.ts')} GROUP BY 1,2,3"""),
    ("vessel_berth_aux_engine", 3, 9, "kWh", "vessel_call", "vessel_aux_engine", False, f"""
        SELECT coalesce(vc.terminal_id, b.terminal_id, %(root)s) AS entity_id,
               date_trunc('month', vc.atb)::date AS m, NULL AS k,
               sum(v.aux_engine_kw * %(lf)s * vc.hours_at_berth) AS qty, {_ids('vc.call_id')} AS ids
        FROM vessel_call vc JOIN vessel v ON v.imo_number = vc.imo_number
        LEFT JOIN berth b ON b.berth_id = vc.berth_id
        WHERE {RANGE.format(col='vc.atb')} AND NOT vc.shore_power_connected AND vc.hours_at_berth IS NOT NULL
          AND v.aux_engine_kw IS NOT NULL AND vc.validation_status <> 'rejected' GROUP BY 1,2"""),
    ("purchased_goods_services", 3, 1, "AED", "spend_record", "spend_based", True, f"""
        SELECT coalesce(s.entity_id, %(root)s) AS entity_id, date_trunc('month', s.spend_date)::date AS m,
               s.category AS k, sum(s.amount) AS qty, {_ids('s.spend_id')} AS ids
        FROM spend_record s
        WHERE s.scope3_category = 1 AND s.currency = 'AED' AND s.spend_date BETWEEN %(s)s AND %(e)s
          AND s.validation_status <> 'rejected' GROUP BY 1,2,3"""),
]


def root_entity(c):
    r = db.row(c, "SELECT entity_id FROM entity WHERE entity_type = 'port' AND parent_entity_id IS NULL ORDER BY code LIMIT 1")
    if not r:
        raise ValueError("No root entity of type 'port' defined")
    return r["entity_id"]


def find_factor(c, cache, activity, key, on):
    ck = (activity, key, on)
    if ck not in cache:
        cache[ck] = db.row(c, """
            SELECT factor_id, value, uom, source FROM emission_factor
            WHERE activity_type = %s AND coalesce(fuel_or_grid, '') = coalesce(%s, '') AND gas = 'CO2e'
              AND valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s)
            ORDER BY valid_from DESC LIMIT 1""", (activity, key, on, on))
    return cache[ck]


def month_bounds(m, start, end):
    from datetime import date, timedelta
    nxt = date(m.year + (m.month == 12), (m.month % 12) + 1, 1)
    return max(m, start), min(nxt - timedelta(days=1), end)


def run_calculation(c, actor, start, end):
    from .periods import locked_overlap
    lk = locked_overlap(c, start, end)
    if lk:
        raise ValueError(f"Period \"{lk['label']}\" ({lk['period_start']} to {lk['period_end']}) is locked. "
                         f"Unlock it with a reason before recalculating, or choose dates outside it.")
    root = root_entity(c)
    run = db.row(c, """
        INSERT INTO calc_run (period_start, period_end, triggered_by, factor_set_version)
        SELECT %s, %s, %s, coalesce(substr(md5(string_agg(factor_id::text, ',' ORDER BY factor_id)), 1, 8), 'none')
        FROM emission_factor WHERE valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s) RETURNING *""",
        (start, end, actor, end, end))
    rid = run["run_id"]
    cache, warnings, n_act, n_em = {}, [], 0, 0
    params = {"s": start, "e": end, "root": root, "lf": LOAD_FACTOR}
    for (atype, scope, cat, uom, src, fact_act, key_from_row, sql) in ACTIVITIES:
        gaps = set()
        for r in db.rows(c, sql, params):
            fkey = r["k"] if key_from_row else (r["k"] if r["k"] else None)
            fac = find_factor(c, cache, fact_act, fkey, min(r["m"].replace(day=28), end))
            if not fac:
                gaps.add(fkey or "-")
                continue
            ps, pe = month_bounds(r["m"], start, end)
            act = db.row(c, """
                INSERT INTO ghg_activity_data (run_id, entity_id, scope, scope3_category, activity_type, quantity, uom,
                                               period_start, period_end, data_quality, source_table, source_record_ids)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING activity_id""",
                (rid, r["entity_id"], scope, cat, atype if not key_from_row else f"{atype}:{r['k']}", r["qty"], uom, ps, pe,
                 "estimated" if atype in ("vessel_berth_aux_engine", "purchased_goods_services") else "measured",
                 src, r["ids"]))
            db.row(c, """
                INSERT INTO ghg_emission (activity_id, factor_id, run_id, gas, co2e_tonnes, method)
                VALUES (%s,%s,%s,'CO2e', %s, %s) RETURNING emission_id""",
                (act["activity_id"], fac["factor_id"], rid, float(r["qty"]) * float(fac["value"]) / 1000.0,
                 f"quantity x factor ({fac['uom']}; {fac['source']})"))
            n_act += 1
            n_em += 1
        if gaps:
            warnings.append(f"{atype}: no emission factor for {sorted(gaps)}; those quantities are excluded")
    kpi_count = kpis.compute(c, rid, start, end, root)
    totals = db.rows(c, """
        SELECT a.scope, round(sum(e.co2e_tonnes), 2) AS tco2e FROM ghg_activity_data a
        JOIN ghg_emission e ON e.activity_id = a.activity_id WHERE a.run_id = %s GROUP BY 1 ORDER BY 1""", (rid,))
    if n_act == 0:
        warnings.append("No activity data found in this period")
    db.row(c, "UPDATE calc_run SET status='succeeded', finished_at=now(), notes=%s WHERE run_id=%s RETURNING run_id",
           ("; ".join(warnings) or None, rid))
    db.audit(c, actor, "calc_run", "calc_run", rid, after={"period": [start, end], "activities": n_act, "kpis": kpi_count},
             detail="; ".join(warnings) or None)
    return {"run_id": rid, "period_start": start, "period_end": end, "activities": n_act, "kpis": kpi_count,
            "totals_by_scope": totals, "warnings": warnings}
