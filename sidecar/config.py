"""Tunables. Starting values borrowed from the videocr family (CLAUDE.md:
"Do not tune from zero"), on a 0..1 scale where videocr used 0..100."""

OCR_CONF_GATE = 0.75      # drop reads below this mean confidence (videocr's ~75)
DEDUP_SIMILARITY = 80     # >= this % similar to last shipped line = duplicate
CONSENSUS_FLOOR = 0.5     # if the 3 reads agree less than this, treat as no clean text
