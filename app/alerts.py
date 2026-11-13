"""Alerting: seven rule types evaluated on a schedule (and on demand), with de-duplication,
auto-resolve when a condition clears, an acknowledge / resolve workflow and webhook / email delivery.

Each rule type returns "findings" (dedupe_key, title, message, context). The engine:
  * raises one alert per new finding (a live alert for the same rule + key is never duplicated),
  * auto-resolves live alerts whose finding has disappeared,
  * notifies the rule's channels for newly raised alerts only.
"""
import asyncio
import json
import logging
import os
import smtplib
import urllib.request
from datetime import date, timedelta
from email.message import EmailMessage

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import db
from .auth import require

log = logging.getLogger("esg.alerts")
router = APIRouter(prefix="/api")
READERS = require("esg_admin", "data_owner", "auditor")
HANDLERS = require("esg_admin", "data_owner")

SENSOR_COLUMNS = {
    "air_quality_reading": {"pm25", "pm10", "no2", "so2", "co", "o3"},
    "water_quality_reading": {"ph", "turbidity_ntu", "dissolved_oxygen_mgl", "temperature_c", "hydrocarbon_ppm"},
    "noise_reading": {"leq_db", "lmax_db"},
}
OPS = {">", ">=", "<", "<="}
EXPIRY = {"certification": ("certification_id", "expiry_date", "standard"),
          "policy": ("policy_id", "next_review_date", "title")}
MONTHLY = {"hours_worked": "period_start", "training_record": "period_start", "employee_snapshot": "period_end"}


# ------------------------------------------------------------------ rule evaluators
def f_threshold(c, p):
    t, col, op = p["table"], p["column"], p["op"]
    hours = int(p.get("window_hours", 24))
    rows = db.rows(c, f"""
        SELECT s.sensor_id, s.sensor_code, s.station_id, max(r.{col}) AS peak, min(r.{col}) AS low, count(*) AS n
        FROM esg.{t} r JOIN esg.sensor s ON s.sensor_id = r.sensor_id
        WHERE r.ts >= now() - make_interval(hours => %s) AND r.{col} {op} %s
        GROUP BY 1, 2, 3""", (hours, p["value"]))
    out = []
    for r in rows:
        shown = r["peak"] if op in (">", ">=") else r["low"]
        out.append(dict(dedupe_key=f"{r['sensor_id']}:{date.today()}",
                        title=f"{col} {op} {p['value']} at {r['sensor_code']}",
                        message=f"{r['n']} reading(s) in the last {hours} h breached the limit; worst value {shown}.",
                        context={"sensor": r["sensor_code"], "station": r["station_id"], "table": t, "column": col, "value": float(shown)}))
    return out


def f_incident(c, p):
    days = int(p.get("lookback_days", 14))
    if p["source"] == "environmental_incident":
        rows = db.rows(c, """SELECT incident_id AS id, incident_type, severity, occurred_at, location_desc, status
                             FROM environmental_incident WHERE severity = ANY(%s) AND status <> 'closed'
                             AND occurred_at >= now() - make_interval(days => %s)""", (p.get("severities", ["high", "critical"]), days))
    else:
        rows = db.rows(c, """SELECT incident_id AS id, incident_type, severity, occurred_at, location_desc, status
                             FROM safety_incident WHERE incident_type = ANY(%s) AND status <> 'closed'
                             AND occurred_at >= now() - make_interval(days => %s)""", (p.get("types", ["fatality", "lost_time"]), days))
    return [dict(dedupe_key=str(r["id"]), title=f"{r['incident_type'].replace('_', ' ').title()} reported ({p['source'].split('_')[0]})",
                 message=f"{r['occurred_at']:%Y-%m-%d %H:%M} at {r['location_desc'] or 'unspecified location'}; status {r['status']}.",
                 context={"incident_id": str(r["id"]), "source": p["source"], "severity": r["severity"]}) for r in rows]


def f_freshness(c, p):
    types = p.get("meter_types", ["electricity", "shore_power"])
    # automatic meters (SCADA): expected every max_age_hours. Meters that are read by hand are judged on their own cadence.
    rows = db.rows(c, """
        SELECT m.meter_id, m.meter_code, max(r.ts) AS last_ts FROM meter m
        LEFT JOIN energy_meter_reading r ON r.meter_id = m.meter_id
        WHERE m.meter_type = ANY(%s) AND NOT EXISTS (SELECT 1 FROM meter_register_reading g WHERE g.meter_id = m.meter_id)
        GROUP BY 1, 2
        HAVING max(r.ts) IS NULL OR max(r.ts) < now() - make_interval(hours => %s)""",
        (types, int(p.get("max_age_hours", 48))))
    out = [dict(dedupe_key=str(r["meter_id"]), title=f"No data from meter {r['meter_code']}",
                message=f"Last reading: {r['last_ts'] or 'never'}. Expected at least every {p.get('max_age_hours', 48)} h.",
                context={"meter": r["meter_code"]}) for r in rows]
    days = int(p.get("manual_max_age_days", 8))
    for r in db.rows(c, """
        SELECT m.meter_id, m.meter_code, max(g.read_at) AS last_ts FROM meter m
        JOIN meter_register_reading g ON g.meter_id = m.meter_id AND g.validation_status <> 'rejected'
        WHERE m.meter_type = ANY(%s) GROUP BY 1, 2
        HAVING max(g.read_at) < now() - make_interval(days => %s)""", (types, days)):
        out.append(dict(dedupe_key=str(r["meter_id"]), title=f"Manual reading overdue: {r['meter_code']}",
                        message=f"Last register reading {r['last_ts']:%Y-%m-%d}. Expected at least every {days} days.",
                        context={"meter": r["meter_code"]}))
    return out


def f_evidence(c, p):
    """Field photos carrying a critical integrity flag whose reading has not yet been decided."""
    days = int(p.get("lookback_days", 14))
    out = []
    for r in db.rows(c, """
        SELECT ec.capture_id, ec.captured_by, ec.created_at, ec.flags, ec.target_table, m.meter_code
        FROM evidence_capture ec
        LEFT JOIN meter m ON m.meter_id = ec.meter_id
        LEFT JOIN meter_register_reading g ON ec.target_table = 'meter_register_reading' AND g.register_reading_id::text = ec.target_record_id
        WHERE ec.status <> 'discarded' AND ec.created_at >= now() - make_interval(days => %s)
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(ec.flags) f WHERE f->>'severity' = 'critical')
          AND (ec.target_table IS DISTINCT FROM 'meter_register_reading' OR g.validation_status = 'pending')""", (days,)):
        crit = [f for f in r["flags"] if f["severity"] == "critical"]
        out.append(dict(dedupe_key=str(r["capture_id"]),
                        title=f"Suspect field evidence{' for ' + r['meter_code'] if r['meter_code'] else ''}: {crit[0]['code'].replace('_', ' ')}",
                        message=f"{r['captured_by']} on {r['created_at']:%Y-%m-%d %H:%M}. " + " ".join(f["message"] for f in crit),
                        context={"capture_id": str(r["capture_id"]), "flags": [f["code"] for f in crit]}))
    return out


def f_expiry(c, p):
    pk, col, label = EXPIRY[p["table"]]
    rows = db.rows(c, f"SELECT {pk} AS id, {col} AS due, {label} AS label FROM esg.{p['table']} "
                       f"WHERE {col} <= current_date + %s ORDER BY {col}", (int(p.get("days_ahead", 30)),))
    out = []
    for r in rows:
        left = (r["due"] - date.today()).days
        out.append(dict(dedupe_key=str(r["id"]),
                        title=f"{p['table'].title()} {'expired' if left < 0 else 'due soon'}: {r['label']}",
                        message=f"{col.replace('_', ' ')} {r['due']} ({'overdue by ' + str(-left) if left < 0 else 'in ' + str(left)} days).",
                        context={"table": p["table"], "id": str(r["id"]), "days_left": left}))
    return out


def f_backlog(c, p):
    days = int(p.get("older_than_days", 7))
    out = []
    if p.get("kind", "submissions") == "submissions":
        for r in db.rows(c, """SELECT s.submission_id, t.name, m.metric_code, s.ingested_at FROM tenant_esg_submission s
                               JOIN tenant t ON t.tenant_id = s.tenant_id JOIN metric m ON m.metric_id = s.metric_id
                               WHERE s.status = 'submitted' AND s.ingested_at < now() - make_interval(days => %s)""", (days,)):
            out.append(dict(dedupe_key=str(r["submission_id"]), title=f"Tenant submission awaiting review: {r['name']}",
                            message=f"{r['metric_code']} submitted {r['ingested_at']:%Y-%m-%d}; waiting more than {days} days.",
                            context={"submission_id": str(r["submission_id"])}))
    else:
        from .capture import validatable, INGESTIBLE
        for t in [x for x in validatable(c) if x in INGESTIBLE]:
            r = db.row(c, f"SELECT count(*) AS n, min(ingested_at) AS oldest FROM esg.{t} WHERE validation_status = 'pending' "
                          f"AND ingested_at < now() - make_interval(days => %s)", (days,))
            if r["n"]:
                out.append(dict(dedupe_key=t, title=f"{r['n']} {t} record(s) pending validation",
                                message=f"Oldest pending since {r['oldest']:%Y-%m-%d}.", context={"table": t, "pending": r["n"]}))
    return out


def f_kpi(c, p):
    run = db.row(c, "SELECT run_id, period_start, period_end FROM calc_run WHERE status='succeeded' ORDER BY finished_at DESC LIMIT 1")
    if not run:
        return []
    rows = db.rows(c, """SELECT m.metric_code, m.name, k.value, t.target_value, m.unit FROM kpi_value k
                         JOIN metric m ON m.metric_id = k.metric_id JOIN entity e ON e.entity_id = k.entity_id
                         LEFT JOIN kpi_target t ON t.target_id = k.target_id
                         WHERE k.run_id = %s AND k.status = 'off_track' AND e.entity_type = 'port'
                           AND (%s::text[] IS NULL OR m.pillar = ANY(%s::text[]))""",
                   (run["run_id"], p.get("pillars"), p.get("pillars")))
    return [dict(dedupe_key=r["metric_code"], title=f"KPI off track: {r['name']}",
                 message=f"Value {float(r['value']):.2f} {r['unit'] or ''} vs target {r['target_value']} "
                         f"(period {run['period_start']} to {run['period_end']}).",
                 context={"metric": r["metric_code"], "run_id": str(run["run_id"])}) for r in rows]


def f_missing_monthly(c, p):
    t = p.get("table", "hours_worked")
    col = MONTHLY[t]
    today = date.today()
    last_end = today.replace(day=1) - timedelta(days=1)
    first = last_end.replace(day=1)
    rows = db.rows(c, f"""SELECT e.entity_id, e.name FROM entity e WHERE e.entity_type = 'terminal'
                          AND NOT EXISTS (SELECT 1 FROM esg.{t} x WHERE x.entity_id = e.entity_id
                                          AND x.{col} BETWEEN %s AND %s)""", (first, last_end))
    return [dict(dedupe_key=f"{r['entity_id']}:{first:%Y-%m}", title=f"Missing {t.replace('_', ' ')} for {r['name']}",
                 message=f"No {t} record for {first:%B %Y}.", context={"entity": r["name"], "month": f"{first:%Y-%m}", "table": t})
            for r in rows]


from . import anomaly, ems

EVALUATORS = {"threshold_reading": f_threshold, "new_incident": f_incident, "data_freshness": f_freshness,
              "expiry": f_expiry, "workflow_backlog": f_backlog, "kpi_off_track": f_kpi, "missing_monthly": f_missing_monthly,
              "evidence_flagged": f_evidence, "data_anomaly": anomaly.find, "ems_overdue": ems.f_ems_overdue}


def validate_params(rule_type, p):
    bad = lambda m: HTTPException(400, f"Invalid params for {rule_type}: {m}")
    if rule_type == "threshold_reading":
        if p.get("table") not in SENSOR_COLUMNS or p.get("column") not in SENSOR_COLUMNS[p.get("table")]:
            raise bad("table/column must be one of " + "; ".join(f"{k}: {sorted(v)}" for k, v in SENSOR_COLUMNS.items()))
        if p.get("op") not in OPS:
            raise bad(f"op must be one of {sorted(OPS)}")
        if not isinstance(p.get("value"), (int, float)) or isinstance(p.get("value"), bool):
            raise bad("value must be a number")
    elif rule_type == "new_incident":
        if p.get("source") not in ("environmental_incident", "safety_incident"):
            raise bad("source must be environmental_incident or safety_incident")
    elif rule_type == "expiry":
        if p.get("table") not in EXPIRY:
            raise bad(f"table must be one of {sorted(EXPIRY)}")
    elif rule_type == "workflow_backlog":
        if p.get("kind", "submissions") not in ("submissions", "validation"):
            raise bad("kind must be submissions or validation")
    elif rule_type == "missing_monthly":
        if p.get("table", "hours_worked") not in MONTHLY:
            raise bad(f"table must be one of {sorted(MONTHLY)}")
    elif rule_type == "data_anomaly":
        anomaly.validate(p, bad)
    elif rule_type == "evidence_flagged":
        d = p.get("lookback_days", 14)
        if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 365:
            raise bad("lookback_days must be a whole number between 1 and 365")


# ------------------------------------------------------------------ delivery
def notify(rule, alert) -> str:
    parts = []
    text = f"[{alert['severity'].upper()}] {alert['title']}\n{alert['message'] or ''}"
    if "webhook" in rule["channels"]:
        url = os.environ.get("ALERT_WEBHOOK_URL")
        if not url:
            parts.append("webhook: not configured")
        else:
            try:
                req = urllib.request.Request(url, json.dumps({"text": text, "title": alert["title"]}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                parts.append("webhook: sent")
            except Exception as e:  # delivery problems must never break evaluation
                parts.append(f"webhook: failed ({type(e).__name__})")
    if "email" in rule["channels"]:
        host = os.environ.get("SMTP_HOST")
        if not host or not rule["recipients"]:
            parts.append("email: not configured")
        else:
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = f"[ESG alert] {alert['title']}", os.environ.get("SMTP_FROM", "esg@localhost"), ", ".join(rule["recipients"])
                msg.set_content(text)
                with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=10) as s:
                    if os.environ.get("SMTP_USER"):
                        s.starttls()
                        s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    s.send_message(msg)
                parts.append("email: sent")
            except Exception as e:
                parts.append(f"email: failed ({type(e).__name__})")
    return "; ".join(parts) or "in-app only"


def run_rule(rule):
    """Evaluate one rule. Returns {raised, resolved}. Notifications go out after the commit."""
    validate_params(rule["rule_type"], rule["params"])  # guards the SQL identifiers used by some evaluators
    with db.conn() as c:
        findings = EVALUATORS[rule["rule_type"]](c, rule["params"])
        keys = [f["dedupe_key"] for f in findings]
        raised = []
        for f in findings:
            a = db.row(c, """
                INSERT INTO alert (rule_id, dedupe_key, severity, title, message, context)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (rule_id, dedupe_key) WHERE status <> 'resolved' DO NOTHING RETURNING *""",
                (rule["rule_id"], f["dedupe_key"], rule["severity"], f["title"], f["message"], Jsonb(db.jsonable(f["context"]))))
            if a:
                raised.append(a)
        resolved = db.rows(c, """
            UPDATE alert SET status = 'resolved', resolved_at = now(), resolved_by = 'system (condition cleared)'
            WHERE rule_id = %s AND status <> 'resolved' AND dedupe_key <> ALL(%s) RETURNING alert_id""",
            (rule["rule_id"], keys))
    for a in raised:  # outside the transaction: slow webhooks must not hold locks
        status = notify(rule, a)
        with db.conn() as c:
            c.execute("UPDATE alert SET notify_status = %s WHERE alert_id = %s", (status, a["alert_id"]))
    return {"rule": rule["name"], "found": len(findings), "raised": len(raised), "resolved": len(resolved)}


def evaluate_all():
    with db.conn() as c:
        rules = db.rows(c, "SELECT * FROM alert_rule WHERE enabled ORDER BY name")
    out = []
    for r in rules:
        try:
            out.append(run_rule(r))
        except Exception as e:
            log.exception("rule %s failed", r["name"])
            out.append({"rule": r["name"], "error": f"{type(e).__name__}: {e}"})
    return out


async def scheduler():
    interval = int(os.environ.get("ALERT_INTERVAL_SEC", 300))
    if interval <= 0:
        return
    await asyncio.sleep(5)
    while True:
        try:
            await run_in_threadpool(evaluate_all)
        except Exception:
            log.exception("alert evaluation cycle failed")
        await asyncio.sleep(interval)


# ------------------------------------------------------------------ API
@router.get("/alerts")
def list_alerts(status: str | None = None, severity: str | None = None, limit: int = 200, user: dict = Depends(READERS)):
    with db.conn() as c:
        return db.rows(c, """
            SELECT a.*, r.name AS rule_name, r.rule_type FROM alert a JOIN alert_rule r ON r.rule_id = a.rule_id
            WHERE (%s::text IS NULL OR a.status = %s) AND (%s::text IS NULL OR a.severity = %s)
            ORDER BY (a.status = 'resolved'), CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     a.raised_at DESC LIMIT %s""", (status, status, severity, severity, min(limit, 500)))


@router.get("/alerts/summary")
def alert_summary(user: dict = Depends(READERS)):
    with db.conn() as c:
        return db.rows(c, "SELECT severity, status, count(*) AS n FROM alert GROUP BY 1, 2 ORDER BY 1, 2")


def _transition(alert_id, new, user):
    with db.conn() as c:
        a = db.row(c, "SELECT * FROM alert WHERE alert_id = %s FOR UPDATE", (alert_id,))
        if not a:
            raise HTTPException(404, "Alert not found")
        if a["status"] == "resolved":
            raise HTTPException(409, "Alert already resolved")
        if new == "acknowledged":
            r = db.row(c, "UPDATE alert SET status='acknowledged', acknowledged_by=%s, acknowledged_at=now() WHERE alert_id=%s RETURNING *",
                       (user["email"], alert_id))
        else:
            r = db.row(c, "UPDATE alert SET status='resolved', resolved_by=%s, resolved_at=now() WHERE alert_id=%s RETURNING *",
                       (user["email"], alert_id))
        db.audit(c, user["email"], f"alert_{new}", "alert", alert_id, before={"status": a["status"]}, after={"status": new})
        return r


@router.post("/alerts/{alert_id}/ack")
def ack(alert_id: str, user: dict = Depends(HANDLERS)):
    return _transition(alert_id, "acknowledged", user)


@router.post("/alerts/{alert_id}/resolve")
def resolve(alert_id: str, user: dict = Depends(HANDLERS)):
    return _transition(alert_id, "resolved", user)


@router.post("/alerts/evaluate")
def evaluate_now(user: dict = Depends(HANDLERS)):
    return evaluate_all()


@router.post("/alerts/test-notification")
def test_notification(user: dict = Depends(require("esg_admin"))):
    status = notify({"channels": ["webhook"], "recipients": []},
                    {"severity": "info", "title": "ESG system test notification", "message": f"Sent by {user['email']}"})
    return {"status": status}


class RuleBody(BaseModel):
    name: str
    rule_type: str
    severity: str = "warning"
    params: dict = {}
    channels: list[str] = []
    recipients: list[str] = []
    enabled: bool = True


class RulePatch(BaseModel):
    name: str | None = None
    severity: str | None = None
    params: dict | None = None
    channels: list[str] | None = None
    recipients: list[str] | None = None
    enabled: bool | None = None


def _check_common(severity, channels):
    if severity is not None and severity not in ("info", "warning", "critical"):
        raise HTTPException(400, "severity must be info, warning or critical")
    if channels is not None and not set(channels) <= {"webhook", "email"}:
        raise HTTPException(400, "channels must be a subset of webhook, email")


@router.get("/alert-rules")
def list_rules(user: dict = Depends(READERS)):
    with db.conn() as c:
        return db.rows(c, """SELECT r.*, (SELECT count(*) FROM alert a WHERE a.rule_id = r.rule_id AND a.status <> 'resolved') AS live_alerts
                             FROM alert_rule r ORDER BY r.name""")


@router.post("/alert-rules")
def create_rule(body: RuleBody, user: dict = Depends(require("esg_admin"))):
    if body.rule_type not in EVALUATORS:
        raise HTTPException(400, f"rule_type must be one of {sorted(EVALUATORS)}")
    _check_common(body.severity, body.channels)
    validate_params(body.rule_type, body.params)
    try:
        with db.conn() as c:
            r = db.row(c, """INSERT INTO alert_rule (name, rule_type, severity, params, channels, recipients, enabled)
                             VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                       (body.name, body.rule_type, body.severity, Jsonb(body.params), body.channels, body.recipients, body.enabled))
            db.audit(c, user["email"], "insert", "alert_rule", r["rule_id"], after=r)
            return r
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "A rule with that name already exists")


@router.patch("/alert-rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, user: dict = Depends(require("esg_admin"))):
    _check_common(body.severity, body.channels)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "Nothing to change")
    with db.conn() as c:
        before = db.row(c, "SELECT * FROM alert_rule WHERE rule_id = %s FOR UPDATE", (rule_id,))
        if not before:
            raise HTTPException(404, "Rule not found")
        if "params" in changes:
            validate_params(before["rule_type"], changes["params"])
            changes["params"] = Jsonb(changes["params"])
        sets = ", ".join(f"{k} = %s" for k in changes)
        after = db.row(c, f"UPDATE alert_rule SET {sets} WHERE rule_id = %s RETURNING *", list(changes.values()) + [rule_id])
        db.audit(c, user["email"], "update", "alert_rule", rule_id, before=before, after=after)
        return after
