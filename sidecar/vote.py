"""Per-character majority vote across the 3 OCR reads (DOCUMENTATION.md §4).

The 3 frames are spaced (t, t+100ms, t+200ms) so they carry *different*
background noise; the subtitle text is the only signal consistent across all
three. Voting per character corrects single-frame misreads that busy
backgrounds cause. Engine-agnostic: operates purely on the OCR strings, so it
is already final even though the OCR behind it is still a mock.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher


def majority_vote(reads: list[str]) -> str:
    reads = [r for r in reads if r]
    if not reads:
        return ""
    if len(reads) == 1:
        return reads[0]

    # Clean CJK OCR yields equal-length reads (one glyph per character), so a
    # positional column vote is correct and cheap. Vote only among reads that
    # share the modal length; a frame that dropped/added a glyph is misaligned
    # and must not corrupt the columns.
    modal_len = Counter(len(r) for r in reads).most_common(1)[0][0]
    aligned = [r for r in reads if len(r) == modal_len]
    if len(aligned) >= 2:
        out = []
        for i in range(modal_len):
            out.append(Counter(r[i] for r in aligned).most_common(1)[0][0])
        return "".join(out)

    # No two reads agree on length: fall back to the most "central" read (the
    # one most similar to the others) rather than voting misaligned columns.
    def centrality(idx: int) -> float:
        return sum(
            SequenceMatcher(None, reads[idx], reads[j]).ratio()
            for j in range(len(reads))
            if j != idx
        )

    return reads[max(range(len(reads)), key=centrality)]


def consensus_ratio(reads: list[str]) -> float:
    """Mean pairwise similarity of the non-empty reads (0..1). Low means the
    three disagree wildly -> no clean text, wait (DOCUMENTATION.md §4)."""
    reads = [r for r in reads if r]
    if len(reads) < 2:
        return 1.0 if reads else 0.0
    pairs = [
        SequenceMatcher(None, reads[i], reads[j]).ratio()
        for i in range(len(reads))
        for j in range(i + 1, len(reads))
    ]
    return sum(pairs) / len(pairs)
