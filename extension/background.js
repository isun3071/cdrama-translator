/* Background script.
 *
 * Two jobs:
 *  1. Toolbar button -> toggle the detector in the active tab.
 *  2. Proxy the sidecar call for content scripts.
 *
 * The sidecar fetch lives HERE, not in the content script, on purpose: a
 * content script's fetch runs in the page's origin and is subject to the page's
 * Content-Security-Policy (`connect-src`), which on sites like YouTube would
 * block a request to 127.0.0.1 outright. The background page has the extension
 * origin + a host permission for the sidecar, so it is bound by neither the
 * page CSP nor CORS. Content scripts reach the sidecar only through here.
 *
 * Nothing in this file knows what the sidecar *is* (Python now, wasm later) —
 * it only knows the URL and the contract JSON. That keeps the B->A seam intact.
 */

"use strict";

const SIDECAR_BASE = "http://127.0.0.1:8000";
const SIDECAR_URL = SIDECAR_BASE + "/translate";
const LOG_URL = SIDECAR_BASE + "/log";
const REQUEST_TIMEOUT_MS = 6000;

const CONTENT_FILES = [
  "content/util.js",
  "content/capture.js",
  "content/detector.js",
  "content/service.js",
  "content/overlay.js",
  "content/panel.js",
  "content/main.js",
];

async function callSidecar(payload) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(SIDECAR_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      // 422 = contract violation (our bug); other codes = sidecar trouble.
      let detail = "";
      try { detail = JSON.stringify(await resp.json()); } catch (e) {}
      return { ok: false, error: `HTTP ${resp.status}`, detail };
    }
    return { ok: true, data: await resp.json() };
  } catch (e) {
    // Sidecar not running, refused, or timed out.
    return { ok: false, error: e.name === "AbortError" ? "timeout" : "unreachable" };
  } finally {
    clearTimeout(timer);
  }
}

browser.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "cdt-translate") {
    // Returning a Promise makes this an async response (Firefox MV2).
    return callSidecar(msg.payload);
  }
  if (msg && msg.type === "cdt-log-display") {
    // Fire-and-forget audit report; don't make the content script wait.
    fetch(LOG_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.payload),
    }).catch(() => {});
    return undefined;
  }
  return undefined;
});

browser.browserAction.onClicked.addListener(async (tab) => {
  try {
    await browser.tabs.sendMessage(tab.id, { type: "cdt-toggle" });
  } catch (e) {
    // No listener in the tab yet: inject, then toggle.
    try {
      for (const file of CONTENT_FILES) {
        await browser.tabs.executeScript(tab.id, {
          file,
          allFrames: true,
          matchAboutBlank: true,
        });
      }
      await browser.tabs.sendMessage(tab.id, { type: "cdt-toggle" });
    } catch (e2) {
      console.warn("[CDT] cannot run here:", e2.message);
    }
  }
});
