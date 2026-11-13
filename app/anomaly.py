"""Statistical anomaly detection on reported quantities. For each series (fuel by entity and fuel type, meter intervals by
meter, water volumes by entity, flow and source) recent values are compared with the series' own earlier history using
the modified z-score (median and MAD, robust to the odd outlier). Rejected records are ignored. No model, no training:
the maths is the same every run and every flag shows the numbers behind it."""
from statistics import median

from . import db

# name -> (table, id column, value column, time expression, group columns, status filter, label SQL)
DETECTORS = {
    "fuel": ("fuel_transaction", "txn_id", "litres", "ts", ["entity_id", "fuel_type"], "validation_status <> 'rejected'", "fuel_type"),
    "energy": ("energy_meter_reading", "reading_id", "energy_kwh", "ts", ["meter_id"], "true", "'meter'"),
    "water": ("water_record", "water_record_id", "volume_m3", "period_start::timestamptz", ["entity_id", "flow_type", "source_type"],
              "validation_status <> 'rejected'", "flow_type || ' ' || source_type"),
}
UNITS = {"fuel": "litres", "energy": "kWh", "water": "m3"}


def mod_z(x, med, mad):
    return 0.6745 * (x - med) / mad if mad else None


def find(c, p):
    kind = p.get("detector", "fuel")
    table, idc, valc, timec, groups, flt, label = DETECTORS[kind]
    look, base, zmax = int(p.get("lookback_days", 7)), int(p.get("baseline_days", 180)), float(p.get("z_threshold", 6))
    min_n = int(p.get("min_baseline", 8))
    gcols = ", ".join(groups)
    rows = db.rows(c, f"""
        SELECT {idc}::text AS id, {valc}::float AS v, ({timec}) AS t, ({timec}) >= now() - make_interval(days => %s) AS recent,
               ({gcols.replace(', ', "::text || '|' || ")}::text) AS g, {label} AS label
        FROM esg.{table}
        WHERE {flt} AND ({timec}) >= now() - make_interval(days => %s) AND ({timec}) <= now()""", (look, look + base))
    series = {}
    for r in rows:
        series.setdefault(r["g"], []).append(r)
    out = []
    for g, rs in series.items():
        hist = [r["v"] for r in rs if not r["recent"]]
        if len(hist) < min_n:
            continue
        med = median(hist)
        mad = median(abs(x - med) for x in hist)
        for r in (r for r in rs if r["recent"]):
            z = mod_z(r["v"], med, mad)
            if z is None:
                # flat history (every value alike): only a large jump is worth a flag
                big = med > 0 and (r["v"] >= 3 * med or r["v"] <= med / 3)
                if not big:
                    continue
                z = float("inf")
            if abs(z) >= zmax:
                out.append(dict(
                    dedupe_key=f"{kind}:{r['id']}",
                    title=f"Unusual {kind} value: {r['v']:,.0f} {UNITS[kind]} ({'high' if r['v'] > med else 'low'})",
                    message=f"{r['label']}: {r['v']:,.1f} {UNITS[kind]} on {r['t']:%Y-%m-%d}; usual level is {med:,.1f} "
                            f"(from {len(hist)} earlier records). Check for a typo, a wrong unit or a missing meter reset.",
                    context={"detector": kind, "table": table, "record_id": r["id"], "value": r["v"], "median": med,
                             "mad": mad, "modified_z": None if z == float("inf") else round(z, 1), "baseline_n": len(hist)}))
    return out


def validate(p, bad):
    if p.get("detector", "fuel") not in DETECTORS:
        raise bad(f"detector must be one of {sorted(DETECTORS)}")
    for k, lo, hi in (("lookback_days", 1, 90), ("baseline_days", 14, 730), ("min_baseline", 5, 500)):
        v = p.get(k, lo)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise bad(f"{k} must be a whole number between {lo} and {hi}")
    z = p.get("z_threshold", 6)
    if not isinstance(z, (int, float)) or isinstance(z, bool) or not 3 <= z <= 50:
        raise bad("z_threshold must be a number between 3 and 50")
