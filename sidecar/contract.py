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
    # them, so a bad earlier translation can't poison later context.
    context_lines: list[str] = Field(default_factory=list, max_length=5)
    # True when the extension judges this line completes the previous one (split
    # sentence): the service then renders context_lines + this line as one
    # sentence (re-translation / screen-rewriting, 6a). Default: translate this
    # line alone with context_lines as reference.
    continuation: bool = False
    # Optional user-supplied background about the show/episode (names, register,
    # plot), handed to the service as reference for every line — decoding aid, not
    # content (the service injects it as "reference only, do not translate/inject").
    # Static per session, so it sits in the cacheable prefix. Empty = off.
    context_note: str = Field(default="", max_length=1000)
    # Optional free-text label (the extension sends the page title) so the audit
    # log can group lines by episode. Logging only; never affects translation.
    label: str = Field(default="", max_length=200)
    # Video position (seconds) when the frames were grabbed, so a logged line can
    # be jumped back to in the player. Logging only.
    video_time: float = Field(default=0.0, ge=0.0)


class DisplayEvent(BaseModel):
    """Client-side display outcome for a line, reported after the fact (the
    overlay's fate is only known once it clears). Correlated to a TranslateResponse
    by frame_id in the audit log. Logging only — never part of the render path."""

    model_config = ConfigDict(extra="forbid")

    frame_id: int = Field(ge=0)
    video_time: float = Field(default=0.0, ge=0.0)
    visible_ms: int = Field(default=0, ge=0)
    outcome: Literal["expired", "preempted", "revised", "replaced", "dropped", "cleared"] = "expired"
    label: str = Field(default="", max_length=200)
    # The translation actually on screen for this frame_id when it finalized —
    # i.e. the final rendered text AFTER any continuation revision/tail-masking,
    # not an intermediate. Logging only; lets the audit recover the canonical
    # shown line per sentence group (distillation target). Empty for a line that
    # was dropped before it showed carries the translation that never made it up.
    final_text: str = Field(default="", max_length=500)


class TranslateResponse(BaseModel):
    frame_id: int
    status: Status = "no_text"
    source_text: str = ""       # invariant 2: source always crosses the seam, never dropped
    translation: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicate: bool = False
