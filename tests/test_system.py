import http.server, json, threading, uuid
from datetime import date, datetime, timezone

import pytest
from app import alerts, db

TODAY = date.today()
UNIQ = lambda: uuid.uuid4().hex[:10]


def first_id(client, H, table, **flt):
    q = "&".join(f"{k}={v}" for k, v in flt.items())
    return client.get(f"/api/records/{table}?limit=1&{q}", headers=H["admin"]).json()["rows"][0]


# ------------------------------------------------------------------ auth & roles
def test_login_and_me(client, H):
    assert client.post("/api/auth/login", json={"email": "esg.admin@adports-demo.local", "password": "wrong"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    me = client.get("/api/auth/me", headers=H["tenant"]).json()
    assert me["role"] == "tenant" and me["tenant_id"]


def test_role_boundaries(client, H):
    rec = {"imo_number": "9" + UNIQ()[:6], "vessel_name": "X"}
    assert client.post("/api/ingest/vessel", headers=H["auditor"], json={"records": [rec]}).status_code == 403
    assert client.post("/api/ingest/vessel", headers=H["tenant"], json={"records": [rec]}).status_code == 403
    assert client.get("/api/records/vessel_call", headers=H["tenant"]).status_code == 403
    assert client.post("/api/ingest/entity", headers=H["owner"], json={"records": [{"entity_type": "facility", "code": UNIQ(), "name": "n"}]}).status_code == 403
    assert client.get("/api/admin/users", headers=H["owner"]).status_code == 403
    assert client.post("/api/calc/run", headers=H["auditor"], json={"period_start": "2026-01-01", "period_end": "2026-02-01"}).status_code == 403


# ------------------------------------------------------------------ capture
def test_ingest_valid_duplicate_invalid_and_fk_error(client, H):
    imo = "9" + UNIQ()[:6].replace("a", "1").replace("b", "2").replace("c", "3").replace("d", "4").replace("e", "5").replace("f", "6")
    ok = {"imo_number": imo, "vessel_name": "TEST", "cii_rating": "B"}
    r = client.post("/api/ingest/vessel", headers=H["owner"], json={"records": [ok]}).json()
    assert r["inserted"] == 1 and not r["errors"]
    r = client.post("/api/ingest/vessel", headers=H["owner"], json={"records": [ok]}).json()
    assert r["inserted"] == 0 and r["duplicates"] == 1
    bad_enum = {"imo_number": "1", "vessel_name": "x", "cii_rating": "Z"}
    missing = {"imo_number": "2"}
    extra = {"imo_number": "3", "vessel_name": "x", "bogus": 1}
    r = client.post("/api/ingest/vessel", headers=H["owner"], json={"records": [bad_enum, missing, extra]}).json()
    assert r["inserted"] == 0 and len(r["errors"]) == 3
    # a bad foreign key fails alone; the good record in the same batch is saved
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    good = {"entity_id": entity, "fuel_type": "diesel", "litres": 10, "ts": "2026-01-02T10:00:00Z", "source_record_id": "t-" + UNIQ()}
    badfk = {"entity_id": str(uuid.uuid4()), "fuel_type": "diesel", "litres": 5, "ts": "2026-01-02T11:00:00Z"}
    r = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"source_system_code": "ERP", "records": [badfk, good]}).json()
    assert r["inserted"] == 1 and len(r["errors"]) == 1 and r["errors"][0]["index"] == 0
    assert client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"source_system_code": "NOPE", "records": [good]}).status_code == 400


def test_client_cannot_self_validate(client, H):
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    r = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": entity, "fuel_type": "diesel", "litres": 1, "ts": "2026-01-03T10:00:00Z", "validation_status": "validated", "approved_by": "me"}]}).json()
    row = client.get(f"/api/records/fuel_transaction?limit=1&txn_id={r['ids'][0]}", headers=H["admin"]).json()["rows"][0]
    assert row["validation_status"] == "pending" and row["approved_by"] is None and row["created_by"].startswith("data.owner")


def test_csv_upload(client, H):
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    csv = f"entity_id,fuel_type,litres,ts\n{entity},diesel,12.5,2026-01-04T08:00:00Z\n{entity},diesel,notanumber,2026-01-04T09:00:00Z\n{entity},petrol,7,2026-01-04T10:00:00Z\n"
    r = client.post("/api/ingest/fuel_transaction/csv", headers=H["owner"], files={"file": ("f.csv", csv, "text/csv")}).json()
    assert r["inserted"] == 2 and len(r["errors"]) == 1 and "row 3" in r["errors"][0]["error"]
    r = client.post("/api/ingest/fuel_transaction/csv", headers=H["owner"], files={"file": ("f.csv", "bogus_col\n1\n", "text/csv")})
    assert r.status_code == 400


def test_four_eyes_validation_flow(client, H):
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    ids = client.post("/api/ingest/hours_worked", headers=H["admin"], json={"records": [
        {"entity_id": entity, "period_start": "2020-01-01", "period_end": "2020-01-31", "employee_hours": 1000, "version": 1 + int(UNIQ()[:5], 16) % 90000}]}).json()["ids"]
    rid = ids[0]
    d = lambda who, **b: client.post(f"/api/validation/hours_worked/{rid}", headers=H[who], json=b)
    assert d("admin", action="validate").status_code == 403           # cannot approve own entry
    assert d("owner", action="reject").status_code == 400             # reject needs a comment
    ok = d("owner", action="validate")
    assert ok.status_code == 200 and ok.json()["validation_status"] == "validated" and ok.json()["approved_by"].startswith("data.owner")
    assert d("owner", action="validate").status_code == 409           # already decided
    # correction sends it back to pending and leaves before/after in the audit log
    r = client.patch(f"/api/records/hours_worked/{rid}", headers=H["admin"], json={"employee_hours": 1200})
    assert r.status_code == 200 and r.json()["validation_status"] == "pending"
    log = client.get("/api/audit-log?table=hours_worked&action=update", headers=H["auditor"]).json()
    mine = [x for x in log if x["record_id"] == rid][0]
    assert float(mine["before"]["employee_hours"]) == 1000 and float(mine["after"]["employee_hours"]) == 1200


def test_audit_log_is_immutable(client, H):
    with pytest.raises(Exception, match="append-only"):
        with db.conn() as c:
            c.execute("UPDATE audit_log SET actor = 'x' WHERE audit_id = (SELECT min(audit_id) FROM audit_log)")
    with pytest.raises(Exception, match="append-only"):
        with db.conn() as c:
            c.execute("DELETE FROM audit_log")


def test_tenant_can_only_touch_own_submissions(client, H):
    metric = next(m["id"] for m in client.get("/api/lookup/metric", headers=H["tenant"]).json() if m["label"].startswith("TENANT_WASTE"))
    assert client.get("/api/lookup/entity", headers=H["tenant"]).status_code == 403
    me = client.get("/api/auth/me", headers=H["tenant"]).json()
    other = client.get("/api/lookup/tenant", headers=H["admin"]).json()
    other_id = next(t["id"] for t in other if t["id"] != str(me["tenant_id"]))
    rec = {"tenant_id": other_id, "metric_id": metric, "period_start": "2019-01-01", "period_end": "2019-03-31", "value": 5, "status": "approved"}
    r = client.post("/api/ingest/tenant_esg_submission", headers=H["tenant"], json={"records": [rec]}).json()
    assert r["inserted"] == 0 and "approval is a separate step" in r["errors"][0]["error"]
    rec["status"] = "submitted"
    r = client.post("/api/ingest/tenant_esg_submission", headers=H["tenant"], json={"records": [rec]}).json()
    assert r["inserted"] == 1                                         # tenant_id was forced to the caller's tenant
    row = client.get(f"/api/records/tenant_esg_submission?limit=1&submission_id={r['ids'][0]}", headers=H["tenant"]).json()["rows"][0]
    assert str(row["tenant_id"]) == str(me["tenant_id"])
    assert client.get("/api/records/tenant_esg_submission?limit=500", headers=H["tenant"]).json()["rows"] and \
        all(str(x["tenant_id"]) == str(me["tenant_id"]) for x in client.get("/api/records/tenant_esg_submission?limit=500", headers=H["tenant"]).json()["rows"])
    # the owner approves it; the submission becomes 'approved'
    a = client.post(f"/api/validation/tenant_esg_submission/{r['ids'][0]}", headers=H["owner"], json={"action": "validate"}).json()
    assert a["status"] == "approved" and a["reviewer"].startswith("data.owner")


# ------------------------------------------------------------------ calculation & reports
def test_calc_matches_independent_arithmetic(client, H):
    period = {"period_start": "2026-01-01", "period_end": "2026-06-30"}
    r1 = client.post("/api/calc/run", headers=H["owner"], json=period).json()
    s1 = {x["scope"]: x["tco2e"] for x in r1["totals_by_scope"]}
    with db.conn() as c:
        litres = float(db.row(c, "SELECT sum(litres) l FROM fuel_transaction WHERE ts >= '2026-01-01' AND ts < '2026-07-01' AND fuel_type='diesel' AND validation_status <> 'rejected'")["l"])
        refr = db.row(c, "SELECT coalesce(sum(kg_added * CASE refrigerant_type WHEN 'R410A' THEN 2088 WHEN 'R134a' THEN 1430 END), 0) t FROM refrigerant_event WHERE event_date BETWEEN '2026-01-01' AND '2026-06-30'")["t"]
    expected_s1 = litres * 2.68 / 1000 + float(refr) / 1000
    assert abs(s1[1] - expected_s1) < 1.0, (s1[1], expected_s1)       # rounding to 2 dp per scope only
    # add 1000 L of diesel inside the period: the next run's Scope 1 rises by exactly 2.68 t
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [{"entity_id": entity, "fuel_type": "diesel", "litres": 1000, "ts": "2026-03-15T10:00:00Z"}]})
    r2 = client.post("/api/calc/run", headers=H["owner"], json=period).json()
    s2 = {x["scope"]: x["tco2e"] for x in r2["totals_by_scope"]}
    assert abs((s2[1] - s1[1]) - 2.68) < 0.02
    assert abs(s2[2] - s1[2]) < 0.01 and abs(s2[3] - s1[3]) < 0.01     # other scopes untouched
    # earlier run is preserved and still reproduces its own total
    old = client.get(f"/api/reports/summary?run_id={r1['run_id']}", headers=H["admin"]).json()
    assert abs(next(x["tco2e"] for x in old["by_scope"] if x["key"] == 1) - s1[1]) < 0.01


def test_rejected_records_are_excluded(client, H):
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    period = {"period_start": "2026-02-01", "period_end": "2026-02-28"}
    base = {x["scope"]: x["tco2e"] for x in client.post("/api/calc/run", headers=H["owner"], json=period).json()["totals_by_scope"]}[1]
    rid = client.post("/api/ingest/fuel_transaction", headers=H["admin"], json={"records": [{"entity_id": entity, "fuel_type": "diesel", "litres": 5000, "ts": "2026-02-10T10:00:00Z"}]}).json()["ids"][0]
    with_it = {x["scope"]: x["tco2e"] for x in client.post("/api/calc/run", headers=H["owner"], json=period).json()["totals_by_scope"]}[1]
    client.post(f"/api/validation/fuel_transaction/{rid}", headers=H["owner"], json={"action": "reject", "comment": "duplicate fill"})
    after = {x["scope"]: x["tco2e"] for x in client.post("/api/calc/run", headers=H["owner"], json=period).json()["totals_by_scope"]}[1]
    assert abs(with_it - base - 13.4) < 0.02 and abs(after - base) < 0.01


def test_kpi_formulas(client, H):
    run = client.post("/api/calc/run", headers=H["owner"], json={"period_start": "2026-01-01", "period_end": "2026-10-31"}).json()
    k = {r["metric_code"]: float(r["value"]) for r in client.get(f"/api/reports/kpis?run_id={run['run_id']}", headers=H["admin"]).json()["rows"] if r["entity_type"] == "port"}
    with db.conn() as c:
        lt = db.row(c, "SELECT count(*) n FROM safety_incident WHERE incident_type IN ('lost_time','fatality') AND occurred_at >= '2026-01-01' AND occurred_at < '2026-11-01'")["n"]
        hrs = float(db.row(c, "SELECT sum(employee_hours + coalesce(contractor_hours,0)) h FROM hours_worked WHERE period_start >= '2026-01-01' AND period_end <= '2026-10-31'")["h"])
    assert abs(k["LTIFR"] - lt * 1e6 / hrs) < 0.001
    assert abs(k["GHG_TOTAL"] - (k["GHG_S1"] + k["GHG_S2"] + k["GHG_S3"])) < 0.5
    assert 0 <= k["SHORE_POWER_SHARE"] <= 100 and k["FATALITIES"] == 0


def test_reports_and_exports(client, H):
    csv = client.get("/api/reports/ghg.csv", headers=H["auditor"])
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv") and "co2e_tonnes" in csv.text.splitlines()[0]
    assert "ESG performance report" in client.get("/api/reports/annual.html", headers=H["admin"]).text
    cov = {(d["framework"], d["disclosure_code"]): d["coverage"] for d in client.get("/api/reports/disclosures", headers=H["admin"]).json()["rows"]}
    assert cov[("GRI", "305-1")] == "data" and cov[("GRI", "201-2")] == "not_mapped"
    assert client.get("/api/reports/summary", headers=H["tenant"]).status_code == 403


# ------------------------------------------------------------------ alerting
def titles(client, H, **q):
    s = "&".join(f"{k}={v}" for k, v in q.items())
    return [a["title"] for a in client.get(f"/api/alerts?{s}", headers=H["admin"]).json()]


def test_alert_lifecycle(client, H):
    with db.conn() as c:  # make the test repeatable: the seed's silent meter must be silent again
        c.execute("DELETE FROM energy_meter_reading WHERE ts > now() - interval '3 days' AND meter_id = (SELECT meter_id FROM meter WHERE meter_code = 'ZP-OPS')")
    res = client.post("/api/alerts/evaluate", headers=H["owner"]).json()
    assert not [r for r in res if "error" in r], res
    live = [a for a in client.get("/api/alerts?limit=500", headers=H["admin"]).json() if a["status"] != "resolved"]
    live_titles = " | ".join(a["title"] for a in live)
    assert "pm25 > 35 at AQ-FJ-1" in live_titles                       # sensor spike in seed data
    assert "No data from meter ZP-OPS" in live_titles                  # silent meter
    assert "Certification due soon" in live_titles and "Certification expired" in live_titles
    assert "Lost Time reported (safety)" in live_titles and "Spill reported (environmental)" in live_titles
    # re-running creates no duplicates
    again = client.post("/api/alerts/evaluate", headers=H["owner"]).json()
    assert sum(r.get("raised", 0) for r in again) == 0
    # acknowledge, then clear the condition: the alert resolves itself
    meter_alert = next(a for a in live if a["title"] == "No data from meter ZP-OPS")
    assert client.post(f"/api/alerts/{meter_alert['alert_id']}/ack", headers=H["auditor"]).status_code == 403
    assert client.post(f"/api/alerts/{meter_alert['alert_id']}/ack", headers=H["owner"]).json()["status"] == "acknowledged"
    with db.conn() as c:
        db.row(c, "INSERT INTO energy_meter_reading (meter_id, ts, energy_kwh, energy_type) SELECT meter_id, now(), 1000, 'grid' FROM meter WHERE meter_code='ZP-OPS' RETURNING 1")
    client.post("/api/alerts/evaluate", headers=H["owner"])
    now = {a["alert_id"]: a for a in client.get("/api/alerts", headers=H["admin"]).json()}
    assert now[meter_alert["alert_id"]]["status"] == "resolved" and "condition cleared" in now[meter_alert["alert_id"]]["resolved_by"]


def test_webhook_delivery_and_failure_isolated(client, H, monkeypatch):
    got = []
    class Hdl(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Hdl)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", f"http://127.0.0.1:{srv.server_port}/hook")
    entity = first_id(client, H, "entity", entity_type="terminal")["entity_id"]
    client.post("/api/ingest/safety_incident", headers=H["owner"], json={"records": [
        {"entity_id": entity, "incident_type": "fatality", "person_type": "contractor", "occurred_at": datetime.now(timezone.utc).isoformat(), "location_desc": "Test quay"}]})
    client.post("/api/alerts/evaluate", headers=H["owner"])
    srv.shutdown()
    assert any("Fatality reported" in g["title"] for g in got), got
    sent = [a for a in client.get("/api/alerts", headers=H["admin"]).json() if a["title"].startswith("Fatality")][0]
    assert "webhook: sent" in sent["notify_status"]
    # unreachable webhook must not break evaluation
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1:9/none")
    assert "failed" in alerts.notify({"channels": ["webhook"], "recipients": []}, {"severity": "info", "title": "t", "message": ""})


def test_rule_management_validation(client, H):
    bad = {"name": "bad " + UNIQ(), "rule_type": "threshold_reading", "params": {"table": "meter", "column": "meter_code", "op": ">", "value": 1}}
    assert client.post("/api/alert-rules", headers=H["admin"], json=bad).status_code == 400
    inj = dict(bad, params={"table": "air_quality_reading", "column": "pm25; DROP TABLE esg.entity", "op": ">", "value": 1})
    assert client.post("/api/alert-rules", headers=H["admin"], json=inj).status_code == 400
    good = {"name": "o3 high " + UNIQ(), "rule_type": "threshold_reading", "severity": "info", "params": {"table": "air_quality_reading", "column": "o3", "op": ">", "value": 1}}
    assert client.post("/api/alert-rules", headers=H["owner"], json=good).status_code == 403
    r = client.post("/api/alert-rules", headers=H["admin"], json=good)
    assert r.status_code == 200
    assert client.patch(f"/api/alert-rules/{r.json()['rule_id']}", headers=H["admin"], json={"enabled": False}).json()["enabled"] is False
