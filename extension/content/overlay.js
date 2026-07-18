/* Translation overlay, pinned to the player's content rect (DOCUMENTATION.md §7).
 *
 * Pure rendering: it is handed { source, translation } and shows it; it never
 * decides what to show or when to clear (main.js owns that, driven by the
 * detector's line-start / line-end edges). Keeping source alongside translation
 * means the bilingual view stays a rendering toggle, never a re-architecture
 * (invariant 2).
 *
 * Its own shadow host, re-parented into the fullscreen element like the panel,
 * so it rides along into and out of fullscreen without per-player code.
 */

"use strict";

if (!window.CDT.Overlay) {
  const CDT = window.CDT;

  const CSS = `
    :host { all: initial; }
    .band {
      position: fixed; text-align: center; pointer-events: none;
      display: none; z-index: 1;
      font: 700 24px/1.25 "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    }
    .band .src {
      display: none; color: #dfe3ea; font-size: .62em; font-weight: 600;
      margin-bottom: .15em; text-shadow: 0 1px 3px #000, 0 0 4px #000;
    }
    .band.bilingual .src { display: block; }
    .band .tr {
      color: #fff;
      /* Heavy dark halo so white text stays legible over any video, matching
         how burned-in subtitles are themselves stroked. */
      text-shadow: 0 2px 6px #000, 0 0 3px #000, 1px 1px 0 #000, -1px -1px 0 #000;
    }
    .band .tr .box {
      background: rgba(8,8,10,.42); padding: .1em .5em; border-radius: 4px;
      box-decoration-break: clone; -webkit-box-decoration-break: clone;
    }
    .band .tr .box .tail { transition: opacity .18s ease-in; }
  `;

  class Overlay {
    constructor(cfg) {
      this.cfg = cfg;
      this.state = null; // { source, translation }
      this.rect = null;

      this.host = document.createElement("div");
      this.host.style.cssText =
        "position:fixed;inset:0;pointer-events:none;z-index:2147483646;";
      this.root = this.host.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CSS;
      this.root.appendChild(style);

      this.band = document.createElement("div");
      this.band.className = "band";
      this.srcEl = document.createElement("div");
      this.srcEl.className = "src";
      this.trWrap = document.createElement("div");
      this.trWrap.className = "tr";
      this.trBox = document.createElement("span");
      this.trBox.className = "box";
      this.trPrefix = document.createElement("span"); // already-read, kept in place
      this.trTail = document.createElement("span");   // new continuation, fades in
      this.trTail.className = "tail";
      this.trBox.appendChild(this.trPrefix);
      this.trBox.appendChild(this.trTail);
      this.trWrap.appendChild(this.trBox);
      this._lastTr = "";
      this.band.appendChild(this.srcEl);
      this.band.appendChild(this.trWrap);
      this.root.appendChild(this.band);

      this._attach();
      this._onFs = () => this._attach();
      document.addEventListener("fullscreenchange", this._onFs);
    }

    _attach() {
      const target = document.fullscreenElement || document.documentElement;
      if (this.host.parentNode !== target) target.appendChild(this.host);
    }

    setBilingual(on) {
      this.band.classList.toggle("bilingual", !!on);
    }

    show({ source, translation }) {
      const tr = translation || "";
      this.state = { source, translation };
      this.srcEl.textContent = source || "";
      // Tail-masking (6a): when a continuation revision extends what's already on
      // screen, keep the shared prefix in place and fade in only the new tail, so
      // the viewer doesn't re-read text they've already read. If the revision
      // rephrases the prefix (no shared prefix), fall back to a full render.
      const common = this._commonPrefix(this._lastTr, tr);
      if (this._lastTr && common && common.length < tr.length) {
        if (this.trPrefix.textContent !== common) this.trPrefix.textContent = common;
        this.trTail.textContent = tr.slice(common.length);
        this.trTail.style.opacity = "0";
        void this.trTail.offsetWidth; // reflow so the fade actually runs
        this.trTail.style.opacity = "1";
      } else {
        this.trPrefix.textContent = tr;
        this.trTail.textContent = "";
        this.trTail.style.opacity = "1";
      }
      this._lastTr = tr;
      this.band.style.display = tr ? "block" : "none";
      this._place();
    }

    /* Longest shared prefix of two translations, never splitting a word — so a
     * revision that extends the line reveals only the appended tail. */
    _commonPrefix(a, b) {
      if (!a || !b) return "";
      let i = 0;
      const n = Math.min(a.length, b.length);
      while (i < n && a[i] === b[i]) i++;
      if (i < a.length && i < b.length && a[i] !== " " && b[i] !== " ") {
        while (i > 0 && b[i - 1] !== " ") i--; // divergence mid-word -> back to a space
      }
      return b.slice(0, i);
    }

    clear() {
      this.state = null;
      this._lastTr = "";
      this.band.style.display = "none";
    }

    isShowing() {
      return this.state != null && this.band.style.display !== "none";
    }

    /* cr = player content rect in viewport CSS px (letterbox-aware). Called every
     * tick so the overlay tracks resize, scroll, and fullscreen. */
    reposition(cr) {
      this.rect = cr;
      if (this.isShowing()) this._place();
    }

    _place() {
      const cr = this.rect;
      if (!cr) return;
      const fontPx = Math.max(14, Math.min(44, Math.round(cr.h * 0.052)));
      this.band.style.fontSize = fontPx + "px";
      this.band.style.left = cr.x + cr.w * 0.05 + "px";
      this.band.style.width = cr.w * 0.9 + "px";
      // Anchor the text's baseline near the bottom of the player, where the
      // burned-in line lives, using a viewport-relative bottom offset.
      const bottomGap = Math.max(0, innerHeight - (cr.y + cr.h)) + cr.h * 0.05;
      this.band.style.bottom = bottomGap + "px";
      this.band.style.top = "auto";
    }

    destroy() {
      document.removeEventListener("fullscreenchange", this._onFs);
      this.host.remove();
    }
  }

  CDT.Overlay = Overlay;
}
