"""The extension<->service contract, as executable Pydantic models (CLAUDE.md).

This module *is* the contract. Encoding it as typed models is why we chose
FastAPI: the request/response shapes validate and coerce themselves, unknown
fields are rejected loudly (`extra="forbid"`), and /docs documents the seam for
free. The extension must never learn what runs behind these shapes, so nothing
here mentions OCR, PaddleOCR, localhost, or translation providers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "no_text", "duplicate", "low_confidence"]


class TranslateRequest(BaseModel):
    # Reject unknown fields so the extension and service can never silently
    # drift out of contract — a stray field is a 422, not a shrug.
    model_config = ConfigDict(extra="forbid")

    # 3 spaced PNGs (t, t+100ms, t+200ms) is the norm; 1-2 tolerated at edges.
    frames: list[str] = Field(min_length=1, max_length=3)
    source_lang: str = "ch"
    target_lang: str = "en"
    frame_id: int = Field(ge=0)
    last_shipped_text: str = ""
    # Last 2-3 SOURCE (hanzi) lines, as reference for context-aware decoding
    # (DOCUMENTATION.md 6a/6b). Source lines only, never our own translations of
    # them, so a bad earlier translation can't poison later context. The service
    # still translates only the current line; these are reference.
    context_lines: list[str] = Field(default_factory=list, max_length=5)


class TranslateResponse(BaseModel):
    frame_id: int
    status: Status = "no_text"
    source_text: str = ""       # invariant 2: source always crosses the seam, never dropped
    translation: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicate: bool = False
