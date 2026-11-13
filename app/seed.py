"""Reference data (always) and clearly-labelled SYNTHETIC demo data (optional).

Everything seeded here is illustrative: emission factors, alert thresholds, framework mappings and
all operational figures must be replaced or reviewed before the system is used for real reporting.
"""
import math
import os
import random
import uuid
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Jsonb

from . import auth, calc, db

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo#2026")
ILLUSTRATIVE = "ILLUSTRATIVE - replace with approved factor"

SOURCES = [  # code, name, class, mode, frequency
    ("MANUAL", "Manual entry (web form)", "G_manual", "manual", "ad hoc"),
    ("API", "Direct API client", "D_enterprise", "api", "ad hoc"),
    ("CSV", "CSV upload", "G_manual", "batch", "ad hoc"),
    ("TOS", "Terminal Operating System", "A_operations", "api", "real-time"),
    ("PCS", "Port Community System", "A_operations", "api", "real-time"),
    ("AIS", "AIS feed", "A_operations", "stream", "real-time"),
    ("SCADA", "Energy SCADA / smart meters", "B_energy", "iot", "15 min"),
    ("ENV_GW", "Environmental sensor gateway", "C_environment", "iot", "1-15 min"),
    ("ERP", "ERP (finance, procurement, assets)", "D_enterprise", "batch", "daily"),
    ("HRMS", "HR system", "D_enterprise", "batch", "monthly"),
    ("HSE", "HSE / incident system", "D_enterprise", "api", "daily"),
    ("CMMS", "Maintenance system", "D_enterprise", "batch", "daily"),
    ("TENANT_PORTAL", "Tenant ESG portal", "E_third_party", "portal", "quarterly"),
    ("REF", "Reference data (factors, frameworks)", "F_reference", "batch", "annual"),
    ("FIELD_APP", "Field evidence capture (mobile web, photo + OCR)", "G_manual", "manual", "ad hoc"),
]

# code, name, pillar, unit, direction, frequency, source classes
METRICS = [
    ("GHG_S1", "Scope 1 GHG emissions", "E", "tCO2e", "lower", "monthly", ["B_energy", "D_enterprise"]),
    ("GHG_S2", "Scope 2 GHG emissions", "E", "tCO2e", "lower", "monthly", ["B_energy"]),
    ("GHG_S3", "Scope 3 GHG emissions (screened categories)", "E", "tCO2e", "lower", "monthly", ["A_operations", "B_energy", "D_enterprise"]),
    ("GHG_TOTAL", "Total GHG emissions", "E", "tCO2e", "lower", "monthly", []),
    ("GHG_INT_TEU", "GHG intensity (Scope 1+2 per TEU)", "E", "kgCO2e/TEU", "lower", "monthly", ["A_operations", "B_energy"]),
    ("RENEW_SHARE", "Renewable share of own electricity", "E", "%", "higher", "monthly", ["B_energy"]),
    ("SHORE_POWER_SHARE", "Berth hours on shore power", "E", "%", "higher", "monthly", ["A_operations"]),
    ("WASTE_DIVERSION", "Waste diverted from disposal", "E", "%", "higher", "monthly", ["G_manual"]),
    ("AQ_PM25_MEAN", "Mean PM2.5 at monitoring stations", "E", "ug/m3", "lower", "daily", ["C_environment"]),
    ("WATER_WITHDRAWAL", "Water withdrawal", "E", "m3", "lower", "monthly", ["G_manual"]),
    ("WATER_CONSUMPTION", "Water consumption (withdrawal less discharge)", "E", "m3", "lower", "monthly", ["G_manual"]),
    ("WATER_RECYCLED_SHARE", "Water recycled or reused", "E", "%", "higher", "monthly", ["G_manual"]),
    ("SPILLS", "Reportable spills", "E", "count", "lower", "monthly", ["C_environment"]),
    ("LTIFR", "Lost-time injury frequency rate", "S", "per million hours", "lower", "monthly", ["D_enterprise"]),
    ("TRIR", "Total recordable incident rate", "S", "per million hours", "lower", "monthly", ["D_enterprise"]),
    ("FATALITIES", "Work-related fatalities", "S", "count", "lower", "monthly", ["D_enterprise"]),
    ("TRAINING_HRS_PER_EMP", "Training hours per employee", "S", "hours", "higher", "monthly", ["D_enterprise"]),
    ("FEMALE_SHARE", "Women in workforce", "S", "%", "higher", "monthly", ["D_enterprise"]),
    ("NATIONAL_SHARE", "National workforce share", "S", "%", "higher", "monthly", ["D_enterprise"]),
    ("GRIEVANCE_AVG_DAYS", "Average grievance resolution time", "S", "days", "lower", "monthly", ["G_manual"]),
    ("OPEN_AUDIT_FINDINGS", "Open audit findings", "G", "count", "lower", "monthly", ["D_enterprise"]),
    ("CERT_VALID_SHARE", "Certifications valid at period end", "G", "%", "higher", "monthly", ["D_enterprise"]),
    ("POLICY_REVIEW_OVERDUE", "Policies overdue for review", "G", "count", "lower", "monthly", ["D_enterprise"]),
    ("TENANT_ESG_COVERAGE", "Tenants with approved ESG submission", "G", "%", "higher", "quarterly", []),
    ("DATA_VALIDATED_SHARE", "Key records validated (four-eyes)", "G", "%", "higher", "monthly", []),
    ("TENANT_WASTE_KG", "Tenant waste generated", "E", "kg", "lower", "quarterly", ["E_third_party"]),
    ("TENANT_INCIDENTS", "Tenant recordable incidents", "S", "count", "lower", "quarterly", ["E_third_party"]),
    ("TENANT_TRAINING_HRS", "Tenant safety training hours", "S", "hours", "higher", "quarterly", ["E_third_party"]),
]

FRAMEWORKS = [("GRI", "GRI Standards", "2021"), ("IFRS_S2", "IFRS S2 Climate-related Disclosures", "2023")]
# framework, disclosure, title, [metric codes]  (indicative mappings: confirm against your reporting scope)
REQUIREMENTS = [
    ("GRI", "302-1", "Energy consumption within the organization", ["RENEW_SHARE"]),
    ("GRI", "305-1", "Direct (Scope 1) GHG emissions", ["GHG_S1"]),
    ("GRI", "305-2", "Energy indirect (Scope 2) GHG emissions", ["GHG_S2"]),
    ("GRI", "305-3", "Other indirect (Scope 3) GHG emissions", ["GHG_S3"]),
    ("GRI", "305-4", "GHG emissions intensity", ["GHG_INT_TEU"]),
    ("GRI", "305-7", "Nitrogen oxides, sulfur oxides and other significant air emissions", ["AQ_PM25_MEAN"]),
    ("GRI", "306-4", "Waste diverted from disposal", ["WASTE_DIVERSION"]),
    ("GRI", "403-9", "Work-related injuries", ["LTIFR", "TRIR", "FATALITIES", "TENANT_INCIDENTS"]),
    ("GRI", "404-1", "Average hours of training per year per employee", ["TRAINING_HRS_PER_EMP"]),
    ("GRI", "405-1", "Diversity of governance bodies and employees", ["FEMALE_SHARE", "NATIONAL_SHARE"]),
    ("GRI", "303-3", "Water withdrawal", ["WATER_WITHDRAWAL", "WATER_RECYCLED_SHARE"]),
    ("GRI", "303-5", "Water consumption", ["WATER_CONSUMPTION"]),
    ("GRI", "201-2", "Financial implications and other risks and opportunities due to climate change", []),
    ("IFRS_S2", "S2.29(a)(i)", "Absolute gross Scope 1 greenhouse gas emissions", ["GHG_S1"]),
    ("IFRS_S2", "S2.29(a)(ii)", "Absolute gross Scope 2 greenhouse gas emissions", ["GHG_S2"]),
    ("IFRS_S2", "S2.29(a)(iii)", "Absolute gross Scope 3 greenhouse gas emissions", ["GHG_S3"]),
    ("IFRS_S2", "S2.33-36", "Climate-related targets and progress", ["GHG_TOTAL", "GHG_INT_TEU"]),
    ("IFRS_S2", "S2.22", "Climate resilience (scenario analysis)", []),
]

# activity_type, fuel_or_grid, kg CO2e per unit, unit
FACTORS = [
    ("fuel_combustion", "diesel", 2.68, "kgCO2e/L"), ("fuel_combustion", "petrol", 2.31, "kgCO2e/L"),
    ("fuel_combustion", "lng", 2.75, "kgCO2e/kg"),
    ("refrigerant_leakage", "R410A", 2088, "kgCO2e/kg"), ("refrigerant_leakage", "R134a", 1430, "kgCO2e/kg"),
    ("purchased_electricity", "UAE_grid", 0.40, "kgCO2e/kWh"),
    ("vessel_aux_engine", None, 0.72, "kgCO2e/kWh"),
    ("spend_based", "construction", 0.45, "kgCO2e/AED"), ("spend_based", "maintenance_services", 0.20, "kgCO2e/AED"),
    ("spend_based", "it_services", 0.10, "kgCO2e/AED"), ("spend_based", "fuel_lubricants", 0.80, "kgCO2e/AED"),
    ("spend_based", "equipment", 0.35, "kgCO2e/AED"),
]

RULES = [  # name, type, severity, params, channels
    ("PM2.5 24h level above 35 ug/m3 (illustrative threshold)", "threshold_reading", "warning",
     {"table": "air_quality_reading", "column": "pm25", "op": ">", "value": 35, "window_hours": 24}, ["webhook"]),
    ("Harbour hydrocarbons above 1 ppm (illustrative threshold)", "threshold_reading", "critical",
     {"table": "water_quality_reading", "column": "hydrocarbon_ppm", "op": ">", "value": 1.0, "window_hours": 48}, ["webhook", "email"]),
    ("Environmental incident: high or critical", "new_incident", "critical",
     {"source": "environmental_incident", "severities": ["high", "critical"], "lookback_days": 14}, ["webhook", "email"]),
    ("Safety: fatality or lost-time incident", "new_incident", "critical",
     {"source": "safety_incident", "types": ["fatality", "lost_time"], "lookback_days": 14}, ["webhook", "email"]),
    ("Meter silent for 48 hours", "data_freshness", "warning", {"max_age_hours": 48}, ["webhook"]),
    ("Certification expiring within 30 days", "expiry", "warning", {"table": "certification", "days_ahead": 30}, ["email"]),
    ("Policy review due within 30 days", "expiry", "info", {"table": "policy", "days_ahead": 30}, []),
    ("Tenant submission waiting over 7 days", "workflow_backlog", "warning", {"kind": "submissions", "older_than_days": 7}, ["email"]),
    ("Records pending validation over 7 days", "workflow_backlog", "info", {"kind": "validation", "older_than_days": 7}, []),
    ("KPI off track", "kpi_off_track", "warning", {}, []),
    ("Monthly hours worked missing", "missing_monthly", "warning", {"table": "hours_worked"}, ["email"]),
    ("Suspect field evidence (re-used photo, wrong meter, value not matching photo)", "evidence_flagged", "warning",
     {"lookback_days": 14}, ["email"]),
]


def seed_reference(c):
    for code, name, cls, mode, freq in SOURCES:
        c.execute("INSERT INTO source_system (code, name, source_class, ingestion_mode, refresh_frequency, owner) "
                  "VALUES (%s,%s,%s,%s,%s,'ESG team') ON CONFLICT (code) DO NOTHING", (code, name, cls, mode, freq))
    for code, name, ver in FRAMEWORKS:
        c.execute("INSERT INTO framework (code, name, version_label) VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING", (code, name, ver))
    for code, name, pillar, unit, direction, freq, classes in METRICS:
        c.execute("INSERT INTO metric (metric_code, name, pillar, unit, direction, frequency, source_classes, owner) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,'ESG team') ON CONFLICT (metric_code) DO NOTHING",
                  (code, name, pillar, unit, direction, freq, classes))
    for fw, disc, title, metrics in REQUIREMENTS:
        r = db.row(c, "INSERT INTO framework_requirement (framework_id, disclosure_code, title) "
                      "SELECT framework_id, %s, %s FROM framework WHERE code = %s "
                      "ON CONFLICT (framework_id, disclosure_code) DO UPDATE SET title = EXCLUDED.title RETURNING requirement_id",
                   (disc, title, fw))
        for m in metrics:
            c.execute("INSERT INTO metric_requirement_map (metric_id, requirement_id) "
                      "SELECT metric_id, %s FROM metric WHERE metric_code = %s ON CONFLICT DO NOTHING", (r["requirement_id"], m))
    for name, rtype, sev, params, ch in RULES:
        c.execute("INSERT INTO alert_rule (name, rule_type, severity, params, channels, recipients) VALUES (%s,%s,%s,%s,%s,%s) "
                  "ON CONFLICT (name) DO NOTHING", (name, rtype, sev, Jsonb(params), ch, ["esg-team@example.org"] if "email" in ch else []))


def seed_illustrative_factors(c):
    """Demo only. Production deployments must load approved emission factors themselves."""
    if not db.row(c, "SELECT 1 FROM emission_factor LIMIT 1"):
        for act, key, val, uom in FACTORS:
            c.execute("INSERT INTO emission_factor (source, activity_type, fuel_or_grid, gas, value, uom, gwp_basis, region, valid_from) "
                      "VALUES (%s,%s,%s,'CO2e',%s,%s,'AR5','UAE','2020-01-01')", (ILLUSTRATIVE, act, key, val, uom))


def create_user(c, email, name, role, password, tenant_id=None):
    c.execute("INSERT INTO app_user (email, full_name, role, password_hash, tenant_id) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING",
              (email, name, role, auth.hash_password(password), tenant_id))


def bulk(c, table, cols, data):
    if data:
        with c.cursor() as cur:
            cur.executemany(f"INSERT INTO esg.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", data)


FIELD_EMAIL = "field.operator@adports-demo.local"


def meter_photo(reading_text, serial, taken_at, label="ILLUSTRATIVE DEMO IMAGE"):
    """A synthetic photo of a register display with EXIF capture time and GPS (Khalifa Port area). Demo only."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 600), (58, 62, 66))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((60, 60, 840, 540), 24, fill=(214, 217, 210), outline=(30, 30, 30), width=6)
    d.rectangle((130, 140, 770, 300), fill=(168, 184, 160), outline=(20, 20, 20), width=5)
    d.text((160, 165), reading_text, font=ImageFont.load_default(100), fill=(20, 28, 18))
    d.text((640, 330), "kWh", font=ImageFont.load_default(56), fill=(30, 30, 30))
    d.text((130, 350), f"SN {serial}", font=ImageFont.load_default(52), fill=(30, 30, 30))
    d.text((130, 480), label, font=ImageFont.load_default(26), fill=(90, 90, 90))
    exif = img.getexif()
    exif[0x010F], exif[0x0110] = "Demo", "Handset 1"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = taken_at.astimezone(timezone(timedelta(hours=4))).strftime("%Y:%m:%d %H:%M:%S")
    sub[0x9011] = "+04:00"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (24.0, 48.0, 10.0), "E", (54.0, 40.0, 5.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def seed_field_evidence(c, terms, today, now):
    """Two hand-read meters with validated history, plus three pending field submissions that show what an approver
    sees: a clean one, a typed value that disagrees with the photo, and a re-used photo. All values are illustrative."""
    from . import evidence
    from .evidence import ReadingBody
    ss = db.row(c, "SELECT source_system_id FROM source_system WHERE code = 'FIELD_APP'")["source_system_id"]
    specs = [("KP-WSHOP", "KP", 1, 6, 604300.0, 410.0), ("ZP-GATE", "ZP", 10, 5, 99900.0, 37.0)]
    ids = {}
    for code, tc, mult, digits, start, per_day in specs:
        mid = uuid.uuid4()
        ids[code] = mid
        c.execute("INSERT INTO meter (meter_id, meter_code, entity_id, meter_type, scope_boundary, uom, energy_type, multiplier, "
                  "register_digits, location_desc) VALUES (%s,%s,%s,'electricity','own','kWh','grid',%s,%s,%s)",
                  (mid, code, terms[tc], mult, digits, "Hand-read sub-meter (sample)"))
        prev = None
        for k in range(6, 0, -1):                       # six daily readings, validated
            at = datetime.combine(today - timedelta(days=k), time(3, 30), tzinfo=timezone.utc)
            val = start + per_day * (6 - k)
            if val >= 10 ** digits:                     # the ZP-GATE dial wraps once in the history
                val -= 10 ** digits
            wrapped, prev = prev is not None and val < prev, val
            c.execute("INSERT INTO meter_register_reading (meter_id, read_at, register_value, uom, rollover, source_system_id, "
                      "source_record_id, data_quality, validation_status, created_by, approved_by) "
                      "VALUES (%s,%s,%s,'kWh',%s,%s,%s,'manual','validated',%s,'data.owner@adports-demo.local')",
                      (mid, at, val, wrapped, ss, f"FIELD-{code}-{k}", FIELD_EMAIL))
        evidence.rederive(c, mid, "seed.loader")
    op = {"email": FIELD_EMAIL, "role": "field_operator"}
    t0 = now - timedelta(hours=2)

    def submit(code, shown, typed, taken, reuse=None):
        data = reuse or meter_photo(shown, code, taken)
        cap = evidence.process_image(c, FIELD_EMAIL, data, str(ids[code]), client_at=taken, gps_lat=24.8029, gps_lon=54.6681)
        evidence.submit_reading_tx(c, op, ReadingBody(capture_id=str(cap["capture_id"]), meter_id=str(ids[code]),
                                                       register_value=typed, read_at=taken))
        return data
    a = submit("KP-WSHOP", "606760.0", 606760.0, t0)                          # clean
    submit("ZP-GATE", "00122.0", 212.0, t0)                                   # digits transposed when typing
    submit("KP-WSHOP", "606760.0", 607170.0, t0 + timedelta(minutes=30), reuse=a)   # same file sent again


def seed_demo(c, today: date | None = None):
    rng = random.Random(2026)
    seed_illustrative_factors(c)
    today = today or date.today()
    now = datetime.now(timezone.utc)
    y0 = date(today.year, 1, 1)
    ndays = (today - y0).days
    ss = {r["code"]: r["source_system_id"] for r in db.rows(c, "SELECT code, source_system_id FROM source_system")}
    U = lambda: uuid.uuid4()
    day_ts = lambda d, h=12, m=30: datetime.combine(d, time(h, m), tzinfo=timezone.utc)
    season = lambda d: 1 + 0.22 * math.sin((d.timetuple().tm_yday - 100) / 365 * 2 * math.pi)

    def meta(src, n, validated=True, dq="measured", by="seed.loader"):
        return (ss[src], f"{src}-{n}", dq, "validated" if validated else "pending", by, "seed.approver" if validated else None)
    MC = ["source_system_id", "source_record_id", "data_quality", "validation_status", "created_by", "approved_by"]

    # ---- organisation
    root = U()
    bulk(c, "entity", ["entity_id", "entity_type", "code", "name", "country_code"],
         [(root, "port", "ADPG-PORTS", "AD Ports Group - Ports Cluster (SAMPLE DATA)", "AE")])
    terms = {}
    for code, name in [("KP", "Khalifa Port"), ("ZP", "Zayed Port"), ("FJ", "Fujairah Terminals"), ("MP", "Musaffah Port")]:
        terms[code] = U()
        bulk(c, "entity", ["entity_id", "parent_entity_id", "entity_type", "code", "name", "country_code"],
             [(terms[code], root, "terminal", code, name, "AE")])
    tcodes = list(terms)
    tenants = []
    for i, (name, ttype, tc, esg) in enumerate([
            ("Container Terminal Operator (sample)", "terminal_operator", "KP", True),
            ("Bulk Handling Company (sample)", "terminal_operator", "ZP", True),
            ("Logistics Park Tenant (sample)", "logistics", "KP", True),
            ("Ro-Ro Operator (sample)", "terminal_operator", "ZP", True),
            ("Industrial Tenant A (sample)", "industrial", "MP", False),
            ("Marine Services Company (sample)", "service", "FJ", False)], 1):
        eid, tid = U(), U()
        bulk(c, "entity", ["entity_id", "parent_entity_id", "entity_type", "code", "name"], [(eid, terms[tc], "tenant", f"TEN-{i:02d}", name)])
        bulk(c, "tenant", ["tenant_id", "entity_id", "name", "tenant_type", "contract_ref", "esg_clause", "operational_control"],
             [(tid, eid, name, ttype, f"LEASE-{i:03d}", esg, False)])
        tenants.append((tid, eid, tc))

    # ---- users
    tenant1 = tenants[0][0]
    create_user(c, "esg.admin@adports-demo.local", "ESG Administrator", "esg_admin", DEMO_PASSWORD)
    create_user(c, "data.owner@adports-demo.local", "Data Owner (HSE & Ops)", "data_owner", DEMO_PASSWORD)
    create_user(c, "auditor@adports-demo.local", "Internal Auditor", "auditor", DEMO_PASSWORD)
    create_user(c, "tenant1@adports-demo.local", "Tenant 1 Contact", "tenant", DEMO_PASSWORD, tenant1)
    create_user(c, FIELD_EMAIL, "Field Operator (Khalifa Port)", "field_operator", DEMO_PASSWORD)

    # ---- vessels, berths, calls
    vtypes = [("container", 2500, 5500, 4000, 18000), ("bulk", 1200, 2200, 0, 0), ("tanker", 1500, 3000, 0, 0),
              ("roro", 1800, 3200, 0, 0), ("general_cargo", 600, 1400, 0, 0)]
    vessels = []
    for i in range(80):
        vt, lo, hi, tlo, thi = vtypes[rng.choices(range(5), [40, 20, 15, 15, 10])[0]]
        imo = str(9100000 + i * 7919)
        vessels.append((imo, f"SAMPLE VESSEL {i + 1:02d}", vt, rng.choice(["PA", "LR", "MH", "AE", "SG"]), rng.randint(15000, 150000),
                        rng.randint(20000, 200000), rng.randint(tlo, thi) if thi else None, rng.randint(2000, 2022), "medium-speed diesel",
                        rng.randint(8000, 60000), rng.randint(lo, hi), "VLSFO", vt))
    bulk(c, "vessel", ["imo_number", "vessel_name", "vessel_type", "flag", "gross_tonnage", "dwt", "teu_capacity", "built_year",
                       "engine_type", "main_engine_kw", "aux_engine_kw", "default_fuel_type"], [v[:12] for v in vessels])
    berths = {}
    for tc, n, cap in [("KP", 4, 2), ("ZP", 3, 1), ("FJ", 3, 0), ("MP", 2, 0)]:
        for b in range(1, n + 1):
            bid = U()
            berths.setdefault(tc, []).append((bid, b <= cap))
            bulk(c, "berth", ["berth_id", "terminal_id", "berth_code", "length_m", "shore_power_capable"],
                 [(bid, terms[tc], f"{tc}-B{b}", rng.randint(250, 400), b <= cap)])
    calls = []
    for n in range(int(ndays * 2.3)):
        tc = rng.choices(tcodes, [50, 20, 20, 10])[0]
        v = rng.choice(vessels)
        bid, cap = rng.choice(berths[tc])
        atb = day_ts(y0 + timedelta(days=rng.randint(0, max(ndays - 1, 0))), rng.randint(0, 23), rng.randint(0, 59))
        hrs = round(max(6, rng.gauss(26 if v[2] == "container" else 40, 9)), 1)
        anch = round(rng.uniform(0, 10), 1)
        teu = rng.randint(600, 4200) if v[2] == "container" else 0
        sp = cap and atb.date() > date(today.year, 3, 1) and rng.random() < 0.5
        calls.append((U(), v[0], bid, terms[tc], f"V{n:05d}", atb - timedelta(hours=anch), atb, atb + timedelta(hours=hrs), anch, hrs, 0.5, bool(sp),
                      "none" if sp else "VLSFO", rng.randint(8000, 60000), teu) +
                     meta("TOS", n, validated=(today - atb.date()).days > 5))
    bulk(c, "vessel_call", ["call_id", "imo_number", "berth_id", "terminal_id", "voyage_ref", "ata", "atb", "atd", "hours_at_anchor",
                            "hours_at_berth", "hours_manoeuvring", "shore_power_connected", "fuel_type_at_berth", "cargo_tonnes",
                            "teu_moved"] + MC, calls)

    # ---- assets, meters
    assets = []
    for tc in tcodes:
        spec = [("CR", "crane", None, True, 4), ("RTG", "rtg", "diesel", False, 4), ("TRK", "truck", "diesel", False, 3),
                ("GEN", "generator", "diesel", False, 1), ("RS", "reach_stacker", "diesel", False, 1), ("BLD", "building", None, True, 1)]
        for pre, atype, fuel, elec, n in spec:
            for k in range(1, n + 1):
                electrified = elec or (atype == "rtg" and k <= 2)
                assets.append((U(), terms[tc], f"{tc}-{pre}{k}", atype, None if electrified else fuel, electrified,
                               rng.randint(60, 400), tc))
    bulk(c, "asset", ["asset_id", "entity_id", "asset_code", "asset_type", "fuel_type", "electrified", "rated_kw"], [a[:7] for a in assets])
    meters = []  # id, code, entity, tenant, type, boundary, kind, base_kwh
    base = {"KP": (260e3, 90e3, 140e3, 18e3), "ZP": (120e3, 45e3, 60e3, 8e3), "FJ": (90e3, 30e3, 25e3, 6e3), "MP": (50e3, 20e3, 40e3, 3e3)}
    for tc in tcodes:
        tenant_for = next(t for t in tenants if t[2] == tc)
        for kind, bnd, b, ten in [("MAIN", "own", base[tc][0], None), ("OPS", "own", base[tc][1], None),
                                  ("TEN", "tenant", base[tc][2], tenant_for), ("PV", "own", base[tc][3], None)]:
            meters.append((U(), f"{tc}-{kind}", terms[tc], ten[0] if ten else None, "electricity", bnd, kind, b, tc))
    for tc, b in [("KP", 6000), ("ZP", 2500)]:
        meters.append((U(), f"{tc}-SHORE", terms[tc], None, "shore_power", "own", "SHORE", b, tc))
    bulk(c, "meter", ["meter_id", "meter_code", "entity_id", "tenant_id", "meter_type", "scope_boundary", "uom", "energy_type",
                      "multiplier", "register_digits"],
         [(m[0], m[1], m[2], m[3], m[4], m[5], "kWh", {"PV": "solar", "SHORE": "shore_power"}.get(m[6], "grid"),
           10 if m[6] == "TEN" else 1, 7) for m in meters])   # x10 on tenant meters = illustrative CT ratio
    readings = []
    for m in meters:
        for d in range(ndays):
            day = y0 + timedelta(days=d)
            if m[1] == "ZP-OPS" and (today - day).days <= 5:      # silent meter -> data-freshness alert
                continue
            etype = {"PV": "solar", "SHORE": "shore_power"}.get(m[6], "grid")
            ramp = min(1, d / 120) if m[6] == "SHORE" else 1
            readings.append((m[0], day_ts(day, 0, 0), round(m[7] * season(day) * ramp * rng.uniform(0.92, 1.08), 1), etype, ss["SCADA"]))
    bulk(c, "energy_meter_reading", ["meter_id", "ts", "energy_kwh", "energy_type", "source_system_id"], readings)

    seed_field_evidence(c, terms, today, now)

    diesel = [a for a in assets if a[4] == "diesel"]
    fuel = []
    for n, a in enumerate(diesel):
        for d in range(rng.randint(0, 3), ndays, 4):
            day = y0 + timedelta(days=d)
            fuel.append((U(), a[0], a[1], "diesel", round(rng.uniform(600, 900) if "GEN" in a[2] else rng.uniform(150, 600), 1),
                         day_ts(day, rng.randint(5, 20)), "Depot pump 1") + meta("ERP", f"F{n}-{d}", validated=(today - day).days > 7))
    bulk(c, "fuel_transaction", ["txn_id", "asset_id", "entity_id", "fuel_type", "litres", "ts", "dispensing_point"] + MC, fuel)
    refr = [(U(), rng.choice(assets)[0], f"WO-{1000 + i}", rng.choice(["R410A", "R134a"]), round(rng.uniform(2, 15), 1),
             y0 + timedelta(days=rng.randint(0, max(ndays - 1, 0)))) + meta("CMMS", i) for i in range(18)]
    bulk(c, "refrigerant_event", ["event_id", "asset_id", "work_order_ref", "refrigerant_type", "kg_added", "event_date"] + MC, refr)

    # ---- environment
    sensors = []
    for tc in tcodes:
        for k in (1, 2):
            sensors.append((U(), f"AQ-{tc}-{k}", "air", tc))
    sensors += [(U(), "WQ-KP-1", "water", "KP"), (U(), "WQ-ZP-1", "water", "ZP"), (U(), "N-KP-1", "noise", "KP"),
                (U(), "N-FJ-1", "noise", "FJ"), (U(), "W-KP-1", "weather", "KP")]
    bulk(c, "sensor", ["sensor_id", "sensor_code", "sensor_type", "entity_id", "calibration_date"],
         [(s[0], s[1], s[2], terms[s[3]], today - timedelta(days=rng.randint(20, 300))) for s in sensors])
    air, water, noise, wx = [], [], [], []
    for s in sensors:
        for d in range(ndays):
            day = y0 + timedelta(days=d)
            ts = day_ts(day)
            if s[2] == "air":
                pm = max(5, rng.gauss(22 * (0.8 + 0.4 * season(day) / 1.22), 5))
                air.append((s[0], ts, round(pm * 1.9, 1), round(pm, 1), round(rng.uniform(10, 40), 1), round(rng.uniform(2, 12), 1),
                            round(rng.uniform(0.3, 1.2), 2), round(rng.uniform(40, 70), 1), ss["ENV_GW"]))
            elif s[2] == "water":
                water.append((s[0], ts, round(rng.uniform(7.9, 8.2), 2), round(rng.uniform(2, 6), 1), round(rng.uniform(5.5, 7.5), 1),
                              round(24 + 8 * (season(day) - 0.78) / 0.44, 1), round(rng.uniform(0.1, 0.6), 2), ss["ENV_GW"]))
            elif s[2] == "noise":
                noise.append((s[0], ts, round(rng.uniform(55, 68), 1), round(rng.uniform(72, 90), 1), ss["ENV_GW"]))
            else:
                wx.append((s[0], ts, round(20 + 22 * (season(day) - 0.78) / 0.44 + rng.uniform(-3, 3), 1), round(rng.uniform(2, 9), 1),
                           rng.randint(0, 359), round(rng.uniform(35, 85), 1), round(rng.uniform(-0.5, 1.5), 2), ss["ENV_GW"]))
        if s[2] == "air":  # hourly for the last 72 h; Fujairah station spikes in the last 36 h
            top = now.replace(minute=0, second=0, microsecond=0)
            for h in range(72):
                pm = rng.uniform(46, 62) if (s[1] == "AQ-FJ-1" and h < 36) else max(5, rng.gauss(22, 4))
                air.append((s[0], top - timedelta(hours=h), round(pm * 1.9, 1), round(pm, 1), round(rng.uniform(10, 40), 1),
                            round(rng.uniform(2, 12), 1), round(rng.uniform(0.3, 1.2), 2), round(rng.uniform(40, 70), 1), ss["ENV_GW"]))
    bulk(c, "air_quality_reading", ["sensor_id", "ts", "pm10", "pm25", "no2", "so2", "co", "o3", "source_system_id"], air)
    bulk(c, "water_quality_reading", ["sensor_id", "ts", "ph", "turbidity_ntu", "dissolved_oxygen_mgl", "temperature_c", "hydrocarbon_ppm", "source_system_id"], water)
    bulk(c, "noise_reading", ["sensor_id", "ts", "leq_db", "lmax_db", "source_system_id"], noise)
    bulk(c, "weather_observation", ["sensor_id", "ts", "temp_c", "wind_speed_ms", "wind_dir_deg", "humidity_pct", "tide_level_m", "source_system_id"], wx)
    inc = [(U(), terms["KP"], "spill", now - timedelta(days=6), "Berth KP-B2 bunkering", 800, "L", "critical", "Hose coupling failure", "investigating", True)]
    for i in range(6):
        inc.append((U(), terms[rng.choice(tcodes)], rng.choice(["spill", "exceedance", "odour"]), now - timedelta(days=rng.randint(20, max(ndays, 21))),
                    "Yard area", rng.randint(5, 80), "L", rng.choice(["low", "medium"]), "Minor release, contained", "closed", False))
    bulk(c, "environmental_incident", ["incident_id", "entity_id", "incident_type", "occurred_at", "location_desc", "quantity", "quantity_uom",
                                       "severity", "root_cause", "status", "regulator_notified"],
         inc)

    # ---- enterprise
    cats = ["construction", "maintenance_services", "it_services", "fuel_lubricants", "equipment"]
    spend = [(U(), terms[rng.choice(tcodes)], f"PO-{i:05d}", f"SUP-{rng.randint(1, 40):03d}", rng.choice(cats), round(rng.uniform(5e3, 9e5), 0),
              "AED", y0 + timedelta(days=rng.randint(0, max(ndays - 1, 0))), 1) for i in range(280)]
    spend = [s[:4] + (s[4],) + s[5:] + meta("ERP", s[2]) for s in spend]
    bulk(c, "spend_record", ["spend_id", "entity_id", "po_ref", "supplier_ref", "category", "amount", "currency", "spend_date", "scope3_category"] + MC, spend)
    last_full = today.replace(day=1) - timedelta(days=1)
    months = [date(today.year, m, 1) for m in range(1, last_full.month + 1)]
    heads = {"KP": 1200, "ZP": 600, "FJ": 450, "MP": 300}
    emp, trn, hrs = [], [], []
    for mi, m in enumerate(months):
        end = (date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1))
        for tc in tcodes:
            for gender, gs in (("F", 0.22), ("M", 0.78)):
                for nat, ns in (("UAE national", 0.30), ("Expatriate", 0.70)):
                    emp.append((U(), terms[tc], end, "Operations", gender, nat, "30-45", "all", "permanent",
                                int(heads[tc] * gs * ns), rng.randint(0, 2), rng.randint(0, 1)) + meta("HRMS", f"{tc}{mi}{gender}{nat[:1]}"))
            for cat in ("HSE", "Technical", "Leadership"):
                trn.append((U(), terms[tc], m, end, cat, round(heads[tc] * rng.uniform(1.2, 3.0), 1), int(heads[tc] * rng.uniform(0.2, 0.6))) + meta("HRMS", f"T{tc}{mi}{cat}"))
            if not (tc == "MP" and m.month == last_full.month):      # MP last month missing -> completeness alert
                hrs.append((U(), terms[tc], m, end, heads[tc] * 165, heads[tc] * 70) + meta("HRMS", f"H{tc}{mi}"))
    bulk(c, "employee_snapshot", ["snapshot_id", "entity_id", "period_end", "department", "gender", "nationality_group", "age_band", "grade_band",
                                  "contract_type", "headcount", "leavers", "voluntary_leavers"] + MC, emp)
    bulk(c, "training_record", ["training_id", "entity_id", "period_start", "period_end", "course_category", "training_hours", "participants"] + MC, trn)
    bulk(c, "hours_worked", ["hours_id", "entity_id", "period_start", "period_end", "employee_hours", "contractor_hours"] + MC, hrs)
    safety = []
    mix = ["near_miss"] * 20 + ["first_aid"] * 8 + ["recordable"] * 6 + ["lost_time"] * 3 + ["dangerous_occurrence"]
    for i, t in enumerate(mix):
        day = y0 + timedelta(days=rng.randint(0, max(ndays - 12, 1)))
        safety.append((U(), terms[rng.choice(tcodes)], t, rng.choice(["employee", "contractor"]), day_ts(day, rng.randint(6, 18)),
                       rng.randint(1, 20) if t == "lost_time" else 0, "Yard", "closed") + meta("HSE", i, validated=True))
    safety.append((U(), terms["KP"], "lost_time", "contractor", now - timedelta(days=9), 6, "Quay crane area", "investigating") + meta("HSE", "recent", validated=False))
    bulk(c, "safety_incident", ["incident_id", "entity_id", "incident_type", "person_type", "occurred_at", "lost_days", "location_desc", "status"] + MC, safety)
    bulk(c, "risk", ["risk_id", "risk_code", "entity_id", "category", "title", "likelihood", "impact", "owner", "status"] + MC,
         [(U(), f"R-{i:02d}", root, cat, title, rng.randint(1, 5), rng.randint(2, 5), "Head of HSE", "mitigating") + meta("MANUAL", f"risk{i}")
          for i, (cat, title) in enumerate([("climate", "Sea-level rise and storm surge at quay walls"), ("climate", "Extreme heat impact on equipment and workforce"),
                                            ("E", "Marine pollution from bunkering"), ("E", "Air quality complaints from neighbouring communities"),
                                            ("S", "Contractor safety performance"), ("S", "Skills shortage for electrified equipment"),
                                            ("G", "Data quality in ESG reporting"), ("G", "Tenant non-disclosure"),
                                            ("operational", "Grid outage affecting shore power"), ("climate", "Transition risk: carbon pricing on shipping")], 1)])
    bulk(c, "policy", ["policy_id", "title", "version_label", "approval_date", "next_review_date", "owner_role"] + MC,
         [(U(), t, "v2", today - timedelta(days=rng.randint(200, 700)), today + timedelta(days=dd), "ESG Committee") + meta("MANUAL", f"pol{i}")
          for i, (t, dd) in enumerate([("Environmental policy", 20), ("Health and safety policy", 120), ("Climate and energy policy", 200),
                                       ("Anti-corruption policy", -15), ("Human rights policy", 300), ("Supplier code of conduct", 90),
                                       ("Biodiversity policy", 400), ("Data governance policy", 150)])])
    bulk(c, "audit_finding", ["finding_id", "entity_id", "audit_ref", "severity", "description", "due_date", "status"] + MC,
         [(U(), terms[rng.choice(tcodes)], f"AUD-26-{i:02d}", rng.choice(["low", "medium", "high"]), "Evidence gap in waste records",
           today + timedelta(days=rng.randint(-20, 90)), "open" if i < 5 else "closed") + meta("MANUAL", f"aud{i}") for i in range(8)])
    bulk(c, "certification", ["certification_id", "entity_id", "standard", "issue_date", "expiry_date", "result"] + MC,
         [(U(), terms[tc], std, today - timedelta(days=500), today + timedelta(days=dd), "certified") + meta("MANUAL", f"cert{i}")
          for i, (tc, std, dd) in enumerate([("KP", "ISO 14001", 20), ("KP", "ISO 45001", 210), ("ZP", "ISO 14001", 330), ("ZP", "ISO 45001", -12),
                                             ("FJ", "ISO 14001", 150), ("FJ", "ISO 50001", 400), ("MP", "ISO 14001", 260), ("KP", "ISO 50001", 500)])])
    wtypes = ["general", "metals", "paper", "plastics", "oily waste", "food", "construction debris"]
    methods = ["recycle"] * 8 + ["recover"] * 3 + ["treatment"] * 4 + ["landfill"] * 4 + ["incineration"]
    bulk(c, "waste_manifest", ["manifest_id", "entity_id", "manifest_ref", "waste_type", "hazardous", "quantity_kg", "disposal_method", "contractor", "manifest_date"] + MC,
         [(U(), terms[rng.choice(tcodes)], f"WM-{i:04d}", (w := rng.choice(wtypes)), w == "oily waste", round(rng.uniform(200, 6000), 0), rng.choice(methods),
           "Waste Contractor (sample)", y0 + timedelta(days=rng.randint(0, max(ndays - 1, 0)))) + meta("MANUAL", f"wm{i}", dq="manual") for i in range(140)])
    wsrc = [("desalinated", 0.55), ("municipal", 0.30), ("treated_effluent", 0.15)]
    months = sorted({(y0 + timedelta(days=d)).replace(day=1) for d in range(0, max(ndays, 1), 28)})
    wrows, wi = [], 0
    for tc in tcodes:
        for m0 in months:
            base = rng.uniform(9000, 26000)
            for src, share in wsrc:
                wi += 1
                wrows.append((U(), terms[tc], m0, (m0 + timedelta(days=32)).replace(day=1) - timedelta(days=1), "withdrawal", src, round(base * share, 0), False) + meta("MANUAL", f"wr{wi}", dq="measured"))
            wi += 1
            wrows.append((U(), terms[tc], m0, (m0 + timedelta(days=32)).replace(day=1) - timedelta(days=1), "discharge", "seawater", round(base * 0.35, 0), False) + meta("MANUAL", f"wr{wi}", dq="measured"))
            wi += 1
            wrows.append((U(), terms[tc], m0, (m0 + timedelta(days=32)).replace(day=1) - timedelta(days=1), "recycled", "treated_effluent", round(base * 0.12, 0), False) + meta("MANUAL", f"wr{wi}", dq="measured"))
    bulk(c, "water_record", ["water_record_id", "entity_id", "period_start", "period_end", "flow_type", "source_type", "volume_m3", "water_stress_area"] + MC, wrows)
    bulk(c, "community_grievance", ["grievance_id", "entity_id", "received_date", "channel", "category", "status", "resolution_days"] + MC,
         [(U(), terms[rng.choice(tcodes)], y0 + timedelta(days=rng.randint(0, max(ndays - 1, 0))), rng.choice(["phone", "web", "letter"]),
           rng.choice(["noise", "traffic", "odour", "dust"]), "closed", rng.randint(3, 40)) + meta("MANUAL", f"gr{i}") for i in range(14)])

    # ---- tenant submissions
    mid = {r["metric_code"]: r["metric_id"] for r in db.rows(c, "SELECT metric_code, metric_id FROM metric")}
    subs = []
    quarters = [(date(today.year, 1, 1), date(today.year, 3, 31)), (date(today.year, 4, 1), date(today.year, 6, 30)), (date(today.year, 7, 1), date(today.year, 9, 30))]
    for ti, (tid, _, tc) in enumerate(tenants):
        for code, lo, hi in [("TENANT_WASTE_KG", 4000, 40000), ("TENANT_INCIDENTS", 0, 4), ("TENANT_TRAINING_HRS", 100, 900)]:
            for qi, (qs, qe) in enumerate(quarters):
                late = qi == 2 and ti in (1, 3, 4)
                stat = "submitted" if late else "approved"
                subs.append((U(), tid, mid[code], qs, qe, round(rng.uniform(lo, hi), 0), {"TENANT_WASTE_KG": "kg", "TENANT_INCIDENTS": "count", "TENANT_TRAINING_HRS": "hours"}[code],
                             stat, None if late else "data.owner@adports-demo.local", None if late else today - timedelta(days=20),
                             ss["TENANT_PORTAL"], f"TP-{ti}-{code}-{qi}", "manual", "pending" if late else "validated", "tenant1@adports-demo.local",
                             None if late else "data.owner@adports-demo.local",
                             now - timedelta(days=12 if late else 40)))
    bulk(c, "tenant_esg_submission", ["submission_id", "tenant_id", "metric_id", "period_start", "period_end", "value", "uom", "status", "reviewer",
                                      "review_date", "source_system_id", "source_record_id", "data_quality", "validation_status", "created_by",
                                      "approved_by", "ingested_at"], subs)

    # ---- targets from a first baseline run, then the official run
    start, end = y0, today - timedelta(days=1)
    first = calc.run_calculation(c, "seed.loader", start, end)
    vals = {r["metric_code"]: float(r["value"]) for r in db.rows(
        c, "SELECT m.metric_code, k.value FROM kpi_value k JOIN metric m ON m.metric_id = k.metric_id WHERE k.run_id = %s AND k.entity_id = %s",
        (first["run_id"], root))}
    days = (end - start).days + 1
    annual = lambda code: vals[code] * 365 / days
    targets = {"GHG_TOTAL": annual("GHG_TOTAL") * 0.95, "GHG_S1": annual("GHG_S1") * 1.15, "GHG_S2": annual("GHG_S2") * 0.85,
               "GHG_S3": annual("GHG_S3") * 1.2, "GHG_INT_TEU": vals["GHG_INT_TEU"] * 0.93, "LTIFR": vals.get("LTIFR", 1) * 0.8,
               "TRIR": vals.get("TRIR", 1) * 1.2, "FATALITIES": 0, "SHORE_POWER_SHARE": vals["SHORE_POWER_SHARE"] * 1.25,
               "RENEW_SHARE": vals["RENEW_SHARE"] * 1.05, "WASTE_DIVERSION": vals["WASTE_DIVERSION"] * 0.95,
               "TRAINING_HRS_PER_EMP": vals["TRAINING_HRS_PER_EMP"] * 1.02, "FEMALE_SHARE": vals["FEMALE_SHARE"] * 1.1,
               "NATIONAL_SHARE": vals["NATIONAL_SHARE"] * 1.3, "AQ_PM25_MEAN": vals["AQ_PM25_MEAN"] * 1.2, "SPILLS": 2,
               "OPEN_AUDIT_FINDINGS": 3, "CERT_VALID_SHARE": 100, "POLICY_REVIEW_OVERDUE": 0, "DATA_VALIDATED_SHARE": 98}
    for code, tv in targets.items():
        c.execute("INSERT INTO kpi_target (metric_id, entity_id, baseline_year, target_year, target_value, description) VALUES (%s,%s,%s,%s,%s,%s)",
                  (mid[code], root, today.year - 1, today.year, round(tv, 4), "Sample target (seeded)"))
    for t in ("kpi_value", "ghg_emission", "ghg_activity_data"):   # drop the baseline run, keep a clean history
        c.execute(f"DELETE FROM esg.{t} WHERE run_id = %s", (first["run_id"],))
    c.execute("DELETE FROM esg.calc_run WHERE run_id = %s", (first["run_id"],))
    final = calc.run_calculation(c, "seed.loader", start, end)
    db.audit(c, "seed.loader", "seed", detail=f"Synthetic demo data loaded; calc run {final['run_id']}")
    return final


def apply_migrations(c):
    """Bring a database built from an older schema up to date. Each migration runs once and is recorded."""
    c.execute("CREATE TABLE IF NOT EXISTS esg.schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    if db.row(c, "SELECT to_regclass('esg.evidence_capture') AS t")["t"]:      # built from a schema that already has 001
        c.execute("INSERT INTO esg.schema_migration (name) VALUES ('001_evidence_ocr.sql') ON CONFLICT DO NOTHING")
    done = {r["name"] for r in db.rows(c, "SELECT name FROM esg.schema_migration")}
    d = Path(__file__).resolve().parent.parent / "db" / "migrations"
    for f in sorted(d.glob("*.sql")):
        if f.name not in done:
            c.execute(f.read_text())
            c.execute("INSERT INTO esg.schema_migration (name) VALUES (%s)", (f.name,))


def ensure_initial_data():
    """Called at startup. Seeds reference data, then either demo data or just an admin user."""
    with db.conn() as c:
        apply_migrations(c)
        seed_reference(c)
        if db.row(c, "SELECT 1 FROM app_user LIMIT 1"):
            return "existing"
        if os.environ.get("SEED_DEMO_DATA", "false").lower() == "true":
            seed_demo(c)
            return "demo"
        email, pw = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
        if email and pw:
            create_user(c, email, "Administrator", "esg_admin", pw)
            return "admin-only"
        return "no-users"


if __name__ == "__main__":
    print(ensure_initial_data())
