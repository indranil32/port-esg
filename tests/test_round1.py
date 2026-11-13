"""Collection links, period locks, source trace, audit context, unchanged-reading check."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from tests.test_evidence import NOW, UNIQ, new_meter, photo, upload

PERIOD = ("2020-03-01", "2020-03-31")          # factors are valid from 2020-01-01; keeps clear of current-year tests


@pytest.fixture(autouse=True)
def no_stale_locks():
    with db.conn() as c:
        c.execute("UPDATE reporting_period_lock SET unlocked_by = 'test', unlocked_at = now() WHERE unlocked_at IS NULL")
    yield
    with db.conn() as c:
        c.execute("UPDATE reporting_period_lock SET unlocked_by = 'test', unlocked_at = now() WHERE unlocked_at IS NULL")


def terminal(client, H):
    return client.get("/api/records/entity?limit=1&entity_type=terminal", headers=H["admin"]).json()["rows"][0]["entity_id"]


# ------------------------------------------------------------------ collection links
def link_setup(client, H, n=2):
    metrics = [m["id"] for m in client.get("/api/lookup/metric", headers=H["tenant"]).json()][:n]
    tenant = client.get("/api/records/tenant?limit=1", headers=H["admin"]).json()["rows"][0]["tenant_id"]
    r = client.post("/api/collect-links", headers=H["owner"], json={
        "tenant_id": tenant, "metric_ids": metrics, "period_start": "2026-10-01", "period_end": "2026-10-31", "message": "Q4 data"})
    assert r.status_code == 200, r.text
    return r.json(), metrics, tenant


def tok(t):
    return {"X-Collect-Token": t}


def test_collection_link_full_cycle(client, H):
    lk, metrics, tenant = link_setup(client, H)
    with db.conn() as c:                                   # only a hash is stored
        assert db.row(c, "SELECT 1 FROM collection_link WHERE token_hash = %s", (lk["token"],)) is None
    v = client.get("/api/collect", headers=tok(lk["token"]))
    assert v.status_code == 200 and v.json()["message"] == "Q4 data" and len(v.json()["metrics"]) == len(metrics)
    assert v.headers["cache-control"] == "no-store"
    # bad input does not consume the link: the recipient can fix and resend
    bad = client.post("/api/collect", headers=tok(lk["token"]), data={"entries": json.dumps([{"metric_id": metrics[0], "value": "abc"}])})
    assert bad.status_code == 422
    stray = client.post("/api/collect", headers=tok(lk["token"]), data={"entries": json.dumps([{"metric_id": "00000000-0000-0000-0000-000000000000", "value": 1}])})
    assert stray.status_code == 400
    ok = client.post("/api/collect", headers=tok(lk["token"]), data={"entries": json.dumps([
        {"metric_id": metrics[0], "value": 1234.5, "uom": "kg"}, {"metric_id": metrics[1], "value": ""}])},
        files={"file": ("bill.txt", b"invoice 42", "text/plain")})
    assert ok.status_code == 200 and ok.json()["saved"] == 1, ok.text
    with db.conn() as c:
        row = db.row(c, "SELECT created_by, validation_status, status, evidence_doc_id FROM tenant_esg_submission "
                        "WHERE created_by = %s ORDER BY ingested_at DESC LIMIT 1", ("link:" + lk["link_id"][:8],))
    assert row and row["validation_status"] == "pending" and row["status"] == "submitted" and row["evidence_doc_id"]
    assert client.post("/api/collect", headers=tok(lk["token"]), data={"entries": json.dumps([{"metric_id": metrics[0], "value": 1}])}).status_code == 404
    assert client.get("/api/collect", headers=tok(lk["token"])).status_code == 404            # single use
    listed = [x for x in client.get("/api/collect-links", headers=H["auditor"]).json() if x["link_id"] == lk["link_id"]][0]
    assert listed["status"] == "submitted" and listed["submission_count"] == 1
    # the next link for the same tenant pre-fills the value just given
    lk2, _, _ = link_setup(client, H)
    pre = {m["metric_id"]: m["prefill"] for m in client.get("/api/collect", headers=tok(lk2["token"])).json()["metrics"]}
    assert pre[metrics[0]] and pre[metrics[0]]["value"] == 1234.5


def test_collection_link_guards(client, H):
    assert client.get("/api/collect").status_code == 404
    assert client.get("/api/collect", headers=tok("not-a-token")).status_code == 404
    metrics = [m["id"] for m in client.get("/api/lookup/metric", headers=H["tenant"]).json()][:1]
    tenant = client.get("/api/records/tenant?limit=1", headers=H["admin"]).json()["rows"][0]["tenant_id"]
    body = {"tenant_id": tenant, "metric_ids": metrics, "period_start": "2026-10-01", "period_end": "2026-10-31"}
    assert client.post("/api/collect-links", headers=H["tenant"], json=body).status_code == 403
    assert client.post("/api/collect-links", headers=H["field"], json=body).status_code == 403
    everything = client.get("/api/lookup/metric", headers=H["admin"]).json()
    internal = [m["id"] for m in everything if m["id"] not in {x["id"] for x in client.get("/api/lookup/metric", headers=H["tenant"]).json()}][:1]
    assert client.post("/api/collect-links", headers=H["admin"], json=dict(body, metric_ids=internal)).status_code == 400   # not third-party metrics
    assert client.post("/api/collect-links", headers=H["admin"], json=dict(body, period_end="2026-09-01")).status_code == 400
    lk = client.post("/api/collect-links", headers=H["admin"], json=body).json()
    assert client.post(f"/api/collect-links/{lk['link_id']}/revoke", headers=H["admin"]).status_code == 200
    assert client.get("/api/collect", headers=tok(lk["token"])).status_code == 404
    lk = client.post("/api/collect-links", headers=H["admin"], json=body).json()
    with db.conn() as c:
        c.execute("UPDATE collection_link SET expires_at = now() - interval '1 minute' WHERE link_id::text = %s", (lk["link_id"],))
    assert client.get("/api/collect", headers=tok(lk["token"])).status_code == 404            # expired


# ------------------------------------------------------------------ period lock, trace
def march_2020(client, H):
    """Diesel at a terminal plus a two-reading hand-read meter with photos, all dated March 2020; returns (meter, derived row's meter id)."""
    ent = terminal(client, H)
    r = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": ent, "fuel_type": "diesel", "litres": 1000, "ts": "2020-03-10T08:00:00Z", "source_record_id": "t-" + UNIQ()}]})
    assert r.json()["inserted"] == 1, r.json()
    mid, code = new_meter(client, H, digits=6, multiplier=1)
    ids = []
    for day, val in (("2020-03-05", 100.0), ("2020-03-06", 100.0 + 250.0)):
        at = datetime.fromisoformat(day + "T08:00:00+00:00")
        cap = upload(client, H, "field", photo(f"{val:08.1f}", code, at), mid, at)
        assert cap.status_code == 200, cap.text
        s = client.post("/api/captures/submit-reading", headers=H["field"], json={
            "capture_id": cap.json()["capture_id"], "meter_id": mid, "register_value": val, "read_at": at.isoformat()})
        assert s.status_code == 200, s.text
        ids.append(s.json()["register_reading_id"])
    for rid in ids:
        assert client.post(f"/api/validation/meter_register_reading/{rid}", headers=H["admin"], json={"action": "validate"}).status_code == 200
    return mid, ids


def test_period_lock_freezes_reported_figures(client, H):
    march_2020(client, H)
    run = client.post("/api/calc/run", headers=H["admin"], json={"period_start": PERIOD[0], "period_end": PERIOD[1]})
    assert run.status_code == 200 and run.json()["activities"] >= 1, run.text
    rid = run.json()["run_id"]
    assert client.post("/api/periods/lock", headers=H["owner"], json={"run_id": rid, "label": "March 2020", "reason": "x"}).status_code == 403
    assert client.post("/api/periods/lock", headers=H["admin"], json={"run_id": rid, "label": " ", "reason": "x"}).status_code == 400
    lk = client.post("/api/periods/lock", headers=H["admin"], json={"run_id": rid, "label": "March 2020 (test)", "reason": "Reported to the regulator"})
    assert lk.status_code == 200, lk.text
    assert [r for r in client.get("/api/runs", headers=H["admin"]).json() if r["run_id"] == rid][0]["locked"] is True
    # new data inside the period is refused, whichever way it arrives
    ent = terminal(client, H)
    late = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": ent, "fuel_type": "diesel", "litres": 5, "ts": "2020-03-20T08:00:00Z", "source_record_id": "late-" + UNIQ()}]}).json()
    assert late["inserted"] == 0 and "locked" in late["errors"][0]["error"]
    with db.conn() as c, pytest.raises(Exception, match="locked"):
        c.execute("UPDATE fuel_transaction SET litres = litres + 1 WHERE ts >= '2020-03-01' AND ts < '2020-04-01'")
    # data outside the period is unaffected, and so is a recalculation of another period
    ok = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": ent, "fuel_type": "diesel", "litres": 5, "ts": "2020-04-20T08:00:00Z", "source_record_id": "ok-" + UNIQ()}]}).json()
    assert ok["inserted"] == 1
    again = client.post("/api/calc/run", headers=H["admin"], json={"period_start": "2020-03-15", "period_end": "2020-04-15"})
    assert again.status_code == 409 and "locked" in again.text
    # a factor behind the reported run cannot be edited in place
    act = client.get(f"/api/trace/runs/{rid}/activities", headers=H["admin"]).json()["rows"][0]
    fid = client.get(f"/api/trace/activity/{act['activity_id']}", headers=H["admin"]).json()["emissions"][0]["factor_id"]
    edit = client.patch(f"/api/records/emission_factor/{fid}", headers=H["admin"], json={"value": 9.99})
    assert edit.status_code == 422 and "locked" in edit.text
    factors = {f["factor_id"]: f for f in client.get("/api/periods/factors", headers=H["admin"]).json()}
    assert factors[fid]["in_locked_period"] is True and factors[fid]["valid_from"]
    # unlocking needs a reason, is audited, and re-opens the period
    lid = lk.json()["lock_id"]
    assert client.post(f"/api/periods/{lid}/unlock", headers=H["admin"], json={"reason": " "}).status_code == 400
    assert client.post(f"/api/periods/{lid}/unlock", headers=H["admin"], json={"reason": "Late invoice received"}).status_code == 200
    assert client.post(f"/api/periods/{lid}/unlock", headers=H["admin"], json={"reason": "again"}).status_code == 404
    reopened = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": ent, "fuel_type": "diesel", "litres": 5, "ts": "2020-03-20T08:00:00Z", "source_record_id": "re-" + UNIQ()}]}).json()
    assert reopened["inserted"] == 1
    log = [a for a in client.get("/api/audit-log?action=unlock_period", headers=H["auditor"]).json() if a["record_id"] == lid]
    assert log and log[0]["detail"] == "Late invoice received"


def test_trace_follows_a_number_down_to_the_photo(client, H):
    mid, ids = march_2020(client, H)
    run = client.post("/api/calc/run", headers=H["admin"], json={"period_start": PERIOD[0], "period_end": PERIOD[1]}).json()
    rows = client.get(f"/api/trace/runs/{run['run_id']}/activities?q=purchased", headers=H["auditor"]).json()["rows"]
    assert rows and all(r["tco2e"] is not None for r in rows)
    found = None
    for r in rows:
        d = client.get(f"/api/trace/activity/{r['activity_id']}", headers=H["auditor"]).json()
        found = next((s for s in d["sources"] if s["derived_from"] and str(s["derived_from"]["register_reading_id"]) == ids[1]), None)
        if found:
            break
    assert found, "the derived interval row should trace back to the hand-read register"
    assert float(found["row"]["energy_kwh"]) == pytest.approx(250.0)
    cap = found["captures"][0]
    assert cap["entered_value"] == 350.0 and cap["captured_by"] == "field.operator@adports-demo.local"
    img = client.get(f"/api/captures/{cap['capture_id']}/image", headers=H["auditor"])
    assert img.status_code == 200
    assert d["emissions"][0]["factor_value"] is not None and d["run"]["run_id"] == run["run_id"]
    assert client.get("/api/trace/activity/00000000-0000-0000-0000-000000000000", headers=H["auditor"]).status_code == 404
    assert client.get(f"/api/trace/runs/{run['run_id']}/activities", headers=H["field"]).status_code == 403


# ------------------------------------------------------------------ audit context, unchanged readings
def test_audit_rows_carry_request_id_and_ip(client, H):
    r = client.post("/api/ingest/fuel_transaction", headers={**H["owner"], "X-Forwarded-For": "203.0.113.7"}, json={"records": [
        {"entity_id": terminal(client, H), "fuel_type": "diesel", "litres": 1, "ts": datetime.now(timezone.utc).isoformat(), "source_record_id": "a-" + UNIQ()}]})
    rid = r.headers["x-request-id"]
    assert len(rid) == 16
    row = [a for a in client.get("/api/audit-log?table=fuel_transaction&limit=20", headers=H["auditor"]).json() if a["request_id"] == rid]
    assert row and row[0]["ip"] == "203.0.113.7"


def test_unchanged_reading_is_flagged(client, H):
    mid, code = new_meter(client, H)
    out = []
    for h in (4, 2):
        at = NOW() - timedelta(hours=h)
        cap = upload(client, H, "field", photo("000500.0" if h == 4 else "000500.0", code + ("" if h == 4 else ""), at + timedelta(seconds=h)), mid, at)
        s = client.post("/api/captures/submit-reading", headers=H["field"], json={
            "capture_id": cap.json()["capture_id"], "meter_id": mid, "register_value": 500.0, "read_at": at.isoformat()})
        out.append(s.json()["flags"])
    assert "unchanged_reading" in {f["code"] for f in out[1]} and "unchanged_reading" not in {f["code"] for f in out[0]}


# ------------------------------------------------------------------ ingest batches
def test_batch_withdraw(client, H):
    ent = terminal(client, H)
    recs = [{"entity_id": ent, "fuel_type": "diesel", "litres": 7, "ts": f"2019-05-0{d}T08:00:00Z", "source_record_id": f"b{d}-" + UNIQ()} for d in (1, 2, 3)]
    r = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": recs}).json()
    assert r["inserted"] == 3 and r["batch_id"] and "_all" not in r
    b = [x for x in client.get("/api/batches", headers=H["auditor"]).json() if x["batch_id"] == r["batch_id"]][0]
    assert b["inserted"] == 3 and b["active"]
    assert client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["auditor"], json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["owner"], json={"reason": " "}).status_code == 400
    w = client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["owner"], json={"reason": "Wrong file"})
    assert w.status_code == 200 and w.json()["withdrawn"] == 3
    assert client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["owner"], json={"reason": "again"}).status_code == 409
    rows = client.get("/api/records/fuel_transaction?limit=500", headers=H["admin"]).json()["rows"]
    mine = [x for x in rows if str(x["source_record_id"]).startswith(("b1-", "b2-", "b3-")) and str(x["ts"]).startswith("2019-05")]
    assert mine and all(x["validation_status"] == "rejected" for x in mine)
    # a locked period blocks the withdrawal
    recs = [{"entity_id": ent, "fuel_type": "diesel", "litres": 7, "ts": "2018-05-01T08:00:00Z", "source_record_id": "lk-" + UNIQ()}]
    r2 = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": recs}).json()
    run = client.post("/api/calc/run", headers=H["admin"], json={"period_start": "2018-05-01", "period_end": "2018-05-31"}).json()
    client.post("/api/periods/lock", headers=H["admin"], json={"run_id": run["run_id"], "label": "May 2018 (test)", "reason": "t"})
    blocked = client.post(f"/api/batches/{r2['batch_id']}/withdraw", headers=H["owner"], json={"reason": "late"})
    assert blocked.status_code == 409 and "locked" in blocked.text


# ------------------------------------------------------------------ consolidation approaches
def test_consolidation_approaches(client, H):
    ent = terminal(client, H)
    client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_id": ent, "fuel_type": "diesel", "litres": 1000, "ts": "2020-06-10T08:00:00Z", "source_record_id": "c-" + UNIQ()}]})
    run = client.post("/api/calc/run", headers=H["admin"], json={"period_start": "2020-06-01", "period_end": "2020-06-30"}).json()
    def tot(a):
        r = client.get(f"/api/reports/ghg?group=scope&run_id={run['run_id']}&approach={a}", headers=H["auditor"])
        assert r.status_code == 200, r.text
        return sum(x["tco2e"] for x in r.json()["rows"])
    full = tot("all")
    assert full > 0 and tot("equity") == pytest.approx(full)          # default share is 100%
    with db.conn() as c:
        c.execute("UPDATE entity SET equity_share_pct = 40, operational_control = false WHERE entity_id = %s", (ent,))
    try:
        assert tot("equity") == pytest.approx(full * 0.4, abs=0.02) and tot("operational") == pytest.approx(0, abs=0.01) and tot("all") == pytest.approx(full)
    finally:
        with db.conn() as c:
            c.execute("UPDATE entity SET equity_share_pct = 100, operational_control = true WHERE entity_id = %s", (ent,))
    assert client.get(f"/api/reports/ghg?approach=bogus&run_id={run['run_id']}", headers=H["auditor"]).status_code == 400
    with db.conn() as c, pytest.raises(Exception):
        c.execute("UPDATE entity SET equity_share_pct = 150 WHERE entity_id = %s", (ent,))


# ------------------------------------------------------------------ water
def test_water_kpis(client, H):
    ent = terminal(client, H)
    def rec(flow, src, v, n):
        return {"entity_id": ent, "period_start": "2020-08-01", "period_end": "2020-08-31", "flow_type": flow, "source_type": src, "volume_m3": v, "source_record_id": f"w{n}-" + UNIQ()}
    r = client.post("/api/ingest/water_record", headers=H["owner"], json={"records": [
        rec("withdrawal", "desalinated", 1000, 1), rec("withdrawal", "municipal", 500, 2), rec("discharge", "seawater", 400, 3), rec("recycled", "treated_effluent", 150, 4),
        {**rec("withdrawal", "municipal", -5, 5)}, {**rec("bogus", "municipal", 5, 6)}]}).json()
    assert r["inserted"] == 4 and len(r["errors"]) == 2
    run = client.post("/api/calc/run", headers=H["admin"], json={"period_start": "2020-08-01", "period_end": "2020-08-31"}).json()
    k = {x["metric_code"]: x["value"] for x in client.get(f"/api/reports/kpis?run_id={run['run_id']}", headers=H["auditor"]).json()["rows"] if x["entity_type"] == "port"}
    assert float(k["WATER_WITHDRAWAL"]) == 1500 and float(k["WATER_CONSUMPTION"]) == 1100 and float(k["WATER_RECYCLED_SHARE"]) == pytest.approx(10.0)
    d = client.get(f"/api/reports/disclosures?run_id={run['run_id']}", headers=H["auditor"]).json()["rows"]
    assert [x for x in d if x["disclosure_code"] == "303-3"][0]["coverage"] == "data"
    b = client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["owner"], json={"reason": "test"})
    assert b.status_code == 200 and b.json()["withdrawn"] == 4


# ------------------------------------------------------------------ database-level audit
def test_direct_database_changes_are_audited(client, H):
    import os, psycopg
    ent = terminal(client, H)
    tag = "da-" + UNIQ()
    r = client.post("/api/ingest/waste_manifest", headers=H["owner"], json={"records": [
        {"entity_id": ent, "waste_type": tag, "quantity_kg": 10, "manifest_date": "2020-09-01", "source_record_id": tag}]}).json()
    rid = r["ids"][0]
    def log():
        return client.get(f"/api/audit-log?table=waste_manifest&limit=500", headers=H["auditor"]).json()
    assert len([a for a in log() if a["record_id"] == rid]) == 1            # the app writes its own row; the trigger stays quiet
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, options="-c search_path=esg,public") as raw:
        raw.execute("UPDATE waste_manifest SET quantity_kg = 99 WHERE manifest_id::text = %s", (rid,))
        raw.execute("DELETE FROM waste_manifest WHERE manifest_id::text = %s", (rid,))
    rows = [a for a in log() if a["record_id"] == rid]
    direct = [a for a in rows if a["actor"].startswith("db:")]
    assert {a["action"] for a in direct} == {"update", "delete"} and len(rows) == 3
    upd = [a for a in direct if a["action"] == "update"][0]
    assert float(upd["before"]["quantity_kg"]) == 10 and float(upd["after"]["quantity_kg"]) == 99


# ------------------------------------------------------------------ anomaly detection
def test_anomaly_detector_flags_outliers_only(client, H):
    from app import anomaly
    ent = terminal(client, H)
    today = date.today()
    def rec(days_ago, v, n):
        d = today - timedelta(days=days_ago)
        return {"entity_id": ent, "period_start": d.isoformat(), "period_end": d.isoformat(), "flow_type": "recycled", "source_type": "groundwater",
                "volume_m3": v, "source_record_id": f"an{n}-" + UNIQ()}
    hist = [rec(20 + 7 * i, 100 + (i % 3) * 4, i) for i in range(9)]          # steady history, 100-108
    r = client.post("/api/ingest/water_record", headers=H["owner"], json={"records": hist + [rec(1, 104, 50), rec(2, 5000, 51)]}).json()
    assert r["inserted"] == 11, r
    params = {"detector": "water", "lookback_days": 5, "baseline_days": 120, "min_baseline": 5, "z_threshold": 6}
    with db.conn() as c:
        found = [f for f in anomaly.find(c, params) if f["context"]["value"] in (104, 5000) and f["context"]["record_id"] in set(r["ids"])]
    assert len(found) == 1 and found[0]["context"]["value"] == 5000 and found[0]["context"]["modified_z"] > 6
    assert "usual level" in found[0]["message"]
    client.post(f"/api/batches/{r['batch_id']}/withdraw", headers=H["owner"], json={"reason": "test cleanup"})
    with db.conn() as c:       # rejected records no longer count
        assert not [f for f in anomaly.find(c, params) if f["context"]["record_id"] in set(r["ids"])]
    bad = client.post("/api/alert-rules", headers=H["admin"], json={"name": "bad " + UNIQ(), "rule_type": "data_anomaly", "severity": "warning", "params": {"detector": "nope"}, "channels": [], "recipients": []})
    assert bad.status_code == 400
    ok = client.post("/api/alert-rules", headers=H["admin"], json={"name": "ok " + UNIQ(), "rule_type": "data_anomaly", "severity": "warning", "params": params, "channels": [], "recipients": []})
    assert ok.status_code == 200, ok.text


# ------------------------------------------------------------------ EMS (ISO 14001 / 50001)
def test_ems_objectives_actions_audits_reviews(client, H):
    from app import ems
    metric = [m for m in client.get("/api/lookup/metric", headers=H["admin"]).json() if m["label"].startswith("WATER_WITHDRAWAL")][0]["id"]
    body = {"standard": "ISO14001", "title": "Cut water withdrawal " + UNIQ(), "metric_id": metric, "baseline_value": 2000, "target_value": 1000, "owner": "HSE"}
    assert client.post("/api/ems/objectives", headers=H["auditor"], json=body).status_code == 403
    assert client.post("/api/ems/objectives", headers=H["admin"], json={**body, "standard": "ISO9001"}).status_code == 400
    o = client.post("/api/ems/objectives", headers=H["admin"], json=body).json()
    a = client.post(f"/api/ems/objectives/{o['objective_id']}/actions", headers=H["owner"], json={"title": "Install flow meters", "owner": "Maintenance", "due_date": "2020-01-31"})
    assert a.status_code == 200
    listed = [x for x in client.get("/api/ems/objectives?standard=ISO14001", headers=H["auditor"]).json() if x["objective_id"] == o["objective_id"]][0]
    assert listed["actions"] == 1 and listed["actions_overdue"] == 1
    assert ems.progress(2000, 1000, 1500) == 50.0 and ems.progress(10, 20, 15) == 50.0 and ems.progress(5, 5, 5) is None
    with db.conn() as c:
        assert any(f["context"].get("action_id") == a.json()["action_id"] for f in ems.f_ems_overdue(c, {}))
    done = client.patch(f"/api/ems/actions/{a.json()['action_id']}", headers=H["owner"], json={"status": "done", "note": "Fitted"})
    assert done.status_code == 200 and done.json()["completed_at"]
    with db.conn() as c:
        assert not any(f["context"].get("action_id") == a.json()["action_id"] for f in ems.f_ems_overdue(c, {}))
    ref = "IA-" + UNIQ()
    assert client.post("/api/ems/audits", headers=H["admin"], json={"audit_ref": ref, "standard": "ISO50001", "planned_date": "2020-02-01", "scope": "Terminal"}).status_code == 200
    assert client.post("/api/ems/audits", headers=H["admin"], json={"audit_ref": ref, "standard": "ISO50001", "planned_date": "2020-02-01"}).status_code == 409
    au = [x for x in client.get("/api/ems/audits", headers=H["auditor"]).json() if x["audit_ref"] == ref][0]
    assert au["overdue"] is True
    assert client.post(f"/api/ems/audits/{au['audit_id']}/complete", headers=H["admin"]).status_code == 200
    assert client.post(f"/api/ems/audits/{au['audit_id']}/complete", headers=H["admin"]).status_code == 404
    rv = client.post("/api/ems/reviews", headers=H["admin"], json={"standard": "ISO14001", "review_date": "2026-11-01", "attendees": "EHS, Ops", "decisions": "Extend target"})
    assert rv.status_code == 200 and "open_findings" in rv.json()["snapshot"] and any(x["title"] == body["title"] for x in rv.json()["snapshot"]["objectives"])
    assert client.get("/api/ems/reviews", headers=H["auditor"]).status_code == 200


# ------------------------------------------------------------------ legal register, nonconformities, energy baselines
def test_legal_register(client, H):
    ref = "LR-" + UNIQ()
    body = {"ref": ref, "title": "Discharge permit", "kind": "permit", "authority": "EAD", "permit_expiry": (date.today() + timedelta(days=20)).isoformat()}
    assert client.post("/api/ems/legal", headers=H["auditor"], json=body).status_code == 403
    assert client.post("/api/ems/legal", headers=H["admin"], json={**body, "kind": "nonsense"}).status_code == 400
    r = client.post("/api/ems/legal", headers=H["admin"], json=body).json()
    assert client.post("/api/ems/legal", headers=H["admin"], json=body).status_code == 409
    row = [x for x in client.get("/api/ems/legal", headers=H["auditor"]).json() if x["ref"] == ref][0]
    assert row["permit_soon"] is True and row["status"] == "under_review"
    from app import ems
    with db.conn() as c:
        assert any(f["context"].get("requirement_id") == r["requirement_id"] for f in ems.f_ems_overdue(c, {}))
    assert client.post(f"/api/ems/legal/{r['requirement_id']}/evaluate", headers=H["admin"], json={"status": "non_compliant"}).status_code == 400
    ok = client.post(f"/api/ems/legal/{r['requirement_id']}/evaluate", headers=H["admin"], json={"status": "compliant", "note": "Checked samples"})
    assert ok.status_code == 200 and ok.json()["evaluated_by"] and ok.json()["last_evaluated"]


def test_nonconformity_lifecycle(client, H):
    ref = "NC-" + UNIQ()
    body = {"nc_ref": ref, "source": "audit", "standard": "ISO14001", "description": "Oil spill kit empty", "severity": "major", "due_date": "2020-01-01"}
    n = client.post("/api/ems/nonconformities", headers=H["owner"], json=body).json()
    nid = n["nc_id"]
    assert client.post("/api/ems/nonconformities", headers=H["owner"], json=body).status_code == 409
    assert client.post(f"/api/ems/nonconformities/{nid}/actions", headers=H["owner"], json={"title": "Refill"}).status_code == 409   # root cause first
    assert client.post(f"/api/ems/nonconformities/{nid}/root-cause", headers=H["owner"], json={"root_cause": " "}).status_code == 400
    assert client.post(f"/api/ems/nonconformities/{nid}/root-cause", headers=H["owner"], json={"root_cause": "No inspection schedule"}).status_code == 200
    assert client.post(f"/api/ems/nonconformities/{nid}/verify", headers=H["owner"]).status_code == 409                       # no actions yet
    a = client.post(f"/api/ems/nonconformities/{nid}/actions", headers=H["owner"], json={"title": "Refill kits and add weekly check", "due_date": "2020-01-15"}).json()
    from app import ems
    with db.conn() as c:
        assert any(f["context"].get("nc_id") == nid for f in ems.f_ems_overdue(c, {})) and any(f["context"].get("action_id") == a["action_id"] for f in ems.f_ems_overdue(c, {}))
    assert client.post(f"/api/ems/nonconformities/{nid}/verify", headers=H["owner"]).status_code == 409                       # action still open
    assert client.patch(f"/api/ems/actions/{a['action_id']}", headers=H["owner"], json={"status": "done"}).status_code == 200
    assert [x for x in client.get("/api/ems/actions", headers=H["auditor"]).json() if x["action_id"] == a["action_id"]][0]["objective"].startswith("Nonconformity")
    assert client.post(f"/api/ems/nonconformities/{nid}/verify", headers=H["owner"]).status_code == 200
    assert client.post(f"/api/ems/nonconformities/{nid}/close", headers=H["owner"], json={"effectiveness_note": "x"}).status_code == 403   # major: not by raiser
    assert client.post(f"/api/ems/nonconformities/{nid}/close", headers=H["admin"], json={"effectiveness_note": " "}).status_code == 400
    c = client.post(f"/api/ems/nonconformities/{nid}/close", headers=H["admin"], json={"effectiveness_note": "Weekly checks logged for 4 weeks"})
    assert c.status_code == 200 and c.json()["status"] == "closed"
    assert client.post(f"/api/ems/nonconformities/{nid}/close", headers=H["admin"], json={"effectiveness_note": "again"}).status_code == 409


def test_enpi_regression_and_baseline(client, H):
    from app import compliance
    icpt, slope, r2 = compliance.ols([1, 2, 3, 4], [102, 104, 106, 108])
    assert icpt == pytest.approx(100) and slope == pytest.approx(2) and r2 == pytest.approx(1)
    assert compliance.ols([5, 5, 5], [1, 2, 3])[1] == 0.0
    root = client.get("/api/entities", headers=H["admin"]).json()[0]["entity_id"]
    thin = client.post("/api/ems/enpi/baselines", headers=H["admin"], json={"name": "t", "entity_id": root, "driver": "teu", "baseline_start": "1999-01-01", "baseline_end": "1999-12-31"})
    assert thin.status_code == 422 and "6 months" in thin.text
    assert client.post("/api/ems/enpi/baselines", headers=H["admin"], json={"name": "t", "entity_id": root, "driver": "bad", "baseline_start": "2025-01-01", "baseline_end": "2025-12-31"}).status_code == 400
    y = date.today().year
    ok = client.post("/api/ems/enpi/baselines", headers=H["admin"], json={"name": "YTD", "entity_id": root, "driver": "none", "baseline_start": f"{y}-01-01", "baseline_end": date.today().isoformat()})
    if ok.status_code == 200:
        p = client.get(f"/api/ems/enpi/baselines/{ok.json()['baseline_id']}/performance?start={y}-01-01&end={date.today().isoformat()}", headers=H["auditor"]).json()
        assert p["months"] and "note" in p
    else:
        assert ok.status_code == 422        # demo data has under 6 months this early in the year


# ------------------------------------------------------------------ template pack, code lookups, reference imports
def test_template_pack_and_code_lookups(client, H):
    import io as _io, zipfile
    z = client.get("/api/templates.zip", headers=H["owner"])
    assert z.status_code == 200
    zf = zipfile.ZipFile(_io.BytesIO(z.content))
    names = set(zf.namelist())
    assert {"templates/fuel_transaction.csv", "examples/fuel_transaction_example.csv", "data_dictionary.csv", "README.txt", "table_index.csv"} <= names
    head = zf.read("templates/fuel_transaction.csv").decode().splitlines()[0].split(",")
    assert "entity_code" in head and "entity_id" not in head and "validation_status" not in head and "txn_id" not in head
    assert "litres" in zf.read("data_dictionary.csv").decode()
    assert client.get("/api/templates/nonsense.csv", headers=H["owner"]).status_code == 404
    assert client.get("/api/templates/emission_factor.csv", headers=H["owner"]).status_code == 404      # admin-only table
    # the example row loads as it is, and a CSV with a code instead of a UUID loads too
    ex = client.get("/api/templates/fuel_transaction.csv?example=true", headers=H["owner"]).text
    hdr, row = ex.strip().splitlines()[:2]
    cols = hdr.split(",")
    vals = row.split(",")
    code = vals[cols.index("entity_code")]
    if "source_record_id" in cols:
        vals[cols.index("source_record_id")] = "tpl-" + UNIQ()
    csv_text = hdr + "\n" + ",".join(vals[:cols.index("litres")] + ["12"] + vals[cols.index("litres") + 1:]).replace(vals[cols.index("ts")], f"2020-10-{1 + int(UNIQ(), 16) % 28:02d}T08:{int(UNIQ(), 16) % 60:02d}:{int(UNIQ(), 16) % 60:02d}+00:00") + "\n"
    r = client.post("/api/ingest/fuel_transaction/csv", headers=H["owner"], files={"file": ("f.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["inserted"] == 1, r.text
    assert r.json()["batch_id"]
    bad = client.post("/api/ingest/fuel_transaction/csv", headers=H["owner"], files={"file": ("f.csv", csv_text.replace(code, "NO-SUCH-CODE").encode(), "text/csv")}).json()
    assert bad["inserted"] == 0 and "NO-SUCH-CODE" in bad["errors"][0]["error"]
    # the JSON API takes codes as well
    ok = client.post("/api/ingest/fuel_transaction", headers=H["owner"], json={"records": [
        {"entity_code": code, "fuel_type": "diesel", "litres": 3, "ts": "2020-10-06T08:00:00Z", "source_record_id": "code-" + UNIQ()}]}).json()
    assert ok["inserted"] == 1


def test_reference_imports(client, H):
    code = "M_" + UNIQ().upper()
    m = f"metric_code,name,pillar,unit,frequency,source_classes\n{code},Test metric,E,m3,monthly,G_manual|B_energy\nBAD,x,Q,,,\n"
    r = client.post("/api/import/metrics", headers=H["admin"], files={"file": ("m.csv", m.encode(), "text/csv")}).json()
    assert r["added"] == 1 and len(r["errors"]) == 1
    again = client.post("/api/import/metrics", headers=H["admin"], files={"file": ("m.csv", m.encode(), "text/csv")}).json()
    assert again["added"] == 0 and again["updated"] == 1
    assert client.post("/api/import/metrics", headers=H["owner"], files={"file": ("m.csv", m.encode(), "text/csv")}).status_code == 403
    fw = f"framework_code,framework_name,disclosure_code,title,metric_codes\nTESTFW,Test framework,T-1,First,{code}|NOPE\n"
    f = client.post("/api/import/framework-map", headers=H["admin"], files={"file": ("f.csv", fw.encode(), "text/csv")}).json()
    assert f["disclosures"] == 1 and f["new_mappings"] == 1 and "NOPE" in f["errors"][0]["error"]
    ref = "IMP-" + UNIQ()
    ent = client.get("/api/records/entity?limit=1", headers=H["admin"]).json()["rows"][0]
    lg = f"ref,title,kind,authority,entity_code,permit_expiry\n{ref},Imported permit,permit,EAD,{ent['code']},2027-01-01\n{ref}2,Bad kind,nonsense,,,\n"
    l = client.post("/api/import/legal", headers=H["admin"], files={"file": ("l.csv", lg.encode(), "text/csv")}).json()
    assert l["added"] == 1 and len(l["errors"]) == 1
    assert any(x["ref"] == ref for x in client.get("/api/ems/legal", headers=H["auditor"]).json())
    miss = client.post("/api/import/legal", headers=H["admin"], files={"file": ("l.csv", b"ref,title\nx,y\n", "text/csv")})
    assert miss.status_code == 400
