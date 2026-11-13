"""Spreadsheet-friendly loading: code lookups for CSV and API rows (terminal code instead of a UUID), a downloadable
template pack with a data dictionary and loadable example rows, and CSV import for the metric library, framework
mappings and the legal register."""
import csv
import io
import zipfile
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import db, files
from .auth import current_user, require

# uuid column -> (alias header, referenced table, its natural key column)
FK_ALIASES = {
    "entity_id": ("entity_code", "entity", "code"), "terminal_id": ("terminal_code", "entity", "code"),
    "parent_entity_id": ("parent_entity_code", "entity", "code"),
    "asset_id": ("asset_code", "asset", "asset_code"), "equipment_id": ("equipment_code", "asset", "asset_code"),
    "meter_id": ("meter_code", "meter", "meter_code"), "sensor_id": ("sensor_code", "sensor", "sensor_code"),
    "berth_id": ("berth_code", "berth", "berth_code"), "tenant_id": ("tenant_name", "tenant", "name"),
    "haulier_tenant_id": ("haulier_tenant_name", "tenant", "name"), "metric_id": ("metric_code", "metric", "metric_code"),
}
HIDE = {"ingested_at", "validation_status", "approved_by", "created_by", "source_system_id", "version", "reviewer", "review_date"}


def aliases_for(props):
    """alias header -> uuid column, for the uuid columns this table really has."""
    return {a: col for col, (a, _, _) in FK_ALIASES.items() if col in props and a not in props}


def resolve_codes(c, table_props, rec, cache):
    """Replace alias columns (entity_code ...) by the uuid they point at. Returns an error string or None."""
    for alias, col in aliases_for(table_props).items():
        if alias not in rec:
            continue
        val = str(rec.pop(alias)).strip()
        if not val:
            continue
        if rec.get(col):
            return f"give either {col} or {alias}, not both"
        _, ref, nat = FK_ALIASES[col]
        key = (ref, nat, val)
        if key not in cache:
            cache[key] = [r[f"{ref}_id"] for r in db.rows(c, f"SELECT {ref}_id FROM esg.{ref} WHERE {nat} = %s LIMIT 2", (val,))]
        ids = cache[key]
        if not ids:
            return f"{alias} '{val}' not found"
        if len(ids) > 1:
            return f"{alias} '{val}' is ambiguous; use {col}"
        rec[col] = str(ids[0])
    return None


# ------------------------------------------------------------------ template pack
def columns_for(c, table, props):
    from .capture import pk_cols
    pks = set(pk_cols(c, table))
    cols = []
    for name, p in props.items():
        if name in HIDE or (name in pks and p.get("format") == "uuid") or (name in pks and name.endswith("_id")):
            continue
        if name in FK_ALIASES and FK_ALIASES[name][0] not in props:
            cols.append((FK_ALIASES[name][0], name, p))
        elif p.get("format") == "uuid" and name.endswith("_id"):
            continue                     # no friendly lookup for this reference; leave it out of the template
        else:
            cols.append((name, name, p))
    return cols


def sample_value(c, header, col, p, cache):
    if header in {a for a, _, _ in FK_ALIASES.values()}:
        _, ref, nat = next(v for v in FK_ALIASES.values() if v[0] == header)
        if (ref, nat) not in cache:
            r = db.row(c, f"SELECT {nat} AS v FROM esg.{ref} ORDER BY 1 LIMIT 1")
            cache[(ref, nat)] = r["v"] if r else f"<{nat}>"
        return cache[(ref, nat)]
    if p.get("enum"):
        return [e for e in p["enum"] if e][0]
    t = p.get("type"); t = [x for x in t if x != "null"][0] if isinstance(t, list) else t
    f = p.get("format")
    if f == "date" or "date" in col:
        return date.today().isoformat()
    if f == "date-time" or col in ("ts", "atb", "atd", "read_at", "occurred_at"):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {"integer": 1, "number": 1, "boolean": "false", "array": "a|b"}.get(t, "example")


def csv_text(rows):
    b = io.StringIO()
    csv.writer(b).writerows(rows)
    return b.getvalue()


def dictionary(c, tables, schemas):
    out = [["table", "csv_header", "required", "type", "allowed_values", "description"]]
    for t in tables:
        d = schemas[t]
        for header, col, p in columns_for(c, t, d["properties"]):
            ty = p.get("type"); ty = "/".join(x for x in ty if x != "null") if isinstance(ty, list) else ty
            desc = p.get("description", "")
            if header != col:
                desc = f"Code of the {FK_ALIASES[col][1]} (looked up for you). " + desc
            out.append([t, header, "yes" if col in d.get("required", []) else "no", ty, " | ".join(map(str, p.get("enum") or [])), desc])
    return out


router = APIRouter(prefix="/api/templates")


def allowed_tables(user):
    from .capture import INGESTIBLE, can_write
    return sorted(t for t in INGESTIBLE if can_write(user, t))


@router.get("/{table}.csv")
def one_template(table: str, example: bool = False, user: dict = Depends(current_user)):
    from .capture import SCHEMAS
    if table not in allowed_tables(user):
        raise HTTPException(404, "No such table, or your role cannot load it")
    with db.conn() as c:
        cols = columns_for(c, table, SCHEMAS[table]["properties"])
        rows = [[h for h, _, _ in cols]]
        if example:
            cache = {}
            rows.append([sample_value(c, h, col, p, cache) for h, col, p in cols])
    return Response(csv_text(rows), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{table}{"_example" if example else ""}.csv"'})


@router.get(".zip")
def pack(user: dict = Depends(current_user)):
    from .capture import GROUP_OF, SCHEMAS
    tables = allowed_tables(user)
    buf = io.BytesIO()
    with db.conn() as c, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tables:
            cols = columns_for(c, t, SCHEMAS[t]["properties"])
            cache = {}
            z.writestr(f"templates/{t}.csv", csv_text([[h for h, _, _ in cols]]))
            z.writestr(f"examples/{t}_example.csv", csv_text([[h for h, _, _ in cols], [sample_value(c, h, col, p, cache) for h, col, p in cols]]))
        z.writestr("data_dictionary.csv", csv_text(dictionary(c, tables, SCHEMAS)))
        z.writestr("table_index.csv", csv_text([["table", "area"]] + [[t, GROUP_OF[t]] for t in tables]))
        z.writestr("README.txt",
                   "Fill one CSV per table, keep the header row, save as UTF-8 CSV and upload it under Data capture > CSV upload.\n"
                   "Columns ending in _code or _name take the code or name shown in the system; no internal IDs are needed.\n"
                   "Dates are YYYY-MM-DD, timestamps are ISO 8601 (2026-11-13T08:00:00+04:00), lists use | between values.\n"
                   "See data_dictionary.csv for every column, whether it is required and its allowed values.\n"
                   "The examples folder holds one loadable example row per table (it uses codes that exist in this system).\n"
                   "Records land as pending and need approval by a second person.\n")
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="esg_csv_template_pack.zip"'})


# ------------------------------------------------------------------ reference-data imports
imp = APIRouter(prefix="/api/import")
ADMIN = require("esg_admin")


async def read_csv(file: UploadFile, required, limit=5000, user=None, target=""):
    data = await file.read()
    if user is not None:                       # keep the original file for audit, even if it is then rejected
        with db.conn() as c:
            files.archive(c, user, "reference_import", target, file.filename, data, file.content_type)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 CSV (save Excel sheets as CSV UTF-8)")
    rd = csv.DictReader(io.StringIO(text))
    missing = [h for h in required if h not in (rd.fieldnames or [])]
    if missing:
        raise HTTPException(400, f"Missing columns: {', '.join(missing)}")
    rows = list(rd)
    if not rows or len(rows) > limit:
        raise HTTPException(400, f"CSV must contain between 1 and {limit} rows")
    return rows


def pipe(s):
    return [x.strip() for x in (s or "").split("|") if x.strip()]


@imp.post("/metrics")
async def import_metrics(file: UploadFile = File(...), user: dict = Depends(ADMIN)):
    rows = await read_csv(file, ["metric_code", "name", "pillar"], user=user, target="metric")
    ins = upd = 0
    errors = []
    with db.conn() as c:
        for i, r in enumerate(rows):
            if r["pillar"] not in ("E", "S", "G") or not r["metric_code"].strip() or not r["name"].strip():
                errors.append({"row": i + 2, "error": "metric_code and name are required and pillar must be E, S or G"})
                continue
            freq = (r.get("frequency") or "").strip() or None
            if freq not in (None, "realtime", "daily", "monthly", "quarterly", "annual"):
                errors.append({"row": i + 2, "error": "frequency must be realtime, daily, monthly, quarterly or annual"})
                continue
            res = db.row(c, """INSERT INTO metric (metric_code, name, pillar, unit, owner, frequency, source_classes, formula_ref)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (metric_code) DO UPDATE SET name = EXCLUDED.name, pillar = EXCLUDED.pillar, unit = EXCLUDED.unit,
                                 owner = EXCLUDED.owner, frequency = EXCLUDED.frequency, source_classes = EXCLUDED.source_classes,
                                 formula_ref = EXCLUDED.formula_ref RETURNING (xmax = 0) AS inserted""",
                         (r["metric_code"].strip(), r["name"].strip(), r["pillar"], r.get("unit") or None, r.get("owner") or None, freq,
                          pipe(r.get("source_classes")), r.get("formula_ref") or None))
            ins, upd = ins + bool(res["inserted"]), upd + (not res["inserted"])
        db.audit(c, user["email"], "import", "metric", None, detail=f"{ins} added, {upd} updated from {file.filename}")
    return {"added": ins, "updated": upd, "errors": errors}


@imp.post("/framework-map")
async def import_framework_map(file: UploadFile = File(...), user: dict = Depends(ADMIN)):
    rows = await read_csv(file, ["framework_code", "disclosure_code"], user=user, target="framework_map")
    reqs = maps = 0
    errors = []
    with db.conn() as c:
        for i, r in enumerate(rows):
            fw = r["framework_code"].strip()
            if not fw or not r["disclosure_code"].strip():
                errors.append({"row": i + 2, "error": "framework_code and disclosure_code are required"})
                continue
            db.row(c, "INSERT INTO framework (code, name, version_label) VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING RETURNING 1",
                   (fw, r.get("framework_name") or fw, r.get("version_label") or None))
            req = db.row(c, """INSERT INTO framework_requirement (framework_id, disclosure_code, title)
                               SELECT framework_id, %s, %s FROM framework WHERE code = %s
                               ON CONFLICT (framework_id, disclosure_code) DO UPDATE SET title = coalesce(EXCLUDED.title, framework_requirement.title)
                               RETURNING requirement_id""", (r["disclosure_code"].strip(), r.get("title") or None, fw))
            reqs += 1
            for m in pipe(r.get("metric_codes")):
                ok = db.row(c, """INSERT INTO metric_requirement_map (metric_id, requirement_id) SELECT metric_id, %s FROM metric WHERE metric_code = %s
                                  ON CONFLICT DO NOTHING RETURNING 1""", (req["requirement_id"], m))
                if ok:
                    maps += 1
                elif not db.row(c, "SELECT 1 FROM metric WHERE metric_code = %s", (m,)):
                    errors.append({"row": i + 2, "error": f"metric '{m}' not found; import the metric first"})
        db.audit(c, user["email"], "import", "framework_requirement", None, detail=f"{reqs} disclosures, {maps} new mappings from {file.filename}")
    return {"disclosures": reqs, "new_mappings": maps, "errors": errors}


@imp.post("/legal")
async def import_legal(file: UploadFile = File(...), user: dict = Depends(require("esg_admin", "data_owner"))):
    rows = await read_csv(file, ["ref", "title", "kind"], user=user, target="legal_requirement")
    ins = upd = 0
    errors = []
    with db.conn() as c:
        cache = {}
        for i, r in enumerate(rows):
            rec = dict(r)
            err = resolve_codes(c, {"entity_id": {}}, rec, cache) if rec.get("entity_code") else None
            if err or r["kind"] not in ("law", "permit", "standard", "customer", "other") or not r["ref"].strip() or not r["title"].strip():
                errors.append({"row": i + 2, "error": err or "ref and title are required and kind must be law, permit, standard, customer or other"})
                continue
            try:
                with c.transaction():
                    res = db.row(c, """INSERT INTO legal_requirement (ref, title, kind, authority, entity_id, obligation, owner, permit_expiry, review_due, created_by)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                       ON CONFLICT (ref) DO UPDATE SET title = EXCLUDED.title, kind = EXCLUDED.kind, authority = EXCLUDED.authority,
                                         entity_id = EXCLUDED.entity_id, obligation = EXCLUDED.obligation, owner = EXCLUDED.owner,
                                         permit_expiry = EXCLUDED.permit_expiry, review_due = EXCLUDED.review_due RETURNING (xmax = 0) AS inserted""",
                                 (r["ref"].strip(), r["title"].strip(), r["kind"], r.get("authority") or None, rec.get("entity_id"), r.get("obligation") or None,
                                  r.get("owner") or None, r.get("permit_expiry") or None, r.get("review_due") or None, user["email"]))
                    ins, upd = ins + bool(res["inserted"]), upd + (not res["inserted"])
            except Exception as e:
                errors.append({"row": i + 2, "error": str(e).splitlines()[0][:160]})
        db.audit(c, user["email"], "import", "legal_requirement", None, detail=f"{ins} added, {upd} updated from {file.filename}")
    return {"added": ins, "updated": upd, "errors": errors}
