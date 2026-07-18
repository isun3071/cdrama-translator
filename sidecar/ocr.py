"""OCR engine seam.

Session 2 ships a mock so the whole contract can be proven before the heavy
PaddleOCR install. The real engine implements the same one-method interface and
drops into app.py by swapping a single line — the endpoint never names it, which
is what keeps the Shape B -> Shape A (wasm) swap invisible to the extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
