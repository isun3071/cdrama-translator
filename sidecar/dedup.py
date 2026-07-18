"""Dedup the voted line against the last line the extension shipped
(DOCUMENTATION.md §5). A subtitle sits on screen 2-4s and re-OCRs to ~the same
text every sample; without this we'd fire many identical translation calls for
one line. Lives service-side because it needs the OCR text, and the contract
hands us `last_shipped_text` for exactly this.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from config import DEDUP_SIMILARITY


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 100.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


def is_duplicate(candidate: str, last_shipped: str) -> tuple[bool, float]:
    if not last_shipped:
        return False, 0.0
    sim = similarity(candidate, last_shipped)
    return sim >= DEDUP_SIMILARITY, sim
