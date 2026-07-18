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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import CONSENSUS_FLOOR, OCR_CONF_GATE
from contract import TranslateRequest, TranslateResponse
from dedup import is_duplicate
from ocr import MockOcr, OcrEngine
from translate import MockTranslator, Translator
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
ocr: OcrEngine = MockOcr()
translator: Translator = MockTranslator()


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
    }


@app.post("/translate", response_model=TranslateResponse)
def translate(req: TranslateRequest) -> TranslateResponse:
    resp = TranslateResponse(frame_id=req.frame_id)
    try:
        reads = ocr.read_frames([_decode(f) for f in req.frames])
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
            voted, req.source_lang, req.target_lang, req.context_lines
        )
        resp.status = "ok"
        return resp
    except Exception:
        # Real OCR/providers can throw; the extension must always get valid
        # contract JSON, never a 500. Degrade to no_text and log for us.
        log.exception("translate pipeline failed for frame_id=%s", req.frame_id)
        resp.status = "no_text"
        return resp
