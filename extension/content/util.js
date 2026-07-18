/* CDT namespace + small shared helpers.
 * All content files share one sandbox scope; each guards against re-injection. */

"use strict";

if (!window.CDT) window.CDT = {};

if (!window.CDT.util) {
  const CDT = window.CDT;

  /* Tunables. Live values sit on a per-session copy (main.js) so the panel
   * sliders can mutate them; these are the known-good starting points
   * (whiteMin/darkMax borrowed from the videocr family, per CLAUDE.md). */
  CDT.defaultConfig = {
    sampleMs: 100,          // ~10 fps
    analysisMaxW: 1280,     // cap crop analysis width; keeps thin strokes intact at 1080p
    whiteMin: 210,          // near-white fill: min(r,g,b) >= this
    darkMax: 80,            // dark stroke: max(r,g,b) <= this
    strokeRadius: 3,        // white pixel must have a dark pixel within this radius
    minTextFrac: 0.0015,    // pixel floor as a fraction of crop area (abs floor below)
    minTextAbs: 60,
    whiteFloodFrac: 0.65,   // raw-white coverage above this = flooded box, not a subtitle
    densityMax: 0.30,       // mask density above this = fill, not strokes
    gridW: 48,              // coarse silhouette grid for hashing
    gridH: 6,
    cellFillThresh: 0.18,   // grid cell "on" if >= this fraction of its pixels set
    sameTolBits: 8,         // hamming tolerance: <= this = same line
    confirmSamples: 2,      // consecutive matching samples to declare a line
    emptyHysteresis: 2,     // consecutive empty samples to declare line end (3a)

    // Translation / overlay (session 3)
    translateEnabled: true, // dispatch to the sidecar on line start
    targetLang: "en",       // any target language; the point of the product
    frameSpacingMs: 100,    // spacing of the 3 OCR frames (t, t+100, t+200)
    pngMaxW: 1600,          // cap width of the PNG frames sent to OCR
    overlayBilingual: false,// pure rendering toggle (invariant 2)

    // Split-sentence re-translation (6a): a line "continues" the previous one if
    // the previous didn't end in terminal punctuation and the gap was short.
    continuationGapMs: 2500,// max gap between lines to still count as one sentence
    sentenceMaxLines: 3,    // cap a re-translated sentence at this many lines
    // Overlay lifetime scales with the SOURCE (hanzi) length (reading speed):
    // hold = max(subtitleDuration, clamp(hanziCount * readMsPerHanzi, min, max)),
    // measured from when the overlay appears so lag extends it, not shortens it.
    // Scaling by source (not target) means one rate works for every target
    // language, no per-language reading table (DOCUMENTATION.md 6b).
    overlayMinMs: 800,      // floor: shortest readable hold, e.g. a 1-3 hanzi line
                            // (~matches pro subtitle floors: Netflix ~0.83s)
    overlayMaxMs: 7000,     // ceiling: a long or garbled line can't pin the overlay
    readMsPerHanzi: 200,    // hold per source hanzi (~5 hanzi/sec-ish once the
                            // target expansion is accounted for)

    verbose: false,
  };

  const pad = (n, w) => String(n).padStart(w, "0");

  function timeTag() {
    const d = new Date();
    return `${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}:${pad(d.getSeconds(), 2)}.${pad(d.getMilliseconds(), 3)}`;
  }

  function log(msg, ...rest) {
    console.log(`[CDT ${timeTag()}] ${msg}`, ...rest);
  }

  /* Returns a fn that runs at most once per windowMs; drops the rest. */
  function rateLimit(windowMs, fn) {
    let last = 0;
    return (...args) => {
      const now = performance.now();
      if (now - last >= windowMs) {
        last = now;
        fn(...args);
      }
    };
  }

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

  /* Ephemeral notice that works even before/without the panel. */
  function toast(text, ms = 4000) {
    const el = document.createElement("div");
    el.textContent = text;
    el.style.cssText =
      "position:fixed;left:50%;bottom:8%;transform:translateX(-50%);" +
      "background:rgba(20,20,24,.92);color:#eee;padding:8px 14px;border-radius:6px;" +
      "font:13px/1.4 system-ui,sans-serif;z-index:2147483647;pointer-events:none;" +
      "box-shadow:0 2px 10px rgba(0,0,0,.5);max-width:70vw;";
    (document.body || document.documentElement).appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  CDT.util = { timeTag, log, rateLimit, clamp, toast };
}
