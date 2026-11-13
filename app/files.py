"""Register of original files. Every CSV that is loaded is archived byte for byte (local disk or Azure Blob,
whichever STORAGE_BACKEND says), hashed, linked to its batch and downloadable. Evidence uploads
(photos, PDFs) are listed alongside so an auditor has one place to find every source document."""
import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from . import db, storage
from .auth import require

router = APIRouter(prefix="/api/files")
MAX_BYTES = 25 * 1024 * 1024
READERS = ("esg_admin", "data_owner", "auditor")


def archive(c, user, kind, target, filename, data: bytes, content_type=None):
    """Keep the original bytes. Called before parsing so even a rejected file stays on record."""
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 25 MB")
    sha = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "upload.csv")[-80:]
    uri = storage.save(f"src_{sha[:16]}_{safe}", data, content_type or "text/csv")
    r = db.row(c, "INSERT INTO source_file (kind, target, filename, content_type, size_bytes, sha256, file_uri, uploaded_by) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING file_id",
               (kind, target, filename or safe, content_type or "text/csv", len(data), sha, uri, user["email"]))
    db.audit(c, user["email"], "file_archived", "source_file", str(r["file_id"]), detail=f"{filename} ({len(data)} bytes, sha256 {sha[:12]}) for {target}")
    return str(r["file_id"])


def link_batch(c, file_id, batch_id):
    c.execute("UPDATE source_file SET batch_id = %s WHERE file_id = %s AND batch_id IS NULL", (batch_id, file_id))


@router.get("")
def register(kind: str | None = Query(None), limit: int = Query(200, le=1000), user: dict = Depends(require(*READERS))):
    """Original files, newest first. kind: csv_ingest, reference_import or evidence."""
    with db.conn() as c:
        rows = []
        if kind in (None, "csv_ingest", "reference_import"):
            rows += db.rows(c, "SELECT file_id::text AS id, kind, target, filename, size_bytes, sha256, uploaded_by, uploaded_at, batch_id::text AS batch_id, "
                               "'/files/' || file_id || '/download' AS download FROM source_file WHERE (%s::text IS NULL OR kind = %s) "
                               "ORDER BY uploaded_at DESC LIMIT %s", (kind, kind, limit))
        if kind in (None, "evidence"):
            rows += db.rows(c, "SELECT doc_id::text AS id, 'evidence' AS kind, description AS target, regexp_replace(file_uri, '^.*[/\\\\]', '') AS filename, "
                               "NULL::bigint AS size_bytes, sha256, uploaded_by, uploaded_at, NULL::text AS batch_id, '/evidence/' || doc_id AS download "
                               "FROM evidence_document ORDER BY uploaded_at DESC LIMIT %s", (limit,))
    rows.sort(key=lambda r: r["uploaded_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:limit]


@router.get("/{file_id}/download")
def download(file_id: str, user: dict = Depends(require(*READERS))):
    with db.conn() as c:
        f = db.row(c, "SELECT file_uri, content_type, filename FROM source_file WHERE file_id = %s", (file_id,))
        if f:
            db.audit(c, user["email"], "file_downloaded", "source_file", file_id, detail=f["filename"])
    if not f:
        raise HTTPException(404, "File not found")
    name = f["filename"].replace('"', "")
    return storage.respond(f["file_uri"], f["content_type"], {"Content-Disposition": f'attachment; filename="{name}"'})
