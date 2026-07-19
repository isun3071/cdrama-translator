/* Debug panel + crop-box UI. Session 1's whole point: watch the mask live.
 * Lives in a shadow root so page CSS can't touch it and ours can't leak. */

"use strict";

if (!window.CDT.Panel) {
  const CDT = window.CDT;
  const { timeTag, clamp } = CDT.util;

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    .panel {
      position: fixed; top: 12px; right: 12px; width: 380px;
      background: rgba(16,17,22,.94); color: #d8dae0;
      font: 11px/1.45 ui-monospace, Menlo, Consolas, monospace;
      border: 1px solid #2c2f3a; border-radius: 8px;
      box-shadow: 0 6px 24px rgba(0,0,0,.55);
      pointer-events: auto; user-select: none; z-index: 3;
    }
    .hdr {
      display: flex; align-items: center; gap: 8px;
      padding: 7px 10px; cursor: move;
      border-bottom: 1px solid #2c2f3a;
    }
    .hdr .title { font-weight: 700; color: #fff; }
    .chip {
      padding: 1px 8px; border-radius: 9px; font-weight: 700;
      background: #333; color: #ccc; text-transform: uppercase; font-size: 10px;
    }
    .chip.idle     { background: #2a2d38; color: #8a93a8; }
    .chip.pending  { background: #4d4420; color: #e8c95a; }
    .chip.active   { background: #1e4023; color: #6fe08a; }
    .chip.changing { background: #4d3420; color: #f0a35a; }
    .chip.reject   { background: #4a2026; color: #f07a8a; }
    .chip.error    { background: #4a2026; color: #f07a8a; }
    .chip.black    { background: #2d2440; color: #a98af0; }
    .chip.paused   { background: #26333f; color: #6ab7e8; }
    .spacer { flex: 1; }
    .x { cursor: pointer; color: #778; padding: 0 4px; font-size: 14px; }
    .x:hover { color: #fff; }
    .body { padding: 8px 10px; }
    .cv-label { color: #667; margin: 4px 0 2px; font-size: 10px; }
    canvas { display: block; width: 100%; image-rendering: pixelated;
             background: #000; border: 1px solid #2c2f3a; border-radius: 3px; }
    .stats {
      display: grid; grid-template-columns: repeat(4, 1fr);
      gap: 2px 8px; margin: 8px 0;
    }
    .stats div { white-space: nowrap; }
    .stats b { color: #fff; font-weight: 600; }
    .stats .k { color: #667; }
    .rows { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
    button {
      background: #262a36; color: #cfd3dd; border: 1px solid #3a3f4f;
      border-radius: 5px; padding: 4px 9px; cursor: pointer;
      font: inherit;
    }
    button:hover { background: #323748; color: #fff; }
    button.warn { background: #4a3420; border-color: #6b4a2a; color: #f0b06a; }
    button.on { background: #1e4023; border-color: #2f6b3e; color: #8fe0a2; }
    select.sel { background: #262a36; color: #cfd3dd; border: 1px solid #3a3f4f;
                 border-radius: 5px; padding: 3px 6px; font: inherit; cursor: pointer; }
    .ctxnote { margin: 6px 0 2px; }
    textarea.ctxta {
      width: 100%; background: #0b0c10; color: #cfd3dd;
      border: 1px solid #3a3f4f; border-radius: 5px; padding: 5px 7px;
      font: inherit; resize: vertical; user-select: text; min-height: 34px;
    }
    textarea.ctxta:focus { outline: none; border-color: #5a7de8; }
    .ctxbar { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
    .ctxstatus { color: #667; font-size: 10px; }
    .ctxstatus.dirty { color: #e8c95a; }
    button:disabled { opacity: .5; cursor: default; }
    button:disabled:hover { background: #262a36; color: #cfd3dd; }
    label.chkbox { display: flex; align-items: center; gap: 4px; color: #99a; cursor: pointer; }
    .slider { display: flex; align-items: center; gap: 7px; margin: 4px 0; }
    .slider label { width: 88px; color: #99a; }
    .slider input[type=range] { flex: 1; accent-color: #5a7de8; }
    .slider .val { width: 30px; text-align: right; color: #fff; }
    .verbose { display: flex; align-items: center; gap: 6px; margin: 6px 0 2px; color: #99a; }
    .log {
      margin-top: 6px; padding: 6px 8px; height: 148px; overflow-y: auto;
      background: #0b0c10; border: 1px solid #23252e; border-radius: 4px;
      white-space: pre-wrap; word-break: break-all; user-select: text;
    }
    .log div { margin-bottom: 1px; }
    .ev-start  { color: #6fe08a; }
    .ev-end    { color: #f0a35a; }
    .ev-idle   { color: #8a93a8; }
    .ev-reject { color: #f07a8a; }
    .ev-stable { color: #5aa8e8; }
    .ev-info   { color: #aab; }
    .boxind {
      position: fixed; border: 2px dashed #46e06a; border-radius: 2px;
      box-shadow: 0 0 0 1px rgba(0,0,0,.6); pointer-events: none;
      display: none; z-index: 1;
    }
    .drawlayer {
      position: fixed; inset: 0; cursor: crosshair; display: none;
      background: rgba(10,10,20,.25); pointer-events: auto; z-index: 2;
    }
    .drawlayer .hint {
      position: absolute; top: 14px; left: 50%; transform: translateX(-50%);
      background: rgba(16,17,22,.92); padding: 6px 12px; border-radius: 6px;
      color: #eee; font: 12px system-ui, sans-serif; pointer-events: none;
    }
    .drawrect {
      position: fixed; border: 2px solid #e8c95a;
      background: rgba(232,201,90,.12); display: none;
    }
    .flash {
      position: fixed; border: 3px solid #5a7de8; border-radius: 4px;
      pointer-events: none; transition: opacity .7s; z-index: 1;
    }
  `;

  class Panel {
    /* cb: { getContentRect, onDrawBox, onResetBox, onCycleVideo,
     *       onPause, onClose, onFallback } */
    constructor(cfg, cb) {
      this.cfg = cfg;
      this.cb = cb;
      this.paused = false;
      this.logLines = [];

      this.host = document.createElement("div");
      this.host.style.cssText =
        "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
      this.root = this.host.attachShadow({ mode: "open" });

      const style = document.createElement("style");
      style.textContent = CSS;
      this.root.appendChild(style);

      this.root.appendChild(this._buildPanel());
      this.boxind = this._el("div", "boxind");
      this.root.appendChild(this.boxind);
      this.root.appendChild(this._buildDrawLayer());

      this._attach();
      this._onFsChange = () => this._attach();
      document.addEventListener("fullscreenchange", this._onFsChange);
    }

    _el(tag, cls, text) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }

    _buildPanel() {
      const p = this._el("div", "panel");

      const hdr = this._el("div", "hdr");
      hdr.appendChild(this._el("span", "title", "CDT detector"));
      this.chip = this._el("span", "chip idle", "idle");
      hdr.appendChild(this.chip);
      hdr.appendChild(this._el("span", "spacer"));
      this.minBtn = this._el("span", "x", "–");
      this.minBtn.title = "minimize";
      this.minBtn.addEventListener("click", () => this._toggleMin());
      hdr.appendChild(this.minBtn);
      const x = this._el("span", "x", "✕");
      x.addEventListener("click", () => this.cb.onClose());
      hdr.appendChild(x);
      p.appendChild(hdr);
      this._makeDraggable(p, hdr);

      const body = this._el("div", "body");

      body.appendChild(this._el("div", "cv-label", "crop"));
      this.cropCv = document.createElement("canvas");
      this.cropCv.width = 384; this.cropCv.height = 36;
      body.appendChild(this.cropCv);

      body.appendChild(this._el("div", "cv-label", "text mask (clusters in green)"));
      this.maskCv = document.createElement("canvas");
      this.maskCv.width = 384; this.maskCv.height = 36;
      body.appendChild(this.maskCv);

      const stats = this._el("div", "stats");
      this.stat = {};
      for (const [key, label] of [
        ["px", "px"], ["cl", "cl"], ["hash", "hash"], ["dcur", "Δcur"],
        ["fps", "fps"], ["mode", "mode"], ["video", "video"], ["floor", "floor"],
        ["svc", "svc"],
      ]) {
        const d = this._el("div");
        d.innerHTML = `<span class="k">${label}</span> <b>–</b>`;
        this.stat[key] = d.querySelector("b");
        stats.appendChild(d);
      }
      body.appendChild(stats);

      const rows = this._el("div", "rows");
      const mkBtn = (label, fn, cls) => {
        const b = this._el("button", cls || "", label);
        b.addEventListener("click", fn);
        rows.appendChild(b);
        return b;
      };
      mkBtn("Draw box", () => this.startDraw());
      mkBtn("Reset box", () => this.cb.onResetBox());
      mkBtn("Video ▸", () => this.cb.onCycleVideo());
      this.pauseBtn = mkBtn("Pause", () => {
        this.paused = !this.paused;
        this.pauseBtn.textContent = this.paused ? "Resume" : "Pause";
        this.cb.onPause(this.paused);
      });
      this.captureBtn = mkBtn("Capture ✗", () => {
        this.captureOn = !this.captureOn;
        this.captureBtn.textContent = this.captureOn ? "Capture ✓" : "Capture ✗";
        this.captureBtn.classList.toggle("on", this.captureOn);
        this.cb.onCaptureMode(this.captureOn);
      });
      body.appendChild(rows);

      // Translation controls (session 3).
      const trow = this._el("div", "rows");
      this.translateBtn = document.createElement("button");
      const paintTr = () => {
        this.translateBtn.textContent = this.cfg.translateEnabled ? "Translate ✓" : "Translate ✗";
        this.translateBtn.classList.toggle("on", !!this.cfg.translateEnabled);
      };
      this.translateBtn.addEventListener("click", () => {
        this.cb.onToggleTranslate(!this.cfg.translateEnabled);
        paintTr();
      });
      paintTr();
      trow.appendChild(this.translateBtn);

      const langSel = document.createElement("select");
      langSel.className = "sel";
      langSel.title = "target language";
      for (const [code, name] of [
        ["en", "English"], ["es", "Español"], ["pt", "Português"],
        ["id", "Indonesia"], ["ar", "العربية"], ["hi", "हिन्दी"], ["fr", "Français"],
      ]) {
        const o = document.createElement("option");
        o.value = code;
        o.textContent = name;
        if (code === this.cfg.targetLang) o.selected = true;
        langSel.appendChild(o);
      }
      langSel.addEventListener("change", () => this.cb.onTargetLang(langSel.value));
      trow.appendChild(langSel);

      // Register lean (feature 3): a default/tie-breaker, not a costume — the service
      // never overrides a register the source itself sets. "" = faithful (default).
      const toneSel = document.createElement("select");
      toneSel.className = "sel";
      toneSel.title = "register lean (never overrides the source's own tone)";
      for (const [code, name] of [
        ["", "tone: auto"], ["casual", "casual"], ["formal", "formal"],
        ["literary", "literary"], ["playful", "playful"], ["romantic", "romantic"],
        ["business", "business"],
      ]) {
        const o = document.createElement("option");
        o.value = code;
        o.textContent = name;
        if (code === (this.cfg.tone || "")) o.selected = true;
        toneSel.appendChild(o);
      }
      toneSel.addEventListener("change", () => { this.cfg.tone = toneSel.value; });
      trow.appendChild(toneSel);

      const biLabel = this._el("label", "chkbox");
      biLabel.title = "bilingual overlay (show source too)";
      const bi = document.createElement("input");
      bi.type = "checkbox";
      bi.checked = !!this.cfg.overlayBilingual;
      bi.addEventListener("change", () => this.cb.onBilingual(bi.checked));
      biLabel.appendChild(bi);
      biLabel.appendChild(document.createTextNode("bilingual"));
      trow.appendChild(biLabel);
      body.appendChild(trow);

      // Show/episode context (optional). Edited as a DRAFT; an explicit Save commits
      // it to cfg.contextNote, so an accidental edit never changes what's sent until
      // you click Save. The saved note is injected server-side into the cached system
      // prefix (prefilled once per session, not re-billed per line). Save also logs a
      // confirmation, so you can see it went active.
      const ctxWrap = this._el("div", "ctxnote");
      ctxWrap.appendChild(this._el("div", "cv-label", "show / episode context (optional)"));
      this.ctxNote = document.createElement("textarea");
      this.ctxNote.className = "ctxta";
      this.ctxNote.rows = 3;
      this.ctxNote.maxLength = 10000;
      this.ctxNote.placeholder =
        "Paste the show + episode summary, key character names (傅正川=Fu Zhengchuan), register…";
      this.ctxNote.value = this.cfg.contextNote || "";
      ctxWrap.appendChild(this.ctxNote);

      const ctxBar = this._el("div", "ctxbar");
      this.ctxSaveBtn = this._el("button", "", "Save context");
      this.ctxStatus = this._el("span", "ctxstatus");
      const paintCtx = () => {
        const draft = this.ctxNote.value;
        const dirty = draft !== (this.cfg.contextNote || "");
        this.ctxSaveBtn.disabled = !dirty;
        this.ctxSaveBtn.classList.toggle("on", dirty);
        this.ctxStatus.textContent =
          (dirty ? "unsaved" : draft ? "saved" : "empty") + ` · ${draft.length}/10000`;
        this.ctxStatus.classList.toggle("dirty", dirty);
      };
      this.ctxNote.addEventListener("input", paintCtx);
      this.ctxSaveBtn.addEventListener("click", () => {
        this.cfg.contextNote = this.ctxNote.value;               // only Save commits it
        paintCtx();
        this.log(`context saved (${this.ctxNote.value.length} chars) — active from the next line`, "info");
      });
      ctxBar.appendChild(this.ctxSaveBtn);
      ctxBar.appendChild(this.ctxStatus);
      ctxWrap.appendChild(ctxBar);
      paintCtx();
      body.appendChild(ctxWrap);

      // Re-watch replay: load a personal track (built by track.py) and play its
      // corrected translations against the video's currentTime — no OCR/sidecar.
      const rrow = this._el("div", "rows");
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = ".json,application/json";
      fileInput.style.display = "none";
      fileInput.addEventListener("change", () => {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        const reader = new FileReader();
        reader.onload = () => {
          let track = null;
          try { track = JSON.parse(reader.result); } catch (e) { /* handled below */ }
          const n = this.cb.onLoadTrack(track);
          this.replayStatus.textContent = n >= 0 ? `track: ${n} cues` : "track: invalid";
          fileInput.value = "";
        };
        reader.readAsText(f);
      });
      this.root.appendChild(fileInput);
      const loadBtn = this._el("button", "", "Load track");
      loadBtn.addEventListener("click", () => fileInput.click());
      rrow.appendChild(loadBtn);
      this.replayBtn = this._el("button", "", "Replay ✗");
      this.replayBtn.addEventListener("click", () => {
        const on = this.cb.onReplay(!this.replayOn);   // main returns the actual new state
        this.replayOn = !!on;
        this.replayBtn.textContent = this.replayOn ? "Replay ✓" : "Replay ✗";
        this.replayBtn.classList.toggle("on", this.replayOn);
      });
      rrow.appendChild(this.replayBtn);
      this.replayStatus = this._el("span", "ctxstatus", "no track");
      rrow.appendChild(this.replayStatus);
      body.appendChild(rrow);

      this.fbBtn = this._el("button", "warn", "Enable screen-capture fallback");
      this.fbBtn.style.display = "none";
      this.fbBtn.style.marginBottom = "4px";
      this.fbBtn.addEventListener("click", () => this.cb.onFallback());
      body.appendChild(this.fbBtn);

      body.appendChild(this._mkSlider("white ≥", "whiteMin", 150, 250));
      body.appendChild(this._mkSlider("dark ≤", "darkMax", 30, 140));
      body.appendChild(this._mkSlider("min ms", "overlayMinMs", 400, 3000));
      body.appendChild(this._mkSlider("ms/hanzi", "readMsPerHanzi", 80, 400));

      const vb = this._el("label", "verbose");
      const cbx = document.createElement("input");
      cbx.type = "checkbox";
      cbx.addEventListener("change", () => (this.cfg.verbose = cbx.checked));
      vb.appendChild(cbx);
      vb.appendChild(document.createTextNode("verbose per-sample logging"));
      body.appendChild(vb);

      this.logEl = this._el("div", "log");
      body.appendChild(this.logEl);

      this.body = body;
      p.appendChild(body);
      return p;
    }

    /* Collapse to just the header bar (status chip stays visible, still draggable).
     * Pure UI: never touches the detector or the overlay — translation keeps running. */
    _toggleMin() {
      this.minimized = !this.minimized;
      this.body.style.display = this.minimized ? "none" : "";
      this.minBtn.textContent = this.minimized ? "▢" : "–";
      this.minBtn.title = this.minimized ? "restore" : "minimize";
    }

    _mkSlider(label, key, min, max) {
      const row = this._el("div", "slider");
      row.appendChild(this._el("label", "", label));
      const input = document.createElement("input");
      input.type = "range";
      input.min = min;
      input.max = max;
      input.value = this.cfg[key];
      const val = this._el("span", "val", String(this.cfg[key]));
      input.addEventListener("input", () => {
        this.cfg[key] = +input.value;
        val.textContent = input.value;
      });
      row.appendChild(input);
      row.appendChild(val);
      return row;
    }

    _buildDrawLayer() {
      const layer = this._el("div", "drawlayer");
      layer.appendChild(
        this._el("div", "hint", "Drag a tight box around the subtitle line — Esc to cancel")
      );
      this.drawRect = this._el("div", "drawrect");
      layer.appendChild(this.drawRect);

      let start = null;
      const rect = (a, b) => ({
        x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
        w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y),
      });
      layer.addEventListener("pointerdown", (e) => {
        start = { x: e.clientX, y: e.clientY };
        layer.setPointerCapture(e.pointerId);
      });
      layer.addEventListener("pointermove", (e) => {
        if (!start) return;
        const r = rect(start, { x: e.clientX, y: e.clientY });
        Object.assign(this.drawRect.style, {
          display: "block",
          left: r.x + "px", top: r.y + "px",
          width: r.w + "px", height: r.h + "px",
        });
      });
      layer.addEventListener("pointerup", (e) => {
        const r = start ? rect(start, { x: e.clientX, y: e.clientY }) : null;
        this._endDraw();
        if (!r || r.w < 10 || r.h < 6) return;
        const cr = this.cb.getContentRect();
        if (!cr) return;
        const fx = clamp((r.x - cr.x) / cr.w, 0, 1);
        const fy = clamp((r.y - cr.y) / cr.h, 0, 1);
        const fw = clamp((r.x + r.w - cr.x) / cr.w, 0, 1) - fx;
        const fh = clamp((r.y + r.h - cr.y) / cr.h, 0, 1) - fy;
        if (fw < 0.02 || fh < 0.01) return;
        this.cb.onDrawBox({ x: fx, y: fy, w: fw, h: fh });
      });
      this._escHandler = (e) => {
        if (e.key === "Escape") {
          this._endDraw();
          e.stopPropagation();
        }
      };
      this.drawLayer = layer;
      return layer;
    }

    startDraw() {
      this.drawLayer.style.display = "block";
      window.addEventListener("keydown", this._escHandler, true);
    }

    _endDraw() {
      this.drawLayer.style.display = "none";
      this.drawRect.style.display = "none";
      window.removeEventListener("keydown", this._escHandler, true);
    }

    _makeDraggable(panel, handle) {
      let off = null;
      handle.addEventListener("pointerdown", (e) => {
        if (e.target.classList.contains("x")) return;
        const r = panel.getBoundingClientRect();
        off = { x: e.clientX - r.left, y: e.clientY - r.top };
        panel.style.left = r.left + "px";
        panel.style.top = r.top + "px";
        panel.style.right = "auto";
        handle.setPointerCapture(e.pointerId);
      });
      handle.addEventListener("pointermove", (e) => {
        if (!off) return;
        panel.style.left = clamp(e.clientX - off.x, 0, innerWidth - 60) + "px";
        panel.style.top = clamp(e.clientY - off.y, 0, innerHeight - 30) + "px";
      });
      handle.addEventListener("pointerup", () => (off = null));
    }

    /* Keep the UI visible when the player goes fullscreen: the top layer hides
     * everything outside the fullscreen element, so ride inside it. */
    _attach() {
      const target = document.fullscreenElement || document.documentElement;
      if (this.host.parentNode !== target) target.appendChild(this.host);
    }

    setBoxIndicator(cssRect) {
      if (!cssRect) {
        this.boxind.style.display = "none";
        return;
      }
      Object.assign(this.boxind.style, {
        display: "block",
        left: cssRect.x + "px", top: cssRect.y + "px",
        width: cssRect.w + "px", height: cssRect.h + "px",
      });
    }

    flashRect(cssRect) {
      const f = this._el("div", "flash");
      Object.assign(f.style, {
        left: cssRect.x + "px", top: cssRect.y + "px",
        width: cssRect.w + "px", height: cssRect.h + "px",
      });
      this.root.appendChild(f);
      setTimeout(() => (f.style.opacity = "0"), 350);
      setTimeout(() => f.remove(), 1100);
    }

    showFallbackButton(show, label) {
      this.fbBtn.style.display = show ? "block" : "none";
      if (label) this.fbBtn.textContent = label;
    }

    update(info) {
      this.chip.textContent = info.state;
      this.chip.className = "chip " + info.state;
      this.stat.px.textContent = info.px != null ? info.px : "–";
      this.stat.cl.textContent = info.clusters != null ? info.clusters : "–";
      this.stat.hash.textContent = info.hex ? info.hex.slice(0, 8) : "–";
      this.stat.dcur.textContent =
        info.dCur != null && isFinite(info.dCur) ? info.dCur : "–";
      this.stat.fps.textContent = info.fps != null ? info.fps.toFixed(1) : "–";
      this.stat.mode.textContent = info.mode || "–";
      this.stat.video.textContent = info.videoLabel || "–";
      this.stat.floor.textContent = info.floor != null ? info.floor : "–";
      if (info.svc !== undefined && this.stat.svc) {
        this.stat.svc.textContent = info.svc || "–";
        this.stat.svc.style.color =
          info.svc === "ok" ? "#6fe08a"
          : info.svc === "down" ? "#f07a8a"
          : info.svc === "…" ? "#e8c95a"
          : "#8a93a8";
      }
    }

    /* srcCanvas: the analysis canvas (already holds the downscaled crop). */
    renderDebug(srcCanvas, sample) {
      if (srcCanvas && srcCanvas.width > 0) {
        if (this.cropCv.width !== srcCanvas.width) this.cropCv.width = srcCanvas.width;
        if (this.cropCv.height !== srcCanvas.height) this.cropCv.height = srcCanvas.height;
        this.cropCv.getContext("2d").drawImage(srcCanvas, 0, 0);
      }
      if (!sample || !sample.mask) return;
      const { w, h, mask } = sample;
      if (this.maskCv.width !== w) this.maskCv.width = w;
      if (this.maskCv.height !== h) this.maskCv.height = h;
      const ctx = this.maskCv.getContext("2d");
      const img = ctx.createImageData(w, h);
      const d = img.data;
      for (let i = 0, p = 0; i < mask.length; i++, p += 4) {
        if (mask[i]) {
          d[p] = d[p + 1] = d[p + 2] = 235;
        } else {
          d[p] = 13; d[p + 1] = 14; d[p + 2] = 18;
        }
        d[p + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);
      if (sample.clusters && sample.clusters.length) {
        ctx.strokeStyle = "rgba(90,220,120,.85)";
        ctx.lineWidth = 1;
        for (const c of sample.clusters) {
          ctx.strokeRect(c.x0 + 0.5, c.y0 + 0.5, c.x1 - c.x0 + 1, c.y1 - c.y0 + 1);
        }
      }
    }

    log(line, cls) {
      this.logLines.push({ t: timeTag(), line, cls: cls || "info" });
      if (this.logLines.length > 14) this.logLines.shift();
      this.logEl.innerHTML = this.logLines
        .map((l) => `<div class="ev-${l.cls}">[${l.t}] ${l.line}</div>`)
        .join("");
      this.logEl.scrollTop = this.logEl.scrollHeight;
    }

    destroy() {
      document.removeEventListener("fullscreenchange", this._onFsChange);
      window.removeEventListener("keydown", this._escHandler, true);
      this.host.remove();
    }
  }

  CDT.Panel = Panel;
}
