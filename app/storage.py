"""Pluggable evidence storage.

STORAGE_BACKEND=local (default)  files under EVIDENCE_DIR
STORAGE_BACKEND=azure            Azure Blob Storage

Azure settings (all environment variables, so moving to another subscription is a config change):
  AZURE_STORAGE_ACCOUNT_URL   e.g. https://myaccount.blob.core.windows.net   (uses managed identity / az login)
  AZURE_STORAGE_CONNECTION_STRING   alternative for local trials (Azurite or a key-based account)
  AZURE_STORAGE_CONTAINER     default "evidence"

evidence_document.file_uri holds either a local path (older rows) or az://<container>/<blob>.
Both keep working side by side, so switching backend never orphans existing evidence.
"""
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", "./data/evidence"))
_client = None


def backend() -> str:
    return os.environ.get("STORAGE_BACKEND", "local").lower()


def _container():
    global _client
    if _client is None:
        from azure.storage.blob import BlobServiceClient
        cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if cs:
            svc = BlobServiceClient.from_connection_string(cs)
        else:
            from azure.identity import DefaultAzureCredential
            svc = BlobServiceClient(os.environ["AZURE_STORAGE_ACCOUNT_URL"], credential=DefaultAzureCredential())
        _client = svc.get_container_client(os.environ.get("AZURE_STORAGE_CONTAINER", "evidence"))
    return _client


def save(name: str, data: bytes, content_type: str | None = None) -> str:
    """Store the bytes and return the URI to keep in evidence_document.file_uri."""
    if backend() == "azure":
        from azure.storage.blob import ContentSettings
        cc = _container()
        cc.upload_blob(name, data, overwrite=True,
                       content_settings=ContentSettings(content_type=content_type or "application/octet-stream"))
        return f"az://{cc.container_name}/{name}"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    p = EVIDENCE_DIR / name
    p.write_bytes(data)
    return str(p)


def read(uri: str) -> bytes:
    if uri.startswith("az://"):
        blob = uri.split("/", 3)[3]
        return _container().download_blob(blob).readall()
    return Path(uri).read_bytes()


def respond(uri: str, media_type: str | None = None, headers: dict | None = None):
    """HTTP response for a stored file, whichever backend holds it."""
    if uri.startswith("az://"):
        try:
            return Response(read(uri), media_type=media_type or "application/octet-stream", headers=headers)
        except Exception:
            raise HTTPException(404, "File not found in storage")
    if not Path(uri).exists():
        raise HTTPException(404, "File not found in storage")
    return FileResponse(uri, media_type=media_type, headers=headers)
