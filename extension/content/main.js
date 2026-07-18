/* Glue: toolbar toggle -> App { capture -> analyze -> detector -> panel }, plus,
 * on each line, capture 3 spaced frames -> sidecar -> overlay.
 *
 * The extension stays dumb on purpose (CLAUDE.md): it owns capture, the crop
 * box, and drop-don't-queue; it does NOT know how OCR/translation happen. All it
 * sees of the service is CDT.Service.translate() and a contract-shaped reply.
 */

"use strict";

if (!window.CDT.__mainLoaded) {
  window.CDT.__mainLoaded = true;
  const CDT = window.CDT;
  const { log, toast, rateLimit } = CDT.util;

  const DEFAULT_BOX = { x: 0.15, y: 0.8, w: 0.7, h: 0.12 };
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  let app = null;

  class App {
    constructor(videos) {
      this.cfg = { ...CDT.defaultConfig };
      this.videos = videos;
      this.videoIdx = 0;
      this.box = { ...DEFAULT_BOX };
      this.paused = false;
      this.blackStreak = 0;
      this.fpsEma = null;
      this.lastTick = 0;
      this.warned = {};

      // Translation / drop-don't-queue state.
      this.frameCounter = 0;      // the contract's frame_id source
      this.lineToken = 0;         // bumps only when a NEWER line supersedes; a
                                  // returning translation whose token != current
                                  // has been superseded and is dropped
      this.lastShippedText = "";  // source (hanzi) last displayed -> dedup
      this.lastTranslation = "";  // cached translation for the current line
      this.sourceHistory = [];    // rolling recent SOURCE lines -> context_lines (6b)
      this.sentenceLines = [];    // SOURCE lines of the in-progress sentence (6a)
      this.sentenceHanzi = 0;     // total hanzi in the in-progress sentence
      this.lastLineEndAt = 0;     // when the previous line ended (continuation gap)
      this.overlayHanzi = 0;      // source hanzi behind the current overlay (hold)
      this.overlayFrameId = null; // frame_id of the line the overlay is showing (audit)
      this.overlayVideoTime = 0;  // video position when the overlay appeared (audit)
      this.lastLineDurMs = 0;     // how long the last line was actually on screen
      this.overlayShownAt = 0;    // when the current overlay content appeared
      this.overlayClearTimer = null;
      this.svc = "off";           // "off" | "ok" | "down" | "…"

      this.capture = new CDT.Capture(this.cfg);
      this.capture.setVideo(videos[0]);
      this.capture.onFallbackEnded = () => {
        this._log("screen capture ended — back to <video> fast path", "info");
        this._resetLine();
      };

      this.detector = new CDT.Detector(this.cfg, (t, i) => this._onEvent(t, i));
      this.overlay = new CDT.Overlay(this.cfg);
      this.overlay.setBilingual(this.cfg.overlayBilingual);

      this.panel = new CDT.Panel(this.cfg, {
        getContentRect: () => this.capture.contentRect(),
        onDrawBox: (frac) => {
          this.box = frac;
          this._resetLine();
          this._log(
            `box set: x=${frac.x.toFixed(2)} y=${frac.y.toFixed(2)} ` +
              `w=${frac.w.toFixed(2)} h=${frac.h.toFixed(2)} (of player)`,
            "info"
          );
        },
        onResetBox: () => {
          this.box = { ...DEFAULT_BOX };
          this._resetLine();
          this._log("box reset to default bottom band", "info");
        },
        onCycleVideo: () => this._cycleVideo(),
        onPause: (paused) => {
          this.paused = paused;
          if (paused) {
            clearTimeout(this.overlayClearTimer);
            this.overlay.clear();
            this.panel.update({ state: "paused" });
          }
        },
        onClose: () => destroyApp(),
        onFallback: () => this._enableFallback(),
        onToggleTranslate: (on) => {
          this.cfg.translateEnabled = on;
          if (!on) {
            this.overlay.clear();
            this.svc = "off";
          }
          this._log(`translation ${on ? "on" : "off"}`, "info");
        },
        onTargetLang: (lang) => {
          this.cfg.targetLang = lang;
          this._resetLine();
          this._log(`target language -> ${lang}`, "info");
        },
        onBilingual: (on) => {
          this.cfg.overlayBilingual = on;
          this.overlay.setBilingual(on);
        },
      });

      this._logStable = rateLimit(2000, (hex, durS, px) =>
        this._log(`stable #${hex.slice(0, 8)} ${durS}s px=${px}`, "stable")
      );
      this._logReject = rateLimit(1500, (reason, px) =>
        this._log(`rejected not-text (${reason}) px=${px}`, "reject")
      );
      this._svcWarn = rateLimit(4000, (err, detail) =>
        this._log(
          `sidecar ${err}${detail ? " " + detail : ""} — running? (cd sidecar && ./run.sh)`,
          "reject"
        )
      );

      this.timer = setInterval(() => this._tick(), this.cfg.sampleMs);
      this._log("detector started — draw a tight box hugging the subtitle line", "info");
    }

    _log(line, cls) {
      log(line);
      this.panel.log(line, cls);
    }

    _warnOnce(key, line, cls) {
      if (this.warned[key]) return;
      this.warned[key] = true;
      this._log(line, cls || "info");
    }

    /* A line boundary that the detector can't see (box/video/lang change): drop
     * any showing overlay and forget dedup/cache so nothing stale carries over. */
    _resetLine() {
      clearTimeout(this.overlayClearTimer);
      this.overlayClearTimer = null;
      this._finalizeOverlay("cleared");
      this.detector.reset();
      this.overlay.clear();
      this.lineToken++;
      this.lastShippedText = "";
      this.lastTranslation = "";
      this.sourceHistory = [];  // box/video/lang change is a genuine context break
      this.sentenceLines = [];
      this.sentenceHanzi = 0;
      this.lastLineEndAt = 0;
    }

    _videoLabel() {
      const v = this.capture.video;
      if (!v) return "lost";
      const multi = this.videos.length > 1 ? ` (${this.videoIdx + 1}/${this.videos.length})` : "";
      return `${v.videoWidth}×${v.videoHeight}${multi}`;
    }

    _cycleVideo() {
      this.videos = CDT.findVideos();
      if (!this.videos.length) {
        this._log("no videos found", "reject");
        return;
      }
      this.videoIdx = (this.videoIdx + 1) % this.videos.length;
      this.capture.setVideo(this.videos[this.videoIdx]);
      this._resetLine();
      const cr = this.capture.contentRect();
      if (cr) this.panel.flashRect(cr);
      this._log(`video ${this.videoIdx + 1}/${this.videos.length}: ${this._videoLabel()}`, "info");
    }

    async _enableFallback() {
      try {
        const mode = await this.capture.enableFallback();
        this.panel.showFallbackButton(false);
        this._resetLine();
        this._log(
          `screen-capture fallback active (${mode} mapping) — keep page zoom at 100%`,
          "info"
        );
        if (mode !== "viewport") {
          this._log("share this window or its screen for correct crop mapping", "info");
        }
      } catch (e) {
        this._log(`screen capture not started: ${e.name}`, "reject");
      }
    }

    _refindVideo() {
      const vids = CDT.findVideos();
      if (vids.length) {
        this.videos = vids;
        this.videoIdx = 0;
        this.capture.setVideo(vids[0]);
        this._resetLine();
        this._log("re-acquired video element", "info");
      }
    }

    _tick() {
      const now = performance.now();
      if (this.lastTick) {
        const inst = 1000 / (now - this.lastTick);
        this.fpsEma = this.fpsEma ? this.fpsEma * 0.8 + inst * 0.2 : inst;
      }
      this.lastTick = now;

      // Box indicator + overlay follow the player bbox every tick (survives
      // resize, scroll and fullscreen with no per-platform code).
      const cr = this.capture.contentRect();
      this.panel.setBoxIndicator(
        cr
          ? {
              x: cr.x + this.box.x * cr.w,
              y: cr.y + this.box.y * cr.h,
              w: this.box.w * cr.w,
              h: this.box.h * cr.h,
            }
          : null
      );
      this.overlay.reposition(cr);

      if (this.paused) return;
      this.frameCounter++;

      const res = this.capture.grab(this.box);
      const base = {
        fps: this.fpsEma,
        mode:
          this.capture.mode === "display"
            ? `display/${this.capture.fbMap.mode}`
            : "video",
        videoLabel: this._videoLabel(),
        svc: this.cfg.translateEnabled ? this.svc : "off",
      };

      if (res.err) {
        this._handleGrabError(res, base);
        return;
      }
      this.blackStreak = 0;

      const sample = CDT.analyzeFrame(res.img, this.cfg);
      this.detector.push(sample, now);

      this.panel.renderDebug(this.capture.canvas, sample);
      this.panel.update({
        ...base,
        state: this.detector.displayState(sample.cls),
        px: sample.count,
        clusters: sample.clusterCount,
        hex:
          this.detector.state === "active" && this.detector.current
            ? this.detector.current.hex
            : sample.hex,
        dCur: sample.dCur,
        floor: sample.floor,
      });

      if (sample.cls === "not_text") {
        this._logReject(sample.reason, sample.count);
      } else if (this.detector.state === "active" && sample.cls === "text") {
        this._logStable(
          this.detector.current.hex,
          ((now - this.detector.current.startedAt) / 1000).toFixed(1),
          sample.count
        );
      }

      if (this.cfg.verbose) {
        log(
          `sample cls=${sample.cls}${sample.reason ? "/" + sample.reason : ""} ` +
            `px=${sample.count} cl=${sample.clusterCount} dCur=${
              sample.dCur != null && isFinite(sample.dCur) ? sample.dCur : "–"
            } state=${this.detector.displayState(sample.cls)}`
        );
      }
    }

    _handleGrabError(res, base) {
      switch (res.err) {
        case "novideo":
          this._refindThrottled = this._refindThrottled || rateLimit(1000, () => this._refindVideo());
          this._refindThrottled();
          this.panel.update({ ...base, state: "error", videoLabel: "lost" });
          break;
        case "notready":
          this.panel.update({ ...base, state: "idle" });
          break;
        case "tainted":
          this._warnOnce(
            "tainted",
            "canvas tainted (cross-origin video without CORS) — use the screen-capture fallback",
            "reject"
          );
          this.panel.showFallbackButton(true);
          this.panel.update({ ...base, state: "error" });
          break;
        case "black":
          this.blackStreak++;
          if (res.img) this.panel.renderDebug(this.capture.canvas, null);
          if (this.blackStreak >= 5) {
            this._warnOnce(
              "black",
              "crop is uniformly black. DRM content is out of scope by design; " +
                "for in-the-clear video, try the screen-capture fallback",
              "reject"
            );
            this.panel.showFallbackButton(true);
          }
          this.panel.update({ ...base, state: "black" });
          break;
        case "badbox":
          this._warnOnce("badbox", "box is too small — redraw it", "reject");
          this.panel.update({ ...base, state: "error" });
          break;
      }
    }

    _onEvent(type, info) {
      if (type === "line-start") {
        const token = ++this.lineToken;
        const cont = this._detectContinuation(performance.now());
        // Always cancel a pending reading-hold clear. On a NON-continuation line,
        // also take the previous translation down immediately (6b) and start a
        // fresh sentence. On a continuation, keep the previous fragment showing
        // and revise it in place when the combined translation lands (6a).
        clearTimeout(this.overlayClearTimer);
        this.overlayClearTimer = null;
        if (!cont) {
          this._finalizeOverlay("preempted"); // a new line took the screen early
          this.overlay.clear();
          this.sentenceLines = [];
          this.sentenceHanzi = 0;
        }
        this._log(
          `LINE START #${info.hex.slice(0, 8)} px=${info.count} cl=${info.clusters}` +
            (cont ? " ↳cont" : ""),
          "start"
        );
        if (this.cfg.translateEnabled) this._dispatchTranslate(info, token, cont);
      } else if (type === "line-end") {
        // Do NOT bump lineToken here. A line merely ending (into silence) must
        // not drop its own in-flight translation — only a *newer* line
        // supersedes (bumped in line-start). This is what lets a sub-0.5s line
        // still render: its translation returns after LINE END but is still the
        // latest thing said, so it shows.
        this.lastLineDurMs = info.durMs;
        this.lastLineEndAt = performance.now();  // continuation-gap reference (6a)
        this._log(`LINE END (${info.reason}) after ${(info.durMs / 1000).toFixed(1)}s`, "end");
        if (info.reason === "empty") this._log("idle (silence)", "idle");
        // Hold a showing overlay for a readable minimum (scaled by the source
        // hanzi behind it) before clearing on silence (§3a) — don't yank it off
        // on very short lines.
        if (this.overlay.isShowing()) {
          this._armOverlayClear(this._overlayHoldMs(this.overlayHanzi, info.durMs));
        }
      }
    }

    /* Grab 3 spaced frames for this line, POST to the sidecar, and overlay the
     * result — unless the line has moved on by the time we return. */
    /* Does the incoming line complete the previous one (split sentence, 6a)?
     * Cheap signals only: we're mid-sentence, the previous source line didn't
     * end in terminal punctuation, and the gap since it ended was short. The
     * service (with the model) does the actual combining. */
    _detectContinuation(now) {
      const prev = this.sourceHistory.length
        ? this.sourceHistory[this.sourceHistory.length - 1]
        : "";
      if (!prev || this.sentenceLines.length === 0) return false;
      if (this.sentenceLines.length >= this.cfg.sentenceMaxLines) return false;
      // Terminal punctuation OR a sentence-final particle (吗/吧/呢/嘛) closes an
      // utterance even when subs carry no punctuation — so a question like
      // 你爱我吗 doesn't bridge onto its answer. (The model is the backstop for
      // turns these cheap signals miss.)
      if (/[。！？!?…吗吧呢嘛]$/.test(prev)) return false;
      const gap = this.lastLineEndAt ? now - this.lastLineEndAt : Infinity;
      return gap <= this.cfg.continuationGapMs;
    }

    async _dispatchTranslate(info, token, cont) {
      const frameId = this.frameCounter;
      const frames = [];
      for (let i = 0; i < 3; i++) {
        if (this.lineToken !== token) return;                  // superseded -> abandon
        if (i > 0 && this.detector.state !== "active") break;  // line gone -> send what we have
        const png = this.capture.grabCropPng(this.box, this.cfg.pngMaxW);
        if (png.b64) frames.push(png.b64);
        else if (!frames.length) return;                       // couldn't capture anything
        if (i < 2) await delay(this.cfg.frameSpacingMs);
      }
      if (!frames.length || this.lineToken !== token) return;

      this.svc = "…";
      const res = await CDT.Service.translate({
        frames,
        sourceLang: "ch",
        targetLang: this.cfg.targetLang,
        frameId,
        lastShippedText: this.lastShippedText,
        // Continuation: hand the service the sentence so far to combine (6a);
        // otherwise the last 2-3 lines as reference (6b).
        contextLines: cont ? this.sentenceLines.slice(-3) : this.sourceHistory.slice(-3),
        continuation: cont,
        contextNote: this.cfg.contextNote || "",  // optional show/episode background
        tone: this.cfg.tone || "",                // optional register lean
        label: document.title || location.hostname || "", // episode grouping in the audit log
        videoTime: (this.capture.video && this.capture.video.currentTime) || 0,
      });

      // Drop-don't-queue (invariant 3): drop ONLY if a newer line superseded this
      // one (token advanced). A line that merely ended into silence keeps its
      // translation — it is still the latest thing said.
      if (this.lineToken !== token) {
        if (res.ok) this.svc = "ok";
        // Translated fine but a newer line superseded it before it could show —
        // exactly the kind of gap worth auditing.
        CDT.Service.logDisplay({
          frame_id: frameId, video_time: 0, visible_ms: 0, outcome: "dropped",
          // the good translation that never got shown (superseded) — worth having
          final_text: (res.ok && res.data && res.data.translation ? res.data.translation : "").slice(0, 500),
          label: (document.title || "").slice(0, 200),
        });
        this._log("dropped stale translation (superseded by newer line)", "info");
        return;
      }

      if (!res.ok) {
        this.svc = "down";
        this._svcWarn(res.error, res.detail);
        return;
      }
      this.svc = "ok";
      const d = res.data;
      if (d.status === "ok") {
        this.lastShippedText = d.source_text;
        this.lastTranslation = d.translation;
        if (d.source_text) {
          // Grow the backward context (6b) — SOURCE only — and the in-progress
          // sentence (6a) so the next line can be combined onto it.
          this.sourceHistory.push(d.source_text);
          if (this.sourceHistory.length > 4) this.sourceHistory.shift();
          this.sentenceLines.push(d.source_text);
          this.sentenceHanzi += [...d.source_text].length;
        }
        // Hold scales by the source behind what's shown: the whole sentence on a
        // continuation (the overlay now holds the combined translation), else
        // just this line.
        const hanzi = cont ? this.sentenceHanzi : [...(d.source_text || "")].length;
        this._showOverlay({ source: d.source_text, translation: d.translation }, token, hanzi, frameId);
        this._log(`OVERLAY${cont ? " (revised)" : ""} [${this.cfg.targetLang}] ${d.translation}`, "start");
      } else if (d.status === "duplicate") {
        // Same line still up — re-show the cached translation instead of
        // re-translating (dedup as intended, §5).
        if (this.lastTranslation) {
          this._showOverlay(
            { source: d.source_text || this.lastShippedText, translation: this.lastTranslation },
            token,
            [...(d.source_text || this.lastShippedText || "")].length,
            frameId
          );
        }
        this._log(`duplicate — kept "${this.lastTranslation}"`, "info");
      } else if (d.status === "low_confidence") {
        this._log(`low_confidence (${d.source_text || "—"}) — not shown`, "reject");
      } else {
        this._log("service: no_text", "info");
      }
    }

    /* Show an overlay and start its readable-lifetime clock. If the line is
     * already over when its translation lands (short line, or slow round-trip),
     * arm the clear now; otherwise the line is still active and line-end arms it.
     * hanziCount is the SOURCE length behind the shown text (a whole sentence on
     * a continuation), which drives the hold. */
    _showOverlay(content, token, hanziCount, frameId) {
      // A lingering overlay (continuation revise) is being replaced — record it.
      this._finalizeOverlay("revised");
      this.overlay.show(content);
      this.overlayShownAt = performance.now();
      this.overlayHanzi = hanziCount || 0;
      this.overlayFrameId = frameId != null ? frameId : null;
      this.overlayVideoTime = (this.capture.video && this.capture.video.currentTime) || 0;
      this.overlayText = (content && content.translation) || ""; // final shown text for this frame_id
      clearTimeout(this.overlayClearTimer);
      this.overlayClearTimer = null;
      if (this.detector.state !== "active") {
        this._armOverlayClear(this._overlayHoldMs(this.overlayHanzi, this.lastLineDurMs));
      }
    }

    /* Emit a display-outcome audit event for the line currently on the overlay
     * (if any), then forget it. Fire-and-forget; never blocks render. */
    _finalizeOverlay(outcome) {
      if (this.overlayFrameId == null) return;
      CDT.Service.logDisplay({
        frame_id: this.overlayFrameId,
        video_time: this.overlayVideoTime,
        visible_ms: Math.round(performance.now() - this.overlayShownAt),
        outcome,
        final_text: (this.overlayText || "").slice(0, 500),
        label: (document.title || "").slice(0, 200),
      });
      this.overlayFrameId = null;
    }

    /* Readable overlay lifetime, scaled to the SOURCE (hanzi) length so one rate
     * works for every target language with no per-language table (6b): floored
     * so a 1-3 hanzi line isn't a flash, ceilinged so a long or garbled line
     * can't pin the overlay, and never shorter than the subtitle was on screen
     * itself (so the overlay and the burned-in hanzi stay aligned). */
    _overlayHoldMs(hanziCount, durMs) {
      const reading = (hanziCount || 0) * this.cfg.readMsPerHanzi;
      const readable = Math.min(this.cfg.overlayMaxMs, Math.max(this.cfg.overlayMinMs, reading));
      return Math.max(readable, durMs || 0);
    }

    /* Clear the overlay holdMs after it appeared — unless a new line has taken
     * over the screen by then, in which case that line owns the overlay and its
     * own translation will replace this one. Bounded and non-accumulating: a
     * newer line always wins immediately, so lag never compounds (invariant 3). */
    _armOverlayClear(holdMs) {
      clearTimeout(this.overlayClearTimer);
      const remaining = Math.max(0, holdMs - (performance.now() - this.overlayShownAt));
      this.overlayClearTimer = setTimeout(() => {
        this.overlayClearTimer = null;
        if (this.detector.state !== "active") {
          this._finalizeOverlay("expired"); // reading-hold elapsed, natural clear
          this.overlay.clear();
        }
      }, remaining);
    }

    destroy() {
      this.lineToken++; // make any in-flight translation drop instead of touching torn-down UI
      clearTimeout(this.overlayClearTimer);
      clearInterval(this.timer);
      this.capture.stop();
      this.overlay.destroy();
      this.panel.destroy();
    }
  }

  function destroyApp() {
    if (app) {
      app.destroy();
      app = null;
      log("detector stopped");
    }
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.type !== "cdt-toggle") return;
    if (app) {
      destroyApp();
      return;
    }
    const videos = CDT.findVideos();
    if (!videos.length) {
      if (window.top === window) {
        toast(
          "CDT: no playable <video> in the top frame. If the player lives in an iframe, its panel opens there."
        );
      }
      return;
    }
    app = new App(videos);
  });
}
