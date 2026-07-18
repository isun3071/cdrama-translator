/* The service seam, content-script side.
 *
 * This is the ONLY place the extension expresses "translate these frames." It
 * hands the contract request to the background (which owns the transport) and
 * returns the contract response. It deliberately knows nothing about HTTP,
 * localhost, Python, or wasm — swapping Shape B for Shape A changes what answers
 * this call, never this call itself. That is invariant: the extension cannot
 * tell B from A.
 */

"use strict";

if (!window.CDT.Service) {
  const CDT = window.CDT;

  CDT.Service = {
    /* frames: array of base64 PNG strings (1-3, spaced t/t+100/t+200).
     * Returns { ok: true, data: <TranslateResponse> } or
     *         { ok: false, error, detail? }. Never throws. */
    async translate({ frames, sourceLang, targetLang, frameId, lastShippedText, contextLines }) {
      try {
        const res = await browser.runtime.sendMessage({
          type: "cdt-translate",
          payload: {
            frames,
            source_lang: sourceLang,
            target_lang: targetLang,
            frame_id: frameId,
            last_shipped_text: lastShippedText || "",
            context_lines: contextLines || [],
          },
        });
        return res || { ok: false, error: "no-response" };
      } catch (e) {
        return { ok: false, error: "messaging", detail: e.message };
      }
    },
  };
}
