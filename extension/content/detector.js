/* Detector: text-mask extraction + change detection (DOCUMENTATION.md §3, 3a, 3b).
 *
 * Change detection never sees raw pixels. Each crop is reduced to a binary
 * text mask (near-white fill that has a dark stroke nearby), the mask is
 * reduced to a coarse silhouette grid, and lines are tracked by comparing
 * grids. Background motion is subtracted before anything is compared, so only
 * the text layer can trigger a change.
 *
 * Whiteness only nominates candidates. Structure (density / blob / border-ring
 * / hairline rules) rejects not-text, and the 2-sample confirmation rejects
 * anything that doesn't hold still — which is what kills explosions (§3b).
 * Full 3b/3c fingerprinting (pixel-pinning, orientation, prop rejection) is
 * later work; these are the session-1 basics.
 */

"use strict";

if (!window.CDT.Detector) {
  const CDT = window.CDT;

  /* Binary dilation by radius r, two O(n) distance passes per axis. */
  function dilateBinary(src, w, h, r, out) {
    const BIG = 1 << 20;
    const tmp = new Int32Array(w * h);
    for (let y = 0; y < h; y++) {
      const row = y * w;
      let d = BIG;
      for (let x = 0; x < w; x++) {
        d = src[row + x] ? 0 : d + 1;
        tmp[row + x] = d;
      }
      d = BIG;
      for (let x = w - 1; x >= 0; x--) {
        d = src[row + x] ? 0 : d + 1;
        if (d < tmp[row + x]) tmp[row + x] = d;
      }
    }
    for (let x = 0; x < w; x++) {
      let d = BIG;
      for (let y = 0; y < h; y++) {
        const i = y * w + x;
        d = tmp[i] === 0 ? 0 : Math.min(d + 1, tmp[i]);
        out[i] = d <= r ? 1 : 0;
      }
      d = BIG;
      for (let y = h - 1; y >= 0; y--) {
        const i = y * w + x;
        d = tmp[i] === 0 ? 0 : Math.min(d + 1, tmp[i]);
        if (d <= r) out[i] = 1;
      }
    }
    return out;
  }

  /* 8-connected components over the mask. Returns [{area,x0,y0,x1,y1}]. */
  function labelComponents(mask, w, h) {
    const labels = new Int32Array(w * h);
    const comps = [];
    const stack = [];
    for (let start = 0; start < mask.length; start++) {
      if (!mask[start] || labels[start]) continue;
      const id = comps.length + 1;
      const c = { area: 0, x0: w, y0: h, x1: 0, y1: 0 };
      labels[start] = id;
      stack.length = 0;
      stack.push(start);
      while (stack.length) {
        const j = stack.pop();
        const x = j % w;
        const y = (j - x) / w;
        c.area++;
        if (x < c.x0) c.x0 = x;
        if (x > c.x1) c.x1 = x;
        if (y < c.y0) c.y0 = y;
        if (y > c.y1) c.y1 = y;
        for (let dy = -1; dy <= 1; dy++) {
          const ny = y + dy;
          if (ny < 0 || ny >= h) continue;
          for (let dx = -1; dx <= 1; dx++) {
            const nx = x + dx;
            if (nx < 0 || nx >= w) continue;
            const k = ny * w + nx;
            if (mask[k] && !labels[k]) {
              labels[k] = id;
              stack.push(k);
            }
          }
        }
      }
      comps.push(c);
      if (comps.length > 400) break; // pathological mask; rejection rules will fire anyway
    }
    return comps;
  }

  /* Merge components into rough character clusters by horizontal adjacency. */
  function clusterComponents(comps, h) {
    const gap = Math.max(2, Math.round(h * 0.04));
    const parts = comps.filter((c) => c.area >= 4).sort((a, b) => a.x0 - b.x0);
    const clusters = [];
    for (const c of parts) {
      const last = clusters[clusters.length - 1];
      if (
        last &&
        c.x0 - last.x1 <= gap &&
        c.y0 <= last.y1 &&
        c.y1 >= last.y0
      ) {
        last.x1 = Math.max(last.x1, c.x1);
        last.y0 = Math.min(last.y0, c.y0);
        last.y1 = Math.max(last.y1, c.y1);
        last.area += c.area;
      } else {
        clusters.push({ x0: c.x0, y0: c.y0, x1: c.x1, y1: c.y1, area: c.area });
      }
    }
    return clusters;
  }

  function median(arr) {
    if (!arr.length) return 0;
    const s = [...arr].sort((a, b) => a - b);
    return s[s.length >> 1];
  }

  /* Coarse silhouette: gridW x gridH occupancy bits. Fixed dims regardless of
   * crop size, so the signature survives box/video-size changes. */
  function silhouetteGrid(mask, w, h, cfg) {
    const gw = cfg.gridW, gh = cfg.gridH;
    const on = new Int32Array(gw * gh);
    const tot = new Int32Array(gw * gh);
    for (let y = 0; y < h; y++) {
      const gy = Math.min(gh - 1, ((y * gh) / h) | 0);
      const row = y * w;
      const grow = gy * gw;
      for (let x = 0; x < w; x++) {
        const cell = grow + Math.min(gw - 1, ((x * gw) / w) | 0);
        tot[cell]++;
        if (mask[row + x]) on[cell]++;
      }
    }
    const bits = new Uint8Array(gw * gh);
    for (let i = 0; i < bits.length; i++) {
      bits[i] = on[i] >= cfg.cellFillThresh * tot[i] ? 1 : 0;
    }
    return bits;
  }

  /* Fold the whole silhouette into a 32-bit digest (FNV-1a) for the debug
   * readout, so different lines show visibly different hashes and one line
   * holds a steady value. Detection uses hamming() on the raw bit array and
   * never touches this string, so its form is free to be display-friendly.
   * (The old per-row hex made the readout show the always-empty top grid row
   * as "00000000"; a fold reflects the entire grid instead.) */
  function gridHex(bits) {
    let h = 0x811c9dc5;
    for (let i = 0; i < bits.length; i++) {
      h = Math.imul(h ^ bits[i], 0x01000193);
    }
    return (h >>> 0).toString(16).padStart(8, "0");
  }

  function hamming(a, b) {
    if (!a || !b || a.length !== b.length) return Infinity;
    let d = 0;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) d++;
    return d;
  }

  /* One frame -> one classified sample. Pure; no state. */
  function analyzeFrame(img, cfg) {
    const w = img.width, h = img.height, n = w * h;
    const data = img.data;
    const white = new Uint8Array(n);
    const dark = new Uint8Array(n);
    let whiteCount = 0;
    for (let i = 0, p = 0; i < n; i++, p += 4) {
      const r = data[p], g = data[p + 1], b = data[p + 2];
      const mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
      const mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
      if (mn >= cfg.whiteMin) {
        white[i] = 1;
        whiteCount++;
      } else if (mx <= cfg.darkMax) {
        dark[i] = 1;
      }
    }
    const whiteFrac = whiteCount / n;

    // Fill must sit on a stroke: white pixel survives only with dark nearby.
    const dilated = dilateBinary(dark, w, h, cfg.strokeRadius, new Uint8Array(n));
    const mask = new Uint8Array(n);
    let count = 0;
    let bx0 = w, by0 = h, bx1 = -1, by1 = -1;
    const edge = cfg.strokeRadius + 1;
    let borderCount = 0;
    for (let y = 0; y < h; y++) {
      const row = y * w;
      for (let x = 0; x < w; x++) {
        const i = row + x;
        if (white[i] && dilated[i]) {
          mask[i] = 1;
          count++;
          if (x < bx0) bx0 = x;
          if (x > bx1) bx1 = x;
          if (y < by0) by0 = y;
          if (y > by1) by1 = y;
          if (x < edge || y < edge || x >= w - edge || y >= h - edge) borderCount++;
        }
      }
    }

    const sample = {
      w, h, mask, count, whiteFrac,
      density: count / n,
      clusters: [],
      clusterCount: 0,
      grid: null,
      hex: null,
      cls: "text",
      reason: null,
    };

    // A flooded-bright box is a positive "not a subtitle" signal, never
    // silence — treating it as empty would end lines during white flashes.
    if (whiteFrac >= cfg.whiteFloodFrac) {
      sample.cls = "not_text";
      sample.reason = "white-flood";
      return sample;
    }

    const floor = Math.max(cfg.minTextAbs, Math.round(n * cfg.minTextFrac));
    sample.floor = floor;
    if (count < floor) {
      sample.cls = "empty";
      return sample;
    }

    const comps = labelComponents(mask, w, h);
    const clusters = clusterComponents(comps, h);
    sample.clusters = clusters;
    sample.clusterCount = clusters.length;
    sample.medianClusterH = median(clusters.map((c) => c.y1 - c.y0 + 1));

    const bboxW = bx1 - bx0 + 1;
    const bboxH = by1 - by0 + 1;
    const bboxFill = count / (bboxW * bboxH);
    let largest = null;
    for (const c of comps) if (!largest || c.area > largest.area) largest = c;
    const lW = largest ? largest.x1 - largest.x0 + 1 : 0;
    const lH = largest ? largest.y1 - largest.y0 + 1 : 0;
    const lFill = largest ? largest.area / (lW * lH) : 0;

    // Structural not-text rules (§3b, session-1 tier). Ordered cheap-to-rich;
    // deliberately conservative so real text is never rejected.
    let reason = null;
    if (sample.density > cfg.densityMax) {
      reason = "density"; // strokes are sparse; fills aren't
    } else if (bboxFill > 0.55 && bboxW > 0.5 * w && bboxH > 0.6 * h) {
      reason = "solid-slab"; // one large solid mass
    } else if (largest && largest.area / count > 0.8 && lH > 0.7 * h && lFill > 0.5) {
      reason = "dominant-blob";
    } else if (borderCount / count > 0.6) {
      reason = "border-ring"; // full-box white leaves only its edge after stroke-gating
    } else if (clusters.length > 0 && sample.medianClusterH < Math.max(4, 0.06 * h)) {
      reason = "hairline"; // 1-2px contour lines from bright-region edges
    }
    if (reason) {
      sample.cls = "not_text";
      sample.reason = reason;
      return sample;
    }

    sample.grid = silhouetteGrid(mask, w, h, cfg);
    sample.hex = gridHex(sample.grid);
    return sample;
  }

  /* State machine over classified samples.
   *
   *   idle   --text x confirmSamples-->  active (LINE START)
   *   active --different text x confirmSamples--> active' (LINE END + LINE START)
   *   active --empty x emptyHysteresis--> idle (LINE END; §3a: the
   *          populated->empty edge is the only "line ended" signal there is)
   *   not_text samples hold all state: they neither start, extend, nor end a line.
   */
  class Detector {
    constructor(cfg, onEvent) {
      this.cfg = cfg;
      this.onEvent = onEvent; // (type, info) => {}
      this.reset();
    }

    reset() {
      this.state = "idle";        // "idle" | "active"
      this.current = null;        // { grid, hex, samples, startedAt }
      this.candidate = null;      // { grid, hex, n }
      this.emptyStreak = 0;
    }

    /* For the panel chip. */
    displayState(cls) {
      if (cls === "not_text") return "reject";
      if (this.state === "active") return this.candidate ? "changing" : "active";
      return this.candidate ? "pending" : "idle";
    }

    push(sample, now) {
      const cfg = this.cfg;
      sample.dCur = this.current ? hamming(this.current.grid, sample.grid) : null;

      if (sample.cls === "not_text") {
        // Hold: an explosion/flash over an active line must not end it,
        // and over silence must not start one.
        this.onEvent("reject", { reason: sample.reason, sample });
        return;
      }

      if (sample.cls === "empty") {
        this.candidate = null;
        if (this.state === "active") {
          this.emptyStreak++;
          if (this.emptyStreak >= cfg.emptyHysteresis) this._endLine("empty", now);
        }
        return;
      }

      // text
      this.emptyStreak = 0;
      if (
        this.state === "active" &&
        hamming(this.current.grid, sample.grid) <= cfg.sameTolBits
      ) {
        // Same line still showing. Follow slow drift so noise never accumulates
        // into a false change, but keep current.hex frozen at the line's
        // start value: it is the line's identity in the log/readout and should
        // hold steady for the whole line even as the grid jitters by a cell.
        this.current.grid = sample.grid;
        this.current.samples++;
        this.candidate = null;
        return;
      }

      if (this.candidate && hamming(this.candidate.grid, sample.grid) <= cfg.sameTolBits) {
        this.candidate.grid = sample.grid;
        this.candidate.hex = sample.hex;
        this.candidate.n++;
        if (this.candidate.n >= cfg.confirmSamples) {
          if (this.state === "active") this._endLine("replaced", now);
          this._startLine(sample, now);
        }
      } else {
        this.candidate = { grid: sample.grid, hex: sample.hex, n: 1 };
      }
    }

    _startLine(sample, now) {
      this.state = "active";
      this.current = {
        grid: sample.grid,
        hex: sample.hex,
        samples: this.candidate ? this.candidate.n : 1,
        startedAt: now,
      };
      this.candidate = null;
      this.onEvent("line-start", {
        hex: sample.hex,
        count: sample.count,
        clusters: sample.clusterCount,
      });
      // Session 3 hook: this is where the 3 spaced frames (t, t+100ms, t+200ms)
      // get grabbed and POSTed to the /translate contract (CLAUDE.md).
    }

    _endLine(reason, now) {
      const durMs = this.current ? now - this.current.startedAt : 0;
      const hex = this.current ? this.current.hex : null;
      this.state = "idle";
      this.current = null;
      this.emptyStreak = 0;
      // Session 3 hook: overlay-clear attaches to this event (§3a) so a stale
      // translation never lingers over a silent shot.
      this.onEvent("line-end", { reason, durMs, hex });
    }
  }

  CDT.analyzeFrame = analyzeFrame;
  CDT.Detector = Detector;
  CDT.hamming = hamming;
}
