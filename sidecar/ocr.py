"""OCR engine seam.

Session 2 ships a mock so the whole contract can be proven before the heavy
PaddleOCR install. The real engine implements the same one-method interface and
drops into app.py by swapping a single line — the endpoint never names it, which
is what keeps the Shape B -> Shape A (wasm) swap invisible to the extension.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from config import (
    OCR_DARK_MAX,
    OCR_MASK,
    OCR_MASK_ADAPTIVE,
    OCR_MASK_FALLBACK_CONF,
    OCR_MASK_PAD,
    OCR_STROKE_RADIUS,
    OCR_WHITE_MIN,
)

log = logging.getLogger("sidecar.ocr")


@dataclass
class OcrRead:
    text: str
    confidence: float  # 0..1


class OcrEngine(Protocol):
    def read_frames(self, frames: list[bytes]) -> list[OcrRead]:
        """One read per input frame, same order. Real engines OCR the 3
        concurrently; the mock just maps positions."""
        ...


# A few distinct lines so the extension shows a *different* overlay per subtitle
# (and dedup doesn't collapse them all into one). Real PaddleOCR replaces this
# next step; the vote/dedup/response around it are already final.
_LINES = [
    "你是怎么忍住不娶的呢",
    "彩兰姐对你可是一片真心",
    "这些年我从来没有后悔过",
    "外面风大你多穿点衣服",
]
_PER_FRAME_CONF = (0.94, 0.92, 0.88)


class MockOcr:
    """Ignores real pixels. Returns three reads of one line with a single-glyph
    misread planted in the third frame, so the per-character majority vote
    visibly corrects it.

    Substantial frames (real crops from the extension) advance a counter so each
    subtitle yields a different line; the tiny 1x1 PNG that test_client sends is
    treated as a fixed deterministic case so the standalone tests stay
    order-independent. A near-empty frame models a silent box -> no_text.
    """

    def __init__(self) -> None:
        self._batch = 0

    def read_frames(self, frames: list[bytes]) -> list[OcrRead]:
        substantial = max((len(f) for f in frames), default=0) >= 300
        if substantial:
            line = _LINES[self._batch % len(_LINES)]
            self._batch += 1
        else:
            line = _LINES[0]  # deterministic for test_client's 1x1 frames

        out: list[OcrRead] = []
        for i, png in enumerate(frames):
            if len(png) < 8:
                out.append(OcrRead("", 0.0))
                continue
            text = line
            if i == 2 and line:  # plant a 1-glyph misread the vote must overrule
                j = len(line) // 2
                text = line[:j] + ("口" if line[j] != "口" else "田") + line[j + 1:]
            conf = _PER_FRAME_CONF[i] if i < len(_PER_FRAME_CONF) else 0.9
            out.append(OcrRead(text, conf))
        return out


def mask_for_ocr(
    bgr,
    white_min: int = OCR_WHITE_MIN,
    dark_max: int = OCR_DARK_MAX,
    stroke_radius: int = OCR_STROKE_RADIUS,
    pad: int = OCR_MASK_PAD,
):
    """Background-subtract a raw crop before OCR (DOCUMENTATION.md §4).

    Keep the near-white subtitle fill that sits next to a dark stroke (plus its
    stroke and anti-aliased edge), flatten everything else to black. This is the
    same isolate-the-text-layer step the extension runs for change detection,
    re-derived service-side at OCR resolution so moving clutter can't be read as
    a stray glyph. Preserves the text's original pixels (not a hard binary) so
    PP-OCR sees a natural white-on-dark line. Returns a BGR image.
    """
    import cv2
    import numpy as np

    b, g, r = cv2.split(bgr)
    mn = np.minimum(np.minimum(b, g), r)
    mx = np.maximum(np.maximum(b, g), r)
    white = (mn >= white_min).astype(np.uint8)
    dark = (mx <= dark_max).astype(np.uint8)

    k = 2 * stroke_radius + 1
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    text = cv2.bitwise_and(white, cv2.dilate(dark, ell))  # fill bordered by stroke
    text = cv2.morphologyEx(
        text, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    )
    # Widen to recover the stroke + anti-aliased glyph edge, so characters stay
    # whole rather than eroding to their cores.
    region = cv2.dilate(text, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

    out = np.zeros_like(bgr)
    m = region.astype(bool)
    out[m] = bgr[m]
    if pad:
        out = cv2.copyMakeBorder(out, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(0, 0, 0))
    return out


class RapidOcrEngine:
    """Real OCR: RapidOCR (ONNX Runtime + PP-OCR models), strong on CJK. This is
    the ONNX/PP-OCR pipeline the wasm Shape A will mirror, so Shape B is a
    stepping stone toward it. The model is loaded once and kept warm across
    requests (CLAUDE.md). With OCR_MASK on, the crop is background-subtracted
    (mask_for_ocr) before recognition."""

    def __init__(self) -> None:
        import cv2
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR

        self._cv2 = cv2
        self._np = np
        self._engine = RapidOCR()

    def read_frames(self, frames: list[bytes]) -> list[OcrRead]:
        return [self._read_one(f) for f in frames]

    def _read_one(self, png: bytes) -> OcrRead:
        if len(png) < 8:
            return OcrRead("", 0.0)
        img = self._cv2.imdecode(
            self._np.frombuffer(png, self._np.uint8), self._cv2.IMREAD_COLOR
        )
        if img is None:
            return OcrRead("", 0.0)
        if not OCR_MASK:
            return self._recognize(img)
        read = self._recognize(mask_for_ocr(img))
        # Adaptive fallback (§4): masking cleans clutter but can hurt thin-stroke /
        # low-contrast text. When the masked read is weak, try the RAW crop too and
        # keep whichever the recognizer trusted more. The second pass runs only on
        # weak frames, so the common case still costs one OCR. Behind the seam.
        if OCR_MASK_ADAPTIVE and read.confidence < OCR_MASK_FALLBACK_CONF:
            raw = self._recognize(img)
            if raw.text and raw.confidence > read.confidence:
                return raw
        return read

    def _recognize(self, img) -> OcrRead:
        result, _elapse = self._engine(img)
        if not result:
            return OcrRead("", 0.0)
        # A subtitle crop can yield several boxes; order them left-to-right and
        # join with no separator (hanzi carry no spaces).
        items = sorted(result, key=lambda it: min(p[0] for p in it[0]))
        # Strip ASCII + ideographic (　) whitespace PP-OCR sometimes appends.
        text = "".join((it[1] or "") for it in items).strip(" \t\r\n　")
        scores = [float(it[2]) for it in items if it[2] is not None]
        conf = sum(scores) / len(scores) if scores else 0.0
        return OcrRead(text, conf)


def make_ocr() -> OcrEngine:
    """RapidOCR if it imports (and not forced off), else the mock. CDT_OCR=mock
    forces the mock for offline / contract testing."""
    if os.getenv("CDT_OCR", "").strip().lower() == "mock":
        log.info("ocr: MockOcr (forced by CDT_OCR=mock)")
        return MockOcr()
    try:
        engine = RapidOcrEngine()
        log.info("ocr: RapidOcrEngine")
        return engine
    except Exception as e:
        log.warning("ocr: RapidOCR unavailable, using MockOcr (%s)", e)
        return MockOcr()
