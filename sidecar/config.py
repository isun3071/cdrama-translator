"""Tunables. Starting values borrowed from the videocr family (CLAUDE.md:
"Do not tune from zero"), on a 0..1 scale where videocr used 0..100."""

# Drop reads below this mean confidence. Lowered from videocr's 0.75: short CJK
# lines score systematically lower (fewer glyphs), and with masking cleaning the
# input, 0.75 dropped correct reads like 走吧 (0.54) / 测试一下 (0.70). The
# 3-frame vote + CONSENSUS_FLOOR are the real garbage filters; this is a floor.
# (A length-aware gate would be more precise — a later refinement.)
OCR_CONF_GATE = 0.50
DEDUP_SIMILARITY = 80     # >= this % similar to last shipped line = duplicate
CONSENSUS_FLOOR = 0.5     # if the 3 reads agree less than this, treat as no clean text

# Mask-before-OCR (DOCUMENTATION.md §4): background-subtract the raw crop so
# moving clutter can't be read as text. Thresholds mirror the extension's
# detector defaults (near-white fill confirmed by a nearby dark stroke).
OCR_MASK = True
OCR_WHITE_MIN = 210       # near-white subtitle fill: min(r,g,b) >= this
OCR_DARK_MAX = 80         # dark stroke: max(r,g,b) <= this
OCR_STROKE_RADIUS = 3     # a fill pixel is kept only if dark stroke is within this
# Keep 0. A black border on the masked image makes PP-OCR's DBNet detection
# post-processing explode: measured ~40ms/frame at pad=0 vs ~1200ms at pad>=6.
OCR_MASK_PAD = 0

# Masking cleans moving clutter but can hurt thin-stroke / low-contrast text — it
# once made the confidence gate (not clutter) the thing dropping correct reads like
# 走吧@0.54. Adaptive fallback: when the masked read is weak (< fallback conf), also
# OCR the RAW crop and keep whichever the recognizer was more confident about. The
# second pass runs only on weak frames, so the common case still costs one OCR. This
# lives entirely behind the OCR seam — the extension never knows. Set
# OCR_MASK_ADAPTIVE=False to always trust the mask; OCR_MASK=False skips masking (raw).
OCR_MASK_ADAPTIVE = True
OCR_MASK_FALLBACK_CONF = 0.65   # masked read below this also tries raw
