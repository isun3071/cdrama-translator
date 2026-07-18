# CLAUDE.md

Operating manual for this repo. Read this first, every session. Depth and rationale live in `DOCUMENTATION.md`; this file is the durable "how to work here."

## What this is

A live subtitle translator for videos with **hardcoded (burned-in) subtitles**, primarily Chinese-language TV dramas. It reads the on-screen subtitle text off a user-drawn box over any video player, translates it, and overlays the translation in real time.

The core insight: cdramas reliably burn Mandarin subtitles into the video pixels regardless of what language characters actually speak on the audio track. Reading that one normalized text channel via OCR sidesteps every multilingual-audio problem and works on any player.

## Two-phase build

- **Shape B (now, prototype):** Firefox extension does capture + overlay + UI. A local Python sidecar (`127.0.0.1`) runs the OCR/vision stack and answers over an HTTP contract. This lets us reuse the mature Python OCR ecosystem (PaddleOCR, VideoSubFinder-style background stripping) immediately.
- **Shape A (later, product):** Pure Firefox extension. The Python sidecar is replaced by in-browser wasm OCR (ONNX Runtime / PP-OCRv5). Ships one-click on addons.mozilla.org with no companion process.

**The whole point of the contract is that the extension cannot tell the difference between B and A.** Never let extension code assume Python, localhost, or anything sidecar-specific beyond the HTTP endpoint. The swap from B to A must touch only the service implementation, never the extension's capture/overlay/drop logic.

## Architecture (the seam)

```
[browser tab pixels]
   -> extension: capture via getDisplayMedia (fallback) or <video> canvas grab (fast path)
   -> extension: crop to user's box (anchored to player bbox, survives resize/fullscreen)
   -> extension: cheap change detection on a text-mask (not raw pixels)
   -> on new line: grab 3 spaced frames (t, t+100ms, t+200ms)
   -> POST frames + metadata to the SERVICE  <-- THE CONTRACT
   -> service: OCR all 3, per-character majority vote, dedup, translate
   -> service returns {source_text, translation, confidence, status, frame_id}
   -> extension: drop if stale (frame moved on), else overlay
```

### The contract (do not break casually)

Request (extension -> service):
```json
{
  "frames": ["<base64 png>", "<base64 png>", "<base64 png>"],
  "source_lang": "ch",
  "target_lang": "en",
  "frame_id": 4821,
  "last_shipped_text": "<what the extension last displayed>",
  "context_lines": ["<prev source line>", "<prev-1 source line>"],
  "continuation": false,
  "label": "<page title, optional — for the audit log only>"
}
```

`context_lines` is the last 2-3 **source** (hanzi) lines, as reference for
context-aware decoding (DOCUMENTATION.md 6a/6b). Source only, never our own
translations. `continuation` is true when the extension judges this line
completes the previous one (split sentence): the service then renders
context_lines + this line as one sentence (re-translation, 6a). Otherwise the
service translates only the current line.

Response (service -> extension):
```json
{
  "frame_id": 4821,
  "status": "ok | no_text | duplicate | low_confidence",
  "source_text": "<hanzi>",
  "translation": "<target language>",
  "confidence": 0.94,
  "duplicate": false
}
```

Division of labor: the **service** owns OCR, the majority vote, and dedup (they need the OCR text). The **extension** owns capture, the crop box, and drop-don't-queue (needs only `frame_id`). Keep the extension dumb on purpose.

## Invariants (must always hold)

1. **Scope: personal use, no redistribution, in-the-clear video only.** Do not build features that target DRM-protected premium platforms or that export/redistribute translated subtitle files. The direct `<video>` canvas grab returning black on DRM is a *feature* of this boundary, not a bug to defeat.
2. **Never throw away `source_text`.** Both source and translation cross the contract and reach render. Bilingual overlay must stay a pure rendering toggle, never a re-architecture.
3. **Bounded lag, never accumulating.** The product promise is a fixed short delay that never grows, not zero lag. Enforce with drop-don't-queue: if a newer line is already on screen when an older translation returns, drop the old one. A translation must always belong to a line currently visible.
4. **Player-agnostic.** No per-platform integration code. The tool points a box at pixels; it must not care whether the player is YouTube, Youku, Rumble, bilibili, or a local file.
5. **Change detection runs on the text-mask, not raw pixels.** Raw-pixel diffing fails because the video behind borderless subtitles is always moving. Threshold to near-white + dark-stroke text first, then diff.
6. **Whiteness only finds candidates; structure confirms text; overlay-ness confirms subtitle.** Never identify subtitles by whiteness alone. Reject not-text (explosions, white flashes) via thin-stroke / character-layout / temporal-stability. Reject not-the-subtitle (held paper, chalkboard) via tight position band + pixel-pinned coordinates + horizontal-frontal orientation + synthetic stroke. Fix every new false positive with a structural/temporal/overlay discriminator, never by tuning the white threshold harder. See DOCUMENTATION.md 3b/3c.
7. **Empty mask is an idle state, not a bug.** No text in the box = silence: skip all processing AND clear any showing overlay (with ~2-sample hysteresis). The populated->empty edge is the only "line ended" signal the detector has. See DOCUMENTATION.md 3a.

## Conventions

- Extension: Manifest V2 (Firefox still supports it; avoids the MV3 service-worker limits). Plain JS, no framework needed for v1.
- Sidecar: Python, Flask (or FastAPI), single `/translate` endpoint matching the contract. Keep OCR model warm across requests.
- Translation LLM: Groq for lowest time-to-first-token (we are TTFT-bound, output is ~20 tokens/line). A local model via Ollama is the zero-cost fallback. Keep the translation call behind a small interface so the provider is swappable.
- Tune from known-good defaults borrowed from videocr: brightness/near-white threshold ~210, string-similarity dedup ~80, OCR confidence gate ~75. Do not tune from zero.

## What to do when unsure

Match the pipeline in `DOCUMENTATION.md`. If a change would make the extension aware of how OCR is implemented, stop: it breaks the B->A swap. If a change accumulates lag or drops the source text, stop: it breaks an invariant.
