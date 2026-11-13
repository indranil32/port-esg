"""Database access: one pooled connection per request, search_path pinned to the esg schema."""
import contextlib
import json
import os

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://esg:esg@localhost:5432/esg")
_pool = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL, min_size=1, max_size=10, open=True,
            kwargs={"row_factory": dict_row, "options": "-c search_path=esg,public -c client_encoding=UTF8"})
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextlib.contextmanager
def conn():
    """Commits on normal exit, rolls back on exception."""
    with pool().connection() as c:
        yield c


def rows(c, sql, params=None):
    return c.execute(sql, params or ()).fetchall()


def row(c, sql, params=None):
    return c.execute(sql, params or ()).fetchone()


def jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def audit(c, actor, action, table=None, record_id=None, before=None, after=None, detail=None):
    c.execute(
        "INSERT INTO audit_log (actor, action, table_name, record_id, before, after, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (actor, action, table, None if record_id is None else str(record_id),
         None if before is None else Jsonb(jsonable(before)),
         None if after is None else Jsonb(jsonable(after)), detail))


_PKS = None


def primary_keys(c) -> dict:
    """table -> [pk columns], read once from the catalog."""
    global _PKS
    if _PKS is None:
        data = rows(c, """
            SELECT cl.relname AS t, a.attname AS col
            FROM pg_index i
            JOIN pg_class cl ON cl.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = cl.relnamespace
            JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = ANY (i.indkey)
            WHERE i.indisprimary AND n.nspname = 'esg'
            ORDER BY cl.relname, array_position(i.indkey::int2[], a.attnum)""")
        _PKS = {}
        for r in data:
            _PKS.setdefault(r["t"], []).append(r["col"])
    return _PKS
