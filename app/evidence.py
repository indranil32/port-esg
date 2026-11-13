"""Field evidence capture: photo upload, OCR, integrity flags, linking to records, and derivation of
interval consumption from cumulative meter register readings.

Flow for a meter reading:  photo -> POST /api/captures (OCR + flags) -> operator confirms or corrects the
number -> POST /api/captures/submit-reading (record + link in one transaction) -> approver sees photo,
OCR result and flags beside the entered value -> approval derives consumption into energy_meter_reading.

Flags never block a capture; they travel with it to the approver. The one hard rule is arithmetic:
a register value lower than the previous validated one cannot be approved unless marked as rollover.
"""
import hashlib
import io
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import db, ocr
from .auth import current_user, require

router = APIRouter(prefix="/api/captures")

EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", "./data/evidence"))
LOCAL_TZ = ZoneInfo(os.environ.get("APP_TZ", "Asia/Dubai"))   # EXIF carries no zone unless OffsetTimeOriginal exists
MAX_BYTES = 15 * 1024 * 1024
STALE_HOURS = float(os.environ.get("EVIDENCE_STALE_HOURS", 24))
CLOCK_SKEW_MIN = 5
READ_AT_TOLERANCE_H = 1.0
MATCH_TOLERANCE = 0.0005          # relative tolerance when comparing the typed value with the photo
NEAR_DUP_BITS = 5                 # dHash Hamming distance at or below which two photos count as the same shot
JUMP_FACTOR = 3.0                 # consumption above this multiple of the meter's recent average is flagged
Image.MAX_IMAGE_PIXELS = 80_000_000

FIELD_ROLES = ("field_operator", "data_owner", "esg_admin")
REVIEW_ROLES = ("esg_admin", "data_owner", "auditor")
LINKABLE = {"meter_register_reading", "fuel_transaction", "refrigerant_event", "waste_manifest",
            "environmental_incident", "safety_incident", "energy_meter_reading", "utility_invoice",
            "certification", "audit_finding", "training_record", "tenant_esg_submission"}


def flag(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


# --------------------------------------------------------------------------- image metadata
def _to_deg(vals, ref):
    d, m, s = (float(x) for x in vals)
    v = d + m / 60 + s / 3600
    return -v if ref in ("S", "W") else v


def read_exif(img: Image.Image) -> dict:
    """Capture time (camera clock), GPS and device from EXIF. Missing or malformed fields are simply absent."""
    out = {}
    try:
        ex = img.getexif()
        sub = ex.get_ifd(0x8769)
        raw = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
        if raw:
            t = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
            off = sub.get(0x9011)                                  # OffsetTimeOriginal, e.g. "+04:00"
            if off and re.fullmatch(r"[+-]\d\d:\d\d", str(off)):
                sign = 1 if off[0] == "+" else -1
                tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6])))
            else:
                tz = LOCAL_TZ
            out["taken_at"] = t.replace(tzinfo=tz)
        gps = ex.get_ifd(0x8825)
        if gps.get(2) and gps.get(4):
            out["lat"] = _to_deg(gps[2], gps.get(1, "N"))
            out["lon"] = _to_deg(gps[4], gps.get(3, "E"))
        dev = " ".join(str(x).strip() for x in (ex.get(0x010F), ex.get(0x0110)) if x)
        if dev:
            out["device"] = dev[:80]
    except Exception:
        pass
    return out


def dhash(img: Image.Image) -> str:
    """64-bit difference hash: robust to re-compression and resizing, sensitive to a different scene."""
    g = ImageOps.exif_transpose(img).convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# --------------------------------------------------------------------------- flags
def _meter_codes(c):
    return [(r["meter_id"], r["meter_code"]) for r in db.rows(c, "SELECT meter_id, meter_code FROM meter")]


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def wrong_meter_flag(c, meter, text):
    """The nameplate in the photo carries a different meter's code than the one being read."""
    if not (meter and text):
        return None
    txt = _norm(text)
    hits = [(mid, code) for mid, code in _meter_codes(c) if len(_norm(code)) >= 4 and _norm(code) in txt]
    if hits and str(meter["meter_id"]) not in {str(m) for m, _ in hits}:
        return flag("wrong_meter", "critical", f"Photo shows {hits[0][1]}, but the reading is for {meter['meter_code']}.")
    return None


def capture_flags(c, *, sha, phash, taken_at, client_at, has_gps, ocr_res, cands, meter, now):
    fl = []
    same = db.rows(c, "SELECT ec.capture_id FROM evidence_capture ec JOIN evidence_document d ON d.doc_id = ec.doc_id "
                      "WHERE d.sha256 = %s AND ec.status <> 'discarded'", (sha,))
    if same:
        fl.append(flag("duplicate_file", "critical", "This exact file was already uploaded. Photos must not be re-used."))
    elif phash:
        # The same dial photographed on another day looks alike, so a visual match only counts when the number
        # read from it is also the same (or cannot be read at all).
        mine = cands[0]["value"] if cands else None
        near = []
        for o in db.rows(c, "SELECT phash, candidates, meter_id FROM evidence_capture WHERE status <> 'discarded' "
                            "AND phash IS NOT NULL ORDER BY created_at DESC LIMIT 5000"):
            if meter and o["meter_id"] and o["meter_id"] != meter["meter_id"]:
                continue                                   # two different meters showing the same number is a coincidence
            d = hamming(phash, o["phash"])
            theirs = o["candidates"][0]["value"] if o["candidates"] else None
            if d <= NEAR_DUP_BITS and (mine is None or theirs is None or mine == theirs):
                near.append(d)
        if near:
            fl.append(flag("near_duplicate", "critical",
                           f"Looks like the same shot as an earlier capture (differs by {min(near)} of 64 bits, same number)."))
    when = taken_at or client_at
    if when is None:
        fl.append(flag("no_timestamp", "warning", "No capture time in the image and none from the device."))
    else:
        if when > now + timedelta(minutes=CLOCK_SKEW_MIN):
            fl.append(flag("future_timestamp", "critical", f"Capture time {when:%Y-%m-%d %H:%M} is in the future."))
        elif now - when > timedelta(hours=STALE_HOURS):
            fl.append(flag("stale_photo", "warning", f"Photo was taken {(now - when).total_seconds() / 3600:.0f} h before upload."))
        if taken_at and client_at and abs((taken_at - client_at).total_seconds()) > 600:
            fl.append(flag("clock_mismatch", "warning", "Camera time and device time differ by more than 10 minutes."))
    if taken_at is None and client_at is not None:
        fl.append(flag("timestamp_unverified", "info", "Time comes from the device clock, not from the image metadata."))
    if not has_gps:
        fl.append(flag("no_gps", "info", "No location attached to this photo."))
    if ocr_res["provider"] == "none" or ocr_res.get("error"):
        fl.append(flag("ocr_unavailable", "info", "OCR did not run" + (f": {ocr_res['error']}" if ocr_res.get("error") else ".")))
    elif not cands:
        fl.append(flag("no_reading_found", "warning", "OCR found no number in the photo."))
    elif cands[0]["confidence"] < 0.6:
        fl.append(flag("low_ocr_confidence", "warning", f"OCR confidence is low ({cands[0]['confidence']:.0%}); check the number by eye."))
    wm = wrong_meter_flag(c, meter, ocr_res["text"])
    if wm:
        fl.append(wm)
    return fl


def value_flags(c, meter_id, value, read_at, cap, exclude_rid=None):
    """Checks that need the typed value: match with the photo, sequence, jump size, time of reading."""
    fl = []
    cands = cap["candidates"] if cap else []
    if cap and cap["ocr_text"]:
        wm = wrong_meter_flag(c, db.row(c, "SELECT meter_id, meter_code FROM meter WHERE meter_id = %s", (meter_id,)), cap["ocr_text"])
        if wm:
            fl.append(wm)
    if cap and cands and cap["ocr_provider"] != "none":
        v = float(value)
        ok = any(abs(float(x["value"]) - v) <= max(MATCH_TOLERANCE * max(abs(v), 1), 0.051) for x in cands)
        if not ok:
            seen = ", ".join("%g" % x["value"] for x in cands[:3])
            fl.append(flag("value_mismatch", "critical", f"Typed value {v:g} does not match the photo (OCR read {seen})."))
    if cap and read_at:
        when = cap["exif_taken_at"] or cap["client_captured_at"]
        if when and abs((read_at - when).total_seconds()) > READ_AT_TOLERANCE_H * 3600:
            fl.append(flag("read_at_mismatch", "warning", "Reading time differs from the photo time by more than an hour."))
    fl += sequence_flags(c, meter_id, value, read_at, exclude_rid)
    return fl


def sequence_flags(c, meter_id, value, read_at, exclude_rid=None, rollover=False):
    fl = []
    prev = db.row(c, "SELECT register_value, read_at FROM meter_register_reading WHERE meter_id = %s AND read_at < %s "
                     "AND validation_status <> 'rejected' AND (register_reading_id::text <> %s OR %s::text IS NULL) "
                     "ORDER BY read_at DESC LIMIT 1", (meter_id, read_at, exclude_rid, exclude_rid))
    if not prev:
        return [flag("first_reading", "info", "No earlier reading: this becomes the baseline and yields no consumption.")]
    delta = Decimal(str(value)) - prev["register_value"]
    if delta < 0 and not rollover:
        fl.append(flag("reading_decreased", "critical",
                       f"Lower than the previous reading ({prev['register_value']:g} on {prev['read_at']:%Y-%m-%d}). Mark as rollover if the dial wrapped."))
    elif delta >= 0:
        hist = db.rows(c, "SELECT consumption_raw / greatest(extract(epoch FROM (read_at - lag_t)) / 86400, 0.04) AS per_day FROM ("
                          "SELECT consumption_raw, read_at, lag(read_at) OVER (ORDER BY read_at) AS lag_t "
                          "FROM meter_register_reading WHERE meter_id = %s AND validation_status = 'validated' "
                          "AND consumption_raw IS NOT NULL) x WHERE lag_t IS NOT NULL ORDER BY read_at DESC LIMIT 12", (meter_id,))
        days = max((read_at - prev["read_at"]).total_seconds() / 86400, 0.04)
        if len(hist) >= 3:
            avg = sum(h["per_day"] for h in hist) / len(hist)
            if avg > 0 and float(delta) / days > JUMP_FACTOR * float(avg):
                fl.append(flag("implausible_jump", "warning",
                               f"Consumption of {float(delta) / days:,.0f}/day is over {JUMP_FACTOR:g}x the recent average ({float(avg):,.0f}/day)."))
    return fl


def merge_flags(*lists):
    seen, out = set(), []
    for lst in lists:
        for f in lst:
            if f["code"] not in seen:
                seen.add(f["code"])
                out.append(f)
    return out


# --------------------------------------------------------------------------- upload
def _owner_or_reviewer(user, cap):
    return user["role"] in ("esg_admin", "data_owner", "auditor") or cap["captured_by"] == user["email"]


def _public(cap):
    return {k: v for k, v in cap.items() if k not in ("ocr_lines",)}


def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=LOCAL_TZ)
    except ValueError:
        raise HTTPException(400, f"Not an ISO date-time: {s}")


@router.get("/config")
def config(user: dict = Depends(current_user)):
    return {"ocr_provider": ocr.provider_name(), "available": ocr.available_providers(),
            "stale_hours": STALE_HOURS, "max_mb": MAX_BYTES // (1024 * 1024), "linkable": sorted(LINKABLE)}


def process_image(c, email, data, meter_id=None, client_at=None, gps_lat=None, gps_lon=None, gps_accuracy_m=None):
    """Validate, fingerprint, OCR, flag and store one photo. Raises HTTPException on unusable input."""
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"Photo too large ({MAX_BYTES // (1024 * 1024)} MB max)")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").lower()
    except Exception:
        raise HTTPException(415, "That file is not a readable image (JPEG, PNG or WebP; convert HEIC to JPEG)")
    if fmt not in ("jpeg", "png", "webp"):
        raise HTTPException(415, f"Unsupported image type {fmt}")
    if (gps_lat is None) != (gps_lon is None) or (gps_lat is not None and not (-90 <= gps_lat <= 90 and -180 <= gps_lon <= 180)):
        raise HTTPException(400, "gps_lat and gps_lon must be given together and be valid coordinates")
    sha = hashlib.sha256(data).hexdigest()
    exif = read_exif(img)
    ph = dhash(img)
    res = ocr.run_ocr(data)
    now = datetime.now(timezone.utc)
    meter = None
    if meter_id:
        meter = db.row(c, "SELECT * FROM meter WHERE meter_id::text = %s", (str(meter_id),))
        if not meter:
            raise HTTPException(404, "Unknown meter")
    cands = ocr.extract_readings(res["lines"], meter["register_digits"] if meter else None)
    lat, lon, src = (exif["lat"], exif["lon"], "exif") if "lat" in exif else \
                    (gps_lat, gps_lon, "browser" if gps_lat is not None else None)
    fl = capture_flags(c, sha=sha, phash=ph, taken_at=exif.get("taken_at"), client_at=client_at,
                       has_gps=lat is not None, ocr_res=res, cands=cands, meter=meter, now=now)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[fmt]
    path = EVIDENCE_DIR / f"{sha[:24]}.{ext}"
    path.write_bytes(data)
    doc = db.row(c, "INSERT INTO evidence_document (file_uri, sha256, mime_type, description, uploaded_by) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING doc_id",
                 (str(path), sha, f"image/{fmt}", "Field photo" + (f" - {meter['meter_code']}" if meter else ""), email))
    cap = db.row(c, """
        INSERT INTO evidence_capture (doc_id, captured_by, meter_id, ocr_provider, ocr_ms, ocr_text, ocr_lines,
            ocr_confidence, candidates, exif_taken_at, client_captured_at, gps_lat, gps_lon, gps_accuracy_m,
            gps_source, device, phash, image_w, image_h, flags)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                 (doc["doc_id"], email, meter["meter_id"] if meter else None, res["provider"], res["ms"],
                  res["text"], Jsonb(res["lines"]), res["confidence"], Jsonb(cands), exif.get("taken_at"), client_at,
                  lat, lon, gps_accuracy_m if src == "browser" else None, src, exif.get("device"), ph,
                  img.width, img.height, Jsonb(fl)))
    db.audit(c, email, "insert", "evidence_capture", cap["capture_id"],
             after={"sha256": sha, "flags": [f["code"] for f in fl], "ocr": res["provider"]})
    return cap


@router.post("")
def upload(file: UploadFile = File(...), meter_id: str | None = Form(None), client_captured_at: str | None = Form(None),
           gps_lat: float | None = Form(None), gps_lon: float | None = Form(None), gps_accuracy_m: float | None = Form(None),
           user: dict = Depends(require(*FIELD_ROLES))):
    data = file.file.read(MAX_BYTES + 1)
    client_at = _parse_dt(client_captured_at)
    with db.conn() as c:
        return _public(process_image(c, user["email"], data, meter_id, client_at, gps_lat, gps_lon, gps_accuracy_m))


# --------------------------------------------------------------------------- read
@router.get("/meters")
def meters_for_capture(user: dict = Depends(require(*FIELD_ROLES))):
    with db.conn() as c:
        return db.rows(c, """
            SELECT m.meter_id, m.meter_code, m.meter_type, m.uom, m.location_desc, m.multiplier, m.register_digits,
                   (SELECT jsonb_build_object('value', r.register_value, 'read_at', r.read_at, 'status', r.validation_status)
                    FROM meter_register_reading r WHERE r.meter_id = m.meter_id AND r.validation_status <> 'rejected'
                    ORDER BY r.read_at DESC LIMIT 1) AS last_reading
            FROM meter m ORDER BY m.meter_code""")


@router.get("")
def list_captures(status: str | None = None, flagged: bool | None = None, meter_id: str | None = None,
                  limit: int = Query(50, le=200), user: dict = Depends(current_user)):
    if user["role"] == "tenant":
        raise HTTPException(403, "Not available to tenants")
    where, params = [], []
    if user["role"] == "field_operator":
        where.append("captured_by = %s")
        params.append(user["email"])
    if status:
        where.append("status = %s")
        params.append(status)
    if flagged is not None:
        where.append("(flags <> '[]'::jsonb)" if flagged else "(flags = '[]'::jsonb)")
    if meter_id:
        where.append("meter_id::text = %s")
        params.append(meter_id)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with db.conn() as c:
        return db.rows(c, f"SELECT ec.*, m.meter_code FROM (SELECT capture_id, doc_id, captured_by, created_at, meter_id, status, "
                          f"target_table, target_record_id, entered_value, ocr_confidence, candidates, exif_taken_at, "
                          f"client_captured_at, gps_lat, gps_lon, flags FROM evidence_capture {w} "
                          f"ORDER BY created_at DESC LIMIT %s) ec LEFT JOIN meter m ON m.meter_id = ec.meter_id "
                          f"ORDER BY ec.created_at DESC", params + [limit])


@router.get("/for/{table}/{rid}")
def captures_for(table: str, rid: str, user: dict = Depends(require(*REVIEW_ROLES))):
    with db.conn() as c:
        return db.rows(c, "SELECT capture_id, captured_by, created_at, ocr_provider, ocr_confidence, candidates, entered_value, "
                          "exif_taken_at, client_captured_at, gps_lat, gps_lon, gps_source, device, image_w, image_h, flags, ocr_lines "
                          "FROM evidence_capture WHERE target_table = %s AND target_record_id = %s ORDER BY created_at",
                       (table, rid))


@router.get("/{capture_id}")
def get_capture(capture_id: str, user: dict = Depends(current_user)):
    with db.conn() as c:
        cap = db.row(c, "SELECT * FROM evidence_capture WHERE capture_id::text = %s", (capture_id,))
    if not cap or user["role"] == "tenant" or not _owner_or_reviewer(user, cap):
        raise HTTPException(404, "Capture not found")
    return cap


@router.get("/{capture_id}/image")
def get_image(capture_id: str, user: dict = Depends(current_user)):
    with db.conn() as c:
        cap = db.row(c, "SELECT ec.captured_by, d.file_uri, d.mime_type FROM evidence_capture ec "
                        "JOIN evidence_document d ON d.doc_id = ec.doc_id WHERE ec.capture_id::text = %s", (capture_id,))
    if not cap or user["role"] == "tenant" or not _owner_or_reviewer(user, cap) or not Path(cap["file_uri"]).exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(cap["file_uri"], media_type=cap["mime_type"], headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------- link
def _link(c, user, cap_id, table, rid, entered=None, extra_flags=()):
    """Atomically attach an unlinked capture to one record; the UPDATE's WHERE clause is the race guard."""
    cap = db.row(c, "SELECT * FROM evidence_capture WHERE capture_id::text = %s FOR UPDATE", (cap_id,))
    if not cap or not _owner_or_reviewer(user, cap):
        raise HTTPException(404, "Capture not found")
    if cap["status"] != "new" or cap["target_table"] is not None:
        raise HTTPException(409, "This photo is already attached to a record" if cap["status"] == "linked" else "This photo was discarded")
    fl = merge_flags(cap["flags"], list(extra_flags))
    new = db.row(c, "UPDATE evidence_capture SET status = 'linked', target_table = %s, target_record_id = %s, linked_at = now(), "
                    "entered_value = %s, flags = %s WHERE capture_id = %s AND status = 'new' AND target_table IS NULL RETURNING *",
                 (table, rid, entered, Jsonb(fl), cap["capture_id"]))
    if not new:
        raise HTTPException(409, "This photo was just attached elsewhere")
    db.audit(c, user["email"], "link_evidence", table, rid, after={"capture_id": cap["capture_id"], "flags": [f["code"] for f in fl]})
    return new


class LinkBody(BaseModel):
    table: str
    record_id: str


@router.post("/{capture_id}/link")
def link(capture_id: str, body: LinkBody, user: dict = Depends(require(*FIELD_ROLES))):
    from .capture import can_write, pk_cols
    if body.table not in LINKABLE or not can_write(user, body.table):
        raise HTTPException(403, f"You cannot attach evidence to {body.table}")
    with db.conn() as c:
        pk = pk_cols(c, body.table)[0]
        rec = db.row(c, f"SELECT * FROM esg.{body.table} WHERE {pk}::text = %s", (body.record_id,))
        if not rec:
            raise HTTPException(404, "Record not found")
        if user["role"] == "field_operator" and rec.get("created_by") != user["email"]:
            raise HTTPException(403, "You can only attach evidence to records you created")
        extra, entered = [], None
        if body.table == "meter_register_reading":
            cap = db.row(c, "SELECT * FROM evidence_capture WHERE capture_id::text = %s", (capture_id,))
            if cap:
                extra = value_flags(c, rec["meter_id"], rec["register_value"], rec["read_at"], cap, str(rec[pk]))
                entered = rec["register_value"]
        return _public(_link(c, user, capture_id, body.table, body.record_id, entered, extra))


class ReadingBody(BaseModel):
    capture_id: str | None = None
    meter_id: str
    register_value: float
    read_at: datetime | None = None
    rollover: bool = False
    note: str | None = None


@router.post("/submit-reading")
def submit_reading(body: ReadingBody, user: dict = Depends(require(*FIELD_ROLES))):
    """Create the register reading and attach its photo in one transaction (either both happen or neither)."""
    with db.conn() as c:
        return submit_reading_tx(c, user, body)


def submit_reading_tx(c, user, body):
    from .capture import ingest_records
    meter = db.row(c, "SELECT * FROM meter WHERE meter_id::text = %s", (body.meter_id,))
    if not meter:
        raise HTTPException(404, "Unknown meter")
    cap = None
    if body.capture_id:
        cap = db.row(c, "SELECT * FROM evidence_capture WHERE capture_id::text = %s", (body.capture_id,))
        if not cap or not _owner_or_reviewer(user, cap):
            raise HTTPException(404, "Capture not found")
        if cap["meter_id"] and cap["meter_id"] != meter["meter_id"]:
            raise HTTPException(409, "The photo was taken for a different meter")
    if body.read_at:
        read_at = body.read_at if body.read_at.tzinfo else body.read_at.replace(tzinfo=LOCAL_TZ)
    else:
        read_at = (cap and (cap["exif_taken_at"] or cap["client_captured_at"])) or datetime.now(timezone.utc)
    if read_at > datetime.now(timezone.utc) + timedelta(minutes=CLOCK_SKEW_MIN):
        raise HTTPException(422, "Reading time is in the future")
    rec = {"meter_id": body.meter_id, "read_at": read_at.isoformat(), "register_value": body.register_value,
           "uom": meter["uom"], "rollover": body.rollover}
    if body.note:
        rec["note"] = body.note
    if cap:
        rec["evidence_doc_id"] = str(cap["doc_id"])
    if body.rollover and not meter["register_digits"]:
        raise HTTPException(422, "Rollover needs the meter's register_digits to be configured")
    res = ingest_records(c, user, "meter_register_reading", [rec], "FIELD_APP")
    if res["errors"]:
        raise HTTPException(422, res["errors"][0]["error"])
    if not res["inserted"]:
        raise HTTPException(409, "A reading for this meter at that time already exists")
    rid = res["ids"][0]
    out = {"register_reading_id": rid, "flags": []}
    if cap:
        fl = value_flags(c, meter["meter_id"], body.register_value, read_at, cap, rid)
        fl = [f for f in fl if not (body.rollover and f["code"] == "reading_decreased")]
        out["capture"] = _public(_link(c, user, body.capture_id, "meter_register_reading", rid, body.register_value, fl))
        out["flags"] = out["capture"]["flags"]
    else:
        out["flags"] = [flag("no_evidence", "warning", "Submitted without a photo.")] + \
                       sequence_flags(c, meter["meter_id"], body.register_value, read_at, rid, body.rollover)
    return out


@router.post("/{capture_id}/discard")
def discard(capture_id: str, user: dict = Depends(require(*FIELD_ROLES))):
    with db.conn() as c:
        cap = db.row(c, "SELECT * FROM evidence_capture WHERE capture_id::text = %s FOR UPDATE", (capture_id,))
        if not cap or not _owner_or_reviewer(user, cap):
            raise HTTPException(404, "Capture not found")
        if cap["status"] != "new":
            raise HTTPException(409, "Only photos not yet attached to a record can be discarded")
        db.row(c, "UPDATE evidence_capture SET status = 'discarded' WHERE capture_id = %s RETURNING 1", (cap["capture_id"],))
        db.audit(c, user["email"], "discard_evidence", "evidence_capture", cap["capture_id"])
    return {"ok": True}


# --------------------------------------------------------------------------- consumption derivation
def rederive(c, meter_id, actor):
    """Recompute consumption for every validated register reading of a meter, in time order, and keep
    energy_meter_reading in step. Raises 409 when the sequence is arithmetically impossible."""
    meter = db.row(c, "SELECT * FROM meter WHERE meter_id = %s", (meter_id,))
    chain = db.rows(c, "SELECT * FROM meter_register_reading WHERE meter_id = %s AND validation_status = 'validated' "
                       "ORDER BY read_at FOR UPDATE", (meter_id,))
    old = [r["derived_reading_id"] for r in chain if r["derived_reading_id"]]
    # a reading sent back to 'pending' keeps no derived row either
    old += [r["derived_reading_id"] for r in db.rows(
        c, "SELECT derived_reading_id FROM meter_register_reading WHERE meter_id = %s AND validation_status <> 'validated' "
           "AND derived_reading_id IS NOT NULL", (meter_id,))]
    if old:
        c.execute("DELETE FROM energy_meter_reading WHERE reading_id = ANY(%s)", (old,))
    c.execute("UPDATE meter_register_reading SET derived_reading_id = NULL, consumption_raw = NULL "
              "WHERE meter_id = %s AND (derived_reading_id IS NOT NULL OR consumption_raw IS NOT NULL)", (meter_id,))
    ss = db.row(c, "SELECT source_system_id FROM source_system WHERE code = 'FIELD_APP'")
    is_energy = (meter["uom"] or "").lower() == "kwh"
    prev = None
    for r in chain:
        if prev is not None:
            delta = r["register_value"] - prev["register_value"]
            if delta < 0:
                if not r["rollover"]:
                    raise HTTPException(409, f"Reading {r['register_value']:g} on {r['read_at']:%Y-%m-%d %H:%M} is lower than the "
                                             f"previous validated reading {prev['register_value']:g}. Mark it as rollover or reject it.")
                if not meter["register_digits"]:
                    raise HTTPException(409, "Rollover needs the meter's register_digits to be configured")
                delta += Decimal(10) ** meter["register_digits"]
            derived = None
            if is_energy:
                row = db.row(c, "INSERT INTO energy_meter_reading (meter_id, ts, energy_kwh, energy_type, data_quality, source_system_id) "
                                "VALUES (%s,%s,%s,%s,'manual',%s) ON CONFLICT (meter_id, ts, energy_type) DO NOTHING RETURNING reading_id",
                             (meter_id, r["read_at"], delta * meter["multiplier"], meter["energy_type"],
                              ss["source_system_id"] if ss else None))
                if not row:
                    raise HTTPException(409, "An interval reading already exists for this meter at that time")
                derived = row["reading_id"]
            c.execute("UPDATE meter_register_reading SET consumption_raw = %s, derived_reading_id = %s WHERE register_reading_id = %s",
                      (delta, derived, r["register_reading_id"]))
        prev = r
    db.audit(c, actor, "derive_consumption", "meter", meter_id, detail=f"{len(chain)} validated readings")


def before_decision(c, table, rec, actor):
    """Called inside the approval transaction after the status change; may veto by raising."""
    if table == "meter_register_reading":
        rederive(c, rec["meter_id"], actor)


def after_correction(c, table, before, actor):
    """A validated reading edited back to pending must stop contributing to the inventory."""
    if table == "meter_register_reading" and before["validation_status"] == "validated":
        rederive(c, before["meter_id"], actor)


def evidence_summary(c, table, ids):
    """Per-record evidence for the approvals queue: {record_id: [capture summaries]}."""
    if not ids:
        return {}
    out = {}
    for r in db.rows(c, "SELECT capture_id, target_record_id, ocr_confidence, candidates, entered_value, exif_taken_at, "
                        "client_captured_at, flags FROM evidence_capture WHERE target_table = %s AND target_record_id = ANY(%s)",
                     (table, ids)):
        out.setdefault(r["target_record_id"], []).append(r)
    return out
