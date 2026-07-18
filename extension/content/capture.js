/* Capture: turns "the user's box over the player" into pixels at ~10fps.
 *
 * Fast path: drawImage(<video>) -> canvas, cropping in the video's intrinsic
 * pixel space. Because the crop is intrinsic, resize and fullscreen change
 * nothing downstream — the detector and OCR see identical pixels.
 *
 * Fallback: getDisplayMedia when the fast path is dead (tainted canvas from a
 * non-CORS cross-origin src, or a crop that comes back uniformly near-black).
 * A persistently black crop on DRM content is the scope boundary working as
 * intended (CLAUDE.md invariant 1), not a failure to route around.
 *
 * Two outputs from the same crop geometry:
 *   grab(box)        -> downscaled ImageData for cheap change detection
 *   grabCropPng(box) -> native-ish-res base64 PNG for the OCR service frames
 * A single _cropSource(box) resolves geometry for both so they never diverge.
 */

"use strict";

if (!window.CDT.Capture) {
  const CDT = window.CDT;
  const { clamp } = CDT.util;

  function findVideos() {
    return [...document.querySelectorAll("video")]
      .filter((v) => v.videoWidth > 0 && v.videoHeight > 0)
      .map((v) => {
        const r = v.getBoundingClientRect();
        const vis =
          Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0)) *
          Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
        return { v, vis };
      })
      .sort((a, b) => b.vis - a.vis)
      .map((x) => x.v);
  }

  function isNearBlack(img) {
    const d = img.data;
    let max = 0, sum = 0, n = 0;
    for (let p = 0; p < d.length; p += 32) {
      const l = (d[p] + d[p + 1] + d[p + 2]) / 3;
      if (l > max) max = l;
      sum += l;
      n++;
    }
    return max < 18 && sum / n < 10;
  }

  class Capture {
    constructor(cfg) {
      this.cfg = cfg;
      this.video = null;
      this.mode = "video";          // "video" | "display"
      this.fastPathDead = null;     // null | "tainted" | "black"
      this.canvas = document.createElement("canvas");
      this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
      this.pngCanvas = null;        // lazily created; separate so PNG grabs don't
      this.pngCtx = null;           // clobber the analysis canvas mid-tick
      this.fbStream = null;
      this.fbVideo = null;
      this.fbMap = null;            // { mode, kx, ky, offX, offY }
      this.onFallbackEnded = null;
    }

    setVideo(v) {
      this.video = v;
      if (this.fastPathDead === "black") this.fastPathDead = null; // new source, retry
    }

    /* Displayed content rect of the video in viewport CSS px, letterbox-aware
     * (assumes default object-fit: contain). The box UI and fractions live in
     * this space so "over the subtitle" means over the video image, not the
     * element's letterbox bars. */
    contentRect() {
      const v = this.video;
      if (!v) return null;
      const r = v.getBoundingClientRect();
      if (r.width < 2 || r.height < 2 || !v.videoWidth) return null;
      const scale = Math.min(r.width / v.videoWidth, r.height / v.videoHeight);
      const w = v.videoWidth * scale;
      const h = v.videoHeight * scale;
      return {
        x: r.left + (r.width - w) / 2,
        y: r.top + (r.height - h) / 2,
        w,
        h,
      };
    }

    /* Resolve crop source + source-pixel rect for the current mode. box is
     * {x,y,w,h} as fractions of the content rect. Returns {source,sx,sy,sw,sh}
     * or {err}. Single source of geometry truth for both outputs. */
    _cropSource(box) {
      if (this.mode === "display") {
        const fv = this.fbVideo;
        if (!fv || !fv.videoWidth) return { err: "notready" };
        const cr = this.contentRect();
        if (!cr) return { err: "novideo" };
        const m = this.fbMap;
        const cssX = cr.x + box.x * cr.w;
        const cssY = cr.y + box.y * cr.h;
        const cssW = box.w * cr.w;
        const cssH = box.h * cr.h;
        const sx = clamp((cssX + m.offX) * m.kx, 0, fv.videoWidth - 1);
        const sy = clamp((cssY + m.offY) * m.ky, 0, fv.videoHeight - 1);
        const sw = Math.min(cssW * m.kx, fv.videoWidth - sx);
        const sh = Math.min(cssH * m.ky, fv.videoHeight - sy);
        return { source: fv, sx, sy, sw, sh };
      }
      const v = this.video;
      if (!v || !v.isConnected) return { err: "novideo" };
      if (v.readyState < 2 || !v.videoWidth) return { err: "notready" };
      if (this.fastPathDead) return { err: this.fastPathDead };
      return {
        source: v,
        sx: box.x * v.videoWidth,
        sy: box.y * v.videoHeight,
        sw: box.w * v.videoWidth,
        sh: box.h * v.videoHeight,
      };
    }

    _analysisDims(sw, sh) {
      const dw = Math.max(48, Math.min(this.cfg.analysisMaxW, Math.round(sw)));
      const dh = Math.max(10, Math.round((dw * sh) / sw));
      return { dw, dh };
    }

    /* Downscaled ImageData for change detection. */
    grab(box) {
      const cs = this._cropSource(box);
      if (cs.err) return { err: cs.err };
      const { source, sx, sy, sw, sh } = cs;
      if (sw < 8 || sh < 4) return { err: "badbox" };
      const { dw, dh } = this._analysisDims(sw, sh);
      if (this.canvas.width !== dw) this.canvas.width = dw;
      if (this.canvas.height !== dh) this.canvas.height = dh;
      this.ctx.drawImage(source, sx, sy, sw, sh, 0, 0, dw, dh);
      let img;
      try {
        img = this.ctx.getImageData(0, 0, dw, dh);
      } catch (e) {
        if (this.mode === "video") this.fastPathDead = "tainted";
        return { err: "tainted" };
      }
      if (isNearBlack(img)) return { err: "black", img };
      return { img };
    }

    /* Native-ish-resolution base64 PNG of the crop, for the OCR service frames.
     * Higher res than the analysis grab so OCR has real strokes to read; the
     * service owns any further preprocessing (keep the extension dumb). */
    grabCropPng(box, maxW = 1600) {
      const cs = this._cropSource(box);
      if (cs.err) return { err: cs.err };
      const { source, sx, sy, sw, sh } = cs;
      if (sw < 8 || sh < 4) return { err: "badbox" };
      const dw = Math.max(16, Math.min(maxW, Math.round(sw)));
      const dh = Math.max(8, Math.round((dw * sh) / sw));
      if (!this.pngCanvas) {
        this.pngCanvas = document.createElement("canvas");
        this.pngCtx = this.pngCanvas.getContext("2d");
      }
      if (this.pngCanvas.width !== dw) this.pngCanvas.width = dw;
      if (this.pngCanvas.height !== dh) this.pngCanvas.height = dh;
      this.pngCtx.drawImage(source, sx, sy, sw, sh, 0, 0, dw, dh);
      let url;
      try {
        url = this.pngCanvas.toDataURL("image/png");
      } catch (e) {
        if (this.mode === "video") this.fastPathDead = "tainted";
        return { err: "tainted" };
      }
      return { b64: url.slice(url.indexOf(",") + 1), w: dw, h: dh };
    }

    /* ---- getDisplayMedia fallback ---------------------------------------- */

    async enableFallback() {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 15 },
        audio: false,
      });
      const fv = document.createElement("video");
      fv.muted = true;
      fv.playsInline = true;
      fv.srcObject = stream;
      fv.style.cssText =
        "position:fixed;left:0;top:0;width:2px;height:2px;opacity:0;pointer-events:none;";
      document.documentElement.appendChild(fv);
      await fv.play();
      if (!fv.videoWidth) {
        await new Promise((res) => fv.addEventListener("loadedmetadata", res, { once: true }));
      }
      this.fbStream = stream;
      this.fbVideo = fv;
      this.fbMap = this._calibrate(fv.videoWidth, fv.videoHeight);
      this.mode = "display";
      stream.getVideoTracks()[0].addEventListener("ended", () => {
        this._teardownFallback();
        if (this.onFallbackEnded) this.onFallbackEnded();
      });
      return this.fbMap.mode;
    }

    _calibrate(sw, sh) {
      const dpr = devicePixelRatio || 1;
      const candidates = [
        { mode: "screen", w: screen.width * dpr },
        { mode: "window", w: window.outerWidth * dpr },
        { mode: "viewport", w: innerWidth * dpr },
      ];
      candidates.forEach((c) => (c.diff = Math.abs(sw - c.w) / c.w));
      candidates.sort((a, b) => a.diff - b.diff);
      const mode = candidates[0].mode;

      const innerSX = window.mozInnerScreenX ?? 0;
      const innerSY = window.mozInnerScreenY ?? 0;

      if (mode === "screen") {
        const left = typeof screen.left === "number" ? screen.left : (screen.availLeft || 0);
        const top = typeof screen.top === "number" ? screen.top : (screen.availTop || 0);
        return {
          mode,
          kx: sw / screen.width,
          ky: sh / screen.height,
          offX: innerSX - left,
          offY: innerSY - top,
        };
      }
      if (mode === "window") {
        return {
          mode,
          kx: sw / window.outerWidth,
          ky: sh / window.outerHeight,
          offX: innerSX - window.screenX,
          offY: innerSY - window.screenY,
        };
      }
      return { mode, kx: sw / innerWidth, ky: sh / innerHeight, offX: 0, offY: 0 };
    }

    _teardownFallback() {
      if (this.fbStream) this.fbStream.getTracks().forEach((t) => t.stop());
      if (this.fbVideo) this.fbVideo.remove();
      this.fbStream = null;
      this.fbVideo = null;
      this.fbMap = null;
      this.mode = "video";
    }

    stop() {
      this._teardownFallback();
    }
  }

  CDT.Capture = Capture;
  CDT.findVideos = findVideos;
}
