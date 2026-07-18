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
import hashlib
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
from glossary import GLOSSARY
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
# Tag this run's audit log by model, so llama/qwen runs land in separate files.
audit_log.set_run_tag(getattr(translator, "model", None) or "mock")


def _decode(frame_b64: str) -> bytes:
    try:
        return base64.b64decode(frame_b64, validate=True)
    except (binascii.Error, ValueError):
        return b""


# Per-episode sequencing so the audit log can be reassembled into an ordered
# script (the teacher's full context for distillation) and split sentences can be
# regrouped. episode_id is a stable hash of the page label, so lines of the same
# video group across runs; line_seq/sentence_group_id are per-process counters.
_ep_state: dict[str, dict] = {}


def _episode_id(label: str) -> str:
    base = (label or "").strip()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12] if base else "unknown"


def _next_seq(ep_id: str, continuation: bool) -> tuple[int, int]:
    """Advance the per-episode line counter, and the sentence-group counter only
    when this line does NOT continue the previous one (continuation=False starts a
    new sentence). So a split sentence's fragments share one sentence_group_id."""
    st = _ep_state.setdefault(ep_id, {"seq": 0, "group": 0})
    if not continuation:
        st["group"] += 1
    st["seq"] += 1
    return st["seq"], st["group"]


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "ocr": type(ocr).__name__,
        "translator": type(translator).__name__,
        "model": getattr(translator, "model", None),
    }


_PROVIDER = {"GroqTranslator": "groq", "MockTranslator": "mock", "OllamaTranslator": "ollama"}


def _audit_entry(req: TranslateRequest, resp: TranslateResponse, raw_reads: list, glossary: dict,
                 latency_ms: int, ep_id: str, line_seq: int | None, group_id: int | None) -> dict:
    # Per-stage record, so a bad output can be traced to the stage it went wrong:
    # raw per-frame OCR reads -> voted source_text -> context/continuation -> translation.
    return {
        "stage": "translate",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "video_time": req.video_time,
        "label": req.label,
        "episode_id": ep_id,          # stable hash of label — groups a video across runs
        "line_seq": line_seq,         # per-episode monotonic order (ok lines only; else null)
        "sentence_group_id": group_id,  # fragments of one split sentence share this (6a)
        "frame_id": resp.frame_id,
        "target_lang": req.target_lang,
        "status": resp.status,
        "reads": raw_reads,                 # the 3 raw frames, pre-vote: [{text, conf}]
        "source_text": resp.source_text,    # after per-character majority vote
        "confidence": resp.confidence,      # mean of the contributing reads
        "context_lines": req.context_lines,
        "continuation": req.continuation,   # candidate-to-bridge flag (↳cont)
        "context_note": req.context_note[:200],  # user-supplied background (truncated; session-constant)
        "tone": req.tone,                   # register lean, if any (session-constant)
        "glossary": glossary,               # terms pinned for this line, if any (6b)
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
        "final_text": ev.final_text,   # the text actually shown for this frame_id (post-revision)
        "label": ev.label,
    })
    return {"ok": True}


@app.post("/translate", response_model=TranslateResponse)
def translate(req: TranslateRequest) -> TranslateResponse:
    t0 = time.perf_counter()
    resp = TranslateResponse(frame_id=req.frame_id)
    raw_reads: list = []
    gloss: dict = {}
    ep_id = _episode_id(req.label)
    line_seq: int | None = None      # assigned only when the line is actually shown (ok)
    group_id: int | None = None
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

        gloss = GLOSSARY.matching(voted, req.context_lines, req.label)
        resp.translation = translator.translate(
            voted, req.source_lang, req.target_lang, req.context_lines, req.continuation,
            gloss, req.context_note, req.tone
        )
        # A real, shown line: assign its place in the episode + sentence group.
        line_seq, group_id = _next_seq(ep_id, req.continuation)
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
        audit_log.append(_audit_entry(req, resp, raw_reads, gloss,
                                      round((time.perf_counter() - t0) * 1000), ep_id, line_seq, group_id))
    return resp
