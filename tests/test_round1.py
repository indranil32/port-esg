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
