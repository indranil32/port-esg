"""Original uploaded files are archived byte for byte, hashed, linked to their batch and downloadable."""
import hashlib
import uuid

import psycopg
import pytest

from .conftest import *  # noqa: F401,F403  (client, H fixtures)


def test_csv_ingest_archived_and_downloadable(client, H):
    bad_csv = b"not_a_column,x\n1,2\n"                       # rejected upload must still be kept
    assert client.post("/api/ingest/fuel_transaction/csv", headers=H["owner"], files={"file": ("bad.csv", bad_csv, "text/csv")}).status_code == 400
    reg = client.get("/api/files", headers=H["auditor"]).json()
    hit = [f for f in reg if f["filename"] == "bad.csv"]
    assert hit and hit[0]["sha256"] == hashlib.sha256(bad_csv).hexdigest() and hit[0]["kind"] == "csv_ingest"
    dl = client.get(hit[0]["download"].replace("/files/", "/api/files/"), headers=H["auditor"])
    assert dl.status_code == 200 and dl.content == bad_csv and "attachment" in dl.headers["content-disposition"]


def test_reference_import_archived_and_roles(client, H):
    code = "ZZ_" + uuid.uuid4().hex[:6]
    body = f"metric_code,name,pillar\n{code},Test metric,E\n".encode()
    assert client.post("/api/import/metrics", headers=H["admin"], files={"file": ("m_arch.csv", body, "text/csv")}).json()["added"] == 1
    reg = client.get("/api/files?kind=reference_import", headers=H["admin"]).json()
    f = next(x for x in reg if x["filename"] == "m_arch.csv")
    assert client.get(f"/api/files/{f['id']}/download", headers=H["admin"]).content == body
    assert client.get("/api/files", headers=H["field"]).status_code == 403
    assert client.get(f"/api/files/{f['id']}/download", headers=H["tenant"]).status_code == 403
    assert client.get(f"/api/files/{uuid.uuid4()}/download", headers=H["admin"]).status_code == 404


def test_batch_links_to_its_file_and_evidence_listed(client, H):
    ev = client.post("/api/evidence", headers=H["owner"], files={"file": ("inv.txt", b"invoice 1", "text/plain")}, data={"description": "arch test"})
    assert ev.status_code == 200
    kinds = {f["kind"] for f in client.get("/api/files", headers=H["auditor"]).json()}
    assert "evidence" in kinds
    assert all("file_id" in b for b in client.get("/api/batches", headers=H["admin"]).json())


def test_archive_is_append_only(client, H):
    import os
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        fid = c.execute("SELECT file_id FROM esg.source_file LIMIT 1").fetchone()
        assert fid, "an earlier test should have archived a file"
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("DELETE FROM esg.source_file WHERE file_id = %s", (fid[0],))
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("UPDATE esg.source_file SET sha256 = 'x' WHERE file_id = %s", (fid[0],))
