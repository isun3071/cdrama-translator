"""FastAPI sidecar: POST /translate, implementing the contract (CLAUDE.md).

Per request: decode frames -> OCR all -> per-character majority vote ->
consensus/confidence gates -> dedup vs last_shipped_text -> translate -> respond.
The extension cannot tell this apart from the future in-browser wasm
implementation; only the JSON shape crosses the seam.

Run (from the sidecar/ directory):
    .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
Interactive contract docs + test UI: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load the repo-root .env (GROQ_API_KEY etc.) before the translator is built.
# Explicit path so it works regardless of the process's cwd.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import audit_log
from config import CONSENSUS_FLOOR, OCR_CONF_GATE
from contract import DisplayEvent, TranslateRequest, TranslateResponse
from dedup import is_duplicate
from ocr import OcrEngine, make_ocr
from translate import Translator, make_translator
from vote import consensus_ratio, majority_vote

log = logging.getLogger("sidecar")
app = FastAPI(title="cdrama-translator sidecar", version="0.2.0")

# Localhost personal-use tool: allow any origin so the endpoint is reachable
# from the extension background, the /docs page, or curl without fuss. (The
# extension actually calls through its background script with a host permission,
# which already sidesteps CORS; this just removes a footgun for other callers.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Warm singletons (CLAUDE.md: keep the OCR model warm across requests). Swapping
# these two lines for PaddleOcr() / GroqTranslator() is the entire OCR-swap and
# provider-swap; nothing below mentions an implementation.
ocr: OcrEngine = make_ocr()               # RapidOCR if available, else mock
translator: Translator = make_translator()  # Groq if GROQ_API_KEY set, else mock


def _decode(frame_b64: str) -> bytes:
    try:
        return base64.b64decode(frame_b64, validate=True)
    except (binascii.Error, ValueError):
        return b""


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "ocr": type(ocr).__name__,
        "translator": type(translator).__name__,
        "model": getattr(translator, "model", None),
    }


_PROVIDER = {"GroqTranslator": "groq", "MockTranslator": "mock", "OllamaTranslator": "ollama"}


def _audit_entry(req: TranslateRequest, resp: TranslateResponse, raw_reads: list, latency_ms: int) -> dict:
    # Per-stage record, so a bad output can be traced to the stage it went wrong:
    # raw per-frame OCR reads -> voted source_text -> context/continuation -> translation.
    return {
        "stage": "translate",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "video_time": req.video_time,
        "label": req.label,
        "frame_id": resp.frame_id,
        "target_lang": req.target_lang,
        "status": resp.status,
        "reads": raw_reads,                 # the 3 raw frames, pre-vote: [{text, conf}]
        "source_text": resp.source_text,    # after per-character majority vote
        "confidence": resp.confidence,      # mean of the contributing reads
        "context_lines": req.context_lines,
        "continuation": req.continuation,   # candidate-to-bridge flag (↳cont)
        "translation": resp.translation,
        "duplicate": resp.duplicate,
        "latency_ms": latency_ms,
        "ocr": type(ocr).__name__,
        "provider": _PROVIDER.get(type(translator).__name__, type(translator).__name__.lower()),
        "model": getattr(translator, "model", None),
    }


@app.post("/log")
def log_display(ev: DisplayEvent) -> dict:
    """Append a client-side display outcome for a line (joined to /translate by
    frame_id in the audit). Fire-and-forget from the extension."""
    audit_log.append({
        "stage": "display",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "frame_id": ev.frame_id,
        "video_time": ev.video_time,
        "visible_ms": ev.visible_ms,
        "outcome": ev.outcome,
        "label": ev.label,
    })
    return {"ok": True}


@app.post("/translate", response_model=TranslateResponse)
def translate(req: TranslateRequest) -> TranslateResponse:
    t0 = time.perf_counter()
    resp = TranslateResponse(frame_id=req.frame_id)
    raw_reads: list = []
    try:
        reads = ocr.read_frames([_decode(f) for f in req.frames])
        raw_reads = [{"text": r.text, "conf": round(r.confidence, 3)} for r in reads]
        texts = [r.text for r in reads if r.text]

        voted = majority_vote(texts)
        resp.source_text = voted

        # Silent box, or the reads disagree too much to trust (§4).
        if not voted or (len(texts) >= 2 and consensus_ratio(texts) < CONSENSUS_FLOOR):
            resp.status = "no_text"
            return resp

        confs = [r.confidence for r in reads if r.text]
        resp.confidence = round(sum(confs) / len(confs), 3) if confs else 0.0
        if resp.confidence < OCR_CONF_GATE:
            resp.status = "low_confidence"
            return resp

        dup, _sim = is_duplicate(voted, req.last_shipped_text)
        if dup:
            # Same line still on screen — never re-translate it (§5).
            resp.status = "duplicate"
            resp.duplicate = True
            return resp

        resp.translation = translator.translate(
            voted, req.source_lang, req.target_lang, req.context_lines, req.continuation
        )
        resp.status = "ok"
        return resp
    except Exception:
        # Real OCR/providers can throw; the extension must always get valid
        # contract JSON, never a 500. Degrade to no_text and log for us.
        log.exception("translate pipeline failed for frame_id=%s", req.frame_id)
        resp.status = "no_text"
    finally:
        # Every path (incl. the early returns above) is audited — the finally
        # runs before the function actually returns.
        audit_log.append(_audit_entry(req, resp, raw_reads, round((time.perf_counter() - t0) * 1000)))
    return resp
