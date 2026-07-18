# cdrama-translator

Live subtitle translator for videos with hardcoded (burned-in) subtitles. See
`CLAUDE.md` for the operating manual and `DOCUMENTATION.md` for the full design.

## Status: session 3 — end to end on mock OCR/translation

The full loop runs: the extension captures 3 spaced frames on each subtitle,
POSTs them to the local sidecar, and overlays the returned translation on the
player. OCR and translation are still **mocked** (so every line comes back as
canned Chinese + a `[lang·mock]` tag), but capture → change detection → 3-frame
POST → vote/dedup/gate → overlay, plus **drop-don't-queue** and overlay-clear on
silence, are all live.

- `extension/` — capture, text-mask change detection, the debug panel, the
  service call (via the background script), and the translation overlay.
- `sidecar/` — local FastAPI `POST /translate`: real majority vote / dedup /
  gating, **mock** OCR + translation behind swappable seams.

Still mocked: real OCR (PaddleOCR) and real translation (Groq). Those are the
next two steps.

```
extension/
  manifest.json        MV2, Firefox
  background.js        toolbar toggle + sidecar fetch proxy (bypasses page CSP)
  content/
    util.js            namespace, config defaults, logging helpers
    capture.js         <video> fast path + getDisplayMedia fallback; analysis + PNG grabs
    detector.js        text mask, not-text rejection, change-detection state machine
    service.js         the service seam: CDT.Service.translate (knows nothing of HTTP)
    overlay.js         translation overlay pinned to the player, fullscreen-aware
    panel.js           debug panel, box-draw UI, mask canvas, translate controls
    main.js            ~10fps loop: capture -> detector -> panel + 3-frame POST -> overlay
sidecar/               local FastAPI /translate service (mock OCR + translation)
  app.py               pipeline: decode -> OCR -> vote -> gate -> dedup -> translate
  contract.py          TranslateRequest/Response — the contract as Pydantic
  ocr.py, translate.py swappable seams (PaddleOCR / Groq drop in here)
  vote.py, dedup.py    per-character majority vote + string-similarity dedup
  test_client.py       standalone HTTP exercise of every status path
```

## Load the extension (Firefox)

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**.
3. Select `extension/manifest.json`.

Temporary add-ons unload when Firefox restarts — just reload it the same way.
After editing code, click **Reload** on the same page.

## Test it

### To see the translation overlay, start the sidecar first

The overlay only appears if the local service is running:

```bash
cd sidecar
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt   # first time only
./run.sh
```

Then load/reload the extension. In the panel, **Translate ✓** means dispatch is
on; the **svc** stat reads `ok` when the sidecar answered, `down` if it can't be
reached (start it, or check the console), `…` while a request is in flight. Pick
a **target language** in the dropdown and toggle **bilingual** to show the source
line too. Because OCR is mocked, every subtitle overlays canned Chinese tagged
`[en·mock]` — that's expected; you're watching the *wiring*, not real
translations. What should be real and correct: an overlay appears within a beat
of each line, and stays up long enough to read: its lifetime scales with the
**source (hanzi) length** —
`max(subtitleDuration, clamp(hanziCount × ms/hanzi, floor, ceiling))` — so a 1-3
hanzi line clears quickly while a long strip lingers, uniform across every
target language with no per-language table (6b). It's never shorter than the
subtitle was on screen (`line-end − line-start`), measured from when the overlay
appears so a slow round-trip *extends* it rather than eating in. Tune it live
with the **min ms** and **ms/hanzi** sliders. It clears on silence after that
hold; a *new* line clears the previous overlay immediately on arrival (no
lingering past the next line), and a line is dropped only when a newer one
supersedes it (drop-don't-queue). Switch the language and the `[lang·mock·ctxN]`
tag changes — that `ctxN` is the count of source context lines the extension
sent, proving `context_lines` and `target_lang` both cross the contract.

### Easiest: the built-in test harness (no cdrama clip needed)

Open `test/harness.html` in Firefox (File ▸ Open File, or drag it in). It draws
a constantly-moving background with an on-demand burned-in-style subtitle,
piped through a real `<video>` element so the extension's fast path grabs it
exactly like a web player. Click the toolbar button, **Draw box** tight over
the bottom subtitle band, then use the buttons (or **▶ Auto demo**) to drive
each transition on demand: Line A/B/C should each fire one `LINE START`,
Silence should go `idle`, and White flash / Explosion should log
`rejected not-text` and never start a line. This is the fastest way to confirm
the detector works before pointing it at real footage.

### On real footage

1. Open a video with burned-in Chinese subtitles. Good sources:
   - YouTube official cdrama channels (search 电视剧 on channels like
     iQIYI 爱奇艺, 腾讯视频, Youku 优酷 — full episodes with burned-in hanzi).
   - A local file: drag an `.mp4` into Firefox (`file://` pages work).
2. Click the extension's toolbar button (puzzle-piece menu if not pinned).
   The **CDT detector** panel appears top-right.
3. A default box sits over the bottom band of the player. Click **Draw box**
   and drag a tight box hugging the subtitle line — about one line tall
   (~8–10% of player height). Tight matters: it is the single biggest
   false-positive defense (DOCUMENTATION.md §3c).
4. Watch the panel: **crop** is what the box sees, **mask** is the extracted
   text silhouette (green rects = character clusters). Open the console (F12)
   and filter on `[CDT]` for the event stream.

### What you should see

| situation | expected |
|---|---|
| subtitle showing, background moving | mask shows clean character shapes and holds still; state `active`; periodic `stable …` logs; **no** events firing |
| subtitle text changes | one `LINE END (replaced)` + one `LINE START #hash` within ~200ms |
| silent scene (no subtitle) | mask goes black, state `idle` after `LINE END (empty)` + `idle (silence)`; nothing processes |
| brief 1-sample dropout between lines | **no** flicker of events (2-sample hysteresis) |
| white flash / explosion / bright cut | `rejected not-text (white-flood / density / border-ring …)`; **no** LINE START |

The two sliders (white ≥ 210, dark ≤ 80) are the videocr-derived defaults;
nudge them only if the mask looks torn (lower white) or noisy (raise white).
`verbose` logs every sample with its classification and hamming distance.

### Troubleshooting

- **State `black` / "crop is uniformly black":** the `<video>` fast path can't
  read these pixels. On DRM platforms this is the scope boundary working as
  intended (CLAUDE.md invariant 1) — this tool is for in-the-clear video. For
  in-the-clear video that still reads black (protected compositing), click
  **Enable screen-capture fallback** and share this window or screen.
- **"canvas tainted":** the site serves the video cross-origin without CORS.
  Same fix: screen-capture fallback.
- **Fallback crop looks offset:** keep page zoom at 100% and share the window
  or the screen the video is on; the CSS→stream mapping is calibrated
  heuristically (see `capture.js`).
- **No panel:** the player may live in an iframe — the panel opens inside that
  frame. On `about:`/AMO pages the extension cannot run at all.
- **Panel but no video:** press **Video ▸** to cycle among the page's `<video>`
  elements (largest visible is picked first).

## Invariant guardrails already honored in this skeleton

- Change detection runs on the text mask, never raw pixels (invariant 5).
- Empty mask = idle state with 2-sample hysteresis; `line-end` is already an
  event so session 3 can hook overlay-clear to it (invariant 7).
- Whiteness only nominates; structure rejects not-text (invariant 6, basic tier).
- Box is stored as fractions of the player content rect (player-agnostic,
  survives resize/fullscreen; invariant 4).
- No assumption anywhere about how the future service works (the B→A seam).
