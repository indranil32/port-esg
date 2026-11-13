"""OCR providers behind one interface, plus meter-reading extraction.

Provider is chosen by OCR_PROVIDER = auto | rapidocr | tesseract | azure | none.
  auto      azure if AZURE_DI_ENDPOINT and AZURE_DI_KEY are set, else rapidocr, else tesseract, else none
  rapidocr  local ONNX models (pip install rapidocr-onnxruntime); no data leaves the server
  tesseract local tesseract binary
  azure     Azure AI Document Intelligence prebuilt-read (REST, api-version 2024-11-30)
  none      no OCR: the operator keys the value in and the photo is kept as evidence only

Every provider returns {"provider", "ms", "lines": [{"text", "confidence"}], "text", "confidence"}.
Confidence is 0..1. A provider failure never blocks a capture: it returns lines=[] with an "error".
"""
import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image, ImageOps

AZURE_API_VERSION = os.environ.get("AZURE_DI_API_VERSION", "2024-11-30")
_rapid = None


def _normalised(data: bytes) -> Image.Image:
    """Decode, apply EXIF orientation (phones store portrait shots rotated), drop alpha."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    return img.convert("RGB")


def available_providers() -> list[str]:
    out = []
    if os.environ.get("AZURE_DI_ENDPOINT") and os.environ.get("AZURE_DI_KEY"):
        out.append("azure")
    try:
        import rapidocr_onnxruntime  # noqa: F401
        out.append("rapidocr")
    except Exception:
        pass
    if shutil.which("tesseract"):
        out.append("tesseract")
    return out


def provider_name() -> str:
    want = os.environ.get("OCR_PROVIDER", "auto").lower()
    avail = available_providers()
    if want == "auto":
        return avail[0] if avail else "none"
    if want == "none":
        return "none"
    return want if want in avail else "none"


# ----------------------------------------------------------------------------- providers
def _run_rapidocr(img: Image.Image):
    global _rapid
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    result, _ = _rapid(np.array(img))
    return [{"text": t.strip(), "confidence": round(float(s), 4)} for _, t, s in (result or []) if t.strip()]


def _run_tesseract(img: Image.Image):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "in.png")
        img.save(p)
        out = subprocess.run(["tesseract", p, "stdout", "--psm", "6", "tsv"], capture_output=True,
                             text=True, timeout=60, check=True).stdout
    lines = {}
    for row in out.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not f[11].strip() or float(f[10]) < 0:
            continue
        key = (f[2], f[3], f[4])
        lines.setdefault(key, []).append((f[11].strip(), float(f[10]) / 100))
    return [{"text": " ".join(w for w, _ in ws), "confidence": round(sum(c for _, c in ws) / len(ws), 4)}
            for ws in lines.values()]


def _run_azure(data: bytes):
    import httpx
    endpoint = os.environ["AZURE_DI_ENDPOINT"].rstrip("/")
    key = os.environ["AZURE_DI_KEY"]
    url = f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze?api-version={AZURE_API_VERSION}"
    hdr = {"Ocp-Apim-Subscription-Key": key}
    with httpx.Client(timeout=30) as h:
        r = h.post(url, headers=hdr, json={"base64Source": base64.b64encode(data).decode()})
        r.raise_for_status()                              # 202 Accepted
        op = r.headers["Operation-Location"]
        for _ in range(30):                               # poll at >= 1 s as the service recommends
            time.sleep(1)
            g = h.get(op, headers=hdr)
            g.raise_for_status()
            body = g.json()
            if body.get("status") == "succeeded":
                break
            if body.get("status") == "failed":
                raise RuntimeError(str(body.get("error")))
        else:
            raise TimeoutError("Azure Document Intelligence did not finish in 30 s")
    lines = []
    for page in body.get("analyzeResult", {}).get("pages", []):
        words = page.get("words", [])
        for ln in page.get("lines", []):
            # a line's confidence is the mean of the words whose spans fall inside it
            lo = ln["spans"][0]["offset"]
            hi = lo + ln["spans"][0]["length"]
            cs = [w["confidence"] for w in words if lo <= w["span"]["offset"] < hi]
            lines.append({"text": ln["content"].strip(), "confidence": round(sum(cs) / len(cs), 4) if cs else 0.0})
    return [x for x in lines if x["text"]]


def run_ocr(data: bytes) -> dict:
    prov = provider_name()
    t0 = time.time()
    lines, err = [], None
    if prov != "none":
        try:
            if prov == "azure":
                lines = _run_azure(data)
            elif prov == "rapidocr":
                lines = _run_rapidocr(_normalised(data))
            elif prov == "tesseract":
                lines = _run_tesseract(_normalised(data))
        except Exception as e:                            # never block the capture on an OCR failure
            err = f"{type(e).__name__}: {e}"[:300]
    conf = round(sum(x["confidence"] for x in lines) / len(lines), 4) if lines else None
    return {"provider": prov, "ms": int((time.time() - t0) * 1000), "lines": lines,
            "text": "\n".join(x["text"] for x in lines), "confidence": conf, "error": err}


# ----------------------------------------------------------------------------- reading extraction
_NUM = re.compile(r"(?<![\w.])\d[\d ,]*(?:\.\d+)?(?![\w])|(?<![\w.])\.\d+")
_SERIAL = re.compile(r"\b(s/?n|serial|ser\.?|no\.?|model|type|cat)\b", re.I)
_UNIT = re.compile(r"\b(kwh|mwh|m3|m³|l|litres?|gal|kg|v|a|hz)\b", re.I)


def extract_readings(lines: list[dict], digits: int | None = None) -> list[dict]:
    """Numeric candidates for a register reading, best first. Serial numbers and short labels rank last."""
    cands = []
    for ln in lines:
        text = ln["text"]
        serial = bool(_SERIAL.search(text))
        for m in _NUM.finditer(text):
            raw = m.group(0).strip()
            cleaned = raw.replace(",", "").replace(" ", "")
            try:
                val = float(cleaned)
            except ValueError:
                continue
            ndig = len(re.sub(r"\D", "", cleaned))
            score = ndig + (0.5 if "." in cleaned else 0) + ln["confidence"]
            if serial:
                score -= 10
            if digits and abs(ndig - digits) <= 1:
                score += 5
            if ndig < 3:
                score -= 3
            cands.append({"value": val, "raw": raw, "confidence": ln["confidence"], "line": text, "_s": score})
    cands.sort(key=lambda c: -c["_s"])
    seen, out = set(), []
    for c in cands:
        if c["value"] in seen:
            continue
        seen.add(c["value"])
        c.pop("_s")
        out.append(c)
    return out[:6]
