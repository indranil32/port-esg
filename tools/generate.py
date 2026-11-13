import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from model import TABLES, DOMAINS, COMMON, LEAN

OUT = os.path.join(os.path.dirname(__file__), "..", "build")
SCHEMA = "esg"
SQL_T = {"text": "text", "int": "integer", "bigint": "bigint", "num": "numeric(18,4)",
         "geo": "numeric(9,6)", "bool": "boolean", "ts": "timestamptz", "date": "date",
         "json": "jsonb", "uuid": "uuid", "text[]": "text[]"}
MM_T = {"text": "string", "int": "int", "bigint": "int", "num": "float", "geo": "float",
        "bool": "boolean", "ts": "datetime", "date": "date", "json": "json",
        "uuid": "string", "text[]": "string"}


def parse_cols(t):
    block = t["cols"].strip()
    if t["common"]:
        block += "\n" + COMMON.format(dq=t["common"])
    if t["lean"]:
        block += "\n" + LEAN
    cols = []
    for line in block.splitlines():
        tok = line.split()
        if not tok:
            continue
        c = dict(name=tok[0], type=tok[1], pk=False, nn=False, uq=False, nonneg=False,
                 identity=False, fk=None, enum=None, default=None, rng=None)
        for o in tok[2:]:
            if o == "pk": c["pk"] = True
            elif o == "nn": c["nn"] = True
            elif o == "uq": c["uq"] = True
            elif o == "nonneg": c["nonneg"] = True
            elif o == "identity": c["identity"] = True
            elif o.startswith("fk="): c["fk"] = tuple(o[3:].split("."))
            elif o.startswith("in="): c["enum"] = o[3:].split("|")
            elif o.startswith("def="): c["default"] = o[4:]
            elif o.startswith("range="): c["rng"] = tuple(float(x) for x in o[6:].split(","))
            else: raise ValueError(f"bad opt {o} in {t['name']}.{tok[0]}")
        if c["pk"]:
            c["nn"] = True
        cols.append(c)
    t["_cols"] = cols
    return cols


for t in TABLES:
    parse_cols(t)
BY = {t["name"]: t for t in TABLES}

# ---- sanity: FK targets exist, column types match, ordering is creatable
seen = set()
for t in TABLES:
    for c in t["_cols"]:
        if c["fk"]:
            tt, tc = c["fk"]
            assert tt in BY, (t["name"], c["name"], tt)
            tgt = next((x for x in BY[tt]["_cols"] if x["name"] == tc), None)
            assert tgt, (t["name"], c["name"], tc)
            assert tgt["type"] == c["type"], (t["name"], c["name"], c["type"], tgt["type"])
            assert tt in seen or tt == t["name"], f"order: {t['name']} before {tt}"
    seen.add(t["name"])


def fmt_num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


# ============================================================== SQL DDL
def ddl():
    L = ["-- Port ESG platform: PostgreSQL 13+ DDL (generated from model.py)",
         f"-- {len(TABLES)} tables in schema \"{SCHEMA}\"",
         "-- High-volume tables (energy_meter_reading, ais_position, *_reading) should be",
         "-- partitioned by month or moved to a time-series extension in production.", "",
         f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};", ""]
    cur = None
    for t in TABLES:
        if t["dom"] != cur:
            cur = t["dom"]
            lab = dict(DOMAINS)[cur]
            L += ["-- " + "=" * 70, f"-- {lab}", "-- " + "=" * 70, ""]
        pks = [c for c in t["_cols"] if c["pk"]]
        composite = len(pks) > 1
        lines = []
        for c in t["_cols"]:
            s = f"    {c['name']} {SQL_T[c['type']]}"
            if c["identity"]:
                s += " GENERATED ALWAYS AS IDENTITY"
            if c["pk"] and not composite:
                s += " PRIMARY KEY"
                if c["type"] == "uuid" and not c["default"]:
                    s += " DEFAULT gen_random_uuid()"
            elif c["nn"]:
                s += " NOT NULL"
            if c["default"] is not None:
                s += f" DEFAULT {c['default']}"
            if c["uq"]:
                s += " UNIQUE"
            if c["enum"]:
                vals = ", ".join(f"'{v}'" for v in c["enum"])
                s += f" CHECK ({c['name']} IN ({vals}))"
            if c["rng"]:
                s += f" CHECK ({c['name']} BETWEEN {fmt_num(c['rng'][0])} AND {fmt_num(c['rng'][1])})"
            if c["nonneg"]:
                s += f" CHECK ({c['name']} >= 0)"
            lines.append(s)
        if composite:
            lines.append(f"    PRIMARY KEY ({', '.join(c['name'] for c in pks)})")
        for c in t["_cols"]:
            if c["fk"]:
                lines.append(f"    FOREIGN KEY ({c['name']}) REFERENCES {SCHEMA}.{c['fk'][0]}({c['fk'][1]})")
        if t["common"]:
            lines.append("    UNIQUE (source_system_id, source_record_id, version)")
        for k in t["cons"]:
            lines.append("    " + k)
        L.append(f"CREATE TABLE {SCHEMA}.{t['name']} (")
        L.append(",\n".join(lines))
        L.append(");")
        desc = t["desc"].replace("'", "''")
        L.append(f"COMMENT ON TABLE {SCHEMA}.{t['name']} IS '{desc}';")
        # indexes: every FK column not already leading a PK/unique, plus explicit ones
        done = set()
        for c in t["_cols"]:
            if c["fk"] and not (c["pk"] and pks[0] is c):
                done.add((c["name"],))
        for cols in t["idx"]:
            done.add(tuple(cols))
        for cols in sorted(done):
            L.append(f"CREATE INDEX ix_{t['name']}_{'_'.join(cols)} ON {SCHEMA}.{t['name']} ({', '.join(cols)});")
        L.append("")
    # convenience view
    L += ["-- " + "=" * 70, "-- Convenience view: Scope 1/2/3 totals by entity and period", "-- " + "=" * 70, "",
          f"""CREATE VIEW {SCHEMA}.v_ghg_by_scope AS
SELECT a.run_id, a.entity_id, a.scope, a.scope3_category, a.period_start, a.period_end,
       SUM(e.co2e_tonnes) AS co2e_tonnes
FROM {SCHEMA}.ghg_activity_data a
JOIN {SCHEMA}.ghg_emission e ON e.activity_id = a.activity_id
GROUP BY a.run_id, a.entity_id, a.scope, a.scope3_category, a.period_start, a.period_end;
""", ""]
    return "\n".join(L)


# ============================================================== Mermaid
def mm_entity(t, full=True):
    if not full:
        c = next(x for x in t["_cols"] if x["pk"])
        return f"    {t['name']} {{\n        {MM_T[c['type']]} {c['name']} PK\n    }}"
    out = [f"    {t['name']} {{"]
    for c in t["_cols"]:
        keys = []
        if c["pk"]: keys.append("PK")
        if c["fk"]: keys.append("FK")
        if c["uq"] and not c["pk"]: keys.append("UK")
        out.append(f"        {MM_T[c['type']]} {c['name']} {', '.join(keys)}".rstrip())
    out.append("    }")
    return "\n".join(out)


def mm_rels(tables, only_names=None):
    rels = []
    for t in tables:
        for c in t["_cols"]:
            if not c["fk"]:
                continue
            p = c["fk"][0]
            if c["name"] == "source_system_id":
                continue
            if p == t["name"]:
                lhs = "|o--o{"  # self reference
            elif c["nn"]:
                lhs = "||--o{"
            else:
                lhs = "|o--o{"
            rels.append(f'    {p} {lhs} {t["name"]} : "{c["name"]}"')
    return rels


def erd():
    md = ["# Port ESG data model: entity relationship diagrams", "",
          f"{len(TABLES)} tables, split into {len(DOMAINS)} domain diagrams plus a relationship overview. "
          "Generated from the same definition as `schema.sql` and the JSON Schemas.", "",
          "Legend: `PK` primary key, `FK` foreign key, `UK` unique. A dotted-circle end (`|o`) means the foreign key is optional.", "",
          "Every fact table in classes A, D, E and G also carries the common metadata columns "
          "(`source_system_id`, `source_record_id`, `ingested_at`, `data_quality`, `validation_status`, `version`, `created_by`, `approved_by`). "
          "To keep the diagrams readable they are omitted here (including the link to `source_system`); see `schema.sql` for the full definitions.", "",
          "## Overview (relationships only)", "", "```mermaid", "erDiagram"]
    md += mm_rels(TABLES)
    md += ["```", ""]
    shown_meta = False
    for key, label in DOMAINS:
        ts = [t for t in TABLES if t["dom"] == key]
        names = {t["name"] for t in ts}
        stubs = []
        for t in ts:
            for c in t["_cols"]:
                if c["fk"] and c["name"] != "source_system_id" and c["fk"][0] not in names and c["fk"][0] not in stubs:
                    stubs.append(c["fk"][0])
        md += [f"## {label}", "", "```mermaid", "erDiagram"]
        for t in ts:
            tt = dict(t)
            if t["common"] or t["lean"]:
                meta = {"source_system_id", "source_record_id", "ingested_at", "data_quality",
                        "validation_status", "version", "created_by", "approved_by"}
                tt["_cols"] = [c for c in t["_cols"] if c["name"] not in meta]
            md.append(mm_entity(tt))
        for s in stubs:
            md.append(mm_entity(BY[s], full=False))
        md += mm_rels(ts)
        md += ["```", ""]
    return "\n".join(md)


# ============================================================== JSON Schema
def col_schema(c):
    ty = c["type"]
    s = {}
    if ty in ("text",): s["type"] = "string"
    elif ty in ("int", "bigint"): s["type"] = "integer"
    elif ty in ("num", "geo"): s["type"] = "number"
    elif ty == "bool": s["type"] = "boolean"
    elif ty == "ts": s.update(type="string", format="date-time")
    elif ty == "date": s.update(type="string", format="date")
    elif ty == "uuid": s.update(type="string", format="uuid")
    elif ty == "json": s["type"] = "object"
    elif ty == "text[]": s.update(type="array", items={"type": "string"})
    if c["enum"]:
        s["enum"] = list(c["enum"])
    if c["rng"]:
        lo, hi = c["rng"]
        s["minimum"] = int(lo) if lo.is_integer() else lo
        s["maximum"] = int(hi) if hi.is_integer() else hi
    elif c["nonneg"]:
        s["minimum"] = 0
    if c["fk"]:
        s["description"] = f"FK to {c['fk'][0]}.{c['fk'][1]}"
    if not c["nn"]:
        s["type"] = [s["type"], "null"]
        if "enum" in s:
            s["enum"] = s["enum"] + [None]
    return s


def table_schema(t, with_id=True):
    props, req = {}, []
    for c in t["_cols"]:
        props[c["name"]] = col_schema(c)
        server_made = c["identity"] or (c["pk"] and c["type"] == "uuid") or c["default"] is not None \
            or c["name"] == "ingested_at"
        if c["nn"] and not server_made:
            req.append(c["name"])
    s = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if with_id:
        s["$id"] = f"https://example.org/esg/schemas/{t['name']}.schema.json"
    s.update(title=t["name"], description=t["desc"], type="object", properties=props,
             required=req, additionalProperties=False)
    return s


def bundle():
    defs = {}
    for t in TABLES:
        d = table_schema(t, with_id=False)
        d.pop("$schema")
        defs[t["name"]] = d
    envelope = {
        "type": "object",
        "description": "Ingestion batch envelope: one table, many records.",
        "properties": {
            "source_system_code": {"type": "string"},
            "batch_id": {"type": "string"},
            "sent_at": {"type": "string", "format": "date-time"},
            "table": {"enum": [t["name"] for t in TABLES]},
            "records": {"type": "array", "minItems": 1},
        },
        "required": ["source_system_code", "batch_id", "table", "records"],
        "additionalProperties": False,
        "allOf": [{"if": {"properties": {"table": {"const": t["name"]}}, "required": ["table"]},
                   "then": {"properties": {"records": {"items": {"$ref": f"#/$defs/{t['name']}"}}}}}
                  for t in TABLES],
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.org/esg/schemas/esg.bundle.schema.json",
            "title": "Port ESG data model: JSON Schema bundle",
            "description": "All entity schemas under $defs, plus an ingestion envelope at the root.",
            **envelope, "$defs": defs}


if __name__ == "__main__":
    os.makedirs(f"{OUT}/json_schemas", exist_ok=True)
    open(f"{OUT}/schema.sql", "w").write(ddl())
    open(f"{OUT}/erd.md", "w").write(erd())
    for t in TABLES:
        json.dump(table_schema(t), open(f"{OUT}/json_schemas/{t['name']}.schema.json", "w"), indent=2)
    json.dump(bundle(), open(f"{OUT}/esg_schemas.json", "w"), indent=2)
    print(len(TABLES), "tables generated")
