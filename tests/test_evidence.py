"""Field evidence capture: photo -> OCR -> flags -> linked reading -> approval -> derived consumption."""
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db, ocr
from app.seed import meter_photo

UNIQ = lambda: uuid.uuid4().hex[:8].upper()
NOW = lambda: datetime.now(timezone.utc)


def letters(n=8):
    return "".join(random.choices(string.ascii_uppercase, k=n))      # label text must not add numbers for OCR to find


def photo(shown, serial, taken):
    return meter_photo(shown, serial, taken)


def new_meter(client, H, digits=6, multiplier=10, code=None):
    ent = client.get("/api/records/entity?limit=1&entity_type=terminal", headers=H["admin"]).json()["rows"][0]["entity_id"]
    code = code or "TM" + UNIQ()
    r = client.post("/api/ingest/meter", headers=H["admin"], json={"records": [{
        "meter_code": code, "entity_id": ent, "meter_type": "electricity", "scope_boundary": "own", "uom": "kWh",
        "energy_type": "grid", "multiplier": multiplier, "register_digits": digits}]})
    assert r.json()["inserted"] == 1, r.json()
    row = client.get(f"/api/records/meter?meter_code={code}", headers=H["admin"]).json()["rows"][0]
    return row["meter_id"], code


def upload(client, H, who, data, meter_id=None, at=None):
    form = {"client_captured_at": (at or NOW()).isoformat()}
    if meter_id:
        form["meter_id"] = meter_id
    return client.post("/api/captures", headers=H[who], files={"file": ("m.jpg", data, "image/jpeg")}, data=form)


def reading(client, H, who, meter_id, code, shown, typed, hours_ago, **extra):
    at = NOW() - timedelta(hours=hours_ago)
    cap = upload(client, H, who, photo(shown, code, at), meter_id, at)
    assert cap.status_code == 200, cap.text
    r = client.post("/api/captures/submit-reading", headers=H[who], json={
        "capture_id": cap.json()["capture_id"], "meter_id": meter_id, "register_value": typed,
        "read_at": at.isoformat(), **extra})
    return cap.json(), r


def codes(flags):
    return {f["code"] for f in flags}


needs_ocr = pytest.mark.skipif(ocr.provider_name() == "none", reason="no OCR engine installed")


# ------------------------------------------------------------------ pure logic
def test_reading_extraction_ranks_dial_above_serial_and_labels():
    lines = [{"text": "SN 88231", "confidence": 0.95}, {"text": "004567.8", "confidence": 0.8}, {"text": "kWh", "confidence": 0.9},
             {"text": "50 Hz", "confidence": 0.9}]
    out = ocr.extract_readings(lines)
    assert out[0]["value"] == 4567.8
    assert ocr.extract_readings([{"text": "1 234 567", "confidence": 0.9}])[0]["value"] == 1234567
    assert ocr.extract_readings([{"text": "no digits here", "confidence": 0.9}]) == []


def test_ocr_failure_never_blocks_capture(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "none")
    res = ocr.run_ocr(b"anything")
    assert res["provider"] == "none" and res["lines"] == [] and res["confidence"] is None


# ------------------------------------------------------------------ end to end
@needs_ocr
def test_photo_to_approved_consumption(client, H):
    mid, code = new_meter(client, H, digits=6, multiplier=10)
    cap1, r1 = reading(client, H, "field", mid, code, "001000.0", 1000.0, 3)
    assert r1.status_code == 200, r1.text
    assert cap1["candidates"][0]["value"] == 1000.0 and cap1["ocr_provider"] != "none"
    assert cap1["exif_taken_at"] is not None and cap1["gps_source"] == "exif" and cap1["gps_lat"] is not None
    assert "value_mismatch" not in codes(r1.json()["flags"])
    cap2, r2 = reading(client, H, "field", mid, code, "001100.5", 1100.5, 1)
    assert r2.status_code == 200, r2.text
    rid1, rid2 = r1.json()["register_reading_id"], r2.json()["register_reading_id"]
    # the approver sees the photo, the OCR candidates and the typed value together
    q = client.get(f"/api/validation/meter_register_reading?meter_id={mid}", headers=H["admin"]).json()
    mine = [x for x in q if x["_id"] == rid2][0]
    assert mine["_evidence"][0]["candidates"][0]["value"] == 1100.5 and mine["_evidence"][0]["entered_value"] == 1100.5
    for rid in (rid1, rid2):                                           # baseline first, then the next one
        assert client.post(f"/api/validation/meter_register_reading/{rid}", headers=H["admin"], json={"action": "validate"}).status_code == 200
    got = client.get(f"/api/records/energy_meter_reading?meter_id={mid}", headers=H["admin"]).json()
    assert got["total"] == 1 and float(got["rows"][0]["energy_kwh"]) == pytest.approx(1005.0)    # (1100.5-1000) x 10
    assert got["rows"][0]["energy_type"] == "grid"
    # the photo can be fetched by reviewers; the file is the one that was uploaded
    img = client.get(f"/api/captures/{cap2['capture_id']}/image", headers=H["auditor"])
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    linked = client.get(f"/api/captures/for/meter_register_reading/{rid2}", headers=H["auditor"]).json()
    assert len(linked) == 1 and linked[0]["capture_id"] == cap2["capture_id"]


@needs_ocr
def test_typed_value_that_disagrees_with_photo_is_flagged(client, H):
    mid, code = new_meter(client, H)
    cap, r = reading(client, H, "field", mid, code, "000122.0", 212.0, 1)
    assert r.status_code == 200
    assert "value_mismatch" in codes(r.json()["flags"])
    row = client.get(f"/api/captures/{cap['capture_id']}", headers=H["admin"]).json()
    assert row["entered_value"] == 212.0 and row["status"] == "linked"


def test_reused_photo_and_wrong_meter_are_flagged(client, H):
    mid, code = new_meter(client, H, code="PUMP" + letters(4))      # letters only: reliable for OCR to read back
    mid2, code2 = new_meter(client, H)
    at = NOW() - timedelta(hours=1)
    data = photo("000500.0", code, at)
    first = upload(client, H, "field", data, mid, at).json()
    again = upload(client, H, "field", data, mid, at).json()
    assert "duplicate_file" in codes(again["flags"]) and "duplicate_file" not in codes(first["flags"])
    # the plate says `code`, but the operator submits it against another meter
    other = upload(client, H, "field", photo("000777.0", code, at), None, at).json()
    r = client.post("/api/captures/submit-reading", headers=H["field"], json={
        "capture_id": other["capture_id"], "meter_id": mid2, "register_value": 777.0, "read_at": at.isoformat()})
    if ocr.provider_name() != "none":
        assert "wrong_meter" in codes(r.json()["flags"])


def test_photo_with_future_or_missing_time_and_bad_files(client, H):
    mid, code = new_meter(client, H)
    soon = NOW() + timedelta(hours=3)
    cap = upload(client, H, "field", photo("000100.0", code, soon), mid, NOW()).json()
    assert "future_timestamp" in codes(cap["flags"])
    bad = client.post("/api/captures", headers=H["field"], files={"file": ("x.jpg", b"not an image", "image/jpeg")})
    assert bad.status_code == 415
    assert client.post("/api/captures", headers=H["field"], files={"file": ("x.jpg", photo("1", code, NOW()), "image/jpeg")},
                       data={"gps_lat": "95", "gps_lon": "10"}).status_code == 400


# ------------------------------------------------------------------ integrity of the link and the sequence
def test_linking_is_atomic_and_single_use(client, H):
    mid, code = new_meter(client, H)
    mid2, _ = new_meter(client, H)
    at = NOW() - timedelta(hours=2)
    cap = upload(client, H, "field", photo("000300.0", code, at), mid, at).json()
    body = {"capture_id": cap["capture_id"], "meter_id": mid, "register_value": 300.0, "read_at": at.isoformat()}
    # photo taken for another meter: refused, and neither the reading nor the link is created
    wrong = dict(body, meter_id=mid2)
    assert client.post("/api/captures/submit-reading", headers=H["field"], json=wrong).status_code == 409
    assert client.get(f"/api/records/meter_register_reading?meter_id={mid2}", headers=H["admin"]).json()["total"] == 0
    assert client.get(f"/api/captures/{cap['capture_id']}", headers=H["admin"]).json()["status"] == "new"
    assert client.post("/api/captures/submit-reading", headers=H["field"], json=body).status_code == 200
    again = client.post("/api/captures/submit-reading", headers=H["field"], json=dict(body, read_at=(at + timedelta(minutes=5)).isoformat()))
    assert again.status_code == 409                                      # photo already used by a record
    assert client.get(f"/api/records/meter_register_reading?meter_id={mid}", headers=H["admin"]).json()["total"] == 1


def test_decrease_needs_rollover_and_blocks_approval(client, H):
    mid, code = new_meter(client, H, digits=4, multiplier=1)
    _, a = reading(client, H, "field", mid, code, "009000.0", 9000.0, 5)
    _, b = reading(client, H, "field", mid, code, "000150.0", 150.0, 3)
    assert "reading_decreased" in codes(b.json()["flags"])
    ra, rb = a.json()["register_reading_id"], b.json()["register_reading_id"]
    assert client.post(f"/api/validation/meter_register_reading/{ra}", headers=H["admin"], json={"action": "validate"}).status_code == 200
    blocked = client.post(f"/api/validation/meter_register_reading/{rb}", headers=H["admin"], json={"action": "validate"})
    assert blocked.status_code == 409 and "rollover" in blocked.text.lower()
    still = client.get(f"/api/records/meter_register_reading?meter_id={mid}", headers=H["admin"]).json()["rows"]
    assert {r["validation_status"] for r in still if r["register_reading_id"] == rb} == {"pending"}       # veto rolled the approval back
    fixed = client.patch(f"/api/records/meter_register_reading/{rb}", headers=H["field"], json={"rollover": True})
    assert fixed.status_code == 200
    assert client.post(f"/api/validation/meter_register_reading/{rb}", headers=H["admin"], json={"action": "validate"}).status_code == 200
    e = client.get(f"/api/records/energy_meter_reading?meter_id={mid}", headers=H["admin"]).json()["rows"]
    assert len(e) == 1 and float(e[0]["energy_kwh"]) == pytest.approx(1150.0)                             # 9000 -> wrap at 10 000 -> 150


def test_editing_an_approved_reading_withdraws_its_consumption(client, H):
    mid, code = new_meter(client, H, multiplier=1)
    _, a = reading(client, H, "field", mid, code, "000100.0", 100.0, 4)
    _, b = reading(client, H, "field", mid, code, "000160.0", 160.0, 2)
    for r in (a, b):
        client.post(f"/api/validation/meter_register_reading/{r.json()['register_reading_id']}", headers=H["admin"], json={"action": "validate"})
    assert client.get(f"/api/records/energy_meter_reading?meter_id={mid}", headers=H["admin"]).json()["total"] == 1
    client.patch(f"/api/records/meter_register_reading/{b.json()['register_reading_id']}", headers=H["admin"], json={"register_value": 170.0})
    assert client.get(f"/api/records/energy_meter_reading?meter_id={mid}", headers=H["admin"]).json()["total"] == 0


# ------------------------------------------------------------------ roles
def test_field_operator_boundaries(client, H):
    mid, code = new_meter(client, H)
    assert client.post("/api/ingest/entity", headers=H["field"], json={"records": [{"code": "X", "name": "x", "entity_type": "facility"}]}).status_code == 403
    assert client.get("/api/records/spend_record", headers=H["field"]).status_code == 403
    assert client.get("/api/lookup/emission_factor", headers=H["field"]).status_code == 403
    assert client.get("/api/lookup/meter", headers=H["field"]).status_code == 200
    assert client.get("/api/validation/summary", headers=H["field"]).status_code == 403
    _, r = reading(client, H, "field", mid, code, "000100.0", 100.0, 1)
    rid = r.json()["register_reading_id"]
    assert client.post(f"/api/validation/meter_register_reading/{rid}", headers=H["field"], json={"action": "validate"}).status_code == 403
    mine = client.get("/api/records/meter_register_reading", headers=H["field"]).json()["rows"]
    assert mine and all(x["created_by"] == "field.operator@adports-demo.local" for x in mine)
    # tenants and other users cannot see someone else's photos; reviewers can
    cap_id = client.get("/api/captures?limit=1", headers=H["field"]).json()[0]["capture_id"]
    assert client.get(f"/api/captures/{cap_id}/image", headers=H["tenant"]).status_code == 404
    assert client.get(f"/api/captures/{cap_id}/image", headers=H["auditor"]).status_code == 200
    assert client.get("/api/captures", headers=H["tenant"]).status_code == 403
    # the creator cannot approve their own submission even as admin
    ent = client.post("/api/captures/submit-reading", headers=H["admin"], json={"meter_id": mid, "register_value": 120.0})
    assert ent.status_code == 200 and "no_evidence" in codes(ent.json()["flags"])
    own = client.post(f"/api/validation/meter_register_reading/{ent.json()['register_reading_id']}", headers=H["admin"], json={"action": "validate"})
    assert own.status_code == 403


def test_suspect_evidence_raises_an_alert(client, H):
    mid, code = new_meter(client, H)
    at = NOW() - timedelta(hours=1)
    data = photo("000321.0", code, at)
    upload(client, H, "field", data, mid, at)
    dup = upload(client, H, "field", data, mid, at).json()
    r = client.post("/api/captures/submit-reading", headers=H["field"], json={
        "capture_id": dup["capture_id"], "meter_id": mid, "register_value": 321.0, "read_at": at.isoformat()})
    assert r.status_code == 200
    client.post("/api/alerts/evaluate", headers=H["admin"])
    titles = [a["title"] for a in client.get("/api/alerts", headers=H["admin"]).json()]
    assert any(t.startswith("Suspect field evidence") for t in titles), titles


def test_manual_meters_are_not_judged_by_the_scada_freshness_rule(client, H):
    from app import alerts
    mid, code = new_meter(client, H)
    reading(client, H, "field", mid, code, "000100.0", 100.0, 1)
    with db.conn() as c:
        silent = [f["context"]["meter"] for f in alerts.f_freshness(c, {})]
    assert code not in silent
